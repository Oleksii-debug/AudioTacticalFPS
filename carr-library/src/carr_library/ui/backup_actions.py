"""Existing full-backup actions on the current Desktop queue and library owner."""
from datetime import datetime
from pathlib import Path

from ..durable_state import local_path, regular
from .backup_resources import review_backup_options


class BackupOwner:
    """Captured Python identity; native checks are used only on the GUI thread."""
    def __init__(self, frame):
        self.frame, self.app = frame, frame.library
        self.root = Path(self.app.root)
        info = regular(local_path(self.root, exists=True), directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.native_id = frame.GetId()

    def guard(self):
        if (getattr(self.frame, '_closing', False) or self.frame.library is not self.app
                or Path(self.app.root) != self.root):
            raise ValueError('The current library changed. Review this backup action again.')
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The current library folder changed. Review this backup action again.')

    def current(self):
        if not self.live():
            return False
        try:
            self.guard()
            return True
        except (ValueError, OSError):
            return False

    def live(self):
        try:
            return (bool(self.frame) and not self.frame.IsBeingDeleted()
                    and self.frame.GetId() == self.native_id
                    and not getattr(self.frame, '_closing', False))
        except (RuntimeError, AttributeError):
            return False

    def cancelled(self):
        if getattr(self.frame, '_closing', False):
            return True
        self.guard()
        return self.frame.cancel_event.is_set()

    def report(self, message):
        if self.live():
            self.frame.report(str(message))


def choose_directory(owner, title, wx):
    owner.guard()
    if not owner.live():
        return None
    chooser = wx.DirDialog(owner.frame, title)
    native_id, alive = chooser.GetId(), True
    def destroyed(event):
        nonlocal alive
        if event.GetId() == native_id:
            alive = False
        event.Skip()
    chooser.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    try:
        accepted = chooser.ShowModal()
        if (not alive or not owner.current() or not chooser
                or chooser.IsBeingDeleted() or chooser.GetId() != native_id):
            return None
        owner.guard()
        return Path(chooser.GetPath()) if accepted == wx.ID_OK else None
    finally:
        try:
            if alive and chooser and not chooser.IsBeingDeleted() and chooser.GetId() == native_id:
                chooser.Destroy()
        except RuntimeError:
            pass


def choose_checksum(owner, title, message, wx):
    """Read only a still-owned native peer after its modal loop returns."""
    owner.guard()
    if not owner.live():
        return None
    chooser = wx.TextEntryDialog(owner.frame, message, title)
    native_id = chooser.GetId()
    alive = True
    def destroyed(event):
        nonlocal alive
        if event.GetId() == native_id:
            alive = False
        event.Skip()
    chooser.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    try:
        accepted = chooser.ShowModal()
        if (not alive or not owner.current() or not chooser
                or chooser.IsBeingDeleted() or chooser.GetId() != native_id):
            return None
        owner.guard()
        return chooser.GetValue() if accepted == wx.ID_OK else None
    finally:
        try:
            if alive and chooser and not chooser.IsBeingDeleted() and chooser.GetId() == native_id:
                chooser.Destroy()
        except RuntimeError:
            pass


def show_report(owner, title, body, wx):
    """Show a completed result without touching a destroyed modal peer."""
    if not owner.current():
        return
    dialog = wx.Dialog(owner.frame, title=title, size=(800, 600))
    native_id = dialog.GetId()
    alive = True
    def destroyed(event):
        nonlocal alive
        if event.GetId() == native_id:
            alive = False
        event.Skip()
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        reader = wx.TextCtrl(dialog, value=body, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reader.SetName(title)
        layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
        back = wx.Button(dialog, wx.ID_CANCEL, '&Close')
        layout.Add(back, 0, wx.ALL, 8)
        dialog.SetSizer(layout)
        reader.SetInsertionPoint(0)
        reader.SetFocus()
        dialog.ShowModal()
    finally:
        try:
            if alive and dialog and not dialog.IsBeingDeleted() and dialog.GetId() == native_id:
                dialog.Destroy()
        except RuntimeError:
            pass


def create_backup(frame):
    import wx
    from ..backup_report import backup_scope_text
    if frame.working or getattr(frame, '_closing', False) or getattr(frame, '_backup_review', None) is not None:
        return
    owner = BackupOwner(frame)
    token = frame._backup_review = object()
    try:
        options = review_backup_options(frame, guard=owner.guard, wx_module=wx)
        if options is None or not owner.live():
            return
        parent = choose_directory(owner, 'Choose a parent folder for the library and project backup', wx)
        if parent is None:
            return
        destination = parent / ('ResearchLibrary-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        owner.guard()
        def work():
            owner.guard()
            kwargs = dict(include_generated=options.include_generated, cancelled=owner.cancelled)
            if options.budget is not None:
                kwargs['backup_budget'] = options.budget
            return owner.app.backup(destination, **kwargs)
        def done(checksum):
            if not owner.current():
                return
            show_report(owner, 'Backup complete', f'Backup folder: {destination}\n\n'
                + backup_scope_text(options.include_generated) + '\n\n'
                f'Keep this checksum separately; it is needed to verify a restore:\n{checksum}', wx)
        frame.run_job('Backing up all libraries', work, done)
    except (Exception, KeyboardInterrupt) as error:
        owner.report(error)
    finally:
        if getattr(frame, '_backup_review', None) is token:
            frame._backup_review = None


def restore_backup(frame):
    import wx
    from ..library_backup import restore_backup as restore
    if frame.working or getattr(frame, '_closing', False) or getattr(frame, '_backup_review', None) is not None:
        return
    owner = BackupOwner(frame)
    token = frame._backup_review = object()
    try:
        options = review_backup_options(frame, restore=True, guard=owner.guard, wx_module=wx)
        if options is None or not owner.live():
            return
        source = choose_directory(owner, 'Select the backup folder', wx)
        if source is None:
            return
        checksum = choose_checksum(owner, 'Verify backup',
            'Paste the manifest checksum saved when this backup was created.', wx)
        if checksum is None or not owner.live():
            return
        owner.guard()
        parent = choose_directory(owner, 'Choose a parent folder for the restored libraries', wx)
        if parent is None:
            return
        destination = parent / ('ResearchLibrary-restored-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        owner.guard()
        def work():
            owner.guard()
            kwargs = dict(expected_manifest_sha256=checksum.strip(), cancelled=owner.cancelled)
            if options.budget is not None:
                kwargs['backup_budget'] = options.budget
            return restore(source, destination, **kwargs)
        def done(path):
            if not owner.current():
                return
            from ..backup_report import recovery_summary
            show_report(owner, 'Recovery details', recovery_summary(path), wx)
            if not owner.current():
                return
            answer = wx.MessageBox('Backup restored successfully. Open the restored libraries now?',
                'Restore complete', wx.YES_NO | wx.ICON_QUESTION, frame)
            if not owner.current():
                return
            if answer == wx.YES:
                frame._open_data_root(path)
            else:
                show_report(owner, 'Restore complete', f'Restored library folder: {path}\n\n'
                    'You can open it later through Library, Open existing library folder.', wx)
        frame.run_job('Restoring backup', work, done)
    except (Exception, KeyboardInterrupt) as error:
        owner.report(error)
    finally:
        if getattr(frame, '_backup_review', None) is token:
            frame._backup_review = None
