"""Independent catalogue acceptance: actual SQLite services, headless controls.

These checks verify user actions and stored identities rather than native wx or
NVDA behaviour. The latter still requires a Windows acceptance run.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import patch

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.catalog.read_views import active_collections
from carr_library.storage.revisions import Page, Storage


class Control:
    def __init__(self, *args, **kwargs):
        self.items = []
        self.index = -1
        self.value = ''
        self.focus = False
        self.enabled = True

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


@pytest.fixture
def panel_class():
    controls = ('Panel', 'Choice', 'ListBox', 'TextCtrl', 'BoxSizer',
                'StaticText', 'Button')
    wx = types.SimpleNamespace(**{name: Control for name in controls},
        LB_SINGLE=1, TE_READONLY=2, VERTICAL=4, EXPAND=8, EVT_BUTTON=16,
        EVT_CHOICE=32, YES_NO=64, NO_DEFAULT=128, ICON_QUESTION=256,
        YES=1, MessageBox=lambda *args: 1)
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/catalog_panel.py'
    spec = importlib.util.spec_from_file_location('_independent_catalog_panel', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx}):
        spec.loader.exec_module(module)
    return module.CatalogPanel


class Library:
    def __init__(self, path):
        self.storage = Storage(path, schema_version=2)
        self.workspace = 'a'
        with self.storage.transaction():
            self.storage.db.execute("INSERT INTO workspace VALUES('a'),('b')")
            for scope in ('a', 'b'):
                catalog = Catalog(self.storage.db, scope)
                catalog.create_collection('small', 'Small collection')
                catalog.create_collection('large', 'Large collection')
                catalog.create_collection('destination', 'Destination')
            other = Catalog(self.storage.db, 'b')
            other.add_work('work-00000', 'Private title', ('Private author',))
            other.add_edition('edition-00000', 'work-00000',
                             dict(sha256='b' * 64, mime='text/plain', size=16))
            other.assign('small', 'edition-00000')

    def populate(self, count):
        with self.transaction() as catalog:
            for n in range(count):
                identity = f'{n:05d}'
                catalog.add_work(f'work-{identity}', 'Однакова назва',
                                 (f'Author {identity}', 'Žan', 'Олена'))
                catalog.add_edition(f'edition-{identity}', f'work-{identity}',
                    dict(sha256='a' * 64, mime='text/plain', size=16),
                    language='uk', edition='First', volume='II')
                catalog.assign('large', f'edition-{identity}')
                if n in (0, count - 1):
                    catalog.assign('small', f'edition-{identity}')
            self.storage.stage_revision('a', 'edition-00000', 'revision-a',
                'a' * 64, 'synthetic-catalog-acceptance', [Page('Source body α')])

    @contextmanager
    def transaction(self):
        with self.storage.transaction():
            yield Catalog(self.storage.db, self.workspace)

    def make_panel(self, panel_class):
        return panel_class(None, 'a', self.transaction, active_collections)

    def stored_sources(self):
        db = self.storage.db
        return tuple(tuple(r) for r in db.execute('SELECT * FROM catalog_asset ORDER BY digest')), \
            tuple(tuple(r) for r in db.execute('SELECT * FROM page ORDER BY id'))


@pytest.fixture
def library(tmp_path):
    fixture = Library(tmp_path / 'catalog.sqlite')
    try:
        yield fixture
    finally:
        fixture.storage.close()


def choose(panel, *, collection=None, target='destination', edition=None):
    panel.filter.SetSelection(panel.filter_ids.index(collection))
    assert panel.refresh()
    panel.target.SetSelection(panel.target_ids.index(target))
    if edition is not None:
        while edition not in panel.book_ids and panel.next_books.IsEnabled():
            assert panel.next_page()
        panel.books.SetSelection(panel.book_ids.index(edition))


def test_all_author_rows_beyond_two_pages_are_reachable(library, panel_class):
    library.populate(1001)
    before = library.stored_sources()
    panel = library.make_panel(panel_class)
    assert panel.ready
    assert panel.book_ids == [f'edition-{n:05d}' for n in range(200)]
    assert not panel.previous_books.IsEnabled() and panel.next_books.IsEnabled()
    seen = list(panel.book_ids)
    labels = list(panel.books.items)
    assert panel._selection()[2] == 'edition-00000'
    while panel.next_books.IsEnabled():
        assert panel.next_page()
        assert 0 < len(panel.book_ids) <= 200
        seen.extend(panel.book_ids)
        labels.extend(panel.books.items)
    assert seen == [f'edition-{n:05d}' for n in range(1001)]
    assert len(set(labels)) == 1001
    assert 'Author 01000; Žan; Олена' in panel.books.items[-1]
    assert not any('Private' in label for label in labels)
    assert panel._selection()[2] == 'edition-01000'
    assert 'Last page' in panel.status.value
    assert library.stored_sources() == before


def test_filtered_membership_action_uses_exact_last_page_identity(library, panel_class):
    library.populate(1001)
    before = library.stored_sources()
    panel = library.make_panel(panel_class)
    choose(panel, collection='large', edition='edition-01000')
    panel.assign()
    assert panel._selection() == ('large', 'destination', 'edition-01000')
    with library.transaction() as catalog:
        assert [r[0] for r in catalog.editions('destination')] == ['edition-01000']
        assert len(catalog.editions('large')) == 1001
    panel.remove()
    with library.transaction() as catalog:
        assert catalog.editions('destination') == ()
        assert len(catalog.editions('large')) == 1001
    assert library.stored_sources() == before


def test_author_change_rejects_stale_row_before_membership_write(library, panel_class):
    library.populate(2)
    panel = library.make_panel(panel_class)
    choose(panel, edition='edition-00001')
    with library.transaction() as catalog:
        catalog.db.execute('UPDATE catalog_author SET name=? '
            'WHERE workspace=? AND work=? AND position=0',
            ('Corrected author', 'a', 'work-00001'))
    panel.assign()
    assert 'rejected' in panel.status.value
    with library.transaction() as catalog:
        assert catalog.editions('destination') == ()
    assert 'Corrected author' in panel.books.items[1]
    panel.assign()
    with library.transaction() as catalog:
        assert [r[0] for r in catalog.editions('destination')] == ['edition-00001']


def test_changed_workspace_cannot_reuse_selected_catalog_identity(library, panel_class):
    library.populate(2)
    panel = library.make_panel(panel_class)
    choose(panel, edition='edition-00000')
    library.workspace = 'b'
    panel.assign()
    assert panel.ready is False
    assert panel.books.items == [] and panel.filter.items == []
    with library.transaction() as catalog:
        assert catalog.editions('destination') == ()
    assert 'Private' not in panel.status.value


@pytest.mark.parametrize('count, has_next', [(199, False), (200, False), (201, True)])
def test_next_button_describes_actual_remaining_books(library, panel_class, count, has_next):
    library.populate(count)
    panel = library.make_panel(panel_class)
    assert panel.ready
    assert len(panel.book_ids) == min(200, count)
    assert panel.next_books.IsEnabled() is has_next
    if has_next:
        assert 'Next books' in panel.status.value
    else:
        assert 'Last page' in panel.status.value
        assert panel.next_page() is False


def test_more_than_ten_thousand_books_are_reachable_without_collections(library, panel_class):
    library.populate(10001)
    with library.transaction() as catalog:
        for identity, _ in active_collections(catalog):
            catalog.archive(identity)
    before = library.stored_sources()
    panel = library.make_panel(panel_class)
    assert panel.ready and panel.filter_ids == [None] and panel.target_ids == []
    assert len(panel.book_ids) == 200
    assert 'Page 1.' in panel.status.value and 'Next books' in panel.status.value
    seen = []
    while True:
        assert 0 < len(panel.book_ids) <= 200
        seen.extend(panel.book_ids)
        if not panel.next_books.IsEnabled():
            break
        assert panel.next_page()
    assert seen == [f'edition-{n:05d}' for n in range(10001)]
    assert panel.book_ids == ['edition-10000']
    assert 'Author 10000; Žan; Олена' in panel.books.items[0]
    assert 'Page 51.' in panel.status.value and 'Last page' in panel.status.value
    assert panel.previous_books.IsEnabled()
    assert library.stored_sources() == before


def test_collection_change_resets_cursor_and_history(library, panel_class):
    library.populate(401)
    panel = library.make_panel(panel_class)
    choose(panel, collection='large', edition='edition-00300')
    assert panel.previous_books.IsEnabled()
    panel.filter.SetSelection(panel.filter_ids.index('small'))
    assert panel.refresh()
    assert panel.book_ids == ['edition-00000', 'edition-00400']
    assert not panel.previous_books.IsEnabled() and not panel.next_books.IsEnabled()
    panel.filter.SetSelection(panel.filter_ids.index('large'))
    assert panel.refresh()
    assert panel.book_ids[0] == 'edition-00000'
    assert not panel.previous_books.IsEnabled() and panel.next_books.IsEnabled()
    assert panel._selection()[:2] == ('large', 'destination')


def test_previous_page_restores_the_prior_selected_identity(library, panel_class):
    library.populate(401)
    panel = library.make_panel(panel_class)
    panel.books.SetSelection(panel.book_ids.index('edition-00123'))
    assert panel.next_page()
    assert panel.book_ids == [f'edition-{n:05d}' for n in range(200, 400)]
    panel.books.SetSelection(panel.book_ids.index('edition-00245'))
    assert panel.next_page()
    assert panel.book_ids == ['edition-00400'] and not panel.next_books.IsEnabled()
    assert panel.previous_page()
    assert panel._selection()[2] == 'edition-00245'
    assert panel.previous_page()
    assert panel._selection()[2] == 'edition-00123'
    assert not panel.previous_books.IsEnabled()
    assert panel.previous_page() is False


def test_removing_only_book_on_last_page_returns_to_previous_page(library, panel_class):
    library.populate(401)
    before = library.stored_sources()
    panel = library.make_panel(panel_class)
    choose(panel, collection='large', target='large', edition='edition-00400')
    assert panel.book_ids == ['edition-00400']
    panel.remove()
    assert panel.book_ids == [f'edition-{n:05d}' for n in range(200, 400)]
    assert panel._selection() == ('large', 'large', 'edition-00399')
    assert panel.previous_books.IsEnabled() and not panel.next_books.IsEnabled()
    assert 'Page 2.' in panel.status.value and 'Catalog updated.' in panel.status.value
    assert panel.books.focus
    with library.transaction() as catalog:
        assert len(catalog.editions('large')) == 400
        assert len(catalog.editions()) == 401
    assert library.stored_sources() == before


def test_membership_removed_after_display_rejects_stale_page_selection(library, panel_class):
    library.populate(401)
    panel = library.make_panel(panel_class)
    choose(panel, collection='large', edition='edition-00200')
    with library.transaction() as catalog:
        catalog.remove('large', 'edition-00200')
    panel.assign()
    assert 'rejected' in panel.status.value
    with library.transaction() as catalog:
        assert catalog.editions('destination') == ()
    assert panel.book_ids[0] == 'edition-00201'


def test_new_row_moving_selection_off_page_cannot_redirect_membership_write(library, panel_class):
    library.populate(401)
    panel = library.make_panel(panel_class)
    choose(panel, collection='large', edition='edition-00399')
    with library.transaction() as catalog:
        catalog.add_work('new-work', 'New book', ('New author',))
        catalog.add_edition('edition-00200-new', 'new-work',
            dict(sha256='a' * 64, mime='text/plain', size=16))
        catalog.assign('large', 'edition-00200-new')
    panel.assign()
    assert 'rejected' in panel.status.value
    with library.transaction() as catalog:
        assert catalog.editions('destination') == ()
    assert 'edition-00399' not in panel.book_ids


def test_next_page_rechecks_workspace_authorization(library, panel_class):
    library.populate(201)
    panel = library.make_panel(panel_class)
    assert panel.next_books.IsEnabled()
    library.workspace = 'b'
    assert panel.next_page() is False
    assert not panel.ready and panel.books.items == []
    assert panel.filter.items == [] and panel.target.items == []
    assert not panel.previous_books.IsEnabled() and not panel.next_books.IsEnabled()
    assert 'Private' not in panel.status.value
