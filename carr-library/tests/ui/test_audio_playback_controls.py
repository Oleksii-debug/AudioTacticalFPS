"""Playback command and cleanup boundaries; native wx/device proof is separate."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.playback import PlaybackStatus
from carr_library.ui.audio_studio import AudioStudioDialog, LocalPlayback


class Text:
    def __init__(self, value=''):
        self.value, self.changes = value, []

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value
        self.changes.append(value)


def studio(state='playing', position=40, total=80):
    view = AudioStudioDialog.__new__(AudioStudioDialog)
    view.closed = view.busy = False
    view.playback = Mock()
    view.playback.native = object()
    view.playback.poll.return_value = None
    view.playback.playlist_index = view.playback.ab_loop = view.playback.sleep_remaining_seconds = None
    view.playback.playlist = ()
    view._refresh_playback_choices = Mock()
    view.playback.status.return_value = PlaybackStatus(state=state,
        sample_rate=100, position_frames=position * 100, total_frames=total * 100,
        device_name='Selected synthetic output')
    view.playback_status, view.seek_seconds = Text(), Text('0')
    view._message, view._error = Mock(), Mock()
    view._last_playback_error = None
    return view


@pytest.mark.parametrize('state,method', [('playing', 'pause'), ('paused', 'resume')])
def test_pause_toggle_uses_the_current_worker_state(state, method):
    view = studio(state)
    view.pause_resume()
    getattr(view.playback, method).assert_called_once_with()
    view._error.assert_not_called()


@pytest.mark.parametrize('position,delta,expected', [(4, -10, 0), (74, 10, 80), (40, -10, 30)])
def test_relative_navigation_clamps_to_the_real_audio_duration(position, delta, expected):
    view = studio(position=position)
    view.seek_relative(delta)
    view.playback.seek.assert_called_once_with(expected)
    assert float(view.seek_seconds.GetValue()) == expected


def test_invalid_seek_text_does_not_reach_the_player():
    view = studio()
    view.seek_seconds.ChangeValue('not a number')
    view.seek_playback()
    view.playback.seek.assert_not_called()
    assert isinstance(view._error.call_args.args[0], ValueError)


def test_timer_updates_position_without_focus_or_duplicate_error_announcements():
    view = studio()
    view.refresh_playback()
    view.refresh_playback()
    assert len(view.playback_status.changes) == 1
    assert '00:00:40 / 00:01:20' in view.playback_status.GetValue()
    assert 'Selected synthetic output' in view.playback_status.GetValue()
    view._message.assert_not_called()
    view.playback.status.return_value = replace(view.playback.status.return_value,
                                               state='error', error='Synthetic device disconnected')
    view.refresh_playback()
    view.refresh_playback()
    view._message.assert_called_once_with('Synthetic device disconnected')
    view.read_playback_status()
    assert view._message.call_args.kwargs == {'focus': True}


def test_failed_playback_cleanup_cannot_start_a_render_or_leave_the_ui_busy():
    view = studio()
    view.executor, view.cancelled = Mock(), Mock()
    view.playback.stop.side_effect = RuntimeError('Synthetic driver retained its buffers')
    action = Mock()
    view._run('Render', action, Mock())
    assert not view.busy and not view.closed
    view.executor.submit.assert_not_called()
    action.assert_not_called()
    view.cancelled.clear.assert_not_called()
    assert 'retained' in str(view._error.call_args.args[0])


def test_failed_close_keeps_dialog_and_recovery_timer_available():
    view = studio()
    view._unsaved = Mock(return_value=True)
    view.timer, view.playback_timer, view.dialog = Mock(), Mock(), Mock()
    view.playback.close.side_effect = RuntimeError('Synthetic device did not close')
    event = Mock()
    event.CanVeto.return_value = True
    view.close(event)
    assert not view.closed
    event.Veto.assert_called_once_with()
    view.timer.Stop.assert_not_called()
    view.playback_timer.Stop.assert_not_called()
    view.dialog.Hide.assert_not_called()


def test_new_project_cannot_replace_context_while_the_previous_driver_is_busy():
    view = studio()
    view._unsaved = Mock(return_value=True)
    view.controller = Mock()
    view.playback.stop.side_effect = RuntimeError('Synthetic previous output is still active')
    view.new()
    view.controller.new.assert_not_called()
    assert 'still active' in str(view._error.call_args.args[0])


def test_play_uses_pcm_master_and_the_explicit_selected_output_device(tmp_path):
    view = studio()
    view.editing_dirty = False
    view.controller = SimpleNamespace(result={'output': str(tmp_path / 'book.mp3')})
    view.playback_device = Mock()
    view.playback_device.GetSelection.return_value = 1
    view.playback_device_ids = [None, 7]
    view.play()
    view.playback.play.assert_called_once_with(tmp_path / 'master.wav', device_id=7)


def test_existing_external_player_reports_playing_completion_and_failure_truthfully():
    player = LocalPlayback.__new__(LocalPlayback)
    player.native = None
    player._external_status = PlaybackStatus(state='playing', position_accuracy='external_player')
    player.process = Mock()
    player.process.poll.return_value = None
    assert player.status().state == 'playing'
    player.process.poll.return_value = 0
    player.process.returncode = 0
    assert player.status().state == 'completed'
    player.process.returncode = 5
    assert player.status().state == 'error'
    assert 'code 5' in player.status().error


def test_timer_polls_queue_and_exposes_repeat_and_sleep_without_taking_focus():
    view = studio()
    view.playback.poll.return_value = 'next'
    view.playback.playlist_index = 1
    view.playback.playlist = (Path('first.wav'), Path('second.wav'))
    view.playback.ab_loop = (20, 50)
    view.playback.sleep_remaining_seconds = 12
    view.refresh_playback()
    text = view.playback_status.GetValue()
    assert 'Playlist 2 of 2' in text and 'Repeating 20.00 to 50.00' in text and 'Sleep in 12' in text
    view._refresh_playback_choices.assert_called_once_with()
    view._message.assert_not_called()


def test_playlist_poll_failure_is_visible_once_without_escaping_timer():
    view = studio()
    view.playback.poll.side_effect = ValueError('The queued recording changed')
    view.refresh_playback()
    view.refresh_playback()
    assert 'queued recording changed' in view.playback_status.GetValue()
    view._message.assert_called_once_with('The queued recording changed')



@pytest.mark.parametrize('name', ['new', 'open', 'recover', 'import_text'])
def test_project_replacement_waits_for_owned_playback_cleanup(tmp_path, name):
    view = studio()
    original = tmp_path / 'original.txt'
    original.write_bytes(b'Public synthetic original.\r\n')
    view._unsaved = Mock(return_value=True)
    view._file = Mock(return_value=original)
    view.editing_dirty = False
    view.controller = Mock()
    view.controller.loading_budget = 1024
    view.playback.stop.side_effect = RuntimeError('Synthetic driver still owns playback.')
    getattr(view, name)()
    view.controller.new.assert_not_called()
    view.controller.open.assert_not_called()
    view.controller.recover.assert_not_called()
    assert original.read_bytes() == b'Public synthetic original.\r\n'
    assert 'still owns playback' in str(view._error.call_args.args[0])


def test_playback_shortcuts_cannot_start_media_beside_a_studio_job():
    view = studio()
    view.busy = True
    view.progress_view = Mock()
    action = Mock()
    view._playback_action(action)
    action.assert_not_called()
    view.progress_view.set_media_active.assert_not_called()
    assert 'current studio operation' in str(view._error.call_args.args[0])


def test_native_cleanup_failure_keeps_progress_suppression_owned():
    view = studio(state='error')
    view.progress_view = Mock()
    view.playback.native = SimpleNamespace(_cleanup_error='Synthetic retained native buffers')
    view._sync_playback_media()
    view.progress_view.set_media_active.assert_called_once_with(True)
