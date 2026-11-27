"""Native EPUB publication through the shared Desktop background job lifecycle."""
import json
from pathlib import Path
from threading import Event

from ..audio.models import AudioBudget
from ..durable_state import local_path
from ..studio.publishing import NOTICE, PublicationService, language_tag
from ..studio.render import ProductionService


def create_publishing_dialog(frame, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class PublishingDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='EPUB3 publication — measured Studio line synchronization',
                size=(920, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self.alive, self.busy = True, False
            self._destroy_requested = False
            self.cancel = Event()
            self.library_root = local_path(frame.library.root, exists=True)
            self.production = ProductionService(self.library_root / 'state' / 'studio')
            self.service = PublicationService(self.production)
            self.jobs, self.receipts = (), ()
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label=NOTICE)
            notice.Wrap(860); box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
            def field(label, *, choices=None, value='', style=0, proportion=0):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
                control = wx.Choice(panel, choices=choices) if choices is not None else wx.TextCtrl(panel, value=value, style=style)
                control.SetName(label.replace('&', ''))
                box.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
                return control
            self.job_choice = field('Completed Studio &job — choose explicitly', choices=[])
            self.language = field('Publication &language tag; und means undetermined', value='und')
            self.creator = field('&Creator (optional)')
            self.output = field('Chosen new EPUB &file, or existing EPUB for receipt verification')
            self.max_gib = field('Maximum source/staging size in &GiB', value='32')
            self.minutes = field('Operation time budget in &minutes', value='60')
            self.receipt_choice = field('Saved publication &receipts — verify to check current bytes', choices=[])
            self.status = field('Publication &status and result', style=wx.TE_MULTILINE | wx.TE_READONLY, proportion=1)
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL); layout.Add(panel, 1, wx.EXPAND | wx.ALL, 4)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.buttons = {}
            for key, label, handler in (
                ('refresh', '&Load jobs / receipts', self.on_refresh), ('choose', 'Choose &destination', self.on_choose),
                ('export', '&Export EPUB3', self.on_export), ('verify', '&Verify receipt', self.on_verify),
                ('cancel', '&Cancel operation', self.on_cancel), ('close', '&Close', self.on_close)):
                button = wx.Button(self, label=label); button.Bind(wx.EVT_BUTTON, handler)
                self.buttons[key] = button; row.Add(button, 0, wx.ALL, 3)
            layout.Add(row, 0, wx.ALL, 3); self.SetSizer(layout)
            panel.FitInside()
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.report('Load saved Studio jobs, explicitly choose a completed job and a new EPUB destination. '
                        'Export verifies all source receipts and does not open a reader.')
            self.controls()

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def controls(self):
            if not self.alive:
                return
            for control in (self.job_choice,self.language,self.creator,self.output,self.max_gib,self.minutes,self.receipt_choice):
                control.Enable(not self.busy)
            for key in ('refresh','choose','export','verify'):
                self.buttons[key].Enable(not self.busy)
            self.buttons['cancel'].Enable(self.busy)

        def stopping(self):
            return (self.cancel.is_set() or frame.cancel_event.is_set() or not self.alive
                    or frame.library.root != self.library_root or getattr(frame, '_closing', False))

        def budget(self):
            return AudioBudget(max_output_bytes=int(self.max_gib.GetValue()) * 1024**3,
                               timeout=float(self.minutes.GetValue()) * 60)

        def begin(self, title, operation, complete):
            if self.busy or not self.alive:
                return False
            if local_path(frame.library.root, exists=True) != self.library_root:
                raise ValueError('The library location changed. Reopen EPUB publication for the selected library.')
            if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                self.report('Another operation is active. Finish or cancel it before publishing.')
                return False
            self.cancel.clear(); self.busy = True; self.controls()
            def done(value):
                self.busy = False
                if self.alive:
                    if frame.library.root == self.library_root:
                        complete(value)
                    else:
                        self.report('The library location changed. Reopen EPUB publication to review its records.')
                    self.controls(); self.status.SetFocus()
            def failed(error):
                self.busy = False
                if self.alive:
                    self.report(str(error)); self.controls(); self.status.SetFocus()
            frame.run_job(title, operation, done=done, failed=failed)
            return True

        def on_refresh(self, event=None):
            if not self.alive:
                return
            try:
                def done(value):
                    self.jobs, self.receipts = value
                    self.job_choice.Set([f'{row["title"]}; {row["lines"]} measured lines; {row["id"]}' for row in self.jobs])
                    self.receipt_choice.Set([f'{row.get("receipt_status", row["status"])}; {row["id"]}; {Path(row["output"]).name}' for row in self.receipts])
                    self.report('Choose a completed job and a new output filename. Saved receipt status is metadata; '
                                'Verify receipt checks the current EPUB bytes.')
                self.begin('Loading Studio publication records', lambda: (self.service.jobs(), self.service.publications()), done)
            except Exception as exc:
                self.report(exc)

        def on_choose(self, event=None):
            if not self.alive:
                return
            picker = wx.FileDialog(self, 'Choose a new EPUB, or the existing file whose receipt you will verify',
                                   defaultFile='publication.epub', wildcard='EPUB (*.epub)|*.epub', style=wx.FD_SAVE)
            try:
                if picker.ShowModal() == wx.ID_OK:
                    self.output.ChangeValue(picker.GetPath())
            finally:
                picker.Destroy()

        def on_export(self, event=None):
            if not self.alive:
                return
            try:
                index = self.job_choice.GetSelection()
                if not 0 <= index < len(self.jobs):
                    raise ValueError('Load and explicitly choose a completed Studio job first.')
                if not self.output.GetValue().strip():
                    raise ValueError('Choose a new EPUB file before export.')
                identity, output = self.jobs[index]['id'], Path(self.output.GetValue())
                language, creator = language_tag(self.language.GetValue().strip()), self.creator.GetValue()
                service = PublicationService(self.production, budget=self.budget())
                approval = service.prepare_destination(output)
                def operation():
                    token = frame.operation_progress.event.token
                    def progress(message):
                        frame.operation_progress.update(phase=message, token=token)
                        wx.CallAfter(self.report, message)
                    return service.export_epub(identity, output, language=language, creator=creator,
                                               cancelled=self.stopping, progress=progress, approval=approval)
                def done(value):
                    self.receipts = (*self.receipts, value)
                    self.receipt_choice.Set([f'{row.get("receipt_status", row["status"])}; {row["id"]}; {Path(row["output"]).name}' for row in self.receipts])
                    self.receipt_choice.SetSelection(len(self.receipts) - 1)
                    warnings = '\n'.join(value.get('warnings', ()))
                    self.report('EPUB saved and its bytes verified.\n' + (warnings + '\n' if warnings else '')
                                + json.dumps(value, ensure_ascii=False, indent=2))
                self.begin('Publishing synchronized EPUB3', operation, done)
            except Exception as exc:
                self.report(exc)

        def on_verify(self, event=None):
            if not self.alive:
                return
            try:
                index = self.receipt_choice.GetSelection()
                if not 0 <= index < len(self.receipts):
                    raise ValueError('Load and choose a saved publication receipt first.')
                identity = self.receipts[index]['id']
                selected = Path(self.output.GetValue()) if self.output.GetValue().strip() else None
                service = PublicationService(self.production, budget=self.budget())
                def done(result):
                    self.receipts = tuple(result if row['id'] == identity else row for row in self.receipts)
                    self.receipt_choice.Set([f'{row.get("receipt_status", row["status"])}; {row["id"]}; {Path(row["output"]).name}'
                                             for row in self.receipts])
                    self.receipt_choice.SetSelection(index)
                    warnings = '\n'.join(result.get('warnings', ()))
                    self.report('Verified exact saved EPUB bytes and media overlay graph.\n'
                                + (warnings + '\n' if warnings else '')
                                + json.dumps(result, ensure_ascii=False, indent=2))
                self.begin('Verifying saved EPUB publication',
                    lambda: service.verify(identity, output=selected, cancelled=self.stopping), done)
            except Exception as exc:
                self.report(exc)

        def on_cancel(self, event=None):
            if not self.alive:
                return
            if self.busy:
                self.cancel.set(); frame.cancel_event.set()
                self.report('Cancellation requested. A completed EPUB and its prepared receipt remain available for verification.')

        def dispose(self):
            """Detach pending work without touching a possibly deleted wx peer."""
            if not self.alive:
                return
            self.alive = False
            self.cancel.set()
            if self.busy:
                frame.cancel_event.set()

        def on_destroy(self, event):
            # A destruction event can carry a different Python wrapper. The
            # cached native ID remains usable after the original peer is gone.
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def on_close(self, event=None):
            if not self.alive:
                return
            if self.busy:
                self.on_cancel()
            self.dispose()
            if self:
                if self.IsModal(): self.EndModal(wx.ID_CLOSE)
                else: self.Hide()

    return PublishingDialog()


def show_publishing(frame):
    dialog = create_publishing_dialog(frame)
    try:
        dialog.ShowModal()
    finally:
        dialog.on_close()
        if dialog:
            dialog.Destroy()
