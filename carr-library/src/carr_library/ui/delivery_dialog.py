"""Native license/update controls on the existing desktop worker and menu."""
from pathlib import Path

from ..delivery_cli import create_update_backup, status_text
from ..delivery_contract import Capability, provider_status
from ..delivery_service import DeliveryService
from ..research.export_file import write_binary_export
from .productivity import _plain


def show_delivery_dialog(frame, *, on_ready=None, check_on_open=False):
    import wx

    class DeliveryDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='License and updates', size=(790, 650),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.service, self.offer, self.prepared = frame.delivery, None, None
            self.running = False
            layout = wx.BoxSizer(wx.VERTICAL)
            self.status = wx.TextCtrl(self, value=
                'License renewal and software updates use separate private services. '
                'Choose Refresh status to read local state. Your saved work remains available.',
                style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('License and update status')
            layout.Add(self.status, 1, wx.EXPAND | wx.ALL, 8)
            self.controls = {}
            entries = (
                ('status', '&Refresh local status', self.local_status),
                ('renew', 'Renew signed &license...', self.renew),
                ('import_license', '&Import signed license...', self.import_license),
                ('export_license', '&Export signed license...', self.export_license),
                ('check', '&Check for updates...', self.check),
                ('stage', '&Download selected update...', self.stage),
                ('import_update', 'Import &signed offline update...', self.import_update),
                ('recover', 'Inspect update &recovery', self.recover),
                ('install', '&Back up all libraries and install...', self.install),
            )
            for key, label, handler in entries:
                button = wx.Button(self, label=label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, handler)
                self.controls[key] = button
                layout.Add(button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.cancel = wx.Button(self, label='Cancel current operation')
            self.cancel.Bind(wx.EVT_BUTTON, lambda event: frame.cancel_event.set())
            row.Add(self.cancel, 0, wx.ALL, 8)
            close = wx.Button(self, wx.ID_CANCEL, '&Close')
            close.Bind(wx.EVT_BUTTON, self.close)
            row.Add(close, 0, wx.ALL, 8)
            layout.Add(row, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.enable_controls()
            self.status.SetFocus()

        def enable_controls(self):
            ent = provider_status(self.service.entitlement, family='entitlement').capabilities
            upd = provider_status(self.service.updater, family='update').capabilities
            available = {
                'status': True, 'renew': Capability.SIGNED_RENEWAL in ent,
                'import_license': Capability.OFFLINE_LICENSE_IMPORT in ent,
                'export_license': Capability.OFFLINE_LICENSE_IMPORT in ent,
                'check': Capability.UPDATE_CHECK in upd,
                'stage': Capability.UPDATE_CHECK in upd and self.offer is not None,
                'import_update': Capability.OFFLINE_UPDATE_IMPORT in upd,
                'recover': Capability.UPDATE_RECOVERY in upd,
                'install': Capability.UPDATE_INSTALL in upd and self.prepared is not None,
            }
            for key, button in self.controls.items():
                button.Enable(not self.running and available[key])
            self.cancel.Enable(self.running)

        def run(self, title, operation, done):
            if self.running or frame.working or getattr(frame, 'busy', False):
                return
            self.running = True
            self.enable_controls()
            def finished(value):
                self.running = False
                try:
                    done(value)
                finally:
                    self.enable_controls()
                    self.status.SetFocus()
            def failed(exc):
                self.running = False
                self.status.ChangeValue(str(exc))
                self.enable_controls()
                self.status.SetFocus()
            frame.run_job(title, operation, finished, failed, with_progress=True)

        def approve_network(self, purpose):
            return wx.MessageBox(
                purpose + ' through the configured private service? No library contents are sent.',
                'Approve network operation', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                self) == wx.YES

        def choose_source(self, title):
            picker = wx.FileDialog(self, title, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                return Path(picker.GetPath()) if picker.ShowModal() == wx.ID_OK else None
            finally:
                picker.Destroy()

        def local_status(self, event=None):
            self.run('Reading local license status',
                lambda progress: self.service.snapshot(cancelled=frame.cancel_event.is_set),
                lambda value: self.status.ChangeValue(status_text(value)))

        def renew(self, event=None):
            if not self.approve_network('Renew the signed license'):
                return
            self.run('Renewing signed license', lambda progress: self.service.refresh(
                network_approved=True, cancelled=frame.cancel_event.is_set),
                lambda value: self.status.ChangeValue(status_text(value)))

        def import_license(self, event=None):
            source = self.choose_source('Import signed license or offline renewal')
            if source is not None:
                self.run('Verifying signed license', lambda progress: self.service.import_license(
                    source, cancelled=frame.cancel_event.is_set),
                    lambda value: self.status.ChangeValue(status_text(value)))

        def export_license(self, event=None):
            picker = wx.FileDialog(self, 'Export signed license to a new file',
                defaultFile='signed-license.json', style=wx.FD_SAVE)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                target = Path(picker.GetPath())
            finally:
                picker.Destroy()
            root = Path(frame.library.root)
            def export(progress):
                raw = self.service.export_license(cancelled=frame.cancel_event.is_set)
                write_binary_export(target, raw, private_root=root,
                    cancelled=frame.cancel_event.is_set, require_absent=True)
                return len(raw)
            self.run('Exporting signed license', export, lambda size:
                self.status.ChangeValue('Signed license exported to the selected new file.'))

        def show_offer(self, offer):
            self.offer = offer
            self.status.ChangeValue('No update is offered.' if offer is None else
                f'Available build: {offer.target_build_id}. Package: {offer.package_bytes} bytes. '
                f'SHA-256: {offer.package_sha256}. Review the package before choosing Download.')

        def check(self, event=None):
            if not self.approve_network('Check for software updates'):
                return
            self.run('Checking for updates', lambda progress: self.service.check_update(
                network_approved=True, cancelled=frame.cancel_event.is_set), self.show_offer)

        def show_prepared(self, value):
            self.prepared = value
            self.status.ChangeValue('There is no verified update to recover.' if value is None else
                f'Verified update prepared: {value.offer.target_build_id}. '
                f'{value.offer.package_bytes} bytes. SHA-256: {value.offer.package_sha256}. '
                'Installation requires a fresh approval and a complete backup in a new folder.')

        def stage(self, event=None):
            offer = self.offer
            if offer is None or not self.approve_network(
                    f'Download the reviewed {offer.package_bytes}-byte update'):
                return
            self.run('Downloading and verifying update', lambda progress: self.service.stage_update(
                offer, network_approved=True, cancelled=frame.cancel_event.is_set, progress=progress),
                self.show_prepared)

        def import_update(self, event=None):
            source = self.choose_source('Import signed offline update package')
            if source is not None:
                self.run('Verifying offline update', lambda progress: self.service.import_update(
                    source, cancelled=frame.cancel_event.is_set, progress=progress), self.show_prepared)

        def recover(self, event=None):
            self.run('Verifying update recovery', lambda progress:
                self.service.recover_update(cancelled=frame.cancel_event.is_set), self.show_prepared)

        def install(self, event=None):
            prepared = self.prepared
            if prepared is None:
                return
            from uuid import uuid4
            picker = wx.DirDialog(self, 'Choose a parent folder for the complete pre-update backup',
                style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                destination = Path(picker.GetPath()) / ('Resonance-before-update-' + uuid4().hex)
            finally:
                picker.Destroy()
            if wx.MessageBox(
                    f'Back up all libraries, projects and retained generated assets, then install '
                    f'build {prepared.offer.target_build_id}?\n'
                    f'Package SHA-256: {prepared.offer.package_sha256}\nBackup: {destination}\n'
                    'The private installer must preserve the previous application and user data.',
                    'Confirm reviewed update', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    self) != wx.YES:
                return
            library = frame.library
            approval = self.service.approve_install(prepared)
            def install(progress):
                return self.service.install_update(approval,
                    idle=lambda: frame.library is library and not getattr(frame, '_closing', False),
                    backup=lambda: create_update_backup(library, destination,
                        cancelled=frame.cancel_event.is_set),
                    cancelled=frame.cancel_event.is_set, progress=progress)
            def done(result):
                self.prepared = None
                self.status.ChangeValue('The verified update was installed. '
                    'The pre-update backup remains available. Restart when ready.')
            self.run('Backing up and installing update', install, done)

        def close(self, event=None):
            if self.running:
                frame.cancel_event.set()
                self.status.ChangeValue('Stopping the operation. Wait for its verified result before closing.')
                if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return
            self.EndModal(wx.ID_CANCEL)

    dialog = DeliveryDialog()
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog)
        elif check_on_open and dialog.controls['check'].IsEnabled():
            wx.CallAfter(dialog.check)
        dialog.ShowModal()
    finally:
        dialog.Destroy()


class DeliveryActions:
    def __init__(self, frame):
        self.frame, self.timer = frame, None
        service = frame.delivery
        policy = service.refresh_policy
        if policy is not None and policy.network_authorized:
            import wx
            self.timer = wx.Timer(frame)
            frame.Bind(wx.EVT_TIMER, self.tick, self.timer)
            # Polling frequency is not the private renewal cadence.
            self.timer.Start(30000)

    def idle(self):
        frame = self.frame
        return (not getattr(frame, '_closing', False) and not frame.working
            and not getattr(frame, 'busy', False) and frame.IsEnabled())

    def tick(self, event=None):
        try:
            self.frame.delivery.submit_refresh(self.frame.executor.submit, idle=self.idle)
        except Exception:
            # Unattended failures remain in the provider's local status and
            # retry schedule. They never steal focus or emit progress speech.
            pass

    def show(self, event=None, *, on_ready=None, check_on_open=False):
        if not self.idle():
            self.frame.report('Finish the current operation before opening license and update controls.')
            return False
        show_delivery_dialog(self.frame, on_ready=on_ready, check_on_open=check_on_open)
        return True

    def close(self):
        if self.timer is not None:
            self.timer.Stop()
        self.frame.delivery.close()


def install_delivery_actions(frame):
    import wx
    current = getattr(frame, 'delivery_actions', None)
    if current is not None:
        return current
    if getattr(frame, 'keyboard_actions', None) is not None:
        raise ValueError('Install delivery actions before capturing keyboard shortcuts.')
    if getattr(frame, 'delivery', None) is None:
        frame.delivery = DeliveryService()
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
        if _plain(frame.menu_bar.GetMenuLabel(index)).casefold() == 'help'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Help')
    actions = DeliveryActions(frame)
    for label, handler in (
            ('License and updates...', actions.show),
            ('Check for updates...', lambda event: actions.show(event, check_on_open=True))):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.delivery_actions = actions
    return actions
