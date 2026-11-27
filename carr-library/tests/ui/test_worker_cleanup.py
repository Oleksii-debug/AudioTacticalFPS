"""Actual executor ordering prevents disposal during synthesis without blocking UI."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from carr_library.ui.worker_cleanup import close_after_worker


@pytest.mark.parametrize('failed', (False, True))
def test_cleanup_waits_for_active_worker_and_returns_before_it_finishes(failed):
    entered, release, returned, closed = Event(), Event(), Event(), Event()
    order, cleanup = [], []
    executor = ThreadPoolExecutor(max_workers=1)
    def work():
        entered.set()
        assert release.wait(5)
        order.append('worker finished')
        if failed:
            raise OSError('Interrupted production')
    def close():
        order.append('service closed')
        closed.set()
    pending = executor.submit(work)
    assert entered.wait(2)
    def from_ui():
        cleanup.append(close_after_worker(executor, SimpleNamespace(close=close)))
        returned.set()
    ui = Thread(target=from_ui)
    ui.start()
    try:
        assert returned.wait(2), 'UI must return while the active worker is still blocked.'
        assert not closed.is_set() and not cleanup[0].done()
    finally:
        release.set()
        ui.join(5)
        executor.shutdown(wait=True)
    cleanup[0].result(timeout=1)
    if failed:
        with pytest.raises(OSError):
            pending.result()
    else:
        pending.result()
    assert order == ['worker finished', 'service closed']
