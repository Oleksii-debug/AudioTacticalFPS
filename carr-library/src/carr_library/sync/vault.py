"""Local, immutable transport for prepared backup directories or opaque payloads.

The caller creates and verifies a consistent backup before publishing it here.
This module neither opens SQLite nor interprets archives, encryption or secrets.
It uses the existing cooperative writer lock, never a network client. Selected
directories and their parents must remain private and stable during an operation;
this is not an OS sandbox against a hostile process replacing directory handles.

The independently retained transfer token authenticates the canonical manifest.
Digests obtained from ``head``/``list_snapshots`` are informational, not a trusted
substitute for that token. Content hashes detect corruption, not who authored an
otherwise unauthenticated snapshot. Cancelling a transfer keeps verified chunks
for the next attempt; incomplete manifests never become the current generation.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from tempfile import TemporaryDirectory, NamedTemporaryFile
import unicodedata

from ..ingest.writer_guard import exclusive_writer, WriterBusy


_DIGEST = re.compile(r'[0-9a-f]{64}')
_VAULT = b'{"kind":"carr-local-snapshot-vault","version":1}\n'
_MARKER = 'vault.json'
_HEAD = 'head.json'
_CHUNKS = 'chunks'
_SNAPSHOTS = 'snapshots'
_MAX_PATH = 4096
_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
             *(f'lpt{i}' for i in range(1, 10))}


class VaultError(ValueError):
    """An invalid or changed local snapshot cannot be transported safely."""


class VaultConflict(VaultError):
    """The expected parent is no longer the current vault generation."""


class VaultBusy(RuntimeError):
    """Another cooperating operation holds the vault writer lock."""


@dataclass(frozen=True)
class TransferReceipt:
    snapshot_id: str
    parent: str | None
    manifest_sha256: str
    transfer_token: str
    created_at: str
    label: str
    file_count: int
    total_bytes: int
    durability_warning: str | None = None


@dataclass(frozen=True)
class TransferProgress:
    stage: str
    files_completed: int
    files_total: int
    bytes_completed: int
    total_bytes: int
    chunks_reused: int = 0
    chunks_written: int = 0


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise VaultError('Duplicate snapshot manifest field.')
        value[key] = item
    return value


def _id(value, *, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise VaultError('A complete lowercase SHA256 snapshot identity is required.')
    return value


def _label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(unicodedata.category(c) in ('Cc', 'Cs') for c in value)):
        raise VaultError('Checkpoint label must be nonblank text of at most 200 characters.')
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or len(value) > _MAX_PATH
            or '\\' in value or ':' in value
            or any(unicodedata.category(c) in ('Cc', 'Cs') for c in value)):
        raise VaultError('Invalid portable snapshot path.')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or str(path) != value:
        raise VaultError('Invalid portable snapshot path.')
    for part in path.parts:
        if (part in ('.', '..') or part.endswith((' ', '.'))
                or any(c in part for c in '<>"|?*')
                or part.split('.')[0].casefold() in _RESERVED
                or PureWindowsPath(part).is_reserved()):
            raise VaultError('Invalid portable snapshot path.')
    return value


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise VaultError('Only ordinary, unlinked local files and directories are supported.')
    return info


def _local(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise VaultError('Choose an absolute local directory.') from None
    if (not isinstance(raw, str) or not raw or len(raw) > 32767
            or raw.startswith(('//', '\\\\')) or '://' in raw
            or any(unicodedata.category(c) in ('Cc', 'Cs') for c in raw)):
        raise VaultError('Choose an absolute local path; network paths and URIs are unsupported.')
    windows = PureWindowsPath(raw)
    if windows.drive.startswith('\\\\') or '..' in windows.parts or '..' in Path(raw).parts:
        raise VaultError('Network paths and parent traversal are unsupported.')
    path = Path(raw)
    if not path.is_absolute() or (os.name != 'nt' and ('\\' in raw or ':' in raw)):
        raise VaultError('Choose an absolute local path; network paths and URIs are unsupported.')
    if os.name == 'nt':
        # Also reject mapped remote drives, not only visible UNC spellings.
        kind = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
        if kind not in (2, 3, 6):  # removable, fixed, RAM disk
            raise VaultError('Choose a local writable drive.')
    for parent in reversed(path.parents):
        _plain(parent, directory=True)
    return path


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read(path, limit):
    before = _plain(path)
    if before.st_size > limit:
        raise VaultError('Snapshot metadata exceeds the configured resource budget.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise VaultError('Snapshot file changed while opening.')
        raw = stream.read(limit + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise VaultError('Snapshot file changed while reading.')
    if len(raw) != before.st_size or _identity(_plain(path)) != _identity(before):
        raise VaultError('Snapshot file changed while reading.')
    return raw


def _write_file(path, raw, *, replace=False):
    """Flush new bytes, then publish one file without exposing partial contents."""
    temporary = None
    try:
        with NamedTemporaryFile(prefix='.snapshot-', suffix='.tmp', dir=path.parent,
                                delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            if os.path.lexists(path):
                _plain(path)
            os.replace(temporary, path)
            temporary = None
        else:
            if os.name == 'nt':
                # Windows rename is exclusive even for an existing empty file.
                # Unlike hard links, it is available on FAT/exFAT removable media.
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
                temporary.unlink()
            temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_directory(stage, destination):
    """Publish through the shared native exclusive-rename primitive."""
    from ..file_publication import publish_directory_no_replace
    try:
        publish_directory_no_replace(stage, destination)
    except RuntimeError as exc:
        raise VaultError(str(exc)) from exc
    _sync_directory(destination.parent)


class Vault:
    """Bounded local snapshot transport; no live database sync or implicit merge.

    Budgets are explicit and configurable. They bound metadata/IO rather than
    declaring a product duration or document-count ceiling. Valid reused chunks
    are rehashed on every transfer. Corruption fails closed and is never silently
    overwritten. Unknown vault files are left untouched.
    """

    def __init__(self, path, *, chunk_bytes=4 * 1024 * 1024,
                 max_files=100_000, max_total_bytes=1024**4,
                 max_manifest_bytes=64 * 1024 * 1024):
        for value, low, high in ((chunk_bytes, 1024, 16 * 1024 * 1024),
                                 (max_files, 1, 1_000_000),
                                 (max_total_bytes, 1, 1024**5),
                                 (max_manifest_bytes, 4096, 256 * 1024 * 1024)):
            if type(value) is not int or not low <= value <= high:
                raise VaultError('Invalid snapshot resource budget.')
        self.chunk_bytes = chunk_bytes
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_manifest_bytes = max_manifest_bytes
        self.root = _local(path)
        self.root.mkdir(mode=0o700, exist_ok=True)
        info = _plain(self.root, directory=True)
        self._root_identity = info.st_dev, info.st_ino
        with self._locked():
            marker = self.root / _MARKER
            if not os.path.lexists(marker):
                if any(os.path.lexists(self.root / name) for name in (_HEAD, _CHUNKS, _SNAPSHOTS)):
                    raise VaultError('Existing files do not identify a complete local snapshot vault.')
                _write_file(marker, _VAULT)
            elif _read(marker, 4096) != _VAULT:
                raise VaultError('Unsupported local snapshot vault format.')
            for name in (_CHUNKS, _SNAPSHOTS):
                folder = self.root / name
                folder.mkdir(mode=0o700, exist_ok=True)
                _plain(folder, directory=True)
            _sync_directory(self.root)

    def _check_root(self):
        _local(self.root)
        info = _plain(self.root, directory=True)
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise VaultError('Selected snapshot vault changed.')

    @contextmanager
    def _locked(self):
        self._check_root()
        try:
            with exclusive_writer(self.root):
                self._check_root()
                yield
        except WriterBusy:
            raise VaultBusy('Another local checkpoint operation is running.') from None

    @staticmethod
    def _callbacks(cancelled, on_progress):
        if not callable(cancelled) or (on_progress is not None and not callable(on_progress)):
            raise VaultError('Cancellation and progress handlers must be callable.')

        def check():
            if cancelled():
                raise InterruptedError('Local checkpoint transfer cancelled; verified chunks remain reusable.')
        return check

    def _head(self):
        path = self.root / _HEAD
        if not os.path.lexists(path):
            return None
        raw = _read(path, 4096)
        try:
            value = json.loads(raw, object_pairs_hook=_unique)
            if (type(value) is not dict or set(value) != {'version', 'snapshot'}
                    or type(value['version']) is not int or value['version'] != 1
                    or _canonical(value) != raw):
                raise VaultError('Invalid checkpoint head.')
            identity = _id(value['snapshot'])
            self._manifest(identity, identity)
            return identity
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise VaultError('Invalid checkpoint head.') from None

    def head(self):
        """Return the current informational digest, not independent trust evidence."""
        self._check_root()
        return self._head()

    def _inventory(self, source, check):
        files, directories, stamps, seen = [], [], {}, set()
        total, path_bytes = 0, 0
        pending = [(source, '')]
        while pending:
            folder, prefix = pending.pop()
            check()
            stamps[prefix] = _identity(_plain(folder, directory=True))
            with os.scandir(folder) as entries:
                for child in entries:
                    check()
                    relative = _relative(prefix + '/' + child.name if prefix else child.name)
                    # Bound names while enumerating, before retaining a large
                    # inventory or constructing its canonical JSON representation.
                    path_bytes += len(relative.encode('utf-8'))
                    if path_bytes > self.max_manifest_bytes:
                        raise VaultError('Snapshot paths exceed the configured manifest budget.')
                    portable_key = unicodedata.normalize('NFC', relative).casefold()
                    if portable_key in seen:
                        raise VaultError('Snapshot paths collide on a portable filesystem.')
                    seen.add(portable_key)
                    if len(seen) > self.max_files:
                        raise VaultError('Snapshot file count exceeds the configured resource budget.')
                    path = source / relative
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        _plain(path, directory=True)
                        directories.append(relative)
                        pending.append((path, relative))
                    else:
                        info = _plain(path)
                        total += info.st_size
                        if total > self.max_total_bytes:
                            raise VaultError('Snapshot bytes exceed the configured resource budget.')
                        files.append((relative, _identity(info)))
        return tuple(sorted(files)), tuple(sorted(directories)), stamps, total

    def _chunk_path(self, digest, *, create=False):
        _id(digest)
        chunks = self.root / _CHUNKS
        _plain(chunks, directory=True)
        folder = chunks / digest[:2]
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        _plain(folder, directory=True)
        return folder / digest[2:]

    def _chunk(self, digest, expected_size, check):
        check()
        path = self._chunk_path(digest)
        raw = _read(path, expected_size)
        if len(raw) != expected_size or sha256(raw).hexdigest() != digest:
            raise VaultError('Stored checkpoint chunk failed integrity validation.')
        check()
        return raw

    def _put_chunk(self, raw, check):
        check()
        digest = sha256(raw).hexdigest()
        path = self._chunk_path(digest, create=True)
        if os.path.lexists(path):
            self._chunk(digest, len(raw), check)
            return digest, True
        _write_file(path, raw)
        _sync_directory(path.parent.parent)
        check()
        return digest, False

    @staticmethod
    def _receipt(identity, manifest):
        return TransferReceipt(identity, manifest['parent'], identity, identity,
                               manifest['created_at'], manifest['label'],
                               len(manifest['files']), manifest['total_bytes'])

    def publish(self, source_directory, *, expected_head, label='Portable checkpoint',
                cancelled=lambda: False, on_progress=None):
        """Publish an already verified stable directory, comparing its expected parent.

        Pass ``None`` only for an empty vault. A later generation requires the
        previously inspected head explicitly. This method never selects all
        libraries, creates a backup, or reads credentials on the caller's behalf.
        """
        check = self._callbacks(cancelled, on_progress)
        _id(expected_head, optional=True)
        _label(label)
        check()
        source = _local(source_directory)
        _plain(source, directory=True)
        if source.is_relative_to(self.root) or self.root.is_relative_to(source):
            raise VaultError('Prepared payload and vault must be separate directories.')
        with self._locked():
            if self._head() != expected_head:
                raise VaultConflict('Checkpoint head changed; review the current generation before publishing.')
            inventory = self._inventory(source, check)
            files, directories, _, total = inventory
            chunk_count = sum((info[2] + self.chunk_bytes - 1) // self.chunk_bytes for _, info in files)
            metadata_bytes = (512 + len(label.encode('utf-8'))
                + sum(len(path.encode('utf-8')) + 192 for path, _ in files)
                + sum(len(path.encode('utf-8')) + 8 for path in directories))
            if metadata_bytes + chunk_count * 67 > self.max_manifest_bytes:
                raise VaultError('Chunk inventory exceeds the configured manifest budget.')
            rows, completed, reused, written = [], 0, 0, 0
            for index, (relative, expected) in enumerate(files):
                check()
                path = source / relative
                if _identity(_plain(path)) != expected:
                    raise VaultError('Prepared payload changed before transfer.')
                digest, chunks, count = sha256(), [], 0
                flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                with os.fdopen(os.open(path, flags), 'rb') as stream:
                    if _identity(os.fstat(stream.fileno())) != expected:
                        raise VaultError('Prepared payload changed while opening.')
                    while count < expected[2]:
                        check()
                        raw = stream.read(min(self.chunk_bytes, expected[2] - count))
                        if not raw:
                            raise VaultError('Prepared payload was truncated during transfer.')
                        digest.update(raw)
                        identity, used = self._put_chunk(raw, check)
                        chunks.append(identity)
                        count += len(raw)
                        completed += len(raw)
                        reused += int(used)
                        written += int(not used)
                        if on_progress is not None:
                            on_progress(TransferProgress('publish', index, len(files),
                                completed, total, reused, written))
                        check()
                    if stream.read(1) or _identity(os.fstat(stream.fileno())) != expected:
                        raise VaultError('Prepared payload changed during transfer.')
                if _identity(_plain(path)) != expected:
                    raise VaultError('Prepared payload changed during transfer.')
                rows.append(dict(path=relative, size=count, sha256=digest.hexdigest(), chunks=chunks))
                if on_progress is not None:
                    on_progress(TransferProgress('publish', index + 1, len(files),
                        completed, total, reused, written))
                check()
            if self._inventory(source, check) != inventory:
                raise VaultError('Prepared payload tree changed during transfer.')
            manifest = dict(version=1, kind='carr-portable-snapshot', parent=expected_head,
                created_at=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z'),
                label=label, chunk_bytes=self.chunk_bytes, directories=list(directories),
                files=rows, total_bytes=total)
            raw = _canonical(manifest)
            if len(raw) > self.max_manifest_bytes:
                raise VaultError('Snapshot manifest exceeds the configured resource budget.')
            identity = sha256(raw).hexdigest()
            self._validate_manifest(manifest)
            check()
            self._check_root()
            if self._head() != expected_head:
                raise VaultConflict('Checkpoint head changed before publication.')
            snapshots = self.root / _SNAPSHOTS
            _plain(snapshots, directory=True)
            target = snapshots / (identity + '.json')
            if os.path.lexists(target):
                if _read(target, self.max_manifest_bytes) != raw:
                    raise VaultError('Snapshot identity already has different content.')
            else:
                _write_file(target, raw)
            # The last cancellation check precedes the single HEAD commit. No
            # user callback runs after successful publication or reports failure
            # after that commit. A crash before HEAD may leave a complete orphan.
            check()
            try:
                _write_file(self.root / _HEAD, _canonical(dict(version=1, snapshot=identity)), replace=True)
            except (Exception, KeyboardInterrupt) as error:
                # A directory sync or temporary-file cleanup can fail after the
                # atomic HEAD replacement. Confirm this exact new generation
                # while still holding its writer lock before reporting success.
                try:
                    committed = (self._head() == identity
                                 and _read(target, self.max_manifest_bytes) == raw)
                except (Exception, KeyboardInterrupt):
                    raise error
                if not committed:
                    raise
                return replace(self._receipt(identity, manifest), durability_warning=(
                    'Checkpoint published; final directory durability or temporary cleanup was not confirmed.'))
            return self._receipt(identity, manifest)

    def _validate_manifest(self, value):
        fields = {'version', 'kind', 'parent', 'created_at', 'label', 'chunk_bytes',
                  'directories', 'files', 'total_bytes'}
        if (type(value) is not dict or set(value) != fields
                or type(value['version']) is not int or value['version'] != 1
                or value['kind'] != 'carr-portable-snapshot'):
            raise VaultError('Unsupported checkpoint manifest.')
        _id(value['parent'], optional=True)
        _label(value['label'])
        stamp = value['created_at']
        if (not isinstance(stamp, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z', stamp)):
            raise VaultError('Invalid checkpoint timestamp.')
        try:
            datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        except ValueError:
            raise VaultError('Invalid checkpoint timestamp.') from None
        size = value['chunk_bytes']
        if (type(size) is not int or not 1024 <= size <= 16 * 1024 * 1024
                or type(value['total_bytes']) is not int or not 0 <= value['total_bytes'] <= self.max_total_bytes
                or type(value['directories']) is not list or type(value['files']) is not list
                or len(value['directories']) + len(value['files']) > self.max_files):
            raise VaultError('Checkpoint exceeds configured resource budgets.')
        directories, seen = set(), set()
        for path in value['directories']:
            _relative(path)
            key = unicodedata.normalize('NFC', path).casefold()
            if key in seen:
                raise VaultError('Duplicate or colliding checkpoint path.')
            seen.add(key)
            directories.add(path)
        if value['directories'] != sorted(directories):
            raise VaultError('Checkpoint directory order is not canonical.')
        total, paths = 0, []
        for row in value['files']:
            if (type(row) is not dict or set(row) != {'path', 'size', 'sha256', 'chunks'}
                    or type(row['size']) is not int or not 0 <= row['size'] <= self.max_total_bytes
                    or type(row['chunks']) is not list):
                raise VaultError('Invalid checkpoint file.')
            path = _relative(row['path'])
            key = unicodedata.normalize('NFC', path).casefold()
            if key in seen:
                raise VaultError('Duplicate or colliding checkpoint path.')
            seen.add(key)
            paths.append(path)
            _id(row['sha256'])
            if len(row['chunks']) != (row['size'] + size - 1) // size:
                raise VaultError('Checkpoint chunk count does not match file size.')
            for identity in row['chunks']:
                _id(identity)
            total += row['size']
        if paths != sorted(paths) or total != value['total_bytes']:
            raise VaultError('Checkpoint inventory is inconsistent.')
        for path in [*directories, *paths]:
            for parent in PurePosixPath(path).parents:
                if str(parent) != '.' and str(parent) not in directories:
                    raise VaultError('Checkpoint parent directory is missing.')
        return value

    def _manifest(self, identity, token):
        _id(identity)
        _id(token)
        if identity != token:
            raise VaultError('Independent transfer token does not match the selected checkpoint.')
        snapshots = self.root / _SNAPSHOTS
        _plain(snapshots, directory=True)
        raw = _read(snapshots / (identity + '.json'), self.max_manifest_bytes)
        if sha256(raw).hexdigest() != token:
            raise VaultError('Checkpoint manifest failed independent-token validation.')
        try:
            manifest = json.loads(raw, object_pairs_hook=_unique)
            self._validate_manifest(manifest)
            if _canonical(manifest) != raw:
                raise VaultError('Checkpoint manifest is not canonical.')
            return manifest
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise VaultError('Invalid checkpoint manifest.') from None

    def list_snapshots(self):
        """List untrusted descriptive receipts; restoration still requires a token.

        Complete manifests left before a crashed HEAD publication may appear here.
        Listing is not evidence that a checkpoint is the selected current branch.
        Unknown names are ignored, never removed or interpreted as payloads.
        """
        self._check_root()
        folder = self.root / _SNAPSHOTS
        _plain(folder, directory=True)
        rows = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if not re.fullmatch(r'[0-9a-f]{64}\.json', entry.name):
                    continue
                if len(rows) >= self.max_files:
                    raise VaultError('Checkpoint list exceeds the configured resource budget.')
                identity = entry.name[:-5]
                rows.append(self._receipt(identity, self._manifest(identity, identity)))
        return tuple(sorted(rows, key=lambda row: (row.created_at, row.snapshot_id)))

    def materialize(self, snapshot_id, destination, *, expected_token,
                    cancelled=lambda: False, on_progress=None):
        """Verify every chunk and publish a complete tree at a NEW local directory.

        This only transports bytes. The caller still verifies/decrypts the
        prepared backup and invokes the normal library restore workflow. No
        existing directory, live database or evidence is ever overwritten.
        """
        check = self._callbacks(cancelled, on_progress)
        check()
        target = _local(destination)
        if os.path.lexists(target):
            raise FileExistsError('Restore destination already exists.')
        if target.is_relative_to(self.root) or self.root.is_relative_to(target):
            raise VaultError('Materialized payload and vault must be separate directories.')
        with self._locked():
            manifest = self._manifest(snapshot_id, expected_token)
            files = manifest['files']
            completed = 0
            with TemporaryDirectory(prefix='.carr-snapshot-', dir=target.parent) as temporary:
                stage = Path(temporary)
                for directory in manifest['directories']:
                    check()
                    (stage / directory).mkdir(mode=0o700)
                for index, row in enumerate(files):
                    check()
                    digest, count = sha256(), 0
                    with (stage / row['path']).open('xb') as stream:
                        for identity in row['chunks']:
                            remaining = min(manifest['chunk_bytes'], row['size'] - count)
                            raw = self._chunk(identity, remaining, check)
                            stream.write(raw)
                            digest.update(raw)
                            count += len(raw)
                            completed += len(raw)
                            if on_progress is not None:
                                on_progress(TransferProgress('materialize', index, len(files),
                                    completed, manifest['total_bytes']))
                            check()
                        stream.flush()
                        os.fsync(stream.fileno())
                    if count != row['size'] or digest.hexdigest() != row['sha256']:
                        raise VaultError('Materialized checkpoint file failed integrity validation.')
                    if on_progress is not None:
                        on_progress(TransferProgress('materialize', index + 1, len(files),
                            completed, manifest['total_bytes']))
                    check()
                for directory in reversed(manifest['directories']):
                    _sync_directory(stage / directory)
                _sync_directory(stage)
                check()
                self._check_root()
                _local(target)
                _publish_directory(stage, target)
            return target
