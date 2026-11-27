"""Reuse the pinned native filelock implementation; never fall back to soft locks."""
from importlib.metadata import version
import os
from pathlib import Path
import sys
import threading
import weakref

from filelock import FileLock, Timeout, UnixFileLock, WindowsFileLock
from ..durable_state import UnlinkedRegularFile, local_path, regular
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


def _checked_lock_path(value):
    # The pinned backend unlinks a lock file on normal release. Its absence can
    # race either local_path's leaf check or our regular-file check below.
    # Validate the still-existing parent; never hide invalid types, links,
    # access failures, or removal of the resource directory itself.
    try:
        path = local_path(value)
    except (FileNotFoundError, UnlinkedRegularFile) as exc:
        candidate = Path(value).absolute()
        if isinstance(exc, UnlinkedRegularFile) and exc.path != candidate:
            raise
        parent = local_path(candidate.parent, exists=True)
        regular(parent, directory=True)
        path = parent / candidate.name
    parent = local_path(path.parent, exists=True)
    regular(parent, directory=True)
    try:
        regular(path)
    except (FileNotFoundError, UnlinkedRegularFile) as exc:
        if isinstance(exc, UnlinkedRegularFile) and exc.path != path:
            raise
        regular(parent, directory=True)
    return path


class NativeLock:
    def __init__(self, path):
        expected = WindowsFileLock if sys.platform == 'win32' else UnixFileLock
        if version('filelock') != '3.29.0' or FileLock is not expected:
            raise ResourceStateError('The reviewed native file locking backend is unavailable.')
        self.path = _checked_lock_path(path)
        self.expected, self.pid = expected, os.getpid()
        with _GUARD:
            self.lock = FileLock(self.path, mode=0o600, thread_local=False)
            _LOCKS.add(self)

    def acquire(self):
        if self.pid != os.getpid():
            raise ResourceStateError('A resource lock cannot be reused in a forked child.')
        _checked_lock_path(self.path)
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
