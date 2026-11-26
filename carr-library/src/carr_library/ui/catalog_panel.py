"""Native catalog UI; reuse Catalog, with caller-authorized collection enumeration.

The transaction factory yields a workspace-bound Catalog. collections(service)
returns active (id, name) pairs from that same authorized snapshot. No migration,
source deletion, raw SQL or implicit global search is performed by this panel.
"""
from uuid import uuid4

import wx
from carr_library.catalog.read_views import browse_editions


class _ViewTooLarge(ValueError):
    """A bounded view failed, but its authorized collection choices remain valid."""
    def __init__(self, collections):
        super().__init__('Choose a smaller collection for this view.')
        self.collections = collections


class CatalogPanel(wx.Panel):
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
        self.filter = wx.Choice(self)
        self.books = wx.ListBox(self, style=wx.LB_SINGLE)
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

    def _snapshot(self, service, collection):
        if service.workspace != self.workspace:
            raise ValueError('workspace changed')
        pairs = tuple(self.collections(service))
        groups = dict(pairs)
        if len(groups) != len(pairs) or None in groups:
            raise ValueError('invalid collection identities')
        if collection is not None and collection not in groups:
            raise ValueError('collection no longer active')
        editions = []
        after = None
        while True:
            page = browse_editions(service, collection=collection, after=after, limit=500)
            editions.extend(page)
            if len(editions) > 10000:
                raise _ViewTooLarge(groups)
            if len(page) < 500:
                break
            after = page[-1].edition_id
        rows = {r.edition_id: r for r in editions}
        if len(rows) != len(editions):
            raise ValueError('duplicate edition identity')
        return groups, rows

    def _clear(self):
        self.ready = False
        self.groups, self.rows = {}, {}
        self.filter_ids, self.target_ids, self.book_ids = [], [], []
        for control in (self.filter, self.target, self.books):
            control.Clear()

    def _show_collections(self, groups, collection, target):
        self.groups = groups
        self.filter_ids = [None, *groups]
        self.target_ids = list(groups)
        self.filter.Append('All books in this library')
        for name in groups.values():
            self.filter.Append(name)
            self.target.Append(name)
        self.filter.SetSelection(self.filter_ids.index(collection))
        if target in self.target_ids:
            self.target.SetSelection(self.target_ids.index(target))
        elif self.target_ids:
            self.target.SetSelection(self.target_ids.index(collection) if collection in self.target_ids else 0)

    def refresh(self, event=None, *, selection=None):
        collection, target, edition = self._selection() if selection is None else selection
        try:
            with self.transaction() as service:
                groups, rows = self._snapshot(service, collection)
            self._clear()
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
                self.books.SetSelection(0)
            self.ready = True
            self.status.SetValue(f'{len(rows)} editions in this view.' if rows else
                ('This collection is empty. Add an existing book or import one.' if collection is not None else
                 'This library has no books. Import a file or add original text.'))
            return True
        except _ViewTooLarge as exc:
            # Do not show a partial list as the complete catalogue. Keep the
            # authorized selectors so the user can actually choose another view.
            self._clear()
            self._show_collections(exc.collections, collection, target)
            self.status.SetValue('This view has more than 10,000 editions. '
                                 'Use Browse collection to choose a smaller collection.')
            self.filter.SetFocus()
            return False
        except Exception:
            self._clear()
            self.status.SetValue('Catalog unavailable. Refresh after checking the active library.')
            return False

    def _change(self, action, selection, *, needs_target=False, needs_book=False):
        if not self.ready or selection != self._selection():
            self.status.SetValue('Selection changed. Refresh and try again.')
            self.status.SetFocus()
            return
        collection, target, edition = selection
        try:
            with self.transaction() as service:
                groups, rows = self._snapshot(service, collection)
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
            self.status.SetValue('Catalog updated.' + (' This collection is now empty.' if not self.rows and next_selection[0] is not None else ''))
            self.books.SetFocus()
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
