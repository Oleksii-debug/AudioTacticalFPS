"""Canonical voice-registry/PCM coupling; device calls are explicitly doubled."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import wave

import pytest

from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech import FliteProvider, NativeSapiProvider, Voice, VoiceRegistry
from carr_library.speech.progress_speech import LocalProgressSpeech, activate_progress_voice
from carr_library.ui.operation_progress import ProgressEvent, WindowsProgressSound


class PCMProvider:
    name = 'Purpose-written PCM fixture'

    def __init__(self, *, language='en', capabilities=('volume',), seconds=.1):
        self.voice = Voice('fixture:chosen', 'Selected fixture voice', 'Fixture', language,
                           capabilities=capabilities)
        self.seconds, self.calls = seconds, []
        self.entered = self.release = None

    def voices(self):
        return (self.voice,)

    def render(self, text, voice, output, settings, cancelled):
        self.calls.append((text, voice.id, Path(output), settings))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(3), 'The test did not release synthesis'
        with wave.open(str(output), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            audio.writeframes(struct.pack('<h', 1234) * round(22050 * self.seconds))


class PrivatePlayer:
    available = True

    def __init__(self):
        self.played, self.closed, self.state = [], 0, 'stopped'
        self.started = threading.Event()
        self.keep_playing = False

    def play(self, path):
        with wave.open(str(path), 'rb') as audio:
            frames = audio.getnframes()
            raw = audio.readframes(frames)
            assert frames > 0 and any(raw)
        self.played.append((Path(path), frames, raw))
        self.state = 'playing' if self.keep_playing else 'stopped'
        self.started.set()

    def close(self):
        self.closed += 1
        self.state = 'stopped'


def selected(provider=None, *, player=None, **kwargs):
    provider, player = provider or PCMProvider(), player or PrivatePlayer()
    registry = VoiceRegistry((provider,))
    speech = LocalProgressSpeech(registry, provider.voice.id, _player_factory=lambda: player, **kwargs)
    return speech, provider, player


def test_actual_registry_writes_bounded_private_pcm_and_never_touches_user_output(tmp_path):
    source = tmp_path / 'saved-master.wav'
    source.write_bytes(b'A separately owned user output is preserved.')
    original = source.read_bytes()
    speech, provider, player = selected()
    assert speech.speak_progress('Importing files. 50 percent.', volume=.25, cancelled=lambda: False)
    text, identity, part, settings = provider.calls[0]
    assert text == 'Importing files. 50 percent.' and identity == 'fixture:chosen'
    assert settings.volume == 25 and settings.rate == 0
    assert settings.max_output_bytes == 2 * 1024 * 1024 and settings.timeout_seconds == 20
    assert player.closed == 1 and speech.quiescent
    assert len(player.played) == 1 and player.played[0][1] == 2205
    assert not player.played[0][0].exists() and not part.exists()
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


def test_actual_installed_local_voice_generates_private_progress_pcm():
    if os.name == 'nt':
        registry = VoiceRegistry((NativeSapiProvider(),))
        # This Windows acceptance must not silently skip a broken packaged SAPI.
        assert registry.voices(), 'Native local SAPI is unavailable: ' + '; '.join(registry.notices)
    else:
        executable = shutil.which('ffmpeg')
        if not executable:
            pytest.skip('No local FFmpeg; the Windows installed gate uses native SAPI.')
        registry = VoiceRegistry((FliteProvider(executable),))
        if not registry.voices():
            pytest.skip('This local FFmpeg does not expose the optional Flite provider.')
    voice = next((voice for voice in registry.voices()
                  if voice.language.replace('_', '-').split('-')[0].lower() in ('en', 'uk', 'sk')
                  and 'volume' in voice.capabilities), None)
    assert voice is not None, 'No supported installed notification language/volume capability.'
    player = PrivatePlayer()
    speech = LocalProgressSpeech(registry, voice.id, _player_factory=lambda: player)
    notice = speech.notification_text(ProgressEvent(state='running', phase='Checking files', percent=50))
    assert speech.speak_progress(notice, volume=.3, cancelled=lambda: False)
    assert player.closed == 1 and speech.quiescent
    path, frames, raw = player.played[0]
    assert frames > 100 and any(raw) and not path.exists()
    assert not path.parent.exists()


@pytest.mark.parametrize(('language', 'state', 'expected'), [
    ('en-US', 'running', 'In progress. 49 percent.'),
    ('uk-UA', 'running', 'Робота триває. 49 відсотків.'),
    ('sk-SK', 'cancelled', 'Zrušené. 49 percent.'),
])
def test_notification_language_comes_from_the_explicit_voice(language, state, expected):
    speech, _, _ = selected(PCMProvider(language=language))
    event = ProgressEvent(state=state, percent=49, phase='A visible English phase')
    message = speech.notification_text(event)
    assert message.startswith(expected) and len(message) <= 240
    if language != 'en-US':
        assert 'A visible English phase' not in message


def test_unknown_total_and_resumed_state_have_localized_honest_notices():
    speech, _, _ = selected(PCMProvider(language='uk'))
    message = speech.notification_text(ProgressEvent(state='running', phase='Inspecting'))
    assert 'поки невідомий' in message and 'відсотків' not in message
    resumed = speech.notification_text(ProgressEvent(state='running', percent=30), state='resumed')
    assert resumed.startswith('Роботу відновлено. 30')


@pytest.mark.parametrize('change', ['missing-id', 'missing-volume', 'unsupported-language'])
def test_unavailable_selection_never_substitutes_or_starts_a_provider(change):
    provider = PCMProvider(language='de' if change == 'unsupported-language' else 'en',
                           capabilities=() if change == 'missing-volume' else ('volume',))
    registry = VoiceRegistry((provider,))
    with pytest.raises(ValueError):
        LocalProgressSpeech(registry, 'absent' if change == 'missing-id' else provider.voice.id)
    assert not provider.calls


@pytest.mark.parametrize(('text', 'volume'), [('', .2), ('x' * 241, .2), ('line\nbreak', .2),
    ('Notice', True), ('Notice', -1), ('Notice', 10**1000), ('Notice', float('nan'))])
def test_invalid_notifications_do_not_invoke_the_selected_provider(text, volume):
    speech, provider, _ = selected()
    with pytest.raises(ValueError):
        speech.speak_progress(text, volume=volume, cancelled=lambda: False)
    assert provider.calls == [] and speech.quiescent


def test_cancel_during_real_registry_synthesis_never_starts_late_playback():
    provider = PCMProvider()
    provider.entered, provider.release = threading.Event(), threading.Event()
    speech, _, player = selected(provider)
    cancelled, results = threading.Event(), []
    worker = threading.Thread(target=lambda: results.append(speech.speak_progress(
        'A bounded pending notice.', volume=.2, cancelled=cancelled.is_set)))
    worker.start()
    assert provider.entered.wait(2)
    cancelled.set()
    provider.release.set()
    worker.join(3)
    assert not worker.is_alive() and results == [False]
    assert not player.played and player.closed == 1 and speech.quiescent
    assert not provider.calls[0][2].exists()


def test_cancel_after_synthesis_before_play_cannot_emit_a_late_notice():
    cancelled, calls = threading.Event(), []
    def guard():
        calls.append(True)
        if len(calls) == 2:
            cancelled.set()
    speech, _, player = selected(_guard=guard)
    assert not speech.speak_progress('Measured notice.', volume=.2, cancelled=cancelled.is_set)
    assert not player.played and player.closed == 1 and speech.quiescent


def test_cancel_owned_playback_stops_it_before_temporary_pcm_cleanup():
    player = PrivatePlayer()
    player.keep_playing = True
    speech, _, _ = selected(player=player)
    cancelled, results = threading.Event(), []
    worker = threading.Thread(target=lambda: results.append(speech.speak_progress(
        'Owned playback notice.', volume=.2, cancelled=cancelled.is_set)))
    worker.start()
    assert player.started.wait(2)
    assert not speech.quiescent
    path = player.played[0][0]
    assert path.exists()
    cancelled.set()
    worker.join(3)
    assert not worker.is_alive() and results == [False]
    assert player.closed == 1 and speech.quiescent and not path.exists()


def test_overlong_actual_pcm_is_rejected_and_cleaned_before_playback():
    speech, provider, player = selected(PCMProvider(seconds=21))
    with pytest.raises(ValueError, match='budget'):
        speech.speak_progress('Short input with excessive output.', volume=.2, cancelled=lambda: False)
    assert not player.played and speech.quiescent and player.closed == 1
    assert not provider.calls[0][2].exists()


def test_failed_owned_device_cleanup_preserves_uncertainty_and_blocks_new_audio():
    class BrokenPlayer(PrivatePlayer):
        def play(self, path):
            super().play(path)
            raise RuntimeError('Synthetic device failure after opening audio')
        def close(self):
            self.state = 'playing'
            raise RuntimeError('Synthetic device did not confirm close')
    speech, _, player = selected(player=BrokenPlayer())
    try:
        with pytest.raises(RuntimeError, match='confirm close'):
            speech.speak_progress('A synthetic device failure.', volume=.2, cancelled=lambda: False)
        assert not speech.quiescent
        with pytest.raises(RuntimeError, match='confirmed silence'):
            speech.speak_progress('No new audio.', volume=.2, cancelled=lambda: False)
        assert len(player.played) == 1
    finally:
        # The test double owns no native device; clean its deliberately retained fixture.
        if speech._retained is not None:
            speech._retained[1].cleanup()


def test_explicit_activation_reauthorizes_current_settings_and_detects_revocation(tmp_path):
    fixture = PCMProvider()
    registry, calls = VoiceRegistry((fixture,)), []
    def configured(state_root):
        calls.append(state_root)
        return registry
    speech = activate_progress_voice(tmp_path, fixture.voice.id, _registry_factory=configured)
    assert calls == [tmp_path / 'state'] and speech.library_root == tmp_path
    # A disabled local helper declaration changes approval configuration without
    # executing any helper or requiring a model download.
    helper = tmp_path / 'unused-helper.txt'
    helper.write_text('Purpose-written, never executed fixture.', encoding='utf-8')
    settings = load_settings(tmp_path / 'state')
    settings['voice_helpers'] = [{'executable': str(helper), 'enabled': False}]
    save_settings(tmp_path / 'state', settings)
    with pytest.raises(ValueError, match='settings changed'):
        speech.speak_progress('Do not use an old approval.', volume=.2, cancelled=lambda: False)
    assert not fixture.calls and len(calls) == 1


def test_copy_selection_keeps_independent_cancellation_for_two_native_views():
    speech, provider, player = selected()
    copy = speech.copy_selection()
    speech.close()
    assert not speech.speak_progress('Closed view.', volume=.2, cancelled=lambda: False)
    assert copy.speak_progress('Another explicitly authorized view.', volume=.2, cancelled=lambda: False)
    assert len(provider.calls) == 1 and player.closed == 1


def test_actual_registry_notification_runs_on_existing_feedback_worker():
    speech, provider, player = selected()
    sound = WindowsProgressSound(backend=None)
    sound.set_speech_provider(speech)
    assert sound.speak('Existing worker and registry.', volume=.2)
    sound._thread.join(3)
    assert not sound._thread.is_alive()
    assert sound.last_error is None and sound.quiescent
    assert len(provider.calls) == len(player.played) == 1
    sound.close()


class OwnedProcessProvider(PCMProvider):
    """Real owned child and registry; PCM and the playback device remain fixtures."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.process = None
        self.close_calls = 0

    def render(self, *args, **kwargs):
        if self.process is None:
            self.process = subprocess.Popen(
                [sys.executable, '-I', '-B', '-c', 'import sys; sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        super().render(*args, **kwargs)

    def close(self):
        self.close_calls += 1
        if self.process is not None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
                raise


def owned_selection(root, provider):
    registry = VoiceRegistry((provider,))
    selected = activate_progress_voice(root, provider.voice.id,
        _registry_factory=lambda _: registry)
    selected._player_factory = PrivatePlayer
    return selected, registry


@pytest.mark.parametrize('invalid', ['missing-id', 'unsupported-language', 'settings-changed'])
def test_failed_activation_releases_its_new_registry(tmp_path, invalid):
    provider = OwnedProcessProvider(language='de' if invalid == 'unsupported-language' else 'en')
    registry = VoiceRegistry((provider,))

    def configured(state_root):
        if invalid == 'settings-changed':
            values = load_settings(state_root)
            unused = tmp_path / 'unused'
            unused.write_text('Never executed.', encoding='utf-8')
            values['voice_helpers'] = [{'executable': str(unused), 'enabled': False}]
            save_settings(state_root, values)
        return registry

    try:
        with pytest.raises(ValueError):
            activate_progress_voice(tmp_path, 'absent' if invalid == 'missing-id' else provider.voice.id,
                                    _registry_factory=configured)
        assert provider.close_calls == 1
        assert registry.voices() == ()
    finally:
        registry.close()


def test_owned_selection_copies_release_real_child_only_after_final_close(tmp_path):
    provider = OwnedProcessProvider()
    selected, registry = owned_selection(tmp_path, provider)
    copied = selected.copy_selection()
    try:
        assert selected.speak_progress('First native view.', volume=.2, cancelled=lambda: False)
        child = provider.process
        assert child.poll() is None
        selected.close()
        selected.close()
        assert provider.close_calls == 0 and child.poll() is None
        assert copied.speak_progress('Second native view.', volume=.2, cancelled=lambda: False)
        copied.close()
        copied.close()
        assert child.poll() is not None and provider.close_calls == 1
        assert registry.voices() == ()
        with pytest.raises(ValueError, match='closed'):
            copied.copy_selection()
    finally:
        registry.close()


def test_final_close_during_synthesis_waits_for_worker_acknowledgement(tmp_path):
    provider = OwnedProcessProvider()
    provider.entered, provider.release = threading.Event(), threading.Event()
    selected, registry = owned_selection(tmp_path, provider)
    copied = selected.copy_selection()
    result, failures = [], []

    def synthesize():
        try:
            result.append(selected.speak_progress('Pending notification.', volume=.2,
                                                   cancelled=lambda: False))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=synthesize)
    worker.start()
    try:
        assert provider.entered.wait(2)
        child = provider.process
        selected.close()
        copied.close()
        assert provider.close_calls == 0 and child.poll() is None
        provider.release.set()
        worker.join(3)
        assert not worker.is_alive() and not failures and result == [False]
        assert child.poll() is not None and provider.close_calls == 1
    finally:
        provider.release.set()
        worker.join(3)
        registry.close()


def test_progress_voice_reselection_releases_old_owned_child(tmp_path):
    first_provider, second_provider = OwnedProcessProvider(), OwnedProcessProvider()
    first, first_registry = owned_selection(tmp_path, first_provider)
    second, second_registry = owned_selection(tmp_path, second_provider)
    sound = WindowsProgressSound(backend=None)
    try:
        sound.set_speech_provider(first)
        assert sound.speak('Old selected voice.', volume=.2)
        sound._thread.join(3)
        assert not sound._thread.is_alive() and sound.last_error is None
        child = first_provider.process
        assert child.poll() is None
        sound.set_speech_provider(second)
        assert child.poll() is not None and first_provider.close_calls == 1
        assert second_provider.close_calls == 0
        sound.close()
        assert second_provider.close_calls == 1
    finally:
        sound.close()
        first_registry.close()
        second_registry.close()


def test_direct_selection_does_not_close_a_borrowed_registry():
    provider = OwnedProcessProvider()
    registry = VoiceRegistry((provider,))
    selected = LocalProgressSpeech(registry, provider.voice.id, _player_factory=PrivatePlayer)
    try:
        selected.close()
        assert provider.close_calls == 0 and registry.voices()
    finally:
        registry.close()
