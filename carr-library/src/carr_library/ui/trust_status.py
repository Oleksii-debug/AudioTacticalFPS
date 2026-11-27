"""Native readable status and functional signed-file/repository review."""
from __future__ import annotations

import wx

from ..security.controller import TrustController, release_text
from ..security.refresh import IdleRenewalHost


class GrantReviewDialog(wx.Dialog):
    def __init__(self, parent, review):
        super().__init__(parent, title='Review signed permission', size=(750, 600),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.review = review
        layout = wx.BoxSizer(wx.VERTICAL)
        self.text = wx.TextCtrl(self, value=review.description, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetName('Signed permission and owner terms')
        layout.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
        self.accepted = wx.CheckBox(self, label='I reviewed the permission and terms above')
        self.accepted.SetName('Accept reviewed permission and terms')
        layout.Add(self.accepted, 0, wx.ALL, 8)
        self.import_button = wx.Button(self, wx.ID_OK, label='Import this signed permission')
        self.import_button.SetName('Import this signed permission')
        self.import_button.Disable()
        self.accepted.Bind(wx.EVT_CHECKBOX, lambda event: self.import_button.Enable(self.accepted.GetValue()))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.import_button, 0, wx.ALL, 6)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, label='Cancel'), 0, wx.ALL, 6)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.text.SetFocus()


class TrustStatusDialog(wx.Dialog):
    def __init__(self, frame, controller, *, status_text=None):
        super().__init__(frame, title='License and signed updates', size=(700, 450),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame, self.controller = frame, controller
        layout = wx.BoxSizer(wx.VERTICAL)
        # A configured store may call the OS vault. Its snapshot is prepared
        # by the existing job executor before this native dialog is shown.
        if status_text is None:
            status_text = (controller.status_text() if controller.entitlement is None
                           else 'Choose Refresh license status to verify the saved permission.')
        self.text = wx.TextCtrl(self, value=status_text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text.SetName('License and update status')
        layout.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
        self.buttons = []
        actions = wx.BoxSizer(wx.HORIZONTAL)
        def add(label, callback):
            button = wx.Button(self, label=label)
            button.SetName(label)
            button.Bind(wx.EVT_BUTTON, callback)
            actions.Add(button, 0, wx.ALL, 5)
            self.buttons.append(button)
        if controller.entitlement is not None:
            add('Refresh license status', self.on_status)
            add('Import signed permission file', self.on_import)
        if controller.refresh is not None:
            add('Check renewal now', self.on_refresh)
        if controller.updates is not None:
            add('Check offline update repository', self.on_update)
        actions.Add(wx.Button(self, wx.ID_CANCEL, label='Close'), 0, wx.ALL, 5)
        layout.Add(actions, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.text.SetFocus()

    def on_status(self, event=None):
        self.EndModal(wx.ID_OK)
        _read_status(self.frame, self.controller)

    def on_import(self, event=None):
        with wx.FileDialog(self, 'Choose the signed permission file', wildcard='JSON documents (*.json)|*.json',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = chooser.GetPath()
        self.EndModal(wx.ID_OK)
        self.frame.run_job('Verify signed permission file',
            lambda: self.controller.preview_file(path),
            done=lambda review: show_permission_review(self.frame, self.controller, review),
            failed=lambda error: self.frame.report('The signed permission could not be verified. Check its publisher, product, device, validity and matching terms.'))

    def on_refresh(self, event=None):
        self.EndModal(wx.ID_OK)
        self.frame.run_job('Check signed renewal',
            lambda: self.controller.check_renewal(cancelled=self.frame.cancel_event.is_set),
            done=self.frame.report,
            failed=lambda error: self.frame.report('Renewal was cancelled or unavailable. The last verified permission and access to existing work are retained.'))

    def on_update(self, event=None):
        with wx.DirDialog(self, 'Choose the signed offline repository folder') as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            repository = chooser.GetPath()
        self.EndModal(wx.ID_OK)
        self.frame.run_job('Check signed offline update',
            lambda: self.controller.check_repository(repository, cancelled=self.frame.cancel_event.is_set),
            done=lambda release: show_update_review(self.frame, self.controller, repository, release))


def show_permission_review(frame, controller, review):
    with GrantReviewDialog(frame, review) as dialog:
        if dialog.ShowModal() != wx.ID_OK or not dialog.accepted.GetValue():
            return
    frame.run_job('Import reviewed signed permission',
        lambda: controller.import_review(review, accepted=True, cancelled=frame.cancel_event.is_set),
        done=lambda grant: frame.report('The reviewed signed permission was imported. Use Help, License and signed updates to read its current status.'),
        failed=lambda error: frame.report('The permission could not be imported. The previous verified permission remains available for recovery.'))


def show_update_review(frame, controller, repository, release):
    dialog = wx.Dialog(frame, title='Review signed update', size=(740, 540),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dialog, value=release_text(release), style=wx.TE_MULTILINE | wx.TE_READONLY)
    text.SetName('Verified update details')
    layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
    buttons = dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
    dialog.FindWindow(wx.ID_OK).SetLabel('Download verified package')
    dialog.FindWindow(wx.ID_OK).SetName('Download verified package')
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    dialog.SetSizer(layout)
    text.SetFocus()
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
    finally:
        dialog.Destroy()
    frame.run_job('Prepare signed update package',
        lambda: controller.download_review(repository, release, cancelled=frame.cancel_event.is_set),
        done=lambda package: frame.report('Verified update package prepared: ' + str(package.path)
            + '\nThe running application and open work are unchanged. Use the owner-provided installation procedure for activation.'))


def _display_status(frame, controller, value=None):
    previous = wx.Window.FindFocus()
    try:
        with TrustStatusDialog(frame, controller, status_text=value) as dialog:
            dialog.ShowModal()
    finally:
        if previous is not None:
            try:
                if previous.IsShown() and previous.IsEnabled():
                    previous.SetFocus()
            except RuntimeError:
                pass


def _read_status(frame, controller):
    frame.run_job('Read signed client status', controller.status_text,
        done=lambda value: _display_status(frame, controller, value),
        failed=lambda error: frame.report('Signed client status is unavailable. Reading, saving, export, backup and recovery remain available.'))


def show_trust_status(frame):
    controller = getattr(frame, 'trust_controller', None) or TrustController()
    if controller.entitlement is None:
        _display_status(frame, controller)
    else:
        _read_status(frame, controller)


def install_trust_actions(frame):
    menu_bar = frame.GetMenuBar()
    help_index = next((index for index in range(menu_bar.GetMenuCount())
                       if menu_bar.GetMenuLabelText(index) == 'Help'), None)
    if help_index is None:
        raise ValueError('The host Help menu is unavailable.')
    item = menu_bar.GetMenu(help_index).Append(wx.ID_ANY, 'License and signed updates')
    frame.Bind(wx.EVT_MENU, lambda event: show_trust_status(frame), item)
    controller = getattr(frame, 'trust_controller', None)
    if controller is None or controller.refresh is None:
        return
    host = IdleRenewalHost(controller.refresh, enabled=controller.automatic_refresh)
    frame.trust_renewal_host = host
    timer = wx.Timer(frame)
    frame.trust_renewal_timer = timer
    def idle(event=None):
        host.set_idle(not getattr(frame, 'working', False) and not getattr(frame, '_closing', False))
    def close(event):
        if event.GetEventObject() is frame:
            timer.Stop()
            host.close()
        event.Skip()
    frame.Bind(wx.EVT_TIMER, idle, timer)
    frame.Bind(wx.EVT_WINDOW_DESTROY, close)
    if controller.automatic_refresh:
        idle()
        host.start()
        timer.Start(1000)
