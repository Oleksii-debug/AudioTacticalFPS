"""Complete local research-library backups using the canonical SQLite snapshot.

Backups include all workspaces, saved research and registered original files.
Version 4 includes resumable import journals and their pinned unpublished sources.
The independently retained manifest digest binds the database and every asset.
Version 2 also identifies the exact schema, not just its numeric user_version.
Legacy version 1 backups and version 2 backups are accepted only for an exact
supported V3, V4 or V5 schema. Older snapshots are restored unchanged; opening
the restored library performs the normal checked upgrade to the current schema.
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
from .ingest.catalog_asset_copy import (catalog_inventory, _checked_path,
    copy_catalog_assets, verify_catalog_assets)
from .ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from .ingest.restore import RestoreError
from .ingest.writer_guard import exclusive_writer
from .data_paths import guard_data_directory
from .durable_state import local_path
from .storage.revisions import Storage, CURRENT_SCHEMA_VERSION
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


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Restore cancelled; no completed destination was published.')


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
        _check_cancelled(cancelled)
        path = _checked_path(source, entry)
        count, digest = 0, sha256()
        with path.open('rb') as incoming, (destination / entry.filename).open('xb') as output:
            while block := incoming.read(min(1024 * 1024, entry.size + 1 - count)):
                _check_cancelled(cancelled)
                count += len(block)
                if count > entry.size:
                    raise RestoreError('original file grew during backup')
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if count != entry.size or digest.hexdigest() != entry.digest:
            raise RestoreError('original file changed during backup')
    _check_cancelled(cancelled)
    return verify_catalog_assets(db, RetainedAssets(destination), cancelled=cancelled, **limits)


def create_backup(app, destination, *, include_generated=False, max_bytes=None,
                  cancelled=lambda: False, authorized_workspaces=None):
    """Back up every library to a fresh folder and return its manifest SHA256.

    The host's explicit Backup action authorizes all-workspace inclusion. The
    private source/destination parents must be stable local directories. A
    publication interrupted before manifest.json is incomplete and not restorable.
    """
    if type(include_generated) is not bool or not callable(cancelled):
        raise ValueError('Invalid backup options.')
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise RestoreError('backup byte budget must be a positive integer')
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
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            _check_budget(size, max_bytes)
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                included = {row[0] for row in snapshot_storage.db.execute('SELECT id FROM workspace')}
                if authorized_workspaces is not None and not included <= authorized_workspaces:
                    raise RestoreError('Complete backup contains a library outside the authorized workspace scope.')
                asset_budget = MAX_ASSET_BYTES if max_bytes is None else min(MAX_ASSET_BYTES, max_bytes - size)
                entries = _copy_originals(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    max_bytes=asset_budget, cancelled=cancelled)
                fingerprint = schema_fingerprint(snapshot_storage.db)
            total = size + sum(entry.size for entry in entries)
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
                from .state_backup import MAX_STATE_BYTES, snapshot_state
                from .backup_extensions import scoped_state_selection
                selected = (None if authorized_workspaces is None else
                    scoped_state_selection(state_root, snapshot, included))
                (stage / 'state').mkdir()
                state = snapshot_state(state_root, stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled,
                                       include_paths=selected, missing_ok=False,
                                       max_bytes=MAX_STATE_BYTES if max_bytes is None else max_bytes - total)
                if selected is not None and selected != scoped_state_selection(state_root, snapshot, included):
                    raise RestoreError('Source-bound reading drafts changed during backup; retry after saving.')
                total += sum(entry['size_bytes'] for entry in state)
                _check_budget(total, max_bytes)
                manifest.update(version=3, state=state, generated_audio_included=include_generated)
            from .pending_backup import MAX_TOTAL, pending_roots, snapshot_pending
            roots = pending_roots(app.root)
            if roots:
                manifest.update(version=4, pending=snapshot_pending(app, stage / 'pending',
                    cancelled=cancelled, expected_roots=roots,
                    max_bytes=MAX_TOTAL if max_bytes is None else max_bytes - total))
                total += sum(entry['size_bytes'] for entry in manifest['pending']['files'])
                _check_budget(total, max_bytes)
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            from .backup_extensions import MAX_BYTES as MAX_EXTENSION_BYTES, snapshot as snapshot_extensions
            extensions = snapshot_extensions(app.root, stage / 'extensions', snapshot,
                manifest.get('state', []), independent=authorized_workspaces is None, cancelled=cancelled,
                max_bytes=MAX_EXTENSION_BYTES if max_bytes is None else max_bytes - total)
            if extensions is not None:
                manifest.update(version=5, extensions=extensions)
                total += sum(entry['size_bytes'] for entry in extensions['files'])
                _check_budget(total, max_bytes)
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode('utf-8')
            if len(raw) > (MAX_EXTENDED_MANIFEST if manifest['version'] == 5 else MAX_MANIFEST):
                raise RestoreError('manifest size limit')
            _check_budget(total + len(raw), max_bytes)
            _sync_file(stage / MANIFEST, raw)
            _attest_backup(stage, manifest, sha256(raw).hexdigest(), cancelled=cancelled)
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
                publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
                if 'pending' in manifest:
                    publish_state(stage / 'pending', destination / 'pending', cancelled=cancelled)
                if manifest['version'] == 5:
                    publish_state(stage / 'extensions', destination / 'extensions', cancelled=cancelled)
            _sync_directory(destination / ORIGINALS)
            if cancelled():
                raise InterruptedError('Backup cancelled before publication. The incomplete folder has no manifest.')
            os.link(stage / MANIFEST, destination / MANIFEST)
            _sync_directory(destination)
            _sync_directory(destination.parent)
    return sha256(raw).hexdigest()


def _uses_legacy_archive(manifest):
    # These exact historical envelopes used an earlier producer family. Keep
    # their bytes under the existing inert archive adapter, never relabel them.
    legacy = (manifest['version'] in (3, 4)
              and manifest.get('schema_version') in (3, 4)
              and 'generated_audio_included' not in manifest
              and ('sidecars' in manifest or 'recovery' in manifest))
    if not legacy or manifest['version'] != 3:
        return legacy
    # V3 also named the later explicit sidecar inventory. A filename outside
    # the old finite adapter selects that reader; its own allowlist still must
    # validate every entry. Common old shapes keep their inert archive path.
    rows = manifest.get('sidecars')
    if type(rows) is list:
        from .backup_compat.library import _sidecar_limit
        for row in rows:
            if type(row) is dict and type(row.get('filename')) is str:
                try:
                    _sidecar_limit(row['filename'])
                except RestoreError:
                    return False
    return True


def _uses_canonical_sidecars(manifest):
    return ('reading_state' in manifest or 'sidecars' in manifest) and not _uses_legacy_archive(manifest)


def _read_manifest(directory, expected_manifest_sha256, *, cancelled=lambda: False):
    _check_cancelled(cancelled)
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(
            r'[0-9a-f]{64}', expected_manifest_sha256):
        raise RestoreError('independently saved manifest SHA256 required')
    size = _plain(directory / MANIFEST).st_size
    if not 0 < size <= MAX_EXTENDED_MANIFEST:
        raise RestoreError('manifest size limit')
    with (directory / MANIFEST).open('rb') as stream:
        chunks, count = [], 0
        while block := stream.read(min(1024 * 1024, size + 1 - count)):
            _check_cancelled(cancelled)
            chunks.append(block)
            count += len(block)
        raw = b''.join(chunks)
    if len(raw) != size or sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    _check_cancelled(cancelled)
    if (type(manifest) is not dict or type(manifest.get('version')) is not int
            or manifest['version'] not in (1, 2, 3, 4, 5)):
        raise RestoreError('unsupported library manifest')
    # Numeric versions collide with an older implementation. Dispatch only
    # after the independently supplied digest authenticated the exact envelope.
    # The legacy parser enforces its complete field set and rejects any mixture.
    if _uses_legacy_archive(manifest):
        from .backup_compat.library import _read_manifest as read_legacy_manifest
        return read_legacy_manifest(directory, expected_manifest_sha256)
    if manifest['version'] < 5 and size > MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    if _uses_canonical_sidecars(manifest):
        from . import backup_sidecars
        if size > MAX_MANIFEST:
            raise RestoreError('manifest size limit')
        inventory = backup_sidecars.normalize_manifest(manifest)
        backup_sidecars.check_namespace(directory, inventory, cancelled=cancelled)
        fields = set(manifest)  # normalize_manifest already enforced exact fields.
    else:
        expected_files = {DATABASE, ORIGINALS, MANIFEST}
        if manifest['version'] >= 3:
            expected_files.add('state')
        if manifest['version'] == 4 or (manifest['version'] == 5 and 'pending' in manifest):
            expected_files.add('pending')
        if manifest['version'] == 5:
            expected_files.add('extensions')
        actual_files = set()
        with os.scandir(directory) as children:
            for child in children:
                _check_cancelled(cancelled)
                if child.name not in expected_files:
                    raise RestoreError('incomplete backup or unexpected files')
                actual_files.add(child.name)
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
    _check_cancelled(cancelled)
    manifest = library_manifest['database']
    if _plain(source).st_size != manifest['size_bytes']:
        raise RestoreError('database size mismatch')
    count = 0
    digest = sha256()
    with source.open('rb') as incoming, target.open('xb') as output:
        while block := incoming.read(min(1024 * 1024, manifest['size_bytes'] + 1 - count)):
            _check_cancelled(cancelled)
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


def _complete_inventory(directory, manifest, digest):
    """Reuse the finite sidecar walker for all authenticated backup components."""
    from .backup_sidecars import Inventory, normalize_manifest
    files = [dict(manifest['database']),
             dict(filename=MANIFEST, size_bytes=_plain(directory / MANIFEST).st_size,
                  sha256=digest)]
    roots = {ORIGINALS}
    files.extend(dict(filename=ORIGINALS + '/' + row['filename'],
                      size_bytes=row['size_bytes'], sha256=row['sha256'])
                 for row in manifest['assets'])
    if _uses_legacy_archive(manifest):
        files.extend(manifest['sidecars'])
        files.extend(dict(filename='recovery/' + row['path'],
                          size_bytes=row['size_bytes'], sha256=row['sha256'])
                     for row in manifest.get('recovery', {}).get('files', []))
    elif _uses_canonical_sidecars(manifest):
        compatible = normalize_manifest(manifest)
        files.extend(compatible.entries)
        roots.update(compatible.empty_roots)
        if compatible.pending is not None:
            roots.add('pending')
            files.extend({**row, 'filename': 'pending/' + row['filename']}
                         for row in compatible.pending['files'])
    else:
        for component, rows in (
                ('state', manifest.get('state')),
                ('pending', manifest.get('pending', {}).get('files')),
                ('extensions', manifest.get('extensions', {}).get('files'))):
            if rows is not None:
                roots.add(component)
                files.extend({**row, 'filename': component + '/' + row['filename']}
                             for row in rows)
        if 'extensions' in manifest:
            from .backup_extensions import _legacy_original_directories
            roots.update('extensions/' + name for name in _legacy_original_directories(
                row['filename'] for row in manifest['extensions']['files']))
    return Inventory(tuple(sorted(files, key=lambda row: row['filename'])), frozenset(roots))


def _attest_backup(directory, manifest, digest, *, cancelled=lambda: False):
    """Check every listed byte twice and reject unlisted or missing children."""
    from .backup_sidecars import check_namespace, _stream_file
    inventory = _complete_inventory(directory, manifest, digest)
    check_namespace(directory, inventory, cancelled=cancelled)
    for _ in range(2):
        for entry in inventory.entries:
            _check_cancelled(cancelled)
            _stream_file(directory / entry['filename'], expected=entry,
                         maximum=entry['size_bytes'], cancelled=cancelled,
                         allow_empty=entry['size_bytes'] == 0)
        check_namespace(directory, inventory, cancelled=cancelled)


def _prepare_restore(source, stage, new_root, manifest, *, copy_originals=True, cancelled=lambda: False):
    """Run the same checked component readers for restore and verification."""
    _plain(source / ORIGINALS, directory=True)
    snapshot = stage / DATABASE
    _copy_database(source / DATABASE, snapshot, manifest, cancelled=cancelled)
    compatible = _uses_canonical_sidecars(manifest)
    inventory = None
    with closing(Storage(snapshot, schema_version=manifest['schema_version'])) as storage:
        storage.db.set_progress_handler(lambda: int(cancelled()), 10_000)
        try:
            storage.check()
        except sqlite3.OperationalError:
            _check_cancelled(cancelled)
            raise
        original_store = RetainedAssets(source / ORIGINALS)
        entries = verify_catalog_assets(storage.db, original_store,
            include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES, cancelled=cancelled)
        if _asset_manifest(entries) != manifest['assets']:
            raise RestoreError('asset manifest disagrees with the database')
        if copy_originals:
            _copy_originals(storage.db, original_store, stage / ORIGINALS, cancelled=cancelled)
        if compatible:
            from . import backup_sidecars
            inventory = backup_sidecars.normalize_manifest(manifest)
            backup_sidecars.restore(storage.db, source, stage, inventory, cancelled=cancelled)
            for name in inventory.empty_roots:
                (stage / name).mkdir(mode=0o700, exist_ok=True)
    paths = set()
    if compatible:
        paths.update(entry['filename'].split('/')[0] for entry in inventory.entries)
        paths.update(inventory.empty_roots)
        pending_manifest = inventory.pending
    else:
        pending_manifest = manifest.get('pending')
        if manifest['version'] >= 3:
            from .state_backup import restore_state
            restore_state(source / 'state', stage / 'state', manifest['state'], cancelled=cancelled)
            from .backup_extensions import validate_primary_state
            validate_primary_state(stage / 'state', manifest['state'], snapshot,
                legacy_opaque=manifest.get('extensions', {}).get('version', 0) < 4, cancelled=cancelled)
            paths.add('state')
    pending = {'assets': [], 'paths': []}
    if pending_manifest is not None:
        from .pending_backup import restore_pending
        pending = restore_pending(source / 'pending', stage, new_root, pending_manifest, cancelled=cancelled)
        paths.update(pending['paths'])
    if not compatible and manifest['version'] == 5:
        from .backup_extensions import restore as restore_extensions
        paths.update(restore_extensions(source / 'extensions', stage, manifest['extensions'],
                                       manifest['state'], cancelled=cancelled))
    if not compatible and manifest['version'] >= 3:
        from .studio.backup_inputs import restore_inputs
        restore_inputs(stage / 'state', new_root / 'state', manifest['state'], cancelled=cancelled)
    _check_cancelled(cancelled)
    return entries, pending['assets'], sorted(paths)


def verify_backup(source, expected_manifest_sha256, *, cancelled=lambda: False):
    """Authenticate each supported exact envelope without changing source bytes."""
    if not callable(cancelled):
        raise ValueError('A backup verification cancellation callback is required.')
    _check_cancelled(cancelled)
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    manifest = _read_manifest(source, expected_manifest_sha256, cancelled=cancelled)
    if _uses_legacy_archive(manifest):
        from .backup_compat.archive import verify_archive
        verify_archive(source, expected_manifest_sha256, cancelled=cancelled)
    else:
        try:
            with TemporaryDirectory(prefix='carr-backup-verify-') as tmp:
                stage = Path(tmp)
                _prepare_restore(source, stage, stage, manifest,
                                 copy_originals=False, cancelled=cancelled)
        except sqlite3.Error:
            _check_cancelled(cancelled)
            raise
    _attest_backup(source, manifest, expected_manifest_sha256, cancelled=cancelled)
    _check_cancelled(cancelled)
    return manifest


def _restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False):
    """Restore a trusted backup into a new data root; return that root's Path.

    Keep the expected digest independently of the backup. The source is never
    opened writable. A failed/terminated publication retains .restore-incomplete;
    the application must reject such a root instead of opening partial data.
    """
    if not callable(cancelled):
        raise ValueError('A restore cancellation callback is required.')
    _check_cancelled(cancelled)
    # Validate the original lexical path before resolving it, otherwise an
    # ancestor symlink/junction would be hidden from the component verifiers.
    source = local_path(source, exists=True)
    _plain(source, directory=True)
    source = source.resolve(strict=True)
    destination = _destination(destination, source)
    manifest = _read_manifest(source, expected_manifest_sha256, cancelled=cancelled)
    if _uses_legacy_archive(manifest):
        from .backup_compat.archive import restore_archive
        _attest_backup(source, manifest, expected_manifest_sha256, cancelled=cancelled)
        return restore_archive(source, destination, expected_manifest_sha256, cancelled=cancelled)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        entries, pending_assets, paths = _prepare_restore(
            source, stage, destination, manifest, cancelled=cancelled)
        _attest_backup(source, manifest, expected_manifest_sha256, cancelled=cancelled)
        _check_cancelled(cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(stage / DATABASE, destination / DATABASE)
        for entry in entries:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending_assets:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        from .state_backup import publish_state
        for name in paths:
            _check_cancelled(cancelled)
            if (stage / name).is_dir():
                publish_state(stage / name, destination / name, cancelled=cancelled)
            else:
                os.link(stage / name, destination / name)
        _sync_directory(destination / ORIGINALS)
        _sync_directory(destination)
    # Temporary hard links are now gone; retained originals have one link.
    _check_cancelled(cancelled)
    (destination / INCOMPLETE).unlink()
    _sync_directory(destination)
    _sync_directory(destination.parent)
    return destination


def restore_backup(source, destination, expected_manifest_sha256, *, cancelled=lambda: False):
    """Restore to a new root with cancellable copying, checks and publication.

    Cancellation before publication leaves no destination. Cancellation during
    publication keeps its incomplete marker; normal application open rejects
    that root. After the marker is durably removed, the completed result wins.
    """
    if not callable(cancelled):
        raise ValueError('A restore cancellation callback is required.')
    try:
        return _restore_backup(source, destination, expected_manifest_sha256, cancelled=cancelled)
    except sqlite3.Error:
        _check_cancelled(cancelled)
        raise
