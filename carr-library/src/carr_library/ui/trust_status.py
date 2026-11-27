"""Native readable status and functional signed-file/repository review."""
from __future__ import annotations

import wx

from ..security.controller import TrustController, release_text
from ..security.refresh import IdleRenewalHost


def _alive(frame):
    return getattr(frame, '_trust_alive', False) and not getattr(frame, '_closing', False)


def _cancelled(frame):
    return not _alive(frame) or frame.cancel_event.is_set()


def _dialog_lifetime(dialog):
    owner_id = dialog.GetId()
    state = {'alive': True}
    def destroyed(event):
        if event.GetId() == owner_id:
            state['alive'] = False
        event.Skip()
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    return state


def _dispose(dialog, state):
    if state['alive']:
        dialog.Destroy()


def _run_job(frame, title, work, *, done, failed):
    """Detach callbacks while keeping the host's existing executor and token."""
    if not _alive(frame):
        return
    def invoke():
        if _cancelled(frame):
            raise InterruptedError('Signed client work was cancelled before it started.')
        return work()
    def completed(value):
        if _alive(frame):
            done(value)
    def unavailable(error):
        if _alive(frame):
            failed(error)
    frame.run_job(title, invoke, done=completed, failed=unavailable)


class GrantReviewDialog(wx.Dialog):
    def __init__(self, parent, review):
        super().__init__(parent, title='Review signed permission', size=(750, 600),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._trust_lifetime = _dialog_lifetime(self)
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
        def acceptance(event):
            if self._trust_lifetime['alive'] and _alive(parent):
                self.import_button.Enable(self.accepted.GetValue())
        self.accepted.Bind(wx.EVT_CHECKBOX, acceptance)
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
        self._trust_lifetime = _dialog_lifetime(self)
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
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        self.EndModal(wx.ID_OK)
        _read_status(self.frame, self.controller)

    def on_import(self, event=None):
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        chooser = wx.FileDialog(self, 'Choose the signed permission file', wildcard='JSON documents (*.json)|*.json',
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        state = _dialog_lifetime(chooser)
        try:
            if chooser.ShowModal() != wx.ID_OK or not state['alive'] or not _alive(self.frame):
                return
            path = chooser.GetPath()
        finally:
            _dispose(chooser, state)
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, 'Verify signed permission file',
            lambda: self.controller.preview_file(path),
            done=lambda review: show_permission_review(self.frame, self.controller, review),
            failed=lambda error: self.frame.report('The signed permission could not be verified. Check its publisher, product, device, validity and matching terms.'))

    def on_refresh(self, event=None):
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, 'Check signed renewal',
            lambda: self.controller.check_renewal(cancelled=lambda: _cancelled(self.frame)),
            done=self.frame.report,
            failed=lambda error: self.frame.report('Renewal was cancelled or unavailable. The last verified permission and access to existing work are retained.'))

    def on_update(self, event=None):
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        chooser = wx.DirDialog(self, 'Choose the signed offline repository folder')
        state = _dialog_lifetime(chooser)
        try:
            if chooser.ShowModal() != wx.ID_OK or not state['alive'] or not _alive(self.frame):
                return
            repository = chooser.GetPath()
        finally:
            _dispose(chooser, state)
        if not self._trust_lifetime['alive'] or not _alive(self.frame):
            return
        self.EndModal(wx.ID_OK)
        _run_job(self.frame, 'Check signed offline update',
            lambda: self.controller.check_repository(repository, cancelled=lambda: _cancelled(self.frame)),
            done=lambda release: show_update_review(self.frame, self.controller, repository, release),
            failed=lambda error: self.frame.report('The signed offline update could not be verified. The running application is unchanged.'))


def show_permission_review(frame, controller, review):
    if not _alive(frame):
        return
    dialog = GrantReviewDialog(frame, review)
    try:
        if (dialog.ShowModal() != wx.ID_OK or not dialog._trust_lifetime['alive']
                or not _alive(frame) or not dialog.accepted.GetValue()):
            return
    finally:
        _dispose(dialog, dialog._trust_lifetime)
    _run_job(frame, 'Import reviewed signed permission',
        lambda: controller.import_review(review, accepted=True, cancelled=lambda: _cancelled(frame)),
        done=lambda grant: frame.report('The reviewed signed permission was imported. Use Help, License and signed updates to read its current status.'),
        failed=lambda error: frame.report('The permission could not be imported. The previous verified permission remains available for recovery.'))


def show_update_review(frame, controller, repository, release):
    if not _alive(frame):
        return
    dialog = wx.Dialog(frame, title='Review signed update', size=(740, 540),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    state = _dialog_lifetime(dialog)
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
        if dialog.ShowModal() != wx.ID_OK or not state['alive'] or not _alive(frame):
            return
    finally:
        _dispose(dialog, state)
    _run_job(frame, 'Prepare signed update package',
        lambda: controller.download_review(repository, release, cancelled=lambda: _cancelled(frame)),
        done=lambda package: frame.report('Verified update package prepared: ' + str(package.path)
            + '\nThe running application and open work are unchanged. Use the owner-provided installation procedure for activation.'),
        failed=lambda error: frame.report('The verified package was not prepared. Review the signed repository again before retrying.'))


def _display_status(frame, controller, value=None):
    if not _alive(frame):
        return
    previous = wx.Window.FindFocus()
    dialog = TrustStatusDialog(frame, controller, status_text=value)
    try:
        dialog.ShowModal()
    finally:
        _dispose(dialog, dialog._trust_lifetime)
        if _alive(frame) and previous is not None:
            try:
                if previous.IsShown() and previous.IsEnabled():
                    previous.SetFocus()
            except RuntimeError:
                pass


def _read_status(frame, controller):
    _run_job(frame, 'Read signed client status', controller.status_text,
        done=lambda value: _display_status(frame, controller, value),
        failed=lambda error: frame.report('Signed client status is unavailable. Reading, saving, export, backup and recovery remain available.'))


def show_trust_status(frame):
    if not _alive(frame):
        return
    controller = getattr(frame, 'trust_controller', None) or TrustController()
    if controller.entitlement is None:
        _display_status(frame, controller)
    else:
        _read_status(frame, controller)


def install_trust_actions(frame):
    owner_id = frame.GetId()
    frame._trust_alive = True
    hosts, timer = [], None
    def close(event):
        if event.GetId() == owner_id and frame._trust_alive:
            frame._trust_alive = False
            if timer is not None:
                try:
                    timer.Stop()
                except RuntimeError:
                    pass  # Native owner teardown can delete its timer first.
            for host in hosts:
                host.close()
        event.Skip()
    frame.Bind(wx.EVT_WINDOW_DESTROY, close)
    menu_bar = frame.GetMenuBar()
    help_index = next((index for index in range(menu_bar.GetMenuCount())
                       if menu_bar.GetMenuLabelText(index) == 'Help'), None)
    if help_index is None:
        raise ValueError('The host Help menu is unavailable.')
    item = menu_bar.GetMenu(help_index).Append(wx.ID_ANY, 'License and signed updates')
    frame.Bind(wx.EVT_MENU, lambda event: show_trust_status(frame), item)
    controller = getattr(frame, 'trust_controller', None)
    if controller is None:
        return
    if controller.refresh is not None:
        renewal = IdleRenewalHost(controller.refresh, enabled=controller.automatic_refresh)
        frame.trust_renewal_host = renewal
        hosts.append(renewal)
    if controller.update_poll is not None:
        frame.trust_update_poll_host = controller.update_poll
        hosts.append(controller.update_poll)
    if not hosts:
        return
    timer = wx.Timer(frame)
    frame.trust_client_timer = timer
    if controller.refresh is not None:
        frame.trust_renewal_timer = timer
    def idle(event=None):
        if not _alive(frame):
            return
        available = not getattr(frame, 'working', False) and not getattr(frame, '_closing', False)
        for host in hosts:
            host.set_idle(available)
    frame.Bind(wx.EVT_TIMER, idle, timer)
    if any(host.enabled for host in hosts):
        idle()
        for host in hosts:
            host.start()
        timer.Start(1000)
