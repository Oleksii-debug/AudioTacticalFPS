"""Retained ownership when a native audio process cannot yet confirm shutdown.

Only the owner retries native cleanup. Callers may retain their admission and
staging leases until its plain completion event or callback confirms quiescence.
Retries use a daemon, so an uncertain child never blocks interpreter shutdown.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import tempfile
import threading

from .models import AudioCleanupPending

_RETAINED = set()
_RETAINED_LOCK = threading.Lock()


class CleanupOwner:
    """A serialized, retryable cleanup attempt with a side-effect-free status."""

    def __init__(self, attempt):
        if not callable(attempt):
            raise TypeError('Cleanup requires a callable owned-resource attempt.')
        self._attempt = attempt
        self._attempt_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._callbacks = []
        self.quiescent_event = threading.Event()
        self.last_error = None

    @property
    def quiescent(self):
        return self.quiescent_event.is_set()

    @staticmethod
    def _notify(callback):
        try:
            callback()
        except Exception:
            # A consumer's failed notification cannot undo native shutdown.
            pass

    def add_quiescent_callback(self, callback):
        if not callable(callback):
            raise TypeError('A quiescence notification must be callable.')
        with self._callback_lock:
            ready = self.quiescent
            if not ready:
                self._callbacks.append(callback)
        if ready:
            self._notify(callback)

    def close(self):
        """Attempt once, without overlapping native waits; return confirmed status."""
        if self.quiescent:
            return True
        if not self._attempt_lock.acquire(blocking=False):
            return self.quiescent
        callbacks = ()
        try:
            if self.quiescent:
                return True
            try:
                if self._attempt() is False:
                    return False
            except Exception as exc:
                self.last_error = exc
                return False
            with self._callback_lock:
                self.quiescent_event.set()
                callbacks, self._callbacks = tuple(self._callbacks), []
            with _RETAINED_LOCK:
                _RETAINED.discard(self)
        finally:
            self._attempt_lock.release()
        for callback in callbacks:
            self._notify(callback)
        return True

    def _retry(self):
        while not self.close():
            self.quiescent_event.wait(.25)


def pending_cleanup(attempt, *, message, callbacks=()):
    """Keep a failed native owner strongly reachable and return its public error."""
    owner = CleanupOwner(attempt)
    for callback in callbacks:
        owner.add_quiescent_callback(callback)
    with _RETAINED_LOCK:
        _RETAINED.add(owner)
    try:
        worker = threading.Thread(target=owner._retry, name='audio-cleanup', daemon=True)
        worker.start()
    except Exception as exc:
        # Retain the owner even if thread admission fails. Its public close()
        # remains available for a bounded explicit retry.
        owner.last_error = exc
    return AudioCleanupPending(message, cleanup_owner=owner)


@contextmanager
def retained_state_lock(root, name='state'):
    """Transfer this exact lock to a pending owner until native cleanup finishes."""
    from filelock import FileLock, Timeout
    from ..durable_state import local_path, regular
    root = local_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ('.' + name + '.lock')
    if os.path.lexists(path):
        regular(path)
    lock = FileLock(path, timeout=0, mode=0o600, thread_local=False)
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeError('This project is already being changed by another operation.') from None
    transferred = False
    try:
        yield
    except AudioCleanupPending as exc:
        transferred = True
        exc.cleanup_owner.add_quiescent_callback(lock.release)
        raise
    finally:
        if not transferred:
            lock.release()


@contextmanager
def retained_temporary_directory(*, prefix, dir):
    """Keep existing worker staging alive until its native owner confirms exit."""
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=dir)
    transferred = False
    try:
        yield temporary.name
    except AudioCleanupPending as exc:
        # The bound callback also owns TemporaryDirectory, preventing its
        # finalizer from removing files while the native worker may use them.
        transferred = True
        exc.cleanup_owner.add_quiescent_callback(temporary.cleanup)
        raise
    finally:
        if not transferred:
            temporary.cleanup()
