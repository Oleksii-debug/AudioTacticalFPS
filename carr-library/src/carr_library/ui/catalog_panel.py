"""Native catalog UI; reuse Catalog, with caller-authorized collection enumeration.

The transaction factory yields a workspace-bound Catalog. collections(service)
returns active (id, name) pairs from that same authorized snapshot. No migration,
source deletion, raw SQL or implicit global search is performed by this panel.
"""
from uuid import uuid4

import wx
from carr_library.catalog.read_views import browse_editions


class CatalogPanel(wx.Panel):
    PAGE_SIZE = 200

    def __init__(self, parent, workspace, transaction, collections):
        super().__init__(parent)
        self.workspace = workspace
        self.transaction = transaction
        self.collections = collections
        self.ready = False
        self.groups = {}
        self.rows = {}
        self.filter_ids = []
        self.target_ids = []
        self.book_ids = []
        self._collection = None
        self._after = None
        self._history = []
        self._has_next = False
        self.filter = wx.Choice(self)
        self.books = wx.ListBox(self, style=wx.LB_SINGLE)
        self.previous_books = wx.Button(self, label='Previous books')
        self.next_books = wx.Button(self, label='Next books')
        self.previous_books.SetName('Previous books')
        self.next_books.SetName('Next books')
        self.previous_books.Bind(wx.EVT_BUTTON, self.previous_page)
        self.next_books.Bind(wx.EVT_BUTTON, self.next_page)
        self.target = wx.Choice(self)
        self.name = wx.TextCtrl(self)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY)
        layout = wx.BoxSizer(wx.VERTICAL)
        for label, control in [('Browse collection', self.filter),
                               ('Books and editions', self.books),
                               ('Membership destination', self.target),
                               ('New collection name', self.name)]:
            layout.Add(wx.StaticText(self, label=label))
            control.SetName(label)
            layout.Add(control, 1 if control is self.books else 0, wx.EXPAND)
            if control is self.books:
                layout.Add(self.previous_books)
                layout.Add(self.next_books)
        for label, action in [('Create collection', self.create),
                              ('Add selected edition to collection', self.assign),
                              ('Remove selected edition from collection', self.remove),
                              ('Archive selected collection', self.archive),
                              ('Refresh catalog', self.refresh)]:
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, action)
            layout.Add(button)
        self.status.SetName('Catalog operation status')
        layout.Add(self.status, 0, wx.EXPAND)
        self.SetSizer(layout)
        self.filter.Bind(wx.EVT_CHOICE, self.refresh)
        self.refresh()

    @staticmethod
    def _selected(control, ids):
        index = control.GetSelection()
        return ids[index] if 0 <= index < len(ids) else None

    def _selection(self):
        return (self._selected(self.filter, self.filter_ids),
                self._selected(self.target, self.target_ids),
                self._selected(self.books, self.book_ids))

    def _snapshot(self, service, collection, *, after=None):
        if service.workspace != self.workspace:
            raise ValueError('workspace changed')
        pairs = tuple(self.collections(service))
        groups = dict(pairs)
        if len(groups) != len(pairs) or None in groups:
            raise ValueError('invalid collection identities')
        if collection is not None and collection not in groups:
            raise ValueError('collection no longer active')
        # One extra row tells us whether Next is available without loading the
        # whole catalogue or inventing a total. Authors share this same snapshot.
        page = browse_editions(service, collection=collection, after=after,
                               limit=self.PAGE_SIZE + 1)
        editions = page[:self.PAGE_SIZE]
        rows = {r.edition_id: r for r in editions}
        if len(rows) != len(editions):
            raise ValueError('duplicate edition identity')
        return groups, rows, len(page) > self.PAGE_SIZE

    def _clear(self):
        self.ready = False
        self._has_next = False
        self.groups, self.rows = {}, {}
        self.filter_ids, self.target_ids, self.book_ids = [], [], []
        for control in (self.filter, self.target, self.books):
            control.Clear()
        self.previous_books.Enable(False)
        self.next_books.Enable(False)

    def _show_collections(self, groups, collection, target):
        self.groups = groups
        self.filter_ids = [None, *groups]
        self.target_ids = list(groups)
        self.filter.Append('All books in this library')
        for name in groups.values():
            self.filter.Append(f'Collection: {name}')
            self.target.Append(name)
        self.filter.SetSelection(self.filter_ids.index(collection))
        if target in self.target_ids:
            self.target.SetSelection(self.target_ids.index(target))
        elif self.target_ids:
            self.target.SetSelection(self.target_ids.index(collection) if collection in self.target_ids else 0)

    def refresh(self, event=None, *, selection=None):
        collection, target, edition = self._selection() if selection is None else selection
        after, history = self._after, list(self._history)
        if collection != self._collection:
            after, history = None, []
        fell_back = False
        try:
            with self.transaction() as service:
                groups, rows, has_next = self._snapshot(service, collection, after=after)
                # A removed final member can empty the current page. Revisit a
                # previous cursor rather than strand the user on an empty page.
                # Every query remains bounded and uses this authorized snapshot.
                while not rows and history:
                    after, _ = history.pop()
                    fell_back = True
                    groups, rows, has_next = self._snapshot(service, collection, after=after)
            self._clear()
            self._collection, self._after, self._history = collection, after, history
            self._has_next = has_next
            self._show_collections(groups, collection, target)
            self.rows = rows
            self.book_ids = list(rows)
            for number, row in enumerate(rows.values(),1):
                self.books.Append(f'{number}. {row.title or "Untitled"}; '
                    f'author {"; ".join(row.authors) or "unknown"}; language {row.language or "unknown"}; '
                    f'edition {row.edition or "unknown"}; volume {row.volume or "unknown"}')
            if edition in self.book_ids:
                self.books.SetSelection(self.book_ids.index(edition))
            elif self.book_ids:
                self.books.SetSelection(len(self.book_ids) - 1 if fell_back else 0)
            self.ready = True
            self.previous_books.Enable(bool(history))
            self.next_books.Enable(has_next)
            self.status.SetValue(self._page_status())
            return True
        except Exception:
            self._clear()
            self._collection, self._after, self._history = None, None, []
            self.status.SetValue('Catalog unavailable. Refresh after checking the active library.')
            return False

    def _page_status(self):
        if not self.rows:
            return ('This collection is empty. Add an existing book or import one.'
                    if self._collection is not None else
                    'This library has no books. Import a file or add original text.')
        return (f'Page {len(self._history) + 1}. Showing {len(self.rows)} editions. ' +
                ('More books are available. Choose Next books.' if self._has_next else 'Last page.'))

    def next_page(self, event=None):
        collection, target, edition = self._selection()
        if not self.ready:
            return False
        if collection != self._collection:
            return self.refresh()
        if not self._has_next or not self.book_ids:
            return False
        self._history.append((self._after, edition))
        self._after = self.book_ids[-1]
        if self.refresh(selection=(collection, target, None)):
            (self.books if self.book_ids else self.status).SetFocus()
            return True
        self.status.SetFocus()
        return False

    def previous_page(self, event=None):
        collection, target, _ = self._selection()
        if not self.ready:
            return False
        if collection != self._collection:
            return self.refresh()
        if not self._history:
            return False
        self._after, edition = self._history.pop()
        if self.refresh(selection=(collection, target, edition)):
            (self.books if self.book_ids else self.status).SetFocus()
            return True
        self.status.SetFocus()
        return False

    def _change(self, action, selection, *, needs_target=False, needs_book=False):
        if (not self.ready or selection != self._selection() or
                selection[0] != self._collection):
            self.status.SetValue('Selection changed. Refresh and try again.')
            self.status.SetFocus()
            return
        collection, target, edition = selection
        try:
            with self.transaction() as service:
                groups, rows, _ = self._snapshot(service, collection, after=self._after)
                if needs_target and (target not in groups or groups[target] != self.groups.get(target)):
                    raise ValueError('stale collection')
                if needs_book and (edition not in rows or rows[edition] != self.rows.get(edition)):
                    raise ValueError('stale edition')
                next_selection = action(service, selection)
        except Exception:
            self.refresh()
            self.status.SetValue('Catalog change rejected. Review the selection and try again.')
            self.status.SetFocus()
            return
        if self.refresh(selection=next_selection):
            self.status.SetValue('Catalog updated. ' +
                ('This collection is now empty.' if not self.rows and next_selection[0] is not None
                 else self._page_status()))
            (self.books if self.book_ids else self.status).SetFocus()
        else:
            self.status.SetValue('Catalog change committed; refresh failed. Refresh before another change.')
            self.status.SetFocus()

    def create(self, event=None):
        def action(service, selected):
            key = uuid4().hex
            service.create_collection(key, self.name.GetValue())
            return (selected[0], key, selected[2])
        self._change(action, self._selection())

    def assign(self, event=None):
        def action(service, selected):
            service.assign(selected[1], selected[2])
            return selected
        self._change(action, self._selection(), needs_target=True, needs_book=True)

    def remove(self, event=None):
        def action(service, selected):
            if not service.remove(selected[1], selected[2]):
                raise ValueError('membership absent')
            return selected
        self._change(action, self._selection(), needs_target=True, needs_book=True)

    def archive(self, event=None):
        selected = self._selection()
        if not self.ready or selected[1] not in self.groups:
            self.status.SetValue('Choose a collection to archive.')
            self.status.SetFocus()
            return
        if wx.MessageBox(f'Archive collection "{self.groups[selected[1]]}"? Books will remain in the library.',
                         'Confirm collection archive', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                         self) != wx.YES:
            return
        def action(service, selection):
            service.archive(selection[1])
            return (None if selection[0] == selection[1] else selection[0], None, selection[2])
        self._change(action, selected, needs_target=True)
