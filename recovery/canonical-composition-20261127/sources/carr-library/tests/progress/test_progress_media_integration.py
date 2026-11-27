"""Real hub/feedback/registry coupling with explicit wx and audio-device doubles.

The provider writes actual private PCM. These checks establish contracts and
cleanup, not native playback, speech quality or screen-reader acceptance.
"""
from dataclasses import replace
from pathlib import Path
import struct
import sys
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech.progress_speech import (
    LocalProgressSpeech, activate_progress_voice, play_when_quiet,
)
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library.ui import progress_panel


def eventually(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, 'Owned synthetic operation did not finish.'
        Event().wait(.005)


class Control:
    """Only the native view surface is doubled; no wx event loop is claimed."""
    def __init__(self, *args, **kwargs):
        self.value, self.running = kwargs.get('value', ''), False
        self.native_id = id(self)
    def GetId(self): return self.native_id
    def SetName(self, value): self.name = value
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def Bind(self, *args): pass
    def Add(self, *args): pass
    def Insert(self, *args): pass
    def Layout(self): pass
    def Pulse(self): pass
    def Start(self, interval): self.running = True
    def Stop(self): self.running = False
    def SetFocus(self): pytest.fail('An automatic progress update stole focus.')


class PCMVoice:
    name = 'Purpose-written progress PCM fixture'
    def __init__(self, *, blocked=False):
        self.voice = Voice('fixture:uk', 'Selected Ukrainian fixture', 'Fixture', 'uk-UA',
                           capabilities=('volume',))
        self.entered, self.release, self.closed = Event(), Event(), Event()
        if not blocked:
            self.release.set()
        self.calls, self.discoveries, self.close_count = [], 0, 0
        self.rendering = self.closed_during_render = False

    def voices(self):
        self.discoveries += 1
        return (self.voice,)

    def render(self, text, voice, output, settings, cancelled):
        self.rendering = True
        self.calls.append((text, settings, Path(output)))
        self.entered.set()
        try:
            assert self.release.wait(3), 'The test did not release private synthesis.'
            with wave.open(str(output), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(22050)
                audio.writeframes(struct.pack('<h', 1200) * 1102)
        finally:
            self.rendering = False

    def close(self):
        self.closed_during_render |= self.rendering
        self.close_count += 1
        self.closed.set()


class Player:
    available = True
    def __init__(self, state='stopped'):
        self.state, self.played, self.close_count = state, [], 0
    def play(self, path):
        with wave.open(str(path), 'rb') as audio:
            assert audio.getnframes() == 1102 and any(audio.readframes(1102))
        self.played.append(Path(path))
    def close(self):
        self.state = 'stopped'
        self.close_count += 1


@pytest.fixture
def panel(tmp_path, monkeypatch):
    queued = []
    wx = SimpleNamespace(**{name: Control for name in
        ('Panel', 'BoxSizer', 'WrapSizer', 'Gauge', 'TextCtrl', 'Button', 'CheckBox', 'Timer')})
    for name in ('VERTICAL', 'HORIZONTAL', 'GA_HORIZONTAL', 'TE_READONLY', 'TE_MULTILINE',
                 'EXPAND', 'LEFT', 'RIGHT', 'TOP', 'ALL', 'ALIGN_CENTER_VERTICAL',
                 'EVT_BUTTON', 'EVT_CHECKBOX', 'EVT_TIMER', 'EVT_WINDOW_DESTROY'):
        setattr(wx, name, 0)
    wx.CallAfter = lambda callback: queued.append(callback)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(progress_panel, 'WindowsToneSink', lambda: None)
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), host=Control(),
                            layout=Control(), working=False, IsActive=lambda: True)
    view = progress_panel.NativeProgressPanel(frame)
    resources = []
    view.fixture_resources, view.fixture_callbacks = resources, queued
    try:
        yield view
    finally:
        for resource in resources:
            resource.engine.release.set()
        view.close()
        worker = view.feedback._worker
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive()
        for resource in resources:
            resource.speech.close()
            resource.registry.close()


def voice_for(panel, *, blocked=False, owned=False):
    engine, player = PCMVoice(blocked=blocked), Player()
    registry = VoiceRegistry((engine,))
    if owned:
        speech = activate_progress_voice(panel.frame.library.root, engine.voice.id,
                                         _registry_factory=lambda root: registry)
        speech._player_factory = lambda: player
    else:
        speech = LocalProgressSpeech(registry, engine.voice.id,
            library_root=panel.frame.library.root, _player_factory=lambda: player)
    resource = SimpleNamespace(engine=engine, player=player, registry=registry, speech=speech)
    panel.fixture_resources.append(resource)
    return resource


def measured_task(panel):
    task = panel.hub.start('Import source material', total=100)
    task.update(completed=49, phase='An English phase stays visible, without fabricated translation')
    panel.active_task = task
    return task


def enable(panel, resource, *, volume=25, quiet=True):
    panel.set_speech_provider(resource.speech, root=panel.frame.library.root)
    assert panel.preferences.mode == 'off' and not resource.engine.calls
    panel.save_preferences(replace(panel.preferences, mode='speech', volume=volume,
                                   quiet_during_media=quiet, interval_seconds=1))


@pytest.mark.parametrize('volume', (1, 25, 100))
def test_native_panel_scales_volume_and_localizes_real_registry_pcm(panel, volume):
    resource = voice_for(panel)
    enable(panel, resource, volume=volume)
    measured_task(panel)
    panel.refresh()
    assert resource.engine.entered.wait(2)
    eventually(lambda: panel.feedback.quiescent)
    assert panel.feedback.last_error == ''
    text, settings, temporary = resource.engine.calls[0]
    assert text == 'Робота триває. 49 відсотків.'
    assert settings.volume == volume
    assert 'English phase' in panel.text.GetValue()
    assert len(resource.player.played) == 1 and not temporary.exists()


def test_requested_cancellation_is_not_announced_as_completed(panel):
    resource = voice_for(panel)
    enable(panel, resource)
    measured_task(panel).request_cancel()
    panel.refresh()
    assert resource.engine.entered.wait(2)
    eventually(lambda: panel.feedback.quiescent)
    assert resource.engine.calls[0][0] == 'Надіслано запит на скасування. 49 відсотків.'
    assert 'waiting for the operation to stop' in panel.text.GetValue()


@pytest.mark.parametrize('change', ('selected-root', 'library-switch'))
def test_stale_voice_selection_cannot_cross_library_root(panel, tmp_path, change):
    current = voice_for(panel)
    panel.set_speech_provider(current.speech, root=tmp_path)
    other = tmp_path / 'other-library'
    other.mkdir()
    requested = voice_for(panel)
    if change == 'selected-root':
        requested.speech.library_root = other
    else:
        panel.frame.library.root = other
    with pytest.raises(ValueError, match='library'):
        panel.set_speech_provider(requested.speech, root=tmp_path)
    assert panel.speech_provider.provider is current.speech
    assert not current.speech._closed.is_set()
    assert current.engine.calls == requested.engine.calls == []


@pytest.mark.parametrize('state', ('recording', 'monitoring', 'unrecognized-device-state'))
def test_recording_and_uncertain_devices_remain_quiet_despite_playback_opt_out(panel, state):
    resource = voice_for(panel)
    enable(panel, resource, quiet=False)
    player = Player(state)
    panel.register_player(player)
    task = measured_task(panel)
    panel.refresh()
    assert resource.engine.calls == [] and panel.feedback.quiescent
    player.state = 'stopped'
    task.update(completed=50)
    panel.refresh()
    assert resource.engine.entered.wait(2)
    eventually(lambda: panel.feedback.quiescent)
    assert resource.engine.calls[0][0] == 'Робота триває. 50 відсотків.'


def test_unconfirmed_player_close_blocks_recording_until_actual_silence(panel):
    player = Player('playing')
    panel.register_player(player)
    panel.unregister_player(player, silence_confirmed=False)
    hold = panel.hold_audio(recording=True)
    try:
        assert not hold.ready
        assert player in panel.media.uncertain
        player.state = 'stopped'
        assert hold.ready
        assert player not in panel.media.uncertain
    finally:
        hold.close()
    assert not panel.media.holds


def test_cancelled_deferred_content_playback_never_starts_after_notification_cleanup(panel):
    resource = voice_for(panel, blocked=True)
    enable(panel, resource)
    measured_task(panel)
    panel.refresh()
    assert resource.engine.entered.wait(2)
    scheduled, played, errors = [], [], []
    alive = [True]
    play_when_quiet((panel,), lambda: played.append(True),
        schedule=lambda milliseconds, callback: scheduled.append(callback),
        alive=lambda: alive[0], on_error=errors.append)
    assert len(scheduled) == 1 and panel.media.holds
    alive[0] = False
    scheduled.pop()()
    resource.engine.release.set()
    eventually(lambda: panel.feedback.quiescent)
    assert not played and not errors and not resource.player.played
    assert not panel.media.holds and not scheduled


def test_library_switch_cancels_pending_owned_speech_before_registry_cleanup(panel, tmp_path):
    resource = voice_for(panel, blocked=True, owned=True)
    enable(panel, resource)
    measured_task(panel)
    panel.refresh()
    assert resource.engine.entered.wait(2)
    destination = tmp_path / 'next-library'
    destination.mkdir()
    panel.frame.library.root = destination
    panel.change_root(destination)
    assert resource.speech._closed.is_set()
    assert resource.engine.close_count == 0
    resource.engine.release.set()
    assert resource.engine.closed.wait(2)
    eventually(lambda: panel.feedback.quiescent)
    assert resource.engine.close_count == 1 and not resource.engine.closed_during_render
    assert not resource.player.played and panel.speech_provider is None


def test_directly_injected_registry_is_borrowed_after_all_selection_closes(tmp_path):
    engine = PCMVoice()
    registry = VoiceRegistry((engine,))
    first = LocalProgressSpeech(registry, engine.voice.id, library_root=tmp_path)
    second = first.copy_selection()
    try:
        first.close()
        second.close()
        first.close()
        assert registry.get(engine.voice.id) == engine.voice and engine.close_count == 0
    finally:
        registry.close()


def test_owned_registry_survives_one_panel_close_then_releases_once_after_last_copy(tmp_path):
    engine, player = PCMVoice(blocked=True), Player()
    registry = VoiceRegistry((engine,))
    first = activate_progress_voice(tmp_path, engine.voice.id, _registry_factory=lambda root: registry)
    first._player_factory = lambda: player
    second = first.copy_selection()
    results, errors = [], []
    def speak():
        try:
            results.append(second.speak_progress('Робота триває.', volume=.25, cancelled=lambda: False))
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=speak)
    try:
        thread.start()
        assert engine.entered.wait(2)
        first.close()
        first.close()
        assert not second._closed.is_set() and engine.close_count == 0
        engine.release.set()
        thread.join(3)
        assert not thread.is_alive() and results == [True] and errors == []
        assert len(player.played) == 1 and engine.discoveries == 1
        assert engine.close_count == 0
        second.close()
        second.close()
        assert engine.closed.wait(2)
        assert engine.close_count == 1 and not engine.closed_during_render
        with pytest.raises(RuntimeError, match='closed'):
            second.copy_selection()
    finally:
        engine.release.set()
        thread.join(3)
        first.close()
        second.close()
        registry.close()


def test_rejected_owned_voice_selection_closes_the_fresh_registry(tmp_path):
    engine = PCMVoice()
    registry = VoiceRegistry((engine,))
    with pytest.raises(ValueError, match='available'):
        activate_progress_voice(tmp_path, 'absent-voice', _registry_factory=lambda root: registry)
    assert engine.closed.wait(2)
    assert engine.close_count == 1 and not engine.calls


def test_live_media_waits_for_finite_progress_cue_without_interrupting_it(panel):
    from carr_library.progress_feedback import tone_wave
    entered, finished = Event(), Event()
    calls = []

    def play(tone, *, cancelled):
        calls.append(tone_wave(tone))
        entered.set()
        assert finished.wait(3), 'The test did not release its finite progress cue.'

    sink = SimpleNamespace(play=play)
    panel.tone_sink = panel.feedback.tone_sink = sink
    panel.save_preferences(replace(panel.preferences, mode='beep', interval_seconds=1))
    bridge = panel.frame.live_media_feedback
    assert bridge is panel.media is panel.live_media_feedback and bridge.quiescent
    task = measured_task(panel)
    try:
        panel.refresh()
        assert entered.wait(2)
        release = bridge.hold(lambda: True)
        assert bridge.busy and not bridge.quiescent
        task.update(completed=50)
        panel.refresh()
        assert len(calls) == 1
    finally:
        finished.set()
    eventually(lambda: panel.feedback.quiescent)
    assert bridge.quiescent and len(calls) == 1
    assert isinstance(calls[0], bytes) and calls[0].startswith(b'RIFF')
    release()
    panel.refresh_audio_state()
    assert not bridge.busy


def test_live_media_leases_cover_pause_cleanup_and_release_off_thread_after_close(panel):
    bridge = panel.frame.live_media_feedback
    activity = {'paused': True, 'cleanup': True}
    first = bridge.hold(lambda: activity['paused'])
    second = bridge.hold(lambda: activity['cleanup'])
    first()
    first()
    panel.refresh_audio_state()
    assert bridge.busy and panel.media.flags()[1]
    panel.close()
    assert bridge.busy  # Native destruction cannot invent worker cleanup.
    activity['cleanup'] = False
    worker = Thread(target=second)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and not bridge.busy
    assert not bridge._activities and panel.feedback._closed
    assert panel.fixture_callbacks == []


def test_unknown_media_state_stays_quiet_and_explicit_overlap_policy_is_respected(panel):
    resource = voice_for(panel, blocked=True)
    enable(panel, resource)
    measured_task(panel)
    panel.refresh()
    assert resource.engine.entered.wait(2)

    def unknown():
        raise RuntimeError('Cleanup has not acknowledged completion')

    bridge = panel.live_media_feedback
    release = bridge.hold(unknown)
    panel.refresh_audio_state()
    assert bridge.busy and panel.media.flags()[1]
    assert not bridge.quiescent  # The actual bounded notification is still active.
    panel.save_preferences(replace(panel.preferences, quiet_during_media=False))
    assert bridge.quiescent  # Explicit overlap is allowed for ordinary activity.
    release()
    resource.engine.release.set()
    eventually(lambda: panel.feedback.quiescent)
    panel.save_preferences(replace(panel.preferences, quiet_during_media=True))
    panel.frame.progress_media_busy = lambda: True
    assert panel.media.flags() == (False, True)
    panel.frame.progress_media_busy = lambda: False
    assert panel.media.flags() == (False, False)


@pytest.mark.parametrize('broken_property', [False, True])
def test_uncertain_native_silence_blocks_new_content_even_when_cue_overlap_is_allowed(panel, broken_property):
    class UnconfirmedDevice:
        def __call__(self):
            return True

        @property
        def silence_uncertain(self):
            if broken_property:
                raise RuntimeError('Device close status could not be read')
            return True

    panel.save_preferences(replace(panel.preferences, quiet_during_media=False))
    bridge = panel.live_media_feedback
    assert bridge.quiescent
    release = bridge.hold(UnconfirmedDevice())
    hold = panel.hold_audio()
    try:
        assert bridge.busy and not bridge.quiescent and not hold.ready
        release()
        assert bridge.quiescent and hold.ready
    finally:
        release()
        hold.close()


def test_live_reader_worker_releases_its_plain_lease_after_panel_close_without_native_callbacks(panel):
    from concurrent.futures import Future
    from unittest.mock import Mock
    from carr_library.ui.live_reading import LiveReadingControls, _MediaActivity

    completion = Future()
    core = SimpleNamespace(quiescent=False, busy=True, silence_uncertain=False,
                           close=Mock(return_value=completion))
    activity = _MediaActivity(False, core)
    release = panel.media.hold(activity)
    controls = object.__new__(LiveReadingControls)
    controls.closed = False
    controls.timer = SimpleNamespace(Stop=Mock())
    controls.core = core
    controls._media_indicator, controls._release_media = activity, release
    controls._host, controls._previous_media = panel.frame, False
    controls.close()
    controls.close()
    core.close.assert_called_once_with()
    controls.timer.Stop.assert_called_once_with()
    assert panel.media.busy and activity.core is core
    panel.close()
    assert activity.core is core and panel.media._activities
    core.quiescent = True
    worker = Thread(target=lambda: completion.set_result(None))
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert activity.core is None and not panel.media.busy
    assert not panel.media._activities and panel.fixture_callbacks == []


def test_progress_destroy_uses_captured_native_id_after_wrapper_replacement(panel):
    from unittest.mock import Mock

    child = SimpleNamespace(GetId=lambda: panel._native_id + 1,
                            GetEventObject=Mock(side_effect=AssertionError('Do not compare dying SIP wrappers.')),
                            Skip=Mock())
    panel._destroyed(child)
    child.Skip.assert_called_once_with()
    assert not panel.closed
    panel.timer.Stop = Mock(side_effect=RuntimeError('Native timer already destroyed'))
    actual = SimpleNamespace(GetId=lambda: panel._native_id,
                             GetEventObject=Mock(side_effect=AssertionError('Do not compare dying SIP wrappers.')),
                             Skip=Mock())
    panel._destroyed(actual)
    actual.Skip.assert_called_once_with()
    assert panel.closed and panel.feedback._closed
    child.GetEventObject.assert_not_called()
    actual.GetEventObject.assert_not_called()
