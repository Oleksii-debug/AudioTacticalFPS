"""Native readable status and functional signed-file/repository review."""
from __future__ import annotations

import wx

from ..security._files import TrustError
from ..security.controller import TrustController, release_text
from ..security.refresh import IdleRenewalHost
from .command_registry import CommandSpec
from .keyboard_settings import register_menu_command


def _scope_current(frame, controller):
    """Bind an explicit review to this live host and selected client."""
    library = getattr(frame, 'library', None)
    root = str(getattr(library, 'root', ''))
    configured = getattr(frame, 'trust_controller', None)

    def current():
        active = getattr(frame, 'library', None)
        return (not getattr(frame, '_closing', False)
                and active is library
                and str(getattr(active, 'root', '')) == root
                and getattr(frame, 'trust_controller', None) is configured
                and (configured is None or configured is controller))
    return current


def _run_job(frame, controller, title, operation, *, done=None, failed=None, current=None):
    """Use the existing host worker; never apply a queued stale review."""
    current = current or _scope_current(frame, controller)

    def cancelled():
        return not current() or frame.cancel_event.is_set()

    def work():
        if cancelled():
            raise TrustError('The signed-client operation was cancelled or its host changed.')
        return operation(cancelled)

    def finish(value):
        if current() and done is not None:
            done(value)

    def failure(error):
        if current() and failed is not None:
            failed(error)

    if not current():
        return
    frame.run_job(title, work, done=finish, failed=failure if failed is not None else None)


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
        self._current = _scope_current(frame, controller)
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
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        _read_status(self.frame, self.controller, current=self._current)

    def on_import(self, event=None):
        if not self._current():
            return
        with wx.FileDialog(self, 'Choose the signed permission file', wildcard='JSON documents (*.json)|*.json',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            path = chooser.GetPath()
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, self.controller, 'Verify signed permission file',
            lambda cancelled: self.controller.preview_file(path),
            done=lambda review: show_permission_review(self.frame, self.controller, review),
            failed=lambda error: self.frame.report('The signed permission could not be verified. Check its publisher, product, device, validity and matching terms.'),
            current=self._current)

    def on_refresh(self, event=None):
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, self.controller, 'Check signed renewal',
            lambda cancelled: self.controller.check_renewal(cancelled=cancelled),
            done=self.frame.report,
            failed=lambda error: self.frame.report('Renewal was cancelled or unavailable. The last verified permission and access to existing work are retained.'),
            current=self._current)

    def on_update(self, event=None):
        if not self._current():
            return
        with wx.DirDialog(self, 'Choose the signed offline repository folder') as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            repository = chooser.GetPath()
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, self.controller, 'Check signed offline update',
            lambda cancelled: self.controller.check_repository(repository, cancelled=cancelled),
            done=lambda release: show_update_review(self.frame, self.controller, repository, release),
            current=self._current)


def show_permission_review(frame, controller, review):
    current = _scope_current(frame, controller)
    if not current():
        return
    with GrantReviewDialog(frame, review) as dialog:
        if dialog.ShowModal() != wx.ID_OK or not dialog.accepted.GetValue():
            return
    _run_job(frame, controller, 'Import reviewed signed permission',
        lambda cancelled: controller.import_review(review, accepted=True, cancelled=cancelled),
        done=lambda grant: frame.report('The reviewed signed permission was imported. Use Help, License and signed updates to read its current status.'),
        failed=lambda error: frame.report('The permission could not be imported. The previous verified permission remains available for recovery.'),
        current=current)


def show_update_review(frame, controller, repository, release):
    current = _scope_current(frame, controller)
    if not current():
        return
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
    _run_job(frame, controller, 'Prepare signed update package',
        lambda cancelled: controller.download_review(repository, release, cancelled=cancelled),
        done=lambda package: frame.report('Verified update package prepared: ' + str(package.path)
            + '\nThe running application and open work are unchanged. Use the owner-provided installation procedure for activation.'),
        current=current)


def _display_status(frame, controller, value=None):
    current = _scope_current(frame, controller)
    if not current():
        return
    previous = wx.Window.FindFocus()
    try:
        with TrustStatusDialog(frame, controller, status_text=value) as dialog:
            dialog.ShowModal()
    finally:
        if current() and previous is not None:
            try:
                if previous.IsShown() and previous.IsEnabled():
                    previous.SetFocus()
            except RuntimeError:
                pass


def _read_status(frame, controller, *, current=None):
    _run_job(frame, controller, 'Read signed client status', lambda cancelled: controller.status_text(),
        done=lambda value: _display_status(frame, controller, value),
        failed=lambda error: frame.report('Signed client status is unavailable. Reading, saving, export, backup and recovery remain available.'),
        current=current)


def show_trust_status(frame):
    controller = getattr(frame, 'trust_controller', None) or TrustController()
    if controller.entitlement is None:
        _display_status(frame, controller)
    else:
        _read_status(frame, controller)


def install_trust_actions(frame):
    if getattr(frame, '_trust_actions_installed', False):
        return
    menu_bar = frame.GetMenuBar()
    help_index = next((index for index in range(menu_bar.GetMenuCount())
                       if menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'help'), None)
    if help_index is None:
        raise ValueError('The host Help menu is unavailable.')
    item = menu_bar.GetMenu(help_index).Append(wx.ID_ANY, 'License and signed updates')
    frame.Bind(wx.EVT_MENU, lambda event: show_trust_status(frame), item)
    register_menu_command(frame, CommandSpec('delivery.trust_status',
                          'License and signed updates', 'main', ''), item)
    frame._trust_actions_installed = True
    controller = getattr(frame, 'trust_controller', None)
    if controller is None or controller.refresh is None:
        return
    host = IdleRenewalHost(controller.refresh, enabled=controller.automatic_refresh)
    frame.trust_renewal_host = host
    window_id = frame.GetId()
    timer = wx.Timer(frame)
    frame.trust_renewal_timer = timer
    def idle(event=None):
        host.set_idle(not getattr(frame, 'working', False) and not getattr(frame, '_closing', False))
    def close(event):
        if event.GetId() == window_id:
            timer.Stop()
            host.close()
        event.Skip()
    frame.Bind(wx.EVT_TIMER, idle, timer)
    frame.Bind(wx.EVT_WINDOW_DESTROY, close)
    if controller.automatic_refresh:
        idle()
        host.start()
        timer.Start(1000)
