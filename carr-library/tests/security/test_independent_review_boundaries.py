"""Reuse signed-grant cancellation/commit tests; installer activation is outside this client.
"""
import threading
import time

import pytest

from carr_library.security.entitlements import EntitlementStore
from carr_library.security.refresh import RefreshWorker
from .conftest import MemoryAnchor, REFERENCE, RenewalProvider, accept, envelope, grant


@pytest.mark.parametrize('stop', ['close', 'deadline'])
def test_renewal_cannot_advance_anchor_after_stop_during_local_acceptance(
        tmp_path, signer, policy, clock, stop):
    entered, release = threading.Event(), threading.Event()

    class BarrierAnchor(MemoryAnchor):
        block = False

        def load(self):
            if self.block and threading.current_thread().name == 'carr-entitlement-refresh':
                entered.set()
                assert release.wait(2)
            return super().load()

    anchor = BarrierAnchor()
    store = EntitlementStore(tmp_path / 'client', policy, anchor, clock=clock)
    original = envelope(signer, grant(clock))
    accept(store, original)

    class Provider(RenewalProvider):
        def renew(self, request, cancelled):
            return envelope(signer, grant(clock, sequence=2, request_nonce=request.nonce))

    worker = RefreshWorker(store, Provider(), REFERENCE, profile_id='trust-test',
                           clock=clock, timeout_seconds=.05 if stop == 'deadline' else 20)
    anchor.block = True
    try:
        assert worker.tick(manual=True)
        assert entered.wait(1)
        if stop == 'close':
            worker.close(0)
        else:
            time.sleep(.07)
    finally:
        release.set()
        if worker._thread is not None:
            worker._thread.join(2)
        worker.close()
    assert not worker.running
    assert anchor.load().sequence == 1
    assert store.current_path.read_bytes() == original
