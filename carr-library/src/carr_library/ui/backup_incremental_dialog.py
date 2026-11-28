"""Explicit incremental choices through the existing Desktop backup queue."""
from datetime import datetime
import json
from pathlib import Path

from ..backup_incremental import (BackupReference, MAX_CHAIN, DEFAULT_MAX_BYTES,
    create_incremental_backup, verify_incremental_backup, restore_incremental_backup)
from .backup_actions import BackupOwner, choose_checksum, show_report
from .backup_resources import review_backup_options


def create_base_review(parent, first, *, choose_reference, guard, wx_module=None):
    """Review explicit references without reading a large manifest on the GUI."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class BaseReview(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Required backup generations', size=(760, 540),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self._destroy_requested = True, False
            self._native_id = self.GetId()
            self.references, self.result = [], None
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.Bind(wx.EVT_CLOSE, self.cancel)
            layout = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(self, label=('Select each required ancestor explicitly, ending with its full backup. '
                'Every folder needs the manifest checksum you retained separately. Leave this list empty when '
                'the selected generation is already a full backup. Verification runs on the existing background worker.'))
            label.Wrap(690)
            layout.Add(label, 0, wx.EXPAND | wx.ALL, 10)
            selected = wx.StaticText(self, label='Selected generation: ' + str(first.path) + '\nSHA256: ' + first.sha256)
            selected.Wrap(690)
            layout.Add(selected, 0, wx.EXPAND | wx.ALL, 10)
            self.items = wx.ListBox(self)
            self.items.SetName('Explicit required backup generations')
            layout.Add(self.items, 1, wx.EXPAND | wx.ALL, 10)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.add = wx.Button(self, label='Add required base…')
            self.add.SetName('Add required backup base')
            self.add.Bind(wx.EVT_BUTTON, self.add_reference)
            self.remove = wx.Button(self, label='Remove selected base')
            self.remove.SetName('Remove selected backup base')
            self.remove.Bind(wx.EVT_BUTTON, self.remove_reference)
            row.Add(self.add, 0, wx.ALL, 6)
            row.Add(self.remove, 0, wx.ALL, 6)
            layout.Add(row, 0, wx.EXPAND)
            limit = wx.BoxSizer(wx.HORIZONTAL)
            limit.Add(wx.StaticText(self, label='Logical snapshot limit (MiB)'), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.ALL, 6)
            self.maximum = wx.TextCtrl(self, value=str(DEFAULT_MAX_BYTES // 1024**2))
            self.maximum.SetName('Incremental logical snapshot limit in MiB')
            limit.Add(self.maximum, 1, wx.EXPAND | wx.ALL, 6)
            layout.Add(limit, 0, wx.EXPAND)
            self.notice = wx.StaticText(self, label='The originals and all selected generations remain unchanged.')
            self.notice.SetName('Backup generation review status')
            self.notice.Wrap(690)
            layout.Add(self.notice, 0, wx.EXPAND | wx.ALL, 10)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.accept = wx.Button(self, wx.ID_OK, 'Use selected base chain')
            self.accept.SetName('Accept explicit backup generations')
            self.accept.Bind(wx.EVT_BUTTON, self.confirm)
            cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
            cancel.Bind(wx.EVT_BUTTON, self.cancel)
            buttons.Add(self.accept, 0, wx.ALL, 6)
            buttons.Add(cancel, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.add.SetFocus()

        def failed(self, error):
            if self.alive:
                self.notice.SetLabel(str(error))
                self.notice.Wrap(max(300, self.GetClientSize().width - 30))
                self.Layout()

        def add_reference(self, event=None):
            if not self.alive:
                return
            try:
                guard()
                if len(self.references) >= MAX_CHAIN:
                    raise ValueError('Select at most eight required ancestor generations.')
                selected = choose_reference('Select a required ancestor backup', guard)
                if not self.alive or selected is None:
                    return
                guard()
                if selected.sha256 in {first.sha256, *(item.sha256 for item in self.references)}:
                    raise ValueError('Choose each exact generation only once.')
                self.references.append(selected)
                self.items.Append(selected.sha256 + '  ' + str(selected.path))
                self.items.SetSelection(len(self.references)-1)
            except (Exception, KeyboardInterrupt) as error:
                self.failed(error)

        def remove_reference(self, event=None):
            if not self.alive:
                return
            try:
                guard()
                index = self.items.GetSelection()
                if not 0 <= index < len(self.references):
                    raise ValueError('Select an ancestor to remove.')
                self.references.pop(index)
                self.items.Delete(index)
            except (Exception, KeyboardInterrupt) as error:
                self.failed(error)

        def confirm(self, event=None):
            if not self.alive:
                return
            try:
                guard()
                raw = self.maximum.GetValue().strip()
                if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
                    raise ValueError('Choose a positive logical snapshot limit in whole MiB.')
                maximum = int(raw) * 1024**2
                if not 1 <= maximum <= 2**63-1:
                    raise ValueError('The logical snapshot limit is outside the bounded byte range.')
                self.result = tuple(self.references), maximum
            except (Exception, KeyboardInterrupt) as error:
                self.failed(error)
                return
            self.EndModal(wx.ID_OK)

        def cancel(self, event=None):
            if not self.alive:
                return
            self.alive, self.result = False, None
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)

        def destroyed(self, event):
            if event.GetId() == self._native_id:
                self.alive = False
            event.Skip()

        def Destroy(self):
            self.alive = False
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    return BaseReview()


class NativeDialogs:
    def __init__(self, frame):
        self.frame = frame

    def folder(self, title):
        import wx
        dialog = wx.DirDialog(self.frame, title)
        try:
            accepted = dialog.ShowModal()
            if not dialog or dialog.IsBeingDeleted():
                return None
            return Path(dialog.GetPath()) if accepted == wx.ID_OK else None
        finally:
            try:
                if dialog and not dialog.IsBeingDeleted():
                    dialog.Destroy()
            except RuntimeError:
                pass

    def checksum(self, owner):
        import wx
        return choose_checksum(owner, 'Verify the selected backup identity',
            'Paste the manifest SHA256 kept separately when this backup was created.', wx)

    def options(self, owner, action):
        return review_backup_options(self.frame, restore=action != 'create', guard=owner.guard)

    def chain(self, first, choose_reference, guard):
        import wx
        dialog = create_base_review(self.frame, first, choose_reference=choose_reference, guard=guard)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog or not dialog.alive:
                return None
            guard()
            return dialog.result
        finally:
            try:
                if dialog and not dialog.IsBeingDeleted():
                    dialog.Destroy()
            except RuntimeError:
                pass

    def show(self, title, body, owner):
        import wx
        show_report(owner, title, body, wx)


class IncrementalBackupActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs(frame)

    def _reference(self, title, guard):
        guard()
        folder = self.dialogs.folder(title)
        if not self.owner.live():
            return None
        guard()
        if folder is None:
            return None
        checksum = self.dialogs.checksum(self.owner)
        if not self.owner.live() or checksum is None:
            return None
        guard()
        return BackupReference(folder, checksum.strip().lower())

    def start(self, action):
        if action not in ('create', 'verify', 'restore'):
            raise ValueError('Choose create, verify or restore.')
        frame = self.frame
        if getattr(frame, '_closing', False) or getattr(frame, '_backup_review', None) is not None:
            return
        if frame.working:
            frame.report('An operation is already running. Wait or cancel it before using backups.')
            return
        owner = self.owner = BackupOwner(frame)
        token = frame._backup_review = object()
        try:
            options = self.dialogs.options(owner, action)
            if options is None or not owner.current():
                return
            first = self._reference('Select the base backup' if action == 'create' else
                                    'Select the backup generation to ' + action, owner.guard)
            if first is None:
                return
            chosen = self.dialogs.chain(first, self._reference, owner.guard)
            if chosen is None or not owner.current():
                return
            bases, maximum = chosen
            destination = None
            if action != 'verify':
                parent = self.dialogs.folder('Choose a parent folder for a new incremental backup' if action == 'create'
                                            else 'Choose a parent folder for the restored library')
                if parent is None or not owner.current():
                    return
                name = 'ResearchLibrary-incremental-' if action == 'create' else 'ResearchLibrary-restored-'
                destination = parent / (name + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            owner.guard()
            def work():
                owner.guard()
                kwargs = dict(max_bytes=maximum, cancelled=owner.cancelled, backup_budget=options.budget)
                if action == 'create':
                    return create_incremental_backup(owner.app, destination, base=first, ancestors=bases,
                        include_generated=options.include_generated, **kwargs).to_dict()
                if action == 'verify':
                    return verify_incremental_backup(first.path, first.sha256, bases=bases, **kwargs)
                result = restore_incremental_backup(first.path, destination, first.sha256, bases=bases, **kwargs)
                return {'restored_root': str(result), 'manifest_sha256': first.sha256}
            def done(result):
                if not owner.current():
                    return
                if action == 'create':
                    title = 'Incremental backup complete'
                    note = ('Keep this checksum separately. This generation requires its selected full base and every '
                            'intermediate generation. It is not a standalone backup.')
                elif action == 'verify':
                    title, note = 'Backup chain verified', 'Every required layer and canonical snapshot was verified.'
                else:
                    title = 'Backup restored'
                    note = ('Open the restored folder through Library, Open existing library folder. '
                            'Restored external engines, scan roots and execution approvals require explicit review.')
                self.dialogs.show(title, note + '\n\n' + json.dumps(result, ensure_ascii=False, indent=2), owner)
            frame.run_job({'create': 'Creating incremental backup', 'verify': 'Verifying backup chain',
                           'restore': 'Restoring incremental backup'}[action], work, done)
        except (Exception, KeyboardInterrupt) as error:
            owner.report(error)
        finally:
            if getattr(frame, '_backup_review', None) is token:
                frame._backup_review = None

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
    actions = getattr(frame, 'incremental_backup_actions', None)
    if actions is not None:
        return actions
    actions = IncrementalBackupActions(frame)
    bar = frame.GetMenuBar()
    count = bar.GetMenuCount() if bar is not None else 0
    if not 0 < count <= 64:
        raise ValueError('The current Library menu is unavailable.')
    matches = [bar.GetMenu(index) for index in range(count)
               if bar.GetMenuLabel(index).replace('&', '') == 'Library']
    if len(matches) != 1:
        raise ValueError('The current Library menu must be present exactly once.')
    menu = matches[0]
    menu.AppendSeparator()
    for identity, label, handler in (
            ('backup.incremental.create', 'Create incremental backup...', actions.create),
            ('backup.incremental.verify', 'Verify incremental backup chain...', actions.verify),
            ('backup.incremental.restore', 'Restore incremental backup to a new folder...', actions.restore)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, label), item)
    frame.incremental_backup_actions = actions
    return actions
