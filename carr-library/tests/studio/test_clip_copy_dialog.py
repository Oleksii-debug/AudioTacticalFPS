"""Actual queued ClipDialog copy/paste handlers over the existing host scope."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.studio.project_history import ClipReference, RevisionRef

_spec = importlib.util.spec_from_file_location(
    '_carr_clip_copy_dialog_fixture', Path(__file__).with_name('test_clip_history_dialog.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
dialog = fixture.dialog


def setup(dialog):
    project = dialog.project
    reference = ClipReference(project.id, RevisionRef(project.revision, project.fingerprint),
                              'scene', 'first')
    dialog._copied_clip = None
    dialog.editor.copy = Mock(return_value=reference)
    dialog.editor.cut_for_paste = Mock(return_value={
        'project': replace(project, revision=project.revision + 1), 'reference': reference})
    dialog.editor.paste = Mock(return_value=replace(project, revision=project.revision + 1))
    return reference


def test_copy_is_queued_and_remembers_only_the_completed_saved_selection(dialog):
    reference = setup(dialog)
    original = dialog.project
    dialog.copy_clip()
    assert dialog.pending and dialog._copied_clip is None
    dialog.finish()
    assert dialog.editor.copy.call_args.args == (original, 'scene', 'first')
    assert dialog._copied_clip == reference
    assert not dialog._undo and not dialog._redo
    dialog._accept.assert_not_called()
    assert 'Copied the saved clip' in dialog.status.ChangeValue.call_args.args[0]


@pytest.mark.parametrize('operation', ['copy_clip', 'cut_for_paste', 'paste_clip'])
def test_changed_library_before_queued_clipboard_work_prevents_all_mutation(dialog, operation):
    reference = setup(dialog)
    dialog._copied_clip = reference
    getattr(dialog, operation)()
    assert dialog.pending
    dialog.frame.library = SimpleNamespace(root=dialog.library_root)
    dialog.finish()
    dialog.editor.copy.assert_not_called()
    dialog.editor.cut_for_paste.assert_not_called()
    dialog.editor.paste.assert_not_called()
    dialog._accept.assert_not_called()


def test_paste_freezes_saved_reference_destination_and_time_before_queue(dialog):
    reference = setup(dialog)
    dialog._copied_clip = reference
    original = dialog.project
    dialog.picker.GetValue.return_value = '2.25'
    dialog.paste_clip()
    dialog._copied_clip = replace(reference, clip_id='another')
    dialog.picker.GetValue.return_value = '999'
    dialog.finish()
    assert dialog.editor.paste.call_args.args == (original, 'scene', reference, 2.25)
    assert callable(dialog.editor.paste.call_args.kwargs['cancelled'])
    dialog._accept.assert_called_once_with(dialog.editor.paste.return_value)
    dialog.picker.Destroy.assert_called_once()


def test_cancelled_paste_picker_creates_no_job_and_retains_copied_reference(dialog):
    reference = setup(dialog)
    dialog._copied_clip = reference
    dialog.picker.ShowModal.return_value = dialog.wx.ID_CANCEL
    dialog.paste_clip()
    assert not dialog.calls and not dialog.pending and dialog._copied_clip == reference
    dialog.editor.paste.assert_not_called()
    dialog.picker.Destroy.assert_called_once()


def test_paste_without_a_saved_reference_is_a_visible_noop(dialog):
    setup(dialog)
    dialog.paste_clip()
    assert not dialog.calls and not dialog.pending
    dialog.wx.TextEntryDialog.assert_not_called()
    dialog.editor.paste.assert_not_called()
    assert 'Copy or cut' in dialog.status.ChangeValue.call_args.args[0]


def test_committed_cut_keeps_reference_and_completion_after_late_stop(dialog):
    reference = setup(dialog)
    result = dialog.editor.cut_for_paste.return_value
    def committed(*args, **kwargs):
        dialog.cancelled.set()
        return result
    dialog.editor.cut_for_paste.side_effect = committed
    dialog.cut_for_paste()
    dialog.finish()
    assert dialog._copied_clip == reference
    dialog._accept.assert_called_once_with(result['project'])
    assert 'Original audio is retained' in dialog.status.ChangeValue.call_args.args[0]


def test_failed_cut_preserves_previous_clipboard_and_history(dialog):
    reference = setup(dialog)
    dialog._copied_clip = reference
    dialog.editor.cut_for_paste.side_effect = ValueError('Concurrent project changed')
    dialog.cut_for_paste()
    dialog.finish()
    assert dialog._copied_clip == reference
    dialog._accept.assert_not_called()
    assert 'changed' in dialog.status.ChangeValue.call_args.args[0]


def test_same_path_directory_replacement_revokes_queued_paste(dialog, tmp_path):
    reference = setup(dialog)
    dialog._copied_clip = reference
    dialog.paste_clip()
    moved = tmp_path / 'previous-owned-root'
    dialog.library_root.rename(moved)
    dialog.library_root.mkdir()
    dialog.finish()
    dialog.editor.paste.assert_not_called()
    dialog._accept.assert_not_called()
