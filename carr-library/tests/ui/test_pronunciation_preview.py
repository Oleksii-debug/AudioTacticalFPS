"""Cancellable preview lifecycle and existing editor publication boundaries."""
from dataclasses import replace
import json
from pathlib import Path
import queue
import threading

import pytest

from carr_library.reader.narration import (NarrationStore, PronunciationRule, new_project,
    encode_pronunciation_dictionary)
from carr_library.ui import narration_editor as subject
from carr_library.ui.narration_editor import NarrationController, PronunciationPreviewTask


def _task():
    callbacks, outcomes = queue.Queue(), []
    task = PronunciationPreviewTask(dispatch=lambda fn, *args: callbacks.put((fn, args)),
                                   completed=lambda result, error: outcomes.append((result, error)))
    return task, callbacks, outcomes


def _finish(callbacks):
    callback, args = callbacks.get(timeout=5)
    callback(*args)


def test_async_literal_preview_reaches_one_completion_and_never_mutates_source():
    task, callbacks, outcomes = _task()
    task.start('Original', (PronunciationRule('Original', 'Reading'),))
    _finish(callbacks)
    assert not task.busy and outcomes == [(('Reading', 1), None)]
    assert callbacks.empty()


def test_closed_editor_ignores_a_late_success_callback():
    task, callbacks, outcomes = _task()
    task.start('Original', (PronunciationRule('Original', 'Reading'),))
    callback, args = callbacks.get(timeout=5)
    task.dispose()
    callback(*args)
    assert outcomes == [] and task.closed


def test_cancel_after_worker_completion_before_dispatch_never_applies_a_result():
    task, callbacks, outcomes = _task()
    task.start('Original', (PronunciationRule('Original', 'Reading'),))
    callback, args = callbacks.get(timeout=5)
    task.stop()
    callback(*args)
    assert outcomes[0][0] is None and isinstance(outcomes[0][1], InterruptedError)


def test_failed_preview_settles_without_partial_result_or_false_success():
    task, callbacks, outcomes = _task()
    task.start('Original', (PronunciationRule('(', 'Reading', regex=True),), timeout=3)
    _finish(callbacks)
    assert not task.busy and outcomes[0][0] is None and isinstance(outcomes[0][1], ValueError)


def test_overlapping_preview_cannot_replace_its_pending_completion():
    task, callbacks, outcomes = _task()
    task.start('a', (PronunciationRule('a', 'b'),))
    with pytest.raises(ValueError, match='active'):
        task.start('different', ())
    _finish(callbacks)
    assert outcomes == [(('b', 1), None)]


def test_stale_generation_callback_cannot_finish_a_later_preview():
    task, callbacks, outcomes = _task()
    task.start('a', (PronunciationRule('a', 'b'),))
    first, args = callbacks.get(timeout=5)
    first(*args)
    task.start('c', (PronunciationRule('c', 'd'),))
    first(*args)
    assert task.busy and len(outcomes) == 1
    _finish(callbacks)
    assert outcomes == [(('b', 1), None), (('d', 1), None)]


def test_invalid_dictionary_import_leaves_existing_rules_and_pending_text(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    project = replace(new_project('Original'), rules=(PronunciationRule('a', 'b'),))
    controller = NarrationController(NarrationStore(root), project)
    path = tmp_path / 'bad.json'
    path.write_text('[{"written":"a","spoken":"b","execute":"shell"}]')
    with pytest.raises(ValueError):
        controller.import_dictionary(path)
    assert controller.project == project and controller.pending_text == 'Original'


def test_dictionary_import_keeps_source_and_rechecks_the_current_guard(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = NarrationController(NarrationStore(root), new_project('Original'))
    path = tmp_path / 'dictionary.json'
    rules = (PronunciationRule('a', 'b', language='uk', language_scope=True),)
    path.write_bytes(encode_pronunciation_dictionary(rules))
    count = 0
    def guard():
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError('Scope closed during import')
    controller.guard = guard
    with pytest.raises(ValueError, match='Scope closed'):
        controller.import_dictionary(path)
    assert controller.project.rules == () and controller.project.original_text == 'Original'


def test_dictionary_export_preserves_an_externally_changed_approved_destination(tmp_path):
    from carr_library.ui.export_destination import approve_export_destination
    root = tmp_path / 'library'
    root.mkdir()
    controller = NarrationController(NarrationStore(root), new_project('Original'))
    target = tmp_path / 'dictionary.json'
    target.write_bytes(b'Approved earlier dictionary')
    approval = approve_export_destination(None, target, private_root=root, confirm=lambda *_: True)
    controller.guard = lambda: target.write_bytes(b'Newer external dictionary')
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_dictionary(target, overwrite=True, approval=approval)
    assert target.read_bytes() == b'Newer external dictionary'


def test_dictionary_export_cannot_replace_the_reading_project_original(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    original = tmp_path / 'original.txt'
    original.write_text('Original')
    project = new_project('Original', original_path=str(original))
    controller = NarrationController(NarrationStore(root), project)
    with pytest.raises(ValueError, match='immutable'):
        controller.export_dictionary(original, overwrite=True)
    assert original.read_text() == 'Original'


def _completion_callback():
    """Execute the production nested native completion policy with owned controls."""
    import ast
    import inspect
    import textwrap
    from types import SimpleNamespace
    from unittest.mock import Mock
    tree = ast.parse(textwrap.dedent(inspect.getsource(subject.open_narration_editor)))
    node = next(item for item in ast.walk(tree)
                if isinstance(item, ast.FunctionDef) and item.name == 'completed_preview')
    state = dict(closed=False, close_after_preview=False,
                 preview_snapshot=('Unsaved source copy', (), 'project'))
    controller = SimpleNamespace(guard=Mock(), pending_text='Unsaved source copy',
                                 project=SimpleNamespace(rules=(), id='project'))
    namespace = dict(state=state, controller=controller, preview=SimpleNamespace(busy=False),
                     timer=Mock(), preview_controls=Mock(), close=Mock(), say=Mock(),
                     preview_transform=Mock())
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, '<pronunciation-completion>', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('outcome', ['cancelled', 'failed', 'declined', 'stale'],
                         ids=['cancel', 'error', 'declined-proposal', 'changed-draft'])
def test_settled_preview_restores_autosave_without_applying_partial_text(outcome):
    ui = _completion_callback()
    error = (InterruptedError('Cancelled') if outcome == 'cancelled' else
             ValueError('Invalid pattern') if outcome == 'failed' else None)
    if outcome == 'stale':
        ui['state']['preview_snapshot'] = ('Earlier copy', (), 'project')
    ui['completed_preview'](('Proposed text', 1), error)
    ui['timer'].StartOnce.assert_called_once_with(1200)
    assert ui['controller'].pending_text == 'Unsaved source copy'
    if outcome in ('cancelled', 'failed', 'stale'):
        ui['preview_transform'].assert_not_called()


@pytest.mark.parametrize('closed', [False, True], ids=['keep-editing', 'closed'])
def test_preview_settlement_restarts_recovery_only_when_close_keeps_editor_open(closed):
    ui = _completion_callback()
    ui['state']['close_after_preview'] = True
    def settle_close():
        ui['state']['closed'] = closed
    ui['close'].side_effect = settle_close
    ui['completed_preview'](None, InterruptedError('Cancelled for close'))
    ui['close'].assert_called_once_with()
    assert ui['state']['close_after_preview'] is False
    ui['preview_transform'].assert_not_called()
    if closed:
        ui['timer'].StartOnce.assert_not_called()
    else:
        ui['timer'].StartOnce.assert_called_once_with(1200)


def test_late_completion_after_destroy_does_not_restart_native_timer():
    ui = _completion_callback()
    ui['state']['closed'] = True
    ui['completed_preview'](('Late text', 1), None)
    ui['timer'].StartOnce.assert_not_called()
    ui['preview_controls'].assert_not_called()


def test_settlement_does_not_restart_timer_during_a_new_preview():
    ui = _completion_callback()
    def start_another(*args):
        ui['preview'].busy = True
    ui['preview_transform'].side_effect = start_another
    ui['completed_preview'](('Proposed text', 1), None)
    ui['timer'].StartOnce.assert_not_called()
