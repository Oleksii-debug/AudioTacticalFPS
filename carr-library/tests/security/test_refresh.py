from dataclasses import asdict
import threading

import pytest

from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, envelope, grant


def join(worker):
    worker._thread.join(2)
    assert not worker.running


@pytest.mark.parametrize('days', [7, 14])
def test_unattended_refresh_cadence_is_separate_from_expiry_and_retains_terms(tmp_path, signer, policy, clock, days):
    store = EntitlementStore(tmp_path / 'state', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock, refresh_interval=days * 86400)))
    calls = []
    class Transport(RenewalProvider):
        def renew(self, request, cancelled):
            calls.append(request)
            return envelope(signer, grant(clock, sequence=request.after_sequence + 1,
                request_nonce=request.nonce, refresh_interval=days * 86400))
    worker = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock, jitter=lambda: .5)
    assert not worker.tick()
    clock.now += days * 86400
    assert worker.tick()
    join(worker)
    assert store.current().sequence == 2
    assert store.current().expires_at == clock.now + 30 * 86400
    assert store.current().terms_sha256 == 'a' * 64
    assert set(asdict(calls[0])) == {'product', 'subject', 'device', 'entitlement_id',
        'after_sequence', 'nonce', 'credential_profile_id', 'credential_reference', 'timeout_seconds'}
    worker.close()
    restarted = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock)
    assert not restarted.tick()
    clock.now += days * 86400
    assert restarted.tick()
    join(restarted)
    assert store.current().sequence == 3


def test_failed_refresh_backoff_survives_restart_and_never_logs_secrets(tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'state', policy, MemoryAnchor(), clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)
    class Transport(RenewalProvider):
        def renew(self, request, cancelled):
            raise OSError('TOP-SECRET-CREDENTIAL')
    worker = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock, jitter=lambda: .5)
    assert worker.tick(manual=True)
    join(worker)
    assert store.current_path.read_bytes() == original
    assert worker._schedule['next_attempt'] == clock.now + 60
    assert 'TOP-SECRET' not in worker.message
    second = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock, jitter=lambda: .5)
    assert not second.tick()
    clock.now += 60
    assert second.tick()
    join(second)
    assert second._schedule['next_attempt'] == clock.now + 120


def test_terms_change_stops_automatic_retries_until_review(tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'state', policy, MemoryAnchor(), clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)
    class Transport(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=2, terms_sha256='b' * 64, request_nonce=request.nonce))
    worker = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock)
    assert worker.tick(manual=True)
    join(worker)
    assert worker._schedule['review']
    assert not worker.tick(manual=True)
    assert store.current_path.read_bytes() == original
    accept(store, envelope(signer, grant(clock, sequence=2, terms_sha256='b' * 64)))
    worker.acknowledge_review()
    assert not worker._schedule['review']


def test_hung_adapter_does_not_block_ui_or_spawn_retries_and_late_response_is_ignored(tmp_path, signer, policy, clock):
    store = EntitlementStore(tmp_path / 'state', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    started, finish = threading.Event(), threading.Event()
    class Transport(RenewalProvider):
        def renew(self, request, cancelled):
            started.set()
            finish.wait(2)
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))
    worker = RefreshWorker(store, Transport(), REFERENCE, profile_id='trust-test', clock=clock)
    assert worker.tick(manual=True)
    assert started.wait(1)
    assert not worker.tick(manual=True)
    worker.close(0)
    assert worker.running
    finish.set()
    join(worker)
    assert store.current().sequence == 1


def test_saved_automatic_backoff_does_not_reopen_vault_until_due_but_manual_rechecks(
        tmp_path, signer, policy, clock, monkeypatch):
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    accept(store, envelope(signer, grant(clock)))
    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=request.after_sequence + 1,
                                          request_nonce=request.nonce))
    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test', clock=clock)
    assert worker.tick(manual=True)
    join(worker)
    due = worker._schedule['next_attempt']
    assert due > clock.now and store.current().sequence == 2
    reads = []
    original = store.current
    def checked():
        reads.append(clock.now)
        return original()
    monkeypatch.setattr(store, 'current', checked)
    for _ in range(20):
        assert worker.tick() is False
    assert reads == [] and not worker.running
    assert worker.tick(manual=True)
    join(worker)
    assert len(reads) == 1 and original().sequence == 3
    clock.now = worker._schedule['next_attempt']
    assert worker.tick()
    join(worker)
    assert len(reads) == 2 and original().sequence == 4
