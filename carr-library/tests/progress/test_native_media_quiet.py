"""Canonical quiet/terminal guarantees on the retained shared ProgressHub.

Actual task/feedback/preference code runs; native widgets and the owned voice
device are explicit doubles. No screen-reader or audio-listening claim is made.
"""
from dataclasses import replace
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
from carr_library.ui.progress_media import ProgressMedia
from carr_library.ui.progress_panel import NativeProgressPanel


def eventually(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, 'The owned notification did not settle.'
        Event().wait(.005)


class Voice:
    def __init__(self, root):
        self.library_root = root
        self.entered, self.release = Event(), Event()
        self.heard, self.calls = [], []
        self.closed, self.busy, self.uncertain = False, False, False

    @property
    def quiescent(self):
        return not self.busy and not self.uncertain

    def speak_progress(self, text, *, volume, cancelled):
        self.busy = True
        self.calls.append(text)
        self.entered.set()
        try:
            assert self.release.wait(3), 'The fixture did not release owned speech.'
            if not self.closed and not cancelled():
                self.heard.append(text)
        finally:
            self.busy = False

    def close(self):
        self.closed = True


class Field:
    def __init__(self, value=''):
        self.value = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value


@pytest.fixture
def panel(tmp_path):
    view = object.__new__(NativeProgressPanel)
    clock = [0.0]
    voice = Voice(tmp_path)
    view.frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), working=False, IsActive=lambda: True)
    view.wx = SimpleNamespace(CallAfter=lambda callback: callback())
    view.hub, view.active_task = ProgressHub(), None
    view.closed, view.notice = False, ''
    view._last_revision = view._reporting_failure = None
    view._fixed_gauge_name = None
    view._retained_providers = []
    view._gauge_value, view._gauge_indeterminate, view._gauge_name = 0, False, ''
    view.gauge, view.timer, view.text, view.mute = Mock(), Mock(), Field(), Field(False)
    view.tone_sink, view.speech_provider = None, voice
    view.preferences = ProgressPreferences(mode='speech', volume=25)
    view.store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    view.store.save(view.preferences)
    view.feedback = ProgressFeedback(view.preferences, speech_provider=voice, clock=lambda: clock[0])
    view.media = ProgressMedia(view)
    view.fixture_clock, view.fixture_voice = clock, voice
    try:
        yield view
    finally:
        voice.release.set()
        view.close()
        if view.feedback._worker is not None:
            view.feedback._worker.join(3)
            assert not view.feedback._worker.is_alive()


@pytest.mark.parametrize('ending,label', [('succeeded', 'Completed successfully'),
    ('failed', 'Failed'), ('cancelled', 'Cancelled'), ('paused', 'Paused')])
def test_terminal_notice_is_retained_while_owned_running_speech_drains(panel, ending, label):
    task = panel.begin('First operation', total=2)
    voice = panel.fixture_voice
    assert voice.entered.wait(1)
    panel.fixture_clock[0] = 1.0
    if ending == 'succeeded':
        task.update(completed=2)
    panel.finish(task, ending)
    assert panel.feedback._pending is not None and voice.heard == []
    voice.release.set()
    eventually(lambda: panel.feedback.quiescent)
    # Preserve the canonical final-cue guarantee and the current stronger
    # rule: a real state change cancels obsolete running speech immediately.
    assert len(voice.calls) == 2
    assert len(voice.heard) == 1 and label in voice.heard[-1]
    assert ('100 percent' in voice.heard[-1]) is (ending == 'succeeded')


@pytest.mark.parametrize('terminal_pending', [False, True])
def test_new_operation_discards_old_active_and_deferred_notices(panel, terminal_pending):
    task = panel.begin('Superseded operation', total=1)
    voice = panel.fixture_voice
    assert voice.entered.wait(1)
    if terminal_pending:
        panel.fixture_clock[0] = 1.0
        task.update(completed=1)
        panel.finish(task, 'succeeded')
    panel.fixture_clock[0] = 8.0
    current = panel.begin('Current operation', total=3)
    voice.release.set()
    eventually(lambda: panel.feedback.quiescent)
    assert current is panel.active_task
    assert len(voice.heard) == 1 and 'Current operation' in voice.heard[0]
    assert not any('Superseded' in value for value in voice.heard)


def test_quiet_toggle_before_next_timer_discards_a_deferred_final_notice(panel):
    task = panel.begin('Completed while muted', total=1)
    voice = panel.fixture_voice
    assert voice.entered.wait(1)
    panel.fixture_clock[0] = 1.0
    task.update(completed=1)
    panel.finish(task, 'succeeded')
    assert panel.feedback._pending is not None
    panel.save_preferences(replace(panel.preferences, muted=True))
    panel.save_preferences(replace(panel.preferences, muted=False))
    voice.release.set()
    eventually(lambda: panel.feedback.quiescent)
    panel.fixture_clock[0] = 10.0
    panel.refresh()
    assert panel.feedback._pending is None and voice.heard == []


def test_root_change_discards_pending_final_even_when_new_root_enables_speech(panel, tmp_path):
    task = panel.begin('Old library', total=1)
    voice = panel.fixture_voice
    assert voice.entered.wait(1)
    panel.fixture_clock[0] = 1.0
    task.update(completed=1)
    panel.finish(task, 'succeeded')
    root = tmp_path / 'second-library'
    root.mkdir()
    ProgressPreferenceStore(root / 'progress-preferences.json').save(panel.preferences)
    panel.frame.library.root = root
    panel.change_root(root)
    voice.release.set()
    eventually(lambda: panel.feedback.quiescent)
    panel.fixture_clock[0] = 10.0
    panel.refresh()
    assert panel.speech_provider is None and panel.active_task is None
    assert panel.feedback.preferences.mode == 'off' and voice.heard == []


def test_independent_recording_holds_never_unmute_each_other(panel):
    panel.preferences = replace(panel.preferences, quiet_during_media=False)
    first, second = panel.hold_audio(recording=True), panel.hold_audio(recording=True)
    assert panel.media.flags()[0]
    first.close()
    assert panel.media.flags()[0] and second.ready
    second.close()
    assert not panel.media.flags()[0]


@pytest.mark.parametrize('observed', ['unknown', 'recording', 'monitoring'])
def test_actual_player_uncertainty_and_capture_ignore_playback_opt_out(panel, observed):
    class Player:
        state = observed
    player = Player()
    panel.preferences = replace(panel.preferences, quiet_during_media=False)
    panel.register_player(player)
    assert panel.media.flags()[0]
    panel.begin('Silent observed capture', total=2)
    assert panel.feedback._pending is None and panel.fixture_voice.calls == []


def test_unconfirmed_close_retains_each_player_until_its_own_silence(panel):
    class Player:
        state = 'playing'
    first, second = Player(), Player()
    for player in (first, second):
        panel.register_player(player)
        panel.unregister_player(player, silence_confirmed=False)
    assert panel.media.flags()[0]
    first.state = 'stopped'
    panel.refresh_audio_state()
    assert panel.media.flags()[0] and second in panel.media.uncertain
    second.state = 'paused'
    panel.refresh_audio_state()
    assert not panel.media.flags()[0] and not panel.media.uncertain
