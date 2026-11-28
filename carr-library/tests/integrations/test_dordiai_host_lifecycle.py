"""Public host ownership boundaries with real synthetic Dordiai children.

No private engine, model, recording, acoustic test or external network is used.
"""
from hashlib import sha256
import gc
import os
from pathlib import Path
import sys
from threading import Event, Thread
from time import monotonic
import weakref

import pytest

from carr_library import runtime_settings
from carr_library.integrations import dordiai_voices as api
from carr_library.integrations.dordiai_speech import DordiaiSpeechProvider
from carr_library.speech import SpeechSettings, VoiceRegistry
from carr_library.studio.render import ProductionService
from .test_dordiai_voices import ACTOR, runtime
from .test_dordiai_probe_cancellation import adapter_for, probe_runtime


def provider_for(runtime):
    return DordiaiSpeechProvider(runtime, python_executable=sys.executable,
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())


def warm(registry, destination):
    voice = next(item for item in registry.voices() if item.language == 'uk')
    result = registry.synthesize('Public synthetic PCM ownership fixture.', voice.id, destination,
                                 settings=SpeechSettings())
    assert result.frames == 6
    provider = registry._owners[voice.id]
    worker = provider.adapter._worker
    assert worker is not None and worker.process.poll() is None
    return voice, provider, worker


def stopped(worker):
    assert worker.process.poll() is not None
    assert not worker._reader.is_alive() and not worker._writer.is_alive()
    assert worker._cleanup_done and not worker._cleanup_registered


def fail_stop(_process):
    raise OSError('PRIVATE-NATIVE-ERROR-MUST-NOT-REACH-REGISTRY-NOTICES')


def test_actual_warm_owner_survives_failed_close_until_explicit_retry(runtime, tmp_path, monkeypatch):
    provider = provider_for(runtime)
    registry = VoiceRegistry([provider])
    actual_stop = api.stop_process
    try:
        voice, provider, worker = warm(registry, tmp_path / 'warm.wav')
        monkeypatch.setattr(runtime_settings, 'voice_registry', lambda *_a, **_k: registry)
        service = ProductionService(tmp_path / 'library/state/studio')
        assert service.discover_voices() is registry
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        with pytest.raises(RuntimeError, match='owner is retained'):
            service.close()
        assert service._voices is registry
        assert registry._providers == [provider] and registry.voices() == ()
        assert registry._cleanup_registered
        assert provider.adapter._worker is worker and worker.process.poll() is None
        assert worker._cleanup_registered
        assert all('PRIVATE-NATIVE' not in message for message in registry.notices)
        with pytest.raises(RuntimeError, match='closed'):
            registry.reload()
        with pytest.raises(RuntimeError, match='closed'):
            registry.add_provider(provider)
        with pytest.raises(ValueError, match='available'):
            registry.synthesize('Must not reactivate.', voice.id, tmp_path / 'refused.wav')
        assert not (tmp_path / 'refused.wav').exists()
        monkeypatch.setattr(api, 'stop_process', actual_stop)
        service.close()
        service.close()
        assert service._voices is None and registry._providers == []
        assert not registry._cleanup_registered
        stopped(worker)
    finally:
        monkeypatch.setattr(api, 'stop_process', actual_stop)
        registry.close()


def test_failed_registry_is_strongly_retained_after_ordinary_owner_is_gone(runtime, tmp_path, monkeypatch):
    registry = VoiceRegistry([provider_for(runtime)])
    _, provider, worker = warm(registry, tmp_path / 'retained.wav')
    original = api.stop_process
    reference = weakref.ref(registry)
    try:
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        with pytest.raises(RuntimeError, match='owner is retained'):
            registry.close()
        del registry
        gc.collect()
        assert reference() is not None
        assert reference()._providers == [provider] and worker.process.poll() is None
        monkeypatch.setattr(api, 'stop_process', original)
        reference().close()
        stopped(worker)
        gc.collect()
        assert reference() is None
    finally:
        monkeypatch.setattr(api, 'stop_process', original)
        if reference() is not None:
            reference().close()
        provider.close()


def test_desktop_finalizer_retains_real_service_and_retries_same_native_owner(runtime, tmp_path, monkeypatch):
    from carr_library.ui.worker_lifetime import (DesktopExecutor, retained_cleanup_failures,
                                                retry_failed_cleanup)
    registry = VoiceRegistry([provider_for(runtime)])
    _, provider, worker = warm(registry, tmp_path / 'desktop.wav')
    monkeypatch.setattr(runtime_settings, 'voice_registry', lambda *_a, **_k: registry)
    service = ProductionService(tmp_path / 'library/state/studio')
    assert service.voices is registry
    executor = DesktopExecutor()
    executor.register_cleanup(service.close)
    existing = {token for token, _error in retained_cleanup_failures()}
    original = api.stop_process
    tokens = []
    try:
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        with pytest.warns(RuntimeWarning, match='owner is retained'):
            executor.shutdown(wait=True, cancel_futures=True)
        tokens = [token for token, _error in retained_cleanup_failures() if token not in existing]
        assert len(tokens) == 1 and len(executor.cleanup_failures) == 1
        assert service._voices is registry and provider.adapter._worker is worker
        assert worker.process.poll() is None
        monkeypatch.setattr(api, 'stop_process', original)
        assert retry_failed_cleanup(tokens[0]) is True
        assert retry_failed_cleanup(tokens[0]) is False
        assert not executor.cleanup_failures and service._voices is None
        stopped(worker)
    finally:
        monkeypatch.setattr(api, 'stop_process', original)
        for token in tokens:
            retry_failed_cleanup(token)
        service.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_failed_probe_cleanup_retains_actual_child_and_refuses_a_replacement(probe_runtime, monkeypatch):
    directory, _checksum = probe_runtime
    hold = directory / 'hold-probe'
    hold.touch()
    cancel = Event()
    errors, children = [], []
    original_transport, original_stop = api._JsonLinesProcess, api.stop_process

    class Observed(original_transport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setattr(api, '_JsonLinesProcess', Observed)
    adapter = adapter_for(probe_runtime)

    def work():
        try:
            adapter.voices(cancelled=cancel)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=work)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not (directory / 'probe-started').exists() and thread.is_alive() and monotonic() < deadline:
            cancel.wait(.01)
        assert (directory / 'probe-started').exists() and len(children) == 1
        child = children[0]
        assert child.process.poll() is None
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        cancel.set()
        thread.join(10)
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], OSError)
        assert adapter._probe_child is child and child.process.poll() is None
        assert adapter._inventory is None and adapter._inventory_epoch is None
        with pytest.raises(api.DordiaiUnavailable, match='retained'):
            adapter.voices()
        assert len(children) == 1
        monkeypatch.setattr(api, 'stop_process', original_stop)
        adapter.close()
        assert adapter._probe_child is None
        stopped(child)
    finally:
        cancel.set()
        monkeypatch.setattr(api, 'stop_process', original_stop)
        hold.unlink(missing_ok=True)
        thread.join(10)
        adapter.close()
        for child in children:
            child.close()


def test_generator_cleanup_failure_releases_operation_guard_but_keeps_worker(runtime, monkeypatch):
    adapter = api.DordiaiVoicesAdapter(runtime, python_executable=sys.executable,
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    original_stop = api.stop_process
    stream = adapter.stream_pcm('Synthetic interrupted stream.', 'fixture_one', 'uk')
    worker = None
    try:
        assert next(stream).frames == 3
        worker = adapter._worker
        original_send = worker.send

        def refuse_cancel(request):
            if request['mode'] == 'cancel':
                raise api.DordiaiProtocolError('Synthetic unusable request pipe.')
            return original_send(request)

        monkeypatch.setattr(worker, 'send', refuse_cancel)
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        with pytest.raises(OSError):
            stream.close()
        assert adapter._worker is worker and worker.process.poll() is None
        assert adapter._active_cancel is None and not adapter._operation.locked()
        with pytest.raises(api.DordiaiUnavailable, match='retained'):
            adapter.voices()
        monkeypatch.setattr(api, 'stop_process', original_stop)
        adapter.close()
        assert adapter._worker is None
        stopped(worker)
    finally:
        monkeypatch.setattr(api, 'stop_process', original_stop)
        stream.close()
        adapter.close()


def test_failed_thread_start_retains_transport_when_native_stop_also_fails(tmp_path, monkeypatch):
    original_thread, original_stop = api.Thread, api.stop_process
    registered = []
    actual_register = api.atexit.register

    def record(callback):
        registered.append(callback)
        return actual_register(callback)

    class FailingWriter(original_thread):
        def start(self):
            if self.name == 'CARR dordiai requests':
                raise RuntimeError('Synthetic writer thread startup failure.')
            return super().start()

    monkeypatch.setattr(api, 'Thread', FailingWriter)
    monkeypatch.setattr(api.atexit, 'register', record)
    monkeypatch.setattr(api, 'stop_process', fail_stop)
    child = None
    try:
        with pytest.raises(OSError):
            api._JsonLinesProcess([sys.executable, '-I', '-B', '-u', '-c',
                                   'import sys; sys.stdin.buffer.read()'], tmp_path)
        assert len(registered) == 1
        child = registered[0].__self__
        assert child.process.poll() is None and child._cleanup_registered
        monkeypatch.setattr(api, 'stop_process', original_stop)
        child.close()
        child.close()
        stopped(child)
    finally:
        monkeypatch.setattr(api, 'stop_process', original_stop)
        if child is not None:
            child.close()


@pytest.mark.parametrize('failure', [PermissionError, FileNotFoundError])
def test_actual_unreadable_asset_subtree_refuses_fingerprint_and_render(runtime, tmp_path, monkeypatch, failure):
    hidden = runtime / 'models/Hidden'
    hidden.mkdir()
    (hidden / 'model.fixture').write_bytes(b'Synthetic local bytes.')
    provider = provider_for(runtime)
    registry = VoiceRegistry([provider])
    voice = next(item for item in registry.voices() if item.language == 'uk')
    before = provider.fingerprint(voice.id)
    revision = provider._installation_revision
    original_scandir = os.scandir

    def blocked(path):
        if isinstance(path, int):
            return original_scandir(path)  # Preserve fd-based TemporaryDirectory cleanup.
        if Path(path) == hidden:
            raise failure('PRIVATE-ASSET-PATH')
        return original_scandir(path)

    try:
        with monkeypatch.context() as context:
            context.setattr(os, 'scandir', blocked)
            with pytest.raises(ValueError, match='could not be read completely') as error:
                provider.fingerprint(voice.id)
            assert 'PRIVATE-ASSET' not in str(error.value)
            assert provider._installation_revision == revision
            with pytest.raises(ValueError, match='could not be read completely'):
                registry.synthesize('Cannot use a partial inventory.', voice.id, tmp_path / 'refused.wav')
            assert not (tmp_path / 'refused.wav').exists()
            assert not tuple(tmp_path.glob('.speech-*')), 'Refused synthesis left owned staging behind.'
            assert not (runtime / 'requests.jsonl').exists()
        assert provider.fingerprint(voice.id) == before
    finally:
        registry.close()


def test_inventory_walk_error_preserves_cancellation_without_updating_identity(runtime, monkeypatch):
    hidden = runtime / 'models/Hidden'
    hidden.mkdir()
    provider = provider_for(runtime)
    registry = VoiceRegistry([provider])
    voice = registry.voices()[0]
    provider.fingerprint(voice.id)
    revision = provider._installation_revision
    original_scandir, cancel = os.scandir, Event()

    def blocked(path):
        if isinstance(path, int):
            return original_scandir(path)  # Preserve fd-based TemporaryDirectory cleanup.
        if Path(path) == hidden:
            cancel.set()
            raise PermissionError('Synthetic denied subtree.')
        return original_scandir(path)

    try:
        with monkeypatch.context() as context:
            context.setattr(os, 'scandir', blocked)
            with pytest.raises(InterruptedError):
                provider.fingerprint(voice.id, cancelled=cancel.is_set)
            assert provider._installation_revision == revision
    finally:
        registry.close()


def test_failed_add_cleanup_keeps_actual_started_provider_without_reactivation(runtime, tmp_path, monkeypatch):
    class QuietProvider:
        name = 'Other synthetic provider'
        def __init__(self): self.closes = 0
        def voices(self): return ()
        def close(self): self.closes += 1

    quiet = QuietProvider()
    registry = VoiceRegistry([quiet])
    provider = provider_for(runtime)
    voice = next(item for item in provider.voices() if item.language == 'uk')
    provider.render('Owned before cancelled registration.', voice, tmp_path / 'before-add.wav',
                    SpeechSettings(), lambda: False)
    worker = provider.adapter._worker
    before = (runtime / 'requests.jsonl').read_bytes()
    original_stop = api.stop_process
    try:
        monkeypatch.setattr(api, 'stop_process', fail_stop)
        with pytest.raises(RuntimeError, match='owner is retained'):
            registry.add_provider(provider, cancelled=lambda: True)
        assert registry._providers == [quiet, provider] and registry.voices() == ()
        assert registry._cleanup_registered and worker.process.poll() is None
        with pytest.raises(RuntimeError, match='closed'):
            registry.reload()
        assert (runtime / 'requests.jsonl').read_bytes() == before
        monkeypatch.setattr(api, 'stop_process', original_stop)
        registry.close()
        registry.close()
        assert quiet.closes == 1 and registry._providers == []
        stopped(worker)
    finally:
        monkeypatch.setattr(api, 'stop_process', original_stop)
        registry.close()
        provider.close()


def test_explicit_unconfirmed_close_result_keeps_only_failed_provider():
    class Provider:
        name = 'Synthetic cleanup owner'
        def __init__(self, result): self.result, self.closes = result, 0
        def voices(self): return ()
        def close(self):
            self.closes += 1
            return self.result

    failed, succeeded = Provider(False), Provider(None)
    registry = VoiceRegistry([failed, succeeded])
    try:
        with pytest.raises(RuntimeError, match='owner is retained'):
            registry.close()
        assert registry._providers == [failed] and registry._cleanup_registered
        failed.result = None
        registry.close()
        registry.close()
        assert (failed.closes, succeeded.closes) == (2, 1)
        assert registry._providers == [] and not registry._cleanup_registered
    finally:
        failed.result = None
        registry.close()


@pytest.mark.parametrize('operation', ['reload', 'add'])
def test_concurrent_close_refuses_late_discovery_publication_without_double_close(tmp_path, operation):
    from carr_library.speech import Voice
    entered, release = Event(), Event()
    class BlockedProvider:
        name = 'Synthetic delayed inventory'
        def __init__(self): self.blocked, self.closes = False, 0
        def voices(self):
            if self.blocked:
                entered.set()
                assert release.wait(10), 'The controlled discovery was not released.'
            return (Voice('late', 'Late synthetic voice', 'synthetic', 'uk'),)
        def close(self): self.closes += 1

    provider = BlockedProvider()
    registry = VoiceRegistry([provider] if operation == 'reload' else [])
    provider.blocked = True
    errors = []
    def discover():
        try:
            if operation == 'reload':
                registry.reload()
            else:
                registry.add_provider(provider)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=discover)
    thread.start()
    try:
        assert entered.wait(10)
        registry.close()
        assert thread.is_alive(), 'Close must not wait on the blocked provider inventory.'
        assert registry.voices() == () and registry._providers == []
        with pytest.raises(ValueError, match='available'):
            registry.get('late')
        with pytest.raises(ValueError, match='available'):
            registry.synthesize('Late work must stay closed.', 'late', tmp_path / 'never.wav')
        assert not (tmp_path / 'never.wav').exists()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert 'closed' in str(errors[0])
        assert registry.voices() == () and registry._owners == {} and registry._providers == []
        registry.close()
        assert provider.closes == 1 and not registry._cleanup_registered
    finally:
        release.set()
        thread.join(10)
        registry.close()
