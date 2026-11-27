"""One explicit native action, queued from a worker with bounded stale ownership."""
from threading import Event, Lock
from time import monotonic
import math


def call_on_ui(post, action, *, cancelled=lambda: False, timeout=30.0):
    """Run action once on the event thread; retain caller resources until return.

    This helper owns no subprocess and does not create a new executor. A queued
    action expires if cancellation/timeout wins before it begins. An action that
    has already entered the native opener must settle before its worker releases
    verified readers; it cannot be replayed by a late second callback.
    """
    if (not callable(post) or not callable(action) or not callable(cancelled)
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 60):
        raise ValueError('Choose a bounded native launch handoff.')
    if cancelled():
        raise InterruptedError('Opening the original was cancelled.')
    lock, finished = Lock(), Event()
    state = dict(phase='queued', value=None, error=None)

    def invoke():
        with lock:
            if state['phase'] != 'queued':
                return
            try:
                stopping = cancelled()
                expired = monotonic() >= deadline
            except BaseException as error:
                state.update(phase='finished', error=error)
                finished.set()
                return
            if stopping or expired:
                error = (InterruptedError('Opening the original was cancelled.') if stopping else
                         TimeoutError('The native source action was not accepted before its handoff deadline.'))
                state.update(phase='finished', error=error)
                finished.set()
                return
            state['phase'] = 'running'
        try:
            value = action()
        except BaseException as error:
            with lock:
                state.update(phase='finished', error=error)
        else:
            with lock:
                state.update(phase='finished', value=value)
        finally:
            finished.set()

    deadline = monotonic() + timeout
    deferred = None
    try:
        post(invoke)
    except BaseException as error:
        with lock:
            if state['phase'] == 'queued':
                state['phase'] = 'abandoned'
                raise
            # A dispatcher that already ran the native action cannot revoke
            # its acknowledgement. Process interrupts still propagate after
            # an already-started action has released its borrowed readers.
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                deferred = error
    while True:
        try:
            settled = finished.wait(.025)
            if settled:
                with lock:
                    error, value = state['error'], state['value']
                break
            stopping = cancelled()
            expired = monotonic() >= deadline
            with lock:
                if state['phase'] == 'queued' and (stopping or expired):
                    state['phase'] = 'abandoned'
                    if stopping:
                        raise InterruptedError('Opening the original was cancelled before native launch.')
                    raise TimeoutError('The native source action was not accepted before its handoff deadline.')
                # A started native opener is synchronous on the event thread.
                # Keep all payload readers until it actually settles.
        except BaseException as interrupted:
            with lock:
                phase = state['phase']
                if phase == 'queued':
                    state['phase'] = 'abandoned'
                error, value = state['error'], state['value']
            if phase in ('queued', 'abandoned'):
                raise
            if deferred is None:
                deferred = interrupted
            if phase == 'finished':
                break
            # Even an interrupt in Event.wait/lock observation must not unwind
            # the caller's held readers while the native action still runs.
    if deferred is not None:
        raise deferred
    if error is not None:
        raise error
    return value
