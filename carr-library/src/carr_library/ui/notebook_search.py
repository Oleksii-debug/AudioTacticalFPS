"""Native, cancellable search of saved research in one current library.

Source verification runs in the dialog's worker. Controls and clipboard access
stay on the UI thread; closing the modal never waits for that worker.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import wx

from ..citations.anchors import format_citation


FIELDS = ('both', 'quote', 'note')


def evidence_key(hit):
    return hit.evidence.topic_id, hit.evidence.evidence_id


def validate_hit(hit, workspace):
    row = hit.evidence
    if (not row.topic_id or not row.evidence_id or not row.anchors or
            any(anchor.identity.workspace != workspace for anchor in row.anchors)):
        raise ValueError('Saved research does not belong to the current library.')
    return hit


def hit_label(hit, number):
    fields = ' and '.join('quotation' if field == 'quote' else 'note'
                          for field in hit.matched_fields)
    match = 'Matched ' + fields if fields else 'Exact tag match'
    source = ' '.join(format_citation(hit.evidence.anchors[0]).split())
    topic = ' / '.join(' '.join(name.split()) for name in hit.topic_path)
    state = 'Archived topic, read only' if hit.archived else 'Active topic'
    return f'{number}. Topic: {topic}. Source: {source}. {match}. {state}.'


def quotation_payload(hit):
    """Labels separate each exact source slice; research notes are not copied."""
    return '\n\n'.join(
        f'Source part {number}\nSource citation: {format_citation(anchor)}\n'
        f'Exact quotation:\n{anchor.quote}'
        for number, anchor in enumerate(hit.evidence.anchors, 1))


def same_saved_hit(before, fresh):
    # A fresh lookup has no search-match fields. Everything the user selected,
    # including the note, topic path and archived state, must still agree.
    return (before.evidence == fresh.evidence and before.topic_path == fresh.topic_path
            and before.tags == fresh.tags and before.archived == fresh.archived)


class NotebookSearchDialog(wx.Dialog):
    def __init__(self, frame, workspace):
        super().__init__(frame, title='Search saved research', size=(940, 860),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame, self.library, self.workspace = frame, frame.library, workspace
        self.hits = ()
        self.open_target = None
        self._closed = self._busy = False
        self._epoch = 0
        self._cancel = Event()
        self._future = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='notebook-search')
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label='Search saved quotations and your research notes '
            'across topics in the current library. Original book searches remain in Search.')
        explanation.Wrap(880)
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        form = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        form.AddGrowableCol(1)
        self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.field = wx.Choice(self, choices=['Quotations and notes', 'Quotations only', 'Notes only'])
        self.field.SetSelection(0)
        self.tag = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        for label, control in [('Search words', self.query), ('Search in', self.field),
                               ('Exact tag (optional)', self.tag)]:
            form.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            control.SetName(label)
            form.Add(control, 1, wx.EXPAND)
        layout.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.archived = wx.CheckBox(self, label='Include archived topics (read only)')
        layout.Add(self.archived, 0, wx.ALL, 8)
        commands = wx.BoxSizer(wx.HORIZONTAL)
        self.search = wx.Button(self, label='&Search saved research')
        self.cancel = wx.Button(self, label='Cancel current operation')
        commands.Add(self.search, 0, wx.RIGHT, 8)
        commands.Add(self.cancel)
        layout.Add(commands, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.items = wx.ListBox(self, style=wx.LB_SINGLE)
        self.items.SetName('Saved research results: topic, source, match field and archived state')
        layout.Add(wx.StaticText(self, label='Saved research results'), 0, wx.LEFT | wx.RIGHT, 8)
        layout.Add(self.items, 2, wx.EXPAND | wx.ALL, 8)
        self.parts = wx.Choice(self)
        self.parts.SetName('Selected quotation source page or segment')
        layout.Add(wx.StaticText(self, label='Source page or segment'), 0, wx.LEFT | wx.RIGHT, 8)
        layout.Add(self.parts, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.quote = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.citation = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.note = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        for label, control in [('Exact source quotation (read only)', self.quote),
                               ('Source citation (read only)', self.citation),
                               ('Saved research note (read only)', self.note)]:
            control.SetName(label)
            layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            layout.Add(control, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Saved research search status')
        self.status.SetMinSize((-1, 55))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.copy = wx.Button(self, label='Copy quotations and citations')
        self.open = wx.Button(self, label='Open topic and evidence')
        self.close = wx.Button(self, wx.ID_CANCEL, '&Close')
        for control in (self.copy, self.open, self.close):
            actions.Add(control, 0, wx.RIGHT, 8)
        layout.Add(actions, 0, wx.ALL, 8)
        self.SetSizer(layout)
        self.SetMinSize((720, 650))
        self.SetEscapeId(wx.ID_CANCEL)
        self.search.SetDefault()
        self.search.Bind(wx.EVT_BUTTON, self.on_search)
        self.query.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.tag.Bind(wx.EVT_TEXT_ENTER, self.on_search)
        self.cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.items.Bind(wx.EVT_LISTBOX, self.on_selection)
        self.parts.Bind(wx.EVT_CHOICE, self.show_part)
        self.copy.Bind(wx.EVT_BUTTON, lambda event: self.verify_selected('copy'))
        self.open.Bind(wx.EVT_BUTTON, lambda event: self.verify_selected('open'))
        self.close.Bind(wx.EVT_BUTTON, self.on_close)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
        for control, event in ((self.query, wx.EVT_TEXT), (self.tag, wx.EVT_TEXT),
                               (self.field, wx.EVT_CHOICE), (self.archived, wx.EVT_CHECKBOX)):
            control.Bind(event, self.on_options)
        self.clear_results()
        self.set_busy(False)
        self.status.SetValue('Enter search words, an exact tag, or both. Archived topics are excluded by default.')
        self.query.SetFocus()

    def same_library(self):
        return (not self._closed and not getattr(self.frame, '_closing', False)
                and self.frame.library is self.library and self.frame.active is not None
                and self.frame.active[0] == self.workspace)

    def settings(self):
        index = self.field.GetSelection()
        if not 0 <= index < len(FIELDS):
            raise ValueError('Choose quotations, notes, or both.')
        return (self.query.GetValue().strip(), FIELDS[index], self.tag.GetValue().strip() or None,
                bool(self.archived.GetValue()))

    def selected(self):
        index = self.items.GetSelection()
        return self.hits[index] if 0 <= index < len(self.hits) else None

    def clear_details(self):
        self.parts.Clear()
        self.parts.Disable()
        for control in (self.quote, self.citation, self.note):
            control.SetValue('')
        self.copy.Disable()
        self.open.Disable()

    def clear_results(self):
        self.hits = ()
        self.items.Clear()
        self.clear_details()

    def set_busy(self, busy):
        self._busy = busy
        for control in (self.query, self.field, self.tag, self.archived, self.search, self.items):
            control.Enable(not busy)
        self.cancel.Enable(busy)
        hit = self.selected()
        self.copy.Enable(not busy and hit is not None)
        self.open.Enable(not busy and hit is not None and not hit.archived)
        self.parts.Enable(not busy and hit is not None and len(hit.evidence.anchors) > 1)

    def fail(self, message):
        self.clear_results()
        self.status.SetValue(message)
        self.status.SetFocus()

    def on_options(self, event=None):
        if self._busy:
            self._cancel.set()
        self.clear_results()
        self.status.SetValue('Search options changed. Choose Search saved research.')
        if event is not None:
            event.Skip()

    def start(self, title, operation, done):
        if not self.same_library():
            self.fail('The current library changed. Close this search and open it again.')
            return False
        if self._busy:
            self.status.SetValue('An operation is running. Cancel it or close this search.')
            self.status.SetFocus()
            return False
        self._epoch += 1
        epoch = self._epoch
        self._cancel = cancellation = Event()
        self.set_busy(True)
        self.status.SetValue(title + '… Cancel current operation and Close remain available.')
        self.status.SetFocus()
        try:
            self._future = self._executor.submit(operation, cancellation.is_set)
        except Exception as exc:
            self.set_busy(False)
            self.fail(f'Could not start: {exc}')
            return False

        def completed(future):
            if not self._closed:
                wx.CallAfter(self.complete, epoch, cancellation, future, done)
        self._future.add_done_callback(completed)
        return True

    def complete(self, epoch, cancellation, future, done):
        if self._closed or epoch != self._epoch:
            return
        self.set_busy(False)
        if not self.same_library():
            self.fail('The current library changed. Previous results were cleared.')
            return
        if cancellation.is_set():
            self.fail('Operation cancelled. No result was copied or opened. Search again to reload saved research.')
            return
        try:
            done(future.result())
        except Exception as exc:
            self.fail(f'Saved research unavailable: {exc}')

    def on_search(self, event=None):
        if self._busy:
            return
        try:
            query, field, tag, archived = self.settings()
            if not query and tag is None:
                raise ValueError('Enter search words or an exact tag.')
        except Exception as exc:
            self.fail(str(exc))
            return
        self.clear_results()
        library, workspace = self.library, self.workspace
        self.start('Searching saved research', lambda cancelled: library.search_notebook(
            workspace, query, field=field, tag=tag, include_archived=archived,
            limit=50, timeout=60, cancelled=cancelled), self.show_results)

    def show_results(self, result):
        hits = tuple(validate_hit(hit, self.workspace) for hit in result.hits)
        if len({evidence_key(hit) for hit in hits}) != len(hits):
            raise ValueError('Duplicate saved research identity.')
        if not self.archived.GetValue() and any(hit.archived for hit in hits):
            raise ValueError('Archived results were not requested.')
        self.clear_results()
        self.hits = hits
        for number, hit in enumerate(hits, 1):
            self.items.Append(hit_label(hit, number))
        if hits:
            self.items.SetSelection(0)
            self.on_selection()
        message = (f'Showing {len(hits)} of {result.total_matches} matching saved evidence items. '
                   if hits else 'No matching saved research. ')
        message += f'Scanned {result.evidence_scanned} saved items in {result.topics_scanned} topics.'
        if result.truncated:
            message += ' The result limit was reached. Refine the search words or exact tag.'
        self.status.SetValue(message)
        self.status.SetFocus()

    def on_selection(self, event=None):
        self.clear_details()
        hit = self.selected()
        if hit is None:
            return
        for number, anchor in enumerate(hit.evidence.anchors, 1):
            self.parts.Append(f'Source part {number} of {len(hit.evidence.anchors)}; {anchor.locator.display()}')
        self.parts.SetSelection(0)
        self.note.SetValue(hit.evidence.note)
        self.show_part()
        self.set_busy(self._busy)

    def show_part(self, event=None):
        hit, index = self.selected(), self.parts.GetSelection()
        if hit is None or not 0 <= index < len(hit.evidence.anchors):
            self.quote.SetValue('')
            self.citation.SetValue('')
            return
        anchor = hit.evidence.anchors[index]
        self.quote.SetValue(anchor.quote)
        self.citation.SetValue(format_citation(anchor))

    def verify_selected(self, action):
        if self._busy:
            return
        hit = self.selected()
        if hit is None:
            self.fail('Select one saved research result first.')
            return
        if action not in ('copy', 'open'):
            self.fail('Choose Copy quotations and citations or Open topic and evidence.')
            return
        if action == 'open' and hit.archived:
            self.status.SetValue('This archived topic is read only. Its quotations and note remain readable here. '
                                 'Restore the topic explicitly before opening it for editing.')
            self.status.SetFocus()
            return
        key, part = evidence_key(hit), self.parts.GetSelection()
        archived = bool(self.archived.GetValue())
        library, workspace = self.library, self.workspace

        def done(fresh):
            validate_hit(fresh, workspace)
            selected = self.selected()
            if (selected is None or evidence_key(selected) != key or evidence_key(fresh) != key
                    or not same_saved_hit(hit, fresh)):
                raise ValueError('Saved research or its selection changed. Search again before copying or opening.')
            if action == 'open':
                if fresh.archived:
                    raise ValueError('The topic is archived. Restore it explicitly before opening for editing.')
                self.open_target = fresh, max(0, part)
                self.end(wx.ID_OK)
                return
            if not wx.TheClipboard.Open():
                raise RuntimeError('Clipboard is busy. Nothing copied.')
            try:
                if not wx.TheClipboard.SetData(wx.TextDataObject(quotation_payload(fresh))):
                    raise RuntimeError('Clipboard rejected the quotations. Nothing copied.')
                persisted = wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
            self.status.SetValue('Verified quotations and citations copied.' if persisted else
                'Verified quotations and citations copied. Paste before closing; availability after exit is not confirmed.')
            self.status.SetFocus()

        self.start('Verifying selected saved research', lambda cancelled: library.get_notebook_hit(
            workspace, *key, include_archived=archived, timeout=60, cancelled=cancelled), done)

    def on_cancel(self, event=None):
        self._cancel.set()
        if self._future is not None:
            self._future.cancel()
        self.status.SetValue('Cancellation requested. You can close this search immediately.')
        self.status.SetFocus()

    def shutdown(self):
        if not self._closed:
            self._closed = True
            self._epoch += 1
            self._cancel.set()
            self._executor.shutdown(wait=False, cancel_futures=True)

    def end(self, code):
        self.shutdown()
        if self.IsModal():
            self.EndModal(code)
        else:
            self.Hide()

    def on_close(self, event=None):
        self.end(wx.ID_CANCEL)

    def on_destroy(self, event):
        if event.GetEventObject() is self:
            self.shutdown()
        event.Skip()


def open_notebook_hit(frame, library, workspace, hit, part=0):
    """Use the existing discard confirmation and asynchronous evidence loader."""
    if (getattr(frame, '_closing', False) or frame.working or frame.library is not library
            or frame.active is None or frame.active[0] != workspace):
        frame.report('The current library changed or an operation is running. Open saved research search again.')
        return False
    if hit.archived:
        frame.report('Archived research is read only. Restore the topic explicitly before opening it for editing.')
        return False
    validate_hit(hit, workspace)
    topic, identity = evidence_key(hit)
    previous = frame.book
    # _open_workspace invokes the existing _confirm. A refused switch retains
    # the old notebook and focuses its draft; never load over that notebook.
    frame._open_workspace(workspace, topic)
    if frame.book is previous or frame.active != (workspace, topic) or frame.evidence is None:
        return False
    panel = frame.evidence
    if not panel.refresh(evidence_id=identity):
        return False
    expected_epoch = getattr(panel, '_evidence_epoch', None)
    deadline = monotonic() + 65

    def focus_loaded():
        if (getattr(frame, '_closing', False) or frame.library is not library
                or frame.active != (workspace, topic) or frame.evidence is not panel
                or getattr(panel, '_evidence_epoch', None) != expected_epoch):
            return
        if frame.working:
            if monotonic() < deadline:
                wx.CallLater(100, focus_loaded)
            return
        row = panel.displayed
        if row is None or (row.topic_id, row.evidence_id) != (topic, identity):
            panel._clear_source()
            frame.report('The selected evidence could not be reopened. Search saved research again.')
            return
        for index in range(frame.book.GetPageCount()):
            if frame.book.GetPage(index) is panel:
                frame.book.ChangeSelection(index)
                break
        panel.page_choice.SetSelection(part if 0 <= part < len(row.anchors) else 0)
        panel.show_page()
        panel.note.SetFocus()
    wx.CallAfter(focus_loaded)
    return True


def search_saved_research(frame, event=None):
    if frame.working or getattr(frame, 'busy', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return
    if getattr(frame, '_closing', False) or frame.active is None:
        frame.report('Open a library before searching saved research.')
        return
    library, workspace = frame.library, frame.active[0]
    dialog = NotebookSearchDialog(frame, workspace)
    try:
        dialog.ShowModal()
        target = dialog.open_target
    finally:
        dialog.shutdown()
        dialog.Destroy()
    if target is not None:
        try:
            open_notebook_hit(frame, library, workspace, *target)
        except Exception as exc:
            frame.report(f'Saved research was not opened: {exc}')

