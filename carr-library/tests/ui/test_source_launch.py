"""Actual worker/queued-event ownership; no simulated native device result."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from time import monotonic, sleep

import pytest

from carr_library.ui.source_launch import call_on_ui


def queued(queue, future):
    deadline = monotonic() + 5
    while not queue and not future.done():
        if monotonic() >= deadline:
            raise AssertionError('The source opener did not queue an event.')
        sleep(.001)


def test_actual_worker_waits_and_native_action_runs_only_on_driving_thread():
    queue, calls = deque(), []
    event_thread = get_ident()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, queue.append,
                                 lambda: calls.append(get_ident()) or True)
        queued(queue, future)
        assert not future.done() and calls == []
        queue.popleft()()
        assert future.result(timeout=5) is True
    assert calls == [event_thread]


def test_cancelled_queued_launch_is_inert_when_delivered_late():
    queue, calls, cancelled = deque(), [], Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, queue.append, lambda: calls.append(True),
                                 cancelled=cancelled.is_set)
        queued(queue, future)
        cancelled.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
        queue.popleft()()
    assert calls == []


def test_expired_queued_launch_is_inert_when_delivered_late():
    queue, calls = deque(), []
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, queue.append, lambda: calls.append(True),
                                 timeout=.05)
        queued(queue, future)
        with pytest.raises(TimeoutError):
            future.result(timeout=5)
        queue.popleft()()
    assert calls == []


def test_dispatch_failure_after_queueing_revokes_the_unacknowledged_action():
    queue, calls = deque(), []
    def broken_post(callback):
        queue.append(callback)
        raise RuntimeError('Posting refused')
    with pytest.raises(RuntimeError, match='Posting refused'):
        call_on_ui(broken_post, lambda: calls.append(True))
    queue.popleft()()
    assert calls == []


def test_duplicate_event_cannot_open_the_source_twice():
    queue, calls = deque(), []
    def duplicate(callback):
        queue.extend((callback, callback))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, duplicate, lambda: calls.append(True) or True)
        queued(queue, future)
        queue.popleft()()
        assert future.result(timeout=5) is True
        queue.popleft()()
    assert calls == [True]


def test_started_native_action_settles_before_cancellation_releases_worker():
    queue, cancelled, calls = deque(), Event(), []
    with ThreadPoolExecutor(max_workers=1) as executor:
        def action():
            assert not future.done()
            cancelled.set()
            calls.append('native action acknowledged')
            return True
        future = executor.submit(call_on_ui, queue.append, action, cancelled=cancelled.is_set)
        queued(queue, future)
        queue.popleft()()
        assert future.result(timeout=5) is True
    assert calls == ['native action acknowledged']


def test_native_failure_returns_to_worker_without_unhandled_event_exception():
    queue = deque()
    def failed():
        raise ValueError('Native opener refused')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, queue.append, failed)
        queued(queue, future)
        queue.popleft()()
        with pytest.raises(ValueError, match='Native opener refused'):
            future.result(timeout=5)


def test_cancellation_callback_failure_is_settled_without_leaving_queued_authority():
    queue, calls, started = deque(), [], Event()
    def cancellation():
        if started.is_set():
            raise RuntimeError('Cancellation observer failed')
        return False
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(call_on_ui, queue.append, lambda: calls.append(True),
                                 cancelled=cancellation)
        queued(queue, future)
        started.set()
        queue.popleft()()
        with pytest.raises(RuntimeError, match='Cancellation observer failed'):
            future.result(timeout=5)
    assert calls == []


@pytest.mark.parametrize('timeout', [0, -1, True, float('inf'), float('nan'), 61],
                         ids=['zero', 'negative', 'boolean', 'infinite', 'nan', 'over-bound'])
def test_invalid_handoff_deadline_never_posts_an_action(timeout):
    posted = []
    with pytest.raises(ValueError):
        call_on_ui(posted.append, lambda: True, timeout=timeout)
    assert posted == []


@pytest.mark.parametrize('arrival', [.05, .051], ids=['exact-deadline', 'after-deadline'])
def test_expired_event_is_refused_before_worker_poll_observes_timeout(monkeypatch, arrival):
    from carr_library.ui import source_launch
    clock, calls = [100.0], []
    monkeypatch.setattr(source_launch, 'monotonic', lambda: clock[0])
    def late_event(callback):
        # Deliver the event within post(): the waiting loop has not begun, so
        # only invoke's locked deadline check can revoke this queued action.
        clock[0] = 100.0 + arrival
        callback()
    with pytest.raises(TimeoutError, match='handoff deadline'):
        source_launch.call_on_ui(late_event, lambda: calls.append(True), timeout=.05)
    assert calls == []


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit], ids=['keyboard', 'exit'])
def test_wait_interrupt_retains_borrowed_resources_until_started_action_settles(monkeypatch, interrupt):
    from carr_library.ui import source_launch
    queue, calls = deque(), []
    started, release, interrupted, unwound = Event(), Event(), Event(), Event()
    class InterruptedWait:
        def __init__(self):
            self.actual = Event()
            self.injected = False
        def set(self):
            self.actual.set()
        def wait(self, timeout):
            if started.is_set() and not self.injected:
                self.injected = True
                interrupted.set()
                raise interrupt('Injected worker wait interruption')
            return self.actual.wait(timeout)
    monkeypatch.setattr(source_launch, 'Event', InterruptedWait)
    def native_action():
        started.set()
        assert release.wait(5), 'The held native action was not released.'
        assert not unwound.is_set()
        calls.append('acknowledged')
        return True
    def borrower():
        try:
            return source_launch.call_on_ui(queue.append, native_action)
        finally:
            unwound.set()
    with ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(borrower)
        queued(queue, worker)
        native = executor.submit(queue.popleft())
        try:
            assert interrupted.wait(5), 'The worker wait was not interrupted.'
            assert not worker.done() and not unwound.is_set()
        finally:
            release.set()
        native.result(timeout=5)
        with pytest.raises(interrupt, match='Injected worker wait interruption'):
            worker.result(timeout=5)
    assert calls == ['acknowledged'] and unwound.is_set()
