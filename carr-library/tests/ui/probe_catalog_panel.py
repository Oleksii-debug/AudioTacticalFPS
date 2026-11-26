"""Explicit headless probe: actual PR53 SQLite Catalog, not native wx acceptance."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from carr_library.catalog.membership import Catalog


class Control:
    def __init__(self, *args, **kwargs):
        self.items = []
        self.index = -1
        self.value = ''
        self.focus = False

    def SetName(self, name): self.name = name
    def Bind(self, event, handler): pass
    def SetSizer(self, layout): pass
    def Add(self, *args): pass
    def Clear(self): self.items.clear(); self.index = -1
    def Append(self, label): self.items.append(label)
    def GetSelection(self): return self.index
    def SetSelection(self, index): self.index = index
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def SetFocus(self): self.focus = True


wx = types.SimpleNamespace(**{name: Control for name in (
    'Panel', 'Choice', 'ListBox', 'TextCtrl', 'BoxSizer', 'StaticText', 'Button')},
    LB_SINGLE=1, TE_READONLY=2, VERTICAL=4, EXPAND=8, EVT_BUTTON=16,
    EVT_CHOICE=32, YES_NO=64, NO_DEFAULT=128, ICON_QUESTION=256, YES=1,
    MessageBox=lambda *args: 1)
path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/catalog_panel.py'
spec = importlib.util.spec_from_file_location('_catalog_panel_probe', path)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'wx': wx}):
    spec.loader.exec_module(module)


class CatalogPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(Path(self.tmp.name) / 'synthetic.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript('PRAGMA foreign_keys=ON; PRAGMA recursive_triggers=ON; '
                              'CREATE TABLE workspace(id TEXT PRIMARY KEY); '
                              "INSERT INTO workspace VALUES('a'),('b');")
        self.db.executescript((Path(__file__).resolve().parents[1] /
                              'catalog/catalog_membership_candidate.sql').read_text())
        self.active = 'a'
        with self.transaction() as catalog:
            for key in ('e1', 'e2'):
                catalog.add_work(key, 'Історія', ['Author ' + key])
                catalog.add_edition(key, key, {'sha256': 'a'*64, 'mime': 'text/plain', 'size': 2})
            for key in ('c1', 'c2'):
                catalog.create_collection(key, key)
            catalog.assign('c1', 'e1')
        self.active = 'b'
        with self.transaction() as catalog:
            catalog.add_work('e1', 'Private synthetic B')
            catalog.add_edition('e1', 'e1', {'sha256': 'a'*64, 'mime': 'text/plain', 'size': 2})
            catalog.create_collection('c1', 'Other workspace')
        self.active = 'a'
        self.panel = module.CatalogPanel(None, 'a', self.transaction, self.collections)

    @contextmanager
    def transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield Catalog(self.db, self.active)

    @staticmethod
    def collections(service):
        # Test-only stand-in for caller-authorized collection enumeration API.
        return service.db.execute('SELECT id,name FROM catalog_collection WHERE workspace=? '
                                  'AND archived=0 ORDER BY id', (service.workspace,)).fetchall()

    def select(self, book='e1', target='c2', collection=None):
        p = self.panel
        p.filter.SetSelection(p.filter_ids.index(collection))
        p.refresh()
        p.books.SetSelection(p.book_ids.index(book))
        p.target.SetSelection(p.target_ids.index(target))

    def test_duplicate_title_and_workspace(self):
        self.assertEqual(self.panel.book_ids, ['e1', 'e2'])
        self.assertNotEqual(*self.panel.books.items)
        self.assertTrue(all('Історія' in x for x in self.panel.books.items))
        self.assertEqual(self.panel.target_ids, ['c1', 'c2'])

    def test_membership_only_remove(self):
        self.select()
        self.panel.assign()
        self.assertEqual(len(Catalog(self.db, 'a').editions('c2')), 1)
        self.panel.remove()
        self.assertEqual(Catalog(self.db, 'a').editions('c2'), ())
        self.assertEqual(len(Catalog(self.db, 'a').editions('c1')), 1)
        self.assertEqual(len(Catalog(self.db, 'a').editions()), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)

    def test_create_and_filter_empty(self):
        self.panel.name.SetValue('Нова колекція')
        self.panel.create()
        key = self.panel._selection()[1]
        self.assertEqual(self.panel.groups[key], 'Нова колекція')
        self.panel.filter.SetSelection(self.panel.filter_ids.index(key))
        self.panel.refresh()
        self.assertEqual(self.panel.book_ids, [])
        self.assertIn('No books', self.panel.status.value)

    def test_workspace_switch_fails_closed(self):
        self.select()
        self.active = 'b'
        self.panel.assign()
        self.assertFalse(self.panel.ready)
        self.assertEqual(self.panel.books.items, [])
        self.assertEqual(Catalog(self.db, 'b').editions('c1'), ())
        self.assertEqual(Catalog(self.db, 'a').editions('c2'), ())

    def test_stale_collection_rejected(self):
        self.select()
        with self.transaction() as catalog:
            catalog.archive('c2')
        self.panel.assign()
        self.assertIn('rejected', self.panel.status.value)
        self.assertNotIn('c2', self.panel.target_ids)

    def test_archive_confirm_selection_race(self):
        self.select()
        def change_selection(*args):
            self.panel.target.SetSelection(0)
            return wx.YES
        with patch.object(wx, 'MessageBox', change_selection):
            self.panel.archive()
        self.assertEqual(len(self.collections(Catalog(self.db, 'a'))), 2)
        self.assertIn('Selection changed', self.panel.status.value)

    def test_archive_preserves_books_and_membership(self):
        self.select(target='c1', collection='c1')
        self.panel.archive()
        self.assertEqual(self.panel._selection()[0], None)
        self.assertEqual(self.panel.book_ids, ['e1', 'e2'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM catalog_membership WHERE workspace='a'").fetchone()[0], 1)

    def test_write_requires_recursive_triggers(self):
        self.select()
        self.db.execute('PRAGMA recursive_triggers=OFF')
        self.panel.assign()
        self.assertIn('rejected', self.panel.status.value)
        self.assertEqual(Catalog(self.db, 'a').editions('c2'), ())


if __name__ == '__main__':
    unittest.main()
