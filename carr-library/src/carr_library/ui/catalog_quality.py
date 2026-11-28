"""Native, keyboard-readable catalog review using the existing desktop job runner."""
import wx

from carr_library.catalog.quality import ISSUE_LABELS, catalog_quality


MAX_RELATED_EDITION_DETAILS = 200


def issue_rows(snapshot, category=None):
    """Return each affected edition once, retaining every applicable reason."""
    if category is not None and category not in dict(ISSUE_LABELS):
        raise ValueError('Unknown catalog issue filter.')
    selected = {}
    for issue in snapshot.issues:
        if category is None or issue.category == category:
            for identity in issue.edition_ids:
                selected.setdefault(identity, []).append(issue)
    return tuple((row, tuple(selected[row.edition_id])) for row in snapshot.editions
                 if row.edition_id in selected)


def edition_label(row):
    return (f'{row.title or "Untitled"}; author {"; ".join(row.authors) or "unknown"}; '
            f'language {row.language or "unknown"}; edition {row.edition or "unknown"}; '
            f'volume {row.volume or "unknown"}; edition ID {row.edition_id}')


def issue_details(snapshot, row, issues):
    by_id = {edition.edition_id: edition for edition in snapshot.editions}
    parts = [edition_label(row), f'Work ID: {row.work_id}',
             f'Registered source asset SHA-256: {row.asset}']
    for issue in issues:
        parts.extend(('', dict(ISSUE_LABELS)[issue.category] + ':', issue.reason))
        if len(issue.edition_ids) > 1:
            parts.append('Related edition records in this checked scope:')
            parts.extend(edition_label(by_id[identity])
                         for identity in issue.edition_ids[:MAX_RELATED_EDITION_DETAILS])
            if len(issue.edition_ids) > MAX_RELATED_EDITION_DETAILS:
                parts.append(f'Showing the first {MAX_RELATED_EDITION_DETAILS} of '
                    f'{len(issue.edition_ids)} related editions here. '
                    'Use the issue filter and Next editions to inspect every record.')
    return '\n'.join(parts)


class CatalogQualityDialog(wx.Dialog):
    PAGE_SIZE = 200

    def __init__(self, parent, snapshot, *, can_edit=False):
        super().__init__(parent, title='Catalog quality', size=(880, 760))
        self.snapshot = snapshot
        self.can_edit = can_edit
        self.requested_collection = snapshot.collection
        self.edit_edition_id = None
        self.page = 0
        self.rows = ()
        self.visible = ()
        layout = wx.BoxSizer(wx.VERTICAL)
        self.summary = wx.TextCtrl(self, value=self._summary_text(),
                                   style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.summary.SetName('Catalog quality summary and limitations')
        self.summary.SetMinSize((-1, 120))
        layout.Add(self.summary, 0, wx.EXPAND | wx.ALL, 8)

        self.collection_ids = [None, *(identity for identity, _ in snapshot.collections)]
        self.collection = wx.Choice(self, choices=['All books in this library',
            *(f'Collection: {name}; ID {identity}' for identity, name in snapshot.collections)])
        self.collection.SetName('Collection to check')
        self.collection.SetSelection(self.collection_ids.index(snapshot.collection))
        layout.Add(wx.StaticText(self, label='Collection to check'), 0, wx.LEFT | wx.RIGHT, 8)
        layout.Add(self.collection, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.category_ids = [None, *(kind for kind, _ in ISSUE_LABELS)]
        self.filter = wx.Choice(self, choices=['All issues', *(label for _, label in ISSUE_LABELS)])
        self.filter.SetName('Catalog issue filter')
        self.filter.SetSelection(0)
        self.filter.Bind(wx.EVT_CHOICE, self.on_filter)
        layout.Add(wx.StaticText(self, label='Catalog issue filter'), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        layout.Add(self.filter, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.results = wx.ListBox(self, style=wx.LB_SINGLE)
        self.results.SetName('Editions requiring catalog review')
        self.results.Bind(wx.EVT_LISTBOX, self.on_selection)
        layout.Add(self.results, 1, wx.EXPAND | wx.ALL, 8)
        pages = wx.BoxSizer(wx.HORIZONTAL)
        self.previous = wx.Button(self, label='&Previous editions')
        self.next = wx.Button(self, label='&Next editions')
        self.previous.SetName('Previous catalog quality page')
        self.next.SetName('Next catalog quality page')
        self.previous.Bind(wx.EVT_BUTTON, self.on_previous)
        self.next.Bind(wx.EVT_BUTTON, self.on_next)
        pages.Add(self.previous, 0, wx.RIGHT, 8)
        pages.Add(self.next)
        layout.Add(pages, 0, wx.LEFT | wx.RIGHT, 8)

        self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.details.SetName('Selected edition issues and related edition identities')
        layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Catalog quality status')
        self.status.SetMinSize((-1, 56))
        layout.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        refresh = wx.Button(self, label='&Check selected collection')
        refresh.SetName('Check selected collection again')
        refresh.Bind(wx.EVT_BUTTON, self.on_refresh)
        self.edit = wx.Button(self, label='&Edit selected edition metadata')
        self.edit.SetName('Edit metadata for the selected edition')
        self.edit.Bind(wx.EVT_BUTTON, self.on_edit)
        close = wx.Button(self, wx.ID_CANCEL, '&Close')
        for control in (refresh, self.edit, close):
            buttons.Add(control, 0, wx.ALL, 8)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.SetMinSize((700, 620))
        self.on_filter()
        self.summary.SetInsertionPoint(0)
        self.summary.SetFocus()

    def _summary_text(self):
        summary = self.snapshot.summary
        scope = ('All books in this library' if self.snapshot.collection is None else
                 'Collection: ' + dict(self.snapshot.collections)[self.snapshot.collection])
        counts = []
        for category, label in ISSUE_LABELS:
            affected = {identity for issue in self.snapshot.issues if issue.category == category
                        for identity in issue.edition_ids}
            counts.append(f'{label}: {len(affected)} editions')
        return (f'{scope}. {summary.sources} source editions; {summary.works} works; '
                f'{summary.unique_assets} registered source assets; {summary.authors} distinct author names; '
                f'{summary.languages} known languages; {summary.collections} active collections.\n'
                f'{summary.affected_sources} editions need review. ' + '; '.join(counts) + '.\n'
                'This is a read-only catalog snapshot. Possible duplicates are tentative; editions '
                'remain separate. No sources are merged or deleted. Author names are not verified people.')

    def on_filter(self, event=None):
        index = self.filter.GetSelection()
        if not 0 <= index < len(self.category_ids):
            return
        self.rows = issue_rows(self.snapshot, self.category_ids[index])
        self.page = 0
        self._show_page()

    def _show_page(self):
        start = self.page * self.PAGE_SIZE
        self.visible = self.rows[start:start + self.PAGE_SIZE]
        labels = dict(ISSUE_LABELS)
        self.results.SetItems([edition_label(row) + '; issues: ' +
                              ', '.join(labels[issue.category] for issue in issues)
                              for row, issues in self.visible])
        self.previous.Enable(self.page > 0)
        self.next.Enable(start + len(self.visible) < len(self.rows))
        self.status.SetValue((f'Page {self.page + 1}. Editions {start + 1} to '
                              f'{start + len(self.visible)} of {len(self.rows)} in this issue filter.')
                             if self.visible else 'No editions match this issue filter.')
        if self.visible:
            self.results.SetSelection(0)
        self.on_selection()

    def on_selection(self, event=None):
        index = self.results.GetSelection()
        selected = self.visible[index] if 0 <= index < len(self.visible) else None
        self.edit.Enable(self.can_edit and selected is not None)
        self.details.SetValue(issue_details(self.snapshot, *selected) if selected else '')
        self.details.SetInsertionPoint(0)

    def on_previous(self, event=None):
        if self.page:
            self.page -= 1
            self._show_page()
            self.results.SetFocus()

    def on_next(self, event=None):
        if (self.page + 1) * self.PAGE_SIZE < len(self.rows):
            self.page += 1
            self._show_page()
            self.results.SetFocus()

    def on_refresh(self, event=None):
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            self.status.SetValue('Choose a collection to check.')
            self.collection.SetFocus()
            return
        self.requested_collection = self.collection_ids[index]
        self.EndModal(wx.ID_APPLY)

    def on_edit(self, event=None):
        index = self.results.GetSelection()
        if self.can_edit and 0 <= index < len(self.visible):
            self.edit_edition_id = self.visible[index][0].edition_id
            self.EndModal(wx.ID_OK)


def open_catalog_quality(frame, *, collection=None, edit_edition=None):
    """Menu contract: asynchronous check; optional callback(workspace, edition_id).

The existing desktop cancel button cancels each read. A scope/data-folder change,
closed frame or late cancellation suppresses stale completion and edit actions.
Rescanning closes the modal dialog before starting the next cancellable job.
"""
    if getattr(frame, '_closing', False):
        return
    if frame.working or frame.busy:
        frame.report('Wait for the current operation before checking catalog quality.')
        return
    try:
        workspace = frame._workspace()
        scope, library = frame.active, frame.library
    except Exception as exc:
        frame.report(str(exc))
        return
    editor = edit_edition or getattr(frame, 'edit_catalog_edition', None)

    def current(*, completed=True):
        return (not getattr(frame, '_closing', False) and frame.library is library and
                frame.active == scope and (not completed or not frame.cancel_event.is_set()))

    def check(selected_collection):
        if not current(completed=False):
            return

        def read():
            return catalog_quality(library, workspace, collection=selected_collection,
                                   cancelled=frame.cancel_event.is_set)

        def done(snapshot):
            if not current():
                return
            dialog = CatalogQualityDialog(frame, snapshot, can_edit=callable(editor))
            try:
                result = dialog.ShowModal()
                requested, identity = dialog.requested_collection, dialog.edit_edition_id
            finally:
                dialog.Destroy()
            if not current():
                return
            if result == wx.ID_APPLY:
                check(requested)
            elif result == wx.ID_OK and identity is not None and callable(editor):
                editor(workspace, identity)

        frame.run_job('Checking catalog quality', read, done)

    # run_job clears the previous operation's cancellation event. Only current
    # completions should consult it; a cancelled earlier operation cannot prevent
    # the user from deliberately starting a fresh catalog check.
    check(collection)
