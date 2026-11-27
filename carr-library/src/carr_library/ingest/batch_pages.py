"""Explicit bounded scan pages over the ordinary ImportBatchStore journals.

This module adds no queue, worker or automatic execution. Each page is an
ordinary batch with the existing row/request identities and import methods.
The extra metadata is a passive traversal continuation and scoped row links.
"""
from contextlib import ExitStack
from hashlib import sha256
import json
import os
import re
import time

from .batch_scan import (new_scan_cursor, scan_cursor_digest, scan_page,
                         scan_page_limits, validate_scan_cursor)

_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_REFERENCE = re.compile(r'batch:([0-9a-f]{32}):([0-9a-f]{32})\Z')
_SCAN_KEYS = {'version', 'session_id', 'page', 'previous', 'next', 'start_sha256',
              'cursor', 'reason', 'limits', 'review_required', 'classification_complete'}
_REASONS = {'complete', 'files', 'entries', 'time', 'bytes', 'cancelled', 'admission'}


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def page_id(session_id, index):
    if (type(session_id) is not str or not _ID.fullmatch(session_id)
            or type(index) is not int or index < 0):
        raise ValueError('Invalid import scan page identity.')
    return session_id if index == 0 else sha256(
        ('carr-import-page/1:' + session_id + ':' + str(index)).encode('ascii')).hexdigest()[:32]


def reference(value):
    return _REFERENCE.fullmatch(value) if type(value) is str else None


def batch_shape(record, legacy_keys):
    return (type(record) is dict and type(record.get('version')) is int
            and (record['version'] == 1 and set(record) == legacy_keys
                 or record['version'] == 2 and set(record) == legacy_keys | {'scan'}))


def validate_scan_metadata(record):
    """Validate data only, including after backup restore; never open scan roots."""
    if record['version'] == 1:
        return
    from .batches import MAX_BATCHES
    scan = record.get('scan')
    if (type(scan) is not dict or set(scan) != _SCAN_KEYS
            or type(scan['version']) is not int or scan['version'] != 1
            or type(scan['session_id']) is not str or not _ID.fullmatch(scan['session_id'])
            or type(scan['page']) is not int or not 0 <= scan['page'] < MAX_BATCHES
            or type(scan['start_sha256']) is not str or not _HASH.fullmatch(scan['start_sha256'])
            or type(scan['review_required']) is not bool
            or type(scan['classification_complete']) is not bool
            or type(scan['reason']) is not str or scan['reason'] not in _REASONS
            or type(scan['limits']) is not dict or set(scan['limits']) != {
                'max_files', 'max_entries', 'timeout', 'max_bytes'}):
        raise ValueError('Invalid bounded import scan page metadata.')
    limits = scan_page_limits(**scan['limits'])
    if limits != scan['limits'] or len(record['rows']) > limits['max_files']:
        raise ValueError('Import scan page differs from its saved resource allowances.')
    cursor = validate_scan_cursor(scan['cursor'])
    if cursor['recursive'] != record['recursive'] or record['batch_id'] != page_id(scan['session_id'], scan['page']):
        raise ValueError('Import scan page lost its exact queue or recursion binding.')
    previous = None if scan['page'] == 0 else page_id(scan['session_id'], scan['page'] - 1)
    following = None if cursor['complete'] else page_id(scan['session_id'], scan['page'] + 1)
    if scan['previous'] != previous or scan['next'] != following:
        raise ValueError('Import scan page predecessor/continuation identities disagree.')
    if cursor['complete'] != (scan['reason'] == 'complete'):
        raise ValueError('Import scan completion differs from its traversal.')
    if scan['page'] == 0:
        initial = dict(_copy(cursor), root_index=0, stack=[], visited=0, planned=0, complete=False)
        if scan['start_sha256'] != scan_cursor_digest(initial) or cursor['planned'] != len(record['rows']):
            raise ValueError('First import scan page lost its initial selection.')
    for row in record['rows']:
        duplicate = row.get('duplicate_of')
        target = reference(duplicate)
        if isinstance(duplicate, str) and duplicate.startswith('batch:'):
            if target is None or row['manifest'] is None:
                raise ValueError('Invalid exact cross-page duplicate reference.')


def validate_scan_link(parent, child):
    validate_scan_metadata(parent)
    validate_scan_metadata(child)
    if parent['version'] != 2 or child['version'] != 2:
        raise ValueError('Import continuation needs two pages from the same saved scan.')
    before, after = parent['scan'], child['scan']
    if (after['previous'] != parent['batch_id'] or before['next'] != child['batch_id']
            or after['session_id'] != before['session_id'] or after['page'] != before['page'] + 1
            or after['start_sha256'] != scan_cursor_digest(before['cursor'])
            or any(child[key] != parent[key] for key in
                   ('workspace', 'data_dir', 'collections', 'recursive', 'config'))
            or after['cursor']['roots'] != before['cursor']['roots']
            or after['cursor']['visited'] < before['cursor']['visited']
            or after['cursor']['planned'] != before['cursor']['planned'] + len(child['rows'])):
        raise ValueError('Import continuation differs from its preceding saved page.')


def validate_scan_relations(batches):
    """Pure backup relation check; row references never grant external reads."""
    for batch in batches.values():
        if batch['version'] != 2:
            continue
        validate_scan_metadata(batch)
        scan = batch['scan']
        if scan['previous'] is not None:
            parent = batches.get(scan['previous'])
            if parent is None:
                raise ValueError('A scan continuation is missing its preceding page.')
            validate_scan_link(parent, batch)
        for row in batch['rows']:
            target = reference(row['duplicate_of'])
            if target is None:
                continue
            prior = batches.get(target[1])
            if (prior is None or prior['version'] != 2
                    or prior['scan']['session_id'] != scan['session_id']
                    or prior['scan']['page'] >= scan['page']
                    or any(prior[key] != batch[key] for key in
                           ('workspace', 'data_dir', 'collections', 'recursive', 'config'))):
                raise ValueError('Cross-page duplicate belongs to another scan or scope.')
            original = next((item for item in prior['rows'] if item['row_id'] == target[2]), None)
            if original is None or original['manifest'] is None or original['manifest'] != row['manifest']:
                raise ValueError('Cross-page duplicate lost its exact planned original.')


def ancestors(store, record):
    """Read one bounded journal at a time; never accumulate all prior pages."""
    from .batches import MAX_BATCHES
    current, count = record, 0
    while current['version'] == 2 and current['scan']['previous'] is not None:
        count += 1
        if count >= MAX_BATCHES:
            raise ValueError('Import scan history exceeds its journal allowance.')
        previous = store._load(current['scan']['previous'])
        validate_scan_link(previous, current)
        yield previous
        current = previous


def referenced_row(store, record, value, manifest):
    target = reference(value)
    if target is None or record['version'] != 2:
        return False
    for prior in ancestors(store, record):
        if prior['batch_id'] == target[1]:
            return any(row['row_id'] == target[2] and row['manifest'] == manifest for row in prior['rows'])
    return False


def _classify(store, workspace, rows, previous, stopped):
    """Bound catalog lookups to this page; retain a visible deferred-check state."""
    selected = {row['manifest']['sha256'] for row in rows if row['manifest'] is not None}
    known = set()
    ordered = sorted(selected)
    for offset in range(0, len(ordered), 256):
        if stopped():
            return False
        group = ordered[offset:offset + 256]
        with store.app.read(workspace) as db:
            known.update(row[0] for row in db.execute(
                'SELECT DISTINCT asset FROM catalog_edition WHERE workspace=? AND asset IN ('
                + ','.join('?' for _ in group) + ')', (workspace, *group)))
    for row in rows:
        if row['manifest'] is not None and row['manifest']['sha256'] in known:
            row.update(state='duplicate', duplicate_of='existing:' + row['manifest']['sha256'],
                       message='Exact source bytes already exist in this library.')
    wanted, earlier = selected - known, {}
    if previous is not None and wanted:
        def records():
            yield previous
            yield from ancestors(store, previous)
        for prior in records():
            if stopped():
                return False
            for old in prior['rows']:
                manifest = old['manifest']
                if manifest is not None and manifest['sha256'] in wanted:
                    # Walking backwards deliberately selects the earliest
                    # retained row if several prior pages share these bytes.
                    key = (manifest['sha256'], manifest['mime'], manifest['size'])
                    earlier[key] = 'batch:' + prior['batch_id'] + ':' + old['row_id']
        for row in rows:
            manifest = row['manifest']
            key = (manifest['sha256'], manifest['mime'], manifest['size']) if manifest is not None else None
            if key in earlier:
                row.update(state='duplicate', duplicate_of=earlier[key],
                           message='Exact source bytes match an earlier saved page of this scan.')
    return True


def _options(collections, recursive, config, cancelled):
    from dataclasses import asdict
    from ..extract.page_ocr import Config
    if (not isinstance(collections, (list, tuple)) or len(collections) > 128
            or type(recursive) is not bool or not callable(cancelled)):
        raise ValueError('Invalid import scan-page options.')
    values = tuple(dict.fromkeys(collections))
    chosen = config or Config()
    settings = asdict(chosen)
    Config(**settings)
    return values, settings


def _make(store, workspace, cursor, *, session_id, index, previous,
          collections, config, limits, cancelled):
    from uuid import uuid4
    from .batches import MAX_BATCHES, _now
    from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled, ResourceTimeout
    identity = page_id(session_id, index)
    if index >= MAX_BATCHES:
        raise ValueError('This scan reached the saved-journal allowance. Retain/export reviewed pages before a new scan.')
    store._authorize(workspace, collections)
    started = time.monotonic()
    with store._lock(identity):
        path = store._path(identity)
        if os.path.lexists(path):
            existing = store._load(identity)
            if previous is None:
                raise ValueError('This import scan identity already exists.')
            validate_scan_link(previous, existing)
            return store._reconcile(existing)
        # This is the same existing journal budget, not a file-count ceiling.
        journals = 0
        with os.scandir(store._directory()) as entries:
            for entry in entries:
                journals += entry.name.endswith('.json')
                if journals >= MAX_BATCHES:
                    raise ValueError('The saved import-journal allowance is full; existing plans were preserved.')
        result = {'rows': (), 'cursor': _copy(cursor), 'reason': 'admission'}
        rows, complete_classification = [], False
        broker = ResourceBroker.for_app(store.root)
        with ExitStack() as lifetime:
            try:
                lease = lifetime.enter_context(broker.reserve(
                    ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2),
                    operation_id='import-scan:' + identity, cancelled=cancelled,
                    timeout=min(60, limits['timeout'])))
            except (ResourceCancelled, ResourceTimeout):
                pass  # Persist an inert start cursor; no source was opened.
            else:
                store._authorize(workspace, collections)
                remaining = limits['timeout'] - max(0, time.monotonic() - started)
                if remaining < .05:
                    result['reason'] = 'time'
                else:
                    result = scan_page(cursor=cursor, recursive=cursor['recursive'],
                        cancelled=lease.cancelled, **dict(limits, timeout=remaining))
                rows = [dict(row, row_id=uuid4().hex, work_id=uuid4().hex,
                             edition_id=uuid4().hex, request_id=uuid4().hex, result=None)
                        for row in result['rows']]
                complete_classification = _classify(store, workspace, rows, previous,
                    lambda: lease.cancelled() or time.monotonic() - started >= limits['timeout'])
            store._authorize(workspace, collections)
            if not complete_classification:
                for row in rows:
                    if row['state'] == 'queued':
                        row['message'] = ('Source identity reviewed. Duplicate checks were paused; '
                                          'the catalog is checked again before import.')
            if previous is not None and not rows and result['cursor'] == cursor:
                # Do not append empty pages forever when the user must raise the
                # time/entry allowance or release the shared resource reservation.
                raise InterruptedError('No scan progress was admitted. The saved continuation is unchanged; retry with an available budget.')
            record = dict(version=2, batch_id=identity, data_dir=str(store.root),
                workspace=workspace, created=_now(), updated=_now(), state='queued',
                collections=list(collections), recursive=cursor['recursive'], config=config,
                rows=rows, counts={}, total=len(rows),
                scan={'version': 1, 'session_id': session_id, 'page': index,
                    'previous': None if previous is None else previous['batch_id'],
                    'next': None if result['cursor']['complete'] else page_id(session_id, index + 1),
                    'start_sha256': scan_cursor_digest(cursor), 'cursor': result['cursor'],
                    'reason': result['reason'], 'limits': limits, 'review_required': False,
                    'classification_complete': complete_classification})
            if previous is not None:
                validate_scan_link(previous, record)
            store._store(record, create=True, paused=result['reason'] in ('cancelled', 'time', 'admission'))
            return _copy(record)


def plan_page(store, workspace, paths, *, collections=(), recursive=True, config=None,
              cancelled=lambda: False, max_files=1000, max_entries=20000,
              timeout=60, max_bytes=4 * 1024**2):
    from uuid import uuid4
    collections, settings = _options(collections, recursive, config, cancelled)
    limits = scan_page_limits(max_files=max_files, max_entries=max_entries,
                             timeout=timeout, max_bytes=max_bytes)
    cursor = new_scan_cursor(paths, recursive=recursive)
    return _make(store, workspace, cursor, session_id=uuid4().hex, index=0, previous=None,
                 collections=collections, config=settings, limits=limits, cancelled=cancelled)


def continue_plan(store, workspace, batch_id, *, cancelled=lambda: False,
                  max_files=1000, max_entries=20000, timeout=60, max_bytes=4 * 1024**2):
    limits = scan_page_limits(max_files=max_files, max_entries=max_entries,
                             timeout=timeout, max_bytes=max_bytes)
    if not callable(cancelled):
        raise ValueError('Scan cancellation must be callable.')
    store._authorize(workspace)
    with store._lock(batch_id):
        previous = store.get(workspace, batch_id)
        if previous['version'] != 2:
            raise ValueError('This legacy batch has no saved scan cursor. Choose its original folders for a new paged scan.')
        scan = previous['scan']
        if scan['review_required']:
            raise ValueError('Restored scan roots require a fresh explicit selection. Saved page rows remain available.')
        if scan['cursor']['complete']:
            return _copy(previous)
        return _make(store, workspace, scan['cursor'], session_id=scan['session_id'],
            index=scan['page'] + 1, previous=previous, collections=tuple(previous['collections']),
            config=previous['config'], limits=limits, cancelled=cancelled)
