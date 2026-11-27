"""Owner finalizers on the desktop's existing single worker.

Direct metadata/editor submissions use the same executor as run_job. Track
their Futures so shutdown can cancel ordinary queued work while retaining only
explicit, idempotent resource finalizers. This adds no second task queue.
"""
from concurrent.futures import CancelledError, ThreadPoolExecutor
import threading


class DesktopExecutor(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=1, thread_name_prefix='library')
        self._lifecycle_guard = threading.RLock()
        self._close_guard = threading.Lock()
        self._accepting_work = True
        self._cancel_queued_work = False
        self._submitted_futures = set()
        self._owner_finalizers = {}

    def submit(self, fn, /, *args, **kwargs):
        with self._lifecycle_guard:
            if not self._accepting_work:
                raise RuntimeError('The desktop worker is closing.')
            future = super().submit(self._run_if_admitted, fn, args, kwargs)
            self._submitted_futures.add(future)
            future.add_done_callback(self._forget)
            return future

    def _run_if_admitted(self, fn, args, kwargs):
        # Future.cancel() invokes callbacks synchronously. A callback may block
        # shutdown while this worker reaches another not-yet-cancelled Future.
        # Admit execution under the same guard as shutdown, before user code.
        # Work admitted before close keeps its existing cooperative lifetime.
        with self._lifecycle_guard:
            if self._cancel_queued_work:
                raise CancelledError('The desktop closed before this work began.')
        return fn(*args, **kwargs)

    def _forget(self, future):
        with self._lifecycle_guard:
            self._submitted_futures.discard(future)

    def register_cleanup(self, callback):
        """Return an unregister function for an idempotent, worker-safe close.

        The callback must release only its owner's existing resources; it must
        not perform user work or access native widgets. Normal close may call
        it first, then unregister. A concurrent shutdown may already have
        captured it, so the owner's close must tolerate repeated invocation.
        """
        if not callable(callback):
            raise TypeError('A resource cleanup callback is required.')
        token = object()
        with self._lifecycle_guard:
            if not self._accepting_work:
                raise RuntimeError('The desktop worker is closing.')
            self._owner_finalizers[token] = callback

        def unregister():
            with self._lifecycle_guard:
                self._owner_finalizers.pop(token, None)
        return unregister

    def shutdown(self, wait=True, *, cancel_futures=False):
        # Serialize shutdown calls through finalizer admission and the standard
        # executor sentinel. The owner never waits on this lock from its work.
        with self._close_guard:
            with self._lifecycle_guard:
                pending = tuple(self._submitted_futures)
                self._accepting_work = False
                self._cancel_queued_work = self._cancel_queued_work or cancel_futures
                finalizers = tuple(self._owner_finalizers.values())
                self._owner_finalizers.clear()
            if cancel_futures:
                for future in pending:
                    future.cancel()
            # Standard shutdown(cancel_futures=True) would also drain cleanup.
            # Ordinary work is either already admitted, canceled, or refused
            # by the entry guard; only these owner releases bypass that guard.
            for callback in finalizers:
                super().submit(callback)
            super().shutdown(wait=wait, cancel_futures=False)
