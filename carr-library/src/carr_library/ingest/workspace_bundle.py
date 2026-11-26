"""Complete published V3 library backup with registered original source bytes.

Reuses Storage.backup, its strict schema/integrity checker and the existing
catalog_asset_copy/RetainedAssets services. Includes every workspace, historical
revision, metadata snapshot, topic, evidence, note and saved query. Unfinished
ingest jobs and their unregistered temporary originals are not published data.

Callers own stable private local parents and hold the cooperative library writer
guard. A separately retained manifest hash checks integrity; obtaining that hash
from an untrusted bundle itself does not authenticate the bundle.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from tempfile import TemporaryDirectory

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from ..storage.revisions import Storage, _expected_signature, _schema_signature
from .bundle import _unique_object
from .catalog_asset_copy import AssetEntry, copy_catalog_assets, verify_catalog_assets
from .restore import RestoreError, _sidecars
from .retained_assets import RetainedAssets, MAX_BYTES, SUFFIXES

SNAPSHOT = 'library.sqlite'
MANIFEST = 'manifest.json'
ORIGINALS = 'originals'
PENDING = 'restore.pending'
MAX_MANIFEST = 2_000_000
MAX_SNAPSHOT = 512 * 1024 * 1024
MAX_ASSETS = 10000
MAX_ASSET_BYTES = 8 * 1024**3


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _plain(path, *, directory=False):
    info = path.lstat()
    allowed = stat.S_ISDIR if directory else stat.S_ISREG
    if (not allowed(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise RestoreError('bundle paths must be private regular local files or directories')
    return info


def _fresh(directory, source):
    directory = Path(directory).absolute()
    _plain(directory.parent, directory=True)
    if os.path.lexists(directory):
        raise FileExistsError(directory)
    resolved = directory.resolve()
    source = Path(source).resolve(strict=True)
    if resolved.is_relative_to(source) or source.is_relative_to(resolved):
        raise RestoreError('destination overlaps source directory')
    return directory


def _limits(max_snapshot_bytes, max_assets, max_asset_bytes):
    if (type(max_snapshot_bytes) is not int or not 0 < max_snapshot_bytes <= MAX_SNAPSHOT
            or type(max_assets) is not int or not 0 < max_assets <= MAX_ASSETS
            or type(max_asset_bytes) is not int or not 0 < max_asset_bytes <= MAX_ASSET_BYTES):
        raise RestoreError('invalid bundle resource limits')
    return dict(include_all_workspaces=True, max_assets=max_assets, max_bytes=max_asset_bytes)


def _canonical(manifest):
    return (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')


def _write(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


@contextmanager
def _verified_snapshot(path, size, expected):
    if _plain(path).st_size != size or _sidecars(path) or hash_file(path) != expected:
        raise RestoreError('snapshot size, digest or sidecar mismatch')
    with path.open('rb') as stream:
        if stream.read(16) != b'SQLite format 3\x00':
            raise RestoreError('not a SQLite snapshot')
    # Inspect only a closed private snapshot before opening it writable. The
    # exact known schema excludes extra trigger/view/extension SQL.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if (db.execute('PRAGMA user_version').fetchone()[0] != 3
                or _schema_signature(db) != _expected_signature(3)):
            raise RestoreError('snapshot schema mismatch')
    with closing(Storage(path, schema_version=3)) as storage:
        storage.check()
        with storage.transaction():
            yield storage.db
    if _sidecars(path) or hash_file(path) != expected:
        raise RestoreError('validation changed snapshot bytes')


def create_workspace_bundle(storage, retained, directory, *, include_all_workspaces=False,
                            max_snapshot_bytes=MAX_SNAPSHOT, max_assets=MAX_ASSETS,
                            max_asset_bytes=MAX_ASSET_BYTES):
    """Publish a fresh directory and return its independently retainable SHA256.

    The manifest is linked last. Failure may leave an incomplete destination;
    never overwrite it or treat its files as a completed bundle without validation.
    """
    if include_all_workspaces is not True:
        raise RestoreError('explicit all-workspaces backup consent required')
    if not isinstance(storage, Storage) or storage.schema_version != 3:
        raise RestoreError('shared V3 library required')
    limits = _limits(max_snapshot_bytes, max_assets, max_asset_bytes)
    directory = _fresh(directory, retained.root.parent)
    with TemporaryDirectory(prefix='.carr-workspace-backup-', dir=directory.parent) as temporary:
        stage = Path(temporary)
        snapshot = stage / SNAPSHOT
        storage.backup(snapshot)
        size = _plain(snapshot).st_size
        if not 0 < size <= max_snapshot_bytes:
            raise RestoreError('snapshot exceeds bundle size limit')
        expected = hash_file(snapshot)
        with _verified_snapshot(snapshot, size, expected) as db:
            entries = copy_catalog_assets(db, retained, stage / ORIGINALS, **limits)
        manifest = dict(version=1, kind='workspace-library', schema_version=3,
                        pending_imports_included=False,
                        snapshot=dict(name=SNAPSHOT, size_bytes=size, sha256=expected),
                        assets=[asdict(entry) for entry in entries])
        raw = _canonical(manifest)
        if len(raw) > MAX_MANIFEST:
            raise RestoreError('manifest size limit')
        _write(stage / MANIFEST, raw)
        directory.mkdir(mode=0o700)
        (directory / ORIGINALS).mkdir(mode=0o700)
        for entry in entries:
            os.link(stage / ORIGINALS / entry.filename, directory / ORIGINALS / entry.filename)
        os.link(snapshot, directory / SNAPSHOT)
        os.link(stage / MANIFEST, directory / MANIFEST)
        result = sha256(raw).hexdigest()
    return result


def _read_manifest(directory, expected, max_snapshot_bytes, max_assets, max_asset_bytes):
    if not _digest(expected):
        raise RestoreError('independent manifest SHA256 required')
    _plain(directory, directory=True)
    if {p.name for p in directory.iterdir()} != {SNAPSHOT, MANIFEST, ORIGINALS}:
        raise RestoreError('incomplete bundle or unexpected files')
    _plain(directory / ORIGINALS, directory=True)
    manifest_path = directory / MANIFEST
    if not 0 < _plain(manifest_path).st_size <= MAX_MANIFEST:
        raise RestoreError('manifest size limit')
    with manifest_path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST or sha256(raw).hexdigest() != expected:
        raise RestoreError('manifest digest mismatch')
    try:
        manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('invalid manifest') from exc
    if (not isinstance(manifest, dict) or set(manifest) != {
            'version', 'kind', 'schema_version', 'pending_imports_included', 'snapshot', 'assets'}
            or type(manifest['version']) is not int or manifest['version'] != 1
            or manifest['kind'] != 'workspace-library'
            or type(manifest['schema_version']) is not int or manifest['schema_version'] != 3
            or manifest['pending_imports_included'] is not False or _canonical(manifest) != raw):
        raise RestoreError('unsupported or noncanonical manifest')
    snapshot = manifest['snapshot']
    if (not isinstance(snapshot, dict) or set(snapshot) != {'name', 'sha256', 'size_bytes'}
            or snapshot['name'] != SNAPSHOT or not _digest(snapshot['sha256'])
            or type(snapshot['size_bytes']) is not int
            or not 0 < snapshot['size_bytes'] <= max_snapshot_bytes):
        raise RestoreError('invalid snapshot manifest')
    assets = manifest['assets']
    if not isinstance(assets, list) or len(assets) > max_assets:
        raise RestoreError('asset count limit')
    entries = []
    for row in assets:
        if (not isinstance(row, dict) or set(row) != {'digest', 'mime', 'size'}
                or not _digest(row['digest']) or not isinstance(row['mime'], str)
                or row['mime'] not in SUFFIXES
                or type(row['size']) is not int or not 0 < row['size'] <= MAX_BYTES):
            raise RestoreError('invalid asset manifest')
        entries.append(AssetEntry(**row))
    if (len({e.digest for e in entries}) != len(entries)
            or entries != sorted(entries, key=lambda e: e.digest)
            or sum(e.size for e in entries) > max_asset_bytes):
        raise RestoreError('ambiguous or oversized asset manifest')
    return snapshot, tuple(entries)


def _copy_snapshot(source, destination, size, expected):
    """Bounded closed-file copy, using the existing revision-restore pattern."""
    info = _plain(source)
    if info.st_size != size or _sidecars(source):
        raise RestoreError('snapshot size or sidecar mismatch')
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream, destination.open('xb') as target:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(info, opened) or opened.st_size != size:
            raise RestoreError('snapshot changed before copy')
        remaining = size
        while remaining:
            chunk = stream.read(min(65536, remaining))
            if not chunk:
                raise RestoreError('snapshot truncated')
            target.write(chunk)
            remaining -= len(chunk)
        if stream.read(1) or _sidecars(source):
            raise RestoreError('snapshot changed during copy')
        target.flush()
        os.fsync(target.fileno())
    if hash_file(destination) != expected:
        raise RestoreError('snapshot digest mismatch')


def _publish_directory(source, destination):
    """Atomic same-filesystem directory publication with no replacement.

    Windows rename refuses an existing destination. POSIX rename can replace
    an existing empty directory, so Linux uses its native RENAME_NOREPLACE
    operation. Other platforms fail closed rather than weakening this boundary.
    File bytes have been fsynced; power-loss directory durability is not claimed.
    """
    if os.name == 'nt':
        os.rename(source, destination)
        return
    if sys.platform.startswith('linux'):
        import ctypes
        native = ctypes.CDLL(None, use_errno=True)
        rename = getattr(native, 'renameat2', None)
        if rename is None:
            raise RestoreError('atomic no-replace directory publication unavailable')
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), os.fspath(destination))
        return
    raise RestoreError('atomic no-replace directory publication unavailable')


def restore_workspace_bundle(directory, destination, *, expected_manifest_sha256,
                             max_snapshot_bytes=MAX_SNAPSHOT, max_assets=MAX_ASSETS,
                             max_asset_bytes=MAX_ASSET_BYTES):
    """Verify the full closed bundle before creating a fresh library directory.

    All work, including its pending marker, stays in a private staging directory.
    The final name appears only after verification in one atomic no-replace
    operation. Interruption before publication leaves no partial final library.
    """
    limits = _limits(max_snapshot_bytes, max_assets, max_asset_bytes)
    directory = Path(directory).absolute()
    snapshot, entries = _read_manifest(directory, expected_manifest_sha256,
                                       max_snapshot_bytes, max_assets, max_asset_bytes)
    destination = _fresh(destination, directory)
    with TemporaryDirectory(prefix='.carr-workspace-restore-', dir=destination.parent) as temporary:
        stage = Path(temporary)
        _write(stage / PENDING,
               _canonical(dict(kind='workspace-restore-pending', manifest_sha256=expected_manifest_sha256)))
        staged_snapshot = stage / SNAPSHOT
        _copy_snapshot(directory / SNAPSHOT, staged_snapshot,
                       snapshot['size_bytes'], snapshot['sha256'])
        with _verified_snapshot(staged_snapshot, snapshot['size_bytes'], snapshot['sha256']) as db:
            source = RetainedAssets(directory / ORIGINALS)
            actual = verify_catalog_assets(db, source, **limits)
            if actual != entries:
                raise RestoreError('manifest does not match catalog assets')
            copy_catalog_assets(db, source, stage / ORIGINALS, **limits)
        (stage / PENDING).unlink()
        _publish_directory(stage, destination)
    return destination
