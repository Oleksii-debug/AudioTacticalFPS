"""Real ClipDialog handlers under the existing queued-worker and scope contract."""
import ast
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.project_history import RevisionRef


@pytest.fixture
def dialog(tmp_path):
    import carr_library
    path = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    parsed = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in parsed.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    wx = SimpleNamespace(Dialog=object, ID_OK=1, ID_CANCEL=2)
    environment = dict(wx=wx, Path=Path, __name__='carr_library.ui._history_probe',
                       __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), environment)
    value = object.__new__(environment['ClipDialog'])
    value.closed = value.closing = value.pending = value.destroyed = False
    value.cancelled = threading.Event()
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.status, value._enable, value._accept = Mock(), Mock(), Mock()
    value._undo, value._redo = [], []
    value.library = SimpleNamespace(root=tmp_path / 'library')
    value.library_root = value.library.root
    value.library_root.mkdir()
    value._library_identity = value._library_stamp()
    value.frame = SimpleNamespace(working=False, cancel_event=threading.Event(),
                                  library=value.library, _closing=False)
    cue = Cue('first', 'inputs/' + 'a' * 64 + '.audio', duration=1)
    second = replace(cue, id='second', start=1, trim_start=1)
    scene = Scene('scene', 'Current scene', (), cues=(cue, second))
    value.project = Project('project', 'Current project', (), (scene,), revision=105, schema=2)
    value._scene, value._clip = lambda: value.project.scenes[0], lambda: value.project.scenes[0].cues[0]
    value.editor = SimpleNamespace(split=Mock(return_value=value.project),
        cut=Mock(return_value=value.project), join=Mock(return_value=value.project),
        restore=Mock(return_value=value.project), history=Mock(return_value=()))
    picker = Mock()
    picker.ShowModal.return_value = wx.ID_OK
    picker.GetValue.return_value = '.5'
    picker.GetSelection.return_value = 0
    wx.TextEntryDialog = Mock(return_value=picker)
    wx.SingleChoiceDialog = Mock(return_value=picker)
    value.picker, value.wx = picker, wx
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
    value.calls, value.finish = calls, finish
    return value


def test_split_binds_saved_project_scene_clip_and_time_before_queue(dialog):
    original = dialog.project
    dialog.split()
    assert len(dialog.calls) == 1 and dialog.pending
    dialog.picker.GetValue.return_value = '99'
    dialog.finish()
    args, kwargs = dialog.editor.split.call_args
    assert args == (original, 'scene', 'first', .5)
    assert callable(kwargs['cancelled'])
    dialog._accept.assert_called_once_with(original)
    dialog.picker.Destroy.assert_called_once()


def test_cut_binds_both_reviewed_positions_before_queue(dialog):
    dialog.picker.GetValue.return_value = '.2 .7'
    original = dialog.project
    dialog.cut()
    dialog.picker.GetValue.return_value = '0 100'
    dialog.finish()
    assert dialog.editor.cut.call_args.args == (original, 'scene', 'first', .2, .7)


@pytest.mark.parametrize('value', ['1', 'not-a-number 2', '1 2 3'])
def test_invalid_cut_entry_starts_no_worker(dialog, value):
    dialog.picker.GetValue.return_value = value
    dialog.cut()
    assert not dialog.calls and not dialog.pending
    dialog.editor.cut.assert_not_called()


@pytest.mark.parametrize('operation', ['split', 'cut', 'join', 'timeline'])
def test_cancelled_native_picker_never_starts_an_edit(dialog, operation):
    dialog.picker.ShowModal.return_value = dialog.wx.ID_CANCEL
    getattr(dialog, operation)()
    assert not dialog.calls and not dialog.pending
    dialog.editor.split.assert_not_called()
    dialog.editor.cut.assert_not_called()
    dialog.editor.join.assert_not_called()
    dialog.picker.Destroy.assert_called_once()


def test_join_queues_exact_selected_following_section(dialog):
    original = dialog.project
    dialog.join()
    dialog.finish()
    assert dialog.editor.join.call_args.args == (original, 'scene', 'first', 'second')
    assert dialog.wx.SingleChoiceDialog.call_args.args[3] == ['second; scene start 1 seconds']


@pytest.mark.parametrize('operation', ['split', 'cut', 'join'])
def test_changed_library_while_queued_prevents_clip_mutation(dialog, operation):
    dialog.picker.GetValue.return_value = '.2 .7' if operation == 'cut' else '.5'
    getattr(dialog, operation)()
    dialog.frame.library = SimpleNamespace(root=dialog.library_root)
    dialog.finish()
    getattr(dialog.editor, operation).assert_not_called()
    dialog._accept.assert_not_called()


def test_session_history_tracks_successful_whole_project_edits_and_redo_branch(dialog):
    first = dialog.project
    second = replace(first, revision=106, title='Second')
    dialog._record_history(second, None)
    assert dialog._undo == [RevisionRef(first.revision, first.fingerprint)] and not dialog._redo
    dialog.project = second
    undone = replace(first, revision=107)
    dialog._record_history(undone, 'undo')
    assert not dialog._undo and dialog._redo == [RevisionRef(second.revision, second.fingerprint)]
    dialog.project = undone
    redone = replace(second, revision=108)
    dialog._record_history(redone, 'redo')
    assert dialog._undo == [RevisionRef(undone.revision, undone.fingerprint)] and not dialog._redo
    dialog.project = redone
    dialog._redo = [RevisionRef(first.revision, first.fingerprint)]
    changed = replace(redone, revision=109, title='New branch')
    dialog._record_history(changed, None)
    assert not dialog._redo and dialog._undo[-1] == RevisionRef(redone.revision, redone.fingerprint)
    dialog.project = changed
    saved = tuple(dialog._undo)
    dialog._record_history(changed, None)
    assert tuple(dialog._undo) == saved


def test_history_is_cleared_when_switching_project_identity(dialog):
    dialog._undo = [RevisionRef(1, 'a' * 64)]
    dialog._redo = [RevisionRef(2, 'b' * 64)]
    dialog._record_history(replace(dialog.project, id='another'), None)
    assert dialog._undo == dialog._redo == []


@pytest.mark.parametrize('mode', ['undo', 'redo'])
def test_undo_redo_uses_exact_saved_reference_and_only_pops_after_success(dialog, mode):
    selected = RevisionRef(100, 'a' * 64)
    getattr(dialog, '_' + mode).append(selected)
    original = dialog.project
    getattr(dialog, mode)()
    assert getattr(dialog, '_' + mode) == [selected]
    dialog.finish()
    assert dialog.editor.restore.call_args.args == (original, selected)
    dialog._accept.assert_called_once_with(original, history_mode=mode)


def test_failed_restore_keeps_session_history_available(dialog):
    selected = RevisionRef(100, 'a' * 64)
    dialog._undo = [selected]
    dialog.editor.restore.side_effect = ValueError('Saved content changed')
    dialog.undo()
    dialog.finish()
    assert dialog._undo == [selected] and not dialog._redo
    dialog._accept.assert_not_called()
    assert 'changed' in dialog.status.ChangeValue.call_args.args[0]


def test_cancelled_queued_restore_preserves_history_and_saved_project(dialog):
    selected = RevisionRef(100, 'a' * 64)
    dialog._undo = [selected]
    dialog.undo()
    dialog.cancelled.set()
    dialog.finish()
    dialog.editor.restore.assert_not_called()
    assert dialog._undo == [selected]
    dialog._accept.assert_not_called()


def test_committed_restore_is_shown_after_late_cancel_but_not_in_another_library(dialog):
    selected = RevisionRef(100, 'a' * 64)
    dialog._undo = [selected]
    original = dialog.project
    def complete(*args, **kwargs):
        dialog.cancelled.set()
        return original
    dialog.editor.restore.side_effect = complete
    dialog.undo()
    dialog.finish()
    dialog._accept.assert_called_once_with(original, history_mode='undo')
    dialog._accept.reset_mock()
    def changed_library(*args, **kwargs):
        dialog.frame.library = SimpleNamespace(root=dialog.library_root)
        return original
    dialog.editor.restore.side_effect = changed_library
    dialog.undo()
    dialog.finish()
    dialog._accept.assert_not_called()


def test_history_pagination_queues_existing_store_cursor_without_loading_all_content(dialog):
    rows = tuple(RevisionRef(number, f'{number:064x}') for number in range(100, 0, -1))
    dialog.editor.history.side_effect = (rows, ())
    dialog.picker.GetSelection.return_value = 100
    dialog.history()
    dialog.finish()
    assert len(dialog.calls) == 1
    dialog.finish()
    calls = dialog.editor.history.call_args_list
    assert calls[0].kwargs['before_revision'] is None
    assert calls[1].kwargs['before_revision'] == 1
    dialog.editor.restore.assert_not_called()


def test_history_modal_scope_change_never_restores_into_new_library(dialog):
    dialog.editor.history.return_value = (RevisionRef(100, 'a' * 64),)
    def choose():
        dialog.frame.library = SimpleNamespace(root=dialog.library_root)
        return dialog.wx.ID_OK
    dialog.picker.ShowModal.side_effect = choose
    dialog.history()
    dialog.finish()
    assert not dialog.calls
    dialog.editor.restore.assert_not_called()
    assert 'changed' in dialog.status.ChangeValue.call_args.args[0]
