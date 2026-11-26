"""Keyboard-first management of the existing saved topic search definitions."""
from ..research.query_manager import QueryManager
from .research_actions import NativeDialogs


class QueryDialogs(NativeDialogs):
    def definition(self, parent, title, current=None):
        import wx
        dialog = wx.Dialog(parent, title=title, size=(660, 460))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            controls = {}
            for key, label, value, multiline in (
                ('name', 'Saved search name', current.name if current else '', False),
                ('query', 'Words to find in this topic', current.query if current else '', True),
            ):
                layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
                control = wx.TextCtrl(dialog, value=value,
                                      style=wx.TE_MULTILINE if multiline else 0)
                control.SetName(label)
                controls[key] = control
                layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Search within'), 0, wx.LEFT | wx.TOP, 8)
            field = wx.Choice(dialog, choices=['Saved quotations', 'Research notes'])
            field.SetName('Search within saved quotations or research notes')
            field.SetSelection(1 if current and current.field == 'note' else 0)
            layout.Add(field, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Maximum results, 1 to 100'), 0, wx.LEFT | wx.TOP, 8)
            limit = wx.SpinCtrl(dialog, min=1, max=100, initial=current.limit if current else 50)
            limit.SetName('Maximum results, 1 to 100')
            layout.Add(limit, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                       0, wx.ALIGN_RIGHT | wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            controls['name'].SetFocus()
            if dialog.ShowModal() != wx.ID_OK:
                return None
            index = field.GetSelection()
            if index not in (0, 1):
                raise ValueError('Choose saved quotations or research notes.')
            return dict(name=controls['name'].GetValue().strip(),
                        query=controls['query'].GetValue().strip(),
                        field=('quote', 'note')[index], limit=limit.GetValue())
        finally:
            dialog.Destroy()


class QueryActions:
    """Small modal steps finish before background searching starts."""

    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or QueryDialogs()

    def _scope(self):
        frame = self.frame
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            raise ValueError('Wait for the current operation or choose Cancel current operation.')
        if frame.active is None or frame.active[1] is None:
            raise ValueError('Open a research topic before managing its saved searches.')
        return frame.library, tuple(frame.active)

    def _same(self, scope):
        current = self._scope()
        if current[0] is not scope[0] or current[1] != scope[1]:
            raise ValueError('The library or topic changed. Open saved searches again.')

    @staticmethod
    def _label(row, position):
        field = 'research notes' if row.field == 'note' else 'saved quotations'
        return f'{position}. {row.name} — {field}; limit {row.limit}; ' + (
            'archived' if row.archived else 'active')

    def manage(self, event=None):
        try:
            scope = self._scope()
            manager = QueryManager(scope[0], *scope[1])
            cursors, after = [], None
            while True:
                self._same(scope)
                page = manager.list(include_archived=True, limit=50, after=after)
                choices = ['Create a new saved search']
                choices += [self._label(row, i + 1) for i, row in enumerate(page.rows)]
                next_index = previous_index = None
                if page.next_cursor is not None:
                    next_index = len(choices)
                    choices.append('Next page of saved searches')
                if cursors:
                    previous_index = len(choices)
                    choices.append('Previous page of saved searches')
                picked = self.dialogs.choose(self.frame, 'Manage saved topic searches',
                    f'{page.total} saved searches in this topic. Archived definitions are kept for restoration.', choices)
                if picked is None:
                    self.frame.report('Saved search manager closed.')
                    return
                self._same(scope)
                if type(picked) is not int or not 0 <= picked < len(choices):
                    raise ValueError('Select a saved search from the current page.')
                if picked == next_index:
                    cursors.append(after)
                    after = page.next_cursor
                    continue
                if picked == previous_index:
                    after = cursors.pop()
                    continue
                if picked == 0:
                    values = self.dialogs.definition(self.frame, 'Create saved topic search')
                    if values is None:
                        self.frame.report('New saved search cancelled.')
                        return
                    self._same(scope)
                    manager.create(**values)
                    self.frame.report('Saved search created. It will search the current evidence each time it runs.')
                    return
                expected = page.rows[picked - 1]
                self._act(scope, manager, expected)
                return
        except Exception as exc:
            self.frame.report(f'Saved search command not completed: {exc}')

    def _act(self, scope, manager, expected):
        actions = (['Restore this saved search', 'Duplicate as a new active search'] if expected.archived else
                   ['Run this saved search', 'Edit name and search criteria',
                    'Duplicate as a new search', 'Archive this saved search'])
        picked = self.dialogs.choose(self.frame, expected.name,
            ('Searches research notes.' if expected.field == 'note' else 'Searches saved quotations.') +
            f' Maximum {expected.limit} results.\nQuery: {expected.query}', actions)
        if picked is None:
            self.frame.report('Saved search action cancelled.')
            return
        self._same(scope)
        if type(picked) is not int or not 0 <= picked < len(actions):
            raise ValueError('Choose one of the saved search actions.')
        if not expected.archived and picked == 0:
            self._run(scope, manager, expected)
        elif not expected.archived and picked == 1:
            values = self.dialogs.definition(self.frame, 'Edit saved topic search', expected)
            if values is None:
                self.frame.report('Saved search edit cancelled.')
                return
            self._same(scope)
            manager.update(expected, **values)
            self.frame.report('Saved search updated. Its definition changed; source quotations and notes are preserved.')
        elif (expected.archived and picked == 1) or (not expected.archived and picked == 2):
            name = self.dialogs.text(self.frame, 'Duplicate saved search', 'Name for the new saved search',
                                     expected.name + ' (copy)')
            if name is None:
                self.frame.report('Saved search duplication cancelled.')
                return
            self._same(scope)
            manager.duplicate(expected, name.strip())
            self.frame.report('Saved search duplicated as a separate active definition.')
        elif expected.archived:
            manager.restore(expected)
            self.frame.report('Saved search restored. It can be run or edited again.')
        else:
            manager.archive(expected)
            self.frame.report('Saved search archived. Use Manage saved topic searches to restore it.')

    def _run(self, scope, manager, expected):
        frame = self.frame

        def complete(rows):
            if (frame.library is not scope[0] or frame.active != scope[1]
                    or getattr(frame, '_closing', False)):
                return
            if frame.cancel_event.is_set():
                frame.report('Saved search cancelled. No results were displayed.')
                return
            if manager.get(expected.id) != expected:
                frame.report('The saved search changed. Run its current definition again.')
                return
            from .desktop_app import format_topic_results
            self.dialogs.show(frame, expected.name, format_topic_results(rows))
            frame.status.SetFocus()

        frame.run_job('Running saved topic search',
            lambda: manager.run(expected, cancelled=frame.cancel_event.is_set), complete)


def install_query_manager_actions(frame):
    import wx
    if getattr(frame, 'query_manager_actions', None) is not None:
        return frame.query_manager_actions
    actions = QueryActions(frame)
    for index in range(frame.menu_bar.GetMenuCount()):
        label = frame.menu_bar.GetMenuLabel(index).replace('&', '')
        if label == 'Research':
            menu = frame.menu_bar.GetMenu(index)
            item = menu.Append(wx.ID_ANY, 'Manage saved topic searches…')
            frame.Bind(wx.EVT_MENU, actions.manage, item)
            frame.query_manager_actions = actions
            return actions
    raise ValueError('Research menu is required for the saved search manager.')
