"""Exact historical state/extension backup reader retained during format convergence.

No new backup is written here. Original V1--V5 manifests stay unchanged; this
adapter retains the existing typed extension, source and detached recovery rules.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

from ..ingest.bundle import _plain, _unique_object
from ..ingest.catalog_asset_copy import copy_catalog_assets, verify_catalog_assets
from ..ingest.retained_assets import MAX_BYTES, SUFFIXES, RetainedAssets
from ..ingest.restore import RestoreError
from ..durable_state import local_path
from ..storage.revisions import Storage
from ..storage.schema_identity import expected_schema_fingerprint
from ..library_backup import (DATABASE, ORIGINALS, MANIFEST, INCOMPLETE,
    MAX_MANIFEST, MAX_EXTENDED_MANIFEST, MAX_DATABASE, MAX_ASSETS, MAX_ASSET_BYTES,
    _check_cancelled, _destination, _asset_manifest, _copy_database,
    _sync_file, _sync_directory)

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
    if manifest['version'] in (3, 4) and ('sidecars' in manifest or 'recovery' in manifest):
        from .library import _read_manifest as read_legacy_manifest
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
        from ..state_backup import validate_state
        fields.update(('state', 'generated_audio_included'))
        if type(manifest.get('generated_audio_included')) is not bool or 'state' not in actual_files:
            raise RestoreError('incomplete application state backup')
        validate_state(manifest.get('state'))
    elif 'state' in actual_files:
        raise RestoreError('unexpected state in legacy backup')
    if manifest['version'] == 4 or (manifest['version'] == 5 and 'pending' in manifest):
        from ..pending_backup import validate_pending
        fields.add('pending')
        if 'pending' not in actual_files:
            raise RestoreError('missing pending import backup')
        validate_pending(manifest.get('pending'))
    elif 'pending' in actual_files:
        raise RestoreError('unexpected pending imports in legacy backup')
    if manifest['version'] == 5:
        from ..backup_extensions import validate_manifest as validate_extensions
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
    if 'sidecars' in manifest:
        from .archive import restore_archive
        return restore_archive(source, destination, expected_manifest_sha256, cancelled=cancelled)
    _plain(source / ORIGINALS, directory=True)
    with TemporaryDirectory(prefix='.carr-library-restore-', dir=destination.parent) as tmp:
        stage = Path(tmp)
        snapshot = stage / DATABASE
        _copy_database(source / DATABASE, snapshot, manifest, cancelled=cancelled)
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
            copy_catalog_assets(storage.db, original_store, stage / ORIGINALS,
                include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES, cancelled=cancelled)
        pending = {'assets': [], 'paths': []}
        if manifest['version'] >= 3:
            from ..state_backup import restore_state
            restore_state(source / 'state', stage / 'state', manifest['state'], cancelled=cancelled)
            from ..backup_current import validate_historical_state
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                validate_historical_state(db, stage, manifest['state'], cancelled=cancelled)
        if 'pending' in manifest:
            from ..pending_backup import restore_pending
            pending = restore_pending(source / 'pending', stage, destination, manifest['pending'], cancelled=cancelled)
        extension_paths = []
        if manifest['version'] == 5:
            from ..backup_extensions import restore as restore_extensions
            extension_paths = restore_extensions(source / 'extensions', stage, manifest['extensions'], manifest['state'], cancelled=cancelled)
        if manifest['version'] >= 3:
            from ..studio.backup_inputs import restore_inputs
            restore_inputs(stage / 'state', destination / 'state', manifest['state'], cancelled=cancelled)
            from ..backup_transcription import limit as transcription_limit, prepare_restored_inventory
            common = tuple(dict(entry, filename='state/' + entry['filename']) for entry in manifest['state'])
            if any(transcription_limit(entry['filename']) is not None for entry in common):
                prepare_restored_inventory(stage, destination, common, cancelled=cancelled)
        _check_cancelled(cancelled)
        destination.mkdir(mode=0o700)
        _sync_file(destination / INCOMPLETE, b'Restore has not completed. Do not open this data directory.\n')
        _sync_directory(destination)
        (destination / ORIGINALS).mkdir(mode=0o700)
        os.link(snapshot, destination / DATABASE)
        for entry in entries:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry.filename, destination / ORIGINALS / entry.filename)
        for entry in pending['assets']:
            _check_cancelled(cancelled)
            os.link(stage / ORIGINALS / entry['filename'], destination / ORIGINALS / entry['filename'])
        if manifest['version'] >= 3:
            from ..state_backup import publish_state
            publish_state(stage / 'state', destination / 'state', cancelled=cancelled)
            for name in pending['paths']:
                if (stage / name).is_dir():
                    publish_state(stage / name, destination / name, cancelled=cancelled)
                else:
                    os.link(stage / name, destination / name)
            for name in extension_paths:
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
