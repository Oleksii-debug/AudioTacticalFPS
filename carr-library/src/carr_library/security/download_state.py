"""Bounded resumable target bytes for the existing TUF download operation.

A checkpoint is untrusted cache data, never a package capability. The caller
refreshes TUF metadata before binding a release and TUF verifies the complete
stream before publication. No process, endpoint or installation owner lives here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat

from ._files import TrustError, atomic_write, directory, file_info, json_bytes, json_value, locked, sync_directory
from .updates import Release, UpdateCancelled, _cancel
from ..durable_state import _open_json_file

_BLOCK = 65536
_CHECKPOINT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PausedDownload:
    """Inert progress only; it cannot be supplied to an update handoff."""
    release: Release
    received: int
    state: str = 'paused'

    def __post_init__(self):
        if (type(self.release) is not Release or type(self.received) is not int
                or not 0 <= self.received <= self.release.length
                or self.state not in ('paused', 'interrupted')):
            raise TrustError('Invalid paused download progress.')


class PauseRequested(Exception):
    pass


class TransferInterrupted(TrustError):
    """The owned transport captured a valid prefix but did not finish."""


class DownloadStopped(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__('Download paused; the retained bytes are not installation authority.')


def _key(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _regular(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400):
        raise TrustError('A download fragment must be one ordinary, unlinked file.')


def _write_stream(path, *, create=False):
    """Open only this cache file; validate before reading or changing bytes."""
    if create:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                     | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_BINARY', 0)
                     | getattr(os, 'O_NOINHERIT', 0), 0o600)
    elif os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        # Read/write, share read only, OPEN_EXISTING, OPEN_REPARSE_POINT.
        handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    else:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
                     | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        _regular(os.fstat(fd))
        return os.fdopen(fd, 'r+b')
    except BaseException:
        os.close(fd)
        raise


def _read_record(path):
    before = file_info(path)
    if not 1 <= before.st_size <= 32768:
        raise TrustError('Invalid download checkpoint length.')
    with _open_json_file(path) as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened)
        if _stamp(opened) != _stamp(before):
            raise TrustError('Download checkpoint changed before reading.')
        raw = stream.read(before.st_size + 1)
        if (len(raw) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(file_info(path)) != _stamp(before)):
            raise TrustError('Download checkpoint changed while reading.')
    return json_value(raw, maximum=32768)


@contextmanager
def download_lock(cache):
    """Keep the same cache lock until a pending native owner is quiescent."""
    from ..audio.models import AudioCleanupPending
    manager = locked(cache)
    manager.__enter__()
    transferred = False
    try:
        yield
    except AudioCleanupPending as error:
        transferred = True
        error.cleanup_owner.add_quiescent_callback(lambda: manager.__exit__(None, None, None))
        raise
    finally:
        if not transferred:
            manager.__exit__(None, None, None)


class _CheckedReader:
    def __init__(self, owner):
        self.owner = owner

    def read(self, size=-1):
        self.owner.check()
        if type(size) is not int or size < 0:
            raise TrustError('The package verifier requested an unbounded read.')
        return self.owner.stream.read(min(size, _BLOCK))

    def readable(self):
        return True

    def readinto(self, buffer):
        self.owner.check()
        return self.owner.stream.readinto(memoryview(buffer)[:_BLOCK])

    def seek(self, *args):
        self.owner.check()
        return self.owner.stream.seek(*args)

    def tell(self):
        return self.owner.stream.tell()


class TargetDownload:
    """One selected target, one retained fragment and one existing cache lock."""
    def __init__(self, downloads, binding, *, cancelled, paused, resume=False, progress=None):
        if type(resume) is not bool or not callable(cancelled) or not callable(paused):
            raise TrustError('Invalid explicit download controls.')
        if progress is not None and not callable(progress):
            raise TrustError('Invalid download progress observer.')
        self.downloads, self.binding = directory(downloads), binding
        self.cancelled, self.paused, self.resume, self.progress = cancelled, paused, resume, progress
        self.release = self.root = self.stream = None
        self.received, self.digest, self.file_key, self.root_key = 0, sha256(), None, None
        self.status = None
        self._last_checkpoint = self._reported = 0

    def check(self, *, pause=True):
        _cancel(self.cancelled)
        if pause and self.paused():
            raise PauseRequested()

    def report(self, received=None):
        if self.progress is not None and self.release is not None:
            value = self.received if received is None else received
            if type(value) is int and max(self.received, self._reported) <= value <= self.release.length:
                self._reported = value
                try:
                    self.progress(value, self.release.length)
                except Exception:
                    pass  # Feedback cannot reverse downloaded bytes.

    def bind(self, release):
        if type(release) is not Release or self.release is not None:
            raise TrustError('Bind the freshly authenticated target exactly once.')
        self.release = release
        identity = sha256(json_bytes({'binding': self.binding, 'release_sha256': release.sha256})).hexdigest()
        self.root = self.downloads / ('.paused-' + identity)
        self.path, self.record = self.root / 'package.part', self.root / 'checkpoint.json'
        return self

    def _directory(self):
        root = directory(self.root)
        if self.root_key is not None and _key(root.stat()) != self.root_key:
            raise TrustError('The retained download directory changed.')
        with os.scandir(root) as entries:
            seen = set()
            for entry in entries:
                if entry.name not in ('package.part', 'checkpoint.json') or entry.name in seen:
                    raise TrustError('The retained download directory contains an unknown entry.')
                seen.add(entry.name)
                if len(seen) > 2:
                    raise TrustError('Too many retained download entries.')
        return seen

    def _record_value(self):
        self._directory()
        value = _read_record(self.record)
        if (type(value) is not dict or set(value) !=
                {'schema', 'binding', 'release', 'received', 'prefix_sha256', 'file', 'directory', 'state'}
                or value['schema'] != 'carr-paused-update/1'
                or value['binding'] != self.binding or value['release'] != asdict(self.release)
                or type(value['received']) is not int or not 0 <= value['received'] <= self.release.length
                or not isinstance(value['prefix_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', value['prefix_sha256'])
                or value['state'] not in ('paused', 'interrupted')
                or any(type(value[name]) is not list or len(value[name]) != 2
                       or any(type(item) is not int or item < 0 for item in value[name])
                       for name in ('file', 'directory'))):
            raise TrustError('The retained download does not match this source and signed release.')
        if _key(self.root.stat()) != tuple(value['directory']):
            raise TrustError('The retained download directory was replaced.')
        return value

    def _open(self, *, writable=True):
        self.check(pause=False)
        if os.path.lexists(self.root):
            if not self.resume:
                raise TrustError('A retained download exists. Choose Resume or Cancel download explicitly.')
            value = self._record_value()
            before = file_info(self.path)
            if (_key(before) != tuple(value['file'])
                    or not value['received'] <= before.st_size <= self.release.length):
                raise TrustError('The retained download file was replaced or changed length.')
            self.root_key, self.file_key = tuple(value['directory']), tuple(value['file'])
            self.stream = _write_stream(self.path) if writable else _open_json_file(self.path)
            if _stamp(os.fstat(self.stream.fileno())) != _stamp(before):
                raise TrustError('The retained download changed while opening.')
            digest, used = sha256(), 0
            while used < value['received']:
                self.check(pause=False)
                block = self.stream.read(min(_BLOCK, value['received'] - used))
                if not block:
                    raise TrustError('The retained download prefix was truncated.')
                used += len(block)
                digest.update(block)
            if (digest.hexdigest() != value['prefix_sha256']
                    or _stamp(os.fstat(self.stream.fileno())) != _stamp(before)
                    or _stamp(file_info(self.path)) != _stamp(before)):
                raise TrustError('The retained download prefix changed. Cancel it before downloading again.')
            self.received, self.digest = used, digest
            # Only uncheckpointed bytes from this exact owned inode are discarded.
            self.stream.seek(used)
            if writable and before.st_size != used:
                self.stream.truncate(used)
            self._last_checkpoint = used
        else:
            if self.resume:
                raise TrustError('No retained download exists for this exact reviewed release.')
            if shutil.disk_usage(self.downloads).free < 2 * self.release.length + 16 * 1024**2:
                raise TrustError('Not enough space for the fragment and fully verified package.')
            self.root.mkdir(mode=0o700)
            self.root_key = _key(directory(self.root).stat())
            self.stream = _write_stream(self.path, create=True)
            self.file_key = _key(os.fstat(self.stream.fileno()))
            self._checkpoint('interrupted')
        if (writable and shutil.disk_usage(self.downloads).free
                < 2 * self.release.length - self.received + 16 * 1024**2):
            raise TrustError('Not enough space to resume and fully verify the package.')
        self.report()

    def _checkpoint(self, state):
        self._directory()
        self.stream.flush()
        os.fsync(self.stream.fileno())
        opened = os.fstat(self.stream.fileno())
        _regular(opened)
        if (_key(opened) != self.file_key or opened.st_size != self.received
                or _stamp(file_info(self.path)) != _stamp(opened)):
            raise TrustError('The owned download fragment changed before checkpointing.')
        value = {'schema': 'carr-paused-update/1', 'binding': self.binding,
                 'release': asdict(self.release), 'received': self.received,
                 'prefix_sha256': self.digest.hexdigest(), 'file': list(self.file_key),
                 'directory': list(self.root_key), 'state': state}
        atomic_write(self.record, json_bytes(value))
        if _read_record(self.record) != value:
            raise TrustError('The saved download checkpoint was not confirmed.')
        self._last_checkpoint = self.received
        self.status = PausedDownload(self.release, self.received, state)

    @contextmanager
    def target_file(self, url, maximum, chunks):
        if self.release is None or maximum != self.release.length:
            raise TrustError('The TUF target differs from the reviewed download length.')
        try:
            self._open()
            self.check()
            if self.received < maximum:
                for block in chunks(url, self.received, maximum, self):
                    self.check(pause=False)
                    if not isinstance(block, bytes) or not block or len(block) > _BLOCK:
                        raise TrustError('Invalid bounded target block.')
                    if self.received + len(block) > maximum:
                        raise TrustError('The target exceeded its signed length.')
                    self.stream.write(block)
                    self.digest.update(block)
                    self.received += len(block)
                    if self.received - self._last_checkpoint >= _CHECKPOINT_BYTES:
                        self._checkpoint('interrupted')
                    self.report()
                self.check()
            if self.received != maximum:
                raise TransferInterrupted('The update target ended before its signed length.')
            self._checkpoint('interrupted')
            self.stream.seek(0)
            # Existing TUF length and every signed hash are checked on this
            # complete stream before its existing destination publication.
            yield _CheckedReader(self)
        except PauseRequested:
            if self.stream is None:
                raise TrustError('The download could not reach a durable pause boundary.') from None
            self._checkpoint('paused')
            raise DownloadStopped(self.status) from None
        except TransferInterrupted:
            if self.stream is not None:
                self._checkpoint('interrupted')
            raise DownloadStopped(self.status) from None
        finally:
            if self.stream is not None:
                self.stream.close()
                self.stream = None

    def inspect(self):
        if not os.path.lexists(self.root):
            return None
        self.resume = True
        try:
            self._open(writable=False)
            value = self._record_value()
            return PausedDownload(self.release, self.received, value['state'])
        finally:
            if self.stream is not None:
                self.stream.close()
                self.stream = None

    def discard(self):
        """Remove only the two recognized files with their retained identities."""
        if not os.path.lexists(self.root):
            return False
        value = self._record_value()
        before = file_info(self.path)
        if _key(before) != tuple(value['file']):
            raise TrustError('Refusing to remove a replaced download fragment.')
        self.root_key = tuple(value['directory'])
        self._directory()
        self.path.unlink()
        # Do not recursively remove unfamiliar files or a changed parent.
        self._directory()
        if _read_record(self.record) != value:
            raise TrustError('The download checkpoint changed during cancellation.')
        self.record.unlink()
        self._directory()
        self.root.rmdir()
        sync_directory(self.downloads)
        self.status = None
        return True
