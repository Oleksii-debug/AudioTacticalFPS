"""Native keyboard-first CSL workbench within the academic library window."""
from concurrent.futures import ThreadPoolExecutor
import threading

from ..citations.bibliography_workbench import (
    BibliographyWorkbench, EXPORT_FORMATS, LOCALES, METADATA_NOTICE, NAME_MODES,
)


def create_citation_styles_dialog(parent, controller, *, selected_identity=None, executor=None):
    import wx
    from ..citations.csl import ATTRIBUTION, available_styles

    class CitationStylesDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Citation styles and bibliography', size=(860, 800),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.workbench = BibliographyWorkbench(controller)
            self.selected_identity = selected_identity
            self.styles = available_styles()
            self.preview = None
            self.pending = None
            self.alive = True
            self.cancelled = threading.Event()
            self.owns_executor = executor is None
            self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='csl-preview')
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.StaticText(self, label=METADATA_NOTICE)
            explanation.Wrap(790)
            layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
            grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=8)
            grid.AddGrowableCol(1)
            self.style_choice = wx.Choice(self, choices=[item.title for item in self.styles])
            self.style_choice.SetSelection(0)
            scopes = ['All active references in this library']
            if selected_identity is not None:
                scopes.append('Selected reference only')
            self.scope_choice = wx.Choice(self, choices=scopes)
            self.scope_choice.SetSelection(1 if selected_identity is not None else 0)
            self.names_choice = wx.Choice(self, choices=[label for _, label in NAME_MODES])
            self.names_choice.SetSelection(0)
            self.locale_choice = wx.Choice(self, choices=[label for _, label in LOCALES])
            self.locale_choice.SetSelection(0)
            for label, control in (
                ('Citation style', self.style_choice),
                ('References to format; independent of list filters', self.scope_choice),
                ('Author-name convention; organisations can be enclosed in braces', self.names_choice),
                ('Citation language', self.locale_choice),
            ):
                control.SetName(label)
                grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                grid.Add(control, 1, wx.EXPAND)
                control.Bind(wx.EVT_CHOICE, self.on_options_changed)
            layout.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
            self.generate = wx.Button(self, label='&Preview formatting')
            self.generate.Bind(wx.EVT_BUTTON, self.on_preview)
            layout.Add(self.generate, 0, wx.ALL, 8)
            self.output = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.output.SetName('Formatted bibliography and citation examples')
            layout.Add(self.output, 1, wx.EXPAND | wx.ALL, 8)
            actions = wx.BoxSizer(wx.HORIZONTAL)
            self.copy_bibliography = wx.Button(self, label='Copy &bibliography')
            self.copy_citations = wx.Button(self, label='Copy citation e&xamples')
            self.export_button = wx.Button(self, label='&Export bibliography')
            self.copy_bibliography.Bind(wx.EVT_BUTTON, lambda event: self.on_copy('bibliography'))
            self.copy_citations.Bind(wx.EVT_BUTTON, lambda event: self.on_copy('citations'))
            self.export_button.Bind(wx.EVT_BUTTON, self.on_export)
            for button in (self.copy_bibliography, self.copy_citations, self.export_button):
                button.Enable(False)
                actions.Add(button, 0, wx.ALL, 4)
            layout.Add(actions, 0, wx.ALL, 4)
            self.status = wx.TextCtrl(self, value='Choose a style and preview the selected references.',
                                      style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Citation formatting status')
            self.status.SetMinSize((-1, 55))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            attribution = wx.StaticText(self, label=ATTRIBUTION)
            attribution.SetName('Citation processor attribution')
            attribution.Wrap(790)
            layout.Add(attribution, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.SetSizer(layout)
            self.SetMinSize((720, 650))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.options = (self.style_choice, self.scope_choice, self.names_choice, self.locale_choice)
            self.style_choice.SetFocus()

        def report(self, message, *, focus=True):
            self.status.SetValue(message)
            self.status.SetInsertionPoint(0)
            if focus:
                self.status.SetFocus()

        def controls(self, *, pending):
            for control in (*self.options, self.generate):
                control.Enable(not pending)
            for control in (self.copy_bibliography, self.copy_citations, self.export_button):
                control.Enable(not pending and self.preview is not None)

        def on_options_changed(self, event=None):
            self.cancelled.set()
            if self.pending is not None:
                self.pending.cancel()
                self.pending = None
            self.preview = None
            self.output.SetValue('')
            self.controls(pending=False)
            self.report('Options changed. Preview again before copying or exporting.', focus=False)

        def on_preview(self, event=None):
            if self.pending is not None:
                return
            try:
                style_index = self.style_choice.GetSelection()
                scope_index = self.scope_choice.GetSelection()
                name_index = self.names_choice.GetSelection()
                locale_index = self.locale_choice.GetSelection()
                if (not 0 <= style_index < len(self.styles) or scope_index not in (0, 1)
                        or (scope_index == 1 and self.selected_identity is None)
                        or not 0 <= name_index < len(NAME_MODES) or not 0 <= locale_index < len(LOCALES)):
                    raise ValueError('Choose a style, reference scope, name convention and language.')
                self.preview = None
                self.output.SetValue('')
                self.cancelled = threading.Event()
                self.controls(pending=True)
                self.report('Formatting locally. Close cancels delivery of this preview.', focus=False)
                future = self.executor.submit(self.workbench.preview,
                    identity=self.selected_identity if scope_index else None,
                    style=self.styles[style_index].id, locale=LOCALES[locale_index][0],
                    name_mode=NAME_MODES[name_index][0])
                self.pending = future
                future.add_done_callback(lambda completed: wx.CallAfter(self.on_ready, completed))
            except Exception as exc:
                self.pending = None
                self.controls(pending=False)
                self.report(f'Formatting not completed: {exc}')

        def on_ready(self, future):
            if not self.alive or self.cancelled.is_set() or self.pending is not future:
                return
            self.pending = None
            try:
                preview = future.result()
                self.workbench.check(preview)
                self.preview = preview
                self.output.SetValue(preview.explanation)
                self.output.SetInsertionPoint(0)
                self.report(f'Formatted {len(preview.result.entries)} references. '
                            'Read the preview and review notes before using the bibliography.', focus=False)
                self.output.SetFocus()
            except Exception as exc:
                self.preview = None
                self.report(f'Formatting not completed: {exc}')
            self.controls(pending=False)

        def on_copy(self, part):
            try:
                content = self.workbench.copy_text(self.preview, part=part)
                if not wx.TheClipboard.Open():
                    raise ValueError('The clipboard is busy. Try again.')
                try:
                    if not wx.TheClipboard.SetData(wx.TextDataObject(content)):
                        raise ValueError('The formatted text could not be copied.')
                finally:
                    wx.TheClipboard.Close()
                self.report('Bibliography copied.' if part == 'bibliography' else 'Citation examples copied.')
            except Exception as exc:
                self.report(f'Text not copied: {exc}')

        def on_export(self, event=None):
            if self.pending is not None:
                return
            try:
                self.workbench.check(self.preview)
                choice = wx.SingleChoiceDialog(self, 'Choose the bibliography file format.',
                    'Export bibliography', [label for _, label, _ in EXPORT_FORMATS])
                try:
                    if choice.ShowModal() != wx.ID_OK:
                        return
                    index = choice.GetSelection()
                    if not 0 <= index < len(EXPORT_FORMATS):
                        raise ValueError('Choose an export format.')
                    format, label, extension = EXPORT_FORMATS[index]
                finally:
                    choice.Destroy()
                chooser = wx.FileDialog(self, 'Save formatted bibliography',
                    defaultFile='bibliography.' + extension,
                    wildcard=f'{label} (*.{extension})|*.{extension}',
                    style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
                try:
                    if chooser.ShowModal() != wx.ID_OK:
                        return
                    destination = chooser.GetPath()
                finally:
                    chooser.Destroy()
                self.cancelled = threading.Event()
                self.controls(pending=True)
                self.report('Preparing the bibliography file locally. Close cancels an unfinished export.', focus=False)
                future = self.executor.submit(self.workbench.export, self.preview, destination,
                    format=format, cancelled=self.cancelled.is_set, return_receipt=True)
                self.pending = future
                future.add_done_callback(lambda completed: wx.CallAfter(self.on_export_ready, completed, destination))
            except Exception as exc:
                self.pending = None
                self.controls(pending=False)
                self.report(f'Bibliography not exported: {exc}')

        def on_export_ready(self, future, destination):
            if not self.alive or self.cancelled.is_set() or self.pending is not future:
                return
            self.pending = None
            try:
                receipt = future.result()
                notice = f'Bibliography saved to {destination}.'
                if receipt.cleanup_warning:
                    notice += ' ' + receipt.cleanup_warning
                self.report(notice)
            except Exception as exc:
                self.report(f'Bibliography not exported: {exc}')
            self.controls(pending=False)

        def on_close(self, event=None):
            self.cancelled.set()
            if self.pending is not None:
                self.pending.cancel()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.SetReturnCode(wx.ID_CANCEL)
                self.Hide()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                self.cancelled.set()
                if self.pending is not None:
                    self.pending.cancel()
                if self.owns_executor:
                    self.executor.shutdown(wait=False, cancel_futures=True)
            event.Skip()

    return CitationStylesDialog()
