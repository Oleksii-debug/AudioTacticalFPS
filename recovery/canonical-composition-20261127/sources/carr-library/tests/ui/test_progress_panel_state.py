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


class RetryableVoice:
    def __init__(self, root, *, failure=None):
        self.library_root = root
        self.label = 'Synthetic local progress voice'
        self.failure = failure
        self.calls = 0
        self.ready = failure is None
        self.quiescent = True

    def speak_progress(self, text, *, volume, cancelled):
        if self.calls:
            raise AssertionError('The accepted voice was closed before use.')
        return text, volume, cancelled()

    def close(self):
        self.calls += 1
        if not self.ready:
            if self.failure == 'raises':
                raise OSError('Previous voice still owns its native worker')
            return False
        return True


def ownership_panel(tmp_path, voice):
    from types import SimpleNamespace
    panel = object.__new__(NativeProgressPanel)
    panel.frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path))
    panel.store = ProgressPreferenceStore(tmp_path / 'progress-preferences.json')
    panel.preferences = ProgressPreferences()
    panel.feedback = ProgressFeedback(panel.preferences, speech_provider=voice)
    panel.tone_sink, panel.speech_provider = None, voice
    panel.notice, panel.closed = '', False
    panel.refresh_audio_state = Mock()
    panel.timer, panel.media = Mock(), Mock()
    return panel


def defer_cleanup_threads(monkeypatch):
    from carr_library.audio import cleanup as cleanup_module
    monkeypatch.setattr(cleanup_module.threading.Thread, 'start',
                        Mock(side_effect=RuntimeError('Synthetic retry-thread admission failure')))


def test_progress_voice_admission_error_keeps_existing_owner_and_rejects_new_root(tmp_path):
    import pytest
    previous = RetryableVoice(tmp_path)
    candidate = RetryableVoice(tmp_path)
    panel = ownership_panel(tmp_path, previous)
    other = tmp_path / 'another-library'
    other.mkdir()
    with pytest.raises(ValueError, match='library folder changed'):
        panel.set_speech_provider(candidate, root=other)
    assert panel.speech_provider is previous and panel.feedback.speech_provider is previous
    assert previous.calls == candidate.calls == 0
    panel.close()


def test_progress_voice_post_adoption_refresh_failure_does_not_close_accepted_owner(tmp_path):
    previous, candidate = RetryableVoice(tmp_path), RetryableVoice(tmp_path)
    panel = ownership_panel(tmp_path, previous)
    panel.refresh_audio_state.side_effect = RuntimeError('Deleted optional progress control')
    assert panel.set_speech_provider(candidate, root=tmp_path) is candidate
    assert panel.speech_provider is candidate and panel.feedback.speech_provider is candidate
    assert previous.calls == 1 and candidate.calls == 0
    assert candidate.speak_progress('Ready', volume=0, cancelled=lambda: False) == ('Ready', 0, False)
    assert 'selected' in panel.notice and 'refresh' in panel.notice
    panel.close()
    assert candidate.calls == 1


def test_progress_voice_failed_retired_owner_is_retained_until_confirmed_retry(tmp_path, monkeypatch):
    defer_cleanup_threads(monkeypatch)
    previous, candidate = RetryableVoice(tmp_path, failure='false'), RetryableVoice(tmp_path)
    panel = ownership_panel(tmp_path, previous)
    assert panel.set_speech_provider(candidate, root=tmp_path) is candidate
    assert panel.speech_provider is candidate and candidate.calls == 0
    assert len(panel._speech_cleanup_owners) == 1
    owner = panel._speech_cleanup_owners[0]
    try:
        assert not owner.quiescent and previous.calls == 1
        assert owner.close() is False
        assert not owner.quiescent and panel._speech_cleanup_owners == [owner]
        previous.ready = True
        assert owner.close() is True
        assert owner.quiescent and not panel._speech_cleanup_owners
        assert previous.calls == 3 and candidate.calls == 0
    finally:
        previous.ready = True
        owner.close()
        panel.close()


def test_progress_close_keeps_failed_voice_owner_and_still_closes_media_observer(tmp_path, monkeypatch):
    defer_cleanup_threads(monkeypatch)
    voice = RetryableVoice(tmp_path, failure='raises')
    panel = ownership_panel(tmp_path, voice)
    panel.close()
    panel.close()
    assert panel.closed and panel.feedback._closed
    panel.timer.Stop.assert_called_once_with()
    panel.media.close.assert_called_once_with()
    assert len(panel._speech_cleanup_owners) == 1
    owner = panel._speech_cleanup_owners[0]
    try:
        assert not owner.quiescent and voice.calls == 1
        voice.ready = True
        assert owner.close() is True
        assert not panel._speech_cleanup_owners and voice.calls == 2
    finally:
        voice.ready = True
        owner.close()


def test_progress_admission_failure_without_a_task_keeps_diagnostic_available(tmp_path):
    panel = ownership_panel(tmp_path, None)
    panel.active_task = None
    panel._reporting_failure = None
    panel.refresh = Mock()
    panel.reporting_unavailable(None, 'failed', 'Progress admission was unavailable; the business result is retained.')
    assert panel._reporting_failure is None
    assert 'business result is retained' in panel.notice
    panel.refresh.assert_called_once_with()
    panel.close()
