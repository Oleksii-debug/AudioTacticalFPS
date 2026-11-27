"""Explicit native update review using the existing Desktop job owner."""
from __future__ import annotations

import threading
import wx

from ..security.maintenance import UpdateConfiguration, MaintenanceSession
from ..security.controller import release_text
from ..security.download_state import PausedDownload
from ..security.updates import UpdateCancelled


def _live(window):
    try:
        return window is not None and bool(window) and not window.IsBeingDeleted()
    except RuntimeError:
        return False


class MaintenanceDialog(wx.Dialog):
    def __init__(self, frame, session):
        super().__init__(frame, title='Review and apply signed updates', size=(820, 620),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame, self.session, self.app = frame, session, session.app
        self._native_id, self._alive, self._destroy_requested = self.GetId(), True, False
        self._cancel, self._token, self._busy = threading.Event(), None, False
        self._pause, self._downloading = threading.Event(), False
        self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.TextCtrl(self, value=session.configuration.description(),
                                  style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Signed update review and recovery details')
        layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.actions = []
        for label, callback in (
                ('Check configured signed source', self.on_check),
                ('Download reviewed package', self.on_download),
                ('Apply reviewed standard update', self.on_apply),
                ('Recover interrupted standard update', self.on_recover),
                ('Show external maintenance command', self.on_command)):
            button = wx.Button(self, label=label)
            button.SetName(label); button.Bind(wx.EVT_BUTTON, callback)
            self.actions.append(button); actions.Add(button, 0, wx.ALL, 4)
        layout.Add(actions, 0, wx.EXPAND | wx.ALL, 4)
        transfer_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.download_actions = []
        for label, callback in (
                ('Pause update download', self.on_pause_download),
                ('Resume update download', self.on_resume_download),
                ('Cancel update download', self.on_cancel_download)):
            button = wx.Button(self, label=label)
            button.SetName(label); button.Bind(wx.EVT_BUTTON, callback)
            self.download_actions.append(button)
            transfer_actions.Add(button, 0, wx.ALL, 4)
        layout.Add(transfer_actions, 0, wx.EXPAND | wx.ALL, 4)
        self.close_button = wx.Button(self, wx.ID_CLOSE, label='Close')
        self.close_button.Bind(wx.EVT_BUTTON, self.on_close)
        layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizer(layout)
        self._capabilities()
        self.status.SetFocus()

    def _current(self):
        return self._alive and _live(self.frame) and getattr(self.frame, 'library', None) is self.app

    def _capabilities(self):
        if not self._current():
            return
        configured = self.session.configuration.checked()['installation']['kind']
        reviewed, downloaded = self.session.release is not None, self.session.package is not None
        retained = getattr(self.session, 'download_state', None) is not None
        enabled = (True, reviewed and not retained, downloaded and configured == 'standard',
                   downloaded and configured == 'standard', reviewed)
        for button, allowed in zip(self.actions, enabled):
            button.Enable(bool(allowed and not self._busy))
        active_download = self._busy and self._downloading
        for button, allowed in zip(self.download_actions, (
                active_download and not self._pause.is_set() and not self._cancel.is_set(),
                reviewed and retained and not self._busy,
                active_download or (reviewed and retained and not self._busy))):
            button.Enable(bool(allowed))

    def _write(self, value):
        if self._current():
            self.status.SetValue(value)
            if self._current():
                self.status.SetFocus()

    def _run(self, label, operation, completed, *, downloading=False):
        if not self._current() or self._busy:
            return
        if self.frame.working:
            self._write('Another operation is using the desktop worker. Finish or cancel that operation before update review.')
            return
        token = object()
        self._token, self._busy, self._downloading = token, True, downloading
        self._cancel.clear()
        self._pause.clear()
        host_cancel = self.frame.cancel_event
        self._capabilities()
        def work(_report_progress=None):
            # Measured Desktop jobs pass their reporting callback. Download
            # bytes update the same bound task through current_progress().
            if self._cancel.is_set() or getattr(self.frame, 'library', None) is not self.app:
                raise InterruptedError('Update review owner changed before work.')
            return operation(lambda: self._cancel.is_set() or host_cancel.is_set())
        def done(value):
            if not self._current() or token is not self._token:
                return
            self._busy, self._token, self._downloading = False, None, False
            completed(value)
            if self._current():
                self._capabilities()
        def failed(error):
            if not self._current() or token is not self._token:
                return
            self._busy, self._token, self._downloading = False, None, False
            self._write('The update operation was not confirmed. Keep the prepared package and recovery workspace. '
                        + str(error)[:1200])
            self._capabilities()
        try:
            accepted = self.frame.run_job(label, work, done=done, failed=failed, with_progress=downloading)
            if (self._token is token and self._busy
                    and (accepted is False or not self.frame.working)):
                failed(RuntimeError('The desktop worker did not admit this update operation.'))
        except (Exception, KeyboardInterrupt) as error:
            failed(error)

    def on_check(self, event=None):
        self._run('Check explicitly configured signed update source',
                  lambda cancelled: self.session.check(cancelled=cancelled),
                  lambda release: self._write(release_text(release) + (
                      '\n\nA retained download is available. Choose Resume update download to recheck and continue it.'
                      if getattr(self.session, 'download_state', None) is not None else '')))

    def _download_result(self, result):
        if not self._current():
            return
        if isinstance(result, dict) and result.get('status') == 'cancelled':
            self._write('Download cancelled. No update was installed. A completed verified package, if already present, remains unchanged.')
            return
        result = result.get('download') if isinstance(result, dict) else result
        if isinstance(result, PausedDownload):
            self._write(f'Download {result.state}. {result.received} of {result.release.length} bytes are retained.'
                        '\n\nResume rechecks these bytes and current signed metadata. The fragment cannot be installed. '
                        'Cancel download discards only this retained fragment; closing this paused review keeps it for a later session.')
        else:
            self._write(
                'The exact signed package is prepared.\n\n' + release_text(result.release)
                + ('\n\n' + self.session.download_notice if self.session.download_notice else '')
                + '\n\nPortable installations: finish and close the program, then run the reviewed external maintenance command. '
                  'Standard installations can explicitly Apply here; older generations and the research backup are retained.')

    def _download(self, *, resume=False):
        if not self._current() or self.session.release is None or self._busy:
            return
        release = self.session.release

        def operation(cancelled):
            from ..progress import current_progress
            task = current_progress()

            def progress(received, total):
                if task is not None:
                    task.update(completed=received, total=total,
                                phase='Receiving reviewed update bytes',
                                message=f'{received} of {total} bytes received; full signed verification is pending.')
            try:
                method = self.session.resume_download if resume else self.session.download
                result = method(release, cancelled=cancelled, paused=self._pause.is_set, progress=progress)
                if isinstance(result, PausedDownload):
                    return {'status': result.state, 'download': result}
                return result
            except UpdateCancelled:
                # Metadata refresh may observe Cancel before the target fetcher
                # opens. Discard the same bound fragment through this worker too.
                return self.session.cancel_download(release)
        self._run('Resume exact reviewed signed update' if resume else 'Download exact reviewed signed update',
                  operation, self._download_result, downloading=True)

    def on_download(self, event=None):
        self._download()

    def on_resume_download(self, event=None):
        if self._current() and getattr(self.session, 'download_state', None) is not None:
            self._download(resume=True)

    def on_pause_download(self, event=None):
        if not self._current() or not self._busy or not self._downloading:
            return
        self._pause.set()
        self._write('Pause requested. Waiting for the owned transfer to stop and the retained bytes to be durably recorded.')
        self._capabilities()

    def on_cancel_download(self, event=None):
        if not self._current() or self.session.release is None:
            return
        if self._busy:
            if self._downloading:
                self._cancel.set()
                self._pause.clear()
                self._write('Cancellation requested. Waiting for the owned transfer to stop before discarding its fragment.')
                self._capabilities()
            return
        if getattr(self.session, 'download_state', None) is not None:
            release = self.session.release
            self._run('Cancel retained update download',
                      lambda _cancelled: self.session.cancel_download(release), self._download_result)

    def _confirm(self, recovery):
        if not self._current() or self._busy or self.session.package is None:
            return False
        if self.session.configuration.checked()['installation']['kind'] != 'standard':
            return False
        message = ('Recover the interrupted standard update using the same freshly authenticated package?'
                   if recovery else 'Apply this exact signed update through the current standard Windows installer?')
        message += ('\n\nThe operation first preserves a complete research backup. Old program generations remain. '
                    'After setup starts, it must finish or confirm cleanup before recovery can proceed. '
                    'This action does not change license permissions or create trust keys.')
        chooser = wx.MessageDialog(self, message, 'Confirm reviewed maintenance',
                                   wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            result = chooser.ShowModal()
            return self._current() and _live(chooser) and result == wx.ID_YES
        finally:
            if _live(chooser):
                chooser.Destroy()

    def _result(self, result):
        if not self._current():
            return
        if isinstance(result, dict):
            rows = ['Maintenance status: ' + str(result.get('status', 'unconfirmed'))]
            for key, label in (('program', 'Selected program'), ('previous', 'Retained previous generation'),
                               ('backup', 'Research backup'), ('warning', 'Completion notice')):
                if result.get(key):
                    rows.append(label + ': ' + str(result[key]))
            self._write('\n\n'.join(rows))
        else:
            self._write('Maintenance status: ' + str(result))

    def on_apply(self, event=None):
        if not self._confirm(False) or not self._current():
            return
        self._run('Apply explicitly reviewed signed standard update',
                  lambda cancelled: self.session.apply(accepted=True, cancelled=cancelled), self._result)

    def on_recover(self, event=None):
        if not self._confirm(True) or not self._current():
            return
        self._run('Recover explicitly reviewed standard update',
                  lambda cancelled: self.session.recover(accepted=True, cancelled=cancelled), self._result)

    def on_command(self, event=None):
        if not self._current() or self.session.release is None:
            return
        self._write('Use the separately reviewed maintenance interpreter. Finish and close the portable application first; '
                    'unclean owner receipts deliberately block activation.\n\nApply:\n'
                    + self.session.command() + '\n\nRecover:\n' + self.session.command(recovery=True)
                    + '\n\nThe command reacquires signed metadata and the exact reviewed package. It never provisions a missing anchor.')

    def dispose(self):
        if self._alive:
            self._alive = False
            self._busy = self._downloading = False
            self._cancel.set()
            self._token = None

    def Destroy(self):
        if self._destroy_requested or not self:
            return False
        self._destroy_requested = True
        self.dispose()
        return super().Destroy()

    def _destroyed(self, event):
        if event.GetId() == self._native_id:
            self._destroy_requested = True
            self.dispose()
        event.Skip()

    def on_close(self, event=None):
        if not self._alive:
            return
        self.dispose()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()


def show_maintenance(frame, session):
    if not _live(frame) or getattr(frame, 'library', None) is not session.app:
        return
    dialog = MaintenanceDialog(frame, session)
    try:
        dialog.ShowModal()
    finally:
        if _live(dialog):
            dialog.Destroy()


def choose_update_configuration(frame):
    if not _live(frame):
        return
    app = getattr(frame, 'library', None)
    if app is None:
        frame.report('Open the research library to review its update backup and maintenance context.')
        return
    picker = wx.FileDialog(frame, 'Choose the owner-provided update configuration',
                           wildcard='JSON configuration (*.json)|*.json',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        chosen = picker.ShowModal()
        if not _live(frame) or not _live(picker) or chosen != wx.ID_OK:
            return
        path = picker.GetPath()
    finally:
        if _live(picker):
            picker.Destroy()
    if not _live(frame) or getattr(frame, 'library', None) is not app:
        return
    digest = wx.TextEntryDialog(frame, 'Enter the configuration SHA256 received independently from the owner.',
                                'Verify owner update configuration')
    try:
        chosen = digest.ShowModal()
        if not _live(frame) or not _live(digest) or chosen != wx.ID_OK:
            return
        expected = digest.GetValue().strip()
    finally:
        if _live(digest):
            digest.Destroy()
    if not _live(frame) or getattr(frame, 'library', None) is not app:
        return
    def read():
        return MaintenanceSession(app, UpdateConfiguration.read(path, expected))
    frame.run_job('Read explicitly selected owner update configuration', read,
                  done=lambda session: show_maintenance(frame, session),
                  failed=lambda error: (frame.report('The owner update configuration could not be verified. '
                                                     'No check, download or activation was requested.')
                                        if _live(frame) and getattr(frame, 'library', None) is app else None))
