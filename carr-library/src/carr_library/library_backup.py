"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research, registered original files and
explicitly validated reader/editor/settings sidecars. Unknown application state
is refused, never silently discarded. The canonical pending-import producer
preserves journals, requests, batch/review identities and required original bytes.
Restore rebinds locations without executing or automatically resuming a job.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Format 4 unifies registered sidecars. Exact legacy formats 1/2 and the three
previous format-3 layouts and exact historical state formats 4–6 are read
without rewriting the source backup. New media uses registered format-4 sidecars;
its immutable receipts declare retained inputs and excluded external outputs.
Database snapshots require an exact supported V3, V4 or V5 schema. Older databases
are restored unchanged; opening the library performs the checked current-schema upgrade.
Restoration always creates a new data directory and never merges or overwrites.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from .ingest.bundle import _plain, _unique_object
from .ingest.catalog_asset_copy import catalog_inventory, _checked_path, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer
from .data_paths import guard_data_directory
from .durable_state import local_path
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from . import backup_sidecars, pending_backup

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 8 * 1024 * 1024
MAX_DATABASE = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3


def _destination(value, source):
    path = local_path(guard_data_directory(value, purpose='backup or restored library'))
    _plain(path.parent, directory=True)
    if os.path.lexists(path):
        raise FileExistsError(path)
    path = path.parent.resolve() / path.name
    source = Path(source).resolve(strict=True)
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise RestoreError('destination must be outside the source directory')
    return path


def _asset_manifest(entries):
    return [dict(filename=e.filename, sha256=e.digest, mime=e.mime, size_bytes=e.size)
            for e in entries]


def _sync_file(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    # Windows lacks a Python directory-fsync handle. Every file is separately
    # flushed; on POSIX also persist namespace changes before completion.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _check_budget(total, maximum):
    if maximum is not None and total > maximum:
        raise RestoreError('complete backup exceeds the requested byte budget')


def _copy_originals(db, source, destination, *, max_bytes=MAX_ASSET_BYTES, cancelled=lambda: False):
    """Use the canonical catalog validator, with bounded cancellable file copying."""
    limits = dict(include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=max(1, max_bytes))
    entries = catalog_inventory(db, **limits)
    _check_budget(sum(entry.size for entry in entries), max_bytes)
    destination.mkdir(mode=0o700)
    for entry in entries:
        backup_sidecars._check_cancelled(cancelled)
        path = _checked_path(source, entry)
        count, digest = 0, sha256()
        with path.open('rb') as incoming, (destination / entry.filename).open('xb') as output:
            while block := incoming.read(min(1024 * 1024, entry.size + 1 - count)):
                backup_sidecars._check_cancelled(cancelled)
                count += len(block)
                if count > entry.size:
                    raise RestoreError('original file grew during backup')
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if count != entry.size or digest.hexdigest() != entry.digest:
            raise RestoreError('original file changed during backup')
    backup_sidecars._check_cancelled(cancelled)
    return verify_catalog_assets(db, RetainedAssets(destination), **limits)


def _check_backup_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Backup or restoration cancelled.')


def _required_media_input(component, kind, reference):
    if (not isinstance(reference, str) or not reference or len(reference) > 32768
            or any(ord(c) < 32 for c in reference)):
        raise RestoreError('Invalid declared external Studio input.')
    return dict(component=component, kind=kind, reference=reference, included=False)


def _ordered_media_inputs(values):
    unique = {json.dumps(item, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')): item for item in values}
    return [unique[key] for key in sorted(unique)]


def _inspect_studio_media(root, entries, *, cancelled=lambda: False):
    """Inspect verified staged files, without opening recorded external paths."""
    from .durable_state import read_json
    from .studio.backup_inputs import inspect_snapshot_inputs, _record

    root = Path(root)
    by_name = {entry['filename']: entry for entry in entries}
    receipt = inspect_snapshot_inputs(root, entries, cancelled=cancelled)
    media = dict(schema=1, studio_input_receipt=receipt is not None,
                 studio_publications=[], studio_recordings=[],
                 studio_unverified_recordings=[], required_inputs=[])
    required = []
    if receipt is not None:
        for binding in receipt['projects']:
            _check_backup_cancelled(cancelled)
            found = _record(root, binding['filename'])
            if found is None:
                raise RestoreError('A verified Studio project binding disappeared.')
            project = found[2]
            component = 'state/' + binding['filename']
            for role in project.roles:
                if role.voice_id:
                    required.append(_required_media_input(component, 'local-voice', role.voice_id))
            for scene in project.scenes:
                if scene.music is not None and scene.music.engine != 'procedural':
                    required.append(_required_media_input(component, 'music-engine', scene.music.engine))
    for name in sorted(by_name):
        _check_backup_cancelled(cancelled)
        if name.casefold().startswith('studio/publications/'):
            if not re.fullmatch(r'studio/publications/[0-9a-f]{32}\.json', name):
                raise RestoreError('Invalid typed Studio publication filename.')
            from .studio.publishing import validate_publication_receipt
            record = validate_publication_receipt(read_json(root / name),
                publication_id=Path(name).stem)
            media['studio_publications'].append(name)
            required.append(_required_media_input('state/' + name, 'publication-output', record['output']))
        elif name.casefold().startswith('studio/recordings/'):
            if re.fullmatch(r'studio/recordings/[0-9a-f]{32}\.json', name):
                from .audio.recording import validate_recording_receipt
                record = validate_recording_receipt(read_json(root / name),
                    recording_id=Path(name).stem)
                audio = by_name.get(name.removesuffix('.json') + '.wav')
                if (audio is None or audio['size_bytes'] != record['audio']['size_bytes']
                        or audio['sha256'] != record['audio']['sha256']):
                    raise RestoreError('A recording receipt differs from its retained original.')
                from .speech.runtime import _wave_info
                expected_pcm = tuple(record['audio'][key] for key in
                    ('sample_rate', 'channels', 'frames', 'sample_width'))
                if _wave_info(root / audio['filename'], cancelled=cancelled,
                        max_output_bytes=record['limits']['max_bytes']) != expected_pcm:
                    raise RestoreError('A recording receipt differs from its retained PCM header.')
                media['studio_recordings'].append(name)
            elif re.fullmatch(r'studio/recordings/[0-9a-f]{32}\.wav', name):
                if name.removesuffix('.wav') + '.json' not in by_name:
                    media['studio_unverified_recordings'].append(name)
            else:
                raise RestoreError('Invalid typed Studio recording filename.')
    media['required_inputs'] = _ordered_media_inputs(required)
    return media


def _validate_studio_media(media, entries):
    """Validate the exact V6 envelope; staged content is checked separately."""
    from .state_backup import MAX_STATE_FILES

    fields = {'schema', 'studio_input_receipt', 'studio_publications',
              'studio_recordings', 'studio_unverified_recordings', 'required_inputs'}
    if (type(media) is not dict or set(media) != fields
            or type(media['schema']) is not int or media['schema'] != 1
            or type(media['studio_input_receipt']) is not bool):
        raise RestoreError('Unsupported Studio media manifest.')
    names = {entry['filename'] for entry in entries}
    for field, pattern in (
            ('studio_publications', r'studio/publications/[0-9a-f]{32}\.json'),
            ('studio_recordings', r'studio/recordings/[0-9a-f]{32}\.json'),
            ('studio_unverified_recordings', r'studio/recordings/[0-9a-f]{32}\.wav')):
        values = media[field]
        if (type(values) is not list or len(values) > MAX_STATE_FILES
                or any(not isinstance(name, str) or not re.fullmatch(pattern, name)
                       or name not in names for name in values)
                or values != sorted(set(values))):
            raise RestoreError('Invalid Studio media file manifest.')
    required = media['required_inputs']
    if type(required) is not list or len(required) > MAX_STATE_FILES:
        raise RestoreError('Invalid required Studio inputs.')
    for item in required:
        if (type(item) is not dict
                or set(item) != {'component', 'kind', 'reference', 'included'}
                or not isinstance(item['component'], str)
                or not item['component'].startswith('state/')
                or item['component'][len('state/'):] not in names
                or item['kind'] not in ('local-voice', 'music-engine', 'publication-output')
                or item['included'] is not False):
            raise RestoreError('Invalid required Studio input.')
        _required_media_input(item['component'], item['kind'], item['reference'])
    if required != _ordered_media_inputs(required):
        raise RestoreError('Duplicate or unordered required Studio inputs.')
    if not any(media[field] for field in ('studio_input_receipt', 'studio_publications',
                                          'studio_recordings', 'studio_unverified_recordings')):
        raise RestoreError('Empty Studio media manifest.')


def create_backup(app, destination, *, include_generated=False, max_bytes=None, cancelled=lambda: False):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    backup_sidecars._check_cancelled(cancelled)
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise RestoreError('backup byte budget must be a positive integer')
    if type(include_generated) is not bool:
        raise RestoreError('Generated-audio inclusion must be an explicit boolean.')
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root), backup_sidecars.producer_locks(
            app.root, include_generated=include_generated, cancelled=cancelled) as locked_jobs:
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            _check_budget(size, max_bytes)
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                asset_budget = MAX_ASSET_BYTES if max_bytes is None else min(MAX_ASSET_BYTES, max_bytes - size)
                entries = _copy_originals(snapshot_storage.db, app.assets, stage / ORIGINALS,
                                          max_bytes=asset_budget, cancelled=cancelled)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                total = size + sum(entry.size for entry in entries)
                pending = None
                pending_roots = pending_backup.pending_roots(app.root)
                if pending_roots:
                    pending = pending_backup.snapshot_pending(app, stage / 'pending',
                        max_bytes=pending_backup.MAX_TOTAL if max_bytes is None else max_bytes - total,
                        cancelled=cancelled, expected_roots=pending_roots)
                    total += sum(entry['size_bytes'] for entry in pending['files'])
                state_budget = backup_sidecars.MAX_SNAPSHOT_BYTES if max_bytes is None else max_bytes - total
                sidecars = backup_sidecars.snapshot(snapshot_storage.db, app.root, stage,
                    max_bytes=state_budget, include_generated=include_generated,
                    cancelled=cancelled, locked_jobs=locked_jobs)
                sidecars, studio_media = backup_sidecars.retain_media_inputs(
                    snapshot_storage.db, app.root, stage, sidecars,
                    max_bytes=state_budget, include_generated=include_generated, cancelled=cancelled)
            manifest = dict(version=4, kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries), sidecars=sidecars, generated_audio_included=include_generated)
            if pending is not None:
                manifest['pending'] = pending
            if studio_media is not None:
                manifest['studio_media'] = studio_media
            backup_sidecars.normalize_manifest(manifest)
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _check_budget(total + sum(entry['size_bytes'] for entry in sidecars) + len(raw), max_bytes)
            _sync_file(stage / MANIFEST, raw)
            backup_sidecars._check_cancelled(cancelled)
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            backup_sidecars.publish(stage, destination, sidecars, sync_directory=_sync_directory)
            if pending is not None:
                (destination / 'pending').mkdir(mode=0o700)
                backup_sidecars.publish(stage, destination,
                    [dict(entry, filename='pending/' + entry['filename']) for entry in pending['files']],
                    sync_directory=_sync_directory)
                _sync_directory(destination / 'pending')
            _sync_directory(destination / ORIGINALS)
            backup_sidecars._check_cancelled(cancelled)
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _read_manifest(directory, expected_manifest_sha256):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    if not {DATABASE, ORIGINALS, MANIFEST} <= {path.name for path in directory.iterdir()}:
        raise RestoreError('incomplete backup or unexpected files')
    size = _plain(directory / MANIFEST).st_size
    if not 0 < size <= MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    with (directory / MANIFEST).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) != size or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    if (type(manifest) is not dict or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2, 3, 4, 5, 6)):
        raise RestoreError('unsupported library manifest')
    inventory = backup_sidecars.normalize_manifest(manifest)
    backup_sidecars.check_namespace(directory, inventory)
    if (manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4, 5)
            or type(manifest['assets']) is not list or len(manifest['assets']) > MAX_ASSETS):
        raise RestoreError('unsupported library manifest')
    if manifest['version'] >= 2:
        fingerprint = manifest['schema_fingerprint']
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or fingerprint != expected_schema_fingerprint(manifest['schema_version'])):
            raise RestoreError('unsupported backup schema fingerprint')
    database = manifest['database']
    if (type(database) is not dict or set(database) != {'filename','size_bytes','sha256'}
            or database['filename'] != DATABASE or type(database['size_bytes']) is not int
            or not 0 < database['size_bytes'] <= MAX_DATABASE
            or not isinstance(database['sha256'],str)
            or not re.fullmatch(r'[0-9a-f]{64}',database['sha256'])):
        raise RestoreError('invalid database manifest')
    filenames, digests, total = set(), set(), 0
    for asset in manifest['assets']:
        if (type(asset) is not dict or set(asset) != {'filename','sha256','mime','size_bytes'}
                or not isinstance(asset['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', asset['sha256'])
                or not isinstance(asset['mime'], str) or asset['mime'] not in SUFFIXES
                or asset['filename'] != asset['sha256'] + SUFFIXES[asset['mime']]
                or type(asset['size_bytes']) is not int or not 0 < asset['size_bytes'] <= MAX_BYTES):
            raise RestoreError('invalid asset manifest')
        if asset['filename'] in filenames or asset['sha256'] in digests:
            raise RestoreError('duplicate asset manifest')
        filenames.add(asset['filename'])
        digests.add(asset['sha256'])
        total += asset['size_bytes']
        if total > MAX_ASSET_BYTES:
            raise RestoreError('asset manifest byte limit')
    return manifest


def _copy_database(source, target, library_manifest, *, cancelled=lambda: False):
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
            backup_sidecars._check_cancelled(cancelled)
            count += len(block)
            if count > manifest['size_bytes']:
                raise RestoreError('database grew during restore')
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if count != manifest['size_bytes'] or digest.hexdigest() != manifest['sha256']:
        raise RestoreError('database digest mismatch')
    # Inspect the closed private copy before allowing writable Storage access.
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        fingerprint = schema_fingerprint(db)
        if fingerprint != expected_schema_fingerprint(library_manifest['schema_version']):
            raise RestoreError('backup schema mismatch')
        if (library_manifest['version'] >= 2
                and fingerprint != library_manifest['schema_fingerprint']):
            raise RestoreError('backup schema fingerprint mismatch')


def _verify_contents(source, stage, manifest, *, copy_originals=False, new_root=None, cancelled=lambda: False):
    """Authenticate the common backup format using only a private database copy."""
    inventory = backup_sidecars.normalize_manifest(manifest)
    _plain(source / ORIGINALS, directory=True)
    snapshot = stage / DATABASE
    _copy_database(source / DATABASE, snapshot, manifest, cancelled=cancelled)
    with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
        storage.check()
        original_store = RetainedAssets(source / ORIGINALS)
        entries = verify_catalog_assets(storage.db, original_store,
            include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        if _asset_manifest(entries) != manifest['assets']:
            raise RestoreError('asset manifest disagrees with the database')
        if copy_originals:
            _copy_originals(storage.db, original_store, stage / ORIGINALS, cancelled=cancelled)
        backup_sidecars.restore(storage.db, source, stage, inventory, cancelled=cancelled)
        if new_root is not None:
            inventory = backup_sidecars.restore_media_inputs(
                storage.db, stage, new_root, inventory, cancelled=cancelled)
    pending_entries = []
    if inventory.pending is not None:
        recovered = pending_backup.restore_pending(source / 'pending', stage,
            stage if new_root is None else new_root, inventory.pending, cancelled=cancelled)
        pending_entries.extend(dict(filename=entry['filename']) for entry in inventory.pending['files']
                               if not entry['filename'].startswith('originals/'))
        pending_entries.extend(dict(filename='originals/' + entry['filename']) for entry in recovered['assets'])
    backup_sidecars._check_cancelled(cancelled)
    return inventory, entries, pending_entries


def verify_backup(source, expected_manifest_sha256, *, cancelled=lambda: False):
    """Authenticate all backup bytes and source bindings without changing it.

    Library workflow recovery and restore use the same checked common-format
    adapters. SQLite validation opens only a private temporary database; no
    source file or completed backup is opened writable.
    """
    backup_sidecars._check_cancelled(cancelled)
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    manifest = _read_manifest(source, expected_manifest_sha256)
    with TemporaryDirectory(prefix='carr-backup-verify-') as tmp:
        _verify_contents(source, Path(tmp), manifest, cancelled=cancelled)
    return manifest


def restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    backup_sidecars._check_cancelled(cancelled)
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        inventory, entries, pending_entries = _verify_contents(source, stage, manifest,
            copy_originals=True, new_root=destination, cancelled=cancelled)
        backup_sidecars._check_cancelled(cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        backup_sidecars.publish(stage, destination, inventory.entries, sync_directory=_sync_directory)
        backup_sidecars.publish(stage, destination, pending_entries, sync_directory=_sync_directory)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    backup_sidecars._check_cancelled(cancelled)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
