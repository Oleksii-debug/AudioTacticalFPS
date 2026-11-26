"""Native desktop entry point; wx is loaded only when the GUI is requested.

Long imports run on one worker thread. SQLite/source work belongs to Library;
all native controls are read or changed on the UI thread through CallAfter.
"""
from pathlib import Path
import os
import stat
from tempfile import NamedTemporaryFile
from threading import Event, Thread


def write_export(path, content, *, private_root):
    """Publish a requested UTF-8 export atomically outside application storage."""
    target = Path(path).absolute()
    if not isinstance(content, str):
        raise ValueError('text export required')
    if target.resolve().is_relative_to(Path(private_root).resolve()):
        raise ValueError('choose an export destination outside the private library folder')
    if target.exists() or target.is_symlink():
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError('export destination must be a regular file')
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                prefix='.carr-export-', dir=target.parent,
                                delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def import_files(library, workspace, paths, *, cancelled, progress, title=None, authors=()):
    """Capture one explicit workspace for the entire batch; preserve each job."""
    results = []
    paths = tuple(paths)
    for number, path in enumerate(paths, 1):
        if cancelled():
            break
        progress(f'Importing file {number} of {len(paths)}. Cancel remains available.')
        try:
            result = library.import_file(workspace, path, title=title, authors=authors,
                                         cancelled=cancelled)
        except Exception as exc:
            result = {'state': 'failed', 'error': str(exc)}
        results.append(result)
    return results


def import_summary(results, *, total=None):
    complete = sum(result.get('state') == 'complete' for result in results)
    verified = sum(result.get('state') == 'complete' and
                   result.get('citation_status') == 'byte_verified' for result in results)
    total = len(results) if total is None else total
    message = f'Imported {complete} of {total} files. {verified} ready for verified text citation.'
    if complete > verified:
        message += f' {complete - verified} require source review before citation.'
    if total > complete:
        message += f' {total - complete} unfinished. Use Resume interrupted import or import the remaining files.'
    errors = [result.get('error') for result in results if result.get('error')]
    if errors:
        message += ' First error: ' + errors[0]
    return message


class DesktopController:
    def __init__(self, wx, frame, library):
        self.wx, self.frame, self.library = wx, frame, library
        self.worker = None
        self.cancelled = Event()
        self.cancellable = True
        self.closed = False
        self.actions = []
        self.cancel_button = wx.Button(frame.host, label='Cancel current operation')
        self.cancel_button.Enable(False)
        self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel)
        frame.layout.Add(self.cancel_button)
        bar = wx.MenuBar()
        for label, items in [
            ('&Library', [('&New library...\tCtrl+N', self.create_workspace),
                          ('&Rename or describe library...', self.edit_workspace),
                          ('&Archive selected library...', self.archive_workspace),
                          ('Back up &all libraries...', self.backup_all),
                          ('&Restore backup to a new folder...', self.restore_backup)]),
            ('&Import', [('&Import TXT or PDF files...\tCtrl+I', self.import_local),
                         ('&Enter original text...', self.enter_text),
                         ('&Resume interrupted import...', self.resume_import)]),
            ('&Research', [('&Export selected topic...', self.export_topic)]),
        ]:
            menu = wx.Menu()
            for title, handler in items:
                item = menu.Append(wx.ID_ANY, title)
                frame.Bind(wx.EVT_MENU, handler, item)
                self.actions.append(item)
            bar.Append(menu, label)
        frame.SetMenuBar(bar)
        frame.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        frame.host.Layout()

    def _destroyed(self, event):
        if event.GetEventObject() is self.frame:
            self.closed = True
            self.cancelled.set()
        event.Skip()

    def _status(self, message, *, focus=True):
        if not self.closed:
            self.frame.status.SetValue(message)
            if focus:
                self.frame.status.SetFocus()

    def _scope(self, *, topic=False):
        if self.frame.active is None or (topic and self.frame.active[1] is None):
            raise ValueError('open a research topic first' if topic else 'open a library first')
        return self.frame.active

    def _form(self, title, fields):
        wx = self.wx
        dialog = wx.Dialog(self.frame, title=title, size=(620, 520))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            controls = {}
            for key, label, value, multiline in fields:
                layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 5)
                control = wx.TextCtrl(dialog, value=value,
                                      style=wx.TE_MULTILINE if multiline else 0)
                control.SetName(label)
                controls[key] = control
                layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.ALL, 5)
            layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 8)
            dialog.SetSizer(layout)
            next(iter(controls.values())).SetFocus()
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return {key: control.GetValue() for key, control in controls.items()}
        finally:
            dialog.Destroy()

    def _set_busy(self, value):
        self.frame.busy = value
        self.frame.choice.Enable(not value)
        if self.frame.book is not None:
            self.frame.book.Enable(not value)
        for item in self.actions:
            item.Enable(not value)
        self.cancel_button.Enable(value and self.cancellable)

    def _progress(self, message):
        self._status(message, focus=False)

    def start_job(self, operation, completed, *, refresh=True, cancellable=True):
        if self.worker is not None or self.frame.busy:
            self._status('An operation is already running. Wait or choose Cancel.')
            return False
        self.cancelled.clear()
        self.cancellable = cancellable
        self._set_busy(True)
        self._status('Working. Cancel current operation is available.' if cancellable else
                     'Working. This operation must finish before the window can close.')

        def progress(message):
            if not self.closed:
                self.wx.CallAfter(self._progress, message)

        def run():
            try:
                result, error = operation(self.cancelled.is_set, progress), None
            except Exception as exc:
                result, error = None, exc
            if not self.closed:
                self.wx.CallAfter(self.finish_job, result, error, completed, refresh)

        self.worker = Thread(target=run, name='carr-library-operation', daemon=False)
        try:
            self.worker.start()
        except Exception:
            self.worker = None
            self._set_busy(False)
            raise
        return True

    def finish_job(self, result, error, completed, refresh):
        self.worker = None
        if self.closed:
            return
        self._set_busy(False)
        if refresh:
            self.frame.refresh_scopes()
            if self.frame.active is not None and self.frame.book is not None:
                # Refresh source views without discarding an unsaved research note.
                self.frame.book.GetPage(0).set_workspace(self.frame.active[0])
                self.frame.book.GetPage(1).refresh()
                self.frame.book.GetPage(2).refresh()
        if error is not None:
            self._status('Operation failed: ' + str(error))
        else:
            try:
                self._status(completed(result))
            except Exception as exc:
                self._status('Operation finished; result unavailable: ' + str(exc))

    def cancel(self, event=None):
        if self.worker is not None:
            if not self.cancellable:
                self._status('This operation is completing safely and cannot be interrupted.')
                return
            self.cancelled.set()
            self._status('Cancellation requested. Waiting for the current safe checkpoint.')

    def on_close(self, event):
        if self.worker is None:
            return False
        self.cancel()
        if event.CanVeto():
            event.Veto()
            self._status('Cancelling the current operation. Close the window again after it stops.'
                         if self.cancellable else
                         'The operation is still running. Close the window again after it finishes.')
        else:
            self.closed = True
            event.Skip()
        return True

    def create_workspace(self, event=None):
        values = self._form('New research library', [
            ('name', 'Library name', '', False), ('description', 'Description', '', True)])
        if values is None:
            return
        try:
            workspace = self.library.create_workspace(**values)
            self.frame.refresh_scopes()
            self.frame.choice.SetSelection(self.frame.scopes.index((workspace.id, None)))
            self.frame.open_selected()
        except Exception as exc:
            self._status('Library not created: ' + str(exc))

    def edit_workspace(self, event=None):
        try:
            workspace, _ = self._scope()
            current = next(row for row in self.library.workspaces() if row.id == workspace)
            values = self._form('Library name and description', [
                ('name', 'Library name', current.name, False),
                ('description', 'Description', current.description, True)])
            if values is None:
                return
            self.library.update_workspace(workspace, **values)
            self.frame.refresh_scopes()
            self._status('Library details saved.')
        except Exception as exc:
            self._status('Library not updated: ' + str(exc))

    def archive_workspace(self, event=None):
        try:
            workspace, _ = self._scope()
            wx = self.wx
            if wx.MessageBox('Archive this library? Its sources and evidence will be retained.',
                             'Archive library', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                             self.frame) != wx.YES:
                return
            if not self.frame._confirm():
                self.frame._focus_evidence()
                return
            if self.frame.active is None or self.frame.active[0] != workspace:
                raise ValueError('selected library changed')
            self.library.archive_workspace(workspace)
            self.frame.refresh_scopes()
            self._status('Library archived. Sources and evidence retained.')
        except Exception as exc:
            self._status('Library not archived: ' + str(exc))

    def import_local(self, event=None):
        try:
            workspace, _ = self._scope()
        except ValueError as exc:
            self._status(str(exc))
            return
        wx = self.wx
        dialog = wx.FileDialog(self.frame, message='Import local text or PDF files',
            wildcard='Text and PDF files (*.txt;*.pdf)|*.txt;*.pdf',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            paths = tuple(dialog.GetPaths())
        finally:
            dialog.Destroy()
        if not paths:
            return
        title, authors = None, ()
        if len(paths) == 1:
            metadata = self._form('Source details', [
                ('title', 'Source title (optional; enter only if known)', '', False),
                ('author', 'Author (optional; enter only if known)', '', False)])
            if metadata is None:
                return
            title = metadata['title'] if metadata['title'].strip() else None
            authors = (metadata['author'],) if metadata['author'].strip() else ()
        self.start_job(lambda cancelled, progress: import_files(
            self.library, workspace, paths, cancelled=cancelled, progress=progress,
            title=title, authors=authors),
            lambda rows: import_summary(rows, total=len(paths)))

    def enter_text(self, event=None):
        try:
            workspace, _ = self._scope()
        except ValueError as exc:
            self._status(str(exc))
            return
        values = self._form('Enter original source text', [
            ('title', 'Source title', '', False), ('author', 'Author (optional)', '', False),
            ('text', 'Original source text', '', True)])
        if values is None:
            return
        authors = (values['author'],) if values['author'].strip() else ()
        self.start_job(lambda cancelled, progress: self.library.import_text(
            workspace, values['text'], title=values['title'], authors=authors, cancelled=cancelled),
            lambda result: import_summary([result]))

    def resume_import(self, event=None):
        try:
            workspace, _ = self._scope()
            jobs = tuple(job for job in self.library.import_jobs(workspace) if job['state'] != 'complete')
            if not jobs:
                self._status('No interrupted imports in this library.')
                return
            wx = self.wx
            dialog = wx.SingleChoiceDialog(self.frame, 'Choose an import to resume',
                'Resume import', [f'{index}. {job["state"]}; import {job["id"]}'
                                  for index, job in enumerate(jobs, 1)])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                job = jobs[dialog.GetSelection()]
            finally:
                dialog.Destroy()
            retry = job['state'] in ('quarantined', 'failed', 'cancelled')
            self.start_job(lambda cancelled, progress: self.library.resume_import(
                workspace, job['id'], cancelled=cancelled, retry=retry),
                lambda result: 'Import state: ' + result['state'] + '. Verify the source before citation.')
        except Exception as exc:
            self._status('Import could not be resumed: ' + str(exc))

    def export_topic(self, event=None):
        try:
            workspace, topic = self._scope(topic=True)
        except ValueError as exc:
            self._status(str(exc))
            return
        wx = self.wx
        options = [('Markdown', 'markdown', 'md'), ('Plain text', 'text', 'txt'),
                   ('RIS bibliography', 'ris', 'ris')]
        choice = wx.SingleChoiceDialog(self.frame, 'Export format', 'Export selected research topic',
                                       [label for label, _, _ in options])
        try:
            if choice.ShowModal() != wx.ID_OK:
                return
            label, format, extension = options[choice.GetSelection()]
        finally:
            choice.Destroy()
        destination = wx.FileDialog(self.frame, message='Save research export',
            defaultFile='research.' + extension, wildcard=f'{label} (*.{extension})|*.{extension}',
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if destination.ShowModal() != wx.ID_OK:
                return
            path = destination.GetPath()
        finally:
            destination.Destroy()

        def export(cancelled, progress):
            content = self.library.export_topic(workspace, topic, format=format)
            if cancelled():
                return False
            write_export(path, content, private_root=self.library.root)
            return True

        self.start_job(export, lambda saved: 'Research exported.' if saved else 'Export cancelled.',
                       refresh=False)

    def _new_directory(self, title):
        wx = self.wx
        dialog = wx.DirDialog(self.frame, message='Choose the parent folder for ' + title,
                               style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            parent = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        suggested = 'Restored-research-library' if title.startswith('Restored') else 'Research-library-backup'
        values = self._form(title, [('name', 'New folder name (must not already exist)', suggested, False)])
        if values is None:
            return None
        name = values['name'].strip()
        if not name or name in ('.', '..') or any(char in name for char in ('/', '\\', '\x00')):
            raise ValueError('enter a single new folder name')
        path = parent / name
        if path.exists() or path.is_symlink():
            raise ValueError('destination already exists; choose a new folder name')
        return path

    def _receipt(self, title, message):
        wx = self.wx
        dialog = wx.Dialog(self.frame, title=title, size=(720, 390))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            text = wx.TextCtrl(dialog, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY)
            text.SetName('Operation receipt; select and copy this text to keep it')
            layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(dialog.CreateButtonSizer(wx.OK), 0, wx.ALL, 8)
            dialog.SetSizer(layout)
            text.SetInsertionPoint(0)
            text.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        return message

    def backup_all(self, event=None):
        wx = self.wx
        if wx.MessageBox('Back up all libraries, including archived libraries, original files and '
                         'saved evidence? Pending import jobs are excluded.',
                         'Back up all libraries', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                         self.frame) != wx.YES:
            return
        try:
            destination = self._new_directory('Backup destination')
            if destination is None:
                return

            def backup(cancelled, progress):
                return self.library.backup(destination, include_all_workspaces=True)

            def completed(checksum):
                return self._receipt('Backup completed', f'Backup folder: {destination}\n'
                    f'Manifest SHA256: {checksum}\n\nKeep this checksum separately from the backup. '
                    'Restoring requires the saved checksum. All published libraries, including '
                    'archived libraries, are included; pending import jobs are excluded.')

            self.start_job(backup, completed, refresh=False, cancellable=False)
        except Exception as exc:
            self._status('Backup could not start: ' + str(exc))

    def restore_backup(self, event=None):
        wx = self.wx
        dialog = wx.DirDialog(self.frame, message='Choose the backup folder containing manifest.json',
                               style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            source = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        try:
            values = self._form('Verify the backup receipt', [
                ('checksum', 'Manifest SHA256 from your separately saved backup receipt', '', False)])
            if values is None:
                return
            checksum = values['checksum'].strip().lower()
            if len(checksum) != 64 or any(char not in '0123456789abcdef' for char in checksum):
                raise ValueError('enter the complete 64-character SHA256 from the saved receipt')
            destination = self._new_directory('Restored library destination')
            if destination is None:
                return

            def restore(cancelled, progress):
                return self.library.restore(source, destination, checksum)

            def completed(restored):
                return self._receipt('Backup restored', f'Restored library folder: {restored.root}\n'
                    f'Verified manifest SHA256: {checksum}\n\nCurrent session: {self.library.root}\n'
                    'Open the restored folder in a separate session to use it.')

            self.start_job(restore, completed, refresh=False, cancellable=False)
        except Exception as exc:
            self._status('Restore could not start: ' + str(exc))


def launch(library):
    """Start the actual native event loop with persistent application services."""
    import wx
    from .ui.workspace_shell import WorkspaceFrame

    class NativeFrame(WorkspaceFrame):
        def on_close(self, event):
            controller = getattr(self, 'controller', None)
            if controller is not None and controller.on_close(event):
                return
            super().on_close(event)

    app = wx.App(False)
    frame = NativeFrame(None, library.list_scopes, library.bind_scope)
    frame.controller = DesktopController(wx, frame, library)
    frame.Show()
    app.SetTopWindow(frame)
    frame.choice.SetFocus()
    app.MainLoop()
    return 0
