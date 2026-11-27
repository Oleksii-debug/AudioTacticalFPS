"""Actual shared-worker ownership and cancellation; no native-wx claim."""
from concurrent.futures import CancelledError, ThreadPoolExecutor
import gc
import weakref
import subprocess
import sys
import threading

import pytest

from carr_library.ui.worker_lifetime import (DesktopExecutor, retained_cleanup_failures,
                                             retry_failed_cleanup)


def test_shutdown_cancels_direct_queued_save_and_finishes_active_child_before_owner_release():
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    order, saves, children = [], [], []
    ui_thread = threading.get_ident()
    def operation():
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c',
                                  'import sys; sys.stdin.buffer.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        children.append(child)
        entered.set()
        assert release.wait(5)
        assert child.poll() is None
        order.append(('work-ended', threading.get_ident()))
    def cleanup():
        assert order and order[-1][0] == 'work-ended'
        child, = children
        child.stdin.close()
        assert child.wait(5) == 0
        order.append(('child-closed', threading.get_ident()))
    unregister = executor.register_cleanup(cleanup)
    try:
        running = executor.submit(operation)
        assert entered.wait(5)
        pending = executor.submit(lambda: saves.append('unwanted metadata save'))
        executor.shutdown(wait=False, cancel_futures=True)
        assert pending.cancelled() and not running.done()
        assert children[0].poll() is None and order == []
        with pytest.raises(RuntimeError, match='closing'):
            executor.submit(lambda: saves.append('late save'))
        release.set()
        running.result(5)
        executor.shutdown(wait=True, cancel_futures=True)
        assert [name for name, _ in order] == ['work-ended', 'child-closed']
        assert order[0][1] == order[1][1] != ui_thread
        assert not saves and children[0].poll() == 0
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        unregister()
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(5)
            if child.stdin and not child.stdin.closed:
                child.stdin.close()


def test_shutdown_closes_admission_atomically_before_synchronous_cancel_callbacks():
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    in_cancel, finish_cancel = threading.Event(), threading.Event()
    closes, saves, errors = [], [], []
    def hold():
        entered.set()
        assert release.wait(5)
    running = executor.submit(hold)
    assert entered.wait(5)
    pending = executor.submit(lambda: saves.append('queued save'))
    def canceled(future):
        assert future.cancelled()
        in_cancel.set()
        assert finish_cancel.wait(5)
    pending.add_done_callback(canceled)
    executor.register_cleanup(lambda: closes.append(threading.get_ident()))
    def shut():
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except BaseException as error:
            errors.append(error)
    closer = threading.Thread(target=shut)
    try:
        closer.start()
        assert in_cancel.wait(5)
        with pytest.raises(RuntimeError, match='closing'):
            executor.submit(lambda: saves.append('racing save'))
        with pytest.raises(RuntimeError, match='closing'):
            executor.register_cleanup(lambda: closes.append('late owner'))
        assert closes == [] and saves == []
        finish_cancel.set()
        closer.join(5)
        assert not closer.is_alive() and not errors
        release.set()
        running.result(5)
        executor.shutdown(wait=True, cancel_futures=True)
        assert len(closes) == 1 and closes[0] != closer.ident
        assert closes[0] != threading.get_ident() and not saves
    finally:
        finish_cancel.set()
        release.set()
        closer.join(5)
        executor.shutdown(wait=True, cancel_futures=True)


def test_one_finalizer_failure_does_not_skip_other_owner_or_repeat_on_shutdown():
    executor = DesktopExecutor()
    calls = []
    release_first = threading.Event()
    def first():
        calls.append(('first', threading.get_ident()))
        if not release_first.is_set():
            raise RuntimeError('Synthetic close failure')
    def second():
        calls.append(('second', threading.get_ident()))
    executor.register_cleanup(first)
    executor.register_cleanup(second)
    prior = {token for token, _ in retained_cleanup_failures()}
    with pytest.warns(RuntimeWarning, match='owner is retained'):
        executor.shutdown(wait=True, cancel_futures=True)
    executor.shutdown(wait=True, cancel_futures=True)
    assert [name for name, _ in calls] == ['first', 'second']
    assert len({thread for _, thread in calls}) == 1
    assert calls[0][1] != threading.get_ident()
    failures = [(token, error) for token, error in retained_cleanup_failures() if token not in prior]
    assert len(failures) == 1 and isinstance(failures[0][1], RuntimeError)
    assert executor.cleanup_failures == ((first, failures[0][1]),)
    # The callback explicitly succeeds only after the owner has been reviewed.
    release_first.set()
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(retry_failed_cleanup, failures[0][0]).result(5)


def test_normally_released_owner_is_unregistered_before_shutdown():
    executor = DesktopExecutor()
    calls = []
    unregister = executor.register_cleanup(lambda: calls.append('unexpected'))
    unregister()
    unregister()
    executor.shutdown(wait=True, cancel_futures=True)
    assert not calls


def test_active_work_keeps_standard_future_result_and_queued_cancellation():
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    def operation():
        entered.set()
        assert release.wait(5)
        return 'accepted existing work'
    running = executor.submit(operation)
    try:
        assert entered.wait(5)
        pending = executor.submit(lambda: 'must not run')
        executor.shutdown(wait=False, cancel_futures=True)
        with pytest.raises(CancelledError):
            pending.result()
        release.set()
        assert running.result(5) == 'accepted existing work'
        executor.shutdown(wait=True, cancel_futures=True)
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_queued_work_cannot_enter_while_an_earlier_cancel_callback_blocks_shutdown():
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    cancel_entered, release_cancel = threading.Event(), threading.Event()
    cancel_guard = threading.Lock()
    ordinary, cleanup, errors = [], [], []
    def active():
        entered.set()
        assert release.wait(5)
        return 'already admitted'
    def canceled(future):
        if not future.cancelled():
            return
        with cancel_guard:
            first = not cancel_entered.is_set()
            cancel_entered.set()
        if first:
            assert release_cancel.wait(5)
    running = executor.submit(active)
    assert entered.wait(5)
    pending = []
    for index in range(4):
        future = executor.submit(lambda value=index: ordinary.append(value))
        future.add_done_callback(canceled)
        pending.append(future)
    executor.register_cleanup(lambda: cleanup.append(threading.get_ident()))
    def shut():
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except BaseException as error:
            errors.append(error)
    closer = threading.Thread(target=shut)
    try:
        closer.start()
        assert cancel_entered.wait(5)
        # Shutdown has not canceled every Future yet. Let the actual worker
        # drain the queue before the blocking cancellation callback returns.
        release.set()
        assert running.result(5) == 'already admitted'
        for future in pending:
            with pytest.raises(CancelledError):
                future.result(5)
        assert ordinary == [] and cleanup == []
        assert closer.is_alive()
        release_cancel.set()
        closer.join(5)
        assert not closer.is_alive() and not errors
        executor.shutdown(wait=True, cancel_futures=True)
        assert len(cleanup) == 1 and cleanup[0] != threading.get_ident()
    finally:
        release.set()
        release_cancel.set()
        closer.join(5)
        executor.shutdown(wait=True, cancel_futures=True)


def test_graceful_shutdown_preserves_explicit_non_canceling_queue_drain():
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    order = []
    def active():
        entered.set()
        assert release.wait(5)
        order.append('active')
    running = executor.submit(active)
    try:
        assert entered.wait(5)
        pending = executor.submit(lambda: order.append('queued'))
        executor.register_cleanup(lambda: order.append('cleanup'))
        executor.shutdown(wait=False, cancel_futures=False)
        release.set()
        running.result(5)
        pending.result(5)
        executor.shutdown(wait=True, cancel_futures=False)
        assert order == ['active', 'queued', 'cleanup']
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_failed_shutdown_owner_survives_executor_gc_and_only_successful_retry_releases_it():
    prior = {token for token, _ in retained_cleanup_failures()}
    entered = []
    class Owner:
        def __init__(self):
            self.attempts = 0
        def close(self):
            self.attempts += 1
            entered.append(threading.get_ident())
            if self.attempts < 3:
                raise RuntimeError('Synthetic retained cleanup failure')
    owner = Owner()
    owner_ref = weakref.ref(owner)
    executor = DesktopExecutor()
    executor.register_cleanup(owner.close)
    with pytest.warns(RuntimeWarning, match='owner is retained'):
        executor.shutdown(wait=True, cancel_futures=True)
    del executor, owner
    gc.collect()
    assert owner_ref() is not None and owner_ref().attempts == 1
    failures = [(token, error) for token, error in retained_cleanup_failures() if token not in prior]
    assert len(failures) == 1
    token = failures[0][0]
    del failures
    with ThreadPoolExecutor(max_workers=1) as caller_worker:
        with pytest.raises(RuntimeError, match='retained cleanup'):
            caller_worker.submit(retry_failed_cleanup, token).result(5)
        gc.collect()
        assert owner_ref() is not None and owner_ref().attempts == 2
        assert any(key is token for key, _ in retained_cleanup_failures())
        assert caller_worker.submit(retry_failed_cleanup, token).result(5)
    gc.collect()
    assert not any(key is token for key, _ in retained_cleanup_failures())
    assert owner_ref() is None
    assert all(thread != threading.get_ident() for thread in entered)


def test_finalizer_enqueue_failure_retains_every_owner_and_successful_retry_runs_off_ui(monkeypatch):
    prior = {token for token, _ in retained_cleanup_failures()}
    released = []
    class Owner:
        def __init__(self, name):
            self.name = name
        def close(self):
            released.append((self.name, threading.get_ident()))
    owners = [Owner('one'), Owner('two')]
    references = [weakref.ref(owner) for owner in owners]
    executor = DesktopExecutor()
    for owner in owners:
        executor.register_cleanup(owner.close)
    del owner
    submit = ThreadPoolExecutor.submit
    def unavailable(self, function, *args, **kwargs):
        if self is executor:
            raise RuntimeError('Synthetic idle worker start failure')
        return submit(self, function, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(ThreadPoolExecutor, 'submit', unavailable)
        with pytest.warns(RuntimeWarning, match='owner is retained'):
            executor.shutdown(wait=True, cancel_futures=True)
    assert released == []
    failures = [(token, error) for token, error in retained_cleanup_failures() if token not in prior]
    assert len(failures) == 2 and all('start failure' in str(error) for _, error in failures)
    del executor, owners
    gc.collect()
    assert all(reference() is not None for reference in references)
    with ThreadPoolExecutor(max_workers=1) as caller_worker:
        for token, error in failures:
            assert caller_worker.submit(retry_failed_cleanup, token).result(5)
    del failures, error
    gc.collect()
    assert {name for name, _ in released} == {'one', 'two'}
    assert all(thread != threading.get_ident() for _, thread in released)
    assert all(reference() is None for reference in references)


def test_actual_start_failure_queued_finalizer_and_explicit_retry_share_one_owner_lock(monkeypatch):
    prior = {token for token, _ in retained_cleanup_failures()}
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    retry_entered, retry_finished = threading.Event(), threading.Event()
    calls, retries, errors = [], [], []
    def first():
        calls.append(('first', threading.get_ident()))
        entered.set()
        assert release.wait(5)
    def second():
        calls.append(('second', threading.get_ident()))
    executor.register_cleanup(first)
    executor.register_cleanup(second)
    native_start = threading.Thread.start
    failed_starts = []
    def start(thread, *args, **kwargs):
        if thread.name.startswith('library') and not failed_starts:
            failed_starts.append(thread.name)
            raise RuntimeError('Synthetic actual Thread.start refusal')
        return native_start(thread, *args, **kwargs)
    retry_thread = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', start)
            with pytest.warns(RuntimeWarning, match='owner is retained'):
                executor.shutdown(wait=False, cancel_futures=True)
        assert failed_starts and entered.wait(5)
        failures = [(token, error) for token, error in retained_cleanup_failures() if token not in prior]
        assert len(failures) == 1 and 'Thread.start' in str(failures[0][1])
        token = failures[0][0]
        def retry():
            retry_entered.set()
            try:
                retries.append(retry_failed_cleanup(token))
            except BaseException as error:
                errors.append(error)
            finally:
                retry_finished.set()
        retry_thread = threading.Thread(target=retry)
        retry_thread.start()
        assert retry_entered.wait(5)
        # The first callback is still actually running behind its barrier.
        # A competing retry must not invoke it or report successful release.
        assert not retry_finished.wait(.2)
        assert [name for name, _ in calls] == ['first']
        release.set()
        retry_thread.join(5)
        assert not retry_thread.is_alive()
        executor.shutdown(wait=True, cancel_futures=True)
        assert errors == [] and retries == [False]
        assert [name for name, _ in calls] == ['first', 'second']
        assert len({thread for _, thread in calls}) == 1
        assert all(thread != threading.get_ident() for _, thread in calls)
        assert not any(key is token for key, _ in retained_cleanup_failures())
        assert executor.cleanup_failures == ()
    finally:
        release.set()
        if retry_thread is not None:
            retry_thread.join(5)
        executor.shutdown(wait=True, cancel_futures=True)


def test_actual_start_failure_never_authorizes_the_orphaned_user_work(monkeypatch):
    executor = DesktopExecutor()
    calls, failed_starts = [], []
    native_start = threading.Thread.start
    def start(thread, *args, **kwargs):
        if thread.name.startswith('library') and not failed_starts:
            failed_starts.append(thread.name)
            raise RuntimeError('Synthetic actual Thread.start refusal')
        return native_start(thread, *args, **kwargs)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', start)
            with pytest.raises(RuntimeError, match='Thread.start refusal'):
                executor.submit(lambda: calls.append('refused first callback'))
            # This starts the real worker and drains the previously queued
            # WorkItem before the second callback. No fake executor is used.
            future = executor.submit(lambda: calls.append('accepted second callback'))
            assert future.result(5) is None
        assert len(failed_starts) == 1
        assert calls == ['accepted second callback']
        assert executor.submit(lambda: 'still usable').result(5) == 'still usable'
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
