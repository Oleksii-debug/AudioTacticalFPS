"""Keyboard-first portable snapshots without changing the open data folder.

The modeless window owns one worker. Passwords are operation arguments, never
window/controller state, and exception details never cross the worker boundary.
Listed history is untrusted; only an independently entered transfer code can
authorize inspection or restoration through SyncService.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
import os
import re


SCOPE = ('All libraries in this data folder, including archived libraries. '
         'Save open changes before publishing. Only data supported by the current '
         'backup format is included; inspect its warnings before restoring.')


class InputError(ValueError):
    """A fixed, nonsecret message safe to present to the user."""


def _path(value, label):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise InputError(f'Enter the {label}.')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InputError(f'Choose an absolute local path for the {label}.')
    # Preserve a terminal symlink so the new-destination preflight can reject
    # it, including a dangling link. The service performs authoritative checks.
    return Path(os.path.abspath(path))


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value.strip()):
        raise InputError('Enter the complete 64-character transfer code obtained separately from the snapshot folder.')
    return value.strip().lower()


def _text(value, password=None):
    value = str(value)
    if password:
        value = value.replace(password, '[redacted]')
    return ''.join(char for char in value if char in '\n\t' or ord(char) >= 32)


def _redact(value, password):
    if isinstance(value, str):
        return _text(value, password)
    if isinstance(value, dict):
        return {key: _redact(item, password) for key, item in value.items()
                if key not in ('password', 'passphrase', 'secret', 'key')}
    if isinstance(value, (tuple, list)):
        return [_redact(item, password) for item in value]
    return value


def _failure(error):
    # Do not interpolate exception messages, paths, reprs or tracebacks: provider
    # and filesystem exceptions can contain a password supplied as an argument.
    if isinstance(error, FileExistsError):
        return 'The destination already exists. Choose a new folder; existing files were not approved for replacement.'
    if isinstance(error, FileNotFoundError):
        return 'A required folder or snapshot file is missing. Check the folder and refresh snapshot history.'
    if isinstance(error, PermissionError):
        return 'The operation could not access a required file. Check folder permissions and available storage.'
    if type(error).__name__ == 'CryptoUnavailable':
        return 'Password encryption is unavailable in this installation. No unencrypted fallback was requested.'
    if type(error).__name__ == 'SyncConflict':
        return ('The snapshot histories differ. Refresh and inspect the portable history; restore its '
                'verified snapshot into a new folder or publish to a separate empty snapshot folder.')
    return ('The operation could not be confirmed. Check the folder, transfer code and password. '
            'Check snapshot history or the restore destination before retrying: an output may already exist.')


@dataclass(frozen=True)
class _Outcome:
    value: object = None
    error: str | None = None
    cancelled: bool = False
    recovery: dict | None = None


def _recovery(error, password):
    from ..sync.service import SyncPublishedError, SyncRestoredError
    if isinstance(error, SyncPublishedError):
        return dict(kind='published', receipt=_redact(error.receipt, password))
    if isinstance(error, SyncRestoredError):
        return dict(kind='restored', destination=_text(error.destination, password),
                    token=_text(error.token, password))
    return None


class _Secret:
    """Release the only queued credential reference even when startup fails."""
    def __init__(self, value):
        self.value = value

    def take(self):
        value, self.value = self.value, None
        return value


class PortableSyncController:
    def __init__(self, frame, view, *, service=None, executor=None, dispatch=None):
        self.frame, self.view = frame, view
        self.app = frame.library
        self.identity = self._identity()
        if service is None:
            from ..sync.service import SyncService
            service = SyncService(self.app)
        self.service = service
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='portable-snapshot')
        if dispatch is None:
            import wx
            dispatch = wx.CallAfter
        self.dispatch = dispatch
        self.cancel_event = Event()
        self.alive, self.running, self.closing = True, False, False
        self.epoch = 0
        self.history, self.history_folder, self.expected_head = (), None, None
        self.inspection, self.inspected_input = None, None

    def _identity(self):
        app = self.frame.library
        return tuple(getattr(app, name, None) for name in ('root', 'database', 'assets_dir'))

    def _same_scope(self):
        return (self.alive and self.frame.library is self.app
                and self._identity() == self.identity and not getattr(self.frame, '_closing', False))

    def _cancelled(self):
        return self.cancel_event.is_set() or not self._same_scope()

    def _ready(self):
        if not self._same_scope():
            raise InputError('The open data folder changed. Close this window and open portable snapshots again.')
        if self.running or self.closing:
            raise InputError('Wait for the snapshot operation to finish, or cancel it.')

    def _error(self, error):
        if self.alive:
            self.view.report(str(error) if isinstance(error, InputError) else _failure(error), focus=True)

    def _clear_inspection(self):
        self.inspection, self.inspected_input = None, None
        if self.alive:
            self.view.show_preview('', verified=False)

    def inputs_changed(self, *, folder=False):
        if not self.alive:
            return
        self.epoch += 1
        self._clear_inspection()
        if folder:
            self.history, self.history_folder, self.expected_head = (), None, None
            self.view.show_history(())
        if self.running:
            self.cancel_event.set()
            self.view.report('Inputs changed. Cancelling the previous operation; verify the current inputs again.')

    def _status(self, epoch, message):
        if self._same_scope() and self.running and self.epoch == epoch and not self.cancel_event.is_set():
            self.view.report(message)

    def _start(self, kind, operation, complete, password=None):
        self._ready()
        self._clear_inspection()
        self.running = True
        self.cancel_event.clear()
        self.epoch += 1
        epoch = self.epoch
        secret = _Secret(password)
        password = None
        self.view.set_busy(True)
        self.view.report({'list': 'Loading unverified snapshot history…',
                          'publish': 'Publishing a snapshot of the current data folder…',
                          'inspect': 'Verifying the independently entered transfer code…',
                          'restore': 'Restoring into the new folder…'}[kind], focus=True)

        def worker():
            credential = secret.take()
            try:
                if self._cancelled() or self.epoch != epoch:
                    return _Outcome(cancelled=True)

                def status(message):
                    safe = ' '.join(_text(message, credential).split())[:1000]
                    self.dispatch(self._status, epoch, safe)

                value = operation(credential, status)
                return _Outcome(value=_redact(value, credential))
            except Exception as error:
                return _Outcome(error=_failure(error), cancelled=(
                    self._cancelled() or isinstance(error, InterruptedError)),
                    recovery=_recovery(error, credential))
            finally:
                credential = None
                secret.take()

        def returned(future):
            try:
                outcome = future.result()
            except Exception:
                outcome = _Outcome(error='The snapshot worker stopped unexpectedly. Check history before retrying.')
            try:
                self.dispatch(self._complete, epoch, kind, outcome, complete)
            except RuntimeError:
                # A terminating wx application may no longer accept CallAfter.
                pass

        try:
            self.executor.submit(worker).add_done_callback(returned)
        except Exception:
            secret.take()
            self._complete(epoch, kind, _Outcome(error='The snapshot worker could not start.'), complete)

    def _complete(self, epoch, kind, outcome, complete):
        self.running = False
        if not self.alive:
            return
        if self.closing:
            self.dispose()
            self.view.close_window()
            return
        self.view.set_busy(False)
        if not self._same_scope() or self.epoch != epoch:
            self.view.report('The data folder or inputs changed. Results were not displayed; check snapshot history before retrying.', focus=True)
            return
        if outcome.recovery is not None:
            self.history_folder = None
            try:
                recovery = outcome.recovery
                if recovery['kind'] == 'published' and kind == 'publish':
                    receipt = recovery['receipt']
                    token = _token(receipt['token'])
                    if _token(receipt['id']) != token or receipt.get('head') != token:
                        raise ValueError('recovery receipt')
                    text = ('Snapshot files were published, but local portable history was not saved.\n'
                            f'Transfer code: {token}\n\nKeep this code separately. Verify this snapshot and '
                            'restore it into a new folder before publishing further changes.')
                elif recovery['kind'] == 'restored' and kind == 'restore':
                    token = _token(recovery['token'])
                    destination = _path(recovery['destination'], 'restored folder')
                    text = ('A restored library exists, but its final completion check failed.\n'
                            f'Destination: {destination}\nTransfer code: {token}\n\n'
                            'The open data folder has not changed. Keep the code and inspect the restored folder '
                            'before attempting another transfer.')
                else:
                    raise ValueError('recovery operation')
                self.view.show_preview(text, verified=False)
                self.view.report('Output exists. Review the recovery receipt before retrying.', focus=True)
            except Exception:
                self.view.report('An output exists, but its recovery receipt could not be displayed. Check the snapshot folder or destination before retrying.', focus=True)
            return
        if outcome.cancelled or (self.cancel_event.is_set() and kind in ('list', 'inspect')):
            self.view.report('Operation cancelled. No verification preview was accepted; refresh history before retrying.', focus=True)
            if kind == 'publish':
                self.history_folder = None
            return
        if outcome.error:
            if kind == 'publish':
                self.history_folder = None
            self.view.report(outcome.error, focus=True)
            return
        try:
            complete(outcome.value)
            if self.cancel_event.is_set():
                self.view.report('The operation completed before cancellation took effect. Review its receipt below.', focus=True)
        except Exception:
            self._clear_inspection()
            self.history_folder = None
            self.view.report('The operation returned an unsupported receipt. Verify the output before retrying.', focus=True)

    def refresh(self, folder):
        try:
            self._ready()
            folder = _path(folder, 'snapshot folder')
            self.history, self.history_folder, self.expected_head = (), None, None
            self.view.show_history(())

            def complete(rows):
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError('history shape')
                if rows and (any(type(row.get('head')) is not bool for row in rows)
                             or sum(row['head'] for row in rows) > 1):
                    raise ValueError('history head')
                ids = [_token(row['id']) for row in rows]
                if len(ids) != len(set(ids)):
                    raise ValueError('duplicate history')
                self.history = tuple(dict(row) for row in rows)
                self.history_folder = folder
                self.expected_head = next((row['id'] for row in rows if row['head']), None)
                self.view.show_history(tuple(history_label(row) for row in rows))
                self.view.report(f'{len(rows)} snapshot(s) listed. History is unverified; enter a separately obtained transfer code to verify or restore.')

            self._start('list', lambda password, status: self.service.list(folder), complete)
        except Exception as error:
            self._error(error)

    def publish(self, folder, label, *, password=None, confirmed=False):
        try:
            self._ready()
            folder = _path(folder, 'snapshot folder')
            if not confirmed:
                raise InputError('Confirm the complete data-folder backup scope before publishing.')
            if self.history_folder != folder:
                raise InputError('Refresh this snapshot folder before publishing, so history conflicts can be checked.')
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 200:
                raise InputError('Enter a snapshot label of 1 to 200 characters.')
            label, head = label.strip(), self.expected_head

            def complete(receipt):
                snapshot = _token(receipt['id'])
                token = _token(receipt['token'])
                if receipt.get('head') != snapshot or type(receipt.get('encrypted')) is not bool:
                    raise ValueError('publication receipt')
                self.expected_head = snapshot
                self.view.show_preview('Snapshot published. Keep this transfer code separately from the snapshot folder.\n'
                    f'Transfer code: {token}\nSnapshot: {snapshot}\n'
                    f'Encrypted: {"Yes" if receipt["encrypted"] else "No"}\n'
                    f'Scope: {_text(receipt.get("scope", SCOPE))}\n'
                    f'{_text(receipt.get("message", ""))}', verified=False)
                self.view.report('Snapshot published. Its transfer code is displayed for manual copying; nothing was placed on the clipboard.', focus=True)

            self._start('publish', lambda secret, status: self.service.publish(folder, label, head,
                password=secret, cancelled=self._cancelled, on_status=status), complete, password)
        except Exception as error:
            self._error(error)
        finally:
            password = None

    def verify(self, folder, token, *, password=None):
        try:
            self._ready()
            folder, token = _path(folder, 'snapshot folder'), _token(token)

            def complete(receipt):
                if (not isinstance(receipt, dict) or receipt.get('verified') is not True
                        or receipt.get('restore_mode') != 'new-folder-only'
                        or _token(receipt['token']) != token or _token(receipt['id']) != token
                        or type(receipt.get('encrypted')) is not bool):
                    raise ValueError('inspection receipt')
                self.inspection, self.inspected_input = dict(receipt), (folder, token)
                self.view.show_preview(inspection_text(receipt), verified=True)
                self.view.report('Snapshot verified against the entered transfer code. Review its scope and warnings, then choose a new restore folder.', focus=True)

            self._start('inspect', lambda secret, status: self.service.inspect(folder, token,
                password=secret, cancelled=self._cancelled, on_status=status), complete, password)
        except Exception as error:
            self._error(error)
        finally:
            password = None

    def restore_preview(self, folder, token, destination):
        self._ready()
        folder, token = _path(folder, 'snapshot folder'), _token(token)
        if self.inspection is None or self.inspected_input != (folder, token):
            raise InputError('Verify this snapshot folder and transfer code before restoring.')
        destination = _path(destination, 'new restore folder')
        if destination.exists() or destination.is_symlink():
            raise InputError('The restore folder already exists. Choose a new folder; existing content will not be replaced.')
        if not destination.parent.is_dir():
            raise InputError('The parent of the new restore folder must already exist.')
        resolved = destination.resolve(strict=False)
        if any(resolved.is_relative_to(root.resolve()) for root in (folder, Path(self.app.root))):
            raise InputError('Choose a restore folder outside the open data folder and snapshot folder.')
        return (inspection_text(self.inspection) + '\n\nRestore destination: ' + str(destination)
                + '\nConflict check: this folder does not exist. The service checks again before publishing.'
                + '\nThe current library data folder and open notebook will stay open.')

    def restore(self, folder, token, destination, *, password=None, confirmed=False):
        try:
            self.restore_preview(folder, token, destination)
            if not confirmed:
                raise InputError('Confirm the verified snapshot and new destination before restoring.')
            folder, token = _path(folder, 'snapshot folder'), _token(token)
            destination = _path(destination, 'new restore folder')

            def complete(receipt):
                if (receipt.get('verified') is not True or _token(receipt['token']) != token
                        or _path(receipt['destination'], 'restored folder') != destination):
                    raise ValueError('restore receipt')
                self.view.show_preview('Restore completed and verified.\n'
                    f'Destination: {destination}\nTransfer code: {token}\n'
                    'The open data folder has not changed. Open the restored folder explicitly when ready.\n'
                    + _text(receipt.get('message', '')), verified=False)
                self.view.report('Restore completed into the new folder. The current library and notebook remain open.', focus=True)

            self._start('restore', lambda secret, status: self.service.restore(folder, token, destination,
                password=secret, cancelled=self._cancelled, on_status=status), complete, password)
        except Exception as error:
            self._error(error)
        finally:
            password = None

    def cancel(self):
        if self.alive and self.running:
            self.cancel_event.set()
            self.view.report('Cancellation requested. Waiting for the current safe stopping point…', focus=True)

    def request_close(self):
        if self.running:
            self.closing = True
            self.cancel()
            return False
        return True

    def dispose(self):
        if not self.alive:
            return
        self.alive = False
        self.cancel_event.set()
        self.inspection, self.inspected_input = None, None
        self.executor.shutdown(wait=False, cancel_futures=False)


def history_label(row):
    encryption = {True: 'encrypted', False: 'unencrypted'}.get(row.get('encrypted'), 'encryption unknown')
    return ' | '.join(' '.join(_text(value).split()) for value in (
        row.get('label') or 'Snapshot', row.get('created_at', ''), row['id'], encryption,
        'current head' if row.get('head') is True else 'not the current head', 'unverified history'))


def inspection_text(receipt):
    libraries = '\n'.join('• ' + _text(name) for name in receipt.get('libraries', ())) or '(none listed)'
    warnings = '\n'.join('• ' + _text(item) for item in receipt.get('warnings', ())) or 'None reported by this backup format.'
    return (f'Verified snapshot: {receipt["id"]}\nScope: {_text(receipt.get("scope", SCOPE))}\n'
            f'Encrypted: {"Yes" if receipt["encrypted"] else "No"}\n'
            f'Schema version: {receipt.get("schema_version", "Unknown")}\n'
            f'Files: {receipt.get("files", "Unknown")}; bytes: {receipt.get("bytes", "Unknown")}\n'
            f'Backup checksum: {receipt.get("backup_checksum", "Unknown")}\n\n'
            f'Libraries:\n{libraries}\n\nBackup warnings and exclusions:\n{warnings}\n\n'
            'Restore mode: new folder only. Existing data is never approved for replacement.')


class NativeSyncDialogs:
    def password(self, parent, *, publish=False, required=False):
        import wx
        dialog = wx.Dialog(parent, title='Snapshot password', size=(560, 300))
        fields = []
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            instruction = ('Enter a password of at least 12 characters and confirm it. Keep it separately; it cannot be recovered.'
                           if publish else 'Enter the snapshot password. It is used only for this operation.' +
                           ('' if required else ' Leave blank for an unencrypted snapshot.'))
            label = wx.StaticText(dialog, label=instruction)
            label.Wrap(510)
            layout.Add(label, 0, wx.EXPAND | wx.ALL, 10)
            for caption in (('Password', 'Confirm password') if publish else ('Password',)):
                layout.Add(wx.StaticText(dialog, label=caption), 0, wx.LEFT | wx.TOP, 10)
                field = wx.TextCtrl(dialog, style=wx.TE_PASSWORD)
                field.SetName(caption + ' for this operation only')
                layout.Add(field, 0, wx.EXPAND | wx.ALL, 10)
                fields.append(field)
            layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 10)
            dialog.SetSizer(layout)
            dialog.SetAffirmativeId(wx.ID_OK)
            dialog.SetEscapeId(wx.ID_CANCEL)
            fields[0].SetFocus()
            while dialog.ShowModal() == wx.ID_OK:
                password = fields[0].GetValue()
                if publish and (len(password) < 12 or len(password.encode('utf-8')) > 1024
                                or password != fields[1].GetValue()):
                    wx.MessageBox('Use 12 or more characters, at most 1024 UTF-8 bytes, and matching confirmation.',
                                  'Check the password', wx.OK | wx.ICON_INFORMATION, dialog)
                    password = None
                    fields[0].SetFocus()
                    continue
                if required and not password:
                    fields[0].SetFocus()
                    continue
                return True, password or None
            return False, None
        finally:
            for field in fields:
                try:
                    field.ChangeValue('')
                except RuntimeError:
                    pass
            try:
                dialog.Destroy()
            except RuntimeError:
                pass

    def confirm(self, parent, title, message):
        import wx
        dialog = wx.MessageDialog(parent, message, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    def folder(self, parent, title):
        import wx
        dialog = wx.DirDialog(parent, title)
        try:
            return dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()


class NativePortableSync:
    def __init__(self, frame, *, on_closed=lambda window: None, dialogs=None, service=None):
        import wx
        self.frame, self.dialogs, self.on_closed = frame, dialogs or NativeSyncDialogs(), on_closed
        self.return_focus = wx.Window.FindFocus()
        self.window = wx.Dialog(frame, title='Portable library snapshots', size=(880, 800),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._window_alive, self._notified = True, False
        self.window.SetMinSize((660, 650))
        layout = wx.BoxSizer(wx.VERTICAL)
        scope = wx.TextCtrl(self.window, value=f'Data folder: {frame.library.root}\n{SCOPE}',
                            style=wx.TE_MULTILINE | wx.TE_READONLY)
        scope.SetName('Complete data-folder backup scope')
        layout.Add(scope, 0, wx.EXPAND | wx.ALL, 8)
        self.folder = self._field(layout, 'Snapshot folder (local portable history)')
        self.browse = wx.Button(self.window, label='Choose snapshot folder…')
        self.browse.Bind(wx.EVT_BUTTON, self._browse)
        layout.Add(self.browse, 0, wx.LEFT | wx.BOTTOM, 8)
        layout.Add(wx.StaticText(self.window, label='Unverified snapshot history; the entered transfer code chooses the snapshot.'),
                   0, wx.LEFT | wx.RIGHT, 8)
        self.history = wx.ListBox(self.window, size=(-1, 85))
        self.history.SetName('Unverified snapshot history')
        layout.Add(self.history, 1, wx.EXPAND | wx.ALL, 8)
        self.label = self._field(layout, 'Label for a new snapshot')
        self.encrypt = wx.CheckBox(self.window, label='Encrypt a new snapshot with an operation-only password')
        layout.Add(self.encrypt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.token = self._field(layout, 'Transfer code obtained separately (64 hexadecimal characters)')
        self.destination = self._field(layout, 'New restore folder (must not exist)')
        self.parent_button = wx.Button(self.window, label='Choose restore parent folder…')
        self.parent_button.Bind(wx.EVT_BUTTON, self._destination_parent)
        layout.Add(self.parent_button, 0, wx.LEFT | wx.BOTTOM, 8)
        buttons = wx.GridSizer(cols=3, vgap=6, hgap=6)
        self.refresh_button = self._button(buttons, 'Refresh history', self._refresh)
        self.publish_button = self._button(buttons, 'Publish snapshot…', self._publish)
        self.verify_button = self._button(buttons, 'Verify code…', self._verify)
        self.restore_button = self._button(buttons, 'Restore to new folder…', self._restore)
        self.cancel_button = self._button(buttons, 'Cancel operation', lambda event: self.controller.cancel())
        self.close_button = self._button(buttons, 'Close', self._close, wx.ID_CLOSE)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self.window, label='Verified preview or transfer receipt'), 0, wx.LEFT | wx.RIGHT, 8)
        self.preview = wx.TextCtrl(self.window, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.preview.SetName('Verified snapshot preview and transfer receipt')
        layout.Add(self.preview, 2, wx.EXPAND | wx.ALL, 8)
        self.status = wx.TextCtrl(self.window, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 58))
        self.status.SetName('Portable snapshot operation status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.window.SetSizer(layout)
        self.window.SetEscapeId(wx.ID_CLOSE)
        self.window.Bind(wx.EVT_CLOSE, self._close)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.controller = None
        try:
            self.controller = PortableSyncController(frame, self, service=service)
        except Exception:
            self._window_alive = False
            self.window.Destroy()
            raise
        self.folder.Bind(wx.EVT_TEXT, lambda event: self.controller.inputs_changed(folder=True))
        self.token.Bind(wx.EVT_TEXT, lambda event: self.controller.inputs_changed())
        self.set_busy(False)
        self.report('Choose a snapshot folder and refresh its history. No network service is used.')

    def _field(self, layout, label):
        import wx
        layout.Add(wx.StaticText(self.window, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        field = wx.TextCtrl(self.window)
        field.SetName(label)
        layout.Add(field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        return field

    def _button(self, layout, label, handler, identifier=-1):
        import wx
        button = wx.Button(self.window, id=identifier, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        layout.Add(button, 0, wx.EXPAND)
        return button

    def show(self):
        if self._window_alive:
            self.window.Show()
            self.window.Raise()
            self.folder.SetFocus()

    def _browse(self, event=None):
        path = self.dialogs.folder(self.window, 'Choose a local snapshot folder')
        if self._window_alive and path is not None:
            self.folder.SetValue(path)
            self.folder.SetFocus()

    def _destination_parent(self, event=None):
        path = self.dialogs.folder(self.window, 'Choose the parent; type a new folder name afterwards')
        if self._window_alive and path is not None:
            self.destination.SetValue(str(Path(path) / 'Restored library'))
            self.destination.SetFocus()

    def _refresh(self, event=None):
        if self._window_alive:
            self.controller.refresh(self.folder.GetValue())

    def _same_inputs(self, expected):
        if not self._window_alive:
            return False
        self.controller._ready()
        if any(control.GetValue() != value for control, value in expected):
            raise InputError('Snapshot options changed while the dialog was open. Review them again.')
        return True

    def _publish(self, event=None):
        if not self._window_alive:
            return
        password = None
        try:
            folder, label = self.folder.GetValue(), self.label.GetValue()
            encrypted = self.encrypt.GetValue()
            expected = ((self.folder, folder), (self.label, label), (self.encrypt, encrypted))
            self.controller._ready()
            if self.controller.history_folder != _path(folder, 'snapshot folder'):
                raise InputError('Refresh this snapshot folder before publishing, so history conflicts can be checked.')
            confirmed = self.dialogs.confirm(self.window, 'Publish all saved libraries in this data folder?',
                f'Source: {self.controller.app.root}\nSnapshot folder: {folder}\n\n{SCOPE}')
            if not confirmed or not self._same_inputs(expected):
                return
            accepted, password = (self.dialogs.password(self.window, publish=True)
                                  if encrypted else (True, None))
            if accepted and self._same_inputs(expected):
                self.controller.publish(folder, label, password=password, confirmed=True)
        except Exception as error:
            self.controller._error(error)
        finally:
            password = None

    def _verify(self, event=None):
        if not self._window_alive:
            return
        password = None
        try:
            folder, token = self.folder.GetValue(), self.token.GetValue()
            self.controller._ready()
            _path(folder, 'snapshot folder')
            _token(token)
            accepted, password = self.dialogs.password(self.window)
            if accepted and self._same_inputs(((self.folder, folder), (self.token, token))):
                self.controller.verify(folder, token, password=password)
        except Exception as error:
            self.controller._error(error)
        finally:
            password = None

    def _restore(self, event=None):
        if not self._window_alive:
            return
        folder, token, destination = (field.GetValue() for field in (self.folder, self.token, self.destination))
        try:
            preview = self.controller.restore_preview(folder, token, destination)
            encrypted = self.controller.inspection['encrypted']
            if not self.dialogs.confirm(self.window, 'Restore the verified snapshot into a new folder?', preview):
                return
            expected = ((self.folder, folder), (self.token, token), (self.destination, destination))
            if not self._same_inputs(expected):
                return
            accepted, password = (self.dialogs.password(self.window, required=True)
                                  if encrypted else (True, None))
            try:
                if accepted and self._same_inputs(expected):
                    self.controller.restore(folder, token, destination, password=password, confirmed=True)
            finally:
                password = None
        except Exception as error:
            self.controller._error(error)

    def set_busy(self, busy):
        if not self._window_alive:
            return
        for control in (self.folder, self.browse, self.history, self.label, self.encrypt, self.token,
                        self.destination, self.parent_button, self.refresh_button, self.publish_button,
                        self.verify_button):
            control.Enable(not busy)
        self.restore_button.Enable(not busy and self.controller.inspection is not None)
        self.cancel_button.Enable(busy)

    def show_history(self, rows):
        if self._window_alive:
            self.history.Set(list(rows))

    def show_preview(self, text, *, verified):
        if self._window_alive:
            self.preview.ChangeValue(text)
            self.restore_button.Enable(verified and not self.controller.running)

    def report(self, text, *, focus=False):
        if self._window_alive:
            self.status.ChangeValue(text)
            if focus:
                self.status.SetFocus()

    def _close(self, event=None):
        if not self._window_alive:
            return
        if self.controller.request_close():
            self.close_window()
        elif event is not None and hasattr(event, 'CanVeto'):
            if event.CanVeto():
                event.Veto()
            else:
                self.close_window()

    def close_window(self):
        self.controller.dispose()
        if self._window_alive:
            self._window_alive = False
            self.window.Destroy()
        self._notify_closed()

    def _notify_closed(self):
        if not self._notified:
            self._notified = True
            self.on_closed(self)
            frame = getattr(self, 'frame', None)
            if frame is not None and not getattr(frame, '_closing', False):
                try:
                    target = getattr(self, 'return_focus', None)
                    if not target or target.IsBeingDeleted():
                        target = getattr(frame, 'choice', None)
                    if target and not target.IsBeingDeleted():
                        target.SetFocus()
                except RuntimeError:
                    # The parent or old notebook may have been destroyed too.
                    pass

    def _destroyed(self, event):
        if event.GetEventObject() is self.window:
            self._window_alive = False
            if self.controller is not None:
                self.controller.dispose()
            self._notify_closed()
        event.Skip()


class PortableSyncActions:
    def __init__(self, frame):
        self.frame, self.window = frame, None

    def open(self, event=None):
        if getattr(self.frame, '_closing', False):
            return
        if self.window is not None and self.window._window_alive:
            if self.window.controller._same_scope():
                self.window.show()
                return
            if not self.window.controller.request_close():
                self.frame.report('The previous snapshot operation is stopping. Open portable snapshots again when it finishes.')
                return
            self.window.close_window()
        try:
            self.window = NativePortableSync(self.frame, on_closed=self._closed)
            self.window.show()
        except Exception:
            self.frame.report('The portable snapshot window could not open. Check this installation before retrying.')

    def _closed(self, window):
        if self.window is window:
            self.window = None


def install_portable_sync_actions(frame):
    import wx
    if getattr(frame, 'portable_sync_actions', None) is not None:
        return frame.portable_sync_actions
    for index in range(frame.menu_bar.GetMenuCount()):
        if frame.menu_bar.GetMenuLabel(index).replace('&', '') == 'Library':
            actions = PortableSyncActions(frame)
            menu = frame.menu_bar.GetMenu(index)
            item = menu.Append(wx.ID_ANY, 'Portable snapshots and transfer…')
            frame.Bind(wx.EVT_MENU, actions.open, item)
            frame.portable_sync_actions = actions
            return actions
    raise ValueError('The Library menu is required for portable snapshots.')
