"""Thin worker/adoption adapter for the existing reader's playback owner.

No player, executor or timer is created here. Prepared PCM stays owned across
late GUI completion, direct window destruction and native cleanup uncertainty.
"""
from pathlib import Path
import tempfile
import threading


class ReaderPlaybackSession:
    def __init__(self, player, *, submit, guard, capture, verify, alive, busy,
                 cancelled, clear_cancel, start, stop, pause, report, refresh,
                 state_root=None, broker=None):
        self.player, self.submit = player, submit
        self.guard, self.capture, self.verify = guard, capture, verify
        self.alive, self.busy, self.cancelled = alive, busy, cancelled
        self.clear_cancel, self.start = clear_cancel, start
        self._stop, self._pause, self.report, self.refresh = stop, pause, report, refresh
        self.state_root, self.broker = state_root, broker
        self.closed, self.generation = False, 0
        self.operation = None
        self._operation_request = self._finished_operation = None
        self._values, self._lock = set(), threading.Lock()

    def check(self, *, idle=True):
        if self.closed or not self.alive():
            raise ValueError('The reading playback owner is closed.')
        self.guard()
        if idle and (self.busy() or self.operation is not None):
            raise ValueError('Wait for or cancel the current reader operation.')

    def execute(self, action, *, audible=False):
        self.check()
        snapshot = self.capture()
        self.generation += 1
        generation, session = self.generation, self.player.session_generation
        # This is an explicit, idle user command. No automatic callback clears
        # cancellation or changes which source the captured command belongs to.
        self.clear_cancel()
        def apply():
            if (self.closed or not self.alive() or self.cancelled()
                    or generation != self.generation
                    or session != self.player.session_generation):
                return
            self.check()
            self.verify(snapshot)
            action()
            self.refresh()
        if audible:
            self.start(apply, on_error=lambda error: self.report(str(error), focus=True))
        else:
            apply()

    def stop(self):
        self.generation += 1
        if self.operation is not None:
            self.operation.set()
        self._stop()

    def pause(self):
        self.generation += 1
        self._pause()

    def _release(self, value):
        # A successfully adopted value belongs to WavePlayer until native Close.
        # Serialize the worker's stale-result release with a simultaneous Close.
        with self._lock:
            if value not in self._values:
                return
            if getattr(value, '_owner', None) is None:
                value.close()
            self._values.discard(value)

    def _acknowledge(self, request=None):
        """Settle only an exact operation whose worker has actually returned."""
        with self._lock:
            if (self.operation is not None and self._finished_operation is self.operation
                    and (request is None or self._operation_request is request)):
                self.operation = None
                self._operation_request = self._finished_operation = None

    def discard(self, value):
        try:
            self._release(value)
        finally:
            self._acknowledge(value.request)

    def prepare_listening(self, request, completed, failed, *, active):
        self.check()
        if self.state_root is None and self.broker is None:
            raise ValueError('Listening preparation needs the current library resource owner.')
        snapshot = self.capture()
        generation, session = self.generation, self.player.session_generation
        cancel = threading.Event()
        self.operation = cancel
        self._operation_request, self._finished_operation = request, None

        def stopped():
            return (cancel.is_set() or self.closed or not self.alive() or not active()
                    or self.cancelled() or generation != self.generation
                    or session != self.player.session_generation)

        def work():
            try:
                if stopped():
                    raise InterruptedError('Listening preparation was cancelled before starting.')
                value = self.player.prepare_listening(request, Path(tempfile.gettempdir()),
                    state_root=self.state_root, broker=self.broker, cancelled=stopped)
                # Register before the final active check. A closing owner either
                # drains this exact result or makes this worker dispose it itself.
                with self._lock:
                    self._values.add(value)
                if stopped():
                    self._release(value)
                    raise InterruptedError('Listening preparation was cancelled before adoption.')
                return value

            finally:
                with self._lock:
                    if self.operation is cancel:
                        self._finished_operation = cancel

        def finish(value):
            self._acknowledge(request)
            try:
                if stopped():
                    raise InterruptedError('Listening preparation was cancelled before adoption.')
                self.check()
                self.verify(snapshot)
                self.player.apply_listening(value)
                with self._lock:
                    self._values.discard(value)
                self.refresh()
            except Exception as exc:
                self._release(value)
                failure(exc)
                return
            if not self.closed and self.alive() and active():
                completed()

        def failure(exc):
            self._acknowledge(request)
            if not self.closed and self.alive() and active():
                failed(exc)

        try:
            started = self.submit(work, finish, on_error=failure)
            if started is not True:
                raise RuntimeError('The existing reader worker did not accept listening preparation.')
        except BaseException:
            if self.operation is cancel:
                self.operation = None
                self._operation_request = self._finished_operation = None
            cancel.set()
            raise
        return cancel.set

    def close(self, *, worker_drained=False):
        # Only the host's serial-executor finalizer or successful wait=True join
        # may supply this proof. It also settles a Future cancelled before entry.
        self.closed = True
        self.generation += 1
        if self.operation is not None:
            self.operation.set()
        with self._lock:
            if worker_drained and self.operation is not None:
                self._finished_operation = self.operation
            values = tuple(self._values)
        errors = []
        for value in values:
            try:
                self._release(value)
            except Exception as exc:
                errors.append(exc)
        self._acknowledge()
        if errors:
            # Keep failed values in _values; explicit owner cleanup can retry.
            raise RuntimeError('Listening-copy cleanup is not confirmed: ' + str(errors[0]))
