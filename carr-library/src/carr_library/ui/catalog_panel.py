"""Native catalog UI; reuse Catalog, with caller-authorized collection enumeration.

The transaction factory yields a workspace-bound Catalog. collections(service)
returns active (id, name) pairs from that same authorized snapshot. No migration,
source deletion, raw SQL or implicit global search is performed by this panel.
"""
from uuid import uuid4

import wx


class CatalogPanel(wx.Panel):
    def __init__(self, parent, workspace, transaction, collections, *, select_collection=None):
        super().__init__(parent)
        self.workspace = workspace
        self.transaction = transaction
        self.collections = collections
        if select_collection is not None and not callable(select_collection):
            raise TypeError('collection selection service must be callable')
        self.select_collection = select_collection
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
        if select_collection is not None:
            button = wx.Button(self, label='Search this collection')
            button.Bind(wx.EVT_BUTTON, self.search_collection)
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
        editions = service.editions(collection)
        rows = {r[0]: r for r in editions}
        if len(rows) != len(editions):
            raise ValueError('duplicate edition identity')
        return groups, rows

    def _clear(self):
        self.ready = False
        self.groups, self.rows = {}, {}
        self.filter_ids, self.target_ids, self.book_ids = [], [], []
        for control in (self.filter, self.target, self.books):
            control.Clear()

    def refresh(self, event=None, *, selection=None):
        collection, target, edition = self._selection() if selection is None else selection
        try:
            with self.transaction() as service:
                groups, rows = self._snapshot(service, collection)
            self._clear()
            self.groups, self.rows = groups, rows
            self.filter_ids = [None, *groups]
            self.target_ids = list(groups)
            self.book_ids = list(rows)
            self.filter.Append('All books in this library')
            for key, name in groups.items():
                label = f'{name} (ID: {key})'
                self.filter.Append(label)
                self.target.Append(label)
            for key, row in rows.items():
                _, _, _, title, language, version, volume = row
                self.books.Append(f'{title or "Untitled"}; language {language or "unknown"}; '
                                  f'edition {version or "unknown"}; volume {volume or "unknown"}; ID {key}')
            self.filter.SetSelection(self.filter_ids.index(collection))
            if target in self.target_ids:
                self.target.SetSelection(self.target_ids.index(target))
            if edition in self.book_ids:
                self.books.SetSelection(self.book_ids.index(edition))
            self.ready = True
            self.status.SetValue(f'{len(rows)} editions in this view.' if rows else
                                 'No books in this view. Import books or choose another collection.')
            return True
        except Exception:
            self._clear()
            self.status.SetValue('Catalog unavailable. Refresh after checking the active library.')
            return False

    def _change(self, action, selection, *, needs_target=False, needs_book=False):
        if not self.ready or selection != self._selection():
            self.status.SetValue('Selection changed. Refresh and try again.')
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
            return
        if self.refresh(selection=next_selection):
            self.status.SetValue('Catalog updated.')
            self.books.SetFocus()
        else:
            self.status.SetValue('Catalog change committed; refresh failed. Refresh before another change.')

    def create(self, event=None):
        def action(service, selected):
            key = uuid4().hex
            service.create_collection(key, self.name.GetValue())
            return (selected[0], key, selected[2])
        self._change(action, self._selection())

    def search_collection(self, event=None):
        selection = self._selection()
        try:
            if not self.ready or not callable(self.select_collection):
                raise ValueError('catalog selection unavailable')
            with self.transaction() as service:
                self._snapshot(service, selection[0])
            if selection != self._selection():
                raise ValueError('selection changed')
            # The bound search service rechecks this identity in its own snapshot.
            self.select_collection(self.workspace, selection[0])
        except Exception:
            self.status.SetValue('Search scope unavailable. Refresh the catalog and retry.')
            self.status.SetFocus()

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
            return
        if wx.MessageBox('Archive the selected collection? Books will not be deleted.',
                         'Confirm collection archive', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                         self) != wx.YES:
            return
        def action(service, selection):
            service.archive(selection[1])
            return (None if selection[0] == selection[1] else selection[0], None, selection[2])
        self._change(action, selected, needs_target=True)
