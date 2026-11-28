"""Native, asynchronous controls for the existing complete-backup snapshot store."""
from pathlib import Path


def start_snapshot(frame, action, repository, *, checksum='', destination='',
                   done=None, failed=None):
    """Capture library and cancellation identity before sending work off the UI thread."""
    if getattr(frame, '_closing', False):
        return False
    if getattr(frame, 'working', False):
        frame.report('An operation is already running. Wait or choose Cancel current operation.')
        return False
    if action not in ('create', 'list', 'verify', 'restore'):
        raise ValueError('Choose a snapshot action.')
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError('Choose a snapshot repository.')
    if action in ('verify', 'restore'):
        from ..incremental_backup import _digest
        _digest(checksum.strip())
    if action == 'restore' and (not isinstance(destination, str) or not destination.strip()):
        raise ValueError('Choose a new restored-library folder.')
    library = frame.library
    stop = frame.cancel_event.is_set
    repository, checksum, destination = repository.strip(), checksum.strip(), destination.strip()

    def execute():
        from ..incremental_backup import (
            create_snapshot, list_snapshots, verify_snapshot, restore_snapshot)
        if action == 'create':
            return create_snapshot(library, repository, cancelled=stop)
        if action == 'list':
            return list_snapshots(repository, cancelled=stop)
        if action == 'verify':
            return verify_snapshot(repository, checksum, cancelled=stop)
        return restore_snapshot(repository, checksum, destination, cancelled=stop)

    def ready(result):
        if (not getattr(frame, '_closing', False)
                and getattr(frame, 'library', None) is library and done is not None):
            done(result)

    def error(exc):
        if (not getattr(frame, '_closing', False)
                and getattr(frame, 'library', None) is library and failed is not None):
            failed(exc)

    titles = {'create': 'Saving incremental snapshot', 'list': 'Reading snapshot history',
              'verify': 'Verifying complete snapshot', 'restore': 'Restoring complete snapshot'}
    frame.run_job(titles[action], execute, ready, error, progress_unit='files')
    return True


def build_snapshot_dialog(frame):
    import wx
    from datetime import datetime

    class SnapshotDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Incremental library snapshots',
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self.pending = True, False
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.TextCtrl(self, value=(
                'Each snapshot contains a complete description of your saved libraries. '
                'Unchanged files share storage across snapshots. Creating a snapshot still '
                'captures and verifies all data. Restore also needs temporary disk space.\n'
                'Choose a new repository folder or an existing snapshot repository. '
                'Keep the checksum separately. History lists metadata until you choose Verify.\n'
                'These local snapshots are not encrypted. External models and credentials '
                'use their existing separate recovery. Restored unfinished jobs require review.'),
                style=wx.TE_MULTILINE | wx.TE_READONLY)
            explanation.SetName('Incremental snapshot explanation')
            layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
            self.repository = wx.TextCtrl(self)
            self.repository.SetName('Snapshot repository folder')
            self.repository.SetMaxLength(4096)
            self.checksum = wx.TextCtrl(self)
            self.checksum.SetName('Separately retained snapshot SHA-256 checksum')
            self.checksum.SetMaxLength(64)
            self.destination = wx.TextCtrl(self)
            self.destination.SetName('New restored library folder')
            self.destination.SetMaxLength(4096)
            self.fields = (self.repository, self.checksum, self.destination)
            for label, field in (('Snapshot repository folder', self.repository),
                                 ('Separately retained snapshot checksum', self.checksum),
                                 ('New restored library folder', self.destination)):
                layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.TOP, 8)
                layout.Add(field, 0, wx.EXPAND | wx.ALL, 8)
            folders = wx.BoxSizer(wx.HORIZONTAL)
            self.choose_repository = wx.Button(self, label='Choose existing repository…')
            self.new_repository = wx.Button(self, label='New repository in folder…')
            self.choose_destination = wx.Button(self, label='Choose restore parent…')
            for button in (self.choose_repository, self.new_repository, self.choose_destination):
                button.SetName(button.GetLabel())
                folders.Add(button, 0, wx.ALL, 4)
            layout.Add(folders, 0, wx.EXPAND)
            self.choose_repository.Bind(wx.EVT_BUTTON, lambda event: self.folder('existing'))
            self.new_repository.Bind(wx.EVT_BUTTON, lambda event: self.folder('new'))
            self.choose_destination.Bind(wx.EVT_BUTTON, lambda event: self.folder('restore'))
            self.actions = {}
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            for action, label in (('create', 'Save new snapshot'), ('list', 'List snapshots'),
                                   ('verify', 'Verify snapshot'), ('restore', 'Restore snapshot')):
                button = wx.Button(self, label=label)
                button.SetName(label)
                button.Bind(wx.EVT_BUTTON, lambda event, name=action: self.submit(name))
                self.actions[action] = button
                buttons.Add(button, 0, wx.ALL, 4)
            layout.Add(buttons, 0, wx.EXPAND)
            self.status = wx.TextCtrl(self, value='Choose a repository and an action.',
                                     style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Snapshot operation result and checksum')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.close = wx.Button(self, wx.ID_CANCEL, label='Close')
            self.close.SetName('Close snapshots; cancel an unfinished operation')
            self.close.Bind(wx.EVT_BUTTON, self.on_close)
            layout.Add(self.close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.SetSizer(layout)
            self.SetSize((940, 690))
            self.SetMinSize((700, 580))
            self.repository.SetFocus()

        def folder(self, kind):
            if self.pending:
                return
            labels = {'existing': 'Choose the existing snapshot repository',
                      'new': 'Choose a parent folder for a new snapshot repository',
                      'restore': 'Choose a parent folder for a new restored library'}
            picker = wx.DirDialog(self, labels[kind])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                selected = Path(picker.GetPath())
            finally:
                picker.Destroy()
            if kind == 'existing':
                self.repository.SetValue(str(selected))
                self.repository.SetFocus()
            else:
                stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                if kind == 'new':
                    self.repository.SetValue(str(selected / ('Resonance-snapshots-' + stamp)))
                    self.repository.SetFocus()
                else:
                    self.destination.SetValue(str(selected / ('Resonance-restored-' + stamp)))
                    self.destination.SetFocus()

        def controls(self, busy):
            self.pending = busy
            for control in (*self.fields, self.choose_repository, self.new_repository,
                            self.choose_destination, *self.actions.values()):
                control.Enable(not busy)
            self.close.Enable(True)

        def report(self, text):
            self.status.SetValue(text)
            self.status.SetFocus()

        def submit(self, action):
            if self.pending:
                return
            self.controls(True)
            self.report('Working locally. Close requests cancellation of unfinished work.')
            try:
                started = start_snapshot(frame, action, self.repository.GetValue(),
                    checksum=self.checksum.GetValue(), destination=self.destination.GetValue(),
                    done=lambda result: self.ready(action, result), failed=self.error)
                if not started:
                    self.controls(False)
                    self.report('The main window is busy or closing.')
            except Exception as exc:
                self.error(exc)

        def ready(self, action, result):
            if not self.alive:
                return
            self.controls(False)
            if action == 'create':
                self.checksum.SetValue(result.snapshot.snapshot_sha256)
                text = (f'Snapshot saved. Keep this checksum separately:\n{result.snapshot.snapshot_sha256}\n\n'
                        f'Files: {result.snapshot.file_count}; logical bytes: {result.snapshot.total_bytes}.\n'
                        f'New content files: {result.new_objects}; new bytes stored: {result.new_bytes}.\n'
                        f'Reused file references: {result.reused_files}; bytes reused: {result.reused_bytes}.')
            elif action == 'list':
                text = 'Snapshot metadata only; use Verify to check all stored bytes.\n\n' + '\n\n'.join(
                    f'{row.created_utc}\n{row.snapshot_sha256}\nFiles: {row.file_count}; bytes: {row.total_bytes}.'
                    for row in result)
                if not result:
                    text += 'No completed snapshots.'
            elif action == 'verify':
                text = (f'Complete snapshot verified:\n{result.snapshot_sha256}\n'
                        f'Files: {result.file_count}; bytes: {result.total_bytes}.')
            else:
                text = (f'Library restored to:\n{result.path}\n\n'
                        'Open it through Library, Open existing library folder. '
                        'Review unfinished jobs before resuming them.')
            warning = getattr(result, 'cleanup_warning', None)
            if warning:
                text += '\n\n' + warning
            self.report(text)

        def error(self, exc):
            if self.alive:
                self.controls(False)
                self.report(f'Snapshot operation not completed: {exc}')

        def on_close(self, event=None):
            if self.pending:
                frame.cancel_event.set()
            self.alive = False
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self.alive = False
                if self.pending:
                    frame.cancel_event.set()
            event.Skip()

    return SnapshotDialog()


def show_snapshot_dialog(frame, event=None):
    if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
        frame.report('Wait for the current operation or cancel it before opening snapshots.')
        return
    dialog = build_snapshot_dialog(frame)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
