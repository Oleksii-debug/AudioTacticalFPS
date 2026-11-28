"""Native exact-preview diagnostics using the existing desktop executor."""
import threading

from .. import support_diagnostics as diagnostics
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command

DIAGNOSTICS_COMMAND = CommandSpec('help.diagnostics', 'Preview and export redacted diagnostics')


class DiagnosticsSession:
    """One library-bound preview; no worker, timer, logger or telemetry owner."""
    def __init__(self, frame, *, changed):
        self.frame = frame
        self.library = frame.library
        self.root = self.library.root
        self.context = diagnostics.bind_library(self.root)
        self.changed = changed
        self.cancelled = threading.Event()
        self.closed, self.busy, self.epoch = False, False, 0
        self.preview = None
        self.message = ('Prepare a report to review fixed technical fields. '
                        'No source text, jobs, transcripts, credentials, paths, '
                        'environment or arbitrary logs are collected.')

    def current(self):
        return (not self.closed and not getattr(self.frame, '_closing', False)
                and getattr(self.frame, 'library', None) is self.library
                and self.library.root == self.root)

    def _cancelled(self):
        host = getattr(self.frame, 'cancel_event', None)
        return (self.cancelled.is_set() or not self.current()
                or (host is not None and host.is_set()))

    def _notify(self):
        if self.current():
            self.changed(self)

    def _start(self, title, operation, completed):
        if not self.current():
            return False
        if self.busy or getattr(self.frame, 'working', False):
            self.message = 'Wait for the current operation or cancel it first.'
            self._notify()
            return False
        self.epoch += 1
        token = self.epoch
        self.busy = True
        self.cancelled.clear()
        self.message = title + '…'
        self._notify()
        settled = False

        def settle(result=None, error=None):
            nonlocal settled
            if settled:
                return
            settled = True
            if token != self.epoch:
                return
            self.busy = False
            if not self.current():
                return
            if error is not None:
                self.message = diagnostics.error_notice(error)
            else:
                try:
                    completed(result)
                except Exception as exc:
                    self.message = diagnostics.error_notice(exc)
            self._notify()

        def work():
            try:
                diagnostics.check_context(self.context, cancelled=self._cancelled)
                return operation()
            except (diagnostics.DiagnosticError, diagnostics.DiagnosticCancelled):
                raise
            except Exception:
                raise diagnostics.DiagnosticError('EXPORT_FAILED') from None

        try:
            self.frame.run_job(title, work, done=lambda value: settle(value),
                               failed=lambda error: settle(error=error))
        except Exception as exc:
            settle(error=exc)
        except BaseException as exc:
            self.cancelled.set()
            settle(error=exc)
            raise
        return True

    def prepare(self):
        if not self.current() or self.busy or getattr(self.frame, 'working', False):
            return False
        self.preview = None
        def completed(report):
            diagnostics.check_report(report, cancelled=self._cancelled)
            self.preview = report
            self.message = (f'Exact preview: {len(report.payload)} UTF-8 bytes. '
                            f'SHA-256: {report.sha256}. '
                            'Resource reservations are declared estimates. Models and audio devices were not probed.')
        return self._start('Preparing redacted diagnostics',
            lambda: diagnostics.prepare_report(self.context, cancelled=self._cancelled), completed)

    def save(self, destination, *, reviewed):
        if (not self.current() or self.busy or getattr(self.frame, 'working', False)
                or reviewed is None or reviewed is not self.preview):
            return False
        expected = reviewed.sha256
        def operation():
            if reviewed is not self.preview:
                raise diagnostics.DiagnosticError('PREVIEW_CHANGED')
            approval = diagnostics.prepare_destination(reviewed, destination, cancelled=self._cancelled)
            return diagnostics.export_report(reviewed, approval, expected_sha256=expected,
                                             cancelled=self._cancelled)
        def completed(receipt):
            # Once the common writer commits, later cancellation cannot undo it.
            self.message = 'The exact diagnostic preview was saved to the selected new file.'
            if getattr(receipt, 'cleanup_warning', None):
                self.message += ' Temporary-file cleanup is still pending; do not repeat the export.'
        return self._start('Saving the reviewed diagnostic report', operation, completed)

    def cancel(self):
        if self.current() and self.busy:
            self.cancelled.set()
            self.message = 'Cancellation requested. Waiting for the current owned operation to finish.'
            self._notify()

    def close(self):
        if not self.closed:
            self.closed = True
            self.cancelled.set()
            self.epoch += 1
            self.preview = None


def create_diagnostics_dialog(frame):
    import wx

    class DiagnosticsDialog(wx.Dialog):
        def __init__(self):
            self.session = DiagnosticsSession(frame, changed=self.update_state)
            super().__init__(frame, title='Redacted diagnostic report', size=(830, 650),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Review the exact report before saving. Only fixed technical versions, '
                'capabilities and numeric resource counts are included. '
                'Nothing is sent over the network. Save requires a new file outside the library.'))
            notice.Wrap(780)
            layout.Add(notice, 0, wx.EXPAND | wx.ALL, 8)
            self.preview = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
            self.preview.SetName('Exact diagnostic report preview; read only')
            layout.Add(self.preview, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.status.SetName('Diagnostic report status, byte count and SHA-256')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.WrapSizer(wx.HORIZONTAL)
            self.prepare = wx.Button(self, label='&Prepare preview')
            self.save = wx.Button(self, label='&Save exact preview…')
            self.cancel = wx.Button(self, label='Cancel &operation')
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            for control in (self.prepare, self.save, self.cancel, self.close_button):
                buttons.Add(control, 0, wx.ALL, 5)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
            self.SetSizer(layout)
            self.prepare.Bind(wx.EVT_BUTTON, lambda event: self.session.prepare())
            self.save.Bind(wx.EVT_BUTTON, self.save_report)
            self.cancel.Bind(wx.EVT_BUTTON, lambda event: self.session.cancel())
            self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.update_state(self.session)
            self.prepare.SetFocus()

        def update_state(self, session):
            if not self or not session.current():
                return
            text = session.preview.text if session.preview is not None else ''
            if self.preview.GetValue() != text:
                self.preview.ChangeValue(text)
                self.preview.SetInsertionPoint(0)
            self.status.ChangeValue(session.message)
            self.prepare.Enable(not session.busy)
            self.save.Enable(not session.busy and session.preview is not None)
            self.cancel.Enable(session.busy)

        def save_report(self, event=None):
            session = self.session
            reviewed = session.preview
            if not session.current() or session.busy or reviewed is None:
                return
            if self.preview.GetValue() != reviewed.text:
                session.message = diagnostics.error_notice(diagnostics.DiagnosticError('PREVIEW_CHANGED'))
                session._notify()
                return
            picker = wx.FileDialog(self, 'Save the exact diagnostic preview as a new file',
                                   defaultFile='resonance-diagnostics.json',
                                   wildcard='JSON report (*.json)|*.json',
                                   style=wx.FD_SAVE)
            try:
                answer = picker.ShowModal()
                if (not self or not session.current() or not picker
                        or answer != wx.ID_OK or reviewed is not session.preview):
                    return
                destination = picker.GetPath()
            finally:
                if picker:
                    picker.Destroy()
            if self and session.current():
                session.save(destination, reviewed=reviewed)

        def on_close(self, event=None):
            self.session.close()
            if self and self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            elif self:
                self.Destroy()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self.session.close()
            event.Skip()

        def Destroy(self):
            self.session.close()
            return super().Destroy()

    return DiagnosticsDialog()


def show_diagnostics(frame, event=None):
    if getattr(frame, '_closing', False):
        return
    if getattr(frame, '_diagnostics_dialog', None) is not None:
        frame.report('Close the current diagnostic preview before opening another.')
        return
    if getattr(frame, 'working', False):
        frame.report('Wait for the current operation or cancel it before preparing diagnostics.')
        return
    dialog = None
    try:
        dialog = create_diagnostics_dialog(frame)
        frame._diagnostics_dialog = dialog
        dialog.ShowModal()
    except Exception as exc:
        if not getattr(frame, '_closing', False):
            frame.report(diagnostics.error_notice(exc))
    finally:
        if dialog is not None:
            dialog.session.close()
            if dialog:
                dialog.Destroy()
        if getattr(frame, '_diagnostics_dialog', None) is dialog:
            frame._diagnostics_dialog = None


def install_diagnostics_actions(frame):
    import wx
    if getattr(frame, '_diagnostics_installed', False):
        return
    menu_bar = frame.GetMenuBar()
    selected = [menu_bar.GetMenu(index) for index in range(menu_bar.GetMenuCount())
                if menu_bar.GetMenuLabel(index).replace('&', '').split('\t', 1)[0].strip().casefold() == 'help']
    if len(selected) != 1:
        raise ValueError('The diagnostic command requires the existing Help menu.')
    item = selected[0].Append(wx.ID_ANY, 'Preview and export &diagnostics…')
    frame.Bind(wx.EVT_MENU, lambda event: show_diagnostics(frame, event), item)
    register_menu_command(frame, DIAGNOSTICS_COMMAND, item)
    frame._diagnostics_installed = True
