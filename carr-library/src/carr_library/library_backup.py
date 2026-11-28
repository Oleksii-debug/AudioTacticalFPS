"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research and registered original files.
Version 4 includes resumable import journals and their pinned unpublished sources.
Version 5 composes that state with the earlier typed reading/studio sidecars.
Independently published version 3 shapes remain strictly distinguishable.
Canonical root-relative v3/v4 and v5 reading-state envelopes are accepted by
an additive read-only adapter; new backups keep the composite format 5 writer.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Legacy version 1 backups and version 2 backups are accepted only for an exact
supported V3, V4, V5 or V6 schema. Older snapshots restore unchanged; opening
the restored library performs the checked upgrade to the current schema.
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
from .ingest.streamed_text import MAX_BYTES as STREAMED_TEXT_MAX_BYTES
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer
from .data_paths import guard_data_directory
from .durable_state import local_path
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint
from . import backup_sidecars, backup_compat, recording_backup

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
    with (exclusive_writer(app.root), backup_sidecars.state_locks(app.root),
          recording_backup.snapshot_guard(app.root, cancelled=cancelled)):
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
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES,
                    cancelled=cancelled)
                fingerprint = schema_fingerprint(snapshot_storage.db)
                # The canonical pending producer is the sole owner of imports
                # in a new composite backup. Legacy sidecar-v3 remains readable.
                sidecars = backup_sidecars.snapshot(app, stage, {entry.digest for entry in entries},
                    db=snapshot_storage.db, include_pending=False,
                    include_generated=include_generated, cancelled=cancelled)
            manifest = dict(version=2, kind='carr-library', schema_version=CURRENT_SCHEMA_VERSION,
                schema_fingerprint=fingerprint,
                database=dict(filename=DATABASE, size_bytes=size, sha256=hash_file(snapshot)),
                assets=_asset_manifest(entries))
            state_root = app.root / 'state'
            # lexists suppresses access errors, which could omit all user work.
            # Only genuine absence is optional; disappearance after this probe fails.
            try:
                state_root.lstat()
            except FileNotFoundError:
                pass
            else:
                from .state_backup import snapshot_state
                from .state_recovery import validate_state_documents
                (stage / 'state').mkdir()
                state = snapshot_state(state_root, stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled,
                                       missing_ok=False)
                from .studio.backup_inputs import snapshot_inputs
                state = snapshot_inputs(stage / 'state', state, source_state_root=state_root,
                                        cancelled=cancelled)
                validate_state_documents(stage / 'state', state, cancelled=cancelled)
                from .studio.book_backup import validate_database_snapshot
                validate_database_snapshot(snapshot, stage / 'state', state, cancelled=cancelled)
                manifest.update(version=3, state=state, generated_audio_included=include_generated)
            from .pending_backup import pending_roots, snapshot_pending
            roots = pending_roots(app.root)
            if roots:
                manifest.update(version=4, pending=snapshot_pending(app, stage / 'pending',
                    cancelled=cancelled, expected_roots=roots))
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            if sidecars is not None:
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
                manifest.update(version=5, sidecars=sidecars)
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
                if cancelled():
                    raise InterruptedError('Backup cancelled before publication.')
                os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
            if 'state' in manifest:
                from .state_backup import publish_state
                publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
                if 'pending' in manifest:
                    publish_state(stage / 'pending', destination / 'pending', cancelled=cancelled)
            if sidecars is not None:
                backup_sidecars.publish(stage, destination, sidecars, backup=True, cancelled=cancelled)
            _sync_directory(destination / ORIGINALS)
            if cancelled():
                raise InterruptedError('Backup cancelled before publication. The incomplete folder has no manifest.')
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _read_manifest(directory, expected_manifest_sha256, *, cancelled=lambda: False):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    try:
        size = _plain(directory / MANIFEST).st_size
    except FileNotFoundError as exc:
        raise RestoreError('incomplete backup or missing manifest') from exc
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
            or manifest['version'] not in (1, 2, 3, 4, 5)):
        raise RestoreError('unsupported library manifest')
    version = manifest['version']
    flat = backup_compat.is_flat_manifest(manifest)
    if flat:
        # The independent manifest digest was already checked. Its exact
        # envelope determines the interpretation; never coerce list/dict
        # sidecars or rewrite a signed input into this host's writer format.
        inventory = backup_compat.normalize_manifest(manifest)
        backup_compat.check_namespace(directory, inventory, cancelled=cancelled)
        fields = set(manifest)  # Exact allowed shape was checked by the adapter.
        legacy_sidecars = False
    else:
        required_files = {DATABASE, ORIGINALS, MANIFEST}
        allowed = required_files | {'state', 'pending', backup_sidecars.DIRECTORY}
        actual_files = set()
        for path in directory.iterdir():
            if cancelled():
                raise InterruptedError('Restore cancelled during manifest inventory.')
            if path.name not in allowed or path.name in actual_files:
                raise RestoreError('incomplete backup or unexpected files')
            actual_files.add(path.name)
        if not required_files <= actual_files:
            raise RestoreError('incomplete backup or unexpected files')
        legacy_sidecars = version == 3 and 'sidecars' in manifest
        fields = {'version','kind','schema_version','database','assets'}
        if version >= 2:
            fields.add('schema_fingerprint')
        if version >= 3 and not legacy_sidecars:
            from .state_backup import validate_state
            fields.update(('state', 'generated_audio_included'))
            if type(manifest.get('generated_audio_included')) is not bool or 'state' not in actual_files:
                raise RestoreError('incomplete application state backup')
            validate_state(manifest.get('state'))
            if not manifest['generated_audio_included']:
                from . import backup_transcription
                if any(backup_transcription.is_cache_file('state/' + entry['filename'])
                       for entry in manifest['state']):
                    raise RestoreError('Transcription cache requires explicit manifest inclusion.')
            required_files.add('state')
        elif 'state' in actual_files:
            raise RestoreError('unexpected state in legacy backup')
        if version == 4 or (version == 5 and 'pending' in manifest):
            from .pending_backup import validate_pending
            fields.add('pending')
            if 'pending' not in actual_files:
                raise RestoreError('missing pending import backup')
            validate_pending(manifest.get('pending'))
            required_files.add('pending')
        elif 'pending' in actual_files:
            raise RestoreError('unexpected pending imports in legacy backup')
        if legacy_sidecars or version == 5:
            fields.add('sidecars')
            required_files.add(backup_sidecars.DIRECTORY)
            sidecars = backup_sidecars.validate_manifest(manifest.get('sidecars'))
            if version == 5 and any(backup_sidecars.pending_path(row['path']) for row in sidecars['files']):
                raise RestoreError('composite pending imports must have one canonical producer')
        if actual_files != required_files:
            raise RestoreError('incomplete backup or unexpected components')
    if (set(manifest) != fields
            or manifest['kind'] != 'carr-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] not in (3, 4, 5, 6)
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
                or type(asset['size_bytes']) is not int or not 0 < asset['size_bytes'] <= (STREAMED_TEXT_MAX_BYTES if asset['mime'] == 'text/plain' else MAX_BYTES)):
            raise RestoreError('invalid asset manifest')
        if asset['filename'] in filenames or asset['sha256'] in digests:
            raise RestoreError('duplicate asset manifest')
        filenames.add(asset['filename'])
        digests.add(asset['sha256'])
        total += asset['size_bytes']
        if total > MAX_ASSET_BYTES:
            raise RestoreError('asset manifest byte limit')
    if legacy_sidecars:
        pending = {Path(row['path']).name for row in sidecars['files']
                   if row['path'].startswith('pending-originals/')}
        if pending.intersection(filenames):
            raise RestoreError('pending originals duplicate registered assets')
    return manifest


def _copy_database(source, target, library_manifest, *, cancelled=lambda: False):
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
            if cancelled():
                raise InterruptedError('Restore cancelled.')
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


def _verify_contents(source, stage, manifest, *, destination=None, cancelled=lambda: False):
    """Use the existing complete restore verifier in one unpublished private stage.

    A destination is supplied only by actual restoration. Plain verification
    checks current typed state without assigning a projected editable revision.
    Both routes retain the same database, original, sidecar and pending proofs.
    """
    flat = (backup_compat.normalize_manifest(manifest)
            if backup_compat.is_flat_manifest(manifest) else None)
    _plain(source / ORIGINALS, directory=True)
    snapshot = stage / DATABASE
    _copy_database(source / DATABASE, snapshot, manifest, cancelled=cancelled)
    with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
        storage.check()
        original_store = RetainedAssets(source / ORIGINALS)
        entries = verify_catalog_assets(storage.db, original_store,
            include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES,
            cancelled=cancelled)
        if _asset_manifest(entries) != manifest['assets']:
            raise RestoreError('asset manifest disagrees with the database')
        copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
            include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES,
            cancelled=cancelled)
        sidecars = None
        if flat is not None:
            flat = backup_compat.restore(storage.db, source, stage, flat, cancelled=cancelled,
                                         destination=destination)
        elif 'sidecars' in manifest:
            sidecars = backup_sidecars.restore(source / backup_sidecars.DIRECTORY,
                stage, manifest['sidecars'], destination if destination is not None else stage,
                db=storage.db, cancelled=cancelled)
    pending = {'assets': [], 'paths': []}
    if flat is None and 'state' in manifest:
        from .state_backup import restore_state
        from .state_recovery import prepare_restored_state, validate_state_documents
        restore_state(source / 'state', stage / 'state', manifest['state'], cancelled=cancelled)
        if destination is None:
            validate_state_documents(stage / 'state', manifest['state'], cancelled=cancelled)
        else:
            prepare_restored_state(stage / 'state', manifest['state'], cancelled=cancelled,
                                   destination_state_root=destination / 'state')
        from .studio.book_backup import validate_database_snapshot
        validate_database_snapshot(snapshot, stage / 'state', manifest['state'], cancelled=cancelled)
    pending_manifest = flat.pending if flat is not None else manifest.get('pending')
    if pending_manifest is not None:
        from .pending_backup import restore_pending
        pending = restore_pending(source / 'pending', stage,
                                  destination if destination is not None else stage, pending_manifest,
                                  cancelled=cancelled)
    if cancelled():
        raise InterruptedError('Restore cancelled before publication.')
    return entries, pending, flat, sidecars


def verify_backup(source, expected_manifest_sha256, *, cancelled=lambda: False,
                  temporary_parent=None):
    """Verify all complete-backup bytes and source bindings without publishing.

    Source backups remain read-only. SQLite and typed producers see only their
    existing checked private copy. No restore destination, engine or queue is
    activated; full formats 1–5 and their checked schema versions are retained.
    """
    if not callable(cancelled):
        raise ValueError('Invalid backup verification cancellation callback.')
    if cancelled():
        raise InterruptedError('Backup verification cancelled.')
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    manifest = _read_manifest(source, expected_manifest_sha256, cancelled=cancelled)
    if temporary_parent is not None:
        temporary_parent = local_path(temporary_parent, exists=True)
        _plain(temporary_parent, directory=True)
    with TemporaryDirectory(prefix='.carr-library-verify-', dir=temporary_parent) as temporary:
        _verify_contents(source, Path(temporary), manifest, cancelled=cancelled)
    if cancelled():
        raise InterruptedError('Backup verification cancelled.')
    return manifest


def restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False,
                   _final_destination=None):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    if not callable(cancelled):
        raise ValueError('Invalid restore cancellation callback.')
    if cancelled():
        raise InterruptedError('Restore cancelled.')
    # Validate the original lexical path before resolving it, otherwise an
    # ancestor symlink/junction would be hidden from the component verifiers.
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    # Internal portable restore can prepare privately, then atomically rename
    # the complete directory. Only typed path projection uses the final root;
    # all byte publication below still targets the owned preparation directory.
    projected = destination
    if _final_destination is not None:
        projected = _destination(_final_destination, source)
        if projected == destination or projected.is_relative_to(destination) or destination.is_relative_to(projected):
            raise RestoreError('final restore projection must be a separate new directory')
    manifest = _read_manifest(source, expected_manifest_sha256, cancelled=cancelled)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        entries, pending, flat, sidecars = _verify_contents(
            source, stage, manifest, destination=projected, cancelled=cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            if cancelled():
                raise InterruptedError('Restore cancelled during publication.')
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending['assets']:
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        if flat is None and 'state' in manifest:
            from .state_backup import publish_state
            publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
        for name in pending['paths']:
            if cancelled():
                raise InterruptedError('Restore cancelled during pending publication.')
            if (stage / name).is_dir():
                from .state_backup import publish_state
                publish_state(stage / name, destination / name, cancelled=cancelled)
            else:
                os.link(stage / name, destination / name)
        if flat is not None:
            backup_compat.publish(stage, destination, flat, sync_directory=_sync_directory,
                                  cancelled=cancelled)
        if sidecars is not None:
            backup_sidecars.publish(stage, destination, sidecars, backup=False, cancelled=cancelled)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    if cancelled():
        raise InterruptedError('Restore cancelled before completion; the incomplete destination is not usable.')
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination
