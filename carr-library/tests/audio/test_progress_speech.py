"""Bounded real PCM through the selected registry; native device doubles are explicit."""
from dataclasses import replace
import json
from pathlib import Path
import struct
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library import progress as p
from carr_library.audio.engines import Voice, VoiceRegistry
from carr_library.audio.progress_speech import LocalProgressSpeech, activate_progress_voice
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest


class PCMEngine:
    """Only a deterministic local fixture engine; it writes actual PCM files."""
    id = 'sapi5'

    def __init__(self, *, languages=('en',), controls=('rate', 'volume')):
        self.voice = Voice('sapi5:fixture', 'Explicit fixture voice', languages, self.id, controls=controls)
        self.calls, self.outputs = [], []
        self.frames, self.before, self.after = 2400, lambda: None, lambda: None

    def voices(self):
        return (self.voice,)

    def synthesize(self, text, output, **settings):
        self.calls.append((text, settings))
        self.outputs.append(Path(output))
        self.before()
        if settings['cancelled']():
            raise InterruptedError('Fixture synthesis stopped.')
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<h', 700) * self.frames)
        self.after()
        if settings['cancelled']():
            raise InterruptedError('Fixture synthesis stopped after writing.')
        return Path(output)


class ObservedPlayer:
    """Explicit device double; all PCM production and validation are real."""
    available = True

    def __init__(self):
        self.paths, self.samples = [], []
        self.played, self.closed = Event(), Event()
        self.fail_close, self.hold = False, False

    def play(self, path):
        self.paths.append(Path(path))
        with wave.open(str(path), 'rb') as stream:
            self.samples.append((stream.getnframes(), stream.getframerate(),
                                 stream.readframes(stream.getnframes())))
        self.played.set()

    @property
    def state(self):
        if self.fail_close:
            return 'error'
        return 'playing' if self.hold and not self.closed.is_set() else 'stopped'

    def close(self):
        if self.fail_close:
            raise RuntimeError('The fixture device still owns its buffers.')
        self.closed.set()


def selection(tmp_path, *, engine=None, player=None, **kwargs):
    engine = engine or PCMEngine()
    player = player or ObservedPlayer()
    root = tmp_path / 'library'
    root.mkdir(exist_ok=True)
    provider = LocalProgressSpeech(VoiceRegistry([engine]), engine.voice.id,
        library_root=root, _player_factory=lambda: player, **kwargs)
    return provider, engine, player


def test_actual_pcm_is_private_bounded_and_removed_only_after_device_close(tmp_path):
    provider, engine, player = selection(tmp_path)
    original = tmp_path / 'user-recording.wav'
    original.write_bytes(b'user-owned, never progress output')
    assert provider.speak_progress('In progress. 25 percent.', volume=.37, cancelled=lambda: False)
    assert len(engine.calls) == len(player.samples) == 1
    text, settings = engine.calls[0]
    assert (text, settings['voice'], settings['language'], settings['volume']) == (
        'In progress. 25 percent.', engine.voice.id, 'en', .37)
    frames, rate, samples = player.samples[0]
    assert (frames, rate) == (2400, 24000) and any(samples)
    assert player.closed.is_set() and provider.quiescent
    assert all(not path.parent.exists() for path in player.paths)
    assert original.read_bytes() == b'user-owned, never progress output'
    assert not list((provider.library_root / 'runtime/resources/leases').glob('*.json'))


@pytest.mark.parametrize('language,unknown,complete,resumed', [
    ('en', 'Total work is not yet known', 'Completed', 'Resumed'),
    ('uk', 'Загальний обсяг роботи поки невідомий', 'Завершено', 'Роботу відновлено'),
    ('sk', 'Celkový rozsah práce zatiaľ nie je známy', 'Dokončené', 'Pokračuje sa'),
])
def test_notice_uses_the_explicit_advertised_language_and_real_completion(
        tmp_path, language, unknown, complete, resumed):
    provider, _, _ = selection(tmp_path, engine=PCMEngine(languages=(language,)))
    tracker = p.ProgressTracker('Synthetic work')
    tracker.update(phase='English internal phase')
    assert unknown in provider.notification_text(tracker.snapshot())
    assert resumed in provider.notification_text(tracker.snapshot(), state='resumed')
    if language != 'en':
        assert 'English internal phase' not in provider.notification_text(tracker.snapshot())
    tracker.finish('cancelled')
    assert '100' not in provider.notification_text(tracker.snapshot())
    tracker = p.ProgressTracker('Verified work')
    tracker.finish(verified=True)
    text = provider.notification_text(tracker.snapshot())
    assert complete in text and '100' in text


@pytest.mark.parametrize('text,volume', [
    ('', .5), ('x' * 241, .5), ('line\nbreak', .5), ('notice', True),
    ('notice', float('nan')), ('notice', float('inf')), ('notice', -.1), ('notice', 1.1),
])
def test_invalid_notice_never_calls_engine(tmp_path, text, volume):
    provider, engine, player = selection(tmp_path)
    with pytest.raises(ValueError):
        provider.speak_progress(text, volume=volume, cancelled=lambda: False)
    assert engine.calls == [] and not player.played.is_set()


@pytest.mark.parametrize('change,language,message', [
    ({'languages': ('en', 'uk')}, None, 'Select an advertised'),
    ({'languages': ('en',)}, 'uk', 'Select an advertised'),
    ({'controls': ('rate',)}, 'en', 'volume control'),
])
def test_selection_refuses_missing_language_or_control_without_fallback(tmp_path, change, language, message):
    with pytest.raises(ValueError, match=message):
        selection(tmp_path, engine=PCMEngine(**change), language=language)


def test_unqualified_neural_voice_and_missing_voice_refused_before_any_work(tmp_path):
    engine = PCMEngine()
    engine.id = 'piper'
    engine.voice = replace(engine.voice, id='piper:fixture', engine='piper')
    registry = VoiceRegistry([engine])
    with pytest.raises(ValueError, match='Neural notification device placement'):
        LocalProgressSpeech(registry, engine.voice.id)
    with pytest.raises(ValueError, match='no substitute'):
        LocalProgressSpeech(registry, 'sapi5:missing')
    assert engine.calls == []


def test_selection_clone_has_independent_cancel_and_does_not_change_saved_preferences(tmp_path):
    provider, engine, _ = selection(tmp_path)
    p.ProgressSettingsStore(provider.library_root).save(p.ProgressSettings())
    before = (provider.library_root / p.FILENAME).read_bytes()
    other = provider.copy_selection()
    provider.close()
    assert not other._closed.is_set() and other.voice == provider.voice
    assert (provider.library_root / p.FILENAME).read_bytes() == before
    assert engine.calls == []


@pytest.mark.parametrize('when', ['before', 'during', 'after'])
def test_cancellation_never_starts_late_audio_and_removes_private_output(tmp_path, when):
    stop = Event()
    provider, engine, player = selection(tmp_path)
    if when == 'before':
        stop.set()
    elif when == 'during':
        engine.before = stop.set
    else:
        engine.after = stop.set
    assert provider.speak_progress('Working.', volume=.2, cancelled=stop.is_set) is False
    assert not player.played.is_set() and provider.quiescent
    assert all(not path.parent.exists() for path in engine.outputs)


@pytest.mark.parametrize('frames,message', [(480001, 'duration'), (1100000, 'byte budget')])
def test_oversized_actual_pcm_is_refused_before_device_start(tmp_path, frames, message):
    provider, engine, player = selection(tmp_path)
    engine.frames = frames
    with pytest.raises(ValueError, match=message):
        provider.speak_progress('Working.', volume=.2, cancelled=lambda: False)
    assert not player.played.is_set() and provider.quiescent
    assert all(not path.parent.exists() for path in engine.outputs)


def test_expired_synthesis_result_cannot_be_played_even_if_engine_returns(tmp_path):
    clock = [0.]
    provider, engine, player = selection(tmp_path, _clock=lambda: clock[0])
    engine.after = lambda: clock.__setitem__(0, 21.)
    # A backend may not raise on its last cancelled() check. The adapter must
    # independently recheck the synthesis deadline before native admission.
    original = engine.synthesize
    def returning_after_limit(*args, **kwargs):
        kwargs['cancelled'] = lambda: False
        return original(*args, **kwargs)
    engine.synthesize = returning_after_limit
    assert provider.speak_progress('Working.', volume=.2, cancelled=lambda: False) is False
    assert not player.played.is_set() and all(not path.parent.exists() for path in engine.outputs)


def test_final_native_admission_can_refuse_after_actual_synthesis(tmp_path):
    provider, engine, player = selection(tmp_path)
    admitted = []
    def refuse(start):
        admitted.append(start)
        return False
    assert not provider.speak_progress('Working.', volume=.2, cancelled=lambda: False, admit=refuse)
    assert len(engine.calls) == len(admitted) == 1
    assert not player.played.is_set() and all(not path.parent.exists() for path in engine.outputs)


def test_changed_voice_configuration_is_rechecked_after_synthesis(tmp_path):
    changed = Event()
    def guard():
        if changed.is_set():
            raise ValueError('Selected configuration changed.')
    provider, engine, player = selection(tmp_path, _guard=guard)
    engine.after = changed.set
    with pytest.raises(ValueError, match='configuration changed'):
        provider.speak_progress('Working.', volume=.2, cancelled=lambda: False)
    assert not player.played.is_set() and all(not path.parent.exists() for path in engine.outputs)


def test_busy_shared_cpu_drops_notice_without_waiting_for_main_operation(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    provider, engine, player = selection(tmp_path)
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='actual-main-operation', timeout=0):
        started = time.monotonic()
        assert not provider.speak_progress('Working.', volume=.2, cancelled=lambda: False)
        assert time.monotonic() - started < 2
        assert 'resources were busy' in provider.last_notice
        assert engine.calls == [] and not player.played.is_set()
    assert not list(broker.leases_root.glob('*.json'))


def test_cancel_playing_notice_confirms_device_close_before_pcm_cleanup(tmp_path):
    stop = Event()
    player = ObservedPlayer()
    player.hold = True
    provider, _, player = selection(tmp_path, player=player)
    result, failures = [], []
    def run():
        try:
            result.append(provider.speak_progress('Working.', volume=.2, cancelled=stop.is_set))
        except BaseException as exc:
            failures.append(exc)
    worker = Thread(target=run)
    worker.start()
    try:
        assert player.played.wait(3)
        assert not provider.quiescent and player.paths[0].is_file()
        stop.set()
        worker.join(3)
        assert not worker.is_alive() and not failures and result == [False]
        assert provider.quiescent and player.closed.is_set()
        assert not player.paths[0].parent.exists()
    finally:
        stop.set()
        worker.join(3)


def test_error_status_does_not_release_unknown_native_buffers_or_private_pcm(tmp_path):
    player = ObservedPlayer()
    player.fail_close = True
    provider, engine, _ = selection(tmp_path, player=player)
    try:
        with pytest.raises(RuntimeError, match='owns its buffers'):
            provider.speak_progress('Working.', volume=.2, cancelled=lambda: False)
        assert not provider.quiescent and provider._retained[0] is player
        assert player.paths[0].is_file()
        with pytest.raises(RuntimeError, match='not confirmed silence'):
            provider.speak_progress('Another notice.', volume=.2, cancelled=lambda: False)
        assert len(engine.calls) == 1
    finally:
        player.fail_close = False
        player.close()
        if provider._retained is not None:
            provider._retained[1].cleanup()


def test_actual_dispatcher_withholds_completed_notice_when_content_enters_during_synthesis(tmp_path, monkeypatch):
    provider, engine, player = selection(tmp_path)
    prepared, release, observed = Event(), Event(), Event()
    owner = object()
    def wait_for_content():
        prepared.set()
        assert release.wait(3)
    engine.after = wait_for_content
    original = provider.speak_progress
    def tracked(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            observed.set()
    provider.speak_progress = tracked
    dispatcher = p.ToneDispatcher()
    monkeypatch.setattr(p, '_dispatcher', dispatcher)
    tracker = p.ProgressTracker('Actual accepted result')
    tracker.finish(verified=True)
    before = tracker.snapshot()
    try:
        assert dispatcher.request_voice(provider, provider.notification_text(before), 20)
        assert prepared.wait(3)
        p.set_media_active(owner, True)
        release.set()
        assert observed.wait(3)
        assert not player.played.is_set()
        assert dispatcher.quiescent and tracker.snapshot() == before
    finally:
        release.set()
        p.set_media_active(owner, False)
        dispatcher.close()
        dispatcher.thread.join(3)
        assert not dispatcher.thread.is_alive()


def test_notification_observer_failure_leaves_actual_completed_operation_unchanged(tmp_path):
    provider, engine, _ = selection(tmp_path)
    failed = Event()
    def failure():
        failed.set()
        raise OSError('Synthetic notification engine failure.')
    engine.before = failure
    tracker = p.ProgressTracker('Saved result')
    tracker.finish(verified=True)
    before = tracker.snapshot()
    dispatcher = p.ToneDispatcher()
    try:
        dispatcher.request_voice(provider, provider.notification_text(before), 20)
        assert failed.wait(3)
        deadline = time.monotonic() + 3
        while not dispatcher.notice and time.monotonic() < deadline:
            time.sleep(.01)
        assert 'operation result is unchanged' in dispatcher.notice
        assert tracker.snapshot() == before and tracker.snapshot().percent == 100
    finally:
        dispatcher.close()
        dispatcher.thread.join(3)
        assert not dispatcher.thread.is_alive()


@pytest.mark.parametrize('mode,version', [('off', 1), ('beep', 1), ('speech', 2), ('both', 2)])
def test_strict_preferences_preserve_legacy_and_new_versioned_modes(tmp_path, mode, version):
    settings = p.ProgressSettings(mode, 31, 7, False, True)
    raw = p.encode_progress_settings(settings)
    assert json.loads(raw)['version'] == version
    assert p.validate_progress_settings_payload(raw) == settings
    p.ProgressSettingsStore(tmp_path).save(settings)
    assert p.ProgressSettingsStore(tmp_path).load() == settings
    payload = json.loads(raw)
    payload['voice_id'] = 'sapi5:must-not-authorize-after-restore'
    with pytest.raises(ValueError, match='Unsupported'):
        p.validate_progress_settings_payload(json.dumps(payload).encode())


def test_actual_library_backup_retains_mode_without_restoring_voice_authority(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'original')
    workspace = app.create_workspace('Synthetic voice preference backup')
    app.add_text(workspace, 'Public fixture', 'Only synthetic source bytes.')
    settings = p.ProgressSettings('both', 23, 8, True, False)
    p.ProgressSettingsStore(app.root).save(settings)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert p.ProgressSettingsStore(restored).load() == settings
    assert (restored / p.FILENAME).read_bytes() == (app.root / p.FILENAME).read_bytes()
    payload = json.loads((restored / p.FILENAME).read_bytes())
    assert set(payload) == {'kind', 'version', 'mode', 'volume', 'interval_seconds', 'muted', 'background'}
    assert 'voice' not in json.dumps(payload)
    assert not (restored / 'state/progress').exists()
