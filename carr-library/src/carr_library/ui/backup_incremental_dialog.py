"""Incremental backup choices over the existing desktop background dispatcher.

Only file/folder choices and confirmation text run on the GUI thread. Header
hashing, ancestor inspection, complete snapshot verification and publication
borrow frame.run_job; no new executor or backup producer is installed.
"""
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
            if dialog:
                dialog.Destroy()

    def checksum(self):
        import wx
        dialog = wx.TextEntryDialog(self.frame,
            'Paste the manifest SHA256 kept separately when this backup was created.',
            'Verify the selected backup identity')
        try:
            return dialog.GetValue().strip().lower() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            if dialog:
                dialog.Destroy()

    def show(self, title, body):
        from .desktop_app import show_text
        show_text(self.frame, title, body)


class IncrementalBackupActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs(frame)
        self._generation = 0

    def _reference(self, title, guard, checksum=None):
        # Return the user's declaration only. Even selected-root validation and
        # the bounded manifest read belong to the existing worker.
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
        return Path(folder), checksum

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
        self._generation += 1
        generation = self._generation
        selected, seen = [], set()
        started = False

        def guard():
            if (generation != self._generation or getattr(frame, '_closing', False)
                    or frame.library is not app or Path(app.root) != root):
                raise InterruptedError('The data folder or backup action changed. Open this action again.')
            if started and frame.cancel_event.is_set():
                raise InterruptedError('The backup action was cancelled.')

        def failed(error):
            if not getattr(frame, '_closing', False) and generation == self._generation:
                frame.report(str(error))

        def queue(title, work, done, *, durable=False):
            nonlocal started
            # In particular, a modal picker cannot let the next run_job clear
            # cancellation raised while the previous header was under review.
            guard()
            started = True
            def checked_work():
                guard()
                return work()
            def checked_done(value):
                if durable:
                    # Once a service returns a committed receipt, late Cancel
                    # cannot turn that success into permission to replay it.
                    if getattr(frame, '_closing', False) or generation != self._generation:
                        return
                else:
                    guard()
                done(value)
            frame.run_job(title, checked_work, checked_done, failed)

        def cancelled():
            guard()
            return frame.cancel_event.is_set()

        def finish(result):
            if getattr(frame, '_closing', False) or generation != self._generation:
                return
            if action == 'create':
                title = 'Incremental backup complete'
                note = ('Keep this manifest checksum separately. Keep the full base and every required '
                        'incremental layer together with their checksums. This new layer is not a standalone backup.')
            elif action == 'verify':
                title, note = 'Backup chain verified', 'Every required layer and complete snapshot was verified.'
            else:
                title = 'Backup restored'
                note = ('Open the restored folder through Library, Open existing library folder. '
                        'Restored external engines and remote execution approvals require review.')
            if frame.library is not app or Path(app.root) != root:
                note += ' This completed operation belongs to the previously selected library.'
            self.dialogs.show(title, note + '\n\n' + json.dumps(result, ensure_ascii=False, indent=2))

        def ready():
            guard()
            first, bases = selected[0], tuple(selected[1:])
            destination = None
            if action != 'verify':
                parent = self.dialogs.folder(
                    'Choose a parent folder for a new incremental backup' if action == 'create'
                    else 'Choose a parent folder for the restored library')
                guard()
                if parent is None:
                    return
                name = 'ResearchLibrary-incremental-' if action == 'create' else 'ResearchLibrary-restored-'
                destination = Path(parent) / (name + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))

            def work():
                guard()
                if action == 'create':
                    return create_incremental_backup(app, destination, base=first, ancestors=bases,
                                                     cancelled=cancelled).to_dict()
                if action == 'verify':
                    return verify_incremental_backup(first.path, first.sha256, bases=bases,
                                                     cancelled=cancelled)
                result = restore_incremental_backup(first.path, destination, first.sha256,
                                                    bases=bases, cancelled=cancelled)
                return {'restored_root': str(result), 'manifest_sha256': first.sha256}
            queue({'create': 'Creating incremental backup', 'verify': 'Verifying backup chain',
                   'restore': 'Restoring incremental backup'}[action], work, finish, durable=True)

        def inspect(declaration):
            def read_header():
                guard()
                reference = BackupReference(*declaration)
                required = required_base_sha256(reference)
                guard()
                return reference, required

            def inspected(result):
                reference, required = result
                if reference.sha256 in seen or len(selected) > MAX_CHAIN:
                    raise ValueError('The selected backup chain repeats or exceeds eight incremental layers.')
                seen.add(reference.sha256)
                selected.append(reference)
                if required is None:
                    ready()
                    return
                if required in seen or len(selected) >= MAX_CHAIN + 1:
                    raise ValueError('The selected backup chain repeats or exceeds eight incremental layers.')
                next_reference = self._reference('Select required base backup: ' + required,
                                                 guard, checksum=required)
                if next_reference is not None:
                    inspect(next_reference)

            queue('Checking selected backup identity', read_header, inspected)

        try:
            first = self._reference('Select the base backup' if action == 'create' else
                                    'Select the incremental backup to ' + action, guard)
            if first is not None:
                inspect(first)
        except Exception as exc:
            failed(exc)

    def create(self, event=None):
        self.start('create')

    def verify(self, event=None):
        self.start('verify')

    def restore(self, event=None):
        self.start('restore')


def install_incremental_backup_actions(frame):
    import wx
    actions = IncrementalBackupActions(frame)
    menu = frame.GetMenuBar().GetMenu(0)
    menu.AppendSeparator()
    # The host's one keyboard/menu capture runs after installers. Do not create
    # the donor's obsolete second CommandSpec registry.
    for label, handler in (
            ('Create incremental backup...', actions.create),
            ('Verify incremental backup chain...', actions.verify),
            ('Restore incremental backup to a new folder...', actions.restore)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.incremental_backup_actions = actions
