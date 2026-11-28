"""One native review of caller-owned backup limits; no saved archive authority."""
from dataclasses import dataclass

from ..backup_budget import BackupBudget
from ..backup_options import FIELDS, NOTICE, parse_resource_values, resource_values


@dataclass(frozen=True)
class BackupOptions:
    include_generated: bool
    budget: BackupBudget | None = None

    def __post_init__(self):
        if type(self.include_generated) is not bool:
            raise ValueError('Choose generated-audio inclusion explicitly.')
        if self.budget is not None and type(self.budget) is not BackupBudget:
            raise ValueError('Review an exact backup resource policy.')


def create_backup_options(parent, *, restore=False, guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class OptionsDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Restore resources' if restore else 'Backup options',
                size=(760, 680), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive = True
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.result = None
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.Bind(wx.EVT_CLOSE, self.cancel)
            body = wx.ScrolledWindow(self)
            body.SetScrollRate(10, 10)
            layout = wx.BoxSizer(wx.VERTICAL)
            self.include_audio = wx.CheckBox(body, label='Include generated audio stored inside this library')
            self.include_audio.SetName('Include generated audio stored inside this library')
            self.include_audio.SetValue(not restore)
            self.include_audio.Show(not restore)
            layout.Add(self.include_audio, 0, wx.EXPAND | wx.ALL, 8)
            scope = wx.StaticText(body, label=('Libraries, reading state, project settings and pending inputs are included. '
                'External exports, engine/model packs and operating-system credentials are separate.'))
            scope.Wrap(680)
            layout.Add(scope, 0, wx.EXPAND | wx.ALL, 8)
            self.reviewed = wx.CheckBox(body, label='Use reviewed resource limits for this operation')
            self.reviewed.SetName('Use reviewed backup resource limits')
            self.reviewed.SetValue(False)
            layout.Add(self.reviewed, 0, wx.EXPAND | wx.ALL, 8)
            note = wx.StaticText(body, label=NOTICE)
            note.Wrap(680)
            layout.Add(note, 0, wx.EXPAND | wx.ALL, 8)
            values = resource_values()
            grid = wx.FlexGridSizer(cols=2, hgap=10, vgap=6)
            grid.AddGrowableCol(1, 1)
            self.fields = {}
            for name, _flag, label, _unit in FIELDS:
                grid.Add(wx.StaticText(body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                field = wx.TextCtrl(body, value=values[name])
                field.SetName('Backup resource: ' + label)
                self.fields[name] = field
                grid.Add(field, 1, wx.EXPAND)
            layout.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
            body.SetSizer(layout)
            self.body = body
            self.notice = wx.StaticText(self, label='Existing limits remain in use unless the review box is selected.')
            self.notice.SetName('Backup resource review status')
            shell = wx.BoxSizer(wx.VERTICAL)
            shell.Add(body, 1, wx.EXPAND)
            shell.Add(self.notice, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.accept = wx.Button(self, wx.ID_OK, 'Continue')
            self.accept.SetName('Accept reviewed backup options')
            self.accept.Bind(wx.EVT_BUTTON, self.confirm)
            cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
            cancel.SetName('Cancel backup resource review')
            cancel.Bind(wx.EVT_BUTTON, self.cancel)
            buttons.Add(self.accept, 0, wx.ALL, 6)
            buttons.Add(cancel, 0, wx.ALL, 6)
            shell.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(shell)
            self.SetMinSize((620, 480))
            self.SetEscapeId(wx.ID_CANCEL)
            self.reviewed.Bind(wx.EVT_CHECKBOX, self.toggle)
            self.toggle()
            (self.reviewed if restore else self.include_audio).SetFocus()

        def toggle(self, event=None):
            if not self.alive:
                return
            selected = self.reviewed.GetValue()
            for field in self.fields.values():
                field.Enable(selected)

        def confirm(self, event=None):
            if not self.alive:
                return
            try:
                guard()
                selected = (parse_resource_values({name: field.GetValue() for name, field in self.fields.items()})
                            if self.reviewed.GetValue() else None)
                self.result = BackupOptions(False if restore else self.include_audio.GetValue(), selected)
                guard()
            except (Exception, KeyboardInterrupt) as error:
                if self.alive:
                    self.notice.SetLabel(str(error))
                    self.notice.Wrap(max(300, self.GetClientSize().width - 30))
                    self.Layout()
                    self.reviewed.SetFocus()
                return
            self.EndModal(wx.ID_OK)

        def cancel(self, event=None):
            if not self.alive:
                return
            self.result = None
            self.alive = False
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

    return OptionsDialog()


def review_backup_options(parent, *, restore=False, guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    dialog = create_backup_options(parent, restore=restore, guard=guard, wx_module=wx_module)
    try:
        if dialog.ShowModal() != wx_module.ID_OK or not dialog or not dialog.alive:
            return None
        guard()
        return dialog.result
    finally:
        try:
            if dialog and not dialog.IsBeingDeleted():
                dialog.Destroy()
        except RuntimeError:
            pass
