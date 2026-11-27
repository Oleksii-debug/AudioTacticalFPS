"""Canonical complete-library backups stored through the shared local Vault.

This adapter does not define a second snapshot format or a second state producer.
It stores the current complete-backup directory directly, so unchanged chunks can be
reused without a ZIP/encryption envelope. Vault transfer tokens authenticate full
inventories; canonical validators remain the database and retained-state authority.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from uuid import uuid4

from . import library_backup, pending_backup
from .data_paths import guard_data_directory
from .backup_files import local_path, regular
from .file_publication import publish_directory_no_replace
from .ingest.restore import RestoreError
from .operations.broker import ResourceBroker
from .operations.models import ResourceCancelled, ResourceRequest
from .sync.vault import (Vault, VaultConflict, VaultMaterializedError, _sync_directory,
                         _relative, _read)

MAX_BYTES = (library_backup.MAX_DATABASE + library_backup.MAX_ASSET_BYTES
             + library_backup.MAX_SIDECAR_BYTES + pending_backup.MAX_TOTAL
             + pending_backup.MAX_JOURNAL + library_backup.MAX_MANIFEST)
MAX_HISTORY = 10_000


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('Cancellation must be a callable operation guard.')
    if cancelled():
        raise InterruptedError('Incremental backup cancelled before publication.')


def _digest(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise RestoreError('An independently retained lowercase snapshot SHA256 is required.')
    return value


def _name(value):
    return PurePosixPath(_relative(value))


def _vault(path, *, max_bytes=MAX_BYTES):
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES:
        raise RestoreError('Choose a finite total snapshot size allowance.')
    return Vault.open_existing(local_path(path, exists=True), max_files=1_000_000,
                               max_total_bytes=max_bytes)


def _manifest_bytes(folder):
    path = local_path(folder / library_backup.MANIFEST, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= library_backup.MAX_MANIFEST:
        raise RestoreError('Complete-backup manifest exceeds its size allowance.')
    return _read(path, library_backup.MAX_MANIFEST)


def _full(vault, token, destination, cancelled):
    """Authenticate transport bytes, then the exact canonical complete snapshot."""
    _digest(token)
    _check(cancelled)
    try:
        vault.materialize(token, destination, expected_token=token, cancelled=cancelled)
    except VaultMaterializedError as exc:
        # This is only our fresh private verification copy. Its bytes still need
        # the complete canonical checks below; no user destination exists yet.
        if exc.destination != destination or exc.transfer_token != token:
            raise
    digest = sha256(_manifest_bytes(destination)).hexdigest()
    # The existing validator owns format migration and every typed state namespace.
    # Do not narrow this to a donor branch's format or bypass retained-input checks.
    library_backup.verify_backup(destination, digest, cancelled=cancelled)
    return digest


def _cleanup(temporary, result=None):
    try:
        temporary.cleanup()
    except OSError:
        if result is not None:
            result['staging_cleanup_pending'] = True
            result['retained_temporary_directory'] = temporary.name
        return
    if result is not None:
        result['staging_cleanup_pending'] = False


def initialize_repository(destination, *, cancelled=lambda: False):
    """Reserve a new local folder explicitly; all other entrypoints open only."""
    _check(cancelled)
    destination = local_path(guard_data_directory(destination, purpose='incremental backup repository'))
    regular(destination.parent, directory=True)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    if any(os.path.lexists(parent / library_backup.DATABASE) for parent in destination.parents):
        raise RestoreError('The backup repository must be outside a library or full backup.')
    destination.mkdir(mode=0o700)
    _check(cancelled)
    Vault(destination, max_files=1_000_000, max_total_bytes=MAX_BYTES)
    _sync_directory(destination.parent)
    return {'path': str(destination), 'transport': 'carr-local-snapshot-vault/1', 'status': 'complete'}


def _outside_source(vault, app):
    root = local_path(app.root, exists=True)
    regular(root, directory=True)
    if vault.root.is_relative_to(root) or root.is_relative_to(vault.root):
        raise RestoreError('The backup repository must be outside the source library.')


@contextmanager
def _admitted(app, cancelled):
    """Use the application's existing coordinator before opening backup writers."""
    _check(cancelled)
    root = local_path(app.root, exists=True)
    regular(root, directory=True)
    broker = ResourceBroker.for_app(root, cancelled=cancelled)
    request = ResourceRequest(cpu_slots=1, memory_bytes=broker.limits.memory_bytes)
    try:
        with broker.reserve(request, operation_id='backup:incremental:' + uuid4().hex,
                            cancelled=cancelled, timeout=60) as lease:
            def stopped():
                lease.checkpoint()
                return cancelled()
            yield stopped
    except ResourceCancelled as exc:
        raise InterruptedError('Incremental backup cancelled during resource admission.') from exc


def create_snapshot(app, repository, *, parent=None, max_bytes=None,
                    cancelled=lambda: False):
    result = None
    try:
        with _admitted(app, cancelled) as stopped:
            result = _create_snapshot(app, repository, parent=parent, max_bytes=max_bytes,
                                      cancelled=stopped)
    except Exception:
        if result is None:
            raise
        # Publication already returned its immutable identity. Coordinator
        # cleanup cannot turn that saved snapshot into an unsent/cancelled job.
        result['resource_cleanup_pending'] = True
        result['status'] = 'attention'
    return result


def _create_snapshot(app, repository, *, parent=None, max_bytes=None,
                     cancelled=lambda: False):
    """Publish a verified complete directory through the shared chunk transport."""
    _check(cancelled)
    if parent is not None:
        _digest(parent)
    vault = _vault(repository, max_bytes=MAX_BYTES if max_bytes is None else max_bytes)
    _outside_source(vault, app)
    if vault.head() != parent:
        raise VaultConflict('The repository has a different current snapshot. Review its separately saved token.')
    temporary = TemporaryDirectory(prefix='.carr-incremental-', dir=vault.root.parent)
    try:
        stage = Path(temporary.name)
        if parent is not None:
            with TemporaryDirectory(prefix='parent-', dir=stage) as parent_stage:
                _full(vault, parent, Path(parent_stage) / 'full', cancelled)
        full = stage / 'full'
        canonical_digest = library_backup.create_backup(app, full, cancelled=cancelled)
        library_backup.verify_backup(full, canonical_digest, cancelled=cancelled)
        stats = dict(new_chunks=0, reused_chunks=0, new_bytes=0, reused_bytes=0)
        completed = 0
        def observe(value):
            nonlocal completed
            delta = value.bytes_completed - completed
            if value.chunks_written > stats['new_chunks']:
                stats['new_bytes'] += delta
            elif value.chunks_reused > stats['reused_chunks']:
                stats['reused_bytes'] += delta
            stats['new_chunks'], stats['reused_chunks'] = value.chunks_written, value.chunks_reused
            completed = value.bytes_completed
        receipt = vault.publish(full, expected_head=parent, label='Canonical complete-library backup',
                                cancelled=cancelled, on_progress=observe)
    except BaseException:
        _cleanup(temporary)
        raise
    result = {'snapshot_sha256': receipt.transfer_token,
              'backup_manifest_sha256': canonical_digest, 'parent': receipt.parent,
              'files': receipt.file_count, 'payload_bytes': receipt.total_bytes,
              'published': True, 'durability_confirmed': receipt.durability_confirmed,
              'lock_cleanup_pending': receipt.lock_cleanup_pending,
              'status': 'complete' if receipt.durability_confirmed and not receipt.lock_cleanup_pending else 'attention', **stats}
    # A post-commit cleanup failure is visible but cannot undo the saved snapshot.
    _cleanup(temporary, result)
    return result


def list_snapshots(repository, *, cancelled=lambda: False):
    """List committed HEAD ancestry, without trusting or verifying payloads.

    Vault's legacy listing also exposes complete orphan manifests for recovery.
    This user action deliberately follows the committed history only. A missing
    older parent stops history enumeration; explicit child-token restore remains
    independent of that parent and never replays a chain.
    """
    _check(cancelled)
    vault = _vault(repository)
    identity = vault.head()
    rows, seen = [], set()
    while identity is not None:
        _check(cancelled)
        if identity in seen or len(rows) >= MAX_HISTORY:
            raise RestoreError('Snapshot history is cyclic or exceeds its allowance.')
        seen.add(identity)
        try:
            value = vault._manifest(identity, identity)
        except FileNotFoundError:
            if not rows:
                raise
            rows[-1]['older_history_unavailable'] = True
            break
        manifest = next((row for row in value['files'] if row['path'] == library_backup.MANIFEST), None)
        if manifest is None:
            raise RestoreError('This snapshot uses another Vault envelope; select its portable-transfer workflow.')
        rows.append({'snapshot_sha256': identity, 'parent': value['parent'],
                     'created_at': value['created_at'],
                     'backup_manifest_sha256': manifest['sha256'],
                     'files': len(value['files']), 'payload_bytes': value['total_bytes'],
                     'payloads_verified': False})
        identity = value['parent']
    return rows


def verify_snapshot(repository, snapshot_sha256, *, cancelled=lambda: False):
    _check(cancelled)
    vault = _vault(repository)
    with TemporaryDirectory(prefix='.carr-incremental-', dir=vault.root.parent) as temporary:
        digest = _full(vault, snapshot_sha256, Path(temporary) / 'full', cancelled)
        value = vault._manifest(snapshot_sha256, snapshot_sha256)
    return {'snapshot_sha256': snapshot_sha256, 'backup_manifest_sha256': digest,
            'files': len(value['files']), 'payload_bytes': value['total_bytes'],
            'payloads_verified': True, 'status': 'verified'}


def materialize_snapshot(repository, snapshot_sha256, destination, *, cancelled=lambda: False):
    """Publish the validated ordinary backup through the shared exclusive rename."""
    _check(cancelled)
    vault = _vault(repository)
    destination = library_backup._destination(destination, vault.root)
    temporary = TemporaryDirectory(prefix='.carr-incremental-', dir=destination.parent)
    try:
        full = Path(temporary.name) / 'full'
        digest = _full(vault, snapshot_sha256, full, cancelled)
        _check(cancelled)
        vault._check_root()
        publish_directory_no_replace(full, destination)
    except BaseException:
        _cleanup(temporary)
        raise
    durable = True
    try:
        _sync_directory(destination.parent)
    except OSError:
        durable = False
    result = {'path': str(destination), 'snapshot_sha256': snapshot_sha256,
              'backup_manifest_sha256': digest, 'published': True,
              'durability_confirmed': durable, 'status': 'complete' if durable else 'attention'}
    _cleanup(temporary, result)
    return result


def restore_snapshot(repository, snapshot_sha256, destination, *, cancelled=lambda: False):
    """Restore only through canonical restore_backup; never open or resume jobs."""
    _check(cancelled)
    vault = _vault(repository)
    destination = library_backup._destination(destination, vault.root)
    temporary = TemporaryDirectory(prefix='.carr-incremental-', dir=vault.root.parent)
    try:
        full = Path(temporary.name) / 'full'
        digest = _full(vault, snapshot_sha256, full, cancelled)
        vault._check_root()
        restored = library_backup.restore_backup(full, destination, digest, cancelled=cancelled)
    except BaseException:
        _cleanup(temporary)
        raise
    result = {'path': str(restored), 'snapshot_sha256': snapshot_sha256,
              'backup_manifest_sha256': digest, 'restored': True, 'status': 'complete'}
    _cleanup(temporary, result)
    return result
