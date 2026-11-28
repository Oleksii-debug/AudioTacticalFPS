"""Explicit native controls for the optional current-schema Word working copy."""
import threading

from ..academic_integration.formatting import CitationRequest
from ..academic_integration.regeneration import _references
from ..academic_integration.word_com import WordBridge
from ..research.export_file import capture_export_destination


def _host(window):
    seen = set()
    while window is not None and id(window) not in seen:
        seen.add(id(window))
        if callable(getattr(window, 'run_job', None)):
            return window
        getter = getattr(window, 'GetParent', None)
        window = getter() if callable(getter) else getattr(window, 'parent', None)
    raise RuntimeError('Open Word controls from the current library desktop window.')


def _live(window, identity):
    try:
        return (window is not None and not window.IsBeingDeleted()
                and window.GetId() == identity)
    except RuntimeError:
        return False


def create_word_citations_dialog(parent, controller, snapshot, result, *, host=None, bridge=None):
    import wx
    host = host or _host(parent)
    library, workspace = controller.library, controller.workspace
    def scope_current():
        active = getattr(host, 'active', None)
        return (getattr(host, 'library', None) is library
                and active is not None and active[0] == workspace)
    host_id, parent_id = host.GetId(), parent.GetId()
    # Default construction initializes the lazy comtypes import on this main
    # thread. It neither attaches to Word nor performs a library read.
    bridge = bridge or WordBridge(controller, snapshot, result)
    rows = tuple(_references(snapshot.references))

    class WordCitationsDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Word citation working copy', size=(790, 700),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id, self._destroy_requested = self.GetId(), False
            self.copy = None
            self.pending = None
            self.closing = False
            self.alive = True
            self.cancelled = threading.Event()
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.StaticText(self, label=(
                'Export the current formatted citations, open that DOCX in desktop Word and save it. '
                'Connect creates a NEW working copy. The original remains unchanged.\n'
                'Place the Word caret in ordinary managed body text, then return here to insert or edit. '
                'Review locators against the selected source. CARR does not independently verify quotations.\n'
                'Save explicitly in Word. If an action is uncertain, inspect it before another edit. '
                'Closing requests cancellation and waits for the actual Word call to return.'))
            explanation.Wrap(740)
            layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
            self.connect_button = wx.Button(self, label='&Connect and create working copy...')
            self.connect_button.SetName('Connect Word and create a new working copy')
            self.connect_button.Bind(wx.EVT_BUTTON, self.on_connect)
            layout.Add(self.connect_button, 0, wx.ALL, 6)
            self.reference = wx.Choice(self, choices=[
                row.get('title', 'Untitled')[:200] + ' [' + row['id'] + ']' for row in rows])
            self.reference.SetName('Current reviewed library reference')
            if rows:
                self.reference.SetSelection(0)
            layout.Add(wx.StaticText(self, label='Reference from the current reviewed library'), 0, wx.ALL, 4)
            layout.Add(self.reference, 0, wx.EXPAND | wx.ALL, 4)
            self.locator = wx.TextCtrl(self)
            self.locator.SetName('Explicit optional source locator')
            layout.Add(wx.StaticText(self, label='Optional locator you have reviewed'), 0, wx.ALL, 4)
            layout.Add(self.locator, 0, wx.EXPAND | wx.ALL, 4)
            self.label = wx.Choice(self, choices=['page', 'chapter', 'section', 'paragraph'])
            self.label.SetName('Locator type')
            self.label.SetSelection(0)
            layout.Add(self.label, 0, wx.EXPAND | wx.ALL, 4)
            self.locator_reviewed = wx.CheckBox(self, label='I checked this exact &locator in the selected source')
            self.locator_reviewed.SetName('Confirm current source locator review')
            self.locator_reviewed.SetValue(False)
            self.locator.Bind(wx.EVT_TEXT, self.revoke_locator)
            self.reference.Bind(wx.EVT_CHOICE, self.revoke_locator)
            self.label.Bind(wx.EVT_CHOICE, self.revoke_locator)
            layout.Add(self.locator_reviewed, 0, wx.ALL, 4)
            self.occurrence = wx.Choice(self, choices=[])
            self.occurrence.SetName('Existing stable Word citation occurrence')
            layout.Add(wx.StaticText(self, label='Existing citation to replace'), 0, wx.ALL, 4)
            layout.Add(self.occurrence, 0, wx.EXPAND | wx.ALL, 4)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            self.insert_button = wx.Button(self, label='&Insert at Word caret')
            self.edit_button = wx.Button(self, label='&Edit selected citation')
            self.refresh_button = wx.Button(self, label='&Refresh bibliography')
            self.inspect_button = wx.Button(self, label='Ins&pect working copy')
            self.insert_button.Bind(wx.EVT_BUTTON, lambda event: self.change(insert=True))
            self.edit_button.Bind(wx.EVT_BUTTON, lambda event: self.change(edit=True))
            self.refresh_button.Bind(wx.EVT_BUTTON, lambda event: self.change())
            self.inspect_button.Bind(wx.EVT_BUTTON, self.inspect)
            for button in (self.insert_button, self.edit_button, self.refresh_button, self.inspect_button):
                actions.Add(button, 0, wx.ALL, 3)
            layout.Add(actions, 0, wx.ALL, 4)
            self.status = wx.TextCtrl(self, value=(
                'Desktop Word is optional. Ordinary offline citations and DOCX exports remain available '
                'without Word or an Office account. Runtime: existing pinned comtypes; notices are included.'),
                style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Word citation action status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.close_button = wx.Button(self, wx.ID_CANCEL, 'Cl&ose')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 6)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.controls()
            self.connect_button.SetFocus()

        def context_alive(self):
            return (self.alive and _live(self, self._native_id)
                    and _live(parent, parent_id) and _live(host, host_id)
                    and not getattr(host, '_closing', False)
                    and scope_current())

        def revoke_locator(self, event=None):
            if self.context_alive():
                self.locator_reviewed.SetValue(False)
            if event is not None:
                event.Skip()

        def controls(self):
            if not self.context_alive():
                return
            idle = self.pending is None and not self.closing
            uncertain = bridge.recovery_required
            self.connect_button.Enable(idle and self.copy is None)
            for control in (self.reference, self.locator, self.label,
                            self.locator_reviewed, self.occurrence):
                control.Enable(idle and not uncertain)
            for button in (self.insert_button, self.edit_button, self.refresh_button):
                button.Enable(idle and self.copy is not None and not uncertain)
            self.inspect_button.Enable(idle and self.copy is not None)

        def report(self, text):
            if self.context_alive():
                self.status.SetValue(text)
                self.status.SetInsertionPoint(0)

        def run(self, operation):
            if not self.context_alive() or self.pending is not None or self.closing:
                return
            if getattr(host, 'working', False) or getattr(host, 'busy', False):
                self.report('Finish or cancel the current desktop operation before using Word.')
                return
            self.cancelled = threading.Event()
            local_cancel, host_cancel = self.cancelled, host.cancel_event
            token, started, completed = object(), threading.Event(), threading.Event()
            self.pending = token
            self.controls()
            self.report('Waiting for Word. Dismiss any open Word dialog if cancellation is pending.')

            def cancelled():
                # Worker: Python owner fields only, never a native wx method.
                return (local_cancel.is_set() or host_cancel.is_set() or not self.alive
                        or getattr(host, '_closing', False)
                        or not scope_current())

            def work():
                started.set()
                value, error = None, None
                try:
                    if cancelled():
                        raise InterruptedError('Word action cancelled before dispatch.')
                    value = operation(cancelled)
                    return value
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    completed.set()
                    # The host intentionally suppresses ordinary callbacks while
                    # closing. This same result settles our retained Python owner.
                    try:
                        wx.CallAfter(self.settle, token, value, error)
                    except RuntimeError:
                        pass

            try:
                host.run_job('Word citation action', work,
                    done=lambda value: self.settle(token, value, None),
                    failed=lambda error: self.settle(token, None, error))
                if (self.pending is token and not started.is_set()
                        and not getattr(host, 'working', False)):
                    raise RuntimeError('The desktop did not admit the Word action.')
            except BaseException as exc:
                if self.pending is token:
                    if (started.is_set() or getattr(host, 'working', False)) and not completed.is_set():
                        local_cancel.set()
                        self.report('Waiting for the admitted Word action to settle; no retry was started.')
                    else:
                        self.settle(token, None, exc)
                if not isinstance(exc, Exception):
                    raise

        def on_connect(self, event=None):
            if not self.context_alive() or self.pending is not None or self.copy is not None:
                return
            try:
                chooser = wx.FileDialog(self, 'Choose a NEW Word working copy',
                    defaultFile='word-citation-working-copy.docx',
                    wildcard='Word document (*.docx)|*.docx', style=wx.FD_SAVE)
                chooser_id = chooser.GetId()
                try:
                    accepted = chooser.ShowModal()
                    if (not self.context_alive() or not _live(chooser, chooser_id)
                            or accepted != wx.ID_OK):
                        return
                    destination = chooser.GetPath()
                finally:
                    if _live(chooser, chooser_id):
                        chooser.Destroy()
                if not self.context_alive():
                    return
                approval = capture_export_destination(destination, private_root=library.root,
                    _reject_existing=FileExistsError('Choose a new Word filename.'))
                if not approval.require_absent:
                    raise FileExistsError('Choose a new filename; existing documents are never overwritten.')
                def connect(cancelled):
                    copy = bridge.connect(destination, approval=approval, cancelled=cancelled)
                    # Report the already confirmed result without another COM
                    # call if cancellation arrived after the actual connection.
                    return bridge.completed_result(copy)
                self.run(connect)
            except Exception as exc:
                self.report('Word copy was not connected: ' + str(exc)[:2000])

        def change(self, *, insert=False, edit=False):
            if not self.context_alive() or self.copy is None or self.pending is not None:
                return
            try:
                arguments = {}
                if insert or edit:
                    index = self.reference.GetSelection()
                    if not 0 <= index < len(rows):
                        raise ValueError('Choose a current library reference.')
                    locator = self.locator.GetValue().strip()
                    label = self.label.GetSelection()
                    if not 0 <= label < 4:
                        raise ValueError('Choose the locator type.')
                    if locator and not self.locator_reviewed.GetValue():
                        raise ValueError('Check this exact locator in the selected source before continuing.')
                    arguments['items'] = (CitationRequest(rows[index]['id'], locator or None,
                        ('page', 'chapter', 'section', 'paragraph')[label],
                        locator_verified=bool(locator)),)
                    if insert:
                        arguments['insert'] = True
                    else:
                        index = self.occurrence.GetSelection()
                        if not 0 <= index < self.occurrence.GetCount():
                            raise ValueError('Choose an existing citation occurrence.')
                        arguments['occurrence_id'] = self.occurrence.GetString(index)
                copy = self.copy
                # Captured typed requests cannot change with later GUI edits.
                self.run(lambda cancelled: bridge.change(copy, cancelled=cancelled, **arguments))
            except Exception as exc:
                self.report('Word action was not started: ' + str(exc)[:2000])

        def inspect(self, event=None):
            if not self.context_alive() or self.copy is None or self.pending is not None:
                return
            copy = self.copy
            self.run(lambda cancelled: bridge.inspect(copy, cancelled=cancelled))

        def settle(self, token, value, error):
            if self.pending is not token:
                return
            self.pending = None
            # The closure retained bridge/copy/source owner until actual worker
            # completion. Dead native owners are never queried for the result.
            if not self.context_alive():
                self.closing = True
                self.finish_close()
                return
            self.copy = bridge.connected_copy or self.copy
            if error is not None:
                self.report('Word action needs review: ' + str(error)[:2000])
            else:
                result = value
                self.occurrence.Clear()
                for identity in result.citation_ids:
                    self.occurrence.Append(identity)
                if result.citation_ids:
                    self.occurrence.SetSelection(0)
                self.report(
                    f'Working copy: {result.path}\n'
                    f'{len(result.citation_ids)} citation occurrences; {len(result.reference_ids)} references; '
                    f'{result.kind} style. Original preserved. Review and save explicitly in Word.\n'
                    'The successful action is one named native Undo group. After Undo, inspect the copy; '
                    'inconsistent metadata blocks further edits instead of guessing recovery.')
            self.controls()
            if self.closing:
                self.finish_close()

        def finish_close(self):
            if not self.alive or not _live(self, self._native_id):
                return
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def on_close(self, event=None):
            if not self.alive:
                return
            self.closing = True
            self.cancelled.set()
            if self.pending is not None:
                self.controls()
                self.report('Cancellation requested. The current Word call still owns this action.')
                if event is not None and hasattr(event, 'Veto'):
                    event.Veto()
                return
            self.finish_close()

        def dispose(self):
            if not self.alive:
                return
            self.alive = False
            self.cancelled.set()

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not _live(self, self._native_id):
                return False
            self._destroy_requested = True
            return super().Destroy()

        def on_destroy(self, event):
            # wx may deliver a rewrapped object; the cached native ID is the
            # owning identity. No dead self.GetId or event object is needed.
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

    return WordCitationsDialog()


def show_word_citations(frame, controller, snapshot, result):
    """Open only after the formatter's modal loop has returned."""
    from .citation_formatting import _guard
    frame_id = frame.GetId()
    dialog = None
    dialog_id = None
    try:
        _guard(frame, controller.library, controller.workspace)
        dialog = create_word_citations_dialog(frame, controller, snapshot, result, host=frame)
        dialog_id = dialog.GetId()
        dialog.ShowModal()
    except Exception as error:
        if _live(frame, frame_id) and not getattr(frame, '_closing', False):
            frame.report('Word controls could not open: ' + str(error)[:2000])
    finally:
        if dialog is not None and _live(dialog, dialog_id):
            dialog.Destroy()
