"""Host lifecycle for explicitly assembled private delivery providers.

The private provider owns signatures, trusted storage, network transport and
installation. This host never loads a module from settings, treats an offer as
installation authority, persists tokens in a library, or changes license terms.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
import threading
import time
from typing import Callable
from uuid import uuid4

from .delivery_contract import (
    Capability, DecisionState, EntitlementBoundary, EntitlementRequest,
    IntegrationContext, Readiness, UpdateOffer, provider_status,
)

_MAX_FILE_BYTES = 1024 * 1024


class DeliveryError(ValueError):
    """A public, bounded message that cannot contain private provider errors."""


class LicenseState(str, Enum):
    ABSENT = 'absent'
    VALID = 'valid'
    PERPETUAL = 'perpetual'
    GRACE = 'grace'
    EXPIRED = 'expired'
    REVIEW = 'review_required'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True)
class LicenseSnapshot:
    context: IntegrationContext
    state: LicenseState
    generation: int
    last_refresh: float | None = None
    valid_until: float | None = None

    def __post_init__(self):
        if type(self.context) is not IntegrationContext or type(self.state) is not LicenseState:
            raise ValueError('Invalid license status.')
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError('Invalid license generation.')
        for value in (self.last_refresh, self.valid_until):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid license timestamp.')
        if self.state is LicenseState.PERPETUAL and self.valid_until is not None:
            raise ValueError('A perpetual status cannot carry an undisclosed expiry.')


@dataclass(frozen=True)
class RefreshPolicy:
    """Private assembly supplies cadence; this is not an offline-validity policy."""
    interval_seconds: int
    retry_initial_seconds: int
    retry_max_seconds: int
    jitter_fraction: float
    network_authorized: bool = False

    def __post_init__(self):
        if (any(type(v) is not int or v <= 0 for v in
                (self.interval_seconds, self.retry_initial_seconds, self.retry_max_seconds))
                or self.retry_initial_seconds > self.retry_max_seconds
                or type(self.jitter_fraction) not in (int, float)
                or not 0 <= self.jitter_fraction <= 0.25
                or type(self.network_authorized) is not bool):
            raise ValueError('Invalid private refresh schedule.')


@dataclass(frozen=True)
class PreparedUpdate:
    offer: UpdateOffer
    stage_id: str

    def __post_init__(self):
        if type(self.offer) is not UpdateOffer:
            raise ValueError('Invalid verified update preparation.')
        if (not isinstance(self.stage_id, str) or len(self.stage_id) != 32
                or any(c not in '0123456789abcdef' for c in self.stage_id)):
            raise ValueError('Invalid update preparation identity.')


@dataclass(frozen=True)
class InstallApproval:
    preparation: PreparedUpdate
    nonce: str


@dataclass(frozen=True)
class UpdateBackup:
    directory: Path
    manifest_sha256: str

    def __post_init__(self):
        if (not isinstance(self.directory, Path) or not self.directory.is_absolute()
                or not isinstance(self.manifest_sha256, str)
                or len(self.manifest_sha256) != 64
                or any(c not in '0123456789abcdef' for c in self.manifest_sha256)):
            raise ValueError('A verified pre-update backup is required.')


@dataclass(frozen=True)
class UpdateResult:
    context: IntegrationContext
    stage_id: str
    target_build_id: str
    state: str

    def __post_init__(self):
        if type(self.context) is not IntegrationContext or self.state not in ('installed', 'recovered'):
            raise ValueError('Invalid update result.')
        PreparedUpdate(UpdateOffer(self.context, 'result', self.target_build_id, 1, '0' * 64), self.stage_id)


class DeliveryService:
    """One optional host around the existing entitlement/update boundaries.

    Inject providers only from the reviewed private application assembly.
    Their optional methods are required by the corresponding declared capability:
    snapshot, import_license, export_license, refresh, stage, import_update,
    install and recover. They receive the exact context and cancellation callback.
    All expensive operations belong to the host's existing background executor.
    """
    def __init__(self, context=None, *, entitlement=None, updater=None,
                 refresh_policy=None, clock=time.time, jitter=lambda: 0.5):
        if context is not None and type(context) is not IntegrationContext:
            raise ValueError('Invalid integration context.')
        if (entitlement is not None or updater is not None) and context is None:
            raise ValueError('Private providers require an explicit build context.')
        if refresh_policy is not None and type(refresh_policy) is not RefreshPolicy:
            raise ValueError('Invalid refresh policy.')
        self.context = context
        self.entitlement = entitlement
        self.updater = updater
        self.refresh_policy = refresh_policy
        self._clock, self._jitter = clock, jitter
        self._lock = threading.RLock()
        self._offer = self._prepared = self._approval = None
        self._next_refresh = None
        self._failures = 0
        self._last_clock = None
        self._closed = threading.Event()
        self._background = False

    def close(self):
        self._closed.set()

    def _cancel(self, cancelled):
        return lambda: self._closed.is_set() or cancelled()

    def _invoke(self, provider, family, capability, method, *args,
                cancelled=lambda: False, _normalize=None, _guard=None, **kwargs):
        if self._closed.is_set() or cancelled():
            raise InterruptedError('Operation cancelled.')
        status = provider_status(provider, family=family)
        if status.state is not Readiness.DECLARED or capability not in status.capabilities:
            raise DeliveryError('This private integration capability is not configured.')
        if not self._lock.acquire(blocking=False):
            raise DeliveryError('Another license or update operation is running.')
        try:
            if _guard is not None:
                _guard()
            try:
                callback = getattr(provider, method)
                result = callback(self.context, *args, cancelled=self._cancel(cancelled), **kwargs)
            except InterruptedError:
                raise InterruptedError('Operation cancelled.') from None
            except Exception:
                raise DeliveryError('The private service could not complete this operation. Your saved work remains available.') from None
            # A provider can return after a durable publication. Late cancellation
            # must not reverse that receipt or encourage an unsafe retry.
            return _normalize(result) if _normalize is not None else result
        finally:
            self._lock.release()

    def _snapshot(self, value):
        if type(value) is not LicenseSnapshot or value.context != self.context:
            raise DeliveryError('The license response does not match this installation.')
        return value

    def snapshot(self, *, cancelled=lambda: False):
        if self.entitlement is None:
            return None
        return self._snapshot(self._invoke(self.entitlement, 'entitlement',
            Capability.ENTITLEMENT_DECISION, 'snapshot', cancelled=cancelled))

    def decision(self, action):
        if self.context is None:
            raise DeliveryError('No private build context is configured.')
        return EntitlementBoundary(self.entitlement).decide(
            EntitlementRequest(self.context, uuid4().hex, action))

    def require(self, action):
        decision = self.decision(action)
        if decision.state is not DecisionState.ALLOWED:
            raise DeliveryError('This operation is not authorized by the configured private provider.')
        return decision

    def import_license(self, source, *, cancelled=lambda: False):
        def accept(result):
            value = self._snapshot(result)
            self._next_refresh = None
            return value
        return self._invoke(self.entitlement, 'entitlement',
            Capability.OFFLINE_LICENSE_IMPORT, 'import_license', Path(source),
            cancelled=cancelled, _normalize=accept)

    def export_license(self, *, cancelled=lambda: False):
        raw = self._invoke(self.entitlement, 'entitlement',
            Capability.OFFLINE_LICENSE_IMPORT, 'export_license', cancelled=cancelled)
        if type(raw) is not bytes or not 0 < len(raw) <= _MAX_FILE_BYTES:
            raise DeliveryError('The signed license export is invalid.')
        return raw

    def refresh(self, *, network_approved=False, cancelled=lambda: False):
        if not network_approved:
            raise DeliveryError('Approve this separate entitlement network operation first.')
        before = self.snapshot(cancelled=cancelled)
        if before is not None and before.state is LicenseState.PERPETUAL:
            return before
        value = self._snapshot(self._invoke(self.entitlement, 'entitlement',
            Capability.SIGNED_RENEWAL, 'refresh', cancelled=cancelled))
        if before is not None and value.generation < before.generation:
            raise DeliveryError('A stale license response was refused.')
        return value

    def check_update(self, *, network_approved=False, cancelled=lambda: False):
        if not network_approved:
            raise DeliveryError('Approve this separate update network operation first.')
        value = self._invoke(self.updater, 'update', Capability.UPDATE_CHECK,
                             'check', cancelled=cancelled)
        if value is not None and (type(value) is not UpdateOffer or value.context != self.context):
            raise DeliveryError('The update offer does not match this installation.')
        self._offer = value
        return value

    def _accept_preparation(self, value):
        with self._lock:
            if type(value) is not PreparedUpdate or value.offer.context != self.context:
                raise DeliveryError('The verified update preparation is invalid.')
            self._prepared, self._approval = value, None
            return value

    def stage_update(self, offer, *, network_approved=False, cancelled=lambda: False, progress=None):
        if type(offer) is not UpdateOffer or offer.context != self.context or offer is not self._offer:
            raise DeliveryError('Choose an update offer for this installation.')
        if not network_approved:
            raise DeliveryError('Approve the update download first.')
        def accept(result):
            if type(result) is not PreparedUpdate or result.offer != offer:
                raise DeliveryError('The downloaded update differs from the approved offer.')
            return self._accept_preparation(result)
        return self._invoke(self.updater, 'update', Capability.UPDATE_CHECK,
            'stage', offer, cancelled=cancelled, progress=progress, _normalize=accept)

    def import_update(self, source, *, cancelled=lambda: False, progress=None):
        return self._invoke(self.updater, 'update',
            Capability.OFFLINE_UPDATE_IMPORT, 'import_update', Path(source),
            cancelled=cancelled, progress=progress, _normalize=self._accept_preparation)

    def recover_update(self, *, cancelled=lambda: False):
        def accept(result):
            self._prepared = self._approval = None
            return self._accept_preparation(result) if result is not None else None
        return self._invoke(self.updater, 'update', Capability.UPDATE_RECOVERY,
                            'recover', cancelled=cancelled, _normalize=accept)

    def approve_install(self, preparation):
        if preparation is not self._prepared:
            raise DeliveryError('Review the current verified update before installing.')
        approval = InstallApproval(preparation, uuid4().hex)
        self._approval = approval
        return approval

    def install_update(self, approval, *, idle, backup, cancelled=lambda: False, progress=None):
        if (type(approval) is not InstallApproval or approval is not self._approval
                or approval.preparation is not self._prepared):
            raise DeliveryError('A fresh approval for this exact update is required.')
        if not callable(idle) or not idle():
            raise DeliveryError('Finish active jobs before installing this update.')
        preparation = self._prepared
        self._approval = None
        if not callable(backup):
            raise DeliveryError('Create a verified backup before installing.')
        saved = backup()
        if type(saved) is not UpdateBackup:
            raise DeliveryError('The pre-update backup could not be verified.')
        if cancelled() or not idle():
            raise InterruptedError('Installation cancelled. The completed backup remains available.')
        def current():
            if self._prepared is not preparation:
                raise DeliveryError('The staged update changed during backup. Review it again.')
        def accept(result):
            self._prepared = None
            if (type(result) is not UpdateResult or result.context != self.context
                    or result.stage_id != preparation.stage_id
                    or result.target_build_id != preparation.offer.target_build_id):
                raise DeliveryError('The installation result could not be verified. Use update recovery.')
            return result
        return self._invoke(self.updater, 'update', Capability.UPDATE_INSTALL,
            'install', preparation, backup=saved, cancelled=cancelled, progress=progress,
            _guard=current, _normalize=accept)

    def refresh_due(self, *, cancelled=lambda: False):
        if self._background:
            return False
        return self._check_refresh_due(cancelled=cancelled)

    def _check_refresh_due(self, *, cancelled=lambda: False):
        policy = self.refresh_policy
        if self._closed.is_set() or policy is None or not policy.network_authorized:
            return False
        status = provider_status(self.entitlement, family='entitlement')
        if Capability.SIGNED_RENEWAL not in status.capabilities:
            return False
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            return False
        if self._last_clock is not None and now < self._last_clock:
            # Clock changes do not revoke data access or force online recovery.
            self._last_clock = now
            self._next_refresh = now + policy.retry_max_seconds
            return False
        self._last_clock = now
        if self._next_refresh is None:
            try:
                snapshot = self.snapshot(cancelled=cancelled)
            except (DeliveryError, InterruptedError):
                self._next_refresh = now + policy.retry_initial_seconds
                return False
            if snapshot is None or snapshot.state in (LicenseState.ABSENT, LicenseState.PERPETUAL, LicenseState.REVIEW):
                self._next_refresh = now + policy.interval_seconds
                return False
            last = snapshot.last_refresh
            self._next_refresh = now if last is None else last + policy.interval_seconds
        return now >= self._next_refresh

    def submit_refresh(self, submit: Callable, *, idle: Callable, completed=lambda value: None):
        """Schedule all private status/verification work on the existing executor."""
        policy = self.refresh_policy
        if (self._closed.is_set() or policy is None or not policy.network_authorized
                or self._background or not idle()):
            return None
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            return None
        if (self._next_refresh is not None and now < self._next_refresh
                and (self._last_clock is None or now >= self._last_clock)):
            self._last_clock = now
            return None
        self._background = True

        def run():
            if self._closed.is_set():
                raise InterruptedError('Operation cancelled.')
            if not self._check_refresh_due(cancelled=self._closed.is_set):
                return None
            return self.refresh(network_approved=True, cancelled=self._closed.is_set)

        def finish(future):
            now, policy = self._clock(), self.refresh_policy
            value = None
            try:
                value = future.result()
            except Exception:
                self._failures = min(self._failures + 1, 30)
                delay = min(policy.retry_max_seconds,
                            policy.retry_initial_seconds * (2 ** (self._failures - 1)))
            else:
                if value is None:
                    # The asynchronous first probe already calculated the due
                    # time from the last verified refresh; do not postpone it.
                    self._background = False
                    return
                self._failures = 0
                delay = policy.interval_seconds
            try:
                sample = float(self._jitter())
                if not math.isfinite(sample) or not 0 <= sample <= 1:
                    sample = 0.5
            except Exception:
                sample = 0.5
            delay *= 1 + policy.jitter_fraction * (2 * sample - 1)
            if self._failures:
                delay = min(policy.retry_max_seconds, delay)
            self._next_refresh = now + delay
            self._background = False
            if not self._closed.is_set():
                try:
                    completed(value)
                except Exception:
                    pass

        try:
            future = submit(run)
            future.add_done_callback(finish)
        except Exception:
            self._background = False
            self._next_refresh = self._clock() + self.refresh_policy.retry_initial_seconds
            raise DeliveryError('Background renewal could not be scheduled.') from None
        return future
