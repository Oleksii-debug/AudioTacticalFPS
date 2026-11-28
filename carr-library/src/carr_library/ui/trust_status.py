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
        if controller.update_endpoint is not None:
            self.background_checks = wx.CheckBox(self,
                label='Check the configured update source while idle; never download automatically')
            self.background_checks.SetName('Allow idle signed-update checks')
            self.background_checks.SetValue(controller.background_update_checks)
            self.background_checks.Bind(wx.EVT_CHECKBOX, self.on_background_checks)
            layout.Add(self.background_checks, 0, wx.ALL, 8)
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
            if controller.update_endpoint is not None:
                add('Check configured website for updates', self.on_online_update)
            if controller.provisioning is not None:
                add('Prepare update host', self.on_prepare_host)
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

    def on_background_checks(self, event=None):
        if not self._current():
            return
        enabled = bool(self.background_checks.GetValue())
        if enabled and wx.MessageBox(
                'Allow at most one signed metadata check each hour while this application is idle? '
                'Only the configured update site is contacted. No package is downloaded or installed, '
                'and the setting does not change license renewal.',
                'Allow idle update checks', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            self.background_checks.SetValue(False)
            return
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        def finished(value):
            install_update_check_timer(self.frame)
            self.frame.report('Idle update checks are enabled.' if value else 'Update checks remain manual.')
        _run_update_job(self.frame, self.controller, 'Save update-check consent',
            lambda cancelled, progress: self.controller.set_background_update_checks(
                enabled, accepted=enabled, cancelled=cancelled), done=finished)

    def on_online_update(self, event=None):
        if not self._current() or self.controller.update_endpoint is None:
            return
        repository = self.controller.update_endpoint
        self.EndModal(wx.ID_OK)
        _run_update_job(self.frame, self.controller, 'Check configured signed update',
            lambda cancelled, progress: self.controller.check_repository(repository, cancelled=cancelled),
            done=lambda release: show_update_review(self.frame, self.controller, repository, release))

    def on_prepare_host(self, event=None):
        if not self._current() or self.controller.provisioning is None:
            return
        recipe = self.controller.provisioning
        message = ('Prepare a separate maintenance runtime from the exact approved current installation?\n\n'
                   'Destination: ' + str(recipe.destination) + '\nSource bundle SHA256: '
                   + recipe.source['bundle_sha256']
                   + '\n\nThis uses the existing offline installer. The current program, private models '
                     'and research library are not copied or replaced. A failed attempt is retained for review.')
        if wx.MessageBox(message, 'Prepare update host',
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
            return
        if not self._current():
            return
        self.EndModal(wx.ID_OK)
        _run_update_job(self.frame, self.controller, 'Prepare external update host',
            lambda cancelled, progress: self.controller.prepare_update_host(cancelled=cancelled),
            done=lambda value: self.frame.report('Update host prepared.\nConfiguration: '
                + value['configuration'] + '\nConfiguration SHA256: ' + value['configuration_sha256']))

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
    dialog.FindWindow(wx.ID_OK).SetLabel('Download or resume verified package')
    dialog.FindWindow(wx.ID_OK).SetName('Download or resume verified package')
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    dialog.SetSizer(layout)
    text.SetFocus()
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
    finally:
        dialog.Destroy()
    _run_update_job(frame, controller, 'Download or resume signed update',
        lambda cancelled, progress: controller.download_review(repository, release,
            cancelled=cancelled, progress=progress),
        done=lambda package: show_prepared_update(frame, controller, repository, package))


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
    update_item = menu_bar.GetMenu(help_index).Append(wx.ID_ANY, 'Check for updates / Update')
    frame.Bind(wx.EVT_MENU, lambda event: show_trust_status(frame), update_item)
    register_menu_command(frame, CommandSpec('delivery.update',
                          'Check for updates / Update', 'main', ''), update_item)
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


def _update_scope_current(frame, controller):
    base = _scope_current(frame, controller)
    def current():
        if not base():
            return False
        if controller.update_library is not None and frame.library is not controller.update_library:
            return False
        try:
            controller._update_current()
        except (ValueError, OSError, RuntimeError):
            return False
        return True
    return current


def _run_update_job(frame, controller, title, operation, *, done):
    """Use the existing progress/cancellation owner for updates too."""
    current = _update_scope_current(frame, controller)
    if not current():
        return None

    def stopped():
        return not current() or frame.cancel_event.is_set()

    def work(progress):
        if stopped():
            raise TrustError('The update review is no longer current.')
        def received(completed, total, phase=None):
            progress(completed, total, phase or 'Received bytes; full signed-target verification remains required')
        return operation(stopped, received)

    def failed(error):
        if not current():
            return
        if getattr(error, 'update_host_prepared', None) is not None:
            value = error.update_host_prepared
            frame.report('The update host configuration was published. Final observation needs review.\n'
                         + value['configuration'] + '\nSHA256: ' + value['configuration_sha256'])
        else:
            frame.report('Update operation stopped: ' + str(error)
                         + '\nThe current installation is retained. An interrupted download keeps only '
                           'an unverified private prefix; Download or resume verifies the complete signed target.')
    return frame.run_job(title, work, with_progress=True, progress_unit='bytes',
        done=lambda value: done(value) if current() else None, failed=failed)


def show_prepared_update(frame, controller, repository, package):
    current = _update_scope_current(frame, controller)
    if not current():
        return
    if controller.handoff is None:
        frame.report('Verified package retained: ' + str(package.path)
            + '\nApply needs the explicitly configured external update host. '
              'Use Help, License and signed updates, Prepare update host when an owner recipe is configured.')
        return
    dialog = wx.Dialog(frame, title='Apply reviewed update', size=(760, 560),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    message = (release_text(package.release) + '\n\nApply first prepares a verified offline handoff. '
        'When the separate host is ready, this application will ask its existing windows to close normally. '
        'Unsaved-work refusal keeps the application open and cancels the handoff. The host waits for '
        'the real installation lease, creates a verified backup, applies the standard setup and checks health. '
        'A failure keeps the exact prior version or records the existing recovery transaction.')
    text = wx.TextCtrl(dialog, value=message, style=wx.TE_MULTILINE | wx.TE_READONLY)
    text.SetName('Reviewed update and backup handoff')
    layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
    restart = wx.CheckBox(dialog, label='Restart this verified library after successful update')
    restart.SetValue(False)
    restart.SetName('Restart after verified update')
    layout.Add(restart, 0, wx.ALL, 8)
    buttons = dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
    dialog.FindWindow(wx.ID_OK).SetLabel('Close application and apply this update')
    dialog.FindWindow(wx.ID_OK).SetName('Close application and apply this update')
    dialog.FindWindow(wx.ID_CANCEL).SetDefault()
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    dialog.SetSizer(layout)
    text.SetFocus()
    try:
        if dialog.ShowModal() != wx.ID_OK or not current():
            return
        should_restart = bool(restart.GetValue())
    finally:
        dialog.Destroy()
    prepare_update_apply(frame, controller, repository, package, restart=should_restart)


def prepare_update_apply(frame, controller, repository, package, *, restart=False):
    """Transfer ownership only after the actual idle host accepts normal Close."""
    current = _update_scope_current(frame, controller)
    if not current() or getattr(frame, 'working', False):
        return None
    owned = {}
    def stopped():
        return not current() or frame.cancel_event.is_set()
    def operation():
        if stopped():
            raise TrustError('The approved update review is no longer current.')
        owner = controller.prepare_apply(repository, package, restart=restart, cancelled=stopped)
        unregister = []
        def cleanup():
            owner.abort()
            if unregister and (owner.closed or owner.transferred):
                unregister[0]()
        try:
            unregister.append(frame.register_worker_cleanup(cleanup))
        except BaseException:
            owner.abort()
            raise
        owned[owner] = cleanup
        if stopped():
            cleanup()
            raise InterruptedError('Update preparation cancelled before desktop Close.')
        return owner

    def abort(owner, message):
        # The existing worker performs finite owned supervisor settlement. The
        # global uncommitted registry also retains it if the host is destroyed
        # before this queued cleanup can run.
        if not getattr(frame, '_closing', False):
            frame.run_job('Cancel prepared update handoff', owned[owner],
                done=lambda value: frame.report(message),
                failed=lambda error: frame.report(message + '\nOwned helper cleanup needs attention: ' + str(error)))
        else:
            owned[owner]()

    def done(owner):
        if not current() or frame.cancel_event.is_set():
            abort(owner, 'The update handoff was cancelled before application replacement.')
            return
        if getattr(frame, 'working', False):
            abort(owner, 'Finish or cancel the current operation before applying the reviewed update.')
            return
        try:
            # DesktopExecutor queues registered owner cleanup during Close.
            # Hold the same owner lock through the explicit COMMIT so that
            # finalizer cannot terminate a helper between Close and transfer.
            with owner.desktop_close_guard():
                accepted = frame.Close()
                if accepted is not True or not getattr(frame, '_closing', False):
                    abort(owner, 'Application Close was refused. The prepared update was cancelled.')
                    return
                receipt = owner.commit_after_close(host_closed=True)
                frame.update_handoff_receipt = receipt
        except BaseException as error:
            receipt = getattr(error, 'update_handoff', None)
            if receipt is not None:
                frame.update_handoff_receipt = receipt
            elif not owner.transferred:
                abort(owner, 'The update was not handed to the maintenance host.')
            raise

    future = frame.run_job('Prepare reviewed update handoff', operation, done=done)
    if future is not None:
        def settled(result):
            # Plain Future settlement remains active even if wx suppresses its
            # completion callback during Close. This runs on the current host
            # worker; no second executor, daemon or detached cleanup is created.
            try:
                owner = result.result()
            except BaseException:
                return
            if stopped() and not owner.transferred:
                owned[owner]()
        future.add_done_callback(settled)
    return future


def install_update_check_timer(frame):
    """Optional metadata checks on the one existing idle host worker."""
    if getattr(frame, '_update_check_timer_installed', False):
        return
    frame._update_check_timer_installed = True
    timer = wx.Timer(frame)
    frame.update_check_timer = timer
    window_id = frame.GetId()
    def tick(event=None):
        controller = getattr(frame, 'trust_controller', None)
        if (controller is None or getattr(frame, '_closing', False)
                or getattr(frame, 'working', False)
                or not controller.background_update_due()):
            return
        current = _update_scope_current(frame, controller)
        if not current() or not controller.reserve_background_update_check():
            return
        def done(release):
            if current():
                # Retain a readable status; never open a modal, move focus,
                # download a target or ask running work to close in the timer.
                controller.record_background_update(release)
        def failed(error):
            if current():
                controller._notice = 'The idle update check was unavailable. The next check remains bounded; manual checking and local work stay available.'
        # Borrow the same serial executor without run_job's focus-changing
        # foreground status. A user job may queue behind this at most60-second
        # metadata check; its admission or root change cooperatively stops us.
        from time import monotonic
        deadline = monotonic() + 60
        def background():
            if not current() or getattr(frame, 'working', False):
                raise InterruptedError('The host is no longer idle.')
            return controller.check_repository(controller.update_endpoint,
                cancelled=lambda: (not current() or getattr(frame, 'working', False)
                    or frame.cancel_event.is_set() or monotonic() >= deadline))
        try:
            future = frame.executor.submit(background)
        except RuntimeError:
            return
        frame.update_check_future = future
        def settled(value):
            try:
                release = value.result()
            except BaseException as error:
                wx.CallAfter(failed, error)
            else:
                wx.CallAfter(done, release)
        future.add_done_callback(settled)
    def closed(event):
        if event.GetId() == window_id:
            timer.Stop()
        event.Skip()
    frame.Bind(wx.EVT_TIMER, tick, timer)
    frame.Bind(wx.EVT_WINDOW_DESTROY, closed)
    timer.Start(60000)
