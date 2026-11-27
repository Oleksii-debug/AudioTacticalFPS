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
def dialog():
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in parsed.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    wx = SimpleNamespace(Dialog=object, ID_CANCEL=2)
    env = dict(wx=wx, Path=Path, __name__='carr_library.ui._clips_probe', __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    value = object.__new__(env['ClipDialog'])
    value.closed = value.closing = value.pending = False
    value.destroyed, value.owner_id = False, 101
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.status = Mock()
    value._enable = Mock()
    value.Hide = Mock()
    value.IsModal = Mock(return_value=False)
    value._accept = Mock()
    value.frame = SimpleNamespace(working=False, cancel_event=threading.Event())
    value.cancel_event = value.frame.cancel_event
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
    generation = dialog.playback_generation
    dialog.close()
    assert dialog.playback_generation > generation
    dialog.player.close.assert_called_once()
    assert not path.exists()


@pytest.mark.parametrize('answer,choose_output', [('no', True), ('yes', False), ('yes', True)])
def test_native_review_defers_exact_file_approval_until_worker(dialog, answer, choose_output):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    parent = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    function = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == 'render')
    function.body = [ast.copy_location(ast.Global(names=node.names), node)
                     if isinstance(node, ast.Nonlocal) else node for node in function.body]
    pending = ('C:/Explicit/original.wav',)
    review = Mock()
    review.ShowModal.return_value = 1 if answer == 'yes' else 0
    folder = Mock()
    folder.ShowModal.return_value = 2 if choose_output else 0
    folder.GetPath.return_value = 'C:/Explicit/new-output'
    wx = SimpleNamespace(MessageDialog=Mock(return_value=review), DirDialog=Mock(return_value=folder),
                         ID_YES=1, ID_OK=2, YES_NO=4, NO_DEFAULT=8, ICON_QUESTION=16)
    service = Mock()
    service.restored_inputs_to_review.return_value = pending
    service.get.return_value = {'recovery': {'requires_output_directory': True}}
    queued = []
    cancel = threading.Event()
    env = dict(wx=wx, Path=Path, busy=False, current_job='selected-job', dialog=object(),
               service=service, cancel=cancel, report=Mock(), run=lambda work, done, **kw: queued.append(work))
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), env)
    env['render']()
    service.approve_restored_inputs.assert_not_called()
    assert wx.MessageDialog.call_args.args[-1] & wx.NO_DEFAULT
    assert pending[0] in wx.MessageDialog.call_args.args[1]
    if answer != 'yes' or not choose_output:
        assert not queued
        service.relocate_restored_job.assert_not_called()
        return
    assert len(queued) == 1
    service.relocate_restored_job.assert_called_once_with('selected-job', Path('C:/Explicit/new-output'))
    queued[0](lambda text: None)
    service.approve_restored_inputs.assert_called_once_with(pending, cancelled=cancel.is_set)
    assert service.run.call_args.args == ('selected-job',)


def test_unconfirmed_playback_stop_keeps_dialog_files_and_blocks_new_work(dialog, tmp_path):
    path = tmp_path / 'clip-preview-retained.wav'
    path.write_bytes(b'Owned preview bytes')
    dialog.previews = [path]
    dialog.player = Mock()
    dialog.player.close.side_effect = RuntimeError('Injected native close failure')
    event = Mock()
    event.CanVeto.return_value = True
    dialog.close(event)
    assert not dialog.closed and path.exists()
    event.Veto.assert_called_once()
    assert 'cleanup is still pending' in dialog.status.ChangeValue.call_args.args[0]
    assert dialog._run('Next clip operation', Mock(), Mock()) is False
    assert not dialog.pending and not dialog.calls
    dialog.player.close.side_effect = None
    dialog.player.close.return_value = None
    dialog.close()
    assert dialog.closed and not path.exists()


def destroy_event(identity):
    return SimpleNamespace(GetId=lambda: identity, Skip=Mock(),
                           GetEventObject=lambda: pytest.fail('The dying wrapper must not be inspected'))


def test_direct_clip_destruction_cancels_pending_work_before_it_can_start(dialog):
    operation, done = Mock(), Mock()
    assert dialog._run('Queued clip work', operation, done)
    event = destroy_event(dialog.owner_id)
    dialog.on_destroy(event)
    assert dialog.destroyed and dialog.closed and dialog.cancel_event.is_set()
    event.Skip.assert_called_once()
    dialog.IsModal = dialog.Hide = Mock(side_effect=AssertionError('Destroyed native owner queried'))
    dialog.status.ChangeValue = dialog.status.SetFocus = Mock(side_effect=AssertionError('Destroyed native control touched'))
    dialog._enable = Mock(side_effect=AssertionError('Destroyed native controls enabled'))
    dialog.finish()
    dialog.close()
    dialog.message('Late message')
    assert not dialog.pending
    operation.assert_not_called()
    done.assert_not_called()
    assert dialog._run('Another clip operation', Mock(), Mock()) is False


@pytest.mark.parametrize('outcome', ['preview', 'error'])
def test_clip_destruction_discards_queued_completion_without_native_access(dialog, tmp_path, outcome):
    path = tmp_path / 'clip-preview-queued.wav'
    path.write_bytes(b'Completed owned preview')
    done = Mock()
    assert dialog._run('Clip preview', lambda: path, done)
    operation, completed, failed = dialog.calls.pop()
    result = operation()
    dialog.on_destroy(destroy_event(dialog.owner_id))
    dialog._enable = dialog.Hide = dialog.IsModal = Mock(side_effect=AssertionError('Destroyed native owner touched'))
    dialog.status.ChangeValue = dialog.status.SetFocus = Mock(side_effect=AssertionError('Destroyed control touched'))
    if outcome == 'preview':
        completed(result)
        assert not path.exists()
    else:
        failed(RuntimeError('Queued worker failure'))
        assert path.read_bytes() == b'Completed owned preview'
    assert not dialog.pending
    done.assert_not_called()


def test_child_destruction_does_not_close_or_cancel_live_clip_owner(dialog):
    dialog._run('Clip operation', lambda: 'live result', dialog._accept)
    event = destroy_event(dialog.owner_id + 1)
    dialog.on_destroy(event)
    assert not dialog.closed and not dialog.destroyed and not dialog.cancel_event.is_set()
    dialog.finish()
    dialog._accept.assert_called_once_with('live result')
    event.Skip.assert_called_once()


def test_destroyed_clip_retains_failed_player_and_preview_after_dialog_collection(dialog, tmp_path):
    import gc
    import weakref
    from carr_library.audio import cleanup

    path = tmp_path / 'clip-preview-native-owned.wav'
    original = b'Preview remains owned until native Close confirms silence.'
    path.write_bytes(original)
    allowed, retried, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    ui_thread = threading.get_ident()

    class Player:
        def close(self):
            calls.append(threading.get_ident())
            if calls[-1] != ui_thread:
                retried.set()
            if not allowed.is_set():
                raise OSError('Injected unconfirmed native Close')

    # A separate unrooted instance lets actual collection prove that cleanup
    # ownership survives the dialog; pytest retains the controller fixture.
    owner = object.__new__(type(dialog))
    owner.closed = owner.closing = owner.pending = owner.destroyed = False
    owner.owner_id, owner.playback_generation = 801, 0
    owner.cancel_event = threading.Event()
    owner.player = Player()
    owner.previews = [path]
    owner_ref, player_ref = weakref.ref(owner), weakref.ref(owner.player)
    retained_before = {id(item) for item in cleanup._RETAINED}
    retained = None
    try:
        owner.on_destroy(destroy_event(owner.owner_id))
        assert owner.destroyed and owner.closed
        assert 'unconfirmed native Close' in owner.cleanup_error
        del owner
        gc.collect()
        assert owner_ref() is None, 'Cleanup must not keep the destroyed dialog alive.'
        assert player_ref() is not None, 'The native Close retry authority was lost with the dialog.'
        assert path.read_bytes() == original
        owners = [item for item in cleanup._RETAINED if id(item) not in retained_before]
        assert len(owners) == 1
        retained = owners[0]
        assert not retained.quiescent and retried.wait(3)
        retained.add_quiescent_callback(finished.set)
        allowed.set()
        assert finished.wait(5) and retained.quiescent
        assert not path.exists() and len(calls) >= 2
        assert retained not in cleanup._RETAINED
    finally:
        allowed.set()
        if retained is not None:
            retained.close()
