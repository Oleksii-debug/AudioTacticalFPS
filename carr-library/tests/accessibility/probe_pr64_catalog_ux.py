"""Independent PR64 handler tests; no native wx, SQLite or NVDA claim.
CARR_CATALOG_PANEL must point to the exact reviewed candidate source.
"""
import importlib.util
import os
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch


class Control:
    def __init__(self, *args, **kwargs):
        self.items, self.index, self.value, self.focus = [], -1, '', False
    def SetName(self, name): self.name = name
    def Bind(self, *args): pass
    def SetSizer(self, *args): pass
    def Add(self, *args): pass
    def Clear(self): self.items.clear(); self.index = -1
    def Append(self, label): self.items.append(label)
    def GetSelection(self): return self.index
    def SetSelection(self, index): self.index = index
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def SetFocus(self): self.focus = True


wx = types.SimpleNamespace(**{n: Control for n in (
    'Panel', 'Choice', 'ListBox', 'TextCtrl', 'BoxSizer', 'StaticText', 'Button')},
    LB_SINGLE=1, TE_READONLY=2, VERTICAL=4, EXPAND=8, EVT_BUTTON=16,
    EVT_CHOICE=32, YES_NO=64, NO_DEFAULT=128, ICON_QUESTION=256, YES=1,
    MessageBox=lambda *args: 1)
spec = importlib.util.spec_from_file_location('catalog_panel_review', os.environ['CARR_CATALOG_PANEL'])
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'wx': wx}):
    spec.loader.exec_module(module)


class Service:
    workspace = 'a'
    def __init__(self):
        self.groups = {'c1': 'Railway history', 'c2': 'Industrial history'}
        self.members = {'c1': {'e1', 'e2'}, 'c2': set()}
        self.rows = {k: (k, 'work-'+k, 'asset-'+k, 'Same title', 'en', '1', '1')
                     for k in ('e1', 'e2')}
        self.archived = []
    def editions(self, collection):
        return tuple(r for k, r in self.rows.items()
                     if collection is None or k in self.members[collection])
    def remove(self, collection, edition):
        if edition not in self.members[collection]: return False
        self.members[collection].remove(edition)
        return True
    def archive(self, collection):
        self.archived.append(collection)
        del self.groups[collection]


class CatalogUX(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        @contextmanager
        def transaction():
            yield self.service
        self.panel = module.CatalogPanel(None, 'a', transaction, lambda s: s.groups.items())

    def select(self, book='e1', target='c1', collection='c1'):
        p = self.panel
        p.filter.SetSelection(p.filter_ids.index(collection))
        self.assertTrue(p.refresh())
        p.target.SetSelection(p.target_ids.index(target))
        p.books.SetSelection(p.book_ids.index(book))

    def test_archive_confirmation_names_destination_not_browse_filter(self):
        self.select(target='c2')
        with patch.object(wx, 'MessageBox', return_value=0) as confirm:
            self.panel.archive()
        self.assertIn('Industrial history', confirm.call_args.args[0])

    def test_cancel_archive_preserves_identity(self):
        self.select(target='c2')
        before = self.panel._selection()
        with patch.object(wx, 'MessageBox', return_value=0):
            self.panel.archive()
        self.assertEqual(self.service.archived, [])
        self.assertEqual(self.panel._selection(), before)

    def test_removed_selection_has_surviving_row_when_focus_returns(self):
        self.select()
        self.panel.remove()
        self.assertEqual(self.panel.book_ids, ['e2'])
        self.assertTrue(self.panel.books.focus)
        self.assertEqual(self.panel._selection()[2], 'e2')

    def test_last_removal_announces_empty_state(self):
        self.service.members['c1'].remove('e2')
        self.select()
        self.panel.remove()
        self.assertEqual(self.panel.book_ids, [])
        self.assertIn('No books', self.panel.status.value)

    def test_same_title_labels_are_distinct(self):
        self.assertEqual(len(set(self.panel.books.items)), 2)

    def test_scope_change_clears_cached_rows(self):
        self.select()
        self.service.workspace = 'b'
        self.panel.remove()
        self.assertFalse(self.panel.ready)
        self.assertEqual(self.panel.books.items, [])
        self.assertEqual(self.service.members['c1'], {'e1', 'e2'})


if __name__ == '__main__':
    unittest.main()
