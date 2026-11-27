"""Quick-mute recovery with real preference bytes and explicit control doubles."""
from threading import Event
from unittest.mock import Mock

from carr_library.progress import ProgressHub
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferenceError, ProgressPreferences, ProgressPreferenceStore
from carr_library.ui.progress_panel import NativeProgressPanel


class Checkbox:
    def __init__(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value


def panel_with_store(tmp_path, preferences, sink):
    panel = object.__new__(NativeProgressPanel)
    panel.preferences = preferences
    panel.store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    panel.store.save(preferences)
    panel.feedback = ProgressFeedback(preferences, tone_sink=sink)
    panel.tone_sink, panel.speech_provider = sink, None
    panel.mute = Checkbox(preferences.muted)
    panel.notice = ''
    panel.refresh = Mock()
    return panel


def test_mute_silences_the_session_even_when_preference_storage_fails(tmp_path, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    heard, cancellation_seen = [], []

    class Sink:
        def play(self, tone, *, cancelled):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('Quick-mute fixture did not release playback.')
            stopped = cancelled()
            cancellation_seen.append(stopped)
            if not stopped:
                heard.append(tone)
            finished.set()
    panel = panel_with_store(tmp_path, ProgressPreferences(mode='beep'), Sink())
    prior = panel.store.path.read_bytes()
    failure = Mock(side_effect=ProgressPreferenceError('Synthetic read-only settings'))
    monkeypatch.setattr(panel.store, 'save', failure)
    try:
        panel.feedback.update(ProgressHub().start('Current work', total=100).snapshot())
        assert entered.wait(1)
        panel.mute.SetValue(True)
        panel.toggle_mute()
        assert panel.preferences.muted and panel.feedback.preferences.muted and panel.mute.GetValue()
        assert 'Muted for this session' in panel.notice and 'could not be saved' in panel.notice
        assert panel.store.path.read_bytes() == prior
        assert not ProgressPreferenceStore(panel.store.path).load().muted
        failure.assert_called_once()
        release.set()
        assert finished.wait(2)
        assert cancellation_seen == [True] and heard == []
    finally:
        release.set()
        panel.feedback.close()


def test_failed_unmute_keeps_sound_muted_and_saved_preferences_unchanged(tmp_path, monkeypatch):
    sink = Mock()
    panel = panel_with_store(tmp_path, ProgressPreferences(mode='beep', muted=True), sink)
    prior = panel.store.path.read_bytes()
    failure = Mock(side_effect=ProgressPreferenceError('Synthetic full disk'))
    monkeypatch.setattr(panel.store, 'save', failure)
    try:
        panel.mute.SetValue(False)
        panel.toggle_mute()
        assert panel.preferences.muted and panel.feedback.preferences.muted and panel.mute.GetValue()
        assert 'remains muted' in panel.notice
        assert panel.store.path.read_bytes() == prior
        assert panel.feedback.update(ProgressHub().start('Still muted', total=1).snapshot()) is None
        sink.play.assert_not_called()
        failure.assert_called_once()
    finally:
        panel.feedback.close()



def test_context_name_survives_real_task_state_and_timer_updates():
    """The host label is independent of live native progress state."""
    from types import SimpleNamespace

    class Gauge:
        def __init__(self):
            self.name, self.value, self.pulses = '', 0, 0
        def SetName(self, name): self.name = name
        def SetValue(self, value): self.value = value
        def Pulse(self): self.pulses += 1

    class Text:
        value = ''
        def GetValue(self): return self.value
        def ChangeValue(self, value): self.value = value

    for context in ('Current operation progress', 'Recording operation progress',
                    'Audio Studio operation progress'):
        panel = object.__new__(NativeProgressPanel)
        panel.closed = False
        panel.hub = ProgressHub()
        panel.active_task = None
        panel.frame = SimpleNamespace(IsActive=lambda: True)
        panel.media = SimpleNamespace(flags=lambda: (False, False))
        panel.feedback = Mock(last_error='')
        panel.gauge, panel.text = Gauge(), Text()
        panel._gauge_name, panel._gauge_value = '', 0
        panel._gauge_indeterminate = False
        panel._last_revision = panel._reporting_failure = None
        panel.notice = ''
        panel.set_context_name(context)
        assert panel.gauge.name == context + ': idle'
        task = panel.begin('Actual recording task', total=100)
        task.update(completed=25)
        panel.refresh()  # Same method used by the native timer.
        assert panel.gauge.name == context + ': running'
        assert panel.gauge.value == 25
        panel.request_cancel()
        assert panel.gauge.name == context + ': cancelling'
        panel.finish(task, 'cancelled')
        assert panel.gauge.name == context + ': cancelled'
        unknown = panel.begin('Measured duration not known')
        panel.refresh()
        assert panel.gauge.name == context + ': indeterminate; running'
        assert panel.gauge.pulses > 0
        panel.finish(unknown, 'succeeded')
        panel.refresh()
        assert panel.gauge.name == context + ': succeeded'
        assert panel.gauge.value == 100
