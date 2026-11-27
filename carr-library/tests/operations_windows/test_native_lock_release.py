"""Actual Windows native lock release races; not portable lock emulation."""
import os
from pathlib import Path
import sys
import threading

import pytest

assert sys.platform == "win32", "This native lock contract requires actual Windows."


@pytest.mark.parametrize('phase', ['construction', 'acquisition'])
def test_native_release_can_remove_the_file_during_unheld_preflight(tmp_path, monkeypatch, phase):
    """Release a real held native lock exactly before the waiter's path check."""
    from carr_library.operations import _locks
    path = tmp_path / 'contended.lock'
    holder = _locks.NativeLock(path)
    assert holder.acquire()
    waiter = _locks.NativeLock(path) if phase == 'acquisition' else None
    contender = None
    requested, released = threading.Event(), threading.Event()
    errors = []
    caller = threading.current_thread()
    actual_regular = _locks.regular
    reached = []

    def release_holder():
        try:
            if not requested.wait(10):
                raise AssertionError('The waiter did not reach its pre-acquisition boundary.')
            holder.release()
            assert not holder.held
            assert not os.path.lexists(path)
        except BaseException as exc:
            errors.append(exc)
        finally:
            released.set()

    def checked(candidate, *arguments, **keywords):
        if (Path(candidate) == path and threading.current_thread() is caller
                and not reached):
            reached.append(True)
            requested.set()
            assert released.wait(10), 'The actual native holder did not release.'
            assert not errors, errors
        return actual_regular(candidate, *arguments, **keywords)

    thread = threading.Thread(target=release_holder, name='resource-release-race')
    thread.start()
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(_locks, 'regular', checked)
            if waiter is None:
                waiter = _locks.NativeLock(path)
            assert waiter.acquire()
            assert reached == [True] and released.is_set() and not errors
            assert waiter.held
            info = path.lstat()
            held = os.fstat(waiter.lock._context.lock_file_fd)
            assert (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)
            contender = _locks.NativeLock(path)
            assert contender.acquire() is False
            assert waiter.held and not contender.held
            waiter.release()
            assert contender.acquire()
    finally:
        requested.set()
        thread.join(12)
        if contender is not None:
            contender.release()
        if waiter is not None:
            waiter.release()
        holder.release()
    assert not thread.is_alive() and not errors
