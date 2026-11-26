"""Shell action contracts using the explicit headless wx/panel probe."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def shell():
    source = Path(__file__).with_name('probe_workspace_shell.py')
    spec = importlib.util.spec_from_file_location('carr_shell_actions_probe', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    case = probe.Tests()
    case.setUp()
    save = Mock(return_value='saved-hit')
    select = Mock()
    original = case.bind
    case.f.bind_scope = lambda w, t: replace(original(w, t), save_hit=save, select_collection=select)
    case.open(0)
    f = case.f
    search = f.book.pages[0][0]
    hit = SimpleNamespace(workspace='a', ref='ref', revision='r', quote='original', citation='citation')
    search.selected = lambda: hit
    return case, probe, search, hit, save, select


def args(hit):
    return hit.workspace, hit.ref, hit.revision, hit.quote, hit.citation


def test_save_result_opens_saved_evidence_note(shell):
    case, _, search, hit, save, _ = shell
    assert search.kwargs['save_evidence'](*args(hit)) == 'saved-hit'
    save.assert_called_once_with(*args(hit))
    f = case.f
    assert f.evidence.displayed.evidence_id == 'saved-hit'
    assert f.book.GetSelection() == 3 and f.evidence.note.focused


def test_cancel_save_preserves_existing_note_and_focus(shell):
    case, _, search, hit, save, _ = shell
    f = case.f
    f.book.SetSelection(0)
    f.evidence.note.SetValue('draft')
    assert search.kwargs['save_evidence'](*args(hit)) is None
    save.assert_not_called()
    assert f.book.GetSelection() == 3
    assert f.evidence.note.GetValue() == 'draft'


def test_selection_change_during_confirmation_cannot_save(shell):
    case, probe, search, hit, save, _ = shell
    case.f.evidence.note.SetValue('draft')

    def change(*unused):
        search.selected = lambda: SimpleNamespace(**{**vars(hit), 'ref': 'different'})
        return probe.M.wx.YES

    probe.M.wx.MessageBox = change
    with pytest.raises(ValueError, match='selection changed'):
        search.kwargs['save_evidence'](*args(hit))
    save.assert_not_called()
    assert case.f.evidence.note.GetValue() == 'draft'


def test_failed_save_does_not_discard_existing_note(shell):
    case, probe, search, hit, save, _ = shell
    case.f.evidence.note.SetValue('draft')
    probe.M.wx.MessageBox = lambda *unused: probe.M.wx.YES
    save.side_effect = ValueError('revoked')
    with pytest.raises(ValueError, match='revoked'):
        search.kwargs['save_evidence'](*args(hit))
    assert case.f.evidence.note.GetValue() == 'draft'


def test_collection_action_clears_results_and_focuses_query(shell):
    case, _, search, _, _, select = shell
    f = case.f
    catalog = f.book.pages[1][0]
    search.set_workspace = Mock()
    catalog.kwargs['select_collection']('a', 'collection')
    select.assert_called_once_with('a', 'collection')
    search.set_workspace.assert_called_once_with('a')
    assert f.book.GetSelection() == 0 and search.query.focused
    assert 'selected collection' in search.status.value


def test_old_scope_actions_never_run_after_switch(shell):
    case, _, search, hit, save, select = shell
    catalog = case.f.book.pages[1][0]
    case.open(1)
    with pytest.raises(ValueError):
        search.kwargs['save_evidence'](*args(hit))
    with pytest.raises(ValueError):
        catalog.kwargs['select_collection']('a', None)
    save.assert_not_called()
    select.assert_not_called()
