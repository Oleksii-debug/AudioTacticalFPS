"""Accessible incremental backup choices using the host's existing background job."""
from datetime import datetime
import json
from pathlib import Path

from ..backup_incremental import (BackupReference, MAX_CHAIN, required_base_sha256,
    create_incremental_backup, verify_incremental_backup, restore_incremental_backup)


class NativeDialogs:
    def __init__(self, frame):
        self.frame = frame

    def folder(self, title):
        import wx
        dialog = wx.DirDialog(self.frame, title)
        try:
            return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def checksum(self):
        import wx
        dialog = wx.TextEntryDialog(self.frame,
            'Paste the manifest SHA256 kept separately when this backup was created.',
            'Verify the selected backup identity')
        try:
            return dialog.GetValue().strip().lower() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def show(self, title, body):
        from .desktop_app import show_text
        show_text(self.frame, title, body)


class IncrementalBackupActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs(frame)

    def _reference(self, title, guard, checksum=None):
        guard()
        folder = self.dialogs.folder(title)
        guard()
        if folder is None:
            return None
        if checksum is None:
            checksum = self.dialogs.checksum()
            guard()
            if checksum is None:
                return None
        return BackupReference(folder, checksum)

    def _chain(self, first, guard):
        current, bases, seen = first, [], {first.sha256}
        while True:
            guard()
            required = required_base_sha256(current)
            if required is None:
                return tuple(bases)
            if required in seen or len(bases) >= MAX_CHAIN:
                raise ValueError('The selected backup chain repeats or exceeds eight incremental layers.')
            current = self._reference('Select required base backup: ' + required, guard, checksum=required)
            if current is None:
                return None
            bases.append(current)
            seen.add(current.sha256)

    def start(self, action):
        if action not in ('create', 'verify', 'restore'):
            raise ValueError('Choose create, verify or restore.')
        frame = self.frame
        if getattr(frame, '_closing', False):
            return
        if frame.working:
            frame.report('An operation is already running. Wait or cancel it before using backups.')
            return
        app, root = frame.library, Path(frame.library.root)
        def guard():
            if (getattr(frame, '_closing', False) or frame.library is not app
                    or Path(app.root) != root):
                raise ValueError('The data folder changed. Open the backup action again.')
        try:
            first = self._reference('Select the base backup' if action == 'create' else
                                    'Select the incremental backup to ' + action, guard)
            if first is None:
                return
            bases = self._chain(first, guard)
            if bases is None:
                return
            destination = None
            if action != 'verify':
                parent = self.dialogs.folder('Choose a parent folder for a new incremental backup' if action == 'create'
                                            else 'Choose a parent folder for the restored library')
                guard()
                if parent is None:
                    return
                name = 'ResearchLibrary-incremental-' if action == 'create' else 'ResearchLibrary-restored-'
                destination = parent / (name + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            guard()
            def cancelled():
                guard()
                return frame.cancel_event.is_set()
            def work():
                guard()
                if action == 'create':
                    return create_incremental_backup(app, destination, base=first, ancestors=bases,
                                                     cancelled=cancelled).to_dict()
                if action == 'verify':
                    return verify_incremental_backup(first.path, first.sha256, bases=bases, cancelled=cancelled)
                result = restore_incremental_backup(first.path, destination, first.sha256,
                                                    bases=bases, cancelled=cancelled)
                return {'restored_root': str(result), 'manifest_sha256': first.sha256}
            def done(result):
                if getattr(frame, '_closing', False):
                    return
                if action == 'create':
                    title = 'Incremental backup complete'
                    note = ('Keep this manifest checksum separately. Keep the full base and every required '
                            'incremental layer together with their checksums. This new layer is not a standalone backup.')
                elif action == 'verify':
                    title, note = 'Backup chain verified', 'Every required layer and canonical snapshot was verified.'
                else:
                    title = 'Backup restored'
                    note = ('Open the restored folder through Library, Open existing library folder. '
                            'Restored external engines and remote execution approvals require review.')
                self.dialogs.show(title, note + '\n\n' + json.dumps(result, ensure_ascii=False, indent=2))
            frame.run_job({'create': 'Creating incremental backup', 'verify': 'Verifying backup chain',
                           'restore': 'Restoring incremental backup'}[action], work, done)
        except Exception as exc:
            frame.report(str(exc))

    def create(self, event=None):
        self.start('create')

    def verify(self, event=None):
        self.start('verify')

    def restore(self, event=None):
        self.start('restore')


def install_incremental_backup_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    actions = IncrementalBackupActions(frame)
    menu = frame.GetMenuBar().GetMenu(0)
    menu.AppendSeparator()
    for identity, label, handler in (
            ('backup.incremental.create', 'Create incremental backup...', actions.create),
            ('backup.incremental.verify', 'Verify incremental backup chain...', actions.verify),
            ('backup.incremental.restore', 'Restore incremental backup to a new folder...', actions.restore)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, label), item)
    frame.incremental_backup_actions = actions
