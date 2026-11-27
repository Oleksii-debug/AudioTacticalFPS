"""One cancellable background renewal at a time, with durable bounded backoff.

There is no built-in endpoint. An owner adapter resolves its OS-vault credential
reference, enforces its HTTPS/redirect policy and bounded network timeout, and
returns signed bytes. Requests contain no library paths, documents or searches.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import random
import secrets
import threading
import time
from typing import Protocol

from .anchors import validate_profile_id, validate_reference
from ..delivery_contract import Capability, ProviderCapabilities, Readiness, provider_status
from ._files import TrustError, atomic_write, json_bytes, json_value, locked, read_bytes
from .entitlements import EntitlementStore, ReviewRequired


@dataclass(frozen=True)
class RefreshRequest:
    product: str
    subject: str
    device: str | None
    entitlement_id: str
    after_sequence: int
    nonce: str
    credential_profile_id: str
    credential_reference: str
    timeout_seconds: float


class AuthorityTransport(Protocol):
    """Explicit owner infrastructure; declarations do not establish qualification."""
    def describe(self) -> ProviderCapabilities: ...
    def renew(self, request: RefreshRequest, cancelled: threading.Event) -> bytes: ...


class RefreshWorker:
    def __init__(self, store: EntitlementStore, transport: AuthorityTransport,
                 credential_reference: str, *, profile_id: str, clock=time.time, jitter=random.random,
                 timeout_seconds=20.0):
        validate_profile_id(profile_id)
        validate_reference(credential_reference, profile_id)
        self._check_provider(transport)
        if not 0 < timeout_seconds <= 60:
            raise TrustError('Invalid authority timeout.')
        self.store, self.transport = store, transport
        self.profile_id = profile_id
        self.credential_reference, self.clock, self.jitter = credential_reference, clock, jitter
        self.timeout_seconds = float(timeout_seconds)
        self.path = store.root / 'refresh-schedule.json'
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._checking = False
        self._cancel = None
        self._last = 'Automatic renewal has not run.'
        self._schedule = self._load()

    @staticmethod
    def _check_provider(provider):
        status = provider_status(provider, family='entitlement')
        if status.state != Readiness.DECLARED or Capability.SIGNED_RENEWAL not in status.capabilities:
            raise TrustError('An explicitly configured signed-renewal provider is required.')

    def _load(self):
        if not os.path.lexists(self.path):
            return {'version': 1, 'failures': 0, 'next_attempt': 0, 'review': False}
        value = json_value(read_bytes(self.path, 2048), maximum=2048)
        if (type(value) is not dict or set(value) != {'version', 'failures', 'next_attempt', 'review'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['failures']) is not int or not 0 <= value['failures'] <= 32
                or type(value['next_attempt']) is not int or not 0 <= value['next_attempt'] <= 4102444800
                or type(value['review']) is not bool):
            raise TrustError('Invalid renewal schedule. Review client settings.')
        return value

    @property
    def message(self):
        with self._guard:
            return self._last

    @property
    def running(self):
        with self._guard:
            return self._checking or (self._thread is not None and self._thread.is_alive())

    def _save(self, value):
        with locked(self.store.root, '.refresh.lock'):
            atomic_write(self.path, json_bytes(value))
        self._schedule = value

    def tick(self, *, manual=False, cancelled=lambda: False):
        """Admit one due renewal from a background host or existing user job.

        Local vault/file/signature checks can block, so GUI callbacks must use
        IdleRenewalHost or the application's existing job executor.
        """
        with self._guard:
            if self._stop.is_set() or cancelled() or self._checking or (self._thread is not None and self._thread.is_alive()):
                return False
            self._checking = True
        try:
            self._check_provider(self.transport)
            grant = self.store.current()
            if grant is None or not grant.needs_refresh:
                return False
            now = int(self.clock())
            due = self._schedule['next_attempt'] or grant.issued_at + grant.refresh_interval
            if self._schedule['review'] or (not manual and now < due):
                return False
            request = RefreshRequest(grant.product, grant.subject, grant.device,
                                     grant.entitlement_id, grant.sequence, secrets.token_hex(16),
                                     self.profile_id, self.credential_reference, self.timeout_seconds)
            with self._guard:
                # Closing while the local vault was busy cannot grant a late
                # request authority to start a connection.
                if self._stop.is_set() or cancelled():
                    return False
                self._cancel = threading.Event()
                self._last = 'Checking the configured authority in the background.'
                self._thread = threading.Thread(target=self._run, args=(request, self._cancel),
                                                name='carr-entitlement-refresh', daemon=True)
                self._thread.start()
                return True
        finally:
            with self._guard:
                self._checking = False

    def _run(self, request, cancelled):
        deadline = time.monotonic() + request.timeout_seconds
        def guard():
            if self._stop.is_set() or cancelled.is_set():
                raise InterruptedError('Renewal was cancelled before protected publication.')
            if time.monotonic() >= deadline:
                raise TimeoutError('The authority exceeded its time budget.')
        try:
            guard()
            raw = self.transport.renew(request, cancelled)
            guard()
            grant = self.store.accept(raw, expected_nonce=request.nonce, guard=guard)
            value = {'version': 1, 'failures': 0, 'next_attempt': int(self.clock()) + (grant.refresh_interval or 0),
                     'review': False}
            message = 'The signed entitlement was refreshed without changing accepted terms.'
        except ReviewRequired:
            value = {'version': 1, 'failures': min(32, self._schedule['failures'] + 1),
                     'next_attempt': 0, 'review': True}
            message = 'The authority response needs explicit review. The last verified entitlement is retained.'
        except Exception:
            # Transport exceptions may contain authorization headers or URLs.
            # Keep only a fixed, nonsecret diagnostic in accessible status.
            count = min(32, self._schedule['failures'] + 1)
            jitter = min(1.0, max(0.0, float(self.jitter())))
            delay = min(3600, 60 * 2 ** min(count - 1, 6))
            value = {'version': 1, 'failures': count,
                     'next_attempt': int(self.clock()) + int(delay * (0.8 + 0.4 * jitter)), 'review': False}
            message = 'Renewal is temporarily unavailable. A bounded retry is scheduled; the last verified entitlement is retained.'
        if self._stop.is_set() or cancelled.is_set():
            return
        try:
            self._save(value)
        except (TrustError, OSError):
            message = 'Renewal finished, but its retry schedule could not be saved. Review client settings.'
        with self._guard:
            self._last = message

    def acknowledge_review(self):
        """After the host has explicitly imported a reviewed replacement."""
        with self._guard:
            if self._checking or (self._thread is not None and self._thread.is_alive()):
                raise TrustError('Wait for the current renewal to finish.')
            self._checking = True
        try:
            grant = self.store.current()
            self._save({'version': 1, 'failures': 0,
                        'next_attempt': int(self.clock()) + (grant.refresh_interval if grant and grant.refresh_interval else 0),
                        'review': False})
        finally:
            with self._guard:
                self._checking = False

    def close(self, timeout=0.25):
        self._stop.set()
        self.cancel_current()
        thread = self._thread
        if thread is not None:
            thread.join(max(0, min(1.0, timeout)))
        # An uncooperative owner transport cannot block shutdown or cause a
        # second thread storm. It remains one daemon, and late replies are ignored.

    def cancel_current(self):
        # Event publication is atomic; cancellation never waits for a vault call.
        cancelled = self._cancel
        if cancelled is not None:
            cancelled.set()


class IdleRenewalHost:
    """Opt-in idle admission with no vault, crypto or network work on the UI.

    The host supplies an idle signal; this scheduler owns no library/job lock
    and never changes user data. RefreshWorker remains the sole renewal worker.
    """
    def __init__(self, worker: RefreshWorker, *, enabled=False, interval=60.0):
        if not isinstance(worker, RefreshWorker) or type(enabled) is not bool:
            raise TrustError('An explicitly configured renewal worker is required.')
        if type(interval) not in (int, float) or not 0.01 <= interval <= 3600:
            raise TrustError('Invalid idle renewal interval.')
        self.worker, self.enabled, self.interval = worker, enabled, float(interval)
        self._stop, self._wake, self._idle = threading.Event(), threading.Event(), threading.Event()
        self._guard = threading.Lock()
        self._thread = None

    def start(self):
        with self._guard:
            if not self.enabled or self._stop.is_set() or self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name='carr-entitlement-idle', daemon=True)
            self._thread.start()
            return True

    def set_idle(self, idle):
        if type(idle) is not bool:
            raise TrustError('The host idle state must be explicit.')
        if idle:
            was_idle = self._idle.is_set()
            self._idle.set()
            if not was_idle:
                self._wake.set()
        else:
            self._idle.clear()

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self._idle.is_set():
                try:
                    self.worker.tick()
                except Exception:
                    # Provider/vault diagnostics can include credentials.
                    with self.worker._guard:
                        self.worker._last = 'Automatic renewal needs attention. The last verified permission is retained.'

    def close(self, timeout=0.25):
        started = time.monotonic()
        budget = max(0, min(1.0, timeout))
        self._stop.set()
        self._wake.set()
        self.worker.close(0)
        thread = self._thread
        if thread is not None:
            thread.join(budget)
        self.worker.close(max(0, budget - (time.monotonic() - started)))
