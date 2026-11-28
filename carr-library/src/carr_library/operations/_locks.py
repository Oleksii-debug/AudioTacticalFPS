"""Reuse the pinned native filelock implementation; never fall back to soft locks."""
from importlib.metadata import version
import os
from pathlib import Path
import sys
import threading
import weakref

from filelock import FileLock, Timeout, UnixFileLock, WindowsFileLock
from ..audio.jobs import checked_directory
from ..audio.paths import filename_component
from ._state import local_path, regular
from .models import ResourceStateError


_GUARD = threading.RLock()
_LOCKS = weakref.WeakSet()


def _after_fork():
    # flock belongs to an open file description. Unlocking an inherited lock
    # would unlock the parent's reservation too. Close only the child's copies.
    # This accesses the reviewed, exactly pinned filelock 3.29 context, not a
    # PID-reaping heuristic or a replacement locking implementation.
    for item in tuple(_LOCKS):
        context = item.lock._context
        if context.lock_file_fd is not None:
            os.close(context.lock_file_fd)
            context.lock_file_fd = None
        context.lock_counter = 0
    _LOCKS.clear()
    _GUARD.release()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=_GUARD.acquire, after_in_parent=_GUARD.release, after_in_child=_after_fork)


def _leaf_present(path):
    """Only an absent leaf is benign; denied metadata must stop admission."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _unheld_path(value):
    """A released Windows lock may disappear before our native acquisition.

    Absence of the unowned leaf is benign; a recreated leaf is revalidated.
    Its existing parent and every present replacement still pass the normal
    local-file checks. Repeated changes fail after bounded observations; the
    acquired descriptor is compared with the path again before admission.
    """
    path = Path(value).expanduser().absolute()
    filename_component(path.name)
    for _ in range(3):
        try:
            # Windows realpath validates a stripped extended prefix with a
            # second native lookup. A disappearing unowned leaf can keep that
            # prefix, so resolve only its existing, fully checked parent.
            parent = checked_directory(local_path(path.parent, exists=True))
            checked = parent / path.name
            if _leaf_present(checked):
                regular(checked)
            return checked
        except FileNotFoundError:
            parent = checked_directory(local_path(path.parent, exists=True))
            checked = parent / path.name
            if not _leaf_present(checked):
                return checked
            # Another contender recreated the unowned leaf. Revalidate its
            # current type, aliases and parent instead of rethrowing an old
            # missing-file observation or accepting an unchecked replacement.
    raise ResourceStateError('A resource lock path kept changing before native acquisition.')


class NativeLock:
    def __init__(self, path):
        expected = WindowsFileLock if sys.platform == 'win32' else UnixFileLock
        if version('filelock') != '3.29.0' or FileLock is not expected:
            raise ResourceStateError('The reviewed native file locking backend is unavailable.')
        self.path = _unheld_path(path)
        self.expected, self.pid = expected, os.getpid()
        with _GUARD:
            self.lock = FileLock(self.path, mode=0o600, thread_local=False)
            _LOCKS.add(self)

    def acquire(self):
        if self.pid != os.getpid():
            raise ResourceStateError('A resource lock cannot be reused in a forked child.')
        _unheld_path(self.path)
        with _GUARD:
            try:
                self.lock.acquire(timeout=0)
            except Timeout:
                return False
            if type(self.lock) is not self.expected:
                self.lock.release(force=True)
                raise ResourceStateError('The selected filesystem has no supported native file locks.')
            try:
                info, held = regular(self.path), os.fstat(self.lock._context.lock_file_fd)
            except (OSError, ValueError) as exc:
                self._close_only()
                raise ResourceStateError('A native resource lock could not be validated.') from exc
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                self._close_only()
                raise ResourceStateError('A native resource lock was replaced during admission.')
            return True

    def _close_only(self):
        context = self.lock._context
        if context.lock_file_fd is not None:
            os.close(context.lock_file_fd)
            context.lock_file_fd = None
        context.lock_counter = 0

    def release(self):
        if self.pid != os.getpid():
            raise ResourceStateError('A child process cannot release its parent reservation.')
        with _GUARD:
            if not self.lock.is_locked:
                return
            try:
                info, held = regular(self.path), os.fstat(self.lock._context.lock_file_fd)
                unchanged = (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                self._close_only()
                raise ResourceStateError('A resource lock changed; its old descriptor was closed safely.')
            self.lock.release(force=True)

    @property
    def held(self):
        return self.pid == os.getpid() and self.lock.is_locked
