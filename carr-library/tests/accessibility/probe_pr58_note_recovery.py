"""Independent PR58 handler regression; wx and persistence are test doubles.
Run: CARR_EVIDENCE_PANEL=/path/to/evidence_panel.py python -m pytest -q probe_pr58_note_recovery.py
"""
import ast
from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest


class Control:
    def __init__(self, *args, **kwargs):
        self.value, self.selection, self.options, self.enabled = '', -1, [], True
        self.focused = False
    def __getattr__(self, name):
        if name in ('SetName', 'Add', 'Bind', 'SetSizer'):
            return lambda *a, **k: None
        raise AttributeError(name)
    def SetFocus(self): self.focused = True
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def Clear(self): self.options.clear(); self.selection = -1
    def Append(self, label): self.options.append(label)
    def Enable(self, value): self.enabled = value


@dataclass(frozen=True)
class Row:
    topic_id: str
    evidence_id: str
    anchor: object
    note: str


@pytest.fixture
def rig():
    wx = SimpleNamespace(**{n: Control for n in ('Panel', 'ListBox', 'TextCtrl', 'BoxSizer', 'StaticText', 'Button')})
    for n in ('LB_SINGLE', 'TE_MULTILINE', 'TE_READONLY', 'VERTICAL', 'EXPAND', 'EVT_BUTTON', 'EVT_LISTBOX', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'YES'):
        setattr(wx, n, 1)
    wx.MessageBox = lambda *a: 0
    source = Path(os.environ['CARR_EVIDENCE_PANEL']).read_text(encoding='utf-8')
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EvidencePanel')
    namespace = dict(wx=wx, uuid4=uuid4, format_citation=lambda anchor: 'Synthetic citation')
    exec(compile(ast.Module(body=[cls], type_ignores=[]), '<reviewed EvidencePanel>', 'exec'), namespace)
    anchor = SimpleNamespace(identity=SimpleNamespace(workspace='a'), quote='Synthetic quote')
    state = SimpleNamespace(rows=[Row('t', 'e1', anchor, 'Stored note')], fail=False, fail_anchor=False)

    class Service:
        workspace = 'a'
        tree = SimpleNamespace(list_topics=lambda: [SimpleNamespace(id='t')])
        def list_saved(self, topic): return tuple(state.rows)
        def edit_note(self, topic, evidence, note):
            if state.fail:
                raise RuntimeError('simulated transient write failure')
            state.rows[:] = [replace(r, note=note) if r.evidence_id == evidence else r for r in state.rows]
        def save(self, topic, evidence, anchor, note):
            if state.fail_anchor:
                raise ValueError('simulated anchor rejection')
            state.rows.append(Row(topic, evidence, anchor, note))

    @contextmanager
    def transaction():
        yield Service()

    return namespace['EvidencePanel'](None, 'a', 't', transaction), state, anchor


def test_successful_save(rig):
    panel, state, _ = rig
    panel.note.SetValue('Draft — примітка')
    panel.save_note()
    assert state.rows[0].note == 'Draft — примітка'
    assert panel.status.value == 'Note saved.'
    assert panel.note.focused


def test_transient_write_failure_keeps_draft(rig):
    panel, state, _ = rig
    panel.note.SetValue('Draft — примітка')
    state.fail = True
    panel.save_note()
    assert state.rows[0].note == 'Stored note'
    assert 'not saved' in panel.status.value
    assert panel.note.value == 'Draft — примітка'


def test_conflicting_update_keeps_draft(rig):
    panel, state, _ = rig
    panel.note.SetValue('My draft')
    state.rows[0] = replace(state.rows[0], note='Concurrent note')
    panel.save_note()
    assert state.rows[0].note == 'Concurrent note'
    assert 'not saved' in panel.status.value
    assert panel.note.value == 'My draft'


def test_rejected_new_anchor_does_not_erase_current_draft(rig):
    panel, state, anchor = rig
    panel.note.SetValue('Unsaved existing note')
    state.fail_anchor = True
    assert panel.save_anchor(anchor) is None
    assert len(state.rows) == 1
    assert panel.note.value == 'Unsaved existing note'


def test_cancel_selection_preserves_draft(rig):
    panel, state, anchor = rig
    state.rows.append(Row('t', 'e2', anchor, 'Second note'))
    panel.refresh()
    panel.note.SetValue('My draft')
    panel.items.SetSelection(1)
    panel.select()
    assert panel.items.GetSelection() == 0
    assert panel.note.value == 'My draft'
    assert panel.note.focused
