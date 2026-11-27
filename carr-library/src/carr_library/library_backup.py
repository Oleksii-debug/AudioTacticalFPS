"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research and registered original files.
Version 4 includes resumable import journals and their pinned unpublished sources.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Legacy version 1 backups and version 2 backups are accepted only for an exact
supported V3 or V4 schema. V3 snapshots are restored unchanged; opening the
restored library performs the normal checked upgrade to V4.
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

DATABASE = 'library.sqlite'
ORIGINALS = 'originals'
MANIFEST = 'manifest.json'
INCOMPLETE = '.restore-incomplete'
MAX_MANIFEST = 8 * 1024 * 1024
MAX_EXTENDED_MANIFEST = 64 * 1024 * 1024
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


def create_backup(app, destination, *, include_generated=False, cancelled=lambda: False,
                  authorized_workspaces=None):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    if type(include_generated) is not bool or not callable(cancelled):
        raise ValueError('Invalid backup options.')
    if authorized_workspaces is not None:
        if isinstance(authorized_workspaces, (str, bytes)):
            raise ValueError('Workspace authorization must be an explicit collection.')
        authorized_workspaces = frozenset(authorized_workspaces)
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
                included = {row[0] for row in snapshot_storage.db.execute('SELECT id FROM workspace')}
                if authorized_workspaces is not None and not included <= authorized_workspaces:
                    raise RestoreError('Complete backup contains a library outside the authorized workspace scope.')
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
                fingerprint = schema_fingerprint(snapshot_storage.db)
            manifest = dict(version=2, kind='carr-library', schema_version=4,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            if os.path.lexists(app.root / 'state'):
                from .state_backup import snapshot_state
                from .backup_extensions import scoped_state_selection
                selected = (None if authorized_workspaces is None else
                    scoped_state_selection(app.root / 'state', snapshot, included))
                (stage / 'state').mkdir()
                state = snapshot_state(app.root / 'state', stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled,
                                       include_paths=selected)
                if selected is not None and selected != scoped_state_selection(app.root / 'state', snapshot, included):
                    raise RestoreError('Source-bound reading drafts changed during backup; retry after saving.')
                manifest.update(version=3, state=state, generated_audio_included=include_generated)
            if any(os.path.lexists(app.root / name) for name in
                   ('imports.sqlite', 'import-requests', 'import-batches')):
                from .pending_backup import snapshot_pending
                manifest.update(version=4, pending=snapshot_pending(app, stage / 'pending', cancelled=cancelled))
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            from .backup_extensions import snapshot as snapshot_extensions
            extensions = snapshot_extensions(app.root, stage / 'extensions', snapshot,
                manifest.get('state', []), independent=authorized_workspaces is None, cancelled=cancelled)
            if extensions is not None:
                manifest.update(version=5, extensions=extensions)
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > (MAX_EXTENDED_MANIFEST if manifest['version'] == 5 else MAX_MANIFEST):
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
            if manifest['version'] >= 3:
                from .state_backup import publish_state
                publish_state(stage / 'state', destination / 'state')
                if 'pending' in manifest:
                    publish_state(stage / 'pending', destination / 'pending')
                if manifest['version'] == 5:
                    publish_state(stage / 'extensions', destination / 'extensions')
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
    size = _plain(directory / MANIFEST).st_size
    if not 0 < size <= MAX_EXTENDED_MANIFEST:
        raise RestoreError('manifest size limit')
    with (directory / MANIFEST).open('rb') as stream:
        raw = stream.read(MAX_EXTENDED_MANIFEST + 1)
    if len(raw) != size or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    if (type(manifest) is not dict or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2, 3, 4, 5)):
        raise RestoreError('unsupported library manifest')
    # Numeric versions collide with an older implementation. Dispatch only
    # after the independently supplied digest authenticated the exact envelope.
    # The legacy parser enforces its complete field set and rejects any mixture.
    if manifest['version'] in (3, 4) and ('sidecars' in manifest or 'recovery' in manifest):
        from .backup_compat.library import _read_manifest as read_legacy_manifest
        return read_legacy_manifest(directory, expected_manifest_sha256)
    if manifest['version'] < 5 and size > MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    actual_files = set(p.name for p in directory.iterdir())
    expected_files = {DATABASE, ORIGINALS, MANIFEST}
    if manifest['version'] >= 3:
        expected_files.add('state')
    if manifest['version'] == 4 or (manifest['version'] == 5 and 'pending' in manifest):
        expected_files.add('pending')
    if manifest['version'] == 5:
        expected_files.add('extensions')
    if actual_files != expected_files:
        raise RestoreError('incomplete backup or unexpected files')
    fields = {'version','kind','schema_version','database','assets'}
    if manifest['version'] >= 2:
        fields.add('schema_fingerprint')
    if manifest['version'] >= 3:
        from .state_backup import validate_state
        fields.update(('state', 'generated_audio_included'))
        if type(manifest.get('generated_audio_included')) is not bool or 'state' not in actual_files:
            raise RestoreError('incomplete application state backup')
        validate_state(manifest.get('state'))
    elif 'state' in actual_files:
        raise RestoreError('unexpected state in legacy backup')
    if manifest['version'] == 4 or (manifest['version'] == 5 and 'pending' in manifest):
        from .pending_backup import validate_pending
        fields.add('pending')
        if 'pending' not in actual_files:
            raise RestoreError('missing pending import backup')
        validate_pending(manifest.get('pending'))
    elif 'pending' in actual_files:
        raise RestoreError('unexpected pending imports in legacy backup')
    if manifest['version'] == 5:
        from .backup_extensions import validate_manifest as validate_extensions
        fields.add('extensions')
        validate_extensions(manifest.get('extensions'))
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


def restore_backup(source, destination, expected_manifest_sha256):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    # Validate the original lexical path before resolving it, otherwise an
    # ancestor symlink/junction would be hidden from the component verifiers.
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256)
    if 'sidecars' in manifest:
        from .backup_compat.archive import restore_archive
        return restore_archive(source, destination, expected_manifest_sha256)
    _plain(source / ORIGINALS, directory=True)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy_database(source / DATABASE, snapshot, manifest)
        with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
            storage.check()
            original_store = RetainedAssets(source / ORIGINALS)
            entries = verify_catalog_assets(storage.db, original_store,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
            if _asset_manifest(entries) != manifest['assets']:
                raise RestoreError('asset manifest disagrees with the database')
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES)
        pending = {'assets': [], 'paths': []}
        if manifest['version'] >= 3:
            from .state_backup import restore_state
            restore_state(source / 'state', stage / 'state', manifest['state'])
        if 'pending' in manifest:
            from .pending_backup import restore_pending
            pending = restore_pending(source / 'pending', stage, destination, manifest['pending'])
        extension_paths = []
        if manifest['version'] == 5:
            from .backup_extensions import restore as restore_extensions
            extension_paths = restore_extensions(source / 'extensions', stage, manifest['extensions'], manifest['state'])
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending['assets']:
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        if manifest['version'] >= 3:
            from .state_backup import publish_state
            publish_state(stage / 'state', destination / 'state')
            for name in pending['paths']:
                if (stage / name).is_dir():
                    publish_state(stage / name, destination / name)
                else:
                    os.link(stage / name, destination / name)
            for name in extension_paths:
                if (stage / name).is_dir():
                    publish_state(stage / name, destination / name)
                else:
                    os.link(stage / name, destination / name)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
