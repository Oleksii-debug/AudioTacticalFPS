"""A bounded group of existing import requests in the one current worker.

There is no second queue or receipt store. Every row retains its own request
lock, frozen PublicationAttempt, journal and atomic catalog publication.
"""
from hashlib import sha256
import math
from pathlib import Path
import re
import sqlite3
from tempfile import gettempdir
from uuid import uuid4

from .resource_budget import checked_budget, effective_budget
from .session import import_session, MAX_SLICE_RECORDS, WRITE_HEADROOM

SCHEMA = 'carr-import-slice/1'
MESSAGE_KEYS = {'action', 'data_dir', 'workspace', 'request_ids', 'requests_sha256',
                'timeout', 'minimum_free'}
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


def _ordered_digest(values):
    from .pipeline import canonical
    value = sha256((SCHEMA + '\n').encode('ascii'))
    for item in values:
        encoded = canonical(item).encode('utf-8')
        value.update(str(len(encoded)).encode('ascii') + b':' + encoded)
    return value.hexdigest()


def _frozen(record):
    from .publication_identity import PublicationAttempt, request_identity
    identity = request_identity(record)
    attempt = PublicationAttempt(record.get('attempt'), identity)
    attempt.request()
    return {'request_id': record['request_id'], 'attempt': attempt.attempt_id,
            'identity': identity}


def _same(record, captured, *, before=False):
    from . import worker
    from .publication_identity import PublicationAttempt, request_identity
    expected = PublicationAttempt(captured['attempt'], captured['identity']).request()
    if (record.get('attempt') != captured['attempt']
            or record.get('request_id') != captured['request_id']):
        raise ValueError('This row was claimed by another import attempt.')
    if before:
        if request_identity(record) != captured['identity']:
            raise ValueError('The frozen import row changed before execution.')
    else:
        if (any(record.get(key) != expected[key] for key in
                ('version', 'request_id', 'data_dir', 'workspace', 'source', 'metadata', 'config'))
                or record.get('expected_manifest') != expected['expected_manifest']
                or record.get('import_budget') != expected.get('import_budget')
                or expected['pinned_asset'] not in (None, record.get('asset_sha256'))
                or expected['resume_job'] not in (None, record.get('job'))):
            raise ValueError('The frozen import row changed during execution.')
    return expected


def _ack(record, captured):
    from .pipeline import canonical
    _same(record, captured)
    result = record.get('result')
    if type(result) is not dict or len(canonical(result).encode('utf-8')) > 64_000:
        raise ValueError('The worker row has no bounded saved result.')
    return {'captured': captured, 'asset': record.get('asset_sha256'),
            'job': record.get('job'), 'state': record.get('state'), 'result': result}


def _limits(records, budget, timeout, broker):
    from . import worker
    from ..extract.page_ocr import config_from_dict
    from ..operations import ResourceRequest, ResourceUnavailable
    if not broker.limits.memory_bytes:
        raise ResourceUnavailable('Configure a positive declared memory allowance before import.')
    policy = effective_budget(budget)
    allowance = policy.seconds if timeout is None else timeout
    if (type(allowance) not in (int, float) or not math.isfinite(allowance)
            or not .1 <= allowance <= (policy.seconds if budget is not None else 600)):
        raise ValueError('The import slice exceeds its reviewed time allowance.')
    pixels = max(config_from_dict(record['config']).max_pixels for record in records)
    minimum_temp = policy.source_bytes + 4 * pixels + worker.MAX_REPLY + worker.MAX_LOG
    temporary = minimum_temp if budget is None else policy.temporary_bytes
    if temporary < minimum_temp:
        raise ResourceUnavailable('The selected temporary allowance does not cover the captured source and parser working space.')
    memory = broker.limits.memory_bytes if budget is None else policy.memory_bytes
    request = ResourceRequest(cpu_slots=broker.limits.cpu_slots, memory_bytes=memory,
        disk_path=Path(records[0]['data_dir']), disk_bytes=policy.library_bytes + WRITE_HEADROOM,
        additional_disks=((Path(gettempdir()), temporary),))
    return allowance, request, temporary


def run_slice(root, workspace, request_ids, *, cancelled=lambda: False,
              timeout=None, import_budget=None, _app=None):
    """Run one explicit bounded slice, then reconcile only its original attempts."""
    from . import worker
    from .publication_identity import request_identity
    from ..operations import (ResourceBroker, ResourceCancelled, ResourceTimeout, ResourceError)
    checked_budget(import_budget)
    if (not callable(cancelled) or not isinstance(request_ids, (tuple, list))
            or not 1 <= len(request_ids) <= MAX_SLICE_RECORDS
            or any(type(value) is not str or not _ID.fullmatch(value) for value in request_ids)
            or len(set(request_ids)) != len(request_ids)):
        raise ValueError('Choose one bounded, distinct ordered import slice.')
    captured, originals = [], []
    for request_id in request_ids:
        if cancelled():
            return tuple({'state': 'cancelled', 'request_id': item, 'isolated': True,
                          'message': 'Import slice cancelled before launch.'} for item in request_ids)
        with worker.request_lock(root, request_id):
            record = worker.load_request(root, request_id)
            if record['workspace'] != workspace or worker._budget_from_record(record) != import_budget:
                raise ValueError('The batch slice scope or reviewed resource profile changed.')
            # Read-only preflight. A pending previous native owner retains
            # admission, so no new attempt may be stored while waiting for it.
            request_identity(record)
            originals.append(record)
    reply = None
    try:
        broker = ResourceBroker.for_app(root, cancelled=cancelled)
        allowance, reservation, temporary = _limits(originals, import_budget, timeout, broker)
        with worker.import_admission(broker, reservation, operation_id='import-slice:' + uuid4().hex,
                            cancelled=cancelled, timeout=min(60, allowance)) as lease:
            for index, request_id in enumerate(request_ids):
                if lease.cancelled():
                    return tuple({'state': 'cancelled', 'request_id': item, 'isolated': True,
                        'message': 'Import slice cancelled before launch.'} for item in request_ids)
                with worker.request_lock(root, request_id):
                    current = worker.load_request(root, request_id)
                    previous = originals[index]
                    if (request_identity(current) != request_identity(previous)
                            or current.get('attempt') != previous.get('attempt')):
                        raise ValueError('An import row changed while waiting for slice admission.')
                    current = dict(current, attempt=uuid4().hex, publication_candidate=None)
                    worker.record_progress(root, request_id, attempt=current['attempt'],
                                           publication_candidate=None)
                    captured.append(_frozen(current))
                    originals[index] = current
            # The child reauthorizes the same workspace before each publication.
            reply = worker._invoke({'action': SCHEMA, 'data_dir': str(Path(root).resolve()),
                'workspace': workspace, 'request_ids': list(request_ids),
                'requests_sha256': _ordered_digest(captured), 'timeout': allowance,
                'minimum_free': broker.limits.min_free_disk_bytes},
                timeout=allowance, cancelled=lease.cancelled, owned_tree=True,
                staging_bytes=temporary)
    except ResourceCancelled:
        reply = {'state': 'cancelled', 'message': 'Import slice cancelled before resource admission.'}
    except ResourceTimeout:
        reply = {'state': 'timed_out', 'message': 'The saved import slice is waiting for local resources.'}
    except ResourceError as exc:
        reply = {'state': 'failed', 'message': str(exc)[:2000]}
    if not captured:
        # Denied admission has no authority to replace prior request state.
        return tuple(dict(reply, request_id=item, isolated=True) for item in request_ids)
    # A successful child's single digest acknowledges exactly its completed
    # prefix without placing thousands of results in the bounded stdout reply.
    acknowledged = 0
    acknowledged_values = ()
    if (type(reply) is dict and set(reply) == {'state', 'visited', 'results_sha256', 'message'}
            and reply['state'] == 'slice' and type(reply['visited']) is int
            and 0 <= reply['visited'] <= len(captured)
            and type(reply['results_sha256']) is str and _HASH.fullmatch(reply['results_sha256'])):
        try:
            values = []
            for frozen in captured[:reply['visited']]:
                with worker.request_lock(root, frozen['request_id']):
                    values.append(_ack(worker.load_request(root, frozen['request_id']), frozen))
            if _ordered_digest(values) != reply['results_sha256']:
                raise ValueError('The saved slice results changed after worker completion.')
            acknowledged = reply['visited']
            acknowledged_values = tuple(values)
        except (ValueError, OSError, TypeError, KeyError, sqlite3.Error):
            acknowledged = 0
    from .reconciliation import reconciliation
    from ..application import LibraryApp
    app = LibraryApp(root) if _app is None else _app
    if type(app) is not LibraryApp or app.root != Path(root).resolve():
        raise ValueError('The bounded slice reconciliation application changed.')
    with reconciliation(app, workspace) as reconciler:
        results = []
        for index, (frozen, initial) in enumerate(zip(captured, originals, strict=True)):
            request_id = frozen['request_id']
            try:
                with worker.request_lock(root, request_id):
                    current = worker.load_request(root, request_id)
                    _same(current, frozen)
                    # Fresh canonical receipts override a late cancellation/error.
                    # A sidecar hint alone is never treated as completion.
                    committed = worker._reconcile_publication(root, request_id, frozen['attempt'],
                            initial=initial, _context=reconciler)
                    if committed is not None:
                        result = dict(committed, request_id=request_id, isolated=True)
                    elif index < acknowledged:
                        # Keep the exact validated value across the lock boundary.
                        # An equal-attempt sidecar rewrite is not the child's reply.
                        if _ack(current, frozen) != acknowledged_values[index]:
                            raise ValueError('The acknowledged import row changed before reconciliation.')
                        result = dict(acknowledged_values[index]['result'])
                    else:
                        state = reply.get('state') if type(reply) is dict else 'failed'
                        if current.get('state') != 'running':
                            state = 'cancelled' if state == 'cancelled' else 'pending'
                        elif state not in ('cancelled', 'timed_out', 'failed'):
                            state = 'failed'
                        result = dict(state=state, request_id=request_id, isolated=True,
                            message=str((reply.get('message') if type(reply) is dict else None) or
                                'This row has no acknowledged worker result; resume verifies its saved checkpoints.')[:2000])
                    reconciler.check(root, workspace)
                    worker.record_progress(root, request_id, state=result['state'], result=result)
            except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
                # In particular, never overwrite a newer competing attempt.
                result = dict(state='failed', request_id=request_id, isolated=True,
                              message=str(exc)[:2000])
            results.append(result)
    return tuple(results)


def run_child(message):
    """Called only by the current worker after its native/network admission."""
    from . import worker
    from .publication_identity import PublicationAttempt
    from ..application import LibraryApp
    from ..extract.page_ocr import config_from_dict
    from ..audio.models import AudioCleanupPending
    if (type(message) is not dict or set(message) != MESSAGE_KEYS or message['action'] != SCHEMA
            or type(message['workspace']) is not str
            or type(message['request_ids']) is not list
            or not 1 <= len(message['request_ids']) <= MAX_SLICE_RECORDS
            or any(type(value) is not str or not _ID.fullmatch(value) for value in message['request_ids'])
            or len(set(message['request_ids'])) != len(message['request_ids'])
            or type(message['requests_sha256']) is not str or not _HASH.fullmatch(message['requests_sha256'])
            or type(message['minimum_free']) is not int or not 0 <= message['minimum_free'] <= 2**63 - 1):
        raise ValueError('Invalid bounded import slice.')
    records = [worker.load_request(message['data_dir'], identity) for identity in message['request_ids']]
    captured = [_frozen(record) for record in records]
    if _ordered_digest(captured) != message['requests_sha256']:
        raise ValueError('The ordered frozen import slice changed before execution.')
    budget = worker._budget_from_record(records[0])
    if any(record['workspace'] != message['workspace']
           or worker._budget_from_record(record) != budget for record in records):
        raise ValueError('The import slice mixes scopes or reviewed budgets.')
    acknowledged, note = [], ''
    app = LibraryApp(message['data_dir'])
    with import_session(app, message['workspace'], budget,
                        minimum_free=message['minimum_free']) as session:
        for frozen in captured:
            try:
                with worker.request_lock(app.root, frozen['request_id']):
                    record = worker.load_request(app.root, frozen['request_id'])
                    _same(record, frozen, before=True)
                    # Nothing above this exact row check writes the request or
                    # authorizes a source. The session only validates existing DBs.
                    attempt = PublicationAttempt(frozen['attempt'], frozen['identity'])
                    metadata = dict(record['metadata'])
                    metadata['authors'] = tuple(metadata['authors'])
                    metadata['collections'] = tuple(metadata['collections'])
                    session.validate(app, record['workspace'], budget)
                    worker.record_progress(app.root, record['request_id'], state='running')
                    source = record['source']
                    if record.get('asset_sha256'):
                        source = app.assets.resolve(record['asset_sha256'],
                            max_bytes=effective_budget(budget).source_bytes)
                    try:
                        result = app._import_file(record['workspace'], source, **metadata,
                            config=config_from_dict(record['config']), _request_id=record['request_id'],
                            _resume_job=record.get('job'), _expected_manifest=record.get('expected_manifest'),
                            _publication_attempt=attempt, import_budget=budget, _ingest_session=session)
                    except AudioCleanupPending:
                        raise
                    except Exception as exc:
                        # Only the parent, after actual family closure, can
                        # reconcile a late committed receipt. Do not reopen the
                        # library or claim completion while this worker owns it.
                        result = {'state': 'failed', 'message': str(exc)[:2000]}
                    result = dict(result, request_id=record['request_id'], isolated=True)
                    # Terminal completion is acknowledged by the parent only
                    # after its actual process family has closed. The child may
                    # persist the bounded receipt/result, but cannot expose a
                    # direct-resume terminal fast path during pending cleanup.
                    saved_state = ('running' if result['state'] in ('complete', 'superseded')
                                   else result['state'])
                    worker.record_progress(app.root, record['request_id'], state=saved_state, result=result)
                    saved = worker.load_request(app.root, record['request_id'])
                    acknowledged.append(_ack(saved, frozen))
                    if result['state'] not in ('complete', 'superseded'):
                        note = 'The current source needs another explicit work slice or review.'
                        break
            except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as exc:
                note = str(exc)[:2000]
                break
    return {'state': 'slice', 'visited': len(acknowledged),
            'results_sha256': _ordered_digest(acknowledged), 'message': note}
