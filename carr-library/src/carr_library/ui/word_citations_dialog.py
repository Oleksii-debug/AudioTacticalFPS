"""Explicit keyboard workflow for the optional managed Word citation copy."""
import threading

from ..citations.bibliography_workbench import records_for_style
from ..citations.word_com import WordBridge
from ..research.export_file import capture_export_destination


def _host(window):
    seen = set()
    while window is not None and id(window) not in seen:
        seen.add(id(window))
        if callable(getattr(window, 'run_job', None)):
            return window
        getter = getattr(window, 'GetParent', None)
        window = getter() if callable(getter) else getattr(window, 'parent', None)
    raise RuntimeError('Open Word controls from the current research-library desktop window.')


def create_word_citations_dialog(parent, workbench, preview, *, host=None, bridge=None):
    import wx
    host = host or _host(parent)
    bridge = bridge or WordBridge(workbench)
    library = workbench.controller.library
    rows, _ = records_for_style(workbench.check(preview), name_mode=preview.name_mode)

    class WordCitationsDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Word citation working copy', size=(770, 600),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.copy = None
            self.pending = None
            self.closing = False
            self.alive = True
            self.cancelled = threading.Event()
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.StaticText(self, label=(
                'Open a saved Editable Word citations and bibliography export in desktop Word. '
                'Connect creates a NEW copy; the original remains unchanged. The document keeps '
                'its saved CSL style. Only the connected copy receives citation edits.\n'
                'Switch to Word to place the caret in ordinary document body text, then return here. '
                'Quotations and locators are not automatically verified. Save the result explicitly in Word.\n'
                'Automation bridge: pywin32 312, Mark Hammond and contributors; mixed upstream licences. '
                'COM/WIN32 notices and all wheel licence files accompany the optional runtime.'))
            explanation.Wrap(720)
            layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
            self.connect_button = wx.Button(self, label='&Connect and create working copy...')
            self.connect_button.SetName('Connect Word and create a new working copy')
            self.connect_button.Bind(wx.EVT_BUTTON, self.on_connect)
            layout.Add(self.connect_button, 0, wx.ALL, 8)
            self.reference = wx.Choice(self, choices=[
                row.get('title', 'Untitled') + ' [' + row['id'] + ']' for row in rows])
            self.reference.SetName('Reference to insert or use for the selected citation')
            if rows:
                self.reference.SetSelection(0)
            layout.Add(wx.StaticText(self, label='Reference from the reviewed library selection'), 0, wx.ALL, 4)
            layout.Add(self.reference, 0, wx.EXPAND | wx.ALL, 4)
            self.locator = wx.TextCtrl(self)
            self.locator.SetName('Explicit optional quotation locator; never inferred')
            layout.Add(wx.StaticText(self, label='Optional locator you have reviewed'), 0, wx.ALL, 4)
            layout.Add(self.locator, 0, wx.EXPAND | wx.ALL, 4)
            self.label = wx.Choice(self, choices=['page', 'chapter', 'section', 'paragraph'])
            self.label.SetName('Locator type')
            self.label.SetSelection(0)
            layout.Add(self.label, 0, wx.EXPAND | wx.ALL, 4)
            self.occurrence = wx.Choice(self, choices=[])
            self.occurrence.SetName('Existing stable citation occurrence to replace')
            layout.Add(wx.StaticText(self, label='Existing citation; editing replaces its selected reference items'), 0, wx.ALL, 4)
            layout.Add(self.occurrence, 0, wx.EXPAND | wx.ALL, 4)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            self.insert_button = wx.Button(self, label='&Insert at Word caret')
            self.edit_button = wx.Button(self, label='&Edit selected citation')
            self.refresh_button = wx.Button(self, label='&Refresh citations and bibliography')
            self.insert_button.Bind(wx.EVT_BUTTON, lambda event: self.change(insert=True))
            self.edit_button.Bind(wx.EVT_BUTTON, lambda event: self.change(edit=True))
            self.refresh_button.Bind(wx.EVT_BUTTON, lambda event: self.change())
            for button in (self.insert_button, self.edit_button, self.refresh_button):
                actions.Add(button, 0, wx.ALL, 4)
            layout.Add(actions, 0, wx.ALL, 4)
            self.status = wx.TextCtrl(self, value=(
                'Requires desktop Microsoft Word and the optional pinned Word runtime. '
                'No Office account or Word dependency is needed for ordinary offline exports.'),
                style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Word citation action status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.controls()
            self.connect_button.SetFocus()

        def controls(self):
            idle = self.pending is None and not self.closing
            self.connect_button.Enable(idle)
            for control in (self.reference, self.locator, self.label, self.occurrence):
                control.Enable(idle)
            for button in (self.insert_button, self.edit_button, self.refresh_button):
                button.Enable(idle and self.copy is not None)

        def report(self, text):
            self.status.SetValue(text)
            self.status.SetInsertionPoint(0)

        def run(self, operation):
            if self.pending is not None or self.closing:
                return
            if (getattr(host, 'working', False) or getattr(host, 'busy', False)
                    or getattr(host, '_closing', False) or getattr(host, 'library', None) is not library):
                self.report('The desktop is busy or its library changed. Finish that action before using Word.')
                return
            self.cancelled = threading.Event()
            token, started = object(), threading.Event()
            self.pending = token
            self.controls()
            self.report('Waiting for Word. Close requests cancellation; dismiss any open dialog in Word.')

            def cancelled():
                return (self.cancelled.is_set() or host.cancel_event.is_set() or not self.alive
                        or getattr(host, '_closing', False) or getattr(host, 'library', None) is not library)

            def work():
                started.set()
                try:
                    if cancelled():
                        raise InterruptedError('The Word action was cancelled before dispatch.')
                    result = operation(cancelled)
                except BaseException as exc:
                    # The worker settles the modal even if the host is closing
                    # and intentionally suppresses ordinary result callbacks.
                    try:
                        wx.CallAfter(self.settle, token, None, exc)
                    except RuntimeError:
                        pass
                    raise
                try:
                    wx.CallAfter(self.settle, token, result, None)
                except RuntimeError:
                    pass
                return result

            try:
                host.run_job('Word citation action', work,
                    done=lambda result: self.settle(token, result, None),
                    failed=lambda error: self.settle(token, None, error))
                # DesktopFrame.run_job intentionally returns None. Its owned
                # busy state, callback or actual worker admission is the signal.
                if self.pending is token and not started.is_set() and not getattr(host, 'working', False):
                    raise RuntimeError('The desktop did not admit the Word operation.')
            except Exception as exc:
                self.settle(token, None, exc)

        def on_connect(self, event=None):
            if self.pending is not None:
                return
            try:
                chooser = wx.FileDialog(self, 'Choose a NEW working copy filename',
                    defaultFile='word-citation-working-copy.docx',
                    wildcard='Word document (*.docx)|*.docx', style=wx.FD_SAVE)
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    destination = chooser.GetPath()
                finally:
                    chooser.Destroy()
                approval = capture_export_destination(destination,
                    private_root=workbench.controller.library.root)
                if not approval.require_absent:
                    raise FileExistsError('Choose a new filename; this action does not overwrite documents.')
                def connect(cancelled):
                    from ..citations.csl import CustomStyle
                    custom = preview.style if isinstance(preview.style, CustomStyle) else None
                    copy = bridge.connect(destination, custom_style=custom, approval=approval, cancelled=cancelled)
                    return copy, bridge.inspect(copy, cancelled=cancelled)
                self.run(connect)
            except Exception as exc:
                self.report('Word working copy was not connected: ' + str(exc))

        def change(self, *, insert=False, edit=False):
            if self.copy is None or self.pending is not None:
                return
            try:
                arguments = {}
                if insert or edit:
                    index = self.reference.GetSelection()
                    if not 0 <= index < len(rows):
                        raise ValueError('Choose the reference to use.')
                    item = {'id': rows[index]['id']}
                    locator = self.locator.GetValue().strip()
                    if locator:
                        kind = self.label.GetSelection()
                        if not 0 <= kind < 4:
                            raise ValueError('Choose the locator type.')
                        item.update(locator=locator, label=('page', 'chapter', 'section', 'paragraph')[kind])
                    arguments['items'] = [item]
                    if insert:
                        arguments['insert'] = True
                    else:
                        index = self.occurrence.GetSelection()
                        if not 0 <= index < self.occurrence.GetCount():
                            raise ValueError('Choose an existing citation occurrence.')
                        arguments['occurrence_id'] = self.occurrence.GetString(index)
                copy = self.copy
                self.run(lambda cancelled: (
                    copy, bridge.change(copy, preview=preview, cancelled=cancelled, **arguments)))
            except Exception as exc:
                self.report('Word citation action was not started: ' + str(exc))

        def settle(self, token, value, error):
            if not self.alive or self.pending is not token:
                return
            self.pending = None
            try:
                if error is not None:
                    raise error
                copy, result = value
                self.copy = copy
                self.occurrence.Clear()
                for identity in result.citation_ids:
                    self.occurrence.Append(identity)
                if result.citation_ids:
                    self.occurrence.SetSelection(0)
                self.report(
                    f'Working copy: {result.path}\n'
                    f'{len(result.citation_ids)} citation occurrences; {len(result.reference_ids)} references; '
                    f'{result.kind} style. Original preserved. Review and save in Word; '
                    'the latest successful citation change is one named Word Undo action.')
            except Exception as exc:
                self.report('Word action did not complete: ' + str(exc))
            self.controls()
            if self.closing:
                self.finish_close()

        def finish_close(self):
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def on_close(self, event=None):
            self.closing = True
            self.cancelled.set()
            if self.pending is not None:
                self.controls()
                self.report('Cancellation requested. Waiting for Word to settle; no Word process will be terminated.')
                if event is not None and hasattr(event, 'Veto'):
                    event.Veto()
                return
            self.finish_close()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                self.cancelled.set()
            event.Skip()

    return WordCitationsDialog()
