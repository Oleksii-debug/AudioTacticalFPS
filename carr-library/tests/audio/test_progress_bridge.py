"""Shared feedback + actual registry contract; device calls are explicit doubles."""
from dataclasses import replace
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.engines import FliteEngine, SAPI5Engine, Voice, VoiceRegistry
from carr_library.audio.ffmpeg import find_local_tool
from carr_library.audio.progress_bridge import (
    LocalVoiceProgressSpeech, MAX_SPOKEN_CHARACTERS, install_media_progress,
)
from carr_library.progress import ProgressHub
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferences


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('The bounded worker did not reach its expected state.')
        threading.Event().wait(.01)


def pcm(path, *, seconds=.02):
    with wave.open(str(path), 'wb') as target:
        target.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        target.writeframes(b'\x11\x00' * round(24000 * seconds))


class Engine:
    id = 'synthetic'

    def __init__(self, before=None, seconds=.02):
        self.calls, self.enumerations = [], 0
        self.before, self.seconds = before, seconds

    def voices(self):
        self.enumerations += 1
        return (Voice('synthetic:one', 'Synthetic contract fixture', ('en',), self.id),)

    def synthesize(self, text, output, **settings):
        self.calls.append((text, Path(output), settings))
        if self.before:
            self.before(settings['cancelled'])
        if settings['cancelled']():
            raise InterruptedError('Synthetic synthesis cancelled.')
        pcm(output, seconds=self.seconds)
        return output


class Player:
    available = True

    def __init__(self, *, running=False, stop_fails=False):
        self.running, self.stop_fails = running, stop_fails
        self.silence_confirmed = True
        self.played, self.closed, self.paths, self.bytes = threading.Event(), False, [], []

    def play(self, path):
        self.paths.append(Path(path))
        self.bytes.append(Path(path).read_bytes())
        self.silence_confirmed = False
        self.played.set()

    @property
    def state(self):
        return 'playing' if self.running else 'stopped'

    def close(self):
        self.closed = True
        if not self.stop_fails:
            self.silence_confirmed = True


class Players:
    def __init__(self, **options):
        self.options, self.instances = options, []

    def __call__(self):
        # First instance probes availability only; the second owns playback.
        player = Player(**self.options)
        self.instances.append(player)
        return player


def setup(*, engine=None, players=None, preferences=None):
    registry = VoiceRegistry([engine or Engine()])
    registry.voices()  # Existing Studio discovery has already happened.
    feedback = ProgressFeedback(preferences or ProgressPreferences(mode='speech'))
    frame = SimpleNamespace(progress_panel=SimpleNamespace(feedback=feedback))
    media = install_media_progress(frame)
    provider = LocalVoiceProgressSpeech(registry, voice_id='synthetic:one', language='en',
                                       media=media, _player_factory=players or Players())
    feedback.set_speech_provider(provider)
    return frame, media, provider, feedback


def test_live_media_state_changes_and_natural_completion_are_polled():
    frame = SimpleNamespace()
    media = install_media_progress(frame)
    assert install_media_progress(frame) is media
    player = SimpleNamespace(state='stopped', silence_confirmed=True)
    remove = media.register_playback(lambda: player)
    assert not frame.progress_media_busy()
    player.state, player.silence_confirmed = 'playing', False
    assert frame.progress_media_busy()
    player.state = 'paused'
    assert not frame.progress_media_busy()
    player.state, player.silence_confirmed = 'stopped', True
    assert not frame.progress_media_busy()
    remove()
    remove()
    assert not frame.progress_media_busy()


def test_existing_media_hooks_and_unknown_transport_errors_fail_quiet():
    flag = [False]
    frame = SimpleNamespace(progress_media_busy=lambda: flag[0])
    media = install_media_progress(frame)
    assert not media.media_busy()
    flag[0] = True
    assert media.media_busy()
    flag[0] = False
    media.register_playback(lambda: (_ for _ in ()).throw(OSError('Device lost')))
    assert media.media_busy()


def test_removed_transport_with_uncertain_native_cleanup_stays_quiet():
    frame = SimpleNamespace()
    media = install_media_progress(frame)
    player = SimpleNamespace(state='stopped', silence_confirmed=False)
    remove = media.register_playback(lambda: player)
    assert media.media_busy()
    remove()
    assert media.media_busy()
    lease = media.begin_recording()
    assert not lease.ready
    lease.close()


@pytest.mark.parametrize('quiet', [False, True])
def test_recording_suppression_is_unconditional_and_marked_before_suspend(quiet):
    calls = []
    feedback = SimpleNamespace(preferences=ProgressPreferences(quiet_during_media=quiet), quiescent=False)
    frame = SimpleNamespace(progress_panel=SimpleNamespace(feedback=feedback))
    media = install_media_progress(frame)
    feedback.suspend = lambda: calls.append(media.recording_requested)
    media.before_playback()
    assert len(calls) == int(quiet)
    lease = media.begin_recording()
    assert calls[-1] is True and frame.progress_recording_active()
    assert not lease.ready
    feedback.quiescent = True
    assert lease.ready
    with pytest.raises(InterruptedError):
        media._notification_started()
    lease.close()
    lease.close()
    assert not lease.ready and not frame.progress_recording_active()


def test_speech_uses_same_selected_registry_and_only_temporary_waveform():
    engine, players = Engine(), Players()
    frame, media, provider, feedback = setup(engine=engine, players=players)
    try:
        provider.speak_progress('Measured progress. ' * 40, volume=23, cancelled=lambda: False)
        assert engine.enumerations == 1
        text, path, settings = engine.calls[0]
        assert len(text) == MAX_SPOKEN_CHARACTERS
        assert settings['voice'] == 'synthetic:one'
        assert settings['volume'] == .23 and settings['rate'] == settings['pitch'] == 0
        assert players.instances[1].played.is_set() and players.instances[1].closed
        assert not path.exists() and not path.parent.exists()
        assert media.begin_recording().ready
    finally:
        feedback.close()


@pytest.mark.parametrize('invalid', ['missing:voice', 'synthetic:missing'])
def test_missing_selected_voice_never_substitutes_another(invalid):
    engine = Engine()
    registry = VoiceRegistry([engine])
    with pytest.raises(ValueError, match='available installed'):
        LocalVoiceProgressSpeech(registry, voice_id=invalid, language='en', _player_factory=Players())
    assert not engine.calls


def test_unsupported_language_and_missing_playback_do_not_advertise_voice():
    registry = VoiceRegistry([Engine()])
    with pytest.raises(ValueError, match='language'):
        LocalVoiceProgressSpeech(registry, voice_id='synthetic:one', language='uk', _player_factory=Players())
    player = Player()
    player.available = False
    with pytest.raises(ValueError, match='playback transport'):
        LocalVoiceProgressSpeech(registry, voice_id='synthetic:one', language='en', _player_factory=lambda: player)
    assert player.closed


@pytest.mark.parametrize('volume', [True, -1, 101, float('nan')])
def test_invalid_progress_volume_never_calls_engine(volume):
    engine = Engine()
    _, _, provider, feedback = setup(engine=engine)
    try:
        with pytest.raises(ValueError):
            provider.speak_progress('Progress', volume=volume, cancelled=lambda: False)
        assert not engine.calls
    finally:
        feedback.close()


def test_cancellation_after_synthesis_cannot_start_playback():
    cancel = threading.Event()
    class LateEngine(Engine):
        def synthesize(self, text, output, **settings):
            pcm(output)
            cancel.set()
    players = Players()
    _, _, provider, feedback = setup(engine=LateEngine(), players=players)
    try:
        provider.speak_progress('Progress', volume=20, cancelled=cancel.is_set)
        assert len(players.instances) == 1  # Availability probe only.
    finally:
        feedback.close()


def test_expired_cooperative_preflight_cannot_start_playback():
    now = [1.0]
    class LateEngine(Engine):
        def synthesize(self, text, output, **settings):
            pcm(output)
            now[0] = 100.0
    registry = VoiceRegistry([LateEngine()])
    players = Players()
    provider = LocalVoiceProgressSpeech(registry, voice_id='synthetic:one', language='en',
                                       _player_factory=players, _clock=lambda: now[0])
    with pytest.raises(TimeoutError, match='cooperative'):
        provider.speak_progress('Progress', volume=20, cancelled=lambda: False)
    assert len(players.instances) == 1


def test_overlong_waveform_is_rejected_before_transport():
    players = Players()
    _, _, provider, feedback = setup(engine=Engine(seconds=21), players=players)
    try:
        with pytest.raises(ValueError, match='too long'):
            provider.speak_progress('Progress', volume=20, cancelled=lambda: False)
        assert len(players.instances) == 1
    finally:
        feedback.close()


def test_actual_feedback_recording_race_cancels_running_voice_and_waits_for_stop():
    players = Players(running=True)
    _, media, provider, feedback = setup(players=players)
    task = ProgressHub().start('Measured fixture', total=10)
    try:
        feedback.update(task.snapshot())
        wait_for(lambda: len(players.instances) == 2 and players.instances[1].played.is_set())
        lease = media.begin_recording()
        assert not lease.ready
        wait_for(lambda: lease.ready)
        assert players.instances[1].closed and players.instances[1].silence_confirmed
        assert not players.instances[1].paths[0].exists()
        task.advance()
        feedback.update(task.snapshot(), media_busy=False)
        wait_for(lambda: feedback.quiescent)
        assert len(players.instances) == 2
        lease.close()
    finally:
        feedback.close()


def test_uncertain_native_stop_blocks_recording_after_feedback_thread_finishes():
    players = Players(running=True, stop_fails=True)
    _, media, _, feedback = setup(players=players)
    task = ProgressHub().start('Measured fixture', total=10)
    try:
        feedback.update(task.snapshot())
        wait_for(lambda: len(players.instances) == 2 and players.instances[1].played.is_set())
        lease = media.begin_recording()
        wait_for(lambda: feedback.quiescent)
        assert not lease.ready
        assert 'could not be confirmed stopped' in feedback.last_error
        lease.close()
    finally:
        feedback.close()


def test_recording_requested_at_audio_start_cancels_without_failure_notice(monkeypatch):
    players = Players()
    _, media, _, feedback = setup(players=players)
    original = media._notification_started
    leases = []

    def recording_starts_first():
        leases.append(media.begin_recording())
        return original()

    monkeypatch.setattr(media, '_notification_started', recording_starts_first)
    try:
        feedback.update(ProgressHub().start('Measured fixture').snapshot())
        wait_for(lambda: leases and feedback.quiescent)
        assert leases[0].ready
        assert not players.instances[1].played.is_set()
        assert feedback.last_error == ''
    finally:
        feedback.close()


def test_replacing_voice_cancels_old_worker_without_enabling_off_preferences():
    preferences = ProgressPreferences()
    players = Players(running=True)
    _, _, provider, feedback = setup(players=players, preferences=preferences)
    task = ProgressHub().start('Measured fixture')
    try:
        assert feedback.preferences == preferences and 'speech' in feedback.available_modes
        feedback.update(task.snapshot())
        assert len(players.instances) == 1
        feedback.set_preferences(replace(preferences, mode='speech'))
        task.update(phase='Actual work')
        feedback.update(task.snapshot())
        wait_for(lambda: len(players.instances) == 2 and players.instances[1].played.is_set())
        feedback.set_speech_provider(None)
        wait_for(lambda: feedback.quiescent)
        assert players.instances[1].closed
        assert 'speech' not in feedback.available_modes
    finally:
        feedback.close()


def test_actual_installed_local_voice_flows_through_adapter_without_audio_device():
    if os.name == 'nt':
        # The pinned Windows LGPL runtime intentionally has no libflite.
        # Exercise the real existing Windows speech engine, not a substitute
        # synthetic renderer or a skipped Windows synthesis requirement.
        registry = VoiceRegistry([SAPI5Engine()])
    else:
        binary = find_local_tool('ffmpeg')
        if binary is None:
            pytest.skip('An installed local FFmpeg with Flite is required for actual PCM acceptance.')
        registry = VoiceRegistry([FliteEngine(binary)])
    voices = registry.voices()
    if not voices:
        if os.name == 'nt':
            pytest.fail('Actual Windows progress speech requires an installed SAPI 5 voice.')
        pytest.skip('The installed FFmpeg does not contain Flite.')
    players = Players()
    provider = LocalVoiceProgressSpeech(registry, voice_id=voices[0].id, language=voices[0].languages[0],
                                       _player_factory=players)
    provider.speak_progress('Progress is twenty five percent.', volume=25, cancelled=lambda: False)
    player = players.instances[1]
    assert player.played.is_set() and player.closed
    assert len(player.bytes[0]) > 4800 and any(player.bytes[0][44:])
    assert not player.paths[0].exists()
