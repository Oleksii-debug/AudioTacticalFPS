"""Canonical voice-registry/PCM coupling; device calls are explicitly doubled."""
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave

import pytest

from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech import FliteProvider, NativeSapiProvider, Voice, VoiceRegistry
from carr_library.speech.runtime import LocalHelperProvider
from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceCancelled, ResourceRequest
from carr_library.speech.progress_speech import LocalProgressSpeech, activate_progress_voice
from carr_library.ui.operation_progress import ProgressEvent, WindowsProgressSound


_PROGRESS_LIBRARY_ROOT = None


@pytest.fixture(autouse=True)
def _progress_library(tmp_path_factory, monkeypatch):
    # These declared synthetic providers are heavy extension routes. Give every
    # test an actual separate library policy; do not claim a native exemption.
    root = tmp_path_factory.mktemp('progress-local-library')
    monkeypatch.setitem(globals(), '_PROGRESS_LIBRARY_ROOT', root)


class PCMProvider:
    name = 'Purpose-written PCM fixture'

    def __init__(self, *, language='en', capabilities=('volume',), seconds=.1):
        self.voice = Voice('fixture:chosen', 'Selected fixture voice', 'Fixture', language,
                           capabilities=capabilities)
        self.seconds, self.calls = seconds, []
        self.entered = self.release = None

    def voices(self):
        return (self.voice,)

    def fingerprint(self, voice_id):
        assert voice_id == self.voice.id
        return sha256(Path(__file__).read_bytes()).hexdigest()

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
    kwargs.setdefault('library_root', _PROGRESS_LIBRARY_ROOT)
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
    cancelled, calls, completed_pcm = threading.Event(), [], []
    def guard():
        calls.append(True)
        if provider.calls:  # The exact post-synthesis guard, after admission checks.
            output = provider.calls[0][2].parent.parent / 'notification.wav'
            with wave.open(str(output), 'rb') as audio:
                frames = audio.getnframes()
                completed_pcm.append((frames, any(audio.readframes(frames))))
            cancelled.set()
    speech, provider, player = selected(_guard=guard)
    assert not speech.speak_progress('Measured notice.', volume=.2, cancelled=cancelled.is_set)
    assert len(provider.calls) == 1 and completed_pcm == [(2205, True)]
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
    settings['voice_helpers'] = [{'executable': str(helper), 'enabled': False,
        'sha256': sha256(helper.read_bytes()).hexdigest(), 'engine_name': 'Disabled fixture'}]
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


class ClosingPCMProvider(PCMProvider):
    """Real private PCM with observable worker lifetime; never an installed voice."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = threading.Event()
        self.close_count = self.discoveries = 0
        self.rendering = self.closed_during_render = False

    def voices(self):
        self.discoveries += 1
        return super().voices()

    def render(self, *args):
        self.rendering = True
        try:
            return super().render(*args)
        finally:
            self.rendering = False

    def close(self):
        self.closed_during_render |= self.rendering
        self.close_count += 1
        self.closed.set()


def test_borrowed_progress_registry_survives_all_selection_closes():
    engine = ClosingPCMProvider()
    registry = VoiceRegistry((engine,))
    first = LocalProgressSpeech(registry, engine.voice.id)
    second = first.copy_selection()
    try:
        first.close()
        second.close()
        first.close()
        assert registry.get(engine.voice.id) == engine.voice
        assert engine.close_count == 0
        with pytest.raises(RuntimeError, match='closed'):
            second.copy_selection()
    finally:
        registry.close()
        registry.close()
    assert engine.close_count == 1


def test_owned_progress_registry_closes_once_after_last_copy_and_real_speech(tmp_path):
    engine, player = ClosingPCMProvider(), PrivatePlayer()
    engine.entered, engine.release = threading.Event(), threading.Event()
    registry = VoiceRegistry((engine,))
    first = activate_progress_voice(tmp_path, engine.voice.id, _registry_factory=lambda root: registry)
    first._player_factory = lambda: player
    second = first.copy_selection()
    results, errors = [], []
    def speak():
        try:
            results.append(second.speak_progress('The surviving view.', volume=.25, cancelled=lambda: False))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=speak)
    try:
        worker.start()
        assert engine.entered.wait(2)
        first.close()
        first.close()
        assert not second._closed.is_set() and engine.close_count == 0
        engine.release.set()
        worker.join(3)
        assert not worker.is_alive() and results == [True] and errors == []
        assert len(player.played) == 1 and engine.discoveries == 1
        assert engine.close_count == 0
        second.close()
        second.close()
        assert engine.closed.wait(2)
        assert engine.close_count == 1 and not engine.closed_during_render
        assert registry.voices() == ()
    finally:
        engine.release.set()
        worker.join(3)
        first.close()
        second.close()
        registry.close()


def test_last_owned_selection_close_defers_registry_cleanup_until_active_synthesis_ends(tmp_path):
    engine, player = ClosingPCMProvider(), PrivatePlayer()
    engine.entered, engine.release = threading.Event(), threading.Event()
    registry = VoiceRegistry((engine,))
    speech = activate_progress_voice(tmp_path, engine.voice.id, _registry_factory=lambda root: registry)
    speech._player_factory = lambda: player
    results, errors = [], []
    def speak():
        try:
            results.append(speech.speak_progress('Cancelled owned selection.', volume=.2, cancelled=lambda: False))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=speak)
    try:
        worker.start()
        assert engine.entered.wait(2)
        speech.close()
        assert speech._closed.is_set() and engine.close_count == 0
        engine.release.set()
        worker.join(3)
        assert not worker.is_alive() and results == [False] and errors == []
        assert engine.closed.wait(2)
        assert engine.close_count == 1 and not engine.closed_during_render
        assert not player.played and not engine.calls[0][2].exists()
    finally:
        engine.release.set()
        worker.join(3)
        speech.close()
        registry.close()


def test_rejected_owned_selection_closes_its_fresh_registry(tmp_path):
    engine = ClosingPCMProvider()
    registry = VoiceRegistry((engine,))
    with pytest.raises(ValueError, match='available'):
        activate_progress_voice(tmp_path, 'missing', _registry_factory=lambda root: registry)
    assert engine.closed.wait(2)
    assert engine.close_count == 1 and engine.calls == []


def test_failed_copy_releases_only_its_temporary_registry_reference(tmp_path):
    engine = ClosingPCMProvider()
    registry = VoiceRegistry((engine,))
    first = activate_progress_voice(tmp_path, engine.voice.id, _registry_factory=lambda root: registry)
    registry._voices.clear()
    try:
        with pytest.raises(ValueError, match='available'):
            first.copy_selection()
        assert engine.close_count == 0
    finally:
        first.close()
    assert engine.closed.wait(2)
    assert engine.close_count == 1


def test_registry_close_releases_failed_discovery_and_other_providers_once():
    good, bad = ClosingPCMProvider(), ClosingPCMProvider()
    bad.name = 'Failed discovery fixture'
    def failed_voices():
        raise RuntimeError('Synthetic unavailable worker')
    bad.voices = failed_voices
    registry = VoiceRegistry((bad, good))
    assert registry.get(good.voice.id) == good.voice
    registry.close()
    registry.close()
    assert good.close_count == bad.close_count == 1


def test_fatal_configured_provider_failure_still_closes_owned_constructor_resources():
    good, bad = ClosingPCMProvider(), ClosingPCMProvider()
    bad.failure_is_fatal = True
    def failed_voices():
        raise ValueError('Synthetic restored registration is not revalidated')
    bad.voices = failed_voices
    with pytest.raises(ValueError, match='not revalidated'):
        VoiceRegistry((good, bad))
    assert good.close_count == bad.close_count == 1


def test_real_configured_factory_revocation_closes_previously_discovered_registry(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    from carr_library.durable_state import atomic_json
    import carr_library.speech.runtime as speech_runtime

    helper = tmp_path / 'reviewed-helper.txt'
    helper.write_bytes(b'Reviewed synthetic declaration; this test must never execute it.')
    settings = load_settings(tmp_path / 'state')
    settings['voice_helpers'] = [{'executable': str(helper), 'enabled': True,
        'sha256': sha256(helper.read_bytes()).hexdigest(), 'engine_name': 'Revoked fixture'}]
    path = runtime_settings.settings_path(tmp_path / 'state')
    atomic_json(path, settings)
    engine = ClosingPCMProvider()
    registry = VoiceRegistry((engine,))
    def discovered(**kwargs):
        # The real helper constructor has already validated its approved hash.
        # Revoke the bytes before add_provider triggers its second validation.
        helper.write_bytes(b'Changed after initial configured-provider admission.')
        return registry
    monkeypatch.setattr(VoiceRegistry, 'discover', discovered)
    monkeypatch.setattr(speech_runtime, 'run_process',
        lambda *args, **kwargs: pytest.fail('A revoked helper reached executable dispatch'))
    with pytest.raises(ValueError, match='hash verification'):
        runtime_settings.voice_registry(tmp_path / 'state')
    assert engine.close_count == 1 and engine.closed.is_set()
    assert registry.voices() == () and registry._providers == []


def test_borrowed_add_provider_failure_does_not_close_the_callers_registry():
    engine, bad = ClosingPCMProvider(), ClosingPCMProvider()
    registry = VoiceRegistry((engine,))
    bad.failure_is_fatal = True
    def revoked():
        raise ValueError('Synthetic revoked registration')
    bad.voices = revoked
    try:
        with pytest.raises(ValueError, match='revoked'):
            registry.add_provider(bad)
        assert engine.close_count == 0 and registry.get(engine.voice.id) == engine.voice
    finally:
        registry.close()
    assert engine.close_count == bad.close_count == 1


def test_progress_model_reserves_actual_shared_resources_only_while_synthesizing():
    from carr_library.speech.synthesis_admission import PROTOCOL_SCRATCH_BYTES
    speech, provider, player = selected()
    broker = ResourceBroker.for_app(speech.library_root)
    observations = []
    render = provider.render
    def admitted(*args):
        observations.append(broker.snapshot())
        return render(*args)
    provider.render = admitted
    original_play = player.play
    def play(path):
        assert not broker.snapshot()['leases']
        return original_play(path)
    player.play = play
    assert speech.speak_progress('A reserved local notification.', volume=.2, cancelled=lambda: False)
    assert len(observations) == 1 and len(observations[0]['leases']) == 1
    record = observations[0]['leases'][0]
    output_parent = provider.calls[0][2].parent.parent
    assert record['operation_id'].startswith('reader.synthesis.')
    assert record['request'] == ResourceRequest(
        cpu_slots=broker.limits.cpu_slots, memory_bytes=broker.limits.memory_bytes,
        disk_bytes=4 * 1024 * 1024 + PROTOCOL_SCRATCH_BYTES,
        disk_path=output_parent).to_dict()
    assert record['request']['gpu_device'] is None
    assert record['request']['gpu_memory_bytes'] == 0
    assert not broker.snapshot()['leases'] and not speech._run_lock.locked()
    assert not output_parent.exists() and len(player.played) == 1


def test_exact_native_progress_route_remains_available_while_heavy_policy_is_occupied():
    # Only native rendering is doubled here. Existing native acceptance above
    # still exercises the actual installed SAPI/Flite implementation.
    pcm = PCMProvider()
    provider = object.__new__(FliteProvider)
    provider.voices = pcm.voices
    provider.render = pcm.render
    registry, player = VoiceRegistry([provider]), PrivatePlayer()
    broker = ResourceBroker.for_app(_PROGRESS_LIBRARY_ROOT)
    speech = LocalProgressSpeech(registry, pcm.voice.id,
        library_root=_PROGRESS_LIBRARY_ROOT, _player_factory=lambda: player)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                            operation_id='fixture.heavy-work'):
            assert speech.speak_progress('Basic selected progress stays available.',
                                         volume=.2, cancelled=lambda: False)
            assert len(broker.snapshot()['leases']) == 1
        assert len(pcm.calls) == len(player.played) == 1
        assert not broker.snapshot()['leases'] and not speech._run_lock.locked()
    finally:
        registry.close()


def test_progress_heavy_route_without_current_library_cannot_borrow_a_native_exemption():
    speech, provider, player = selected(library_root=None)
    with pytest.raises(ValueError, match='current library'):
        speech.speak_progress('No invented resource policy.', volume=.2, cancelled=lambda: False)
    assert not provider.calls and not player.played and player.closed == 1
    assert not speech._run_lock.locked()


@pytest.mark.parametrize('stop_kind', ['explicit-cancel', 'whole-notice-deadline'])
def test_progress_real_resource_wait_is_cancelled_within_original_notification_budget(
        monkeypatch, stop_kind):
    from carr_library.speech import progress_speech
    clock = [0.0]
    speech, provider, player = selected(_clock=lambda: clock[0])
    broker = ResourceBroker.for_app(speech.library_root)
    waiting, cancelled, observed, folders = threading.Event(), threading.Event(), [], []
    original_reason = ResourceBroker._reason
    def reason(self, *args, **kwargs):
        value = original_reason(self, *args, **kwargs)
        if value is not None:
            waiting.set()
        return value
    monkeypatch.setattr(ResourceBroker, '_reason', reason)
    original_temp = progress_speech.TemporaryDirectory
    def temporary(*args, **kwargs):
        directory = original_temp(*args, **kwargs)
        folders.append(Path(directory.name))
        clock[0] = 7.0  # Time already spent within the original 30-second notice.
        return directory
    monkeypatch.setattr(progress_speech, 'TemporaryDirectory', temporary)
    original_run = progress_speech.ReaderSynthesisRequest.run
    def run(self, *args, **kwargs):
        observed.append(kwargs['admission_timeout'])
        return original_run(self, *args, **kwargs)
    monkeypatch.setattr(progress_speech.ReaderSynthesisRequest, 'run', run)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                            operation_id='fixture.independent-heavy-work'):
            future = workers.submit(speech.speak_progress, 'A bounded queued notice.',
                                    volume=.2, cancelled=cancelled.is_set)
            try:
                assert waiting.wait(5), 'The actual shared resource queue was not reached.'
                assert not provider.calls and not player.played
                assert len(folders) == 1 and folders[0].is_dir()
                assert observed == [23.0]
                if stop_kind == 'explicit-cancel':
                    cancelled.set()
                    assert future.result(timeout=5) is False
                else:
                    clock[0] = 30.0
                    with pytest.raises((InterruptedError, ResourceCancelled)):
                        future.result(timeout=5)
                assert len(broker.snapshot()['leases']) == 1
                assert not provider.calls and not player.played
                assert player.closed == 1 and not folders[0].exists()
                assert not speech._run_lock.locked()
            finally:
                cancelled.set()
    assert not broker.snapshot()['leases']


def test_progress_rechecks_same_settings_after_real_admission_without_gui_callback(monkeypatch):
    state, callback_threads = [True], []
    def guard():
        callback_threads.append(threading.get_ident())
        if not state[0]:
            raise ValueError('The selected settings were revoked during the queue.')
    speech, provider, player = selected(_guard=guard)
    broker = ResourceBroker.for_app(speech.library_root)
    waiting, cancelled = threading.Event(), threading.Event()
    original = ResourceBroker._reason
    def reason(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if value is not None:
            waiting.set()
        return value
    monkeypatch.setattr(ResourceBroker, '_reason', reason)
    main_thread = threading.get_ident()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                            operation_id='fixture.earlier-work'):
            future = workers.submit(speech.speak_progress, 'Only the reviewed selection.',
                                    volume=.2, cancelled=cancelled.is_set)
            assert waiting.wait(5)
            state[0] = False
        try:
            with pytest.raises(ValueError, match='revoked during the queue'):
                future.result(timeout=5)
        finally:
            cancelled.set()
    assert callback_threads and all(value != main_thread for value in callback_threads)
    assert not provider.calls and not player.played and player.closed == 1
    assert not broker.snapshot()['leases'] and not speech._run_lock.locked()


@pytest.mark.parametrize('cancel_render', [False, True])
def test_real_progress_helper_holds_shared_lease_until_actual_owned_child_exit(
        tmp_path, monkeypatch, cancel_render):
    script, ready, release = (tmp_path / 'helper.py', tmp_path / 'helper-ready.json',
                              tmp_path / 'helper-release')
    script.write_text(
        'import json,os,sys,time,wave\nfrom pathlib import Path\n'
        'request=json.load(sys.stdin)\n'
        'if request["operation"]=="voices":\n'
        ' print(json.dumps({"schema":"carr.local-voice.v1","voices":['
        '{"id":"one","name":"Explicit synthetic helper","language":"en",'
        '"capabilities":["volume"],"license":"Purpose-written test fixture"}]}))\n'
        'else:\n'
        ' marker=Path(' + repr(str(ready)) + ')\n'
        ' temporary=marker.with_name(marker.name+".pending")\n'
        ' temporary.write_text(json.dumps({"pid":os.getpid(),"output":request["output"],'
        '"settings":request["settings"]}),encoding="utf-8")\n'
        ' os.replace(temporary,marker)\n'
        ' release=Path(' + repr(str(release)) + ')\n'
        ' deadline=time.monotonic()+20\n'
        ' while not release.exists():\n'
        '  if time.monotonic()>=deadline: raise RuntimeError("Fixture release missing")\n'
        '  time.sleep(.01)\n'
        ' with wave.open(request["output"],"wb") as audio:\n'
        '  audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)\n'
        '  audio.writeframes(b"\\x10\\x01"*400)\n'
        ' print(json.dumps({"schema":"carr.local-voice.v1","status":"rendered"}))\n',
        encoding='utf-8')
    executable = Path(sys.executable).resolve()
    provider = LocalHelperProvider(executable,
        sha256=sha256(executable.read_bytes()).hexdigest(), enabled=True,
        engine_name='synthetic progress helper', script=script,
        script_sha256=sha256(script.read_bytes()).hexdigest())
    registry = VoiceRegistry([provider])
    voice, player = registry.voices()[0], PrivatePlayer()
    speech = LocalProgressSpeech(registry, voice.id, library_root=_PROGRESS_LIBRARY_ROOT,
                                 _player_factory=lambda: player)
    broker, cancelled = ResourceBroker.for_app(_PROGRESS_LIBRARY_ROOT), threading.Event()
    original, children, exited = process.owned_process, [], []
    @contextmanager
    def owned(*args, **kwargs):
        child = None
        try:
            with original(*args, **kwargs) as child:
                children.append(child)
                assert len(broker.snapshot()['leases']) == 1
                yield child
        except BaseException as error:
            if child is not None and not _pending_owners(error):
                exited.append(child)
            raise
        else:
            exited.append(child)
    monkeypatch.setattr(process, 'owned_process', owned)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(speech.speak_progress, 'Literal private helper notice.',
                                    volume=.2, cancelled=cancelled.is_set)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and not future.done():
                    assert time.monotonic() < deadline, 'The actual helper did not reach its render.'
                    time.sleep(.01)
                marker = json.loads(ready.read_text(encoding='utf-8'))
                assert marker['pid'] > 0 and marker['settings']['timeout_seconds'] == 20
                assert marker['settings']['max_output_bytes'] == 2 * 1024 * 1024
                part = Path(marker['output'])
                outer = part.parent.parent
                assert part.parent.is_dir() and outer.is_dir()
                assert len(children) == 1 and children[0].poll() is None and not exited
                assert len(broker.snapshot()['leases']) == 1 and speech._run_lock.locked()
                if cancel_render:
                    cancelled.set()
                else:
                    release.write_bytes(b'Explicit actual fixture release')
                assert future.result(timeout=10) is (not cancel_render)
            finally:
                cancelled.set()
                release.touch(exist_ok=True)
        assert children == exited and children[0].returncode is not None
        assert len(player.played) == int(not cancel_render)
        assert player.closed == 1 and speech.quiescent
        assert not outer.exists() and not part.exists()
        assert not broker.snapshot()['leases'] and not speech._run_lock.locked()
    finally:
        cancelled.set()
        release.touch(exist_ok=True)
        registry.close()


@pytest.mark.parametrize('basic', [False, True], ids=['heavy', 'basic'])
@pytest.mark.parametrize('mask', ['direct', 'interrupted', 'resource-cancelled'])
@pytest.mark.parametrize('first', [0, 1], ids=['first-child-first', 'second-child-first'])
def test_progress_actual_pending_families_retain_outer_files_registry_and_serialization(
        monkeypatch, basic, mask, first):
    engine, player = ClosingPCMProvider(), PrivatePlayer()
    actual_stop, children, allowed = process._stop_owned, [], {}
    def stop(child):
        return actual_stop(child) if allowed.get(child.pid, False) else False
    monkeypatch.setattr(process, '_stop_owned', stop)
    cancelled, paths = threading.Event(), []
    def render(text, voice, output, settings, stop_requested):
        engine.calls.append((text, voice.id, Path(output), settings))
        paths.append(Path(output))
        try:
            with ExitStack() as native:
                markers = []
                for index in range(2):
                    marker = Path(output).parent / ('owned-' + str(index))
                    child = native.enter_context(process.owned_process(
                        [sys.executable, '-I', '-S', '-c',
                         'from pathlib import Path; import sys,time; '
                         'Path(sys.argv[1]).write_bytes(b"owned"); time.sleep(60)', str(marker)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    children.append(child)
                    allowed[child.pid] = False
                    markers.append(marker)
                deadline = time.monotonic() + 5
                while not all(marker.exists() for marker in markers):
                    assert time.monotonic() < deadline, 'The real notification children did not enter their stage.'
                    time.sleep(.01)
                assert all(child.poll() is None for child in children)
                raise RuntimeError('Release both actual child families before publishing.')
        except AudioCleanupPending as error:
            cancelled.set()
            if mask == 'interrupted':
                raise InterruptedError('Later cancellation retains both native owners.') from error
            if mask == 'resource-cancelled':
                raise ResourceCancelled('Later resource cancellation retains both native owners.') from error
            raise
    provider = engine
    if basic:
        # Native dispatch only is doubled; the shared registry, private files,
        # actual owned children and complete stop boundary remain real.
        provider = object.__new__(FliteProvider)
        provider.voices, provider.close = engine.voices, engine.close
    provider.render = render
    registry = VoiceRegistry([provider])
    speech = activate_progress_voice(_PROGRESS_LIBRARY_ROOT, engine.voice.id,
                                    _registry_factory=lambda root: registry)
    speech._player_factory = lambda: player
    owner, broker, owners = speech._registry_owner, ResourceBroker.for_app(_PROGRESS_LIBRARY_ROOT), ()
    completed, notified, notification_lock = threading.Event(), set(), threading.Lock()
    def notification(identity):
        with notification_lock:
            notified.add(identity)
            ready = len(notified) == len(owners)
        if ready:
            completed.set()
    try:
        with pytest.raises((AudioCleanupPending, InterruptedError, ResourceCancelled)) as failure:
            speech.speak_progress('Keep the exact native work alive until confirmed exit.',
                                  volume=.2, cancelled=cancelled.is_set)
        owners = _pending_owners(failure.value)
        assert len(owners) == 2 and all(not item.quiescent for item in owners)
        assert len(children) == 2 and all(child.poll() is None for child in children)
        outer = paths[0].parent.parent
        assert outer.is_dir() and paths[0].parent.is_dir()
        assert not (outer / 'notification.wav').exists()
        assert len(broker.snapshot()['leases']) == (0 if basic else 1)
        assert owner._active == owner._selections == 1 and engine.close_count == 0
        assert speech._run_lock.locked() and not player.played and player.closed == 1
        cancelled.clear()
        assert not speech.speak_progress('No overlapping notification.', volume=.2, cancelled=lambda: False)
        assert len(engine.calls) == 1 and len(children) == 2
        speech.close()
        assert owner._selections == 0 and owner._active == 1 and not engine.closed.is_set()
        # These callbacks are registered after all retained context callbacks.
        # Their joint completion proves file/lease/ref release, beyond the
        # earlier native quiescent event itself.
        for item in owners:
            item.add_quiescent_callback(lambda identity=id(item): notification(identity))
        allowed[children[first].pid] = True
        deadline = time.monotonic() + 5
        while sum(item.quiescent for item in owners) != 1:
            assert time.monotonic() < deadline, 'The first actual family did not close.'
            for item in owners:
                item.close()
            time.sleep(.01)
        assert outer.is_dir() and not completed.is_set()
        assert owner._active == 1 and engine.close_count == 0 and speech._run_lock.locked()
        assert len(broker.snapshot()['leases']) == (0 if basic else 1)
        allowed[children[1 - first].pid] = True
        for item in owners:
            item.close()
        assert completed.wait(5), 'All retained cleanup callbacks must finish.'
        assert engine.closed.wait(5)
        assert all(item.quiescent for item in owners) and all(child.returncode is not None for child in children)
        assert not outer.exists() and not broker.snapshot()['leases']
        assert owner._active == 0 and engine.close_count == 1 and not speech._run_lock.locked()
        assert registry.voices() == () and not player.played
    finally:
        for child in children:
            allowed[child.pid] = True
        for item in owners:
            item.close()
        for child in children:
            actual_stop(child)
        speech.close()
        registry.close()
