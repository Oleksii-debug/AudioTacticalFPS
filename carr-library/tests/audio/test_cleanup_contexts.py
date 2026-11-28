"""Actual staging/native locks share the existing pending cleanup ownership."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from carr_library.audio.cleanup import (
    AudioCleanupPending, CleanupOwner, retained_context, retained_state_lock,
    retained_temporary_directory)
from carr_library.operations import ResourceBroker, ResourceRequest
from carr_library.operations._locks import NativeLock


def owner():
    allow = threading.Event()
    return CleanupOwner(lambda: allow.is_set()), allow


def busy(root, name='state'):
    other = NativeLock(root / ('.' + name + '.lock'))
    obtained = other.acquire()
    if obtained:
        other.release()
    return not obtained


@pytest.mark.parametrize('wrapper', ['direct', 'cause', 'context'])
def test_real_stage_survives_direct_and_masked_pending_cleanup(tmp_path, wrapper):
    cleanup, allow = owner()
    pending = AudioCleanupPending('Native process still owns this fixture', cleanup_owner=cleanup)
    with pytest.raises((AudioCleanupPending, OSError)):
        with retained_temporary_directory(prefix='.owned-', dir=tmp_path) as temporary:
            stage = Path(temporary)
            data = stage / 'partial.wav'
            data.write_bytes(b'Real private staging bytes')
            if wrapper == 'direct':
                raise pending
            try:
                raise pending
            except AudioCleanupPending:
                if wrapper == 'cause':
                    raise OSError('Journal publication failed') from pending
                raise OSError('Journal publication failed')
    assert stage.is_dir() and data.read_bytes() == b'Real private staging bytes'
    assert cleanup.close() is False
    assert stage.is_dir()
    allow.set()
    assert cleanup.close() is True
    assert not stage.exists()


def test_real_native_lock_releases_on_cleanup_callback_thread(tmp_path):
    cleanup, allow = owner()
    with pytest.raises(AudioCleanupPending):
        with retained_state_lock(tmp_path, 'conversion'):
            assert busy(tmp_path, 'conversion')
            raise AudioCleanupPending('Unconfirmed child', cleanup_owner=cleanup)
    assert busy(tmp_path, 'conversion') and not cleanup.quiescent
    allow.set()
    results = []
    worker = threading.Thread(target=lambda: results.append(cleanup.close()))
    worker.start(); worker.join(5)
    assert not worker.is_alive() and results == [True]
    assert cleanup.quiescent and not busy(tmp_path, 'conversion')


@pytest.mark.parametrize('first', [0, 1], ids=['process-first', 'output-first'])
def test_two_actual_contexts_and_broker_remain_until_every_owner_is_quiet(tmp_path, first):
    library = tmp_path / 'library'
    library.mkdir()
    broker = ResourceBroker.for_app(library)
    request = ResourceRequest(cpu_slots=1, memory_bytes=1)
    cleanups = [owner(), owner()]
    process_error = AudioCleanupPending('Child is still owned', cleanup_owner=cleanups[0][0])
    output_error = AudioCleanupPending('Native output is still owned', cleanup_owner=cleanups[1][0])
    process_error.__cause__ = output_error
    root = tmp_path / 'state'
    root.mkdir()
    with pytest.raises(OSError, match='Journal'):
        with retained_context(broker.reserve(request, operation_id='cleanup-context-test',
                                              cancelled=lambda: False, timeout=1)):
            with retained_state_lock(root, 'publication'):
                with retained_temporary_directory(prefix='.retained-', dir=root) as temporary:
                    stage = Path(temporary)
                    (stage / 'owned.wav').write_bytes(b'Exact staged data')
                    raise OSError('Journal replacement masked both pending owners') from process_error
    assert len(broker.snapshot()['leases']) == 1
    assert busy(root, 'publication') and stage.is_dir()
    cleanup, allow = cleanups[first]
    allow.set()
    assert cleanup.close() is True
    assert len(broker.snapshot()['leases']) == 1
    assert busy(root, 'publication') and stage.is_dir()
    cleanup, allow = cleanups[1 - first]
    allow.set()
    assert cleanup.close() is True
    assert broker.snapshot()['leases'] == []
    assert not busy(root, 'publication') and not stage.exists()


def test_duplicate_cyclic_exception_links_release_one_borrowed_context_once():
    cleanup, allow = owner()
    first = AudioCleanupPending('One process', cleanup_owner=cleanup)
    second = AudioCleanupPending('Same process', cleanup_owner=cleanup)
    first.__cause__, second.__context__ = second, first
    calls = []

    @contextmanager
    def existing_context():
        calls.append('entered')
        try:
            yield 'borrowed-value'
        finally:
            calls.append('released')

    with pytest.raises(AudioCleanupPending):
        with retained_context(existing_context()) as value:
            assert value == 'borrowed-value'
            raise first
    assert calls == ['entered']
    allow.set()
    assert cleanup.close() is True
    assert calls == ['entered', 'released']
    assert cleanup.close() is True
    assert calls == ['entered', 'released']


@pytest.mark.parametrize('failure', [False, True], ids=['normal-return', 'ordinary-error'])
def test_ordinary_context_completion_does_not_create_deferred_ownership(tmp_path, failure):
    stage = None
    try:
        with retained_state_lock(tmp_path):
            with retained_temporary_directory(prefix='.ordinary-', dir=tmp_path) as temporary:
                stage = Path(temporary)
                assert busy(tmp_path) and stage.is_dir()
                if failure:
                    raise ValueError('Ordinary bounded validation failure')
    except ValueError as error:
        assert failure and str(error) == 'Ordinary bounded validation failure'
    assert stage is not None and not stage.exists()
    assert not busy(tmp_path)


def test_failed_admission_does_not_release_another_native_lock(tmp_path):
    original = NativeLock(tmp_path / '.state.lock')
    assert original.acquire()
    try:
        with pytest.raises(RuntimeError, match='already being changed'):
            with retained_state_lock(tmp_path):
                pytest.fail('Another native owner was bypassed.')
        assert original.held and busy(tmp_path)
    finally:
        original.release()
    assert not busy(tmp_path)


@pytest.mark.parametrize('name', ['', '../foreign', 'a/b', r'a\b', 'a' * 65, None, True],
                         ids=['empty', 'parent', 'slash', 'backslash', 'oversized', 'none', 'bool'])
def test_lock_name_is_rejected_before_creating_state(tmp_path, name):
    root = tmp_path / 'uncreated'
    with pytest.raises(ValueError, match='lock name'):
        with retained_state_lock(root, name):
            pytest.fail('Invalid state-lock name was accepted.')
    assert not root.exists()


def test_cleanup_thread_constructor_failure_still_exposes_and_retains_original_owner(tmp_path, monkeypatch):
    import carr_library.audio.cleanup as module
    ready = threading.Event()
    released = []

    def no_thread(*args, **kwargs):
        assert kwargs['name'] == 'audio-cleanup'
        raise RuntimeError('Synthetic cleanup thread allocation failure')

    monkeypatch.setattr(module.threading, 'Thread', no_thread)
    with pytest.raises(AudioCleanupPending) as failure:
        with retained_state_lock(tmp_path, 'retained'):
            with retained_temporary_directory(prefix='.allocation-', dir=tmp_path) as temporary:
                stage = Path(temporary)
                (stage / 'owned').write_bytes(b'Unconfirmed native owner still needs this')
                raise module.pending_cleanup(ready.is_set, message='Native shutdown is unconfirmed',
                                             callbacks=(lambda: released.append(True),))
    cleanup = failure.value.cleanup_owner
    assert isinstance(cleanup.last_error, RuntimeError)
    assert 'allocation failure' in str(cleanup.last_error)
    assert stage.is_dir() and busy(tmp_path, 'retained') and not released
    assert cleanup.close() is False and stage.is_dir()
    ready.set()
    assert cleanup.close() is True and released == [True]
    assert not stage.exists() and not busy(tmp_path, 'retained')


@pytest.mark.parametrize('first', ['retained-context', 'ready-callback'])
def test_notification_fence_tracks_ready_callback_racing_retained_stage_release(tmp_path, first):
    from tempfile import TemporaryDirectory
    cleanup, allow = owner()
    entered, released = threading.Event(), threading.Event()
    ready_entered, ready_released = threading.Event(), threading.Event()
    failures, threads = [], []
    @contextmanager
    def existing_stage():
        with TemporaryDirectory(prefix='.notification-', dir=tmp_path) as temporary:
            yield Path(temporary)
            entered.set()
            if not released.wait(10):
                failures.append('Retained context release was never allowed.')
    with pytest.raises(AudioCleanupPending) as failure:
        with retained_context(existing_stage()) as stage:
            (stage / 'original').write_bytes(b'exact source bytes')
            raise AudioCleanupPending('Current native owner', cleanup_owner=cleanup)
    allow.set()
    closing = threading.Thread(target=cleanup.close)
    threads.append(closing)
    closing.start()
    try:
        assert entered.wait(10)
        assert cleanup.quiescent and not cleanup.notifications_complete_event.is_set()
        assert stage.is_dir()
        # A native observation remains nonblocking while notifications drain.
        assert cleanup.close() is True
        def ready_callback():
            ready_entered.set()
            if not ready_released.wait(10):
                failures.append('Ready callback release was never allowed.')
        adding = threading.Thread(target=lambda: cleanup.add_quiescent_callback(ready_callback))
        threads.append(adding)
        adding.start()
        assert ready_entered.wait(10)
        assert cleanup.quiescent and not cleanup.notifications_complete_event.is_set()
        if first == 'retained-context':
            released.set()
            closing.join(10)
            assert not closing.is_alive() and not stage.exists()
            assert not cleanup.notifications_complete_event.is_set()
            ready_released.set()
        else:
            ready_released.set()
            adding.join(10)
            assert not adding.is_alive() and stage.is_dir()
            assert not cleanup.notifications_complete_event.is_set()
            released.set()
        for thread in threads:
            thread.join(10)
        assert all(not thread.is_alive() for thread in threads)
        assert cleanup.notifications_complete_event.is_set() and cleanup.quiescent
        assert not stage.exists() and not failures
    finally:
        released.set()
        ready_released.set()
        for thread in threads:
            thread.join(10)


def test_registered_owner_reenters_retention_during_late_ready_notification():
    from carr_library.audio import cleanup as module
    allow, entered, released = threading.Event(), threading.Event(), threading.Event()
    failure = module.pending_cleanup(allow.is_set, message='Existing native owner')
    cleanup, failures = failure.cleanup_owner, []
    allow.set()
    assert cleanup.notifications_complete_event.wait(10)
    with module._RETAINED_LOCK:
        assert cleanup not in module._RETAINED
    def callback():
        entered.set()
        if not released.wait(10):
            failures.append('Late notification was never released.')
    worker = threading.Thread(target=lambda: cleanup.add_quiescent_callback(callback))
    worker.start()
    try:
        assert entered.wait(10)
        assert cleanup.quiescent and not cleanup.notifications_complete_event.is_set()
        with module._RETAINED_LOCK:
            assert cleanup in module._RETAINED
        assert cleanup.close() is True
        released.set()
        worker.join(10)
        assert not worker.is_alive() and not failures
        assert cleanup.notifications_complete_event.is_set()
        with module._RETAINED_LOCK:
            assert cleanup not in module._RETAINED
    finally:
        released.set()
        worker.join(10)


@pytest.mark.parametrize('error_type', [ValueError, KeyboardInterrupt, SystemExit])
def test_notification_failure_finishes_remaining_callbacks_before_original_signal(error_type):
    cleanup, allow = owner()
    error, observed = error_type('Original callback outcome'), []
    def first():
        observed.append('first')
        raise error
    cleanup.add_quiescent_callback(first)
    cleanup.add_quiescent_callback(lambda: observed.append('second'))
    allow.set()
    if error_type is ValueError:
        assert cleanup.close() is True
    else:
        with pytest.raises(error_type) as raised:
            cleanup.close()
        assert raised.value is error
    assert observed == ['first', 'second']
    assert cleanup.quiescent and cleanup.notifications_complete_event.is_set()
    assert cleanup.close() is True and observed == ['first', 'second']


def test_notification_fence_requires_native_quiescence_even_without_callbacks():
    cleanup, allow = owner()
    assert not cleanup.notifications_complete_event.is_set()
    assert cleanup.close() is False
    assert not cleanup.quiescent and not cleanup.notifications_complete_event.is_set()
    allow.set()
    assert cleanup.close() is True
    assert cleanup.quiescent and cleanup.notifications_complete_event.is_set()
    assert cleanup.close() is True


def test_ready_callback_can_register_nested_callback_outside_owner_locks():
    cleanup, allow = owner()
    observed = []
    def nested():
        assert cleanup._callback_lock.acquire(blocking=False)
        cleanup._callback_lock.release()
        observed.append('nested')
        assert not cleanup.notifications_complete_event.is_set()
    def first():
        assert cleanup._callback_lock.acquire(blocking=False)
        cleanup._callback_lock.release()
        observed.append('first')
        cleanup.add_quiescent_callback(nested)
        assert not cleanup.notifications_complete_event.is_set()
        observed.append('last')
    cleanup.add_quiescent_callback(first)
    allow.set()
    assert cleanup.close() is True
    assert observed == ['first', 'nested', 'last']
    assert cleanup.notifications_complete_event.is_set()


def test_cli_drain_waits_for_actual_context_notification_after_native_quiescence(tmp_path, monkeypatch):
    from tempfile import TemporaryDirectory
    from carr_library import translation_cli
    cleanup, allow = owner()
    entered, released, waiting, returned = (threading.Event() for _ in range(4))
    closed, errors = [], []
    @contextmanager
    def stage_context():
        with TemporaryDirectory(prefix='.cli-notification-', dir=tmp_path) as temporary:
            yield Path(temporary)
            entered.set()
            if not released.wait(10):
                errors.append('Context was not released.')
    with pytest.raises(AudioCleanupPending) as failure:
        with retained_context(stage_context()) as stage:
            (stage / 'original').write_bytes(b'exact retained original')
            raise AudioCleanupPending('Current native family', cleanup_owner=cleanup)
    allow.set()
    closing = threading.Thread(target=cleanup.close)
    closing.start()
    assert entered.wait(10) and cleanup.quiescent
    actual_wait = cleanup.notifications_complete_event.wait
    def observed_wait(timeout=None):
        waiting.set()
        return actual_wait(timeout)
    monkeypatch.setattr(cleanup.notifications_complete_event, 'wait', observed_wait)
    class Handle:
        def close(self):
            assert cleanup.quiescent and cleanup.notifications_complete_event.is_set()
            assert not stage.exists()
            closed.append(True)
    def drain():
        try:
            translation_cli._finish_native_runtime(Handle(), failure.value)
        except BaseException as error:
            errors.append(error)
        finally:
            returned.set()
    draining = threading.Thread(target=drain)
    draining.start()
    try:
        assert waiting.wait(10)
        assert not returned.is_set() and not closed and stage.exists()
        released.set()
        draining.join(10)
        closing.join(10)
        assert not draining.is_alive() and not closing.is_alive()
        assert returned.is_set() and closed == [True] and not errors and not stage.exists()
    finally:
        released.set()
        draining.join(10)
        closing.join(10)
