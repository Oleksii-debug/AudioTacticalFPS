"""Controller boundaries for native reading; no device or synthesis claims."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading

import pytest

from carr_library.audio.reading import ReadingAudio
from carr_library.ui.reading_playback import ReadingPlaybackPane


class Value:
    def __init__(self, value):
        self.value = value
    def GetValue(self):
        return self.value
    def GetSelection(self):
        return self.value


class Artifact(ReadingAudio):
    def __init__(self):
        self.closed = False
        self.path = 'owned-reading.wav'
    def close(self):
        self.closed = True


def pane():
    value = ReadingPlaybackPane.__new__(ReadingPlaybackPane)
    value._closed = value._busy = False
    value._generation = 0
    value._cancel = threading.Event()
    value._start_token = value._start_timer = value._close_ready = None
    value._last_state = None
    value.root = 'private-app-root'
    value.renderer = None
    value.editor = Value('Edited reading copy; original is separate.')
    value.project_id = lambda: 'saved-reading-project'
    value.guard = lambda: None
    value.voice = Value(0)
    value.language, value.rate, value.volume = Value('en'), Value(0), Value(100)
    value.rows = [{'id': 'synthetic:one'}]
    value.controls = []
    value.audio = None
    value.events = []
    value.say = lambda message, **kw: value.events.append(message)
    value.session = SimpleNamespace(registry=lambda: object())
    value._stop_player = lambda: value.events.append('player-stopped')
    value.operations = []
    value._submit = lambda operation, done: value.operations.append((operation, done))
    value.played = []
    value._quiet_start = lambda *args: value.played.append(args)
    value.wx = SimpleNamespace(CallAfter=lambda callback, *args: callback(*args))
    return value


def test_read_all_passes_only_the_current_editable_copy(monkeypatch):
    import carr_library.ui.reading_playback as module
    control = pane()
    captured = []
    artifact = Artifact()
    monkeypatch.setattr(module, 'render_reading',
                        lambda root, text, **kwargs: captured.append((text, kwargs)) or artifact)
    control.read('all')
    operation, done = control.operations.pop()
    done(operation())
    assert captured[0][0] == control.editor.GetValue()
    assert control.audio is artifact and control.played and not artifact.closed


def test_edit_during_render_discards_obsolete_audio_before_playback(monkeypatch):
    import carr_library.ui.reading_playback as module
    control, artifact = pane(), Artifact()
    monkeypatch.setattr(module, 'render_reading', lambda *args, **kwargs: artifact)
    control.read('all')
    operation, done = control.operations.pop()
    result = operation()
    control.editor.value = 'A newer edit'
    done(result)
    assert artifact.closed and not control.played and control.audio is None


def test_revoked_workspace_guard_discards_prepared_audio(monkeypatch):
    import carr_library.ui.reading_playback as module
    control, artifact = pane(), Artifact()
    monkeypatch.setattr(module, 'render_reading', lambda *args, **kwargs: artifact)
    control.read('all')
    operation, done = control.operations.pop()
    result = operation()
    control.guard = lambda: (_ for _ in ()).throw(ValueError('workspace revoked'))
    with pytest.raises(ValueError, match='workspace revoked'):
        done(result)
    assert artifact.closed and not control.played


def test_explicit_stop_invalidates_the_queued_render_result(monkeypatch):
    import carr_library.ui.reading_playback as module
    control, artifact = pane(), Artifact()
    monkeypatch.setattr(module, 'render_reading', lambda *args, **kwargs: artifact)
    control.read('all')
    operation, done = control.operations.pop()
    result = operation()
    control._busy = True
    control.stop()
    done(result)
    assert artifact.closed and control._cancel.is_set() and not control.played


def test_close_waits_for_actual_future_and_discards_its_late_result():
    control, artifact = pane(), Artifact()
    control._busy = True
    closed = []
    assert control.request_close(lambda: closed.append(True)) is False
    assert not closed
    future = Future()
    future.set_result(artifact)
    ReadingPlaybackPane._finish(control, future, lambda value: pytest.fail('Close cannot start playback.'))
    assert artifact.closed and closed == [True] and not control._busy


def test_unconfirmed_player_cleanup_does_not_discard_owned_audio_or_close():
    control, artifact = pane(), Artifact()
    control.audio = artifact
    control._stop_player = lambda: (_ for _ in ()).throw(RuntimeError('device still owns buffers'))
    with pytest.raises(RuntimeError, match='still owns'):
        control.request_close(lambda: pytest.fail('Unconfirmed close'))
    assert control.audio is artifact and not artifact.closed


def test_edit_keeps_source_editable_when_player_cleanup_fails():
    control, artifact = pane(), Artifact()
    control.audio = artifact
    control._stop_player = lambda: (_ for _ in ()).throw(RuntimeError('device still owns buffers'))
    assert control.invalidate() is False
    assert not artifact.closed
    assert any('edit is preserved' in event for event in control.events)


def test_display_failure_does_not_erase_completed_audio():
    control, artifact = pane(), Artifact()
    control.audio = artifact
    control.say = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('closed observer'))
    control._say('Prepared')
    assert control.audio is artifact and not artifact.closed


def test_quiet_start_rechecks_source_and_never_starts_before_progress_quiesces(monkeypatch):
    import carr_library.ui.reading_playback as module
    control = pane()
    artifact = Artifact()
    control.audio = artifact
    control._quiet_start = ReadingPlaybackPane._quiet_start.__get__(control)
    starts, holds, timers = [], [], []
    control.player = SimpleNamespace(play=lambda path: starts.append(path))
    control._sync_media = lambda: holds.append('observed')
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module, 'set_media_active', lambda owner, active: holds.append(active))
    monkeypatch.setattr(module, 'progress_audio_quiescent', lambda: False)
    control.wx.CallLater = lambda delay, callback: timers.append(callback) or SimpleNamespace(Stop=lambda: None)
    control._quiet_start(control._signature(), control._generation)
    assert not starts and timers and holds == [True]
    now[0] = 4
    timers.pop()()
    assert not starts and control._start_token is None
    assert any('silence' in event for event in control.events)


def test_project_identity_change_discards_old_copy_without_rebinding(monkeypatch):
    import carr_library.ui.reading_playback as module
    control, artifact = pane(), Artifact()
    monkeypatch.setattr(module, 'render_reading', lambda *args, **kwargs: artifact)
    control.read('all')
    operation, done = control.operations.pop()
    result = operation()
    control.project_id = lambda: 'another-reading-project'
    done(result)
    assert artifact.closed and not control.played
