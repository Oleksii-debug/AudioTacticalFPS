"""Regressions for the single published provider; audio fixtures are synthetic."""
from hashlib import sha256
from pathlib import Path
import shutil
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from carr_library.integrations import dordiai_speech as bridge
from carr_library.integrations import dordiai_voices as api
from test_dordiai_speech import provider, shared_speech
from test_dordiai_voices import ACTOR, requests, runtime


def test_two_installations_with_same_profiles_route_to_their_own_workers(provider, shared_speech, runtime, tmp_path):
    second_runtime = tmp_path / 'Second synthetic installation' / 'kx47Runtime'
    shutil.copytree(runtime, second_runtime)
    shutil.copyfile(runtime.parent / 'manifest.ini', second_runtime.parent / 'manifest.ini')
    second = bridge.DordiaiSpeechProvider(second_runtime, python_executable=str(Path(sys.executable).resolve()),
                                          expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    registry = shared_speech.VoiceRegistry([provider, second])
    try:
        assert len(registry.voices()) == 10 and not registry.notices
        first_voices, second_voices = provider.voices(), second.voices()
        assert {v.id for v in first_voices}.isdisjoint(v.id for v in second_voices)
        assert not any(str(runtime) in v.id or str(second_runtime) in v.id for v in registry.voices())
        first_voice = next(v for v in first_voices if v.language == 'uk')
        second_voice = next(v for v in second_voices if v.language == 'uk')
        registry.synthesize('First installation.', first_voice.id, tmp_path / 'first.wav')
        registry.synthesize('Second installation.', second_voice.id, tmp_path / 'second.wav')
        for installed, expected in ((runtime, 'First installation.'), (second_runtime, 'Second installation.')):
            rows = [row for row in requests(installed) if row['mode'] == 'reference']
            assert [(row['profile'], row['language'], row['text']) for row in rows] == [('fixture_one', 'uk', expected)]
        with pytest.raises(ValueError, match='voice and language'):
            second.render('Wrong installation', first_voice, tmp_path / 'wrong.wav',
                          shared_speech.SpeechSettings(), lambda: False)
        assert not (tmp_path / 'wrong.wav').exists()
    finally:
        registry.close()
        second.close()


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
def test_terminal_stream_cleanup_happens_before_atomic_publication(provider, shared_speech, tmp_path, monkeypatch, terminal):
    voice = provider.voices()[0]
    clock = [0.0]
    cancel = Event()

    def close():
        if terminal == 'failure':
            raise RuntimeError('Synthetic terminal cleanup failed')
        if terminal == 'cancel':
            cancel.set()
        if terminal == 'deadline':
            clock[0] = 2.0

    stream = SyntheticStream(close)
    monkeypatch.setattr(bridge, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provider.adapter, 'stream_pcm', lambda *args, **kwargs: stream)
    expected = {'failure': RuntimeError, 'cancel': InterruptedError, 'deadline': TimeoutError}[terminal]
    target = tmp_path / 'terminal.wav'
    with pytest.raises(expected):
        provider.render('Synthetic text.', voice, target,
                        shared_speech.SpeechSettings(timeout_seconds=1), cancel.is_set)
    assert stream.closed and not target.exists()
    assert not provider._render_lock.locked()
    assert not list(tmp_path.glob('.speech-*'))


def test_utterance_deadline_includes_initial_model_fingerprint(provider, shared_speech, tmp_path, monkeypatch):
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
    monkeypatch.setattr(bridge, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provider, 'fingerprint', delayed)
    monkeypatch.setattr(provider.adapter, 'stream_pcm', lambda *args, **kwargs: stream)
    target = tmp_path / 'fingerprint-timeout.wav'
    with pytest.raises(TimeoutError):
        provider.render('Synthetic text.', voice, target,
                        shared_speech.SpeechSettings(timeout_seconds=1), lambda: False)
    assert not target.exists() and not provider._render_lock.locked()


@pytest.mark.parametrize('cancel_source', ['caller', 'provider'])
def test_provider_cold_discovery_cancellation_closes_owned_child(provider, runtime, monkeypatch, cancel_source):
    assert provider.cancellable_discovery is True
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
        cancel.set() if cancel_source == 'caller' else provider.cancel()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert all(child.process.poll() is not None and not child._reader.is_alive()
                   and not child._writer.is_alive() for child in children)
        assert not provider._render_lock.locked()
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
        signature = provider._signature
        recorded = signature(resource)
        monkeypatch.setattr(provider, '_signature',
                            lambda path: recorded if path == resource else signature(path))
    original = provider.fingerprint(voice.id)
    with pytest.raises(InterruptedError):
        provider.fingerprint(voice.id, cancelled=lambda: True)
    resource.write_bytes(b'other model bytes')
    assert provider.fingerprint(voice.id) != original


def test_same_metadata_model_change_restarts_only_the_changed_warm_worker(
        provider, shared_speech, runtime, tmp_path, monkeypatch):
    voice = provider.voices()[0]
    resource = runtime / 'models/Holos/synthetic-resource.bin'
    resource.write_bytes(b'first model bytes')
    signature = provider._signature
    recorded = signature(resource)
    monkeypatch.setattr(provider, '_signature',
                        lambda path: recorded if path == resource else signature(path))
    settings = shared_speech.SpeechSettings()
    provider.render('First request.', voice, tmp_path / 'first.wav', settings, lambda: False)
    warm = provider.adapter._worker
    assert warm is not None and warm.process.poll() is None
    provider.render('Unchanged model.', voice, tmp_path / 'unchanged.wav', settings, lambda: False)
    assert provider.adapter._worker is warm
    resource.write_bytes(b'other model bytes')
    provider.render('Changed model.', voice, tmp_path / 'changed.wav', settings, lambda: False)
    assert provider.adapter._worker is not None and provider.adapter._worker is not warm
    assert warm.process.poll() is not None
    assert not warm._reader.is_alive() and not warm._writer.is_alive()


def test_voice_fingerprint_rejects_an_unreadable_asset_directory(provider, runtime, monkeypatch):
    voice = provider.voices()[0]
    hidden = runtime / 'models' / 'unreadable'
    hidden.mkdir(parents=True)
    (hidden / 'unlisted-model.bin').write_bytes(b'Actual retained model bytes')
    original = bridge.os.scandir

    def unreadable(path):
        if Path(path) == hidden:
            raise PermissionError('Synthetic unreadable model inventory')
        return original(path)

    monkeypatch.setattr(bridge.os, 'scandir', unreadable)
    with pytest.raises(PermissionError, match='unreadable model'):
        provider.fingerprint(voice.id)


def test_voice_fingerprint_bounds_empty_directories(provider, runtime, monkeypatch):
    voice = provider.voices()[0]
    monkeypatch.setattr(bridge, '_MAX_ASSET_ENTRIES', 64)
    provider.fingerprint(voice.id)
    parent = runtime / 'models' / 'empty-inventory'
    parent.mkdir(parents=True)
    for number in range(65):
        (parent / str(number)).mkdir()
    with pytest.raises(ValueError, match='inventory budget'):
        provider.fingerprint(voice.id)


@pytest.mark.parametrize('through_registry', [False, True])
def test_voice_fingerprint_checks_cancellation_between_directory_entries(
        provider, shared_speech, runtime, monkeypatch, through_registry):
    registry = shared_speech.VoiceRegistry([provider])
    voice = registry.voices()[0]
    selected = registry if through_registry else provider
    original = bridge.os.scandir
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

    monkeypatch.setattr(bridge.os, 'scandir', CountedEntries)
    with pytest.raises(InterruptedError):
        selected.fingerprint(voice.id, cancelled=stop.is_set)
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
        assert not provider._render_lock.locked()
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
        provider, shared_speech, runtime, tmp_path, monkeypatch, cleanup_error):
    second_runtime = tmp_path / 'Retained second installation' / 'kx47Runtime'
    shutil.copytree(runtime, second_runtime)
    shutil.copyfile(runtime.parent / 'manifest.ini', second_runtime.parent / 'manifest.ini')
    second = bridge.DordiaiSpeechProvider(
        second_runtime, python_executable=str(Path(sys.executable).resolve()),
        expected_worker_sha256=sha256(ACTOR.encode()).hexdigest())
    registry = shared_speech.VoiceRegistry([provider])
    original_stop = api.stop_process
    try:
        first_voice = next(voice for voice in registry.voices() if voice.language == 'uk')
        registry.synthesize('Unrelated live owner.', first_voice.id, tmp_path / 'first-owner.wav')
        first_worker = provider.adapter._worker
        second_voice = next(voice for voice in second.voices() if voice.language == 'uk')
        second.render('New owned worker.', second_voice, tmp_path / 'new-owner.wav',
                      shared_speech.SpeechSettings(), lambda: False)
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
