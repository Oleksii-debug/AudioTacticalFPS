"""Real clip controller closures keep saved selections and native owner lifetime."""
from contextlib import contextmanager
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.audio import Effect
from carr_library.studio.project import Cue, Marker, Project, Role, Scene


@pytest.fixture
def dialog(tmp_path):
    namespace = runpy.run_path(str(Path(__file__).with_name('test_clip_dialog.py')))
    value = namespace['dialog'].__wrapped__(tmp_path)
    value.editor = SimpleNamespace(close=Mock(return_value=True))
    return value


def selected(dialog, tmp_path):
    cue = Cue('clip', str(tmp_path / 'selected.wav'), duration=1)
    other = Cue('other', str(tmp_path / 'second.wav'), start=1, duration=1)
    scene = Scene('scene', 'Scene', (), cues=(cue, other), markers=(Marker('marker', .25, 'Keep'),))
    project = Project('project', 'Project', (Role('role', 'Voice'),), (scene,), schema=3)
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    dialog.range_start = SimpleNamespace(GetValue=lambda: '.25')
    dialog.range_end = SimpleNamespace(GetValue=lambda: '.75')
    dialog.fields = {'start': SimpleNamespace(GetValue=lambda: '2')}
    dialog.marker_choice = SimpleNamespace(GetSelection=lambda: 0)
    return project, scene, cue


@pytest.mark.parametrize('name', ['copy_range', 'cut_range', 'trim_range', 'silence_range'])
def test_range_command_binds_reviewed_project_and_timecodes_before_queue(dialog, tmp_path, name):
    project, scene, cue = selected(dialog, tmp_path)
    method = Mock(return_value=project)
    setattr(dialog.editor, name, method)
    getattr(dialog, name)()
    dialog.project = None
    dialog.range_start.GetValue = lambda: pytest.fail('Worker read changed range start')
    dialog.range_end.GetValue = lambda: pytest.fail('Worker read changed range end')
    dialog._scene = lambda: pytest.fail('Worker read changed scene')
    dialog.finish()
    assert method.call_args.args == (project, scene.id, cue.id, '.25', '.75')
    assert method.call_args.kwargs['cancelled'].__self__ is dialog
    if name == 'copy_range':
        dialog._accept.assert_not_called()
    else:
        dialog._accept.assert_called_once_with(project)


@pytest.mark.parametrize('name', ['paste_clip', 'split_clip', 'join_clips', 'join_rendered',
                                  'crossfade', 'add_marker', 'edit_marker', 'role_effects'])
def test_new_nested_modal_actions_do_not_access_deleted_owner_or_publish(dialog, tmp_path, name):
    selected(dialog, tmp_path)
    wx = dialog.new.__globals__['wx']
    wx.ID_OK = 1
    peers, forbidden = [], []
    class Peer:
        def __init__(self, *args, **kwargs):
            self.live = True
            peers.append(self)
        def __bool__(self):
            return self.live
        def ShowModal(self):
            event = SimpleNamespace(GetId=lambda: dialog._native_id, Skip=lambda: None)
            dialog.on_destroy(event)
            self.live = False
            dialog.status.ChangeValue.side_effect = lambda *args: forbidden.append('dead status')
            dialog.status.SetFocus.side_effect = lambda *args: forbidden.append('dead focus')
            return wx.ID_OK
        def GetValue(self):
            forbidden.append('dead chooser text')
            return '.5'
        def GetSelection(self):
            forbidden.append('dead chooser selection')
            return 0
        def Destroy(self):
            forbidden.append('dead chooser destruction')
    wx.TextEntryDialog = wx.SingleChoiceDialog = Peer
    getattr(dialog, name)()
    assert len(peers) == 1 and dialog.closed
    assert not forbidden and not dialog.calls and not dialog.pending
    dialog._accept.assert_not_called()


def test_clip_effect_review_preserves_shared_admission_and_two_original_scope_bindings(dialog, tmp_path, monkeypatch):
    from carr_library.ui import effects_dialog
    project, scene, cue = selected(dialog, tmp_path)
    changes = (Effect('gain', gain_db=-6),)
    review = Mock(return_value=changes)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', review)
    admitted = []
    @contextmanager
    def admission(runtime, **kwargs):
        admitted.append(kwargs)
        yield kwargs['cancelled']
    runtime = SimpleNamespace(capabilities=lambda *, cancelled: (
        {'effects': ['gain', 'tempo']} if not cancelled() else pytest.fail('Unexpected cancellation')))
    method = Mock(return_value=project)
    dialog.editor = SimpleNamespace(_current=Mock(return_value=project), set_effects=method,
        close=Mock(return_value=True), service=SimpleNamespace(
            _runtime=lambda: runtime, _audio_admission=admission))
    dialog.clip_effects()
    dialog.project = None
    dialog._scene = lambda: pytest.fail('Changed selection was reopened')
    dialog.finish()
    assert len(admitted) == 1 and admitted[0]['cancelled'].__self__ is dialog
    assert review.call_args.args == (dialog, dialog.library_root, ())
    assert review.call_args.kwargs['available'] == ('gain',)
    assert review.call_args.kwargs['guard'].__self__ is dialog
    assert not method.called and len(dialog.calls) == 1
    dialog.finish()
    assert method.call_args.args == (project, 'clip', changes)
    assert method.call_args.kwargs['scene_id'] == scene.id and method.call_args.kwargs['clip_id'] == cue.id


@pytest.mark.parametrize('failure', [False, OSError('Still owned'), KeyboardInterrupt()])
def test_close_failure_keeps_real_owner_reference_for_retry(dialog, failure):
    attempts = []
    def close():
        attempts.append(1)
        if len(attempts) == 1:
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return True
    dialog.editor.close = close
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    dialog.close(event)
    assert not dialog.closed and not dialog._editor_closed
    event.Veto.assert_called_once()
    dialog.Hide.assert_not_called()
    dialog.close()
    assert dialog.closed and dialog._editor_closed and len(attempts) == 2
    dialog._cleanup()
    assert len(attempts) == 2


def test_loop_uses_existing_navigation_and_stop_invalidates_queued_restarts(dialog, tmp_path, monkeypatch):
    from carr_library.speech import player, progress_speech
    from carr_library.ui import playback_navigation
    selected(dialog, tmp_path)
    path = tmp_path / 'clip-preview-actual.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 24000)
    actions, plays, seeks, ticks = [], [], [], []
    class Player:
        state, path, duration_seconds, position_seconds = 'stopped', None, 0, 0
        position_is_approximate = False
        def play(self, source):
            with wave.open(str(source), 'rb') as stream:
                self.duration_seconds = stream.getnframes() / stream.getframerate()
            self.path, self.state, self.position_seconds = source, 'playing', 0
            plays.append(source)
        def seek(self, position):
            self.position_seconds, self.state = position, 'playing'
            seeks.append(position)
        def stop(self):
            self.state, self.position_seconds = 'stopped', 0
        def close(self):
            self.stop()
            return True
    class Timer:
        def __init__(self, owner): pass
        def Start(self, interval): ticks.append(interval)
        def Stop(self): ticks.append('stopped')
    wx = dialog.new.__globals__['wx']
    wx.Timer, wx.EVT_TIMER, wx.EVT_WINDOW_DESTROY = Timer, object(), object()
    wx.CallLater = lambda *args: pytest.fail('Quiet scheduling is captured explicitly')
    dialog.Bind = Mock()
    dialog.GetId = lambda: dialog._native_id
    real_host = playback_navigation.PlaybackNavigationHost
    monkeypatch.setattr(playback_navigation, 'PlaybackNavigationHost',
        lambda *args, **kwargs: real_host(*args, **kwargs, wx_module=wx))
    monkeypatch.setattr(player, 'WavePlayer', Player)
    def quiet(unused, action, *, schedule, alive, on_error):
        actions.append((action, alive, on_error))
    monkeypatch.setattr(progress_speech, 'play_when_quiet', quiet)
    dialog.editor.preview_range = Mock(return_value=path)
    assert not plays and not actions
    dialog.preview_loop()
    dialog.finish()
    assert len(actions) == 1 and not plays
    action, alive, failed = actions.pop(0)
    assert alive()
    action()
    assert plays == [path] and dialog.navigation.controller.repeat
    assert (dialog.navigation.controller.a, dialog.navigation.controller.b) == (0, 1)
    dialog.player.position_seconds = 1
    dialog.player.state = 'stopped'
    dialog.navigation.controller.tick()
    assert len(actions) == 1 and not seeks
    action, alive, failed = actions.pop(0)
    assert alive()
    action()
    assert seeks == [0]
    dialog.player.position_seconds = 1
    dialog.navigation.controller.tick()
    action, alive, failed = actions.pop(0)
    dialog.stop()
    assert not alive()
    action()  # The real controller repeats its own current-generation check.
    assert seeks == [0] and dialog.player.state == 'stopped'
    dialog.close()
    assert dialog.navigation.closed and 'stopped' in ticks


def test_pending_and_completed_clip_work_never_rebinds_new_library(dialog, tmp_path):
    done, invoked = Mock(), []
    dialog._run('Captured clip scope', lambda: invoked.append('old') or 'saved', done)
    work, complete, failed = dialog.calls.pop()
    value = work()
    assert value == 'saved'
    dialog.frame.library = SimpleNamespace(root=tmp_path / 'new')
    dialog.frame.working = False
    complete(value)
    done.assert_not_called()
    assert not dialog.pending
    assert dialog._run('Forbidden new save', Mock(), Mock()) is False
    assert invoked == ['old']


def test_native_parent_deletion_makes_all_new_range_effect_marker_actions_inert(dialog, tmp_path):
    selected(dialog, tmp_path)
    dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog._native_id, Skip=lambda: None))
    dialog.status.ChangeValue.side_effect = AssertionError('dead status')
    dialog.status.SetFocus.side_effect = AssertionError('dead focus')
    for name in ('copy_clip', 'cut_clip', 'paste_clip', 'copy_range', 'cut_range', 'trim_range',
                 'silence_range', 'split_clip', 'join_clips', 'join_rendered', 'crossfade',
                 'undo', 'redo', 'add_marker', 'edit_marker', 'remove_marker', 'bypass',
                 'set_sample_rate', 'clip_effects', 'role_effects', 'scene_effects',
                 'master_effects', 'preview_range', 'preview_dry', 'preview_loop', 'playback_controls'):
        getattr(dialog, name)()
    assert not dialog.calls
