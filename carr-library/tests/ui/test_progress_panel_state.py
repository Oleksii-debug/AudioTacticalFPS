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
    panel.closed = False
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
