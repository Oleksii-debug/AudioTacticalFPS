"""Retained ownership when a native audio process cannot yet confirm shutdown.

Only the owner retries native cleanup. Callers may retain their admission and
staging leases until its plain completion event or callback confirms quiescence.
Retries use a daemon, so an uncertain child never blocks interpreter shutdown.
"""
from __future__ import annotations

import threading

class AudioCleanupPending(RuntimeError):
    """An owned audio child has not yet confirmed shutdown."""

    def __init__(self, message, *, cleanup_owner):
        super().__init__(message)
        self.cleanup_owner = cleanup_owner

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
    worker = threading.Thread(target=owner._retry, name='audio-cleanup', daemon=True)
    try:
        worker.start()
    except Exception as exc:
        # Retain the owner even if thread admission fails. Its public close()
        # remains available for a bounded explicit retry.
        owner.last_error = exc
    return AudioCleanupPending(message, cleanup_owner=owner)


def wait_for_cleanup(error):
    """Settle a retained owner before a synchronous caller unwinds its scopes.

    Call this on an operation worker, never the GUI thread. Files, explicit
    streams and admission leases stay owned until the whole native family is
    quiescent. Interrupts are delivered after that boundary, not discarded.
    """
    if not isinstance(error, AudioCleanupPending):
        raise TypeError('An owned pending-cleanup error is required.')
    owner = error.cleanup_owner
    interrupted = None
    while True:
        try:
            if owner.quiescent_event.is_set():
                break
            owner.close()
            if not owner.quiescent_event.is_set():
                owner.quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as exc:
            if interrupted is None:
                interrupted = exc
    if interrupted is not None:
        raise interrupted
