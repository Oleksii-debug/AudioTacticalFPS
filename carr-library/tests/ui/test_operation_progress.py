"""Retained native progress behavior on the canonical shared progress contract.

The retired OperationProgress tracker is not restored. Its measured-unit/ETA,
PCM, preferences and device cases are covered by tests/progress/test_progress_*
and tests/ui/test_progress_media_quiet.py. These native-observer regressions
retain focus, finalization, stale-task and root-switch boundaries here.
"""
from dataclasses import replace
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressError, ProgressHub
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
from carr_library.ui.progress_media import ProgressMedia
from carr_library.ui.progress_panel import NativeProgressPanel


class Field:
    def __init__(self, value=''):
        self.value, self.focuses, self.changes = value, 0, []
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value; self.changes.append(value)
    ChangeValue = SetValue
    def SetInsertionPoint(self, value): self.insertion = value
    def SetFocus(self): self.focuses += 1


@pytest.fixture
def native(tmp_path):
    panel = object.__new__(NativeProgressPanel)
    panel.frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), working=False,
                                  IsActive=lambda: True)
    callbacks = []
    panel.wx = SimpleNamespace(CallAfter=callbacks.append)
    panel.hub, panel.active_task = ProgressHub(), None
    panel.closed, panel.notice = False, ''
    panel._last_revision = panel._reporting_failure = None
    panel._gauge_value, panel._gauge_indeterminate, panel._gauge_name = 0, False, ''
    panel.gauge, panel.timer, panel.text, panel.mute = Mock(), Mock(), Field(), Field(False)
    panel.preferences = ProgressPreferences()
    panel.store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    panel.tone_sink = panel.speech_provider = None
    panel.feedback = ProgressFeedback(panel.preferences)
    panel.media = ProgressMedia(panel)
    panel.callbacks = callbacks
    try:
        yield panel
    finally:
        panel.close()


def test_native_indeterminate_gauge_pulses_with_name_state_and_no_focus(native):
    task = native.begin('Inspecting source')
    native.refresh()
    assert native.gauge.Pulse.call_count >= 2
    assert 'indeterminate' in native.gauge.SetName.call_args.args[0]
    assert native.text.focuses == 0 and task.snapshot().percent is None
    task.update(completed=7, phase='Reading retained bytes')
    native.refresh()
    assert '7' in native.text.GetValue() and 'Reading retained bytes' in native.text.GetValue()
    assert task.snapshot().eta_seconds is None and native.text.focuses == 0


def test_native_known_units_keep_last_value_and_only_committed_completion_reaches_100(native):
    task = native.begin('Export audio', total=100)
    for completed in (25, 50, 75, 100):
        task.update(completed=completed)
        native.refresh()
        assert native.gauge.SetValue.call_args.args == (min(completed, 99),)
    assert task.snapshot().percent == 99 and native.text.focuses == 0
    native.finish(task, 'succeeded', 'Output verified and committed')
    assert task.snapshot().percent == 100
    assert native.gauge.SetValue.call_args.args == (100,)
    assert 'Completed successfully' in native.text.GetValue()


def test_finalize_without_measured_completion_cannot_announce_success(native):
    task = native.begin('Incomplete import', total=2)
    task.advance()
    with pytest.raises(ProgressError):
        native.finish(task, 'succeeded')
    native.reporting_unavailable(task, 'succeeded', 'Work returned; measurement is unavailable.')
    assert task.snapshot().percent != 100
    assert 'measurement is unavailable' in native.text.GetValue()


def test_worker_updates_are_coalesced_and_old_tasks_cannot_change_the_current_view(native):
    old = native.begin('Previous job', total=10)
    current = native.begin('Current job', total=100)
    before = len(native.text.changes)
    def work():
        for value in range(1, 76):
            current.update(completed=value)
        old.update(completed=10)
        old.fail('Old job failed')
    thread = Thread(target=work)
    thread.start(); thread.join(3)
    assert not thread.is_alive()
    assert len(native.text.changes) == before
    native.refresh()
    assert native.gauge.SetValue.call_args.args == (75,)
    assert 'Current job' in native.text.GetValue() and 'Old job failed' not in native.text.GetValue()
    assert native.text.focuses == 0


def test_explicit_progress_query_is_the_only_focus_action(native):
    native.begin('Current operation')
    native.refresh()
    assert native.text.focuses == 0
    native.query()
    assert native.text.focuses == 1 and native.text.insertion == 0


def test_root_switch_loads_independent_off_preference(native, tmp_path):
    native.save_preferences(replace(native.preferences, muted=True, volume=15))
    previous = native.store.path.read_bytes()
    target = tmp_path / 'another-library'; target.mkdir()
    native.frame.library.root = target
    native.change_root(target)
    assert native.active_task is None and native.preferences == ProgressPreferences()
    assert not native.store.path.exists()
    assert (tmp_path / 'progress-preferences.json').read_bytes() == previous


@pytest.mark.parametrize('state', ['stopped', 'paused', 'error'])
def test_player_eof_pause_and_device_error_release_observed_media_state(native, state):
    class Player:
        state = 'playing'
    player = Player()
    native.register_player(player)
    assert native.media.flags()[1]
    player.state = state
    native.refresh_audio_state()
    assert native.media.flags() == (False, False)


def test_worker_player_registration_and_removal_are_marshaled_to_ui(native):
    class Player:
        state = 'playing'
    player = Player()
    thread = Thread(target=lambda: native.register_player(player))
    thread.start(); thread.join(3)
    assert not thread.is_alive() and not native.media.flags()[1]
    native.callbacks.pop(0)()
    assert native.media.flags()[1]
    thread = Thread(target=lambda: native.unregister_player(player))
    thread.start(); thread.join(3)
    assert not thread.is_alive() and native.media.flags()[1]
    native.callbacks.pop(0)()
    assert native.media.flags() == (False, False)


@pytest.mark.parametrize('title', ['', 'A\tB', 'x' * 201, None])
def test_phase_names_are_readable_and_bounded(title):
    with pytest.raises(ProgressError):
        ProgressHub().start(title)
