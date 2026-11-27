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
