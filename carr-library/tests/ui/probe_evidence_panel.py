"""Headless native-handler probe. Requires the PR54/49/38/26 candidate modules."""
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager
from dataclasses import replace


class Control:
    def __init__(self, *args, **kwargs):
        self.value, self.selection, self.options, self.enabled = '', -1, [], True
    def __getattr__(self, name):
        if name in ('SetName', 'Add', 'Bind', 'SetSizer', 'SetFocus'):
            return lambda *a, **k: None
        raise AttributeError(name)
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def Clear(self): self.options.clear(); self.selection = -1
    def Append(self, label): self.options.append(label)
    def Enable(self, value): self.enabled = value


wx = types.ModuleType('wx')
for name in ('Panel', 'ListBox', 'TextCtrl', 'BoxSizer', 'StaticText', 'Button'):
    setattr(wx, name, Control)
for name in ('LB_SINGLE', 'TE_MULTILINE', 'TE_READONLY', 'VERTICAL', 'EXPAND', 'EVT_BUTTON', 'EVT_LISTBOX',
             'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'YES'):
    setattr(wx, name, 1)
wx.MessageBox = lambda *a: wx.YES
sys.modules['wx'] = wx
from carr_library.ui.evidence_panel import EvidencePanel
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.topic_tree import TopicTree


class EvidencePanelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''PRAGMA foreign_keys=ON;
            CREATE TABLE workspace(id TEXT PRIMARY KEY);
            INSERT INTO workspace VALUES('a'),('b');
            CREATE TABLE research_topic(workspace TEXT NOT NULL REFERENCES workspace(id),
                id TEXT NOT NULL,parent TEXT,name TEXT NOT NULL,position INTEGER NOT NULL,
                archived INTEGER NOT NULL,PRIMARY KEY(workspace,id),
                FOREIGN KEY(workspace,parent) REFERENCES research_topic(workspace,id));
            CREATE TABLE research_evidence(workspace TEXT NOT NULL,topic TEXT NOT NULL,
                id TEXT NOT NULL,payload TEXT NOT NULL,checksum TEXT NOT NULL,note TEXT NOT NULL,
                PRIMARY KEY(workspace,topic,id),FOREIGN KEY(workspace,topic) REFERENCES research_topic(workspace,id));''')
        self.active = 'a'
        self.raw = 'Цитата — citácia.\nOriginal.'
        self.identity = Identity('a', 'ed', 'a'*64, 'r1')
        self.loc = Locator('physical_page', 3)
        self.meta = Metadata('Author A', 'Title')
        self.anchor = make_anchor(self.identity, self.loc, self.raw, 0, 17, self.meta, text_verified=True)
        with self.transaction() as s:
            s.tree.create('t', 'Topic')
            s.save('t', 'e', self.anchor, 'Original note')
        self.active = 'b'
        with self.transaction() as s: s.tree.create('t', 'Other topic')
        self.active = 'a'
        self.panel = EvidencePanel(None, 'a', 't', self.transaction)

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            yield SavedEvidenceStore(self.db, self.active, self.load, self.attest)

    def load(self, identity, locator):
        return self.identity, self.loc, self.raw, True

    def attest(self, identity, meta):
        return identity == self.identity and meta == self.meta

    def test_exact_and_edit(self):
        self.assertEqual(self.panel.quote.value, self.anchor.quote)
        self.assertIn('printed page unverified', self.panel.citation.value)
        self.panel.note.SetValue('Нова примітка')
        self.panel.save_note()
        with self.transaction() as s:
            row, = s.list_saved('t')
            self.assertEqual(row.note, 'Нова примітка')
            self.assertEqual(row.anchor, self.anchor)

    def test_stale_note(self):
        with self.transaction() as s: s.edit_note('t', 'e', 'Elsewhere')
        self.panel.note.SetValue('Overwrite')
        self.panel.save_note()
        self.assertIn('not saved', self.panel.status.value)
        self.assertEqual(self.panel.note.value, 'Elsewhere')

    def test_source_drift(self):
        self.raw += ' Changed'
        self.panel.save_note()
        self.assertEqual(self.panel.quote.value, '')
        self.assertEqual(self.panel.citation.value, '')
        self.assertFalse(self.panel.save_button.enabled)

    def test_workspace_change(self):
        self.active = 'b'
        self.panel.save_note()
        self.assertEqual(self.panel.rows, ())
        with self.transaction() as s: self.assertEqual(s.list_saved('t'), ())

    def test_archived_read_only(self):
        with self.transaction() as s: s.tree.archive('t')
        self.panel.refresh()
        self.assertEqual(self.panel.quote.value, self.anchor.quote)
        self.assertFalse(self.panel.note.enabled)
        self.panel.save_note()
        self.assertIn('not saved', self.panel.status.value)

    def test_save_trusted_anchor(self):
        evidence = self.panel.save_anchor(self.anchor, 'Second')
        self.assertTrue(evidence)
        self.assertEqual(self.panel._selected().evidence_id, evidence)
        self.assertIsNone(self.panel.save_anchor(replace(self.anchor, quote='fabricated')))
        self.assertEqual(len(self.panel.rows), 2)

    def test_unsaved_selection_cancel(self):
        second = self.panel.save_anchor(self.anchor, 'Second')
        self.panel.note.SetValue('Draft')
        original = self.panel.displayed
        self.assertEqual(original.evidence_id, second)
        self.panel.items.SetSelection(1 - self.panel.items.GetSelection())
        wx.MessageBox = lambda *a: 0
        self.addCleanup(setattr, wx, 'MessageBox', lambda *a: wx.YES)
        self.panel.select()
        self.assertEqual(self.panel._selected(), original)
        self.assertEqual(self.panel.note.value, 'Draft')

    def test_unannounced_selection_rejects_save(self):
        self.panel.save_anchor(self.anchor, 'Second')
        self.panel.items.SetSelection(1 - self.panel.items.GetSelection())
        self.panel.note.SetValue('Wrong row')
        self.panel.save_note()
        self.assertIn('not saved', self.panel.status.value)
        with self.transaction() as s:
            self.assertNotIn('Wrong row', [r.note for r in s.list_saved('t')])

    def test_empty_topic(self):
        with self.transaction() as s: s.tree.create('empty', 'Empty')
        p = EvidencePanel(None, 'a', 'empty', self.transaction)
        self.assertEqual(p.rows, ())
        self.assertFalse(p.save_button.enabled)


if __name__ == '__main__':
    unittest.main()
