"""Canonical export consent and OCR chooser guards on the current controllers.

Existing menu/Import Center doubles provide the UI boundary. Destination
capture, keymap validation and the atomic file writer are the real owners.
These are source/installed regressions, not native-focus or NVDA evidence.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.extract.page_ocr import Config
from tests.ui.test_import_center import center
from tests.ui.test_keyboard_manager import (
    dialog_double, file_dialog_wx, manager, registry, snapshot,
)


def _export_form(manager, tmp_path):
    private = tmp_path / 'private-keymaps'
    private.mkdir()
    manager.change_root(private)
    manager.apply_overrides({'library.import': 'Ctrl+Shift+I'})
    destination = tmp_path / 'exported-keymap.json'
    view = dialog_double(manager)
    view.draft['library.import'] = 'Ctrl+K'
    view.wx = file_dialog_wx(destination)
    view.wx.FD_SAVE, view.wx.FD_OVERWRITE_PROMPT = 8, 16
    return view, destination


def test_declined_keymap_replacement_never_validates_or_writes(
        manager, tmp_path, monkeypatch):
    view, destination = _export_form(manager, tmp_path)
    original = b'Keep independent existing keymap'
    destination.write_bytes(original)
    before = snapshot(manager)
    question = Mock(return_value=False)
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', question)
    validate = Mock(side_effect=AssertionError('Declined export must not validate'))
    monkeypatch.setattr(manager, 'validate', validate)

    view.export_map()

    question.assert_called_once_with(view.dialog, destination)
    validate.assert_not_called()
    assert destination.read_bytes() == original
    assert snapshot(manager) == before
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existing', [False, True], ids=['new', 'approved-existing'])
def test_approved_keymap_export_preserves_live_bindings(manager, tmp_path, monkeypatch, existing):
    view, destination = _export_form(manager, tmp_path)
    if existing:
        destination.write_bytes(b'Previous explicitly approved export')
    before = snapshot(manager)
    question = Mock(return_value=True)
    monkeypatch.setattr('carr_library.ui.export_destination._confirm_replace', question)

    view.export_map()

    assert json.loads(destination.read_text(encoding='utf-8')) == {
        'version': 1, 'bindings': view.draft,
    }
    assert snapshot(manager) == before
    assert manager.shortcut('library.import') == 'Ctrl+Shift+I'
    assert question.call_count == int(existing)
    if existing:
        question.assert_called_once_with(view.dialog, destination)
    assert 'was exported' in view.messages[-1]
    assert not list(tmp_path.glob('.research-export-*'))


def test_ocr_model_choice_is_not_applied_after_library_changes(center, monkeypatch):
    previous = center.ocr_config
    before_batch = deepcopy(center.batch)
    before_label = center.languages.GetValue()
    selected = Config(languages='ukr+script/Latin', model_profile='fast', orientation=True)

    def choose(parent, initial, *, preset_root):
        assert parent is center and initial is previous
        assert preset_root == center.library.root
        center.frame.library = SimpleNamespace(root=preset_root / 'another-library')
        return selected

    chooser = Mock(side_effect=choose)
    monkeypatch.setattr('carr_library.ui.ocr_options.choose_model_config', chooser)

    center.choose_languages()

    chooser.assert_called_once_with(center, previous, preset_root=center.library.root)
    assert center.ocr_config is previous
    assert center.batch == before_batch
    assert center.languages.GetValue() == before_label
    assert 'library folder changed' in center.status.GetValue()
    center.frame.run_job.assert_not_called()
    center.library.plan_import_batch.assert_not_called()
    center.library.run_import_batch.assert_not_called()
    assert not center.jobs
