"""Actual progress handlers with queued workers; no native/NVDA claim."""
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressSettings, ProgressSettingsStore, ProgressTonePolicy, ProgressTracker
from carr_library.ui import progress_view as ui
from carr_library.ui.audio_studio import AudioStudioDialog


def view(tmp_path):
    value = ui.ProgressView.__new__(ui.ProgressView)
    now = [0.0]
    value.clock = lambda: now[0]
    value.test_time = now
    value.tone_sender = Mock()
    value.closed, value.tracker, value.audible = False, None, True
    value.settings, value.notice = ProgressSettings(), ''
    value.policy = ProgressTonePolicy(clock=value.clock)
    value._last_refresh, value._last_text, value._last_value = float('-inf'), '', None
    value._pending, value._pending_lock = None, Lock()
    value._audio_owner, value._media_active, value._active = object(), False, True
    value.root = lambda: tmp_path
    value.gauge, value.status, value.panel, value.timer = Mock(), Mock(), Mock(), Mock()
    value.panel.GetTopLevelParent.return_value.IsActive.return_value = True
    value.wx = object()
    return value


def test_actual_view_throttles_updates_without_moving_focus_and_caps_unverified_counter(tmp_path):
    widget = view(tmp_path)
    tracker = widget.track(ProgressTracker('Import', total=4, unit='files'))
    widget.gauge.SetValue.assert_called_once_with(0)
    widget.status.SetFocus.assert_not_called()
    tracker.update(completed=1, total=4)
    widget.test_time[0] = .1
    assert widget.refresh() is False
    widget.gauge.SetValue.assert_called_once_with(0)
    widget.test_time[0] = .25
    assert widget.refresh()
    assert widget.gauge.SetValue.call_args.args == (25,)
    for done in (2, 3, 4):
        tracker.update(completed=done, total=4)
        widget.test_time[0] += .25
        widget.refresh()
    assert [call.args[0] for call in widget.gauge.SetValue.call_args_list] == [0, 25, 50, 75, 99]
    assert '4 of 4 files' in widget.status.ChangeValue.call_args.args[0]
    tracker.finish(verified=True)
    widget.refresh(force=True)
    assert widget.gauge.SetValue.call_args.args == (100,)
    widget.status.SetFocus.assert_not_called()
    widget.query()
    widget.status.SetFocus.assert_called_once()


def test_unknown_phase_pulses_but_cancel_stops_animation_without_100(tmp_path):
    widget = view(tmp_path)
    tracker = widget.track(ProgressTracker('Rendering'))
    assert widget.gauge.Pulse.call_count == 1
    tracker.update(phase='Verify final WAV')
    widget.test_time[0] = .25
    widget.refresh()
    assert 'Verify final WAV' in widget.status.ChangeValue.call_args.args[0]
    tracker.finish('cancelled')
    widget.refresh(force=True)
    count = widget.gauge.Pulse.call_count
    widget.test_time[0] = 2
    widget.refresh()
    assert widget.gauge.Pulse.call_count == count
    assert widget.gauge.SetValue.call_args.args == (0,)
    assert 'Cancelled' in widget.status.ChangeValue.call_args.args[0]


def test_worker_mailbox_coalesces_large_burst_and_close_discards_late_events(tmp_path):
    widget = view(tmp_path)
    applied = []
    for number in range(10000):
        widget.defer(lambda value=number: applied.append(value))
    assert applied == []
    widget.refresh(force=True)
    assert applied == [9999]
    widget.defer(lambda: applied.append('stale'))
    widget.close()
    widget.defer(lambda: applied.append('late'))
    assert not widget.refresh(force=True)
    assert applied == [9999]
    widget.timer.Stop.assert_called_once()


@pytest.mark.parametrize('replacement', [ProgressSettings(mode='beep', muted=True),
                                       ProgressSettings(mode='beep', volume=0)])
def test_queued_tone_is_invalid_after_progress_replaced_or_muted(tmp_path, replacement):
    ProgressSettingsStore(tmp_path).save(ProgressSettings(mode='beep'))
    widget = view(tmp_path)
    tracker = widget.track(ProgressTracker('First', total=4))
    valid = widget.tone_sender.call_args.args[2]
    assert valid()
    tracker.update(completed=1, total=4)
    assert not valid()
    widget.track(ProgressTracker('Second'))
    current_valid = widget.tone_sender.call_args.args[2]
    assert current_valid()
    widget.settings = replacement
    assert not current_valid()
    widget.close()


def test_settings_dialog_reloads_persisted_values_before_first_operation_and_after_root_change(tmp_path, monkeypatch):
    first, second = tmp_path / 'one', tmp_path / 'two'
    first.mkdir()
    second.mkdir()
    one, two = ProgressSettings('beep', 28, 7), ProgressSettings('off', 65, 11, True, True)
    ProgressSettingsStore(first).save(one)
    ProgressSettingsStore(second).save(two)
    widget = view(first)
    dialog = Mock(return_value=None)
    monkeypatch.setattr(ui, 'show_progress_settings', dialog)
    widget.configure()
    assert dialog.call_args.args[1:] == (first, one)
    widget.root = lambda: second
    widget.configure()
    assert dialog.call_args.args[1:] == (second, two)
    assert ProgressSettingsStore(first).load() == one
    assert ProgressSettingsStore(second).load() == two


def studio(tmp_path):
    value = AudioStudioDialog.__new__(AudioStudioDialog)
    value.busy = value.closed = value.close_pending = False
    value.cancelled = Event()
    value.playback = Mock()
    value.controls, value.cancel_button = [Mock(), Mock()], Mock()
    value._message, value._error, value.close = Mock(), Mock(), Mock()
    value.executor = Mock()
    value.future = Future()
    value.executor.submit.return_value = value.future
    value.pending = []
    value.wx = SimpleNamespace(CallAfter=value.pending.append)
    value.progress_view = view(tmp_path)
    return value


@pytest.mark.parametrize('ending', ['accepted', 'worker-error', 'handler-error', 'cancelled'])
def test_audio_job_progress_changes_only_after_queued_result_is_accepted(tmp_path, ending):
    dialog = studio(tmp_path)
    tracker = ProgressTracker('Rendering')
    accepted = []

    def done(result):
        accepted.append(result)
        assert tracker.snapshot().percent is None
        if ending == 'handler-error':
            raise ValueError('result not accepted')

    dialog._run('Render', lambda: None, done, progress_tracker=tracker)
    assert dialog.busy and tracker.snapshot().percent is None
    if ending == 'worker-error':
        dialog.future.set_exception(OSError('output failed'))
    else:
        if ending == 'cancelled':
            dialog.cancelled.set()
        dialog.future.set_result('render-result')
    assert dialog.busy and tracker.snapshot().percent is None
    assert len(dialog.pending) == 1
    dialog.pending.pop()()
    assert not dialog.busy
    snapshot = tracker.snapshot()
    assert snapshot.state == {'accepted': 'completed', 'worker-error': 'failed',
                              'handler-error': 'failed', 'cancelled': 'cancelled'}[ending]
    assert (snapshot.percent == 100) is (ending == 'accepted')
    dialog.cancel_button.Disable.assert_called()


def test_audio_submit_error_restores_controls_and_retains_failed_progress(tmp_path):
    dialog = studio(tmp_path)
    dialog.executor.submit.side_effect = RuntimeError('executor stopped')
    done = Mock()
    dialog._run('Render', lambda: None, done)
    assert not dialog.busy
    assert dialog.progress_view.tracker.snapshot().state == 'failed'
    assert dialog.progress_view.tracker.snapshot().percent != 100
    done.assert_not_called()
    for control in dialog.controls:
        control.Enable.assert_called_once()


def test_render_status_is_shared_indeterminate_and_never_enqueues_per_chunk_ui_callbacks(tmp_path):
    dialog = studio(tmp_path)
    dialog._capture, dialog.save = Mock(), Mock(return_value=True)
    dialog.plan_text, dialog.notebook = Mock(), Mock()
    captured = {}
    dialog.controller = SimpleNamespace(output_dir=tmp_path, preview=lambda: 'Synthetic plan',
                                        render=lambda **kwargs: captured.update(kwargs))
    dialog.render()
    action, = dialog.executor.submit.call_args.args
    action()
    for number in range(10000):
        captured['on_status'](f'Chunk {number}; final mix not yet verified')
    assert dialog.pending == []
    snapshot = dialog.progress_view.tracker.snapshot()
    assert snapshot.phase == 'Chunk 9999; final mix not yet verified'
    assert snapshot.percent is None and snapshot.eta_seconds is None


def test_playback_suppresses_tones_before_native_start_and_releases_on_stop_or_error(tmp_path):
    dialog = studio(tmp_path)
    dialog.controller = SimpleNamespace(result={'output': str(tmp_path / 'master.wav')})
    dialog.editing_dirty = False
    admitted = []
    from carr_library.audio.playback import PlaybackStatus
    from dataclasses import replace
    state = [PlaybackStatus()]
    dialog.playback.status.side_effect = lambda: state[0]
    dialog.playback.poll.return_value = None
    dialog.playback.playlist_index = dialog.playback.ab_loop = dialog.playback.sleep_remaining_seconds = None
    dialog.playback.native = SimpleNamespace(_cleanup_error=None)
    dialog.playback_status = Mock()
    dialog.playback_status.GetValue.return_value = ''
    dialog._last_playback_error = None
    dialog._refresh_playback_choices = Mock()
    dialog.playback_device = Mock()
    dialog.playback_device.GetSelection.return_value = 0
    dialog.playback_device_ids = [None]

    def playing(path, *, device_id):
        admitted.append(dialog.progress_view._media_active)
        state[0] = PlaybackStatus(state='playing', sample_rate=8000, total_frames=8000)

    def stopped():
        state[0] = replace(state[0], state='stopped')

    dialog.playback.play.side_effect = playing
    dialog.playback.stop.side_effect = stopped
    dialog.play()
    assert admitted == [True]
    assert dialog.progress_view._media_active
    # Rejecting an invalid new request must not unmute a still-playing preview.
    dialog.editing_dirty = True
    dialog.play()
    assert dialog.progress_view._media_active
    dialog.stop_playback()
    assert not dialog.progress_view._media_active
    dialog.editing_dirty = False
    dialog.playback.play.side_effect = OSError('device unavailable')
    dialog.play()
    assert not dialog.progress_view._media_active



def test_native_playback_poll_releases_tone_broker_only_after_output_stops(tmp_path):
    from carr_library.audio.playback import PlaybackStatus
    from dataclasses import replace
    dialog = studio(tmp_path)
    dialog.playback.native = SimpleNamespace(_cleanup_error=None)
    dialog.playback.poll.return_value = None
    dialog.playback.playlist_index = dialog.playback.ab_loop = dialog.playback.sleep_remaining_seconds = None
    dialog.playback_status = Mock()
    dialog.playback_status.GetValue.return_value = ''
    dialog._last_playback_error = None
    playing = PlaybackStatus(state='playing', sample_rate=8000, total_frames=8000)
    for state in ('starting', 'playing', 'paused'):
        dialog.playback.status.return_value = replace(playing, state=state)
        dialog.refresh_playback()
        assert dialog.progress_view._media_active
    for state in ('completed', 'stopped'):
        dialog.playback.status.return_value = replace(playing, state=state)
        dialog.refresh_playback()
        assert not dialog.progress_view._media_active
