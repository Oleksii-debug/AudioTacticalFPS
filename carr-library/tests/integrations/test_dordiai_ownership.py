"""Real synthetic worker ownership across existing service and progress owners."""
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
import wave

import pytest

from carr_library import runtime_settings
from carr_library.integrations import dordiai_voices as api
from carr_library.speech import SpeechSettings
from carr_library.speech.progress_speech import LocalProgressSpeech, activate_progress_voice
from carr_library.studio.render import ProductionService
from test_dordiai_settings import configured, no_platform_voices
from test_dordiai_voices import runtime


@pytest.fixture
def selected(tmp_path, runtime, monkeypatch):
    no_platform_voices(monkeypatch)
    settings = runtime_settings.load_settings(tmp_path / 'state')
    settings['dordiai'] = configured(runtime)
    runtime_settings.save_settings(tmp_path / 'state', settings)
    return tmp_path


def warm(registry, path):
    voice = next(item for item in registry.voices() if item.language == 'uk')
    result = registry.synthesize('Synthetic retained PCM.', voice.id, path, settings=SpeechSettings())
    assert result.frames == 6
    provider = registry._owners[voice.id]
    return voice, provider.adapter._worker


def test_production_closes_its_owned_warm_registry_and_is_idempotent(selected):
    service = ProductionService(selected / 'state/studio')
    registry = service.discover_voices()
    voice, worker = warm(registry, selected / 'owned.wav')
    assert worker.process.poll() is None
    service.close()
    assert worker.process.poll() is not None and not registry.voices()
    assert not worker._reader.is_alive() and not worker._writer.is_alive()
    service.close()
    with pytest.raises(InterruptedError, match='closed'):
        service.discover_voices()


def test_production_never_closes_a_borrowed_registry_owned_by_another_view(selected):
    registry = runtime_settings.voice_registry(selected / 'state')
    service = ProductionService(selected / 'state/studio', voices=registry)
    try:
        voice, worker = warm(registry, selected / 'borrowed.wav')
        service.close()
        assert worker.process.poll() is None and registry.get(voice.id) == voice
        result = registry.synthesize('Other owner still works.', voice.id, selected / 'other-owner.wav')
        assert result.frames == 6
    finally:
        registry.close()
    assert worker.process.poll() is not None


def test_service_close_cancels_cold_discovery_and_never_adopts_late_registry(selected, runtime, monkeypatch):
    marker, held = runtime / 'ownership-probe-started', runtime / 'ownership-hold'
    held.touch()
    with (runtime / 'windows_runtime.py').open('a') as target:
        target.write('\nfrom pathlib import Path\nfrom time import sleep\n'
            "Path(__file__).with_name('ownership-probe-started').touch()\n"
            "while Path(__file__).with_name('ownership-hold').exists(): sleep(.01)\n")
    children, errors = [], []
    real = api._JsonLinesProcess
    def tracked(*args, **kwargs):
        child = real(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(api, '_JsonLinesProcess', tracked)
    service = ProductionService(selected / 'state/studio')
    def discover():
        try:
            service.discover_voices()
        except Exception as exc:
            errors.append(exc)
    thread = Thread(target=discover)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not marker.exists() and thread.is_alive() and monotonic() < deadline:
            sleep(.01)
        assert marker.exists() and children
        start = monotonic()
        service.close()
        assert monotonic() - start < .5
        thread.join(5)
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert service._voices is None
        assert all(child.process.poll() is not None and not child._reader.is_alive()
                   and not child._writer.is_alive() for child in children)
    finally:
        held.unlink(missing_ok=True)
        service.close()
        thread.join(5)
        for child in children:
            child.close()


class PrivatePlayer:
    available = True
    state = 'stopped'
    def __init__(self): self.played = []
    def play(self, path):
        with wave.open(str(path), 'rb') as stream:
            assert stream.getnframes() == 6 and any(stream.readframes(6))
        self.played.append(Path(path))
    def close(self): self.state = 'stopped'


def activated(selected):
    registry = runtime_settings.voice_registry(selected / 'state')
    voice = next(item for item in registry.voices() if item.language == 'uk')
    progress = activate_progress_voice(selected, voice.id,
        _registry_factory=lambda state, **kwargs: registry)
    player = PrivatePlayer()
    progress._player_factory = lambda: player
    return registry, progress, player


def test_progress_copies_release_only_after_last_owner_and_never_block_another_view(selected):
    registry, progress, player = activated(selected)
    copied = progress.copy_selection()
    try:
        voice, worker = warm(registry, selected / 'progress-initial.wav')
        progress.close()
        progress.close()
        assert worker.process.poll() is None and copied.registry.get(voice.id) == voice
        assert copied.speak_progress('Синтетична перевірка.', volume=.2, cancelled=lambda: False)
        assert len(player.played) == 1 and not player.played[0].exists()
        copied.close()
        assert copied._ownership.finished.wait(5)
        assert worker.process.poll() is not None and not registry.voices()
        assert copied._ownership.error is None
        with pytest.raises(ValueError, match='closed'):
            progress.copy_selection()
    finally:
        progress.close()
        copied.close()
        registry.close()


def test_direct_progress_registry_is_borrowed_and_is_not_torn_down(selected):
    registry = runtime_settings.voice_registry(selected / 'state')
    try:
        voice, worker = warm(registry, selected / 'borrowed-progress.wav')
        progress = LocalProgressSpeech(registry, voice.id)
        progress.close()
        assert worker.process.poll() is None and registry.get(voice.id) == voice
    finally:
        registry.close()


def test_progress_close_cancels_inflight_synthesis_then_releases_owned_worker_in_background(selected, runtime):
    registry, progress, player = activated(selected)
    results = []
    thread = Thread(target=lambda: results.append(progress.speak_progress(
        'fixture:delay', volume=.3, cancelled=lambda: False)))
    thread.start()
    try:
        marker = runtime / 'requests.jsonl'
        deadline = monotonic() + 5
        while not marker.exists() and thread.is_alive() and monotonic() < deadline:
            sleep(.01)
        assert marker.exists()
        worker = next(iter(registry._owners.values())).adapter._worker
        assert worker is not None and worker.process.poll() is None
        start = monotonic()
        progress.close()
        assert monotonic() - start < .5
        thread.join(5)
        assert not thread.is_alive() and results == [False] and player.played == []
        assert progress._ownership.finished.wait(5)
        assert worker.process.poll() is not None
    finally:
        progress.close()
        thread.join(5)
        registry.close()


def test_invalid_explicit_progress_selection_releases_newly_owned_registry(selected):
    registry = runtime_settings.voice_registry(selected / 'state')
    voice, worker = warm(registry, selected / 'invalid-selection.wav')
    with pytest.raises(ValueError):
        activate_progress_voice(selected, 'missing-voice', _registry_factory=lambda state, **kwargs: registry)
    assert worker.process.poll() is not None and not registry.voices()


def test_failed_termination_retains_exact_owned_handles_and_allows_cleanup_retry(selected, monkeypatch):
    service = ProductionService(selected / 'state/studio')
    registry = service.discover_voices()
    voice, worker = warm(registry, selected / 'cleanup-retry.wav')
    provider = registry._owners[voice.id]
    actual = api.stop_process
    def fail(child):
        raise OSError('Synthetic operating-system cleanup failure')
    monkeypatch.setattr(api, 'stop_process', fail)
    service.close()
    assert service._voices is registry and registry._providers == [provider]
    assert provider.adapter._worker is worker and worker.process.poll() is None
    assert registry._cleanup_registered
    monkeypatch.setattr(api, 'stop_process', actual)
    service.close()
    assert service._voices is None and registry._providers == []
    assert not registry._cleanup_registered and worker.process.poll() is not None
