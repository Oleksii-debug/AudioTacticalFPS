"""Actual progress/Studio handlers with explicit device and scheduler doubles."""
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library import progress as p
from carr_library.audio.engines import Voice, VoiceRegistry
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.progress_speech import activate_progress_voice
from carr_library.ui import progress_view as ui
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController


def registry():
    voice = Voice('sapi5:fixture', 'Explicit fixture voice', ('en',), 'sapi5')
    engine = SimpleNamespace(id='sapi5', voices=lambda: (voice,),
                             synthesize=Mock(side_effect=AssertionError('No speech without explicit output mode.')))
    return VoiceRegistry([engine]), voice


def view(root):
    value = ui.ProgressView.__new__(ui.ProgressView)
    now = [0.]
    value.clock, value.now = lambda: now[0], now
    value.tone_sender, value.speech_sender = Mock(), Mock()
    value.speech_provider = None
    value.closed, value.tracker, value.audible = False, None, True
    value.settings, value.notice = p.ProgressSettings(), ''
    value.policy = p.ProgressTonePolicy(clock=value.clock)
    value._last_refresh, value._last_text, value._last_value = float('-inf'), '', None
    value._pending, value._pending_lock = None, Lock()
    value._audio_owner, value._media_active, value._active = object(), False, True
    value.root = lambda: root
    value.gauge, value.status, value.panel, value.timer = Mock(), Mock(), Mock(), Mock()
    value.panel.GetTopLevelParent.return_value.IsActive.return_value = True
    value.wx = object()
    return value


def test_restored_speech_mode_remains_silent_until_explicit_session_selection(tmp_path):
    p.ProgressSettingsStore(tmp_path).save(p.ProgressSettings('speech'))
    widget = view(tmp_path)
    try:
        widget.track(p.ProgressTracker('Synthetic work'))
        widget.speech_sender.assert_not_called()
        assert 'for this session' in widget.notice
        selected, voice = registry()
        provider = activate_progress_voice(tmp_path, selected, voice.id, language='en')
        widget.set_speech_provider(provider)
        widget.refresh(force=True)
        widget.speech_sender.assert_called_once()
        args = widget.speech_sender.call_args.args
        assert args[0] is provider and 'Total work is not yet known' in args[1]
        assert args[3]() is True
        assert widget.speech_sender.call_args.kwargs['segments'] == ()
    finally:
        widget.close()


def test_both_uses_one_dispatch_request_and_language_remains_independent_of_project(tmp_path):
    p.ProgressSettingsStore(tmp_path).save(p.ProgressSettings('both', 37, 9))
    selected, voice = registry()
    widget = view(tmp_path)
    provider = activate_progress_voice(tmp_path, selected, voice.id, language='en')
    widget.set_speech_provider(provider)
    try:
        widget.track(p.ProgressTracker('Work', total=4, completed=1))
        widget.tone_sender.assert_not_called()
        widget.speech_sender.assert_called_once()
        args = widget.speech_sender.call_args.args
        assert args[0] is provider and '25 percent' in args[1] and args[2] == 37
        assert widget.speech_sender.call_args.kwargs['segments'] == ((311, 70),)
    finally:
        widget.close()


@pytest.mark.parametrize('change', ['muted', 'closed', 'replaced', 'new-library', 'new-sequence', 'background'])
def test_queued_voice_validity_tracks_current_scope_and_latest_real_progress(tmp_path, change):
    p.ProgressSettingsStore(tmp_path).save(p.ProgressSettings('speech'))
    selected, voice = registry()
    widget = view(tmp_path)
    provider = activate_progress_voice(tmp_path, selected, voice.id, language='en')
    widget.set_speech_provider(provider)
    tracker = widget.track(p.ProgressTracker('First operation', total=4))
    valid = widget.speech_sender.call_args.args[3]
    assert valid()
    try:
        if change == 'muted':
            widget.settings = p.ProgressSettings('speech', muted=True)
        elif change == 'closed':
            widget.close()
        elif change == 'replaced':
            widget.set_speech_provider(provider.copy_selection())
        elif change == 'new-library':
            other = tmp_path / 'other'
            other.mkdir()
            widget.root = lambda: other
        elif change == 'new-sequence':
            tracker.update(completed=1, total=4)
        else:
            widget._active = False
        assert not valid()
    finally:
        widget.close()


def test_resume_and_completed_notices_use_actual_tracker_transitions(tmp_path):
    p.ProgressSettingsStore(tmp_path).save(p.ProgressSettings('speech'))
    selected, voice = registry()
    widget = view(tmp_path)
    widget.set_speech_provider(activate_progress_voice(tmp_path, selected, voice.id, language='en'))
    tracker = widget.track(p.ProgressTracker('Actual transitions'))
    try:
        tracker.pause()
        widget.refresh(force=True)
        assert widget.speech_sender.call_args.args[1].startswith('Paused')
        tracker.resume()
        widget.refresh(force=True)
        assert widget.speech_sender.call_args.args[1].startswith('Resumed')
        tracker.finish(verified=True)
        before = tracker.snapshot()
        widget.speech_sender.side_effect = OSError('Display observer failed.')
        widget.refresh(force=True)
        assert tracker.snapshot() == before and tracker.snapshot().percent == 100
        assert 'operation result is unchanged' in widget.notice
    finally:
        widget.close()


def test_actual_studio_selection_preserves_text_render_voice_and_off_preference(tmp_path):
    selected, voice = registry()
    controller = StudioController(private_root=tmp_path, registry=selected)
    controller.new('Literal source line.\n  Keep all spaces.', title='Synthetic reading')
    before = (controller.script, controller.voice, controller.language, controller.project_id)
    child, parent = view(tmp_path), view(tmp_path)
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.controller, dialog.parent = controller, SimpleNamespace(
        library=SimpleNamespace(root=tmp_path), progress_view=parent)
    dialog.progress_view = child
    dialog.voice = Mock()
    dialog.voice.GetSelection.return_value = 0
    dialog.voice_rows = [{'id': voice.id}]
    dialog.language = Mock()
    dialog.language.GetValue.return_value = 'en'
    dialog.closed, dialog.cancelled = False, Event()
    dialog._error, dialog._message = Mock(), Mock()
    dialog._run = lambda label, action, accepted: accepted(action())
    try:
        dialog.use_progress_voice()
        dialog._error.assert_not_called()
        assert child.speech_provider.voice == parent.speech_provider.voice == voice
        assert child.speech_provider is not parent.speech_provider
        assert child.settings.mode == parent.settings.mode == 'off'
        assert p.ProgressSettingsStore(tmp_path).load().mode == 'off'
        assert (controller.script, controller.voice, controller.language, controller.project_id) == before
        assert not (tmp_path / p.FILENAME).exists()
        controller.ffmpeg = tmp_path / 'changed-ffmpeg.exe'
        with pytest.raises(ValueError, match='configuration changed'):
            parent.speech_provider.copy_selection()
    finally:
        child.close()
        parent.close()


class Scheduled:
    def __init__(self, callback):
        self.callback, self.stopped = callback, False

    def Stop(self):
        self.stopped = True


def playback_dialog():
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.closed = dialog.busy = False
    dialog._error, dialog._message = Mock(), Mock()
    dialog.clock, dialog.scheduled, dialog.admissions = [0.], [], []
    dialog._quiet_clock = lambda: dialog.clock[0]
    dialog.progress_view = SimpleNamespace(audio_quiescent=False,
        set_media_active=lambda active: dialog.admissions.append(active))
    dialog.playback_state = [PlaybackStatus()]
    dialog.playback = SimpleNamespace(native=SimpleNamespace(_cleanup_error=None),
                                      status=lambda: dialog.playback_state[0],
                                      stop=lambda: dialog.playback_state.__setitem__(0, PlaybackStatus()))
    def schedule(milliseconds, callback):
        assert milliseconds == 25
        timer = Scheduled(callback)
        dialog.scheduled.append(timer)
        return timer
    dialog.wx = SimpleNamespace(CallLater=schedule)
    return dialog


def test_native_content_start_is_deferred_without_blocking_until_owned_speech_is_quiet():
    dialog = playback_dialog()
    def play():
        assert dialog.admissions[-1] is True
        dialog.playback_state[0] = PlaybackStatus(state='playing')
    action = Mock(side_effect=play)
    dialog._with_quiet_output(action)
    assert dialog.admissions == [True] and len(dialog.scheduled) == 1
    action.assert_not_called()
    dialog.progress_view.audio_quiescent = True
    dialog.scheduled.pop().callback()
    action.assert_called_once()
    assert dialog.admissions[-1] is True
    dialog._error.assert_not_called()


@pytest.mark.parametrize('change', ['stop', 'close', 'new-start', 'busy'])
def test_cancelled_or_superseded_deferred_start_cannot_fire_late(change):
    dialog = playback_dialog()
    first, second = Mock(), Mock()
    dialog._with_quiet_output(first)
    timer = dialog.scheduled[-1]
    if change == 'stop':
        dialog.refresh_playback = lambda: dialog._sync_playback_media()
        dialog.stop_playback()
    elif change == 'close':
        dialog.closed = True
        dialog._invalidate_playback_start()
    elif change == 'new-start':
        dialog._with_quiet_output(second)
    else:
        dialog.busy = True
        dialog._invalidate_playback_start()
    dialog.progress_view.audio_quiescent = True
    timer.callback()  # Already-delivered wx callbacks can survive Timer.Stop().
    first.assert_not_called()
    if change == 'new-start':
        dialog.scheduled[-1].callback()
        second.assert_called_once()
    else:
        second.assert_not_called()


def test_unconfirmed_speech_silence_times_out_without_starting_content():
    dialog = playback_dialog()
    action = Mock()
    dialog._with_quiet_output(action)
    dialog.clock[0] = 3.
    dialog.scheduled[-1].callback()
    action.assert_not_called()
    assert 'has not confirmed silence' in str(dialog._error.call_args.args[0])
    assert dialog._playback_start_token is None
