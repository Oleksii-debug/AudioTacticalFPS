"""Integrated CatalogPanel keyboard/label regression; controlled wx and catalog doubles.

Run in assembled carr-library with scripts/ci_check.py. These tests exercise
actual panel handlers, not native Windows focus events or NVDA speech.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest


class Control:
    def __init__(self, *args, **kwargs):
        self.items, self.index, self.value = [], -1, ''
        self.focus, self.enabled = False, True

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
    def Enable(self, enabled): self.enabled = enabled
    def IsEnabled(self): return self.enabled


class CatalogDouble:
    workspace = 'w'

    def __init__(self):
        # Active collection names are unique in production SQLite, but the
        # built-in UI option is not a database row or a reserved name.
        self.groups = {'c': 'All books in this library', 'd': 'Research'}
        self.members = {'c': {'e000'}, 'd': set()}
        self.rows = [types.SimpleNamespace(edition_id=f'e{i:03d}', title='Book',
                     authors=('Author',), language='en', edition='First', volume='I')
                     for i in range(201)]

    def remove(self, collection, edition):
        if edition not in self.members[collection]:
            return False
        self.members[collection].remove(edition)
        return True


def browse(service, *, collection=None, after=None, limit=201):
    return tuple(row for row in service.rows
                 if (collection is None or row.edition_id in service.members[collection])
                 and (after is None or row.edition_id > after))[:limit]


@pytest.fixture
def ui():
    wx = types.SimpleNamespace(**{name: Control for name in (
        'Panel', 'Choice', 'ListBox', 'TextCtrl', 'BoxSizer', 'StaticText', 'Button')},
        LB_SINGLE=1, TE_READONLY=2, VERTICAL=4, EXPAND=8, EVT_BUTTON=16,
        EVT_CHOICE=32, YES_NO=64, NO_DEFAULT=128, ICON_QUESTION=256, YES=1,
        MessageBox=lambda *args: 0)
    views = types.ModuleType('carr_library.catalog.read_views')
    views.browse_editions = browse
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/catalog_panel.py'
    spec = importlib.util.spec_from_file_location('_carr_pr71_catalog_probe', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx, 'carr_library.catalog.read_views': views}):
        spec.loader.exec_module(module)
    service = CatalogDouble()

    @contextmanager
    def tx():
        yield service

    panel = module.CatalogPanel(None, 'w', tx, lambda s: s.groups.items())
    return panel, service, wx


def test_builtin_scope_and_same_named_collection_are_distinguishable(ui):
    panel, _, _ = ui
    assert panel.filter_ids[:2] == [None, 'c']
    assert panel.filter.items[0] != panel.filter.items[1]
    assert 'All books in this library' in panel.filter.items[1]


def test_scope_choice_preserves_actual_collection_identity(ui):
    panel, _, _ = ui
    panel.filter.SetSelection(panel.filter_ids.index('c'))
    assert panel.refresh()
    assert panel.book_ids == ['e000']
    assert panel._selection()[0] == 'c'
    panel.filter.SetSelection(0)
    assert panel.refresh()
    assert len(panel.book_ids) == 200
    assert panel._selection()[0] is None


def test_removing_last_member_focuses_explanation_not_empty_list(ui):
    panel, service, _ = ui
    panel.filter.SetSelection(panel.filter_ids.index('c'))
    assert panel.refresh()
    panel.target.SetSelection(panel.target_ids.index('c'))
    panel.remove()
    assert not service.members['c']
    assert panel.book_ids == [] and panel.books.GetSelection() == -1
    assert 'collection is now empty' in panel.status.value
    assert panel.status.focus and not panel.books.focus


def test_failed_next_page_focuses_error_when_navigation_is_disabled(ui):
    panel, service, _ = ui
    assert panel.next_books.IsEnabled()
    service.workspace = 'revoked'
    assert panel.next_page() is False
    assert not panel.next_books.IsEnabled() and not panel.ready
    assert 'Catalog unavailable' in panel.status.value
    assert panel.status.focus


def test_previous_page_restores_exact_selection(ui):
    panel, _, _ = ui
    panel.books.SetSelection(123)
    assert panel.next_page()
    assert panel.book_ids == ['e200']
    assert panel.previous_page()
    assert panel._selection()[2] == 'e123'
    assert panel.books.focus


def test_archive_cancel_names_target_and_does_not_write(ui):
    panel, service, wx = ui
    panel.target.SetSelection(panel.target_ids.index('d'))
    with patch.object(wx, 'MessageBox', return_value=0) as confirm:
        panel.archive()
    assert '"Research"' in confirm.call_args.args[0]
    assert confirm.call_args.args[2] & wx.NO_DEFAULT
    assert 'd' in service.groups


def test_failed_previous_page_focuses_error_when_navigation_is_disabled(ui):
    panel, service, _ = ui
    assert panel.next_page()
    assert panel.previous_books.IsEnabled()
    panel.books.focus = False
    service.workspace = 'revoked'
    assert panel.previous_page() is False
    assert not panel.previous_books.IsEnabled() and not panel.ready
    assert 'Catalog unavailable' in panel.status.value
    assert panel.status.focus
