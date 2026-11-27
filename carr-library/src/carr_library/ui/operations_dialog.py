"""Native shared operation views through the desktop's existing job executor."""
from dataclasses import asdict
import json
from pathlib import Path
import threading

from ..operations import ResourceBroker, ResourceLimits

MIB = 1024**2
GIB = 1024**3


def policy_from_fields(*, cpu, memory_mib, disk_gib, free_mib, gpu, max_leases):
    """Native fields declare allowances; they do not measure hardware."""
    devices = []
    if not isinstance(gpu, str) or len(gpu) > 4096:
        raise ValueError('Enter a bounded list of GPU device IDs and memory allowances.')
    for line in gpu.splitlines():
        if not line.strip():
            continue
        name, separator, capacity = line.strip().partition('=')
        if not separator or not capacity.strip().isdigit():
            raise ValueError('Use one GPU per line, for example cuda:0=4096, with memory in MiB.')
        devices.append((name.strip(), int(capacity.strip()) * MIB))
    return ResourceLimits(cpu_slots=cpu, memory_bytes=memory_mib * MIB,
        disk_bytes=disk_gib * GIB, min_free_disk_bytes=free_mib * MIB,
        gpu_devices=tuple(devices), max_leases=max_leases)


def _services(library, cancelled):
    # Directory creation, lock admission and policy reads happen only on the
    # host executor. No model, inference runtime or provider is started here.
    from ..operations import OperationFacade
    from ..studio.render import ProductionService
    from ..assistant import AssistantService
    from ..audio.transcription import TranscriptionService
    broker = ResourceBroker.for_app(library.root, cancelled=cancelled)
    facade = OperationFacade(library, broker=broker,
        studio=ProductionService(library.root / 'state' / 'studio', broker=broker),
        assistant=AssistantService(library),
        transcription=TranscriptionService(library.root / 'state', broker=broker))
    return broker, facade


class OperationsController:
    """Short policy/journal operations use frame.run_job; no second executor."""
    def __init__(self, frame, *, changed=lambda focus=False: None, service_factory=_services):
        self.frame, self.library = frame, frame.library
        self.changed, self.service_factory = changed, service_factory
        self.closed = self.busy = self.close_pending = False
        self.status = 'Loading saved operations and local resource allowances.'
        self.jobs, self.snapshot, self.policy = (), None, None
        self._services_pair = self._token = None
        self._stop = threading.Event()

    def guard(self):
        if self.closed or getattr(self.frame, '_closing', False) or self.frame.library is not self.library:
            raise ValueError('The data folder changed or this window closed. Reopen Tasks and resources.')

    def cancelled(self):
        return (self.closed or self._stop.is_set() or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library or self.frame.cancel_event.is_set())

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status = str(message)
            self.changed(focus)

    def _pair(self):
        self.guard()
        if self.cancelled():
            raise InterruptedError('Loading saved operations was cancelled.')
        if self._services_pair is None:
            self._services_pair = self.service_factory(self.library, self.cancelled)
        if self.cancelled():
            raise InterruptedError('Loading saved operations was cancelled.')
        return self._services_pair

    def _load(self):
        broker, facade = self._pair()
        return {'jobs': facade.list(cancelled=self.cancelled),
                'resources': broker.snapshot(cancelled=self.cancelled)}

    def _display(self, result):
        self.jobs = tuple(result['jobs'])
        self.snapshot = result['resources']
        self.policy = ResourceLimits.from_dict(self.snapshot['limits'])
        self.status = (f'{len(self.jobs)} saved operations; {len(self.snapshot["leases"])} active resource reservations. '
                       'Saved completion is a journal observation. Open the original workflow to verify results.')

    def _job(self, title, operation, done):
        try:
            self.guard()
        except ValueError as exc:
            self.report(exc, focus=True)
            return False
        if self.busy or getattr(self.frame, 'working', False):
            self.report('Another operation is running. Use its current pause or cancel controls first.', focus=True)
            return False
        self.busy = True
        self._stop.clear()
        token = self._token = object()
        self.report(title + '…')

        def work():
            from ..operations import ResourceCancelled
            self.guard()
            if self.cancelled():
                raise InterruptedError('The task-view request was cancelled.')
            try:
                return operation()
            except ResourceCancelled as exc:
                raise InterruptedError('The task-view request was cancelled.') from exc

        def completed(result):
            if self.closed or self._token is not token:
                return
            self.busy = False
            try:
                self.guard()
                done(result)
            except Exception as exc:
                self.report(exc, focus=True)
            else:
                self.changed(False)

        def failed(error):
            if self.closed or self._token is not token:
                return
            self.busy = False
            self.report(error, focus=True)

        try:
            self.frame.run_job(title, work, completed, failed)
        except Exception as exc:
            failed(exc)
        return True

    def refresh(self):
        return self._job('Loading saved operations and allowances', self._load, self._display)

    def save(self, policy):
        if not isinstance(policy, ResourceLimits):
            raise TypeError('Use the validated local resource policy.')
        def work():
            broker, _ = self._pair()
            broker.configure(policy)
            try:
                return self._load()
            except Exception as exc:
                # The policy is already durably saved. A cancelled or failed
                # optional refresh cannot turn that commit into a failed save.
                return {'saved_policy': policy.to_dict(), 'refresh_warning': str(exc)}
        def done(result):
            if 'saved_policy' in result:
                self.policy = ResourceLimits.from_dict(result['saved_policy'])
                self.status = ('Resource allowances saved. Refresh was not completed: '
                               + result['refresh_warning'])
                return
            self._display(result)
            self.status = 'Resource allowances saved. ' + self.status
        return self._job('Saving local resource allowances', work, done)

    def control(self, key, action):
        selected = next((row for row in self.jobs if row.key == key), None)
        if selected is None or action not in selected.controls:
            self.report('Select an operation that supports this pause or cancel request.', focus=True)
            return False
        def work():
            _, facade = self._pair()
            facade.control(selected.kind, selected.id, action, workspace=selected.workspace)
            return self._load()
        def done(result):
            self._display(result)
            self.status = (action.capitalize() + ' requested. The owning workflow records when it has stopped. '
                           + self.status)
        return self._job('Requesting ' + action, work, done)

    def open_transcription(self, key, opener):
        self.guard()
        if self.busy:
            return False
        selected = next((row for row in self.jobs if row.key == key and row.kind == 'asr'), None)
        if selected is None:
            self.report('Select a transcription job to open its existing workstation.', focus=True)
            return False
        opener(selected.id)
        return True

    def close(self):
        if self.busy:
            self.close_pending = True
            self._stop.set()
            self.report('Stopping this task-view request before closing.')
            return False
        return True

    def dispose(self):
        self.closed = True
        self._token = None
        self._stop.set()


def create_operations_dialog(frame, *, controller_factory=OperationsController):
    import wx

    class OperationsDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Tasks and resources', size=(900, 740),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.controller = controller_factory(frame, changed=self.render)
            self.handoff = None
            self._shown_policy = None
            outer = wx.BoxSizer(wx.VERTICAL)
            self.pages = wx.Notebook(self)
            jobs_page, policy_page = wx.Panel(self.pages), wx.Panel(self.pages)
            jobs_layout, policy_layout = wx.BoxSizer(wx.VERTICAL), wx.BoxSizer(wx.VERTICAL)
            jobs_page.SetSizer(jobs_layout)
            policy_page.SetSizer(policy_layout)

            def field(parent, layout, label, factory=wx.TextCtrl, proportion=0, **kwargs):
                layout.Add(wx.StaticText(parent, label=label), 0, wx.ALL, 4)
                control = factory(parent, **kwargs)
                control.SetName(label.replace('&', ''))
                layout.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
                return control

            self.listing = field(jobs_page, jobs_layout, 'Saved &jobs', wx.ListBox, proportion=1)
            self.detail = field(jobs_page, jobs_layout, 'Selected operation details',
                                proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.cpu = field(policy_page, policy_layout, 'Allowed &CPU slots', wx.SpinCtrl,
                             min=1, max=65536, initial=1)
            self.memory = field(policy_page, policy_layout, 'Declared &memory allowance in MiB', wx.SpinCtrl,
                                min=0, max=2147483647, initial=0)
            self.disk = field(policy_page, policy_layout, 'Declared &temporary disk allowance in GiB', wx.SpinCtrl,
                              min=0, max=2147483647, initial=0)
            self.free = field(policy_page, policy_layout, 'Keep &free disk space in MiB', wx.SpinCtrl,
                              min=0, max=2147483647, initial=0)
            self.gpu = field(policy_page, policy_layout,
                '&GPU devices and MiB allowances; one per line, for example cuda:0=4096',
                style=wx.TE_MULTILINE, size=(-1, 95))
            notice = wx.StaticText(policy_page, label=
                'Allowances apply to cooperating local operations; they do not measure hardware. '
                'Empty GPU settings disable GPU admission. Existing results remain available.')
            notice.Wrap(780)
            policy_layout.Add(notice, 0, wx.ALL | wx.EXPAND, 4)
            self.pages.AddPage(jobs_page, 'Saved operations')
            self.pages.AddPage(policy_page, 'Resource allowances')
            outer.Add(self.pages, 1, wx.ALL | wx.EXPAND, 5)
            self.status = field(self, outer, 'Tasks and resources status',
                                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.buttons = {}
            for key, label in (('refresh', '&Refresh'), ('pause', '&Pause selected'),
                               ('cancel', 'Cance&l selected'), ('open', 'Open &workstation'),
                               ('save', '&Save allowances'), ('close', 'Cl&ose')):
                button = wx.Button(self, wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label)
                button.SetName(label.replace('&', ''))
                self.buttons[key] = button
                row.Add(button, 0, wx.ALL, 3)
            outer.Add(row, 0, wx.ALL | wx.EXPAND, 3)
            self.SetSizer(outer)
            self.SetEscapeId(wx.ID_CLOSE)
            self.listing.Bind(wx.EVT_LISTBOX, self.selected)
            self.buttons['refresh'].Bind(wx.EVT_BUTTON, lambda event: self.controller.refresh())
            self.buttons['pause'].Bind(wx.EVT_BUTTON, lambda event: self.control('pause'))
            self.buttons['cancel'].Bind(wx.EVT_BUTTON, lambda event: self.control('cancel'))
            self.buttons['save'].Bind(wx.EVT_BUTTON, self.save)
            self.buttons['open'].Bind(wx.EVT_BUTTON, self.open_workstation)
            self.buttons['close'].Bind(wx.EVT_BUTTON, self.close)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.render()

        def selection(self):
            index = self.listing.GetSelection()
            rows = self.controller.jobs
            return rows[index] if 0 <= index < len(rows) else None

        def selected(self, event=None):
            current = self.selection()
            self.detail.ChangeValue(json.dumps(asdict(current), ensure_ascii=False, indent=2) if current else '')
            self.render_controls()

        def render_controls(self):
            ready = self.controller.policy is not None and not self.controller.busy
            for control in (self.listing, self.cpu, self.memory, self.disk, self.free, self.gpu):
                control.Enable(ready)
            self.buttons['refresh'].Enable(not self.controller.busy)
            self.buttons['save'].Enable(ready)
            current = self.selection()
            for action in ('pause', 'cancel'):
                self.buttons[action].Enable(ready and current is not None and action in current.controls)
            self.buttons['open'].Enable(ready and current is not None and current.kind == 'asr')

        def render(self, focus=False):
            if self.controller.closed:
                return
            current = self.selection()
            selected = current.key if current else None
            rows = self.controller.jobs
            self.listing.Set([f'{row.kind}: {row.id[:16]}; {row.status}; '
                              f'{row.completed if row.completed is not None else "?"}/'
                              f'{row.total if row.total is not None else "?"}' for row in rows])
            if rows:
                self.listing.SetSelection(next((i for i, row in enumerate(rows) if row.key == selected), 0))
            policy = self.controller.policy
            if policy is not None and policy.to_dict() != self._shown_policy:
                self._shown_policy = policy.to_dict()
                self.cpu.SetValue(policy.cpu_slots)
                self.memory.SetValue(min(2147483647, (policy.memory_bytes + MIB - 1) // MIB))
                self.disk.SetValue(min(2147483647, (policy.disk_bytes + GIB - 1) // GIB))
                self.free.SetValue(min(2147483647, (policy.min_free_disk_bytes + MIB - 1) // MIB))
                self.gpu.ChangeValue('\n'.join(f'{name}={(capacity + MIB - 1) // MIB}'
                                               for name, capacity in policy.gpu_devices))
            self.status.ChangeValue(self.controller.status)
            self.selected()
            if focus:
                self.status.SetFocus()
            if self.controller.close_pending and not self.controller.busy:
                wx.CallAfter(self.close)

        def control(self, action):
            current = self.selection()
            if current:
                self.controller.control(current.key, action)

        def save(self, event=None):
            policy = self.controller.policy
            if policy is None:
                return
            try:
                value = policy_from_fields(cpu=self.cpu.GetValue(), memory_mib=self.memory.GetValue(),
                    disk_gib=self.disk.GetValue(), free_mib=self.free.GetValue(),
                    gpu=self.gpu.GetValue(), max_leases=policy.max_leases)
                self.controller.save(value)
            except (ValueError, TypeError) as exc:
                self.controller.report(exc, focus=True)

        def open_workstation(self, event=None):
            current = self.selection()
            if current:
                def handoff(identity):
                    self.handoff = identity
                    self.EndModal(wx.ID_OK)
                self.controller.open_transcription(current.key, handoff)

        def close(self, event=None):
            if self.controller.closed:
                return
            if not self.controller.close():
                if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return
            self.EndModal(wx.ID_CLOSE)

        def destroyed(self, event):
            if event.GetEventObject() is self:
                self.controller.dispose()
            event.Skip()

        def Destroy(self):
            """Detach before wx schedules deferred native destruction."""
            controller = getattr(self, 'controller', None)
            if controller is not None:
                controller.dispose()
            return super().Destroy()

    return OperationsDialog()


def show_operations(frame):
    import wx
    dialog = create_operations_dialog(frame)
    handoff = None
    try:
        dialog.CentreOnParent()
        wx.CallAfter(dialog.controller.refresh)
        dialog.ShowModal()
        handoff = dialog.handoff
    finally:
        dialog.controller.dispose()
        dialog.Destroy()
    if handoff is not None:
        from .transcription_dialog import show_transcription
        show_transcription(frame, job_id=handoff)


def install_operations_actions(frame):
    import wx
    menu = next((frame.menu_bar.GetMenu(i) for i in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(i).replace('&', '') == 'Tools'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    item = menu.Append(wx.ID_ANY, 'Tasks and resources...')
    frame.Bind(wx.EVT_MENU, lambda event: show_operations(frame), item)
