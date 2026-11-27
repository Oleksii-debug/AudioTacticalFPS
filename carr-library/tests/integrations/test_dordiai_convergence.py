"""Regressions for the single published provider; audio fixtures are synthetic."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import shutil
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from carr_library.integrations import dordiai_speech as bridge
from carr_library.integrations import dordiai_voices as api

_speech_spec = importlib.util.spec_from_file_location('dordiai_speech_fixtures_for_convergence',
    Path(__file__).with_name('test_dordiai_speech.py'))
_speech_fixtures = importlib.util.module_from_spec(_speech_spec)
_speech_spec.loader.exec_module(_speech_fixtures)
provider, shared_speech = _speech_fixtures.provider, _speech_fixtures.shared_speech
ACTOR, runtime = _speech_fixtures.ACTOR, _speech_fixtures.runtime
requests = _speech_fixtures._voice_fixtures.requests


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
