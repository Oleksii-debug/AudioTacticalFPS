"""Headless handler probe; requires PR49 TopicTree, not native wx/NVDA."""
import sqlite3
import sys
import types
import unittest
from contextlib import contextmanager


class Item:
    def __init__(self, data=None, valid=True):
        self.data, self.valid = data, valid
    def IsOk(self):
        return self.valid


class Control:
    def __init__(self, *args, **kwargs):
        self.value, self.selection, self.options = '', Item(valid=False), []
    def __getattr__(self, name):
        if name in ('SetName', 'Add', 'Bind', 'SetSizer', 'EnsureVisible', 'Expand', 'SetFocus'):
            return lambda *a, **k: None
        raise AttributeError(name)
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def SelectItem(self, item): self.selection = item
    def GetItemData(self, item): return item.data
    def DeleteAllItems(self): self.selection = Item(valid=False)
    def AddRoot(self, label): return Item()
    def AppendItem(self, parent, label, data=None): return Item(data)
    def Clear(self): self.options.clear(); self.selection = -1
    def Append(self, label): self.options.append(label)


wx = types.ModuleType('wx')
for name in ('Panel', 'TreeCtrl', 'TextCtrl', 'Choice', 'BoxSizer', 'StaticText', 'Button'):
    setattr(wx, name, Control)
for name in ('TR_DEFAULT_STYLE', 'TR_SINGLE', 'TE_READONLY', 'VERTICAL', 'EXPAND',
             'EVT_BUTTON', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'YES'):
    setattr(wx, name, 1)
wx.MessageBox = lambda *a: wx.YES
sys.modules['wx'] = wx
from carr_library.ui.topic_panel import TopicPanel
from carr_library.research.topic_tree import TopicTree


class TopicPanelTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''PRAGMA foreign_keys=ON;
            CREATE TABLE workspace(id TEXT PRIMARY KEY);
            INSERT INTO workspace VALUES('a'),('b');
            CREATE TABLE research_topic(workspace TEXT NOT NULL REFERENCES workspace(id),
                id TEXT NOT NULL,parent TEXT,name TEXT NOT NULL,position INTEGER NOT NULL,
                archived INTEGER NOT NULL,PRIMARY KEY(workspace,id),
                FOREIGN KEY(workspace,parent) REFERENCES research_topic(workspace,id));''')
        self.active = 'a'
        with self.transaction() as s:
            s.create('one', 'Parent')
            s.create('two', 'Child', parent='one')
        self.active = 'b'
        with self.transaction() as s:
            s.create('one', 'Private other library')
        self.active = 'a'
        self.panel = TopicPanel(None, 'a', self.transaction)
    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            yield TopicTree(self.db, self.active)
    def select(self, topic): self.panel.tree.SelectItem(self.panel.items[topic])
    def test_create_and_rename(self):
        self.select('two')
        self.panel.name.SetValue('Розділ')
        self.panel.create_child()
        selected = self.panel._selected()
        self.assertEqual(self.panel.rows[selected].parent, 'two')
        self.panel.name.SetValue('Renamed')
        self.panel.rename()
        self.assertEqual(self.panel.rows[selected].name, 'Renamed')
    def test_move_and_cycle(self):
        self.select('one')
        self.panel.destination.SetSelection(self.panel.parents.index('two'))
        self.panel.move()
        self.assertIn('rejected', self.panel.status.value)
        self.select('two')
        self.panel.destination.SetSelection(0)
        self.panel.move()
        self.assertIsNone(self.panel.rows['two'].parent)
    def test_archive(self):
        self.select('one')
        self.panel.archive()
        self.assertEqual(self.panel.rows, {})
        with self.transaction() as service:
            self.assertEqual(len(service.list_topics(include_archived=True)), 2)
    def test_stale_selection(self):
        self.select('one')
        with self.transaction() as service: service.rename('one', 'Changed elsewhere')
        self.panel.name.SetValue('Overwrite')
        self.panel.rename()
        self.assertIn('rejected', self.panel.status.value)
        self.assertEqual(self.panel.rows['one'].name, 'Changed elsewhere')
    def test_workspace_change(self):
        self.select('one')
        self.active = 'b'
        self.panel.name.SetValue('Overwrite')
        self.panel.rename()
        self.assertEqual(self.panel.rows, {})
        with self.transaction() as service:
            self.assertEqual(service.list_topics()[0].name, 'Private other library')
    def test_empty_name(self):
        self.panel.create_root()
        self.assertIn('rejected', self.panel.status.value)
        self.assertEqual(len(self.panel.rows), 2)


if __name__ == '__main__':
    unittest.main()
