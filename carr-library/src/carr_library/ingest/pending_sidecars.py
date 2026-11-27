"""Shared format-4 transport adapter for the existing import producer.

The fixed component contains canonical journals and captured source bytes only.
A restored component is rebound under an incomplete-library marker, then kept
inert until an explicit Import Center review. External scan roots are never read
during verification, restoration or review.
"""
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock, Timeout

from .. import pending_backup as producer
from ..ingest.restore import RestoreError
from ..ingest.retained_assets import RetainedAssets
from ..ingest.pipeline import canonical

DIRECTORY = 'pending-imports'
MAX_BYTES = producer.MAX_TOTAL
MAX_FILES = producer.MAX_FILES + 1
MAX_JOBS = producer.MAX_JOBS
MANIFEST = DIRECTORY + '/manifest.json'


def _tail(name):
    prefix = DIRECTORY + '/'
    if not isinstance(name, str) or not name.startswith(prefix):
        raise RestoreError('invalid pending-import sidecar path')
    tail = name[len(prefix):]
    if tail != 'manifest.json' and not producer._component_name(tail):
        raise RestoreError('unsupported pending-import sidecar')
    return tail


def accepts(name):
    if not isinstance(name, str) or not name.startswith(DIRECTORY + '/'):
        return False
    _tail(name)
    return True


def limit(name):
    tail = _tail(name)
    if tail == 'manifest.json':
        return producer.MAX_COMPONENT_MANIFEST
    if tail == 'import-recovery.json':
        return producer.MAX_RECOVERY
    if tail == 'imports.sqlite':
        return producer.MAX_JOURNAL
    if tail.startswith('originals/'):
        return producer.MAX_BYTES
    if tail.startswith('import-batches/'):
        return producer.MAX_BATCH
    return producer.MAX_REVIEWS if '/reviews/' in tail else producer.MAX_REQUEST


def minimum(name):
    _tail(name)
    return 1


def streamed(name):
    tail = _tail(name)
    return tail == 'imports.sqlite' or tail.startswith('originals/')


def validate_metadata(db, name, raw):
    tail = _tail(name)
    if streamed(name) or type(raw) is not bytes or not 0 < len(raw) <= limit(name):
        raise RestoreError('invalid pending-import metadata')
    value = producer._decode(raw)
    if tail == 'manifest.json':
        producer.validate_pending(value)
    elif tail == 'import-recovery.json':
        from .recovery import validate_document
        validate_document(value, data_dir=value.get('data_dir'))
    elif tail.startswith('import-batches/'):
        producer._batch(value, Path(tail).stem, value.get('data_dir'), db)
    elif tail.startswith('import-requests/reviews/'):
        if type(value) is not dict or len(value) > 1000:
            raise RestoreError('invalid pending-import review metadata')
    else:
        producer._request(value, Path(tail).stem, value.get('data_dir'), db)
    return value


@contextmanager
def _locked(root, names, cancelled):
    # The caller holds exclusive_writer(root). Nonblocking producer locks avoid
    # a lock-order deadlock with a worker already waiting for that writer guard.
    identities = set()
    for name in names:
        if name.startswith('import-batches/'):
            identities.add(root / name.replace('.json', '.lock'))
        elif name.startswith('import-requests/'):
            identities.add(root / 'import-requests' / (Path(name).stem + '.lock'))
    with ExitStack() as stack:
        for path in sorted(identities):
            producer._check(cancelled)
            if os.path.lexists(path):
                producer.regular(path)
            try:
                stack.enter_context(FileLock(path, mode=0o600).acquire(timeout=0))
            except Timeout as exc:
                raise RestoreError('Pause import jobs before taking their complete backup.') from exc
        yield


def snapshot(db, root, stage, *, max_bytes=MAX_BYTES, cancelled=lambda: False):
    root, stage = Path(root), Path(stage)
    producer._check(cancelled)
    before = producer._inventory(root, cancelled)
    if not before and not os.path.lexists(root / 'imports.sqlite'):
        return []
    target = stage / DIRECTORY
    with _locked(root, before, cancelled):
        if producer._inventory(root, cancelled) != before:
            raise RestoreError('Import inventory changed before backup; pause its producer and retry.')
        app = SimpleNamespace(root=root, database=root / 'library.sqlite',
                              assets=RetainedAssets(root / 'originals'))
        metadata = producer.snapshot_pending(app, target, max_bytes=max_bytes, cancelled=cancelled)
        raw = (canonical(metadata) + '\n').encode('utf-8')
        if not 0 < len(raw) <= producer.MAX_COMPONENT_MANIFEST:
            raise RestoreError('Pending-import component manifest exceeds its byte budget.')
        if sum(item['size_bytes'] for item in metadata['files']) + len(raw) > min(MAX_BYTES, max_bytes):
            raise RestoreError('Pending-import component exceeds the remaining backup budget.')
        producer._write(target / 'manifest.json', raw)
        entries = [dict(entry, filename=DIRECTORY + '/' + entry['filename']) for entry in metadata['files']]
        entries.append(dict(filename=MANIFEST, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
        producer._check(cancelled)
        return entries


def _metadata(source, entries):
    entries = tuple(entries)
    if not entries or len(entries) > MAX_FILES:
        raise RestoreError('invalid pending-import component entry count')
    by_name = {}
    total = 0
    for entry in entries:
        name = entry['filename']
        _tail(name)
        if (name in by_name or type(entry['size_bytes']) is not int
                or not 0 < entry['size_bytes'] <= limit(name) or not producer._hash(entry['sha256'])):
            raise RestoreError('invalid pending-import component entry')
        total += entry['size_bytes']
        if total > MAX_BYTES:
            raise RestoreError('pending-import component byte budget exceeded')
        by_name[name] = entry
    manifest = by_name.get(MANIFEST)
    if manifest is None:
        raise RestoreError('pending-import component is missing its manifest')
    raw = producer._read(source / MANIFEST, limit(MANIFEST))
    if len(raw) != manifest['size_bytes'] or sha256(raw).hexdigest() != manifest['sha256']:
        raise RestoreError('pending-import component manifest changed')
    metadata = producer._decode(raw)
    producer.validate_pending(metadata)
    wanted = {DIRECTORY + '/' + entry['filename']: dict(entry, filename=DIRECTORY + '/' + entry['filename'])
              for entry in metadata['files']}
    wanted[MANIFEST] = manifest
    if by_name != wanted:
        raise RestoreError('pending-import component disagrees with the library manifest')
    return metadata


def restore(db, source, stage, entries, *, cancelled=lambda: False, copy_files=True):
    source, stage = Path(source), Path(stage)
    metadata = _metadata(source, entries)
    producer._check(cancelled)
    checked = source
    if copy_files:
        for entry in entries:
            producer._check(cancelled)
            producer._copy(source / entry['filename'], stage / entry['filename'], entry, cancelled)
        checked = stage
    producer.inspect_component(checked / DIRECTORY, metadata, db, cancelled=cancelled)
    producer._check(cancelled)


def _restored_payloads(destination, metadata, records):
    """Use the same checked producer records for initial restore and relocation."""
    from .recovery import FILENAME, restored_document
    requests, batches, reviews, assets, prior, jobs = records
    requests, batches = json.loads(canonical([requests, batches]))
    root = Path(destination)
    original_batches = json.loads(canonical(batches))
    bindings = {(item['filename'], item['row_id']): item for item in metadata['sources']}

    def captured(name, row_id):
        asset = assets[bindings[name, row_id]['asset_sha256']]
        return root / DIRECTORY / 'originals' / asset['filename'], dict(
            sha256=asset['sha256'], mime=asset['mime'], size=asset['size_bytes'])

    for identity, request in requests.items():
        path, manifest = captured('import-requests/' + identity + '.json', None)
        request.update(data_dir=str(root), source=str(path), expected_manifest=manifest)
        # Immutable database receipts remain history. A process-attempt hint is
        # never portable authority after changing root and source bindings.
        request.pop('attempt', None)
        request.pop('publication_candidate', None)
    for identity, batch in batches.items():
        batch['data_dir'] = str(root)
        replacements = {}
        for row in batch['rows']:
            if row['manifest'] is None:
                # An inert rejected path can use another OS's path syntax.
                # Its exact original value remains in the checked component.
                row['source'] = str(root / DIRECTORY / 'rejected' / row['row_id'])
                continue
            path, _ = captured('import-batches/' + identity + '.json', row['row_id'])
            replacements[row['source']] = str(path)
            row['source'] = str(path)
        for row in batch['rows']:
            if row['duplicate_of'] in replacements:
                row['duplicate_of'] = replacements[row['duplicate_of']]
        if 'scan' in batch:
            scan = batch['scan']
            scan.update(roots=[], next_offset=None, next_batch_id=None, unscanned=[])
            scan['inventory_sha256'] = sha256(canonical(
                ['restored-scan-disabled/1', original_batches[identity]['scan']]).encode('utf-8')).hexdigest()
            scan['options_sha256'] = producer._options_digest(batch)
    document = restored_document(root, requests, batches, original_batches, assets, jobs, prior)
    payloads = {FILENAME: (canonical(document) + '\n').encode('utf-8')}
    payloads.update({'import-requests/' + identity + '.json':
                    (canonical(request) + '\n').encode('utf-8')
                    for identity, request in requests.items()})
    payloads.update({'import-batches/' + identity + '.json':
                    (canonical(batch) + '\n').encode('utf-8')
                    for identity, batch in batches.items()})
    return payloads


def _checked_records(root, entries, cancelled):
    from contextlib import closing
    metadata = _metadata(root, entries)
    with closing(producer._catalog(root / 'library.sqlite')) as db:
        records = producer.inspect_component(root / DIRECTORY, metadata, db, cancelled=cancelled)
    return metadata, records


def _journal_entries(metadata):
    return tuple(entry for entry in metadata['files']
                 if entry['filename'] == 'imports.sqlite'
                 or entry['filename'].startswith('import-requests/reviews/'))


def initialize_restored(root, entries, *, cancelled=lambda: False):
    """Bind captured bytes and block execution before the outer restore commits.

    The enclosing restore calls this after its temporary hard links disappear
    and before removing .restore-incomplete. A failure leaves the library closed.
    """
    if not entries:
        return
    root = Path(root)
    metadata, records = _checked_records(root, entries, cancelled)
    # The guard exists first. No restored queue is ever executable during setup.
    for name, raw in _restored_payloads(root, metadata, records).items():
        producer._check(cancelled)
        producer._write(root / name, raw)
    for entry in _journal_entries(metadata):
        producer._copy(root / DIRECTORY / entry['filename'], root / entry['filename'], entry, cancelled)
    producer._check(cancelled)
    producer.sync_directory(root)


def rebind_for_publication(root, destination, entries, *, prior_root, cancelled=lambda: False):
    """Rebind only fresh inert recovery files in private portable staging.

    The captured component and journal bytes remain unchanged. The caller owns
    unpublished staging and publishes it with an exclusive no-replace rename.
    Existing/live libraries and newly approved or edited queues are refused.
    """
    if not entries:
        return
    root = producer.local_path(root, exists=True)
    producer.regular(root, directory=True)
    destination = producer.local_path(destination)
    if os.path.lexists(destination) or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise FileExistsError('Choose a separate new directory for the restored library.')
    metadata, records = _checked_records(root, entries, cancelled)
    previous = _restored_payloads(prior_root, metadata, records)
    journal_entries = _journal_entries(metadata)
    expected = set(previous) | {entry['filename'] for entry in journal_entries
                               if entry['filename'] != 'imports.sqlite'}
    if set(producer._inventory(root, cancelled)) != expected:
        raise RestoreError('Portable import staging has unexpected active recovery files.')
    checked = {}
    for name, raw in previous.items():
        producer._check(cancelled)
        entry = producer._entry(root / name, name, cancelled)
        if entry['size_bytes'] != len(raw) or entry['sha256'] != sha256(raw).hexdigest():
            raise RestoreError('Portable import staging changed or was already approved.')
        checked[name] = entry
    del previous
    for entry in journal_entries:
        producer._check(cancelled)
        if producer._entry(root / entry['filename'], entry['filename'], cancelled) != entry:
            raise RestoreError('Portable import journal changed during recovery.')
    for name, raw in _restored_payloads(destination, metadata, records).items():
        producer._check(cancelled)
        if producer._entry(root / name, name, cancelled) != checked[name]:
            raise RestoreError('Portable import recovery files changed before relocation.')
        # Only exact checked files in unpublished staging are replaced. A
        # failure/cancellation leaves staging unpublished and the live root alone.
        (root / name).unlink()
        producer._write(root / name, raw)
    producer._check(cancelled)
    producer.sync_directory(root)

