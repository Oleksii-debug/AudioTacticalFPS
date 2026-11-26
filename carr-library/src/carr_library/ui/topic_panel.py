"""Native Topics UI candidate. Caller supplies authorized TopicTree transactions."""
from uuid import uuid4

import wx


class TopicPanel(wx.Panel):
    def __init__(self, parent, workspace, transaction):
        super().__init__(parent)
        self.workspace = workspace
        self.transaction = transaction
        self.rows = {}
        self.items = {}
        self.parents = []
        self.tree = wx.TreeCtrl(self, style=wx.TR_DEFAULT_STYLE | wx.TR_SINGLE)
        self.tree.SetName('Research topics and sections')
        self.name = wx.TextCtrl(self)
        self.name.SetName('Topic name')
        self.destination = wx.Choice(self)
        self.destination.SetName('Move to parent topic')
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY)
        self.status.SetName('Topic operation status')
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.tree, 1, wx.EXPAND)
        layout.Add(wx.StaticText(self, label='Topic name'))
        layout.Add(self.name, 0, wx.EXPAND)
        layout.Add(wx.StaticText(self, label='Move to parent topic'))
        layout.Add(self.destination, 0, wx.EXPAND)
        for label, handler in [('Create root topic', self.create_root),
                               ('Create child topic', self.create_child),
                               ('Rename selected topic', self.rename),
                               ('Move selected topic', self.move),
                               ('Archive selected topic', self.archive),
                               ('Refresh topics', self.refresh)]:
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            layout.Add(button)
        layout.Add(self.status, 0, wx.EXPAND)
        self.SetSizer(layout)
        self.refresh()

    def _check(self, service):
        if service.workspace != self.workspace:
            raise ValueError('workspace changed')

    def _selected(self):
        item = self.tree.GetSelection()
        return self.tree.GetItemData(item) if item.IsOk() else None

    def _clear(self):
        self.rows.clear()
        self.items.clear()
        self.parents.clear()
        self.tree.DeleteAllItems()
        self.destination.Clear()

    def _populate(self, rows, selected):
        self._clear()
        self.rows = {row.id: row for row in rows}
        if len(self.rows) != len(rows):
            raise ValueError('duplicate topic identity')
        children = {}
        for row in rows:
            children.setdefault(row.parent, []).append(row)
        root = self.tree.AddRoot('Topics and sections')
        self.parents = [None]
        self.destination.Append('Root (no parent)')
        pending = [(root, row, 0) for row in reversed(children.get(None, []))]
        while pending:
            parent, row, depth = pending.pop()
            item = self.tree.AppendItem(parent, row.name, data=row.id)
            self.items[row.id] = item
            self.parents.append(row.id)
            self.destination.Append('  ' * min(depth, 20) + row.name)
            pending.extend((item, child, depth + 1)
                           for child in reversed(children.get(row.id, [])))
        if len(self.items) != len(rows):
            raise ValueError('unreachable topic')
        self.destination.SetSelection(0)
        target = self.items.get(selected, root)
        self.tree.SelectItem(target)
        self.tree.EnsureVisible(target)
        self.tree.Expand(root)

    def refresh(self, event=None, *, selected=None):
        try:
            if selected is None:
                selected = self._selected()
            with self.transaction() as service:
                self._check(service)
                rows = service.list_topics()
            self._populate(rows, selected)
            self.status.SetValue(f'{len(rows)} active topics.')
        except Exception:
            self._clear()
            self.status.SetValue('Topics unavailable. Refresh after checking the active library.')

    def _change(self, operation, *, require_selection=True, destination=None):
        selected = self._selected()
        try:
            with self.transaction() as service:
                self._check(service)
                live = {row.id: row for row in service.list_topics()}
                if require_selection and (selected not in live or live[selected] != self.rows.get(selected)):
                    raise ValueError('stale topic selection')
                if destination is not None and (destination not in live or live[destination] != self.rows.get(destination)):
                    raise ValueError('stale destination')
                target = operation(service, selected, live)
        except Exception:
            self.refresh()
            self.status.SetValue('Topic change rejected. Review the selection and try again.')
            return
        self.refresh(selected=target)
        self.tree.SetFocus()

    @staticmethod
    def _next_position(rows, parent):
        return max((row.position for row in rows.values() if row.parent == parent), default=-1) + 1

    def _create(self, child):
        def operation(service, selected, rows):
            parent = selected if child else None
            topic = uuid4().hex
            service.create(topic, self.name.GetValue(), parent=parent,
                           position=self._next_position(rows, parent))
            return topic
        self._change(operation, require_selection=child)

    def create_root(self, event=None):
        self._create(False)

    def create_child(self, event=None):
        self._create(True)

    def rename(self, event=None):
        def operation(service, selected, rows):
            service.rename(selected, self.name.GetValue())
            return selected
        self._change(operation)

    def move(self, event=None):
        index = self.destination.GetSelection()
        if not 0 <= index < len(self.parents):
            self.status.SetValue('Choose a destination topic first.')
            return
        parent = self.parents[index]
        def operation(service, selected, rows):
            service.move(selected, parent, position=self._next_position(rows, parent))
            return selected
        self._change(operation, destination=parent)

    def archive(self, event=None):
        selected = self._selected()
        if selected not in self.rows:
            self.status.SetValue('Select a topic first.')
            return
        if wx.MessageBox('Archive the selected topic and hide its sections?',
                         'Confirm archive', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                         self) != wx.YES:
            return
        if selected != self._selected():
            self.status.SetValue('Selection changed. Archive cancelled.')
            return
        def operation(service, selected, rows):
            service.archive(selected)
            return None
        self._change(operation)
