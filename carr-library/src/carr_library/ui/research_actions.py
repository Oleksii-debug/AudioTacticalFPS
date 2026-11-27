"""Native menu actions for existing research services.

These actions keep the current panels and their unsaved notes alive. Every
operation uses the application's scoped services; dialogs do not write SQL,
change source text, or grant quotation approval.
"""
from dataclasses import dataclass

from ..citations.anchors import format_citation
from .scope_bindings import scope_labels


class NativeDialogs:
    """Small keyboard-accessible dialogs, imported lazily for headless tests."""

    def approve_export(self, parent, path, *, private_root, wx_module=None):
        """Capture the real final path before the one replacement question.

        Save pickers using this method must omit FD_OVERWRITE_PROMPT. Its earlier
        confirmation cannot be bound to a file appearing or changing on return.
        The same captured state is passed through queued work to the publisher.
        """
        from .export_destination import confirm_export_destination
        return confirm_export_destination(parent, path, private_root=private_root,
                                          wx_module=wx_module)

    def fields(self, parent, title, fields, explanation=''):
        import wx

        dialog = wx.Dialog(parent, title=title, size=(650, 560))
        controls = {}
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            if explanation:
                explanation_control = wx.StaticText(dialog, label=explanation)
                explanation_control.Wrap(610)
                layout.Add(explanation_control, 0, wx.EXPAND | wx.ALL, 8)
            for key, label, value, multiline in fields:
                layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
                control = wx.TextCtrl(dialog, value=value,
                                      style=wx.TE_MULTILINE if multiline else 0)
                control.SetName(label)
                if multiline:
                    control.SetMinSize((-1, 70))
                controls[key] = control
                layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                       0, wx.ALIGN_RIGHT | wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            first = next(iter(controls.values()))
            first.SetFocus()
            first.SetInsertionPoint(0)
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return {key: control.GetValue() for key, control in controls.items()}
        finally:
            dialog.Destroy()

    def text(self, parent, title, label, value='', *, multiline=False):
        result = self.fields(parent, title, (('value', label, value, multiline),))
        return None if result is None else result['value']

    def choose(self, parent, title, label, choices):
        import wx

        dialog = wx.SingleChoiceDialog(parent, label, title, list(choices))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            index = dialog.GetSelection()
            if not 0 <= index < len(choices):
                raise ValueError('Choose one item from the list.')
            return index
        finally:
            dialog.Destroy()

    def show(self, parent, title, value):
        import wx

        dialog = wx.Dialog(parent, title=title, size=(800, 600))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            reader = wx.TextCtrl(dialog, value=value, style=wx.TE_READONLY | wx.TE_MULTILINE)
            reader.SetName(title)
            layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.Button(dialog, wx.ID_CANCEL, '&Close'), 0, wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            reader.SetInsertionPoint(0)
            reader.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()


def _tag_lines(value):
    """One explicit tag per line; empty input intentionally clears all tags."""
    if not isinstance(value, str):
        raise ValueError('Enter one tag per line.')
    return tuple(dict.fromkeys(line.strip() for line in value.splitlines() if line.strip()))


def _evidence_text(rows):
    sections = []
    for number, row in enumerate(rows, 1):
        anchors = row.anchors or (row.anchor,)
        parts = [f'Result {number}.']
        for anchor in anchors:
            parts.extend(('Source: ' + format_citation(anchor), 'Exact quotation:', anchor.quote))
        parts.extend(('Saved research note:', row.note or '(No saved note)'))
        sections.append('\n\n'.join(parts))
    return '\n\n'.join(sections) if sections else 'No saved evidence has this tag in the selected topic.'


@dataclass(frozen=True)
class _Scope:
    library: object
    workspace: str
    topic: str | None

    @property
    def ids(self):
        return self.workspace, self.topic


class ResearchActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs()

    def _scope(self, *, topic=False):
        frame = self.frame
        if frame.working or getattr(frame, 'busy', False):
            raise ValueError('An operation is already running. Wait or choose Cancel current operation.')
        if getattr(frame, '_closing', False):
            raise ValueError('The library is closing.')
        scope = frame.active
        if scope is None:
            raise ValueError('Open a library first.')
        if topic and scope[1] is None:
            raise ValueError('Open a research topic first.')
        return _Scope(frame.library, *scope)

    def _same_scope(self, scope):
        current = self._scope()
        if current.library is not scope.library or current.ids != scope.ids:
            raise ValueError('The selected library or topic changed. Open the command again.')

    def _focus(self, control, scope):
        # A modal dialog may dispatch a scope change. Never focus a destroyed
        # control from the old scope or move focus away from an active job.
        if (self.frame.library is not scope.library or self.frame.active != scope.ids
                or self.frame.working):
            return
        try:
            control.SetFocus()
        except (AttributeError, RuntimeError):
            self.frame.status.SetFocus()

    def _cancelled(self, message, control, scope):
        self.frame.report(message)
        self._focus(control, scope)

    def _catalog(self, scope):
        self._same_scope(scope)
        if self.frame.book is None:
            raise ValueError('Open Books and Collections first.')
        panel = self.frame.book.GetPage(1)
        if panel.workspace != scope.workspace or not panel.ready:
            raise ValueError('Refresh Books and Collections before editing a book.')
        return panel

    @staticmethod
    def _edition(scope, selection):
        if selection[2] is None:
            raise ValueError('Select an imported book in Books and Collections first.')
        return scope.library.edition_metadata(scope.workspace, selection[2])

    def edit_book_metadata(self, event=None):
        saved = False
        try:
            scope = self._scope()
            panel = self._catalog(scope)
            selection = panel._selection()
            row = self._edition(scope, selection)
            if panel.rows.get(row.edition_id) != row:
                raise ValueError('Book metadata changed. Refresh Books and Collections first.')
            fields = (
                ('title', 'Book title', row.title or '', False),
                ('authors', 'Authors — one author per line', '\n'.join(row.authors), True),
                ('language', 'Language code (optional)', row.language or '', False),
                ('edition', 'Edition (optional)', row.edition or '', False),
                ('volume', 'Volume (optional)', row.volume or '', False),
            )
            values = self.dialogs.fields(self.frame, 'Edit selected book metadata', fields,
                'Title and authors describe the work, including its other editions. '
                'Previously saved quotations keep their original citations. The original file is preserved.')
            if values is None:
                self._cancelled('Book metadata edit cancelled.', panel.books, scope)
                return
            if (self._catalog(scope) is not panel or panel._selection() != selection
                    or self._edition(scope, selection) != row):
                raise ValueError('The selected book or its metadata changed. Review the current book and try again.')
            scope.library.update_metadata(scope.workspace, row.edition_id,
                title=values['title'].strip(),
                authors=tuple(line.strip() for line in values['authors'].splitlines() if line.strip()),
                language=values['language'].strip() or None,
                edition=values['edition'].strip() or None,
                volume=values['volume'].strip() or None, expected_metadata=row)
            saved = True
            self._same_scope(scope)
            if not panel.refresh(selection=selection):
                self.frame.report('Book metadata saved. Refresh Books and Collections before another edit.')
                return
            search = self.frame.book.GetPage(0)
            search.refresh_collections()
            self.frame.report('Book metadata saved. Search again to use the updated citation. Saved quotations are unchanged.')
            self._focus(panel.books, scope)
        except Exception as exc:
            prefix = ('Book metadata saved, but the view could not be refreshed' if saved else
                      'Book metadata edit not completed')
            self.frame.report(f'{prefix}: {exc}')

    def edit_library_description(self, event=None):
        self._edit_library(include_name=False)

    def rename_or_describe_library(self, event=None):
        self._edit_library(include_name=True)

    def _edit_library(self, *, include_name):
        saved = False
        try:
            scope = self._scope()
            current = next((row for row in scope.library.workspaces() if row.id == scope.workspace), None)
            if current is None:
                raise ValueError('The library is no longer active.')
            if include_name:
                values = self.dialogs.fields(self.frame, 'Rename or describe library', (
                    ('name', 'Library name', current.name, False),
                    ('description', 'Library description (optional)', current.description, True),
                ))
            else:
                value = self.dialogs.text(self.frame, 'Edit library description',
                    'Library description', current.description, multiline=True)
                values = None if value is None else {'name': current.name, 'description': value}
            if values is None:
                self._cancelled('Library edit cancelled.', self.frame.choice, scope)
                return
            self._same_scope(scope)
            fresh = next((row for row in scope.library.workspaces() if row.id == scope.workspace), None)
            if fresh != current:
                raise ValueError('The library changed while the dialog was open. Review it and try again.')
            scope.library.rename_workspace(scope.workspace, values['name'], values['description'])
            saved = True
            self._same_scope(scope)
            if values['name'].strip() != current.name:
                # Reuse the shell's authorized list validation. Update choices
                # without dropping the active notebook and its unsaved note.
                rows, ids = self.frame._load_scopes()
                self._same_scope(scope)
                if scope.ids not in ids:
                    raise ValueError('The active topic changed. Open the library again after saving your note.')
                self.frame.choice.SetItems(scope_labels(rows))
                self.frame.scopes = ids
                self.frame.choice.SetSelection(ids.index(scope.ids))
            self.frame.report('Library name and description saved.' if include_name else 'Library description saved.')
            self._focus(self.frame.choice, scope)
        except Exception as exc:
            prefix = 'Library details saved; refresh its list when ready' if saved else 'Library edit not completed'
            self.frame.report(f'{prefix}: {exc}')

    def resume_import(self, event=None):
        try:
            scope = self._scope()
            rows = tuple(row for row in scope.library.import_requests(scope.workspace)
                         if row['state'] not in ('complete', 'superseded'))
            if not rows:
                self.frame.report('There are no interrupted imports in this library.')
                return
            labels = [f'{number}. {row["file"]} — {row["state"]}'
                      for number, row in enumerate(rows, 1)]
            index = self.dialogs.choose(self.frame, 'Resume interrupted import',
                'Choose an interrupted or failed import to retry. Use Cancel current operation to stop it.', labels)
            if index is None:
                self._cancelled('Import resume cancelled.', self.frame.choice, scope)
                return
            if type(index) is not int or not 0 <= index < len(rows):
                raise ValueError('Choose one interrupted import.')
            self._same_scope(scope)
            selected = rows[index]
            fresh = next((row for row in scope.library.import_requests(scope.workspace)
                          if row['request_id'] == selected['request_id']), None)
            if fresh is None or fresh['state'] in ('complete', 'superseded'):
                raise ValueError('This import is no longer pending. Reopen the import list.')

            def done(result):
                if self.frame.library is not scope.library or self.frame.active != scope.ids:
                    return
                state = result.get('state', 'failed')
                if state == 'complete' and self.frame.book is not None:
                    self.frame.book.GetPage(1).refresh()
                    self.frame.book.GetPage(0).refresh_collections()
                message = f'Import of {selected["file"]}: {state}.'
                detail = result.get('message') or result.get('error')
                if detail:
                    message += ' ' + str(detail)
                if state == 'quarantined':
                    message += ' Choose Library, Review pending imported page to check this page.'
                self.frame.report(message)

            def resume():
                if self.frame.library is not scope.library or self.frame.active != scope.ids:
                    raise ValueError('The selected library changed before the import could resume.')
                return scope.library.resume_import(scope.workspace, selected['request_id'],
                    cancelled=self.frame.cancel_event.is_set)

            self.frame.run_job('Resuming import', resume, done)
        except Exception as exc:
            self.frame.report(f'Import resume not completed: {exc}')

    def _selected_evidence(self, scope):
        self._same_scope(scope)
        panel = self.frame.evidence
        if panel is None or (panel.workspace, panel.topic) != scope.ids:
            raise ValueError('Open Evidence and Notes in the selected topic first.')
        row = panel._selected()
        if row is None or panel.displayed is None or row.evidence_id != panel.displayed.evidence_id:
            raise ValueError('Select one saved quotation in Evidence and Notes.')
        fresh = next((item for item in scope.library.evidence(*scope.ids)
                      if item.evidence_id == row.evidence_id), None)
        if fresh is None or (fresh.anchor, fresh.anchors) != (row.anchor, row.anchors):
            raise ValueError('The selected saved quotation changed. Review the evidence and try again.')
        return panel, row

    @staticmethod
    def _evidence_focus(panel):
        return panel.note if panel._dirty() else panel.items

    def edit_evidence_tags(self, event=None):
        try:
            scope = self._scope(topic=True)
            panel, row = self._selected_evidence(scope)
            tags = scope.library.evidence_tags(*scope.ids, row.evidence_id)
            value = self.dialogs.text(self.frame, 'Edit tags for selected evidence',
                'Tags — one per line; leave empty to remove all tags', '\n'.join(tags), multiline=True)
            if value is None:
                self._cancelled('Evidence tag edit cancelled.', self._evidence_focus(panel), scope)
                return
            current_panel, current = self._selected_evidence(scope)
            if (current_panel is not panel or current.evidence_id != row.evidence_id
                    or scope.library.evidence_tags(*scope.ids, row.evidence_id) != tags):
                raise ValueError('The selected quotation or its tags changed. Review it and try again.')
            names = _tag_lines(value)
            scope.library.set_evidence_tags(*scope.ids, row.evidence_id, names)
            # Do not refresh the evidence panel: its note may be an unsaved draft.
            self.frame.report(f'{len(names)} tags saved for the selected quotation.')
            self._focus(self._evidence_focus(panel), scope)
        except Exception as exc:
            self.frame.report(f'Evidence tag edit not completed: {exc}')

    def view_evidence_tags(self, event=None):
        try:
            scope = self._scope(topic=True)
            panel, row = self._selected_evidence(scope)
            tags = scope.library.evidence_tags(*scope.ids, row.evidence_id)
            value = '\n'.join(f'{number}. {name}' for number, name in enumerate(tags, 1))
            self.dialogs.show(self.frame, 'Tags for selected evidence', value or 'This quotation has no tags.')
            self._focus(self._evidence_focus(panel), scope)
        except Exception as exc:
            self.frame.report(f'Evidence tags unavailable: {exc}')

    def find_evidence_by_tag(self, event=None):
        try:
            scope = self._scope(topic=True)
            panel = self.frame.evidence
            value = self.dialogs.text(self.frame, 'Find evidence by tag', 'Tag name (exact spelling)')
            if value is None:
                target = self._evidence_focus(panel) if panel is not None else self.frame.choice
                self._cancelled('Evidence tag search cancelled.', target, scope)
                return
            self._same_scope(scope)
            rows = scope.library.evidence_with_tag(*scope.ids, value.strip())
            self.dialogs.show(self.frame, 'Evidence tagged ' + value.strip(), _evidence_text(rows))
            if panel is not None:
                self._focus(self._evidence_focus(panel), scope)
        except Exception as exc:
            self.frame.report(f'Evidence tag search not completed: {exc}')


def install_research_actions(frame):
    """Bind existing menu items where present, adding only missing workflows."""
    import wx

    if getattr(frame, 'research_actions', None) is not None:
        return frame.research_actions
    actions = ResearchActions(frame)
    groups = (
        ('Library', (
            ('Resume interrupted import...\tCtrl+Shift+I', actions.resume_import,
             ('Resume interrupted import',)),
            ('Edit selected book metadata...\tCtrl+Shift+M', actions.edit_book_metadata,
             ('Edit selected book metadata',)),
            ('Rename or describe library...', actions.rename_or_describe_library,
             ('Rename or describe library', 'Rename library', 'Edit library description')),
        )),
        ('Research', (
            ('Edit tags for selected evidence...\tCtrl+Shift+T', actions.edit_evidence_tags,
             ('Edit selected evidence tags', 'Edit tags for selected evidence')),
            ('View tags for selected evidence...', actions.view_evidence_tags,
             ('View tags for selected evidence',)),
            ('Find evidence by tag...\tCtrl+Alt+T', actions.find_evidence_by_tag,
             ('Find evidence by tag',)),
        )),
    )

    def caption(value):
        return value.split('\t', 1)[0].replace('&', '').rstrip('.').strip().casefold()

    menus = []
    for title, entries in groups:
        index = frame.menu_bar.FindMenu(title)
        if index < 0:
            raise ValueError(f'{title} menu is required for research commands.')
        menus.append((frame.menu_bar.GetMenu(index), entries))
    for menu, entries in menus:
        separated = False
        for label, callback, aliases in entries:
            known = {caption(alias) for alias in aliases}
            matches = [item for item in menu.GetMenuItems()
                       if not item.IsSeparator() and caption(item.GetItemLabel()) in known]
            if matches:
                item, *duplicates = matches
                frame.Unbind(wx.EVT_MENU, id=item.GetId())
                item.SetItemLabel(label)
                for duplicate in duplicates:
                    frame.Unbind(wx.EVT_MENU, id=duplicate.GetId())
                    menu.Delete(duplicate.GetId())
            else:
                if not separated:
                    menu.AppendSeparator()
                    separated = True
                item = menu.Append(wx.ID_ANY, label)
            frame.Bind(wx.EVT_MENU, callback, item)
    frame.research_actions = actions
    return actions
