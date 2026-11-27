"""Explicit reapproval for restored import queues; never an import executor.

Existing batch and request journals stay authoritative. This small receipt only
blocks their execution until a user reviews their newly owned source bindings.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from .. import pending_backup as producer
from ..assistant.state import atomic_json
from ..ingest.pipeline import canonical

FILENAME = 'import-recovery.json'
MAX_BYTES = producer.MAX_RECOVERY
MAX_ITEMS = producer.MAX_JOBS * 2 + producer.MAX_BATCHES
_ID = re.compile(r'[0-9a-f]{32}\Z')


def _key(kind, identity):
    if (kind not in ('batch', 'request', 'source') or not isinstance(identity, str)
            or not (producer._hash(identity) if kind == 'source' else bool(_ID.fullmatch(identity)))):
        raise ValueError('Invalid restored import selection.')
    return kind + '/' + identity


def binding(kind, record):
    if kind == 'request':
        value = {key: record.get(key) for key in (
            'version', 'request_id', 'data_dir', 'workspace', 'source',
            'metadata', 'config', 'expected_manifest')}
    elif kind == 'batch':
        value = {key: record.get(key) for key in (
            'version', 'batch_id', 'data_dir', 'workspace', 'collections', 'recursive', 'config', 'scan')}
        value['rows'] = [{key: row[key] for key in (
            'row_id', 'work_id', 'edition_id', 'request_id', 'source', 'file', 'title', 'manifest')}
            for row in record['rows']]
    elif kind == 'source':
        value = dict(source=record['source'], manifest=record['manifest'],
            job={key: record['job'][key] for key in (
                'id', 'workspace', 'edition', 'asset', 'config', 'count', 'expected_active')})
    else:
        raise ValueError('Invalid restored import kind.')
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def validate_document(value, *, data_dir):
    if (type(value) is not dict or set(value) != {'version', 'data_dir', 'items'}
            or type(value['version']) is not int or value['version'] != 1
            or value['data_dir'] != data_dir or type(value['items']) is not dict
            or len(value['items']) > MAX_ITEMS):
        raise ValueError('Invalid restored import review receipt.')
    producer._source(data_dir)
    for key, entry in value['items'].items():
        if not isinstance(key, str) or '/' not in key:
            raise ValueError('Invalid restored import receipt identity.')
        kind, identity = key.split('/', 1)
        _key(kind, identity)
        if (type(entry) is not dict or set(entry) != {'workspace', 'binding_sha256', 'approved', 'scan_history'}
                or not producer._text(entry['workspace'], 200)
                or not producer._hash(entry['binding_sha256']) or type(entry['approved']) is not bool):
            raise ValueError('Invalid restored import approval.')
        history = entry['scan_history']
        if history is not None:
            if (kind != 'batch' or type(history) is not dict
                    or set(history) != {'batch_id', 'workspace', 'collections', 'recursive', 'config', 'scan'}
                    or history['batch_id'] != identity or history['workspace'] != entry['workspace']
                    or type(history['collections']) is not list or len(history['collections']) > 128
                    or any(not producer._text(c, 200) for c in history['collections'])
                    or type(history['recursive']) is not bool):
                raise ValueError('Invalid restored scan history.')
            producer._config(history['config'])
            producer._scan(history)
    if len((canonical(value) + '\n').encode('utf-8')) > MAX_BYTES:
        raise ValueError('Restored import receipt exceeds its byte budget.')
    return value


def load(root):
    path = Path(root) / FILENAME
    if not os.path.lexists(path):
        return None
    return validate_document(producer._decode(producer._read(path, MAX_BYTES)), data_dir=str(root))


def require_approval(root, kind, identity, record):
    """Called inside the existing producer's lock, before any resumed work."""
    document = load(root)
    if document is None:
        return
    item = document['items'].get(_key(kind, identity))
    if item is None:
        return  # A newly planned local import is not a restored job.
    if item['workspace'] != record['workspace'] or item['binding_sha256'] != binding(kind, record):
        raise PermissionError('The restored import changed. Review a new import plan in Import Center.')
    if not item['approved']:
        raise PermissionError('This restored import is paused for a new review. '
                              'Choose Review restored imports in Import Center before Start or Resume.')


def restored_document(root, requests, batches, old_batches, assets, jobs, prior=None):
    items = {}
    old_items = {} if prior is None else prior['items']
    for kind, records in (('request', requests), ('batch', batches)):
        for identity, record in records.items():
            key = _key(kind, identity)
            history = None
            if kind == 'batch' and 'scan' in old_batches[identity]:
                previous = old_items.get(key, {}).get('scan_history')
                history = previous or {name: old_batches[identity][name] for name in (
                    'batch_id', 'workspace', 'collections', 'recursive', 'config', 'scan')}
            items[key] = dict(workspace=record['workspace'], binding_sha256=binding(kind, record),
                              approved=False, scan_history=history)
    linked = {(request['workspace'], request['metadata']['edition_id'],
               request.get('expected_manifest', {}).get('sha256')) for request in requests.values()}
    for identity, job in jobs.items():
        if (job['workspace'], job['edition'], job['asset']) in linked or job['state'] in ('complete', 'superseded'):
            continue
        asset = assets[job['asset']]
        source = dict(workspace=job['workspace'], job=job,
            source=str(root / 'pending-imports' / 'originals' / asset['filename']),
            manifest=dict(sha256=asset['sha256'], mime=asset['mime'], size=asset['size_bytes']))
        items[_key('source', identity)] = dict(workspace=job['workspace'],
            binding_sha256=binding('source', source), approved=False, scan_history=None)
    result = dict(version=1, data_dir=str(root), items=items)
    return validate_document(result, data_dir=str(root))


def _guard(app, cancelled):
    from .batches import ImportBatchStore
    store = ImportBatchStore(app)
    halted = False
    def check():
        nonlocal halted
        store._bound()
        halted = halted or bool(cancelled())
        store._bound()
        if halted:
            raise InterruptedError('Restored import review cancelled; no approval was saved.')
    check()
    return store, check


def items(app, workspace):
    store, check = _guard(app, lambda: False)
    with app.read(workspace):
        document = load(app.root)
        if document is None:
            return ()
        result = []
        for key, entry in document['items'].items():
            check()
            if entry['workspace'] == workspace and not entry['approved']:
                kind, identity = key.split('/', 1)
                result.append(dict(kind=kind, identity=identity,
                                   label={'batch': 'Saved batch ', 'request': 'Individual import ',
                                          'source': 'Captured source; new plan required '}[kind] + identity))
        return tuple(sorted(result, key=lambda item: (item['kind'], item['identity'])))


def _record(app, store, db, workspace, kind, identity, *, cancelled=lambda: False):
    if kind == 'batch':
        record = store._load(identity)
        producer._batch(record, identity, str(store.root), db)
    elif kind == 'request':
        from .worker import load_request
        record = load_request(store.root, identity)
        producer._request(record, identity, str(store.root), db)
    else:
        journal, jobs = producer._journal(store.root / 'imports.sqlite', db, cancelled)
        journal.close()
        job = jobs.get(identity)
        if job is None:
            raise ValueError('The captured source journal is missing.')
        metadata = producer._decode(producer._read(
            store.root / 'pending-imports' / 'manifest.json', producer.MAX_COMPONENT_MANIFEST))
        producer.validate_pending(metadata)
        asset = next((value for value in metadata['assets'] if value['sha256'] == job['asset']), None)
        if asset is None:
            raise ValueError('The old import is missing its captured source.')
        record = dict(workspace=job['workspace'], job=job,
            source=str(store.root / 'pending-imports' / 'originals' / asset['filename']),
            manifest=dict(sha256=asset['sha256'], mime=asset['mime'], size=asset['size_bytes']))
    if record['workspace'] != workspace:
        raise PermissionError('The restored import belongs to another library.')
    from ..catalog.membership import Catalog
    collections = record['collections'] if kind == 'batch' else record['metadata']['collections'] if kind == 'request' else ()
    for collection in collections:
        Catalog(db, workspace)._active_collection(collection)
    return record


def _sources(root, kind, record):
    if kind == 'batch':
        rows = [(row['file'], row['source'], row['manifest']) for row in record['rows'] if row['manifest']]
    elif kind == 'request':
        rows = [(record['metadata']['title'], record['source'], record.get('expected_manifest'))]
    else:
        rows = [('Captured source ' + record['job']['id'][:12], record['source'], record['manifest'])]
    for label, source, manifest in rows:
        if manifest is None:
            raise ValueError('The restored source has no captured byte identity.')
        producer._manifest(manifest)
        expected = root / 'pending-imports' / 'originals' / (
            manifest['sha256'] + producer.SUFFIXES[manifest['mime']])
        if source != str(expected):
            raise PermissionError('The restored import must use its owned captured source bytes.')
        yield label, expected, manifest


def approved_request_source(root, record):
    """Return only a freshly approved, exactly bound owned capture, or None.

    Ordinary requests keep their normal retained-asset resolution. A missing
    catalog original must never make them fall back to an external location.
    """
    root = Path(root)
    document = load(root)
    identity = record['request_id']
    if document is None or _key('request', identity) not in document['items']:
        return None
    require_approval(root, 'request', identity, record)
    (_, source, manifest), = tuple(_sources(root, 'request', record))
    if record.get('asset_sha256') not in (None, manifest['sha256']):
        raise PermissionError('The restored request does not match its captured original.')
    return source


def review(app, workspace, kind, identity, *, cancelled=lambda: False):
    key = _key(kind, identity)
    store, check = _guard(app, cancelled)
    with app.read(workspace) as db:
        document = load(app.root)
        entry = (document or {}).get('items', {}).get(key)
        if entry is None or entry['workspace'] != workspace:
            raise PermissionError('Select a restored import from this library.')
        record = _record(app, store, db, workspace, kind, identity, cancelled=cancelled)
        if binding(kind, record) != entry['binding_sha256']:
            raise PermissionError('The restored source selection changed; prepare a new import plan.')
        files, total = [], 0
        for label, path, manifest in _sources(app.root, kind, record):
            check()
            total += manifest['size']
            files.append(dict(file=label, **manifest))
        if len(files) > producer.MAX_FILES or total > producer.MAX_TOTAL:
            raise ValueError('The restored import exceeds its review budget.')
        history = entry['scan_history']
        incomplete = bool(history and (
            history['scan']['next_offset'] is not None or history['scan']['unscanned']))
        collection_ids = (record['collections'] if kind == 'batch' else
                          record['metadata']['collections'] if kind == 'request' else ())
        collections = [dict(id=identity, name=db.execute(
            'SELECT name FROM catalog_collection WHERE workspace=? AND id=?',
            (workspace, identity)).fetchone()[0]) for identity in collection_ids]
        workspace_name = db.execute('SELECT name FROM workspace_meta WHERE workspace=?',
                                    (workspace,)).fetchone()[0]
        value = dict(kind=kind, identity=identity, workspace=workspace, workspace_name=workspace_name,
                     destination_collections=collections,
                     metadata=record['metadata'] if kind == 'request' else None,
                     text_recognition=record['config'] if kind != 'source' else None,
                     binding_sha256=entry['binding_sha256'], files=files,
                     file_count=len(files), source_bytes=total,
                     previous_scan_incomplete=incomplete, continuations_disabled=history is not None,
                     already_approved=entry['approved'], new_import_required=kind == 'source')
        value['checksum'] = sha256(canonical(value).encode('utf-8')).hexdigest()
        check()
        return value


def approve(app, workspace, kind, identity, *, expected_checksum, confirmed=False,
            cancelled=lambda: False):
    if confirmed is not True or not producer._hash(expected_checksum):
        raise PermissionError('Review the exact captured-source report and explicitly confirm it.')
    from ..application import exclusive_writer
    store, check = _guard(app, cancelled)
    with exclusive_writer(app.root):
        check()
        shown = review(app, workspace, kind, identity, cancelled=cancelled)
        if shown['checksum'] != expected_checksum:
            raise ValueError('The displayed restored import changed; open its review again.')
        with app.read(workspace) as db:
            record = _record(app, store, db, workspace, kind, identity, cancelled=cancelled)
            checked = set()
            for _, path, expected in _sources(app.root, kind, record):
                check()
                if expected['sha256'] in checked:
                    continue
                actual = producer.planned_snapshot(path)
                if actual.manifest() != expected:
                    raise ValueError('Captured import source changed; no approval was saved.')
                checked.add(expected['sha256'])
            document = load(app.root)
            key = _key(kind, identity)
            if document['items'][key]['binding_sha256'] != binding(kind, record):
                raise ValueError('The restored import changed during review.')
            new_batch = None
            if kind == 'source':
                # Historical inline imports did not save bibliographic intent.
                # Create an ordinary reviewed queue, with new identities, rather
                # than inventing the old title, work or collection assignment.
                check()
                new_batch = app.plan_import_batch(workspace, (record['source'],),
                                                   recursive=False, cancelled=cancelled)
                check()
            document['items'][key]['approved'] = True
            if kind == 'batch':
                for row in record['rows']:
                    request_key = _key('request', row['request_id'])
                    entry = document['items'].get(request_key)
                    if entry is not None:
                        request = _record(app, store, db, workspace, 'request', row['request_id'], cancelled=cancelled)
                        if entry['binding_sha256'] != binding('request', request):
                            raise ValueError('A prepared request changed during restored batch review.')
                        expected = dict(workspace=workspace, source=row['source'], config=record['config'],
                            expected_manifest=row['manifest'], metadata=json.loads(canonical(store._metadata(row, record))))
                        if any(request.get(k) != v for k, v in expected.items()):
                            raise ValueError('The restored request differs from its batch.')
                        entry['approved'] = True
            validate_document(document, data_dir=str(app.root))
            check()
            atomic_json(app.root / FILENAME, document)
    return dict(kind=kind, identity=identity, approved=True, checksum=expected_checksum,
                batch_id=None if new_batch is None else new_batch['batch_id'],
                message='Captured sources approved. Start or Resume remains a separate action; '
                        'old external scan continuations are disabled.')
