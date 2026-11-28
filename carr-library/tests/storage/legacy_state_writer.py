"""Literal prior V5 writer retained only for historical-reader regression fixtures.

The function body comes from library_backup.py at commit
25c7e78e5e0ea415b9938afbf1412826c18deabd (blob
6ed518d0d4ced9ed6261fa5b2ac0b5a18094c618); only package-relative
imports are qualified for this isolated test helper. Product imports resolve
normally to the selected source or installed package.
"""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from carr_library.ingest.bundle import _plain
from carr_library.ingest.catalog_asset_copy import copy_catalog_assets
from carr_library.ingest.restore import RestoreError
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.storage.revisions import Storage, CURRENT_SCHEMA_VERSION
from carr_library.storage.schema_identity import schema_fingerprint
from carr_library.library_backup import (
    DATABASE, ORIGINALS, MANIFEST, INCOMPLETE, MAX_MANIFEST, MAX_EXTENDED_MANIFEST,
    MAX_DATABASE, MAX_ASSETS, MAX_ASSET_BYTES, _destination, _asset_manifest,
    _sync_file, _sync_directory,
)


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
            with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                storage.backup(snapshot)
            size = _plain(snapshot).st_size
            if not 0 < size <= MAX_DATABASE:
                raise RestoreError('database exceeds backup size limit')
            with closing(Storage(snapshot, schema_version=CURRENT_SCHEMA_VERSION)) as snapshot_storage:
                included = {row[0] for row in snapshot_storage.db.execute('SELECT id FROM workspace')}
                if authorized_workspaces is not None and not included <= authorized_workspaces:
                    raise RestoreError('Complete backup contains a library outside the authorized workspace scope.')
                entries = copy_catalog_assets(snapshot_storage.db, app.assets, stage / ORIGINALS,
                    include_all_workspaces=True, max_assets=MAX_ASSETS, max_bytes=MAX_ASSET_BYTES, cancelled=cancelled)
                fingerprint = schema_fingerprint(snapshot_storage.db)
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
                from carr_library.state_backup import snapshot_state
                from carr_library.backup_extensions import scoped_state_selection
                selected = (None if authorized_workspaces is None else
                    scoped_state_selection(state_root, snapshot, included))
                (stage / 'state').mkdir()
                state = snapshot_state(state_root, stage / 'state',
                                       include_generated=include_generated, cancelled=cancelled,
                                       include_paths=selected, missing_ok=False)
                if selected is not None and selected != scoped_state_selection(state_root, snapshot, included):
                    raise RestoreError('Source-bound reading drafts changed during backup; retry after saving.')
                manifest.update(version=3, state=state, generated_audio_included=include_generated)
            from carr_library.pending_backup import pending_roots, snapshot_pending
            roots = pending_roots(app.root)
            if roots:
                manifest.update(version=4, pending=snapshot_pending(app, stage / 'pending',
                    cancelled=cancelled, expected_roots=roots))
                if 'state' not in manifest:
                    (stage / 'state').mkdir()
                    manifest.update(state=[], generated_audio_included=include_generated)
            from carr_library.backup_extensions import snapshot as snapshot_extensions
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
                from carr_library.state_backup import publish_state
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
