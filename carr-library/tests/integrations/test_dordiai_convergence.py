"""Canonical worker convergence with real processes and synthetic PCM only."""
from hashlib import sha256
import os
from pathlib import Path
import runpy
import shutil
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from carr_library.integrations import dordiai_voices as api
from carr_library import speech
from carr_library.speech import dordiai, live_adapter
from carr_library.ui.dordiai_settings import connect_runtime

# Same authored peer and canonical settings fixture, with no import-path edits.
_fixture = runpy.run_path(str(Path(__file__).with_name('test_dordiai_registry.py')))
ACTOR, runtime, requests, configured = (
    _fixture['ACTOR'], _fixture['runtime'], _fixture['requests'], _fixture['configured'])


@pytest.fixture
def provider(configured):
    value = dordiai.DordiaiProvider(configured)
    try:
        yield value
    finally:
        value.close()


def _second_provider(runtime, tmp_path):
    installed = tmp_path / 'Second synthetic installation' / 'kx47Runtime'
    shutil.copytree(runtime, installed)
    shutil.copyfile(runtime.parent / 'manifest.ini', installed.parent / 'manifest.ini')
    root = tmp_path / 'second-library'
    root.mkdir()
    connect_runtime(root, str(installed),
                    python_executable=str(Path(sys.executable).resolve()),
                    expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    return dordiai.DordiaiProvider(root), installed


def _assert_operation_released(provider):
    acquired = []
    def attempt():
        owned = provider._render_lock.acquire(blocking=False)
        acquired.append(owned)
        if owned:
            provider._render_lock.release()
    thread = Thread(target=attempt)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and acquired == [True]


def test_two_libraries_keep_stable_profiles_and_route_to_their_own_workers(provider, runtime, tmp_path):
    second, installed = _second_provider(runtime, tmp_path)
    first_registry, second_registry = speech.VoiceRegistry([provider]), speech.VoiceRegistry([second])
    try:
        assert len(first_registry.voices()) == len(second_registry.voices()) == 5
        first_voice = next(v for v in first_registry.voices() if v.language == 'uk')
        second_voice = next(v for v in second_registry.voices() if v.language == 'uk')
        # Canonical profile IDs are stable across libraries; ownership is scoped
        # by the explicitly selected registry and its saved runtime declaration.
        assert first_voice.id == second_voice.id
        first_registry.synthesize('First installation.', first_voice.id, tmp_path / 'first.wav')
        second_registry.synthesize('Second installation.', second_voice.id, tmp_path / 'second.wav')
        for current, expected in ((runtime, 'First installation.'), (installed, 'Second installation.')):
            rows = [row for row in requests(current) if row['mode'] == 'reference']
            assert [(row['profile'], row['language'], row['text']) for row in rows] == [('fixture_one', 'uk', expected)]
        assert provider.adapter._worker is not second.adapter._worker
    finally:
        first_registry.close()
        second_registry.close()

class SyntheticStream:
    """A finite PCM iterator with explicit terminal cleanup, not a voice engine."""
    def __init__(self, close):
        self.remaining = 1
        self.on_close = close
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self.remaining:
            raise StopIteration
        self.remaining -= 1
        return api.PCMChunk(b'\x01\x00' * 4)

    def close(self):
        self.closed = True
        self.on_close()


@pytest.mark.parametrize('terminal', ['failure', 'cancel', 'deadline'])
def test_terminal_stream_cleanup_happens_before_atomic_publication(provider, tmp_path, monkeypatch, terminal):
    registry = speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    clock, cancel = [0.0], Event()

    def close():
        if terminal == 'failure':
            raise RuntimeError('Synthetic terminal cleanup failed')
        if terminal == 'cancel':
            cancel.set()
        if terminal == 'deadline':
            clock[0] = 2.0

    stream = SyntheticStream(close)
    monkeypatch.setattr(dordiai, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provider.adapter, 'stream_pcm', lambda *args, **kwargs: stream)
    expected = {'failure': RuntimeError, 'cancel': InterruptedError, 'deadline': TimeoutError}[terminal]
    target = tmp_path / 'terminal.wav'
    try:
        with pytest.raises(expected):
            registry.synthesize('Synthetic text.', voice.id, target,
                                settings=speech.SpeechSettings(timeout_seconds=1),
                                cancelled=cancel.is_set)
        assert stream.closed and not target.exists()
        _assert_operation_released(provider)
        assert not list(tmp_path.glob('.speech-*'))
    finally:
        registry.close()

def test_utterance_deadline_includes_initial_model_fingerprint(provider, tmp_path, monkeypatch):
    voice = provider.voices()[0]
    clock = [0.0]
    original = provider.fingerprint
    calls = []

    def delayed(*args, **kwargs):
        if not calls:
            clock[0] = 2.0
        calls.append(True)
        return original(*args, **kwargs)

    stream = SyntheticStream(lambda: None)
    monkeypatch.setattr(dordiai, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provider, 'fingerprint', delayed)
    monkeypatch.setattr(provider.adapter, 'stream_pcm', lambda *args, **kwargs: stream)
    target = tmp_path / 'fingerprint-timeout.wav'
    with pytest.raises(TimeoutError):
        provider.render('Synthetic text.', voice, target,
                        speech.SpeechSettings(timeout_seconds=1), lambda: False)
    assert not target.exists()
    _assert_operation_released(provider)


@pytest.mark.parametrize('cancel_source', ['caller', 'library'])
def test_provider_cold_discovery_cancellation_closes_owned_child(provider, runtime, monkeypatch, cancel_source):
    assert callable(provider.discover_voices)
    helper = runtime / 'windows_runtime.py'
    marker, hold = runtime / 'provider-probe-started', runtime / 'provider-hold-probe'
    hold.touch()
    with helper.open('a', encoding='utf8') as target:
        target.write("\nfrom pathlib import Path\nfrom time import sleep\n"
                     "Path(__file__).with_name('provider-probe-started').touch()\n"
                     "while Path(__file__).with_name('provider-hold-probe').exists(): sleep(.01)\n")
    children = []
    real = api._JsonLinesProcess
    def tracked(*args, **kwargs):
        child = real(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(api, '_JsonLinesProcess', tracked)
    cancel, errors = Event(), []
    def discover():
        try:
            provider.voices(cancelled=cancel.is_set)
        except Exception as error:
            errors.append(error)
    thread = Thread(target=discover)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not marker.exists() and thread.is_alive() and monotonic() < deadline:
            sleep(.01)
        assert marker.exists() and children
        cancel.set() if cancel_source == 'caller' else dordiai.cancel_active(provider.configuration_root)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert all(child.process.poll() is not None and not child._reader.is_alive()
                   and not child._writer.is_alive() for child in children)
        _assert_operation_released(provider)
    finally:
        cancel.set()
        hold.unlink(missing_ok=True)
        thread.join(timeout=5)
        for child in children:
            child.close()


@pytest.mark.parametrize('unchanged_metadata', [False, True])
def test_model_fingerprint_can_be_cancelled_without_losing_content_checks(provider, runtime, monkeypatch,
                                                                        unchanged_metadata):
    voice = provider.voices()[0]
    resource = runtime / 'models/Holos/synthetic-resource.bin'
    resource.write_bytes(b'first model bytes')
    if unchanged_metadata:
        stamp = dordiai._stamp
        recorded = stamp(resource.stat())
        monkeypatch.setattr(dordiai, '_stamp',
                            lambda info: recorded if (info.st_dev, info.st_ino) == recorded[:2] else stamp(info))
    original = provider.fingerprint(voice)
    with pytest.raises(InterruptedError):
        provider.fingerprint(voice, cancelled=lambda: True)
    before = resource.stat()
    resource.write_bytes(b'other model bytes')
    os.utime(resource, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert provider.fingerprint(voice) != original

def test_same_metadata_model_change_restarts_only_the_changed_warm_worker(
        provider, runtime, tmp_path, monkeypatch):
    registry = speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    resource = runtime / 'models/Holos/synthetic-resource.bin'
    resource.write_bytes(b'first model bytes')
    stamp, recorded = dordiai._stamp, dordiai._stamp(resource.stat())
    monkeypatch.setattr(dordiai, '_stamp',
                        lambda info: recorded if (info.st_dev, info.st_ino) == recorded[:2] else stamp(info))
    try:
        registry.synthesize('First request.', voice.id, tmp_path / 'first.wav')
        warm = provider.adapter._worker
        assert warm is not None and warm.process.poll() is None
        registry.synthesize('Unchanged model.', voice.id, tmp_path / 'unchanged.wav')
        assert provider.adapter._worker is warm
        before = resource.stat()
        resource.write_bytes(b'other model bytes')
        os.utime(resource, ns=(before.st_atime_ns, before.st_mtime_ns))
        registry.synthesize('Changed model.', voice.id, tmp_path / 'changed.wav')
        assert provider.adapter._worker is not None and provider.adapter._worker is not warm
        assert warm.process.poll() is not None
        assert not warm._reader.is_alive() and not warm._writer.is_alive()
    finally:
        registry.close()

def test_voice_fingerprint_rejects_an_unreadable_asset_directory(provider, runtime, monkeypatch):
    voice = provider.voices()[0]
    hidden = runtime / 'models' / 'unreadable'
    hidden.mkdir(parents=True)
    (hidden / 'unlisted-model.bin').write_bytes(b'Actual retained model bytes')
    original = dordiai.os.scandir

    def unreadable(path):
        if Path(path) == hidden:
            raise PermissionError('Synthetic unreadable model inventory')
        return original(path)

    monkeypatch.setattr(dordiai.os, 'scandir', unreadable)
    with pytest.raises(PermissionError, match='unreadable model'):
        provider.fingerprint(voice)


def test_voice_fingerprint_bounds_empty_directories(provider, runtime, monkeypatch):
    voice = provider.voices()[0]
    monkeypatch.setattr(dordiai, '_MAX_ASSETS', 64)
    provider.fingerprint(voice)
    parent = runtime / 'models' / 'empty-inventory'
    parent.mkdir(parents=True)
    for number in range(65):
        (parent / str(number)).mkdir()
    with pytest.raises(ValueError, match='inventory budget'):
        provider.fingerprint(voice)


@pytest.mark.parametrize('through_registry', [False, True])
def test_voice_fingerprint_checks_cancellation_between_directory_entries(
        provider, runtime, monkeypatch, through_registry):
    registry = speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    selected = registry if through_registry else provider
    original = dordiai.os.scandir
    stop, visited = Event(), [0]

    class CountedEntries:
        def __init__(self, path):
            self.entries = original(path)

        def __enter__(self):
            self.entries.__enter__()
            return self

        def __exit__(self, *exc):
            return self.entries.__exit__(*exc)

        def __iter__(self):
            for entry in self.entries:
                visited[0] += 1
                if visited[0] == 3:
                    stop.set()
                yield entry

    monkeypatch.setattr(dordiai.os, 'scandir', CountedEntries)
    with pytest.raises(InterruptedError):
        selected.fingerprint(voice.id if through_registry else voice, cancelled=stop.is_set)
    assert visited == [3]


@pytest.mark.parametrize('cleanup_error', [OSError, KeyboardInterrupt])
def test_failed_cold_probe_cleanup_retains_exact_child_for_explicit_retry(
        provider, runtime, monkeypatch, cleanup_error):
    marker, held = runtime / 'retained-probe-started', runtime / 'retained-probe-hold'
    held.touch()
    with (runtime / 'windows_runtime.py').open('a', encoding='utf-8') as target:
        target.write('\nfrom pathlib import Path\nfrom time import sleep\n'
                     "Path(__file__).with_name('retained-probe-started').touch()\n"
                     "while Path(__file__).with_name('retained-probe-hold').exists(): sleep(.01)\n")
    original_process, original_stop = api._JsonLinesProcess, api.stop_process
    children, errors = [], []

    def tracked(*args, **kwargs):
        child = original_process(*args, **kwargs)
        children.append(child)
        return child

    def fail_stop(child):
        raise cleanup_error('Synthetic cold-probe cleanup failure')

    monkeypatch.setattr(api, '_JsonLinesProcess', tracked)
    monkeypatch.setattr(api, 'stop_process', fail_stop)
    cancel = Event()

    def discover():
        try:
            provider.voices(cancelled=cancel.is_set)
        except (Exception, KeyboardInterrupt) as exc:
            errors.append(exc)

    thread = Thread(target=discover)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not marker.exists() and thread.is_alive() and monotonic() < deadline:
            sleep(.01)
        assert marker.exists() and len(children) == 1
        child = children[0]
        assert child.process.poll() is None
        cancel.set()
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], cleanup_error)
        assert provider.adapter._probe_child is child
        assert provider.adapter._worker is None
        assert provider.adapter._inventory is None
        _assert_operation_released(provider)
        # A retry cannot silently replace the sole retained cleanup handle.
        with pytest.raises(cleanup_error, match='cold-probe cleanup'):
            provider.adapter.voices()
        assert len(children) == 1 and provider.adapter._probe_child is child
        with pytest.raises(cleanup_error, match='cold-probe cleanup'):
            provider.close()
        assert provider.adapter._probe_child is child and child.process.poll() is None

        monkeypatch.setattr(api, 'stop_process', original_stop)
        provider.close()
        assert provider.adapter._probe_child is None
        assert child.process.poll() is not None
        assert not child._reader.is_alive() and not child._writer.is_alive()
        provider.close()
    finally:
        cancel.set()
        monkeypatch.setattr(api, 'stop_process', original_stop)
        held.unlink(missing_ok=True)
        thread.join(5)
        provider.close()
        for child in children:
            child.close()


@pytest.mark.parametrize('cleanup_error', [OSError, KeyboardInterrupt])
def test_cancelled_add_provider_keeps_failed_owner_without_closing_other_worker(
        provider, runtime, tmp_path, monkeypatch, cleanup_error):
    second, second_runtime = _second_provider(runtime, tmp_path)
    registry = speech.VoiceRegistry([provider])
    original_stop = api.stop_process
    try:
        first_voice = next(voice for voice in registry.voices() if voice.language == 'uk')
        registry.synthesize('Unrelated live owner.', first_voice.id, tmp_path / 'first-owner.wav')
        first_worker = provider.adapter._worker
        second_voice = next(voice for voice in second.voices() if voice.language == 'uk')
        second.render('New owned worker.', second_voice, tmp_path / 'new-owner.wav',
                      speech.SpeechSettings(), lambda: False)
        second_worker = second.adapter._worker
        assert first_worker.process.poll() is None and second_worker.process.poll() is None

        def fail_selected(child):
            if child is second_worker.process:
                raise cleanup_error('Synthetic newly added owner cleanup failure')
            return original_stop(child)

        monkeypatch.setattr(api, 'stop_process', fail_selected)
        with pytest.raises(InterruptedError):
            registry.add_provider(second, cancelled=lambda: True)
        assert registry._providers == [provider, second]
        assert registry._cleanup_registered
        assert any('newly added owner cleanup' in notice for notice in registry.notices)
        assert second.adapter._worker is second_worker and second_worker.process.poll() is None
        assert first_worker.process.poll() is None and registry.get(first_voice.id) == first_voice

        # Explicit whole-registry shutdown must still attempt the other owner
        # and retain only the selected child whose cleanup remains unconfirmed.
        assert registry.close() is False
        assert registry._providers == [second] and registry._cleanup_registered
        assert first_worker.process.poll() is not None and second_worker.process.poll() is None

        monkeypatch.setattr(api, 'stop_process', original_stop)
        assert registry.close() is True
        assert not registry._cleanup_registered and registry._providers == []
        assert first_worker.process.poll() is not None and second_worker.process.poll() is not None
        assert not second_worker._reader.is_alive() and not second_worker._writer.is_alive()
    finally:
        monkeypatch.setattr(api, 'stop_process', original_stop)
        registry.close()
        second.close()


def test_root_inventory_budget_counts_ignored_entries_incrementally(provider, runtime, monkeypatch):
    voice = provider.voices()[0]
    monkeypatch.setattr(dordiai, '_MAX_ASSETS', 64)
    provider.fingerprint(voice)
    for number in range(65):
        (runtime / ('ignored-' + str(number) + '.log')).write_bytes(b'')
    with pytest.raises(ValueError, match='inventory budget'):
        provider.fingerprint(voice)


@pytest.mark.parametrize('operation', ['close', 'cancelled_add'])
def test_explicit_false_cleanup_retains_actual_warm_owner_for_retry(
        provider, runtime, tmp_path, monkeypatch, operation):
    registry = speech.VoiceRegistry([provider]) if operation == 'close' else speech.VoiceRegistry()
    direct = speech.VoiceRegistry([provider]) if operation == 'cancelled_add' else registry
    voice = direct.voices()[0]
    direct.synthesize('Owned warm process.', voice.id, tmp_path / 'false-owner.wav')
    worker = provider.adapter._worker
    real_close = provider.close
    monkeypatch.setattr(provider, 'close', lambda: False)
    try:
        if operation == 'close':
            assert registry.close() is False
        else:
            with pytest.raises(InterruptedError):
                registry.add_provider(provider, cancelled=lambda: True)
        assert registry._providers == [provider] and registry._cleanup_registered
        assert worker.process.poll() is None
        assert any('unconfirmed' in notice for notice in registry.notices)
        monkeypatch.setattr(provider, 'close', real_close)
        assert registry.close() is True
        assert not registry._cleanup_registered and not registry._providers
        assert worker.process.poll() is not None
        assert not worker._reader.is_alive() and not worker._writer.is_alive()
    finally:
        monkeypatch.setattr(provider, 'close', real_close)
        registry.close()
        direct.close()


@pytest.mark.parametrize('operation', ['success', 'wrong_language', 'worker_failure'])
def test_live_reader_adapter_closes_its_actual_owned_registry_on_every_exit(
        configured, tmp_path, monkeypatch, operation):
    created, children = [], []
    real_child = api._JsonLinesProcess

    def tracked(*args, **kwargs):
        child = real_child(*args, **kwargs)
        children.append(child)
        return child

    def obtain(root, cancelled, *, timeout_seconds=60):
        assert root == configured and 0 < timeout_seconds <= 60
        value = speech.VoiceRegistry([dordiai.DordiaiProvider(root)],
                                    cancelled=cancelled, timeout_seconds=timeout_seconds)
        created.append(value)
        return value

    monkeypatch.setattr(api, '_JsonLinesProcess', tracked)
    monkeypatch.setattr(live_adapter, '_registry', obtain)
    choices = live_adapter.discover_voices(configuration_root=configured)
    voice = next(v for v in choices if v.language == 'uk')
    assert created and not created[0].voices() and not created[0]._providers
    target = tmp_path / 'live-reader.wav'
    request = live_adapter.SpeechRequest(
        'fixture:error' if operation == 'worker_failure' else 'Synthetic reader request.',
        voice.id, language='ja' if operation == 'wrong_language' else 'uk')
    if operation == 'success':
        result = live_adapter.render_speech(request, target, configuration_root=configured)
        assert result.frames == 6 and sha256(target.read_bytes()).hexdigest() == result.sha256
    else:
        expected = ValueError if operation == 'wrong_language' else api.DordiaiError
        with pytest.raises(expected):
            live_adapter.render_speech(request, target, configuration_root=configured)
        assert not target.exists()
    assert len(created) == 2
    assert all(not registry.voices() and not registry._providers for registry in created)
    assert children and all(child.process.poll() is not None and not child._reader.is_alive()
                            and not child._writer.is_alive() for child in children)


@pytest.mark.parametrize('operation', ['complete', 'rejected'])
@pytest.mark.parametrize('cleanup_error', [OSError, KeyboardInterrupt])
def test_live_reader_cleanup_failure_preserves_result_or_original_error_and_retains_owner(
        provider, tmp_path, monkeypatch, capsys, operation, cleanup_error):
    registry = speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    registry.synthesize('Warm selected owner.', voice.id, tmp_path / 'reader-owned.wav')
    worker = provider.adapter._worker
    actual_stop = api.stop_process

    def blocked(child):
        if child is worker.process:
            raise cleanup_error('Synthetic live-reader cleanup failure')
        return actual_stop(child)

    monkeypatch.setattr(api, 'stop_process', blocked)
    monkeypatch.setattr(live_adapter, '_registry', lambda *a, **k: registry)
    target = tmp_path / 'reader-result.wav'
    request = live_adapter.SpeechRequest('Synthetic reader request.', voice.id,
                                        language=voice.language if operation == 'complete' else 'other-language')
    try:
        if operation == 'complete':
            result = live_adapter.render_speech(request, target)
            assert result.frames == 6 and sha256(target.read_bytes()).hexdigest() == result.sha256
        else:
            with pytest.raises(ValueError, match='language differs'):
                live_adapter.render_speech(request, target)
            assert not target.exists()
        assert registry._providers == [provider] and registry._cleanup_registered
        assert worker.process.poll() is None
        assert 'cleanup' in capsys.readouterr().err
        monkeypatch.setattr(api, 'stop_process', actual_stop)
        assert registry.close() is True and worker.process.poll() is not None
        assert not worker._reader.is_alive() and not worker._writer.is_alive()
    finally:
        monkeypatch.setattr(api, 'stop_process', actual_stop)
        registry.close()
