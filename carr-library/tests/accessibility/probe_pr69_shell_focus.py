"""Independent shell regression; reuse PR69's headless probe, not native wx.
Run from the assembled carr-library tree with PR69 and its tests present.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def frame():
    source = Path(__file__).resolve().parents[1] / 'ui/probe_workspace_shell.py'
    spec = importlib.util.spec_from_file_location('carr69_probe', source)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    case = probe.Tests()
    case.setUp()
    case.open(0)
    return case, probe


def hidden_note(case):
    f = case.f
    f.book.SetSelection(0)  # Search tab; Evidence and Notes is tab 3.
    f.evidence.note.SetValue('unsaved synthetic note')
    requests = []
    f.evidence.note.SetFocus = lambda: requests.append(f.book.GetSelection())
    return requests


def test_cancel_switch_does_not_focus_hidden_note(frame):
    case, _ = frame
    requests = hidden_note(case)
    case.open(1)
    assert case.f.active == ('a', 't')
    assert case.f.evidence.note.GetValue() == 'unsaved synthetic note'
    assert all(i == 3 for i in requests), 'note focus requested while Evidence tab is hidden'


def test_cancel_close_does_not_focus_hidden_note(frame):
    case, _ = frame
    requests = hidden_note(case)
    event = SimpleNamespace(CanVeto=lambda: True, veto=False, skipped=False)
    event.Veto = lambda: setattr(event, 'veto', True)
    event.Skip = lambda: setattr(event, 'skipped', True)
    case.f.on_close(event)
    assert event.veto and not event.skipped
    assert case.f.evidence.note.GetValue() == 'unsaved synthetic note'
    assert all(i == 3 for i in requests), 'note focus requested while Evidence tab is hidden'


def test_library_only_distinguishable_from_topic_named_none(frame):
    case, _ = frame
    case.scopes[:] = [('a', None, 'Same title'), ('a', 'none', 'Same title')]
    case.f.refresh_scopes()
    assert len(set(case.f.choice.items)) == 2, 'distinct authorized scopes have identical choice labels'


def test_cancel_when_note_tab_visible_preserves_draft(frame):
    case, _ = frame
    requests = hidden_note(case)
    case.f.book.SetSelection(3)
    case.open(1)
    assert requests == [3]
    assert case.f.active == ('a', 't')
    assert case.f.evidence.note.GetValue() == 'unsaved synthetic note'


def test_confirm_switch_destroys_old_scope(frame):
    case, probe = frame
    hidden_note(case)
    old = case.f.book
    with patch.object(probe.M.wx, 'MessageBox', return_value=probe.M.wx.YES):
        case.open(1)
    assert old.hidden and old.destroyed
    assert case.f.active == ('b', 't')
    assert case.f.evidence.note.GetValue() == 'saved'
