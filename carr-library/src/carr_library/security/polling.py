"""Opt-in, metadata-only checks through the existing trusted update client.

One owner-lifetime thread checks only while idle. A signed release is display
information, never a VerifiedPackage or permission to install. The existing TUF
client retains responsibility for signatures, rollback and its metadata cache.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import threading
import time

from ._files import TrustError
from .updates import Release


@dataclass(frozen=True)
class UpdatePollStatus:
    phase: str
    message: str
    release: Release | None = None
    failures: int = 0


class IdleUpdatePollHost:
    """Default-off owner configuration; no library, render or save locks."""

    def __init__(self, controller, repository, *, enabled=False, interval=3600.0,
                 timeout=20.0, clock=time.monotonic):
        from .controller import TrustController
        if (not isinstance(controller, TrustController) or controller.updates is None
                or controller.target is None or type(enabled) is not bool):
            raise TrustError('An explicit trusted update client and target are required.')
        for value, minimum, maximum in ((interval, 1.0, 86400.0), (timeout, .01, 60.0)):
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise TrustError('Invalid automatic update-check timing.')
        raw = os.fspath(repository)
        if not isinstance(raw, str) or not raw or '\x00' in raw or raw.startswith(('//', '\\\\')):
            raise TrustError('Choose an explicit local signed repository.')
        if not callable(clock):
            raise TrustError('A monotonic scheduler clock is required.')
        self.controller, self.enabled = controller, enabled
        self._repository = Path(raw).absolute()
        self.interval, self.timeout, self.clock = float(interval), float(timeout), clock
        self._context, self._client, self._target = controller.context, controller.updates, controller.target
        self._policy = controller.updates.policy
        self._guard = threading.Lock()
        self._stop, self._wake, self._idle = threading.Event(), threading.Event(), threading.Event()
        self._thread, self._cancel = None, None
        self._next = clock()
        self._status = UpdatePollStatus(
            'waiting' if enabled else 'disabled',
            'Automatic signed update checks are waiting for idle time.' if enabled
            else 'Automatic update checks are off. Use the explicit repository review to check manually.')

    @property
    def repository(self):
        return self._repository

    @property
    def status(self):
        with self._guard:
            return self._status

    @property
    def message(self):
        return self.status.message

    def _bound(self):
        return (self.controller.context == self._context and self.controller.updates is self._client
                and self.controller.target == self._target and self._client.policy is self._policy)

    def start(self):
        with self._guard:
            if not self.enabled or self._stop.is_set() or self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name='carr-update-metadata-idle', daemon=True)
            self._thread.start()
            return True

    def set_idle(self, idle):
        if type(idle) is not bool:
            raise TrustError('The host idle state must be explicit.')
        with self._guard:
            if idle:
                self._idle.set()
            else:
                self._idle.clear()
                cancelled = self._cancel
                if cancelled is not None:
                    cancelled.set()
            self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            if not self._idle.is_set():
                self._wake.wait()
                self._wake.clear()
                continue
            delay = max(0.0, self._next - self.clock())
            if delay:
                self._wake.wait(delay)
                self._wake.clear()
                continue
            if not self._bound():
                with self._guard:
                    if self._stop.is_set():
                        return
                    previous = self._status
                    self._status = UpdatePollStatus('attention',
                        'The configured update client changed. Reopen its settings before automatic checks continue.',
                        previous.release, previous.failures)
                return
            self._check()

    def _check(self):
        with self._guard:
            if self._stop.is_set() or not self._idle.is_set():
                return
            previous = self._status
            cancelled = threading.Event()
            self._cancel = cancelled
            self._status = UpdatePollStatus('checking',
                'Checking signed update metadata in the background; no package is downloaded.',
                previous.release, previous.failures)
        deadline = self.clock() + self.timeout
        def stopped():
            return (self._stop.is_set() or cancelled.is_set() or not self._idle.is_set()
                    or self.clock() >= deadline or not self._bound())
        release = None
        try:
            if stopped():
                raise InterruptedError('Update check cancelled.')
            release = self.controller.check_repository(self.repository, cancelled=stopped)
            if type(release) is not Release:
                raise TrustError('The update client returned an invalid release.')
            if stopped():
                raise InterruptedError('Update check finished after cancellation or its deadline.')
        except Exception:
            # Client diagnostics can include private paths or owner credentials.
            # Only fixed status text crosses back into the native interface.
            release = None
        with self._guard:
            self._cancel = None
            if self._stop.is_set():
                return
            if not self._bound():
                self._status = UpdatePollStatus('attention',
                    'The configured update client changed. Reopen its settings before automatic checks continue.',
                    previous.release, previous.failures)
                self._next = self.clock() + self.interval
            elif cancelled.is_set() or not self._idle.is_set():
                self._status = UpdatePollStatus('waiting',
                    'Automatic update check paused. The last verified metadata is retained.',
                    previous.release, previous.failures)
                self._next = self.clock() + self.interval
            elif release is not None and self.clock() < deadline:
                self._status = UpdatePollStatus('available',
                    f'Signed build {release.build} ({release.version}) is available for explicit review. '
                    'Automatic checks do not download or install packages.', release, 0)
                self._next = self.clock() + self.interval
            else:
                failures = min(32, previous.failures + 1)
                delay = min(86400.0, self.interval * 2 ** min(failures - 1, 6))
                self._status = UpdatePollStatus('attention',
                    'Automatic update metadata check was unavailable or exceeded its time budget. '
                    'A bounded retry is scheduled; the last verified metadata is retained.',
                    previous.release, failures)
                self._next = self.clock() + delay

    def close(self, timeout=.25):
        if type(timeout) not in (int, float) or not math.isfinite(timeout):
            raise TrustError('Invalid update-check shutdown budget.')
        self._stop.set()
        self._wake.set()
        cancelled = self._cancel
        if cancelled is not None:
            cancelled.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, min(1.0, timeout)))
        with self._guard:
            previous = self._status
            self._status = UpdatePollStatus('stopped',
                'Automatic update checks stopped with this application instance.',
                previous.release, previous.failures)
        # An unusually slow filesystem cannot hold the UI open or spawn another
        # worker. It remains one daemon; its late result has no publication path.
