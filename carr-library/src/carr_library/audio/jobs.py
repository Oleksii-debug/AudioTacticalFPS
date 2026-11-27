"""Crash-safe local checkpoints and content-addressed audio cache."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid


class RenderCancelled(RuntimeError):
    """The current step stopped; previously sealed chunks remain reusable."""


def check_cancelled(cancelled):
    if cancelled():
        raise RenderCancelled('Audio production cancelled. Completed chunks are retained for resume.')


def checked_directory(path, *, create=False):
    path = Path(path).absolute()
    if str(path).startswith(('\\\\', '//')) or any(':' in part for part in path.parts[1:]):
        raise ValueError('Audio projects must use local paths, without network shares or alternate streams.')
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise ValueError('Audio project paths cannot contain symbolic links.')
        if parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Audio project paths cannot contain reparse points.')
        if parent.exists() and not parent.is_dir():
            raise ValueError('Expected a project directory.')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('The project directory does not exist.')
    return path.resolve()


def regular_file(path):
    path = Path(path)
    checked_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Expected a regular local audio file.')
    return info


def file_identity(info):
    """Bind validation to the bytes inspected, including same-size replacements."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def verified_reader(path, *, expected_identity=None):
    before = file_identity(regular_file(path))
    if expected_identity is not None and before != expected_identity:
        raise RuntimeError('An audio input changed after it was validated. Restart the production.')
    with Path(path).open('rb') as stream:
        if file_identity(os.fstat(stream.fileno())) != before:
            raise RuntimeError('An audio input changed while it was being opened.')
        yield stream
        if file_identity(os.fstat(stream.fileno())) != before or file_identity(regular_file(path)) != before:
            raise RuntimeError('An audio input changed while it was being read. Restart the production.')


def sha256_file(path, *, cancelled=lambda: False, expected_identity=None):
    result = hashlib.sha256()
    with verified_reader(path, expected_identity=expected_identity) as stream:
        remaining = os.fstat(stream.fileno()).st_size
        while remaining:
            check_cancelled(cancelled)
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError('An audio input was truncated during verification.')
            result.update(block)
            remaining -= len(block)
        check_cancelled(cancelled)
    return result.hexdigest()


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')


def atomic_json(path, value):
    path = Path(path)
    parent = checked_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('Unsafe checkpoint path.')
    encoded = _json_bytes(value)
    descriptor, name = tempfile.mkstemp(prefix='.checkpoint-', dir=parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def publish_noreplace(source, destination):
    """Move a complete local stage atomically without replacing any destination.

    Windows rename has no-replace semantics. Linux needs RENAME_NOREPLACE,
    including for empty destination directories. No hardlink aliases remain
    after a crash. A directory-sync error can occur after the move succeeds.
    """
    source, destination = Path(source), Path(destination)
    checked_directory(source.parent)
    checked_directory(destination.parent)
    if source.is_dir():
        checked_directory(source)
        _sync_directory(source)
    else:
        regular_file(source)
        # Windows CRT rejects fsync on read-only file descriptors. Stages are
        # writable; immutable/read-only inputs use publish_new's private copy.
        with source.open('r+b') as stream:
            os.fsync(stream.fileno())
    if os.name == 'nt':
        os.rename(source, destination)
    elif sys.platform.startswith('linux'):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, 'renameat2', None)
        if rename is None:
            raise RuntimeError('This runtime does not support atomic no-replace publication.')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    else:
        raise RuntimeError('Atomic audio publication currently supports Windows and Linux.')
    _sync_directory(destination.parent)
    if source.parent != destination.parent:
        _sync_directory(source.parent)


def publish_new(source, destination, *, cancelled=lambda: False, expected_identity=None):
    """Publish an independent, durable copy without altering a source or output.

    Copy in bounded blocks to a private writable stage. Exporting a cache chunk
    must not leave a shared inode that an editor can modify through the export.
    """
    source, destination = Path(source), Path(destination)
    parent = checked_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'Audio output already exists: {destination.name}')
    descriptor, name = tempfile.mkstemp(prefix='.publish-', dir=parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            with verified_reader(source, expected_identity=expected_identity) as stream:
                remaining = os.fstat(stream.fileno()).st_size
                while remaining:
                    check_cancelled(cancelled)
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise RuntimeError('An audio input was truncated during publication.')
                    target.write(block)
                    remaining -= len(block)
            target.flush()
            os.fsync(target.fileno())
        check_cancelled(cancelled)
        publish_noreplace(stage, destination)
    finally:
        stage.unlink(missing_ok=True)


def storage_bytes(root, *, cancelled=lambda: False):
    """Count actual project storage, including quarantine and unfinished stages.

    Links are counted themselves, never followed outside the selected root.
    Counting each hardlink is conservative for projects from older versions.
    """
    pending, total = [Path(root)], 0
    while pending:
        check_cancelled(cancelled)
        try:
            entries = os.scandir(pending.pop())
        except FileNotFoundError:
            continue  # A subprocess may have just removed its private stage.
        with entries:
            for entry in entries:
                check_cancelled(cancelled)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400:
                    pending.append(Path(entry.path))
                else:
                    total += info.st_size
    return total


@contextmanager
def project_lock(root):
    """OS lock is released on process death, unlike a persistent lease file."""
    root = checked_directory(root, create=True)
    path = root / '.render.lock'
    if path.is_symlink():
        raise ValueError('Unsafe render lock.')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Render lock must be a regular, unlinked local file.')
        if info.st_size == 0:
            os.write(descriptor, b'0')
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another render is already using this project. Try again when it finishes.') from exc
        yield root
    finally:
        os.close(descriptor)


class ChunkCache:
    def __init__(self, root, *, cancelled=lambda: False, disk_budget_bytes=32 * 1024 ** 3, budget_root=None):
        if type(disk_budget_bytes) is not int or disk_budget_bytes < 0:
            raise ValueError('Invalid audio disk budget.')
        self.root = checked_directory(root, create=True)
        self.cancelled = cancelled
        self.disk_budget_bytes = disk_budget_bytes
        self.budget_root = checked_directory(budget_root) if budget_root is not None else self.root
        self.used = self._retained_bytes()

    def _retained_bytes(self):
        """Count the full selected project, including retained corrupt payloads."""
        return storage_bytes(self.budget_root, cancelled=self.cancelled)

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid chunk identity.')
        return key

    def reserve(self, expected_bytes):
        check_cancelled(self.cancelled)
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise ValueError('Invalid audio disk estimate.')
        self.used = self._retained_bytes()
        if self.used + expected_bytes > self.disk_budget_bytes:
            raise RuntimeError('The project disk budget would be exceeded. Increase the budget or use smaller outputs.')
        if shutil.disk_usage(self.root).free < expected_bytes + 16 * 1024 ** 2:
            raise OSError('There is not enough free space to finish this audio step.')

    def get(self, key):
        key = self._key(key)
        audio, meta = self.root / (key + '.wav'), self.root / (key + '.json')
        if not audio.exists() and not meta.exists():
            return None
        try:
            if regular_file(meta).st_size > 65536:
                raise ValueError('Oversized chunk checkpoint.')
            info = json.loads(meta.read_text('utf-8'))
            if not isinstance(info, dict) or info.get('schema_version') != 1 or info.get('key') != key:
                raise ValueError('Invalid chunk checkpoint.')
            if regular_file(audio).st_size != info['size'] or sha256_file(audio, cancelled=self.cancelled) != info['sha256']:
                raise ValueError('Audio chunk checksum mismatch.')
            from .ffmpeg import wav_info
            current = wav_info(audio)
            if (current.frames, current.rate, current.channels) != (info['frames'], info['sample_rate'], info['channels']):
                raise ValueError('Audio chunk format mismatch.')
            for companion in info.get('companions', []):
                name = companion['name']
                if name not in (key + '.mid', key + '.music.json'):
                    raise ValueError('Invalid audio companion path.')
                path = self.root / name
                if regular_file(path).st_size != companion['size'] or sha256_file(path, cancelled=self.cancelled) != companion['sha256']:
                    raise ValueError('Audio companion checksum mismatch.')
            return audio, info
        except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError):
            quarantine = checked_directory(self.root / 'quarantine', create=True)
            for path in (audio, meta, self.root / (key + '.mid'), self.root / (key + '.music.json')):
                if path.exists() or path.is_symlink():
                    # Move a bad link itself; never read or erase its target.
                    path.rename(quarantine / (path.name + '.' + uuid.uuid4().hex))
            # Corruption may change a file's size. Retaining it never releases
            # its storage allowance, either now or after the next restart.
            self.used = self._retained_bytes()
            return None

    def seal(self, key, source, *, provenance=None, companions=()):
        key = self._key(key)
        source = Path(source)
        from .ffmpeg import wav_info
        source_identity = file_identity(regular_file(source))
        info = wav_info(source)
        size = source_identity[2]
        prepared, seen, payload_size = [], set(), size
        for suffix in companions:
            if suffix not in ('.mid', '.music.json') or suffix in seen:
                raise ValueError('Unsupported or duplicate music companion.')
            seen.add(suffix)
            path = source.with_suffix(suffix)
            identity = file_identity(regular_file(path))
            prepared.append((path, key + suffix, identity))
            payload_size += identity[2]
        self.reserve(payload_size)
        checksum = sha256_file(source, cancelled=self.cancelled, expected_identity=source_identity)
        target = self.root / (key + '.wav')
        metadata = {'schema_version': 1, 'key': key, 'sha256': checksum, 'size': size,
                    'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames,
                    'provenance': provenance or {}, 'companions': []}
        for path, name, identity in prepared:
            metadata['companions'].append({'name': name, 'size': identity[2],
                'sha256': sha256_file(path, cancelled=self.cancelled, expected_identity=identity)})
        metadata_size = len(_json_bytes(metadata))
        if metadata_size > 65536:
            raise ValueError('Oversized chunk checkpoint.')
        # Reserve every copied companion and exactly the checkpoint bytes before
        # exposing any new payload; quarantine never releases storage allowance.
        self.reserve(payload_size + metadata_size)
        published = []
        try:
            for path, name, identity in prepared:
                check_cancelled(self.cancelled)
                publish_new(path, self.root / name, cancelled=self.cancelled, expected_identity=identity)
                published.append(self.root / name)
            check_cancelled(self.cancelled)
            publish_new(source, target, cancelled=self.cancelled, expected_identity=source_identity)
            published.append(target)
            check_cancelled(self.cancelled)
            atomic_json(self.root / (key + '.json'), metadata)
        except Exception:
            for path in published:
                path.unlink()
            # A checkpoint may exist after a durability failure. Accounting must
            # finish even if the original failure was a cancellation request.
            self.used = storage_bytes(self.budget_root)
            raise
        self.used = storage_bytes(self.budget_root)
        return target, metadata
