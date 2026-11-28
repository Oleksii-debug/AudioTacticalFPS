"""Reviewed research-only backup merge on the existing Desktop executor."""
from datetime import datetime
import json
from pathlib import Path
import threading

from ..research_merge import ResearchContentMerge
from .backup_actions import BackupOwner
from .backup_incremental_dialog import NativeDialogs as BackupDialogs
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command

MERGE_COMMAND = CommandSpec('backup.research.merge', 'Merge research content from two backups')


def create_merge_review(parent, action, preview, *, guard, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class Review(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Review research-content merge', size=(940, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id, self.alive, self.result = self.GetId(), True, None
            self._destroy_requested = False
            self.rows = json.loads(preview.payload)['workspaces']
            self.choices = {}
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.Bind(wx.EVT_CLOSE, self.cancel)
            layout = wx.BoxSizer(wx.VERTICAL)
            note = wx.StaticText(self, label=(
                'Only published research content and its originals will be combined in a NEW folder. '
                'Operational/import state, audio projects, reading drafts and preferences are omitted. '
                'Both full input backups remain unchanged. Choose a whole left/right workspace or omit it; '
                'conflicting stable IDs are never renamed or retargeted.'))
            note.Wrap(880)
            layout.Add(note, 0, wx.EXPAND | wx.ALL, 8)
            self.details = wx.TextCtrl(self, value=preview.text,
                                      style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
            self.details.SetName('Exact research merge preview, input checksums and omitted sidecars')
            layout.Add(self.details, 2, wx.EXPAND | wx.ALL, 8)
            stamp = wx.StaticText(self, label='Reviewed preview SHA-256: ' + preview.sha256)
            stamp.SetName('Research merge preview SHA-256')
            layout.Add(stamp, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.items = wx.ListBox(self)
            self.items.SetName('Workspace conflicts and explicit whole-workspace choices')
            layout.Add(self.items, 1, wx.EXPAND | wx.ALL, 8)
            choices = wx.WrapSizer(wx.HORIZONTAL)
            self.left = wx.Button(self, label='Use complete &left workspace')
            self.right = wx.Button(self, label='Use complete &right workspace')
            self.omit = wx.Button(self, label='&Omit selected workspace')
            self.nonconflicting = wx.Button(self, label='Select all &nonconflicting workspaces')
            for control, side in ((self.left, 'left'), (self.right, 'right'), (self.omit, 'omit')):
                control.Bind(wx.EVT_BUTTON, lambda event, selected=side: self.choose(selected))
                choices.Add(control, 0, wx.ALL, 4)
            self.nonconflicting.Bind(wx.EVT_BUTTON, self.choose_nonconflicting)
            choices.Add(self.nonconflicting, 0, wx.ALL, 4)
            layout.Add(choices, 0, wx.EXPAND | wx.ALL, 4)
            self.acknowledge = wx.CheckBox(self, label=(
                'I reviewed the omitted operational, audio and settings files; the full input backups remain separate.'))
            self.acknowledge.SetName('Acknowledge every omitted nonresearch sidecar')
            self.acknowledge.SetValue(False)
            layout.Add(self.acknowledge, 0, wx.EXPAND | wx.ALL, 8)
            self.status = wx.StaticText(self, label='Review every workspace before creating the new library.')
            self.status.SetName('Research merge choice status')
            self.status.Wrap(880)
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.accept = wx.Button(self, wx.ID_OK, 'Create &new research library')
            self.back = wx.Button(self, wx.ID_CANCEL, '&Cancel')
            self.accept.Bind(wx.EVT_BUTTON, self.confirm)
            self.back.Bind(wx.EVT_BUTTON, self.cancel)
            buttons.Add(self.accept, 0, wx.ALL, 6)
            buttons.Add(self.back, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((800, 650))
            self.SetEscapeId(wx.ID_CANCEL)
            self.refresh()
            self.details.SetInsertionPoint(0)
            self.details.SetFocus()

        def refresh(self):
            if not self.alive:
                return
            index = self.items.GetSelection()
            labels = []
            for row in self.rows:
                names = ' / '.join(dict.fromkeys(item['name'] for item in (row['left'], row['right'])
                                                if item is not None))
                labels.append(names + ' | ' + row['id'] + ' | ' + row['state']
                              + ' | ' + self.choices.get(row['id'], 'NOT CHOSEN'))
            self.items.Set(labels)
            if labels:
                self.items.SetSelection(index if 0 <= index < len(labels) else 0)

        def choose(self, side):
            if not self.alive:
                return
            guard()
            index = self.items.GetSelection()
            if not 0 <= index < len(self.rows):
                return
            row = self.rows[index]
            if side != 'omit' and row[side] is None:
                self.status.SetLabel('That workspace is absent on the selected side.')
                return
            self.choices[row['id']] = side
            self.refresh()

        def choose_nonconflicting(self, event=None):
            if not self.alive:
                return
            guard()
            for row in self.rows:
                if row['state'] in ('left_only', 'identical'):
                    self.choices[row['id']] = 'left'
                elif row['state'] == 'right_only':
                    self.choices[row['id']] = 'right'
            self.refresh()
            self.status.SetLabel('Nonconflicting workspaces selected explicitly. Review every remaining conflict.')

        def confirm(self, event=None):
            if not self.alive:
                return
            try:
                guard()
                if self.details.GetValue() != preview.text:
                    raise ValueError('The displayed preview changed. Cancel and prepare it again.')
                acknowledged = self.acknowledge.GetValue()
                selected = dict(self.choices)
                action._choices(preview, selected, acknowledged)
                self.result = selected, acknowledged
                self.EndModal(wx.ID_OK)
            except Exception as error:
                self.status.SetLabel(str(error))
                self.status.Wrap(max(300, self.GetClientSize().width - 30))
                self.Layout()

        def cancel(self, event=None):
            self.result = None
            if self.alive and self:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Destroy()

        def destroyed(self, event):
            if event.GetId() == self._native_id:
                self.alive = False
                self.result = None
            event.Skip()

        def Destroy(self):
            self.alive = False
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    return Review()


class NativeDialogs(BackupDialogs):
    def review(self, action, preview, owner):
        import wx
        dialog = create_merge_review(self.frame, action, preview, guard=owner.guard)
        try:
            answer = dialog.ShowModal()
            if not dialog or not dialog.alive or not owner.current() or answer != wx.ID_OK:
                return None
            owner.guard()
            return dialog.result
        finally:
            try:
                if dialog and not dialog.IsBeingDeleted():
                    dialog.Destroy()
            except RuntimeError:
                pass


class ResearchMergeActions:
    """One action owner across collection, preview and the existing queued writer."""
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs(frame)
        self.withdrawn = threading.Event()
        self.owner = self.token = self.action = self.preview = self.result = None

    def _release(self, token):
        if getattr(self.frame, '_backup_review', None) is token:
            self.frame._backup_review = None
        if self.token is token:
            self.token = None

    def withdraw(self):
        self.withdrawn.set()
        if self.token is not None:
            self._release(self.token)

    def _current(self, owner, token):
        return (not self.withdrawn.is_set() and self.token is token
                and getattr(self.frame, '_backup_review', None) is token and owner.current())

    def _cancelled(self, owner, token):
        return (self.withdrawn.is_set() or self.token is not token
                or getattr(self.frame, '_backup_review', None) is not token or owner.cancelled())

    def _reference(self, title, owner):
        owner.guard()
        folder = self.dialogs.folder(title)
        if folder is None or not owner.current() or self.withdrawn.is_set():
            return None
        checksum = self.dialogs.checksum(owner)
        if checksum is None or not owner.current() or self.withdrawn.is_set():
            return None
        owner.guard()
        return Path(folder), checksum.strip().lower()

    def _queue(self, title, operation, completed, owner, token):
        if not self._current(owner, token) or self.frame.working:
            self._release(token)
            return False
        settled, entered = False, threading.Event()
        def done(value):
            nonlocal settled
            if settled:
                return
            settled = True
            if not self._current(owner, token):
                self._release(token)
                return
            try:
                completed(value)
            except Exception as error:
                owner.report(error)
                self._release(token)
            except BaseException:
                self._release(token)
                raise
        def failed(error):
            nonlocal settled
            if settled:
                return
            settled = True
            if self._current(owner, token):
                owner.report(error)
            self._release(token)
        def work():
            entered.set()
            if self.withdrawn.is_set() or self.token is not token:
                raise InterruptedError('Research merge owner was closed.')
            owner.guard()
            try:
                return operation()
            finally:
                if self.withdrawn.is_set() or getattr(self.frame, '_closing', False):
                    self._release(token)
        try:
            self.frame.run_job(title, work, done=done, failed=failed)
            # The existing host legitimately returns None both after admission
            # and after refusing a closing owner. Do not infer a second worker.
            if not settled and not entered.is_set() and not self.frame.working:
                self._release(token)
                return False
        except Exception as error:
            failed(error)
            return False
        except BaseException:
            self._release(token)
            raise
        return True

    def start(self, event=None):
        frame = self.frame
        if (self.withdrawn.is_set() or getattr(frame, '_closing', False)
                or getattr(frame, '_backup_review', None) is not None):
            return False
        if frame.working:
            frame.report('Wait for the current operation or cancel it before reviewing a research merge.')
            return False
        owner = self.owner = BackupOwner(frame)
        token = self.token = frame._backup_review = object()
        queued = False
        try:
            options = self.dialogs.options(owner, 'restore')
            if options is None or not self._current(owner, token):
                return False
            left = self._reference('Select the LEFT full backup for a research-content merge', owner)
            if left is None:
                return False
            right = self._reference('Select the RIGHT full backup for a research-content merge', owner)
            if right is None:
                return False
            parent = self.dialogs.folder('Choose a parent folder for the NEW merged research library')
            if parent is None or not self._current(owner, token):
                return False
            destination = Path(parent) / ('ResearchLibrary-merged-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            action = self.action = ResearchContentMerge(*left, *right, destination,
                                                        backup_budget=options.budget)
            def prepared(preview):
                self.preview = preview
                selected = self.dialogs.review(action, preview, owner)
                if selected is None or not self._current(owner, token):
                    self._release(token)
                    return
                choices, acknowledged = selected
                choices = dict(choices)
                action._choices(preview, choices, acknowledged)
                def merge():
                    result = action.merge(preview, choices, acknowledged_omissions=acknowledged,
                                          cancelled=lambda: self._cancelled(owner, token))
                    # Pure receipt retention survives a closed native observer.
                    self.result = result
                    return result
                def finished(result):
                    self._release(token)
                    if owner.current() and not self.withdrawn.is_set():
                        self.dialogs.show('New research library created',
                            'This is the reviewed research-content profile. Operational/audio/settings state '
                            'remains only in the input full backups. Open this new folder through Library, '
                            'Open existing library folder.\n\n'
                            + json.dumps(dict(path=str(result.path), profile=result.profile,
                                preview_sha256=result.preview_sha256, choices=result.choices,
                                omitted=result.omitted), ensure_ascii=False, indent=2), owner)
                self._queue('Merging the reviewed research snapshots', merge, finished, owner, token)
            queued = self._queue('Verifying research merge inputs and conflicts',
                lambda: action.prepare(cancelled=lambda: self._cancelled(owner, token)), prepared, owner, token)
            return queued
        except Exception as error:
            owner.report(error)
            return False
        finally:
            if not queued:
                self._release(token)


def install_research_merge_actions(frame):
    import wx
    existing = getattr(frame, 'research_merge_actions', None)
    if existing is not None:
        return existing
    bar = frame.GetMenuBar()
    count = bar.GetMenuCount() if bar is not None else 0
    if not 0 < count <= 64:
        raise ValueError('The current Library menu is unavailable.')
    menus = [bar.GetMenu(index) for index in range(count)
             if bar.GetMenuLabel(index).replace('&', '') == 'Library']
    if len(menus) != 1:
        raise ValueError('The current Library menu must be present exactly once.')
    actions = ResearchMergeActions(frame)
    native_id = frame.GetId()
    def destroyed(event):
        if event.GetId() == native_id:
            actions.withdraw()
        event.Skip()
    frame.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    item = menus[0].Append(wx.ID_ANY, 'Merge research content from two backups…')
    frame.Bind(wx.EVT_MENU, actions.start, item)
    register_menu_command(frame, MERGE_COMMAND, item)
    frame.research_merge_actions = actions
    return actions
