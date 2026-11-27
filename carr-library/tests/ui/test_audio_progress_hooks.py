"""Actual UI handlers with explicit control/transport doubles; no listening claims."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import os
import threading

import pytest

from carr_library.audio import progress_bridge
from carr_library.audio.player import WavePlayer
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferences
from carr_library.ui.audio_studio import AudioStudioDialog, LocalPlayback, StudioController
from carr_library.ui.progress_panel import NativeProgressPanel


class Player:
    def __init__(self, events):
        self.events = events
        self.state = 'stopped'
        self.close_error = None

    def play(self, path):
        self.events.append(('play', path))
        self.state = 'playing'

    def close(self):
        self.events.append('close player')
        if self.close_error is not None:
            raise self.close_error
        self.state = 'stopped'


class Bridge:
    def __init__(self, events):
        self.events = events
        self.getters = []

    def register_playback(self, getter):
        self.getters.append(getter)

        def remove():
            self.events.append('unregister player')
            self.getters.remove(getter)

        return remove

    def before_playback(self):
        self.events.append('silence progress')


@pytest.fixture
def studio(monkeypatch):
    window = object.__new__(AudioStudioDialog)
    window.events, window.messages, window.errors = [], [], []
    window.parent = SimpleNamespace(progress_panel=SimpleNamespace(
        set_speech_provider=lambda provider: window.events.append(('provider', provider))))
    registry = object()
    window.controller = SimpleNamespace(
        _registry=lambda: registry, voice='original:voice', language='en',
        result={'output': '/local/render/result.wav'})
    window.voice_rows = [
        {'id': 'local:first', 'available': True},
        {'id': 'local:selected', 'available': True}]
    window.voice = SimpleNamespace(GetSelection=lambda: 1)
    window.language = SimpleNamespace(GetValue=lambda: 'uk')
    window.playback = Player(window.events)
    window._playback_closed = False
    window.media_progress = None
    window._remove_progress_playback = None
    window.busy = window.closed = window.close_pending = window.editing_dirty = False
    window._message = lambda text, **kwargs: window.messages.append(text)
    window._error = lambda error: window.errors.append(error)
    window.bridge = Bridge(window.events)
    monkeypatch.setattr(progress_bridge, 'install_media_progress', lambda frame: window.bridge)
    return window


def test_ui_reuses_reviewed_player_without_global_sound_stop():
    assert LocalPlayback is WavePlayer


def test_playback_registration_observes_current_player_and_attaches_only_once(studio):
    studio._attach_media_progress()
    studio._attach_media_progress()
    assert len(studio.bridge.getters) == 1
    getter = studio.bridge.getters[0]
    assert getter() is studio.playback
    replacement = Player(studio.events)
    studio.playback = replacement
    assert getter() is replacement
    studio._close_playback()
    assert getter() is None
    assert studio.bridge.getters == []
    assert studio.events == ['close player', 'unregister player']


def test_legacy_host_without_progress_remains_usable(studio):
    del studio.parent.progress_panel
    studio._attach_media_progress()
    assert studio.media_progress is None
    assert studio.bridge.getters == []
    studio.play()
    assert studio.events == [('play', Path('/local/render/result.wav'))]
    assert not studio.errors


@pytest.mark.parametrize('filename,expected', [
    ('result.wav', 'result.wav'), ('result.mp3', 'master.wav')])
def test_quiet_gate_precedes_only_actual_current_result_playback(studio, filename, expected):
    studio.controller.result = {'output': '/local/render/' + filename}
    studio._attach_media_progress()
    studio.play()
    assert studio.events == ['silence progress', ('play', Path('/local/render/' + expected))]
    assert studio.bridge.getters[0]().state == 'playing'
    assert not studio.errors


@pytest.mark.parametrize('invalid', ['missing render', 'dirty edits'])
def test_invalid_playback_does_not_suspend_feedback_or_start_player(studio, invalid):
    studio._attach_media_progress()
    if invalid == 'missing render':
        studio.controller.result = None
    else:
        studio.editing_dirty = True
    studio.play()
    assert studio.events == []
    assert len(studio.errors) == 1


def test_playback_failure_keeps_actual_stopped_state(studio):
    studio._attach_media_progress()

    def unavailable(path):
        raise OSError('Device unavailable')

    studio.playback.play = unavailable
    studio.play()
    assert studio.events == ['silence progress']
    assert studio.bridge.getters[0]().state == 'stopped'
    assert len(studio.errors) == 1
    assert studio.messages == []


def test_cleanup_is_idempotent_and_unregisters_after_close(studio):
    studio._attach_media_progress()
    remove = studio._remove_progress_playback

    def observe_then_remove():
        assert studio._current_playback() is studio.playback
        assert studio.playback.state == 'stopped'
        remove()

    studio._remove_progress_playback = observe_then_remove
    studio._close_playback()
    studio._close_playback()
    assert studio.events == ['close player', 'unregister player']
    assert studio._current_playback() is None


def test_failed_close_retains_observation_until_retry_succeeds(studio):
    studio._attach_media_progress()
    studio.playback.close_error = OSError('Transport close was not confirmed')
    with pytest.raises(OSError):
        studio._close_playback()
    assert len(studio.bridge.getters) == 1
    assert studio.bridge.getters[0]() is studio.playback
    assert not studio._playback_closed
    studio.playback.close_error = None
    studio._close_playback()
    assert studio.events == ['close player', 'close player', 'unregister player']


@pytest.mark.parametrize('busy', [False, True])
def test_veto_or_busy_close_keeps_live_player_observed(studio, busy):
    studio._attach_media_progress()
    studio.busy = busy
    studio._unsaved = lambda: False
    studio.cancel = lambda: studio.events.append('request cancellation')
    event = SimpleNamespace(CanVeto=lambda: True, Veto=lambda: studio.events.append('veto'))
    studio.close(event)
    assert len(studio.bridge.getters) == 1
    assert not studio.closed
    assert 'close player' not in studio.events
    assert studio.close_pending == busy


def test_close_action_and_modal_finally_close_transport_once(studio):
    studio._attach_media_progress()
    studio.cancelled = threading.Event()
    studio.timer = SimpleNamespace(Stop=lambda: None)
    studio.executor = SimpleNamespace(shutdown=lambda **kwargs: None)
    studio.wx = SimpleNamespace(ID_CANCEL=5101)
    studio._unsaved = lambda: True
    studio.reload_voices = lambda: None
    studio.script = SimpleNamespace(SetFocus=lambda: None)
    studio.dialog = SimpleNamespace(
        ShowModal=lambda: studio.close(), IsModal=lambda: True,
        EndModal=lambda result: None, Destroy=lambda: studio.events.append('destroy dialog'))
    studio.show()
    assert studio.events == ['close player', 'unregister player', 'destroy dialog']
    assert studio.closed and studio.cancelled.is_set()


def test_voice_action_uses_explicit_selection_and_same_registry_without_project_edit(studio, monkeypatch):
    provider = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    constructed = []

    def local_voice(registry, **kwargs):
        constructed.append((registry, kwargs))
        return provider

    monkeypatch.setattr(progress_bridge, 'LocalVoiceProgressSpeech', local_voice)
    studio.use_selected_voice_for_progress()
    assert constructed == [(studio.controller._registry(), {
        'voice_id': 'local:selected', 'language': 'uk', 'media': studio.bridge})]
    assert studio.events == [('provider', provider)]
    assert studio.controller.voice == 'original:voice' and studio.controller.language == 'en'
    assert len(studio.bridge.getters) == 1
    assert 'mode is unchanged' in studio.messages[0]
    assert not studio.errors


@pytest.mark.parametrize('selection', [-1, 2, True, '1'])
def test_voice_action_requires_explicit_available_selection(studio, monkeypatch, selection):
    studio.voice.GetSelection = lambda: selection
    monkeypatch.setattr(progress_bridge, 'LocalVoiceProgressSpeech',
                        lambda *args, **kwargs: pytest.fail('No voice adapter before a valid selection.'))
    studio.use_selected_voice_for_progress()
    assert len(studio.errors) == 1
    assert not studio.events and not studio.bridge.getters


@pytest.mark.parametrize('invalid', ['unavailable voice', 'invalid language', 'missing panel'])
def test_voice_action_rejects_unavailable_capabilities_before_binding(studio, monkeypatch, invalid):
    if invalid == 'unavailable voice':
        studio.voice_rows[1]['available'] = False
    elif invalid == 'invalid language':
        studio.language.GetValue = lambda: 'uk\n'
    else:
        del studio.parent.progress_panel
    monkeypatch.setattr(progress_bridge, 'LocalVoiceProgressSpeech',
                        lambda *args, **kwargs: pytest.fail('Invalid selection must not create a provider.'))
    studio.use_selected_voice_for_progress()
    assert len(studio.errors) == 1
    assert not studio.events


@pytest.mark.parametrize('flag', ['busy', 'closed'])
def test_voice_action_cannot_rebind_during_worker_or_after_close(studio, monkeypatch, flag):
    setattr(studio, flag, True)
    monkeypatch.setattr(progress_bridge, 'LocalVoiceProgressSpeech',
                        lambda *args, **kwargs: pytest.fail('The action is unavailable.'))
    studio.use_selected_voice_for_progress()
    assert not studio.events and not studio.errors


@pytest.fixture
def panel():
    value = object.__new__(NativeProgressPanel)
    value.closed = False
    value.preferences = ProgressPreferences()
    value.speech_provider = value.tone_sink = None
    value.frame = SimpleNamespace(progress_speech_provider=None)
    value.feedback = ProgressFeedback(value.preferences)
    value.notice = ''
    value.refresh_count = 0

    def refreshed():
        value.refresh_count += 1

    value.refresh = refreshed
    value.store = SimpleNamespace(save=lambda *args: pytest.fail('Voice binding must not write preferences.'))
    yield value
    value.feedback.close()


def test_panel_binds_voice_capability_without_enabling_sound_or_writing_preferences(panel):
    provider = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    original = panel.preferences
    panel.notice = 'Spoken progress is unavailable until an approved local voice is connected.'
    panel.set_speech_provider(provider)
    assert panel.speech_provider is panel.feedback.speech_provider is panel.frame.progress_speech_provider is provider
    assert panel.preferences is original and panel.preferences.mode == 'off'
    assert panel.feedback.preferences.mode == 'off'
    assert ('Speech', 'speech') in panel.supported_modes()
    assert panel.refresh_count == 1 and panel.notice == ''


def test_panel_unbinds_speech_safely_and_keeps_explicit_saved_choice(panel):
    panel.set_speech_provider(SimpleNamespace(speak_progress=lambda *args, **kwargs: None))
    panel.preferences = replace(panel.preferences, mode='speech')
    panel.feedback.set_preferences(panel.preferences)
    panel.set_speech_provider(None)
    assert panel.speech_provider is panel.feedback.speech_provider is panel.frame.progress_speech_provider is None
    assert panel.preferences.mode == 'speech' and panel.feedback.preferences.mode == 'off'
    assert panel.supported_modes() == (('Off', 'off'),)
    assert 'unavailable' in panel.notice


@pytest.mark.parametrize('provider', [object(), SimpleNamespace(speak_progress='not callable')])
def test_panel_invalid_provider_preserves_active_binding(panel, provider):
    existing = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    panel.set_speech_provider(existing)
    with pytest.raises(ValueError):
        panel.set_speech_provider(provider)
    assert panel.speech_provider is panel.feedback.speech_provider is panel.frame.progress_speech_provider is existing
    assert panel.refresh_count == 1


def test_panel_closed_dispatcher_rejects_binding_without_publishing_new_capability(panel):
    panel.feedback.close()
    with pytest.raises(ValueError, match='closed'):
        panel.set_speech_provider(SimpleNamespace(speak_progress=lambda *args, **kwargs: None))
    assert panel.speech_provider is panel.frame.progress_speech_provider is None
    assert panel.refresh_count == 0


def test_root_switch_disconnects_library_voice_before_loading_preferences(panel, tmp_path, monkeypatch):
    panel.set_speech_provider(SimpleNamespace(speak_progress=lambda *args, **kwargs: None))
    new_preferences = replace(panel.preferences, mode='speech', volume=45)
    selected_paths = []
    muted_values = []

    def loaded():
        assert panel.speech_provider is panel.frame.progress_speech_provider is panel.feedback.speech_provider is None
        return new_preferences

    new_store = SimpleNamespace(load=loaded,
        save=lambda *args: pytest.fail('Root switching must not rewrite either feedback profile.'))

    def store_at(path):
        selected_paths.append(path)
        return new_store

    monkeypatch.setattr('carr_library.ui.progress_panel.ProgressPreferenceStore', store_at)
    panel.mute = SimpleNamespace(SetValue=muted_values.append)
    panel.change_root(tmp_path / 'second library')
    assert selected_paths == [tmp_path / 'second library' / 'progress-preferences.json']
    assert panel.store is new_store and panel.preferences is new_preferences
    assert panel.feedback.preferences.mode == 'off' and panel.preferences.mode == 'speech'
    assert panel.active_task is None and muted_values == [False]


def test_refused_busy_root_switch_keeps_selected_provider(panel, tmp_path):
    provider = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    panel.set_speech_provider(provider)
    panel.frame.working = True
    original_store = panel.store
    with pytest.raises(ValueError, match='current operation'):
        panel.change_root(tmp_path)
    assert panel.speech_provider is panel.frame.progress_speech_provider is panel.feedback.speech_provider is provider
    assert panel.store is original_store


def test_studio_close_keeps_explicit_voice_choice_within_same_library(studio, panel):
    provider = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    panel.set_speech_provider(provider)
    studio.parent.progress_panel = panel
    studio._attach_media_progress()
    studio._close_playback()
    assert panel.speech_provider is panel.frame.progress_speech_provider is panel.feedback.speech_provider is provider


@pytest.mark.skipif(os.name != 'nt' and not os.environ.get('DISPLAY'),
                    reason='Requires an actual native wx display; control doubles are covered separately.')
@pytest.mark.parametrize('with_progress', [False, True])
def test_native_voice_button_exposes_name_and_dispatches_explicit_action(tmp_path, monkeypatch, with_progress):
    wx = pytest.importorskip('wx')
    app = wx.GetApp() or wx.App(False)
    frame = wx.Frame(None, title='Native progress voice binding test')
    received = []
    if with_progress:
        frame.progress_panel = SimpleNamespace(set_speech_provider=received.append)
    bridge = Bridge([])
    monkeypatch.setattr(progress_bridge, 'install_media_progress', lambda parent: bridge)
    provider = SimpleNamespace(speak_progress=lambda *args, **kwargs: None)
    monkeypatch.setattr(progress_bridge, 'LocalVoiceProgressSpeech', lambda *args, **kwargs: provider)
    controller = StudioController(private_root=tmp_path / 'private', registry=object(), ffmpeg=tmp_path / 'ffmpeg')
    studio = AudioStudioDialog(frame, controller)
    try:
        if not with_progress:
            assert studio.progress_voice_button is None
            assert bridge.getters == []
        else:
            button = studio.progress_voice_button
            assert isinstance(button, wx.Button)
            assert button.GetName() == 'Use selected voice for progress'
            studio.voice_rows = [{'id': 'local:selected', 'available': True}]
            studio.voice.SetItems(['Selected local test voice'])
            studio.voice.SetSelection(0)
            studio.language.ChangeValue('uk')
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            button.GetEventHandler().ProcessEvent(event)
            assert received == [provider]
            assert 'mode is unchanged' in studio.status.GetValue()
    finally:
        studio.closed = True
        studio.timer.Stop()
        studio._close_playback()
        studio.executor.shutdown(wait=False, cancel_futures=True)
        studio.dialog.Destroy()
        frame.Destroy()
        app.Yield()
