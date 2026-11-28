"""Owner finalizers on the desktop's existing single worker.

Direct metadata/editor submissions use the same executor as run_job. Track
their Futures so shutdown can cancel ordinary queued work while retaining only
explicit, idempotent resource finalizers. This adds no second task queue.
"""
from concurrent.futures import CancelledError, ThreadPoolExecutor
import threading
import warnings


# Like audio.cleanup's retained owners, this survives a destroyed native frame
# and a collected executor. It never starts a worker or silently retries.
_FAILED_OWNER_GUARD = threading.RLock()
_FAILED_OWNERS = {}


def retained_cleanup_failures():
    """Return opaque retry identities and errors; ownership stays process-local."""
    with _FAILED_OWNER_GUARD:
        return tuple((token, record['error']) for token, record in _FAILED_OWNERS.items())


def retry_failed_cleanup(token):
    """Retry on an existing caller-owned worker, never on a native UI callback."""
    with _FAILED_OWNER_GUARD:
        record = _FAILED_OWNERS.get(token)
    if record is None:
        return False
    with record['retry_guard']:
        with _FAILED_OWNER_GUARD:
            if record['released'] or _FAILED_OWNERS.get(token) is not record:
                return False
        try:
            record['callback']()
        except BaseException as error:
            with _FAILED_OWNER_GUARD:
                record['error'] = error
            raise
        else:
            record['release'](token)
            with _FAILED_OWNER_GUARD:
                if _FAILED_OWNERS.get(token) is record:
                    del _FAILED_OWNERS[token]
            return True


class DesktopExecutor(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=1, thread_name_prefix='library')
        self._lifecycle_guard = threading.RLock()
        self._close_guard = threading.Lock()
        self._accepting_work = True
        self._cancel_queued_work = False
        self._submitted_futures = set()
        self._owner_finalizers = {}
        self._owner_records = {}
        self._finalizers_queued = False
        self._cleanup_futures = []
        self._cleanup_failures = {}

    def submit(self, fn, /, *args, **kwargs):
        with self._lifecycle_guard:
            if not self._accepting_work:
                raise RuntimeError('The desktop worker is closing.')
            # CPython may enqueue before Thread.start fails. A queued wrapper
            # is not authorized user work until submit actually succeeds.
            admission = [False]
            future = super().submit(self._run_if_admitted, admission, fn, args, kwargs)
            self._submitted_futures.add(future)
            future.add_done_callback(self._forget)
            admission[0] = True
            return future

    def _run_if_admitted(self, admission, fn, args, kwargs):
        # Future.cancel() invokes callbacks synchronously. A callback may block
        # shutdown while this worker reaches another not-yet-cancelled Future.
        # Admit execution under the same guard as shutdown, before user code.
        # Work admitted before close keeps its existing cooperative lifetime.
        with self._lifecycle_guard:
            if not admission[0] or self._cancel_queued_work:
                raise CancelledError('This desktop work was not admitted before execution.')
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
            record = {'callback': callback, 'error': None, 'released': False,
                      'release': self._release_finalizer, 'retry_guard': threading.RLock()}
            self._owner_records[token] = record

        def unregister():
            # Contract: the owner invokes this only after its own successful
            # idempotent release. A queued finalizer then becomes a no-op.
            self._release_finalizer(token)
            with _FAILED_OWNER_GUARD:
                _FAILED_OWNERS.pop(token, None)
        return unregister

    @property
    def cleanup_failures(self):
        """Failed shutdown releases remain observable with their retained owner."""
        with self._lifecycle_guard:
            return tuple((self._owner_finalizers[token], error)
                         for token, error in self._cleanup_failures.items()
                         if token in self._owner_finalizers)

    def _retain_failure(self, token, record, error):
        with record['retry_guard']:
            if record['released']:
                return
            record['error'] = error
            with self._lifecycle_guard:
                self._cleanup_failures[token] = error
            with _FAILED_OWNER_GUARD:
                _FAILED_OWNERS[token] = record
            # The desktop can already be gone. Keep a native-console/log
            # diagnostic without exposing source text, paths or credentials.
            try:
                warnings.warn('An owned desktop resource did not close; its owner is retained for review.',
                              RuntimeWarning, stacklevel=1)
            except BaseException:
                pass

    def _release_finalizer(self, token):
        with self._lifecycle_guard:
            record = self._owner_records.pop(token, None)
            if record is not None:
                record['released'] = True
            self._owner_finalizers.pop(token, None)
            self._cleanup_failures.pop(token, None)

    def _finalize(self, token, record):
        # CPython queues its WorkItem before starting the worker. The same
        # owner can therefore be retained after start failure yet still run
        # later from that queue. Use this stable lock for both paths.
        with record['retry_guard']:
            if record['released']:
                return
            try:
                record['callback']()
            except BaseException as error:
                self._retain_failure(token, record, error)
                raise
            else:
                self._release_finalizer(token)
                with _FAILED_OWNER_GUARD:
                    _FAILED_OWNERS.pop(token, None)

    def shutdown(self, wait=True, *, cancel_futures=False):
        # Serialize shutdown calls through finalizer admission and the standard
        # executor sentinel. The owner never waits on this lock from its work.
        with self._close_guard:
            with self._lifecycle_guard:
                pending = tuple(self._submitted_futures)
                self._accepting_work = False
                self._cancel_queued_work = self._cancel_queued_work or cancel_futures
                finalizers = (() if self._finalizers_queued else tuple(self._owner_records.items()))
                self._finalizers_queued = True
            if cancel_futures:
                for future in pending:
                    future.cancel()
            # Standard shutdown(cancel_futures=True) would also drain cleanup.
            # Ordinary work is either already admitted, canceled, or refused
            # by the entry guard; only these owner releases bypass that guard.
            for token, record in finalizers:
                try:
                    future = super().submit(self._finalize, token, record)
                except BaseException as error:
                    # No callback ran: retain this exact owner too, including
                    # failure to start an idle native worker thread.
                    self._retain_failure(token, record, error)
                else:
                    self._cleanup_futures.append(future)
            super().shutdown(wait=wait, cancel_futures=False)
