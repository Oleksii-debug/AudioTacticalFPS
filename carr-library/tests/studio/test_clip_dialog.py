"""Queued native controller behavior, with real native controls tested separately."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio import AudioCancelled
from carr_library.studio.project import Cue, Project, Scene


@pytest.fixture
def dialog(tmp_path):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in parsed.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    wx = SimpleNamespace(Dialog=object, ID_CANCEL=2, CallAfter=lambda callback: callback())
    env = dict(wx=wx, Path=Path, threading=threading, __name__='carr_library.ui._clips_probe', __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    value = object.__new__(env['ClipDialog'])
    value.closed = value.closing = value.pending = False
    value._native_id, value._destroy_requested = 431, False
    value.cleanup_error = ''
    value.library_root = tmp_path
    value.navigation = None
    value._editor_closed = False
    value._editor_close_lock = threading.RLock()
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.status = Mock()
    value._enable = Mock()
    value.Hide = Mock()
    value.IsModal = Mock(return_value=False)
    value._accept = Mock()
    value.frame = SimpleNamespace(working=False, cancel_event=threading.Event(),
                                  library=SimpleNamespace(root=tmp_path))
    calls = []
    def run_job(title, operation, done, failed):
        value.frame.working = True
        value.frame.cancel_event.clear()
        calls.append((operation, done, failed))
    value.frame.run_job = run_job
    def finish():
        operation, done, failed = calls.pop(0)
        try:
            result = operation()
        except Exception as exc:
            value.frame.working = False
            failed(exc)
        else:
            value.frame.working = False
            done(result)
    value.finish, value.calls = finish, calls
    return value


def test_close_during_worker_waits_for_cleanup_and_suppresses_late_controls(dialog):
    done = Mock()
    assert dialog._run('Clip operation', lambda: 'durable result', done)
    dialog.close()
    assert dialog.pending and dialog.closing and not dialog.closed
    assert dialog.frame.cancel_event.is_set()
    dialog.Hide.assert_not_called()
    dialog.finish()
    assert dialog.closed and not dialog.pending
    done.assert_not_called()
    dialog.Hide.assert_called_once()
    before = dialog.status.ChangeValue.call_count
    dialog.message('Late callback')
    assert dialog.status.ChangeValue.call_count == before


def test_close_discards_unplayed_preview_after_worker_returns(dialog, tmp_path):
    path = tmp_path / 'clip-preview-example.wav'
    path.write_bytes(b'Explicit synthetic output')
    dialog._run('Preview', lambda: path, Mock())
    dialog.close()
    dialog.finish()
    assert dialog.closed and not path.exists()


def test_audio_cancellation_keeps_dialog_usable_and_has_interruption_type(dialog):
    def operation():
        raise AudioCancelled('Clip cancelled before publication')
    dialog._run('Import', operation, Mock())
    dialog.finish()
    assert not dialog.closed and not dialog.pending
    error = dialog.status.ChangeValue.call_args.args[0]
    assert 'cancelled before publication' in error
    assert dialog._run('Next operation', lambda: 'done', Mock())


def test_refused_shared_job_never_leaves_clip_controls_pending(dialog):
    dialog.frame.run_job = lambda *args: None
    dialog._run('Clip', lambda: None, Mock())
    assert not dialog.pending
    assert 'could not start' in dialog.status.ChangeValue.call_args.args[0]


def test_another_operation_does_not_receive_clip_cancellation(dialog):
    dialog.frame.working = True
    assert not dialog._run('Clip', Mock(), Mock())
    dialog.cancel()
    assert not dialog.frame.cancel_event.is_set()
    assert not dialog.calls


def test_save_binds_reviewed_clip_and_settings_before_queue(dialog):
    cue = Cue('clip-1', '/retained.audio')
    scene = Scene('scene-1', 'Scene', (), cues=(cue,))
    project = Project('project-1', 'Project', (), (scene,))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    settings = {'start': 2, 'trim_start': .25, 'duration': 1, 'pan': -1}
    dialog._settings = lambda: settings
    dialog.editor = SimpleNamespace(edit=Mock(return_value=project))
    dialog.save()
    dialog.project = None
    dialog._settings = lambda: {'start': 99}
    dialog.finish()
    args, kwargs = dialog.editor.edit.call_args
    assert args == (project, scene.id, cue.id)
    assert kwargs['start'] == 2 and kwargs['trim_start'] == .25 and kwargs['pan'] == -1
    dialog._accept.assert_called_once_with(project)


def test_stop_invalidates_deferred_playback_and_releases_native_file(dialog, tmp_path):
    path = tmp_path / 'clip-preview-current.wav'
    path.write_bytes(b'fixture')
    dialog.previews = [path]
    dialog.player = Mock()
    player = dialog.player
    generation = dialog.playback_generation
    dialog.close()
    assert dialog.playback_generation > generation
    player.close.assert_called_once()
    assert not path.exists()


def test_rewrapped_native_parent_destruction_cancels_only_owned_work_and_late_controls_are_inert(dialog, tmp_path):
    path = tmp_path / 'clip-preview-after-destroy.wav'
    path.write_bytes(b'Owned synthetic preview result')
    done = Mock()
    dialog._run('Pending preview', lambda: path, done)
    event = Mock()
    event.GetId.return_value = 431
    event.GetEventObject.return_value = object()
    dialog.on_destroy(event)
    assert dialog.closed and dialog.closing and dialog._cancelled()
    assert not dialog.frame.cancel_event.is_set()
    dialog.status.ChangeValue.side_effect = AssertionError('Deleted status')
    dialog.status.SetFocus.side_effect = AssertionError('Deleted focus')
    dialog._enable.side_effect = AssertionError('Deleted controls')
    dialog.finish()
    done.assert_not_called()
    dialog.Hide.assert_not_called()
    assert not path.exists() and not dialog.pending
    for name in ('reload', 'new', 'select_project', 'select_scene', 'select_clip',
                 'add', 'recording', 'save', 'remove', 'duration', 'move',
                 'preview', 'stop', 'cancel', 'close'):
        getattr(dialog, name)()
    dialog.message('Late report')
    event.Skip.assert_called_once()


def test_child_destroy_does_not_close_clip_dialog_or_cancel_another_operation(dialog):
    event = Mock()
    event.GetId.return_value = 432
    dialog.frame.working = True
    dialog.on_destroy(event)
    assert not dialog.closed and not dialog.frame.cancel_event.is_set()
    event.Skip.assert_called_once()


def test_dead_parent_can_suppress_ui_completion_without_leaking_owned_preview(dialog, tmp_path):
    path = tmp_path / 'clip-preview-worker-completion.wav'
    path.write_bytes(b'Owned worker result')
    done = Mock()
    dialog._run('Pending preview', lambda: path, done)
    operation, complete, failed = dialog.calls.pop()
    dialog.dispose()
    result = operation()  # Actual production work wrapper; no UI callback follows.
    assert result == path and not path.exists() and not dialog.pending
    done.assert_not_called()



@pytest.mark.parametrize('action', ['new', 'add', 'move', 'recording'])
def test_nested_clip_modal_owner_destruction_never_reads_deleted_peer_or_submits(dialog, tmp_path, monkeypatch, action):
    """The actual handler returns from a successful modal after native deletion."""
    from carr_library.audio import recording
    wx = dialog.new.__globals__['wx']
    wx.ID_OK, wx.FD_OPEN, wx.FD_FILE_MUST_EXIST = 1, 4, 8
    forbidden, peers = [], []
    cue = Cue('clip-1', str(tmp_path / 'retained.wav'))
    scene = Scene('scene-1', 'First', (), cues=(cue,))
    other = Scene('scene-2', 'Second', (), minimum_duration=1)
    project = Project('project-1', 'Project', (), (scene, other))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    dialog._settings = lambda: {'start': 0}
    dialog.frame.library = SimpleNamespace(root=tmp_path / 'library')
    dialog.library_root = dialog.frame.library.root
    dialog.editor = SimpleNamespace(create=Mock(), add=Mock(), move=Mock(),
                                   service=SimpleNamespace(close=Mock(return_value=True)))
    retained = SimpleNamespace(list=Mock(return_value=[{'id': 'recording-1', 'title': 'Saved recording'}]),
                               get=Mock())
    monkeypatch.setattr(recording, 'RecordingService', lambda root: retained)

    class Peer:
        def __init__(self, *args, **kwargs):
            self.live = True
            peers.append(self)
        def __bool__(self):
            return self.live
        def ShowModal(self):
            event = Mock()
            event.GetId.return_value = dialog._native_id
            event.GetEventObject.return_value = object()  # Rewrapped native owner.
            dialog.on_destroy(event)
            self.live = False
            dialog.status.ChangeValue.side_effect = lambda *args: forbidden.append('owner status')
            dialog.status.SetFocus.side_effect = lambda *args: forbidden.append('owner focus')
            dialog._enable.side_effect = lambda *args: forbidden.append('owner controls')
            return wx.ID_OK
        def GetPath(self):
            forbidden.append('deleted chooser path')
            return str(tmp_path / 'selected.wav')
        def GetValue(self):
            forbidden.append('deleted chooser text')
            return 'A project title'
        def GetSelection(self):
            forbidden.append('deleted chooser selection')
            return 0
        def Destroy(self):
            forbidden.append('deleted chooser destruction')

    wx.TextEntryDialog = wx.FileDialog = wx.SingleChoiceDialog = Peer
    getattr(dialog, action)()
    if action == 'recording':
        assert len(dialog.calls) == 1
        dialog.finish()  # Actual saved-recording completion opens the nested chooser.
        retained.list.assert_called_once()
    assert len(peers) == 1 and not peers[0].live
    assert dialog.closed and not dialog.pending
    assert forbidden == []
    assert not dialog.calls and not dialog.frame.working
    dialog.editor.create.assert_not_called()
    dialog.editor.add.assert_not_called()
    dialog.editor.move.assert_not_called()
    retained.get.assert_not_called()


def test_native_self_destruction_detaches_pending_completion_and_cancels_only_its_work(dialog):
    seen, done = [], Mock()
    dialog._run('Pending clip', lambda: seen.append(dialog._cancelled()), done)
    child = Mock()
    child.GetId.return_value = 432
    dialog.on_destroy(child)
    assert not dialog.closed and not dialog._cancelled()
    own = Mock()
    own.GetId.return_value = 431
    dialog.on_destroy(own)
    assert dialog.closed and dialog._cancelled()
    assert not dialog.frame.cancel_event.is_set()
    before = dialog._enable.call_count, dialog.status.ChangeValue.call_count
    dialog.finish()
    assert seen == [True] and before == (dialog._enable.call_count, dialog.status.ChangeValue.call_count)
    done.assert_not_called()
    child.Skip.assert_called_once()
    own.Skip.assert_called_once()


def test_destroy_after_native_parent_deletion_never_calls_dead_base(dialog, monkeypatch):
    monkeypatch.setattr(type(dialog), '__bool__', lambda self: False, raising=False)
    # The test base is object, so calling its nonexistent Destroy also fails.
    assert dialog.Destroy() is False
    assert dialog.closed and not dialog.frame.cancel_event.is_set()


def test_modal_unwind_after_native_parent_deletion_runs_cleanup_without_destroy():
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    node = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(node, ast.FunctionDef) and node.name == 'show_clip_editor')
    class DeletedDialog:
        dispose, _cleanup, reload, ShowModal = Mock(), Mock(), Mock(), Mock()
        cleanup_error = ''
        def __bool__(self): return False
        def Destroy(self): pytest.fail('Dead native clip wrapper was destroyed twice')
    deleted = DeletedDialog()
    namespace = {'ClipDialog': lambda frame: deleted,
                 'wx': SimpleNamespace(CallAfter=lambda callback: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    namespace['show_clip_editor'](SimpleNamespace(working=False))
    deleted.dispose.assert_called_once()
    deleted._cleanup.assert_called_once()
