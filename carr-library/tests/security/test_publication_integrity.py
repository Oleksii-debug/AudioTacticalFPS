"""Reuse signed-grant cancellation/commit tests; installer activation is outside this client.
"""
import threading
import time

import pytest

from carr_library.security.anchors import Anchor, WindowsVaultAnchor
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, envelope, grant
from .test_canonical_boundary import CanonicalVault


@pytest.mark.parametrize('stop', ['close', 'deadline'])
def test_stop_during_final_canonical_vault_compare_prevents_protected_put(
        tmp_path, signer, policy, clock, stop, monkeypatch):
    from types import SimpleNamespace
    from carr_library.security import refresh as refresh_module

    entered, release = threading.Event(), threading.Event()
    # Advance only after the actual canonical vault comparison is blocked.
    # Patching this module's clock leaves threading/Event waits on real time;
    # slow Windows signing or disk I/O cannot consume the tested deadline early.
    elapsed = [100.0]
    monkeypatch.setattr(refresh_module, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))

    class BarrierVault(CanonicalVault):
        refresh_reads = 0

        def get(self, profile_id, reference):
            if threading.current_thread().name == 'carr-entitlement-refresh':
                self.refresh_reads += 1
                if self.refresh_reads == 2:
                    entered.set()
                    assert release.wait(5)
            return super().get(profile_id, reference)

    vault = BarrierVault()
    anchor = WindowsVaultAnchor(REFERENCE, '1' * 64, product='carr-test',
                               profile_id='trust-test', vault=vault)
    vault.put('trust-test', anchor.encode(Anchor()), reference=REFERENCE)
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)
    puts = sum(call[0] == 'put' for call in vault.calls)

    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))

    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test',
                           clock=clock, timeout_seconds=.05 if stop == 'deadline' else 20)
    try:
        assert worker.tick(manual=True)
        assert entered.wait(5)
        assert vault.refresh_reads == 2
        if stop == 'close':
            worker.close(0)
        else:
            elapsed[0] += worker.timeout_seconds + .01
    finally:
        release.set()
        if worker._thread is not None:
            worker._thread.join(5)
        worker.close()
    assert not worker.running
    assert anchor.load().sequence == 1
    assert store.current_path.read_bytes() == original
    assert sum(call[0] == 'put' for call in vault.calls) == puts



def test_close_after_protected_advance_finishes_committed_grant_publication(tmp_path, signer, policy, clock):
    advanced, release = threading.Event(), threading.Event()

    class BarrierAnchor(MemoryAnchor):
        def compare_and_set(self, expected, replacement, *, guard=lambda: None):
            super().compare_and_set(expected, replacement, guard=guard)
            if replacement.sequence == 2:
                advanced.set()
                assert release.wait(2)

    anchor = BarrierAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    accept(store, envelope(signer, grant(clock)))

    class Provider(RenewalProvider):
        raw = None

        def renew(self, request, cancelled):
            self.raw = envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))
            return self.raw

    provider = Provider()
    worker = RefreshWorker(store, provider, REFERENCE, profile_id='trust-test', clock=clock)
    try:
        assert worker.tick(manual=True)
        assert advanced.wait(1)
        worker.close(0)
    finally:
        release.set()
        if worker._thread is not None:
            worker._thread.join(2)
        worker.close()
    assert not worker.running
    assert anchor.load().sequence == 2
    assert store.current_path.read_bytes() == provider.raw
    assert not store.pending_path.exists()
    assert store.current().sequence == 2
