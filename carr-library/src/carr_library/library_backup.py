"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research, registered original files,
source-bound reading places, registered sidecars, private application state and
the exact originals/checkpoints needed to resume durable import requests.
Unreferenced retained files are deliberately not included.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Version 5 combines the sidecar registry, private state and pending imports. The
three historical version-3 and two version-4 layouts are recognized by exact
field sets, never by guessing or merging ambiguous fields. All versions require an exact V3/V4
database schema; opening a restored V3 database performs its checked upgrade.
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
from .ingest.catalog_asset_copy import copy_catalog_assets, verify_catalog_assets
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer
from .data_paths import guard_data_directory
from .durable_state import local_path
from .storage.revisions import Storage
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from .backup_sidecars import (check_namespace, publish_sidecars, sidecar_manifest,
    snapshot_sidecars, stage_sidecars, validate_sidecar_manifest, validate_sidecars)
from .state_backup import read_state_bytes

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


def create_backup(app, destination, *, include_generated=False, cancelled=lambda: False):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    if type(include_generated) is not bool or not callable(cancelled):
        raise ValueError('Invalid backup options.')
    if cancelled():
        raise InterruptedError('Backup cancelled.')
    destination = _destination(destination, app.root)
    with exclusive_writer(app.root):
        _plain(app.database)
        if os.path.lexists(app.root / INCOMPLETE):
            raise RestoreError('the source library restore is incomplete')
        with TemporaryDirectory(prefix='.carr-library-backup-', dir=destination.parent) as tmp:
            stage = Path(tmp)
            snapshot = stage / DATABASE
            with closing(Storage(app.database, schema_version=4)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            with closing(Storage(snapshot, schema_version=4)) as snapshot_storage:
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                sidecars = snapshot_sidecars(app.root, cancelled=cancelled)
                validate_sidecars(snapshot_storage.db, sidecars, cancelled=cancelled)
            stage_sidecars(stage, sidecars, _sync_file)
            manifest = dict(version=2, kind='carr-library', schema_version=4,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            state = None
            if os.path.lexists(app.root / 'state'):
                from .state_backup import snapshot_state
                (stage / 'state').mkdir()
                state = snapshot_state(app.root / 'state', stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled)
            if sidecars or state is not None:
                manifest.update(version=4, sidecars=sidecar_manifest(sidecars), state=state,
                                generated_audio_included=include_generated)
            pending = None
            if any(os.path.lexists(app.root / name) for name in
                   ('imports.sqlite', 'import-requests', 'import-batches')):
                from .pending_backup import snapshot_pending
                pending = snapshot_pending(app, stage / 'pending', cancelled=cancelled)
                manifest.update(version=5, sidecars=sidecar_manifest(sidecars), state=state,
                                generated_audio_included=include_generated, pending=pending)
            # Optional preference writers may use their own lock. Recheck the
            # exact captured bytes after the slower state/audio snapshot too.
            if snapshot_sidecars(app.root, cancelled=cancelled) != sidecars:
                raise RestoreError('a backup sidecar changed during capture; retry after saving')
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > MAX_MANIFEST:
                raise RestoreError('manifest size limit')
            _sync_file(stage / MANIFEST, raw)
            if cancelled():
                raise InterruptedError('Backup cancelled.')
            # mkdir provides exclusive no-replace ownership of the destination.
            # The final manifest is the completion marker; no partial bundle is
            # silently overwritten or presented as a successful backup.
            destination.mkdir(mode=0o700)
            (destination / ORIGINALS).mkdir(mode=0o700)
            os.link(snapshot, destination / DATABASE)
            for entry in entries:
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            if state is not None:
                from .state_backup import publish_state
                publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
            if pending is not None:
                from .state_backup import publish_state
                publish_state(stage / 'pending', destination / 'pending', cancelled=cancelled)
            publish_sidecars(stage, destination, sidecars, _sync_directory)
            _sync_directory(destination / ORIGINALS)
            if cancelled():
                raise InterruptedError('Backup cancelled before publication. The incomplete folder has no manifest.')
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _read_manifest(directory, expected_manifest_sha256):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    try:
        size = _plain(directory / MANIFEST).st_size
    except FileNotFoundError as exc:
        raise RestoreError('incomplete backup: the completion manifest is missing') from exc
    if not 0 < size <= MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    raw = read_state_bytes(directory / MANIFEST, MAX_MANIFEST)
    if len(raw) != size or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    if (type(manifest) is not dict or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2, 3, 4, 5)):
        raise RestoreError('unsupported library manifest')
    fields = {'version','kind','schema_version','database','assets'}
    if manifest['version'] >= 2:
        fields.add('schema_fingerprint')
    if manifest['version'] == 3:
        extension = set(manifest) - fields
        if extension not in ({'reading_state'}, {'sidecars'}, {'state', 'generated_audio_included'}):
            raise RestoreError('ambiguous or unsupported version-3 backup layout')
        fields.update(extension)
    elif manifest['version'] == 4:
        extension = set(manifest) - fields
        if extension not in ({'sidecars', 'state', 'generated_audio_included'},
                             {'pending', 'state', 'generated_audio_included'}):
            raise RestoreError('ambiguous or unsupported version-4 backup layout')
        fields.update(extension)
    elif manifest['version'] == 5:
        fields.update(('sidecars', 'state', 'generated_audio_included'))
        fields.add('pending')
    if (set(manifest) != fields
            or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4)
            or type(manifest['assets']) is not list or len(manifest['assets']) > MAX_ASSETS):
        raise RestoreError('unsupported library manifest')
    if manifest['version'] >= 2:
        fingerprint = manifest['schema_fingerprint']
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or fingerprint != expected_schema_fingerprint(manifest['schema_version'])):
            raise RestoreError('unsupported backup schema fingerprint')
    sidecars = manifest.get('sidecars', [])
    if 'reading_state' in manifest:
        sidecars = [] if manifest['reading_state'] is None else [manifest['reading_state']]
        if sidecars and (type(sidecars[0]) is not dict or sidecars[0].get('filename') != 'reading-state.json'):
            raise RestoreError('invalid legacy reading-state manifest')
    validate_sidecar_manifest(sidecars)
    state = manifest.get('state')
    if 'state' in manifest:
        from .state_backup import validate_state
        if type(manifest.get('generated_audio_included')) is not bool:
            raise RestoreError('invalid generated-audio backup option')
        if state is not None:
            validate_state(state)
        elif manifest['version'] == 3 or manifest['version'] == 4 and 'pending' in manifest:
            raise RestoreError('incomplete legacy application-state backup')
    if 'pending' in manifest:
        from .pending_backup import validate_pending
        validate_pending(manifest['pending'])
    check_namespace(directory, sidecars, state is not None, has_pending='pending' in manifest)
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


def _manifest_sidecars(manifest):
    if 'reading_state' in manifest:
        return [] if manifest['reading_state'] is None else [manifest['reading_state']]
    return manifest.get('sidecars', [])


def _copy_database(source, target, library_manifest):
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
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


def restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    if not callable(cancelled):
        raise ValueError('A restore cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Restore cancelled before publication.')
    # Validate the original lexical path before resolving it, otherwise an
    # ancestor symlink/junction would be hidden from the component verifiers.
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256)
    _plain(source / ORIGINALS, directory=True)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy_database(source / DATABASE, snapshot, manifest)
        with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
            storage.check()
            sidecars = snapshot_sidecars(source, cancelled=cancelled)
            if sidecar_manifest(sidecars) != sorted(_manifest_sidecars(manifest), key=lambda entry: entry['filename']):
                raise RestoreError('backup sidecar size or digest mismatch')
            validate_sidecars(storage.db, sidecars, cancelled=cancelled)
            stage_sidecars(stage, sidecars, _sync_file)
            original_store = RetainedAssets(source / ORIGINALS)
            entries = verify_catalog_assets(storage.db, original_store,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('asset manifest disagrees with the database')
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        if manifest.get('state') is not None:
            from .state_backup import restore_state
            restore_state(source / 'state', stage / 'state', manifest['state'], cancelled=cancelled)
        pending = {'assets': [], 'paths': []}
        if 'pending' in manifest:
            from .pending_backup import restore_pending
            pending = restore_pending(source / 'pending', stage, destination, manifest['pending'],
                                      cancelled=cancelled)
        if (snapshot_sidecars(source, cancelled=cancelled) != sidecars
                or _read_manifest(source, expected_manifest_sha256) != manifest):
            raise RestoreError('backup metadata changed during restoration')
        if cancelled():
            raise InterruptedError('Restore cancelled before publication.')
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending['assets']:
            if cancelled():
                raise InterruptedError('Restore cancelled during pending-original publication.')
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        if manifest.get('state') is not None:
            from .state_backup import publish_state
            publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
        for name in pending['paths']:
            if cancelled():
                raise InterruptedError('Restore cancelled during pending-journal publication.')
            if (stage / name).is_dir():
                from .state_backup import publish_state
                publish_state(stage / name, destination / name, cancelled=cancelled)
            else:
                os.link(stage / name, destination / name)
        publish_sidecars(stage, destination, sidecars, _sync_directory)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    if cancelled():
        raise InterruptedError('Restore cancelled before completion; the incomplete marker was retained.')
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
