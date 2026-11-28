"""Retained ownership when a native audio process cannot yet confirm shutdown.

Only the owner retries native cleanup. Callers may retain their admission and
staging leases until its plain completion event or callback confirms quiescence.
Retries use a daemon, so an uncertain child never blocks interpreter shutdown.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import re
import tempfile
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
        self._notifications_in_flight = 0
        self._retained_notifications = False
        self.quiescent_event = threading.Event()
        # Native quiescence is unchanged. This separate fence also waits for
        # every already-registered retained-context notification to finish.
        self.notifications_complete_event = threading.Event()
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
            else:
                self._notifications_in_flight += 1
                self.notifications_complete_event.clear()
                if self._retained_notifications:
                    with _RETAINED_LOCK:
                        _RETAINED.add(self)
        if ready:
            try:
                self._notify(callback)
            finally:
                self._notification_finished()

    def _notification_finished(self, count=1):
        with self._callback_lock:
            self._notifications_in_flight -= count
            if self.quiescent and self._notifications_in_flight == 0:
                self.notifications_complete_event.set()
                if self._retained_notifications:
                    with _RETAINED_LOCK:
                        _RETAINED.discard(self)

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
                self._notifications_in_flight += len(callbacks)
        finally:
            self._attempt_lock.release()
        interrupted = None
        if not callbacks:
            self._notification_finished(0)
        for callback in callbacks:
            try:
                self._notify(callback)
            except BaseException as signal:
                # One interrupted consumer cannot strand another retained
                # stage. Deliver the same signal after all queued callbacks.
                if interrupted is None:
                    interrupted = signal
            finally:
                self._notification_finished()
        if interrupted is not None:
            raise interrupted
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
        owner._retained_notifications = True
        _RETAINED.add(owner)
    try:
        worker = threading.Thread(target=owner._retry, name='audio-cleanup', daemon=True)
        worker.start()
    except Exception as exc:
        # Retain the owner even if thread admission fails. Its public close()
        # remains available for a bounded explicit retry.
        owner.last_error = exc
    return AudioCleanupPending(message, cleanup_owner=owner)



def _pending_owners(error):
    """Find every real native owner, including a failed journal's context."""
    pending, seen, owners = [error], set(), {}
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, AudioCleanupPending):
            owners[id(current.cleanup_owner)] = current.cleanup_owner
        pending.extend((current.__cause__, current.__context__))
    return tuple(owners.values())


def drain_pending_cleanup(error=None):
    """Drain the same native owners and their notifications without new threads.

    Return the first deferred KeyboardInterrupt/SystemExit only after both
    proofs. An entrypoint can then close its native runtime handle, drain any
    close-origin owners, and deliver its original outcome or deferred signal.
    """

    owners, interrupted = {}, None
    while True:
        try:
            owners.update((id(owner), owner) for owner in _pending_owners(error))
            with _RETAINED_LOCK:
                owners.update((id(owner), owner) for owner in _RETAINED)
            waiting = tuple(owner for owner in owners.values()
                            if not owner.quiescent_event.is_set()
                            or not owner.notifications_complete_event.is_set())
            if not waiting:
                return interrupted
            for owner in waiting:
                owner.close()
            waiting[0].notifications_complete_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as signal:
            if interrupted is None:
                interrupted = signal


@contextmanager
def retained_context(context):
    """Borrow an existing context until all pending native owners are quiet.

    This does not create a worker or another admission system. It retains the
    entered context and uses only each existing CleanupOwner's completion proof.
    Callers must choose contexts whose release supports the callback thread.
    """
    with ExitStack() as ownership:
        value = ownership.enter_context(context)
        try:
            yield value
        except BaseException as error:
            owners = _pending_owners(error)
            if owners:
                retained = ownership.pop_all()
                remaining, guard = {id(owner) for owner in owners}, threading.Lock()

                def completed(identity):
                    with guard:
                        remaining.discard(identity)
                        ready = not remaining
                    if ready:
                        retained.close()

                for owner in owners:
                    owner.add_quiescent_callback(
                        lambda identity=id(owner): completed(identity))
            raise


@contextmanager
def retained_temporary_directory(*, prefix, dir):
    """Keep this exact temporary directory alive through uncertain native exit."""
    with retained_context(tempfile.TemporaryDirectory(prefix=prefix, dir=dir)) as temporary:
        yield temporary


@contextmanager
def retained_state_lock(root, name='state'):
    """Use the existing native lock path, retaining it through pending cleanup."""
    from ..durable_state import local_path, regular
    from ..operations._locks import NativeLock

    if type(name) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) is None:
        raise ValueError('Use a bounded local state-lock name.')
    root = local_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    regular(root, directory=True)

    @contextmanager
    def own_lock():
        lock = NativeLock(root / ('.' + name + '.lock'))
        if not lock.acquire():
            raise RuntimeError('This project is already being changed by another operation.')
        try:
            yield
        finally:
            lock.release()

    with retained_context(own_lock()):
        yield
