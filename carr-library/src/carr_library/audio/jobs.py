"""Crash-safe local checkpoints and content-addressed audio cache."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
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


def sha256_file(path, *, cancelled=lambda: False):
    regular_file(path)
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        while block := stream.read(1024 * 1024):
            check_cancelled(cancelled)
            result.update(block)
    return result.hexdigest()


def _sync_directory(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def sync_file(path):
    """Flush an existing regular file without truncating or following a link.

    Windows' file commit requires a writable descriptor, even when the file was
    already written and closed. Compare the opened file with the checked path
    before flushing, and close the handle on validation and I/O failures alike.
    Cache files can have multiple hard links to completed render outputs.
    """
    path = Path(path)
    expected = regular_file(path)
    flags = (os.O_RDWR | getattr(os, 'O_BINARY', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or not os.path.samestat(expected, opened)):
            raise ValueError('The audio file changed before it could be flushed.')
        os.fsync(descriptor)
        if not os.path.samestat(opened, regular_file(path)):
            raise ValueError('The audio file changed while it was being flushed.')
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


def publish_new(source, destination):
    """Atomically publish without overwriting an existing path, including races."""
    source, destination = Path(source), Path(destination)
    checked_directory(destination.parent)
    sync_file(source)
    os.link(source, destination)
    _sync_directory(destination.parent)


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
    def __init__(self, root, *, cancelled=lambda: False, disk_budget_bytes=32 * 1024 ** 3):
        if type(disk_budget_bytes) is not int or disk_budget_bytes < 0:
            raise ValueError('Invalid audio disk budget.')
        self.root = checked_directory(root, create=True)
        self.cancelled = cancelled
        self.disk_budget_bytes = disk_budget_bytes
        self.used = self._retained_bytes()

    def _retained_bytes(self):
        """Count every retained payload, including forensic quarantine.

        A corrupt link may have been quarantined deliberately. Count the link
        entry itself without following it or traversing a reparse directory.
        Like the existing budget, this counts file sizes, not filesystem block
        allocation; accepted chunks are maintained under the project lock.
        """
        total, pending = 0, [self.root]
        while pending:
            directory = checked_directory(pending.pop())
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancelled(self.cancelled)
                    info = entry.stat(follow_symlinks=False)
                    if (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                            and not getattr(info, 'st_file_attributes', 0) & 0x400):
                        pending.append(Path(entry.path))
                    else:
                        total += info.st_size
        return total

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid chunk identity.')
        return key

    def reserve(self, expected_bytes):
        check_cancelled(self.cancelled)
        if type(expected_bytes) is not int or expected_bytes < 0:
            raise ValueError('Invalid audio disk estimate.')
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
            if info.get('schema_version') != 1 or info.get('key') != key:
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
        info = wav_info(source)
        size = regular_file(source).st_size
        prepared, seen, payload_size = [], set(), size
        for suffix in companions:
            if suffix not in ('.mid', '.music.json') or suffix in seen:
                raise ValueError('Unsupported or duplicate music companion.')
            seen.add(suffix)
            path = source.with_suffix(suffix)
            companion_size = regular_file(path).st_size
            prepared.append((path, key + suffix, companion_size))
            payload_size += companion_size
        self.reserve(payload_size)
        checksum = sha256_file(source, cancelled=self.cancelled)
        target = self.root / (key + '.wav')
        metadata = {'schema_version': 1, 'key': key, 'sha256': checksum, 'size': size,
                    'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames,
                    'provenance': provenance or {}, 'companions': []}
        for path, name, companion_size in prepared:
            metadata['companions'].append({'name': name, 'size': companion_size,
                'sha256': sha256_file(path, cancelled=self.cancelled)})
        metadata_size = len(_json_bytes(metadata))
        if metadata_size > 65536:
            raise ValueError('Oversized chunk checkpoint.')
        # Check the complete committed payload before publishing even the first
        # companion. The checkpoint uses exactly this JSON serialization.
        reserved = payload_size + metadata_size
        self.reserve(reserved)
        published = []
        try:
            for path, name, _size in prepared:
                check_cancelled(self.cancelled)
                publish_new(path, self.root / name)
                published.append(self.root / name)
            check_cancelled(self.cancelled)
            publish_new(source, target)
            published.append(target)
            check_cancelled(self.cancelled)
            atomic_json(self.root / (key + '.json'), metadata)
        except Exception:
            for path in published:
                path.unlink()
            # A checkpoint may have been replaced before a durability error.
            # Its retained bytes still count even though this seal failed.
            self.used = self._retained_bytes()
            raise
        self.used += reserved
        return target, metadata
