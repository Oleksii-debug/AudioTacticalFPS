import threading
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.delivery_contract import CONTRACT_VERSION, Capability, ProviderCapabilities
from carr_library.security._files import BusyError, TrustError
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import IdleRenewalHost, RefreshWorker
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, envelope, grant


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


def test_actual_idle_host_renews_without_clicks_and_stays_off_until_explicit_opt_in(
        tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    clock.now += 7 * 86400
    calls = []
    called = threading.Event()
    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            calls.append((request, threading.get_ident()))
            called.set()
            return envelope(signer, grant(clock, sequence=request.after_sequence + 1, request_nonce=request.nonce))
    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test', clock=clock)

    def renewed(sequence):
        # A live periodic host can start another trust read immediately after
        # running becomes false. Observe the signed state inside the bounded
        # wait; only ordinary lock contention is retryable by this fixture.
        if worker.running:
            return False
        try:
            current = store.current()
        except BusyError:
            return False
        return current is not None and current.sequence == sequence

    disabled = IdleRenewalHost(worker)
    disabled.set_idle(True)
    assert not disabled.start() and disabled._thread is None
    assert not called.wait(.03)
    host = IdleRenewalHost(worker, enabled=True, interval=.02)
    try:
        assert host.start() and not host.start()
        assert not called.wait(.05)  # An active user job has not signalled idle.
        host.set_idle(True)
        assert called.wait(1)
        wait_until(lambda: renewed(2))
        assert len(calls) == 1
        assert calls[0][1] != threading.get_ident()
        assert calls[0][0].credential_profile_id == 'trust-test'
        assert calls[0][0].credential_reference == REFERENCE
        called.clear()
        clock.now += 7 * 86400
        assert called.wait(1)  # The periodic host checks without another UI action.
        wait_until(lambda: renewed(3))
        assert len(calls) == 2
    finally:
        host.close()
    assert not host._thread.is_alive() and not worker.running
    assert store.current().sequence == 3


def test_blocked_vault_check_does_not_block_data_save_or_shutdown_and_cannot_launch_late(
        tmp_path, signer, policy, clock, monkeypatch):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    clock.now += 7 * 86400
    entered, release = threading.Event(), threading.Event()
    original = store.current
    def blocked_current():
        entered.set()
        assert release.wait(2)
        return original()
    monkeypatch.setattr(store, 'current', blocked_current)
    calls = []
    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            calls.append(request)
            raise AssertionError('A closed host cannot start a provider call.')
    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test', clock=clock)
    host = IdleRenewalHost(worker, enabled=True, interval=.01)
    try:
        host.start()
        host.set_idle(True)
        assert entered.wait(1)
        app = LibraryApp(tmp_path / 'library')
        workspace = app.create_workspace('Save while client vault is busy')
        book = app.add_text(workspace, 'Owner data', 'This source remains independent of licensing.')
        assert book['edition']
        assert app.search(workspace, 'independent')[0].quote == 'independent'
        start = time.monotonic()
        host.close(.02)
        assert time.monotonic() - start < .25
        assert not calls
    finally:
        release.set()
        host.close()
    assert not host._thread.is_alive() and not calls
    assert original().sequence == 1


def test_missing_or_withdrawn_canonical_renewal_capability_never_calls_transport(
        tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    calls = []
    class Provider(RenewalProvider):
        enabled = True
        def describe(self):
            return super().describe() if self.enabled else ProviderCapabilities(CONTRACT_VERSION, frozenset({Capability.ENTITLEMENT_DECISION}))
        def renew(self, request, cancelled):
            calls.append(request)
    provider = Provider()
    worker = RefreshWorker(store, provider, REFERENCE, profile_id='trust-test', clock=clock)
    provider.enabled = False
    with pytest.raises(TrustError, match='signed-renewal provider'):
        worker.tick(manual=True)
    with pytest.raises(TrustError, match='signed-renewal provider'):
        RefreshWorker(store, object(), REFERENCE, profile_id='trust-test', clock=clock)
    with pytest.raises(TrustError, match='profile identity'):
        RefreshWorker(store, Provider(), REFERENCE, profile_id='invalid/profile', clock=clock)
    assert not calls and not worker.running


def test_late_signed_reply_after_time_budget_cannot_advance_protected_state(
        tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)
    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            time.sleep(.03)
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))
    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test', clock=clock, timeout_seconds=.01)
    assert worker.tick(manual=True)
    wait_until(lambda: not worker.running)
    assert store.current_path.read_bytes() == original and store.current().sequence == 1
    assert worker._schedule['failures'] == 1
