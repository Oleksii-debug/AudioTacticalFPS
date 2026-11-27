"""Native task metadata and cooperative allowances on the existing executor."""
from dataclasses import asdict
import json
from pathlib import Path
import threading

from ..operations import OperationFacade, ResourceBroker, ResourceLimits
from ..operations.models import ResourceCancelled, ResourceTimeout, ResourceConflict

MIB = 1024**2
GIB = 1024**3


def policy_fields(policy):
    """Display larger byte policies without silently rewriting them on Save."""
    return {
        'cpu': policy.cpu_slots,
        'memory_mib': min(2147483647, (policy.memory_bytes + MIB - 1) // MIB),
        'disk_gib': min(2147483647, (policy.disk_bytes + GIB - 1) // GIB),
        'free_mib': min(2147483647, (policy.min_free_disk_bytes + MIB - 1) // MIB),
        'gpu': '\n'.join(f'{name}={(capacity + MIB - 1) // MIB}'
                        for name, capacity in policy.gpu_devices),
        'max_leases': policy.max_leases,
    }


def policy_from_fields(*, cpu, memory_mib, disk_gib, free_mib, gpu, max_leases,
                       previous=None):
    """Validate explicit allowances; unchanged fields preserve exact bytes."""
    values = dict(cpu=cpu, memory_mib=memory_mib, disk_gib=disk_gib,
                  free_mib=free_mib, gpu=gpu, max_leases=max_leases)
    for key in ('cpu', 'memory_mib', 'disk_gib', 'free_mib', 'max_leases'):
        if type(values[key]) is not int or not 0 <= values[key] <= 2147483647:
            raise ValueError('Resource allowances must be bounded whole numbers.')
    if type(gpu) is not str or len(gpu) > 16384:
        raise ValueError('Enter a bounded list of GPU IDs and MiB allowances.')
    if previous is not None and not isinstance(previous, ResourceLimits):
        raise TypeError('Use the previously validated resource policy.')
    displayed = policy_fields(previous) if previous is not None else {}
    devices = []
    if previous is not None and gpu == displayed['gpu']:
        devices = previous.gpu_devices
    else:
        for line in gpu.splitlines():
            if not line.strip():
                continue
            name, separator, capacity = line.strip().partition('=')
            digits = capacity.strip()
            if not separator or not digits.isascii() or not digits.isdigit() or len(digits) > 19:
                raise ValueError('Use one GPU per line, for example cuda:0=4096, with memory in MiB.')
            devices.append((name.strip(), int(digits) * MIB))
    capacities = {}
    for field, name, factor in (
            ('cpu', 'cpu_slots', 1), ('memory_mib', 'memory_bytes', MIB),
            ('disk_gib', 'disk_bytes', GIB), ('free_mib', 'min_free_disk_bytes', MIB),
            ('max_leases', 'max_leases', 1)):
        capacities[name] = (getattr(previous, name) if previous is not None
            and values[field] == displayed[field] else values[field] * factor)
    return ResourceLimits(**capacities, gpu_devices=tuple(devices))


def _services(library, cancelled):
    # The factory runs only inside the existing frame job. Listing constructs
    # no provider gateway, model adapter or duplicate speech/studio service.
    broker = ResourceBroker.for_app(library.root, cancelled=cancelled)
    return broker, OperationFacade(library, broker=broker)


def _dispatch(function, *arguments):
    import wx
    wx.CallAfter(function, *arguments)


def _error_text(error):
    if isinstance(error, (InterruptedError, ResourceCancelled)):
        return 'The task-view request was cancelled. Saved jobs remain unchanged.'
    if isinstance(error, ResourceTimeout):
        return 'The resource coordinator is busy. Retry after its current short update finishes.'
    if isinstance(error, ResourceConflict):
        return 'The new allowances conflict with active reservations. Existing reservations remain active.'
    return 'The task view could not complete this request. Review the original workflow and retry.'


class OperationsController:
    """Short views/controls reuse frame.run_job and its lifetime ownership."""
    def __init__(self, frame, *, changed=lambda focus=False: None,
                 service_factory=_services, dispatch=_dispatch):
        self.frame, self.library = frame, frame.library
        self.root = Path(self.library.root).absolute()
        self.changed, self.service_factory, self.dispatch = changed, service_factory, dispatch
        self.closed = self.busy = self.close_pending = self.operation_finished = False
        self.status = 'Load saved operations and local resource allowances.'
        self.jobs, self.snapshot, self.policy = (), None, None
        self._services_pair = self._token = None
        self._stop = threading.Event()

    def current(self):
        try:
            return (not self.closed and not getattr(self.frame, '_closing', False)
                and getattr(self.frame, 'library', None) is self.library
                and Path(self.library.root).absolute() == self.root)
        except (ValueError, TypeError, OSError, AttributeError):
            return False

    def guard(self):
        if not self.current():
            raise InterruptedError('The data folder or task window changed.')

    def cancelled(self):
        return (self._stop.is_set() or not self.current()
                or self.frame.cancel_event.is_set())

    def report(self, message, *, focus=False):
        if not self.closed:
            self.status = str(message)
            self.changed(focus)

    def _pair(self):
        self.guard()
        if self.cancelled():
            raise InterruptedError('The task-view request was cancelled.')
        if self._services_pair is None:
            self._services_pair = self.service_factory(self.library, self.cancelled)
        if self.cancelled():
            raise InterruptedError('The task-view request was cancelled.')
        return self._services_pair

    def _load(self):
        _, facade = self._pair()
        return {'jobs': facade.list(cancelled=self.cancelled),
                'resources': facade.resources(cancelled=self.cancelled)}

    def _display(self, result):
        self.jobs, self.snapshot = tuple(result['jobs']), result['resources']
        self.policy = ResourceLimits.from_dict(self.snapshot['limits'])
        self.status = (f'{len(self.jobs)} saved operations; {len(self.snapshot["leases"])} live reservations. '
            'Completion is a journal observation. Verify results and resume in the original workflow.')

    def _settled(self, token):
        if self.closed or self._token is not token:
            return
        self.operation_finished = True
        if self.close_pending or not self.current():
            self.busy = False
            self.close_pending = True
            self.changed(False)

    def _job(self, title, operation, done):
        if not self.current():
            self.close_pending = True
            self.report('The selected data folder changed. Reopen Tasks and resources.', focus=True)
            return False
        if self.busy or getattr(self.frame, 'working', False):
            self.report('This frame is running an operation. Use its current pause or cancel controls; '
                        'reopen Tasks and resources when that operation settles.', focus=True)
            return False
        self.busy, self.operation_finished = True, False
        self._stop.clear()
        token = self._token = object()
        self.report(title + '…')

        def arrived(result=None, error=None):
            if self.closed or self._token is not token:
                return
            self.busy, self.operation_finished = False, True
            if self.close_pending or not self.current():
                self.close_pending = True
                self.changed(False)
                return
            if error is not None:
                self.report(_error_text(error), focus=True)
                return
            try:
                done(result)
            except Exception:
                self.report('The request returned; refresh the task view to review its saved state.', focus=True)
            else:
                self.changed(False)

        def work():
            try:
                self.guard()
                if self.cancelled():
                    raise InterruptedError('The task-view request was cancelled.')
                return operation()
            except (InterruptedError, ResourceCancelled) as exc:
                raise InterruptedError(_error_text(exc)) from None
            except Exception as exc:
                # The main frame also reports failures. Redact before handing
                # an error to that shared callback, not only in this dialog.
                raise RuntimeError(_error_text(exc)) from None
            finally:
                # A changed library may suppress frame.run_job's result callback.
                # This independent settlement closes only after work has returned.
                try:
                    self.dispatch(self._settled, token)
                except RuntimeError:
                    pass

        try:
            started = self.frame.run_job(title, work,
                done=lambda result: arrived(result), failed=lambda error: arrived(error=error),
                progress_unit='checkpoints')
        except Exception as exc:
            arrived(error=exc)
            return False
        if started is False or (self.busy and not getattr(self.frame, 'working', False)
                                and not self.operation_finished):
            self.busy = False
            self._stop.set()
            self.report('The task-view request did not start.', focus=True)
            return False
        return True

    def refresh(self):
        return self._job('Loading tasks and allowances', self._load, self._display)

    def save(self, policy):
        if not isinstance(policy, ResourceLimits):
            raise TypeError('Use a validated local resource policy.')

        def work():
            broker, _ = self._pair()
            broker.configure(policy)
            try:
                return self._load()
            except Exception:
                # A cancelled optional refresh cannot undo a durable save.
                return {'saved_policy': policy.to_dict()}

        def done(result):
            if 'saved_policy' in result:
                self.policy = ResourceLimits.from_dict(result['saved_policy'])
                self.status = 'Resource allowances saved. Refresh was not completed; reload the view when ready.'
            else:
                self._display(result)
                self.status = 'Resource allowances saved. ' + self.status
        return self._job('Saving local resource allowances', work, done)

    def control(self, key, action):
        selected = next((row for row in self.jobs if row.key == key), None)
        if selected is None or action not in selected.controls:
            self.report('Select a job whose existing workflow supports this control.', focus=True)
            return False

        def work():
            _, facade = self._pair()
            receipt = facade.control(selected.kind, selected.id, action,
                workspace=selected.workspace, cancelled=self.cancelled)
            try:
                result = self._load()
            except Exception:
                result = {}
            return dict(result, control_receipt=receipt)

        def done(result):
            if 'resources' in result:
                self._display(result)
            self.status = (action.capitalize() + ' requested. Its owning workflow records when it stops. '
                           'Refresh to observe the next checkpoint.')
        return self._job('Requesting ' + action, work, done)

    def close(self):
        self.close_pending = True
        self._stop.set()
        if self.busy and not self.operation_finished:
            self.report('Waiting for this short task-view request to settle before closing.')
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
            # Native indices belong to the rows actually displayed, never the
            # controller's newly arrived replacement inventory.
            self._displayed_jobs = ()
            self.controller = controller_factory(frame, changed=self.render, dispatch=wx.CallAfter)
            self._shown_policy = None
            outer = wx.BoxSizer(wx.VERTICAL)
            self.pages = wx.Notebook(self)
            jobs_page = wx.Panel(self.pages)
            policy_page = wx.ScrolledWindow(self.pages, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
            policy_page.SetScrollRate(0, 12)
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
            self.maximum = field(policy_page, policy_layout, 'Maximum live reservations', wx.SpinCtrl,
                                 min=1, max=4096, initial=64)
            self.reservations = field(policy_page, policy_layout, 'Current resource reservations',
                style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 95))
            self.gpu.SetMaxLength(16384)
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
                               ('cancel', 'Cance&l selected'),
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
            self.buttons['close'].Bind(wx.EVT_BUTTON, self.close)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.render()

        def selection(self):
            index = self.listing.GetSelection()
            rows = self._displayed_jobs
            return rows[index] if 0 <= index < len(rows) else None

        def selected(self, event=None):
            current = self.selection()
            self.detail.ChangeValue(json.dumps(asdict(current), ensure_ascii=False, indent=2) if current else '')
            self.render_controls()

        def render_controls(self):
            ready = self.controller.policy is not None and not self.controller.busy
            for control in (self.listing, self.cpu, self.memory, self.disk, self.free, self.gpu, self.maximum):
                control.Enable(ready)
            self.buttons['refresh'].Enable(not self.controller.busy)
            self.buttons['save'].Enable(ready)
            current = self.selection()
            for action in ('pause', 'cancel'):
                self.buttons[action].Enable(ready and current is not None and action in current.controls)

        def render(self, focus=False):
            if self.controller.closed:
                return
            current = self.selection()
            selected = current.key if current else None
            rows = tuple(self.controller.jobs)
            self.listing.Set([f'{row.kind}: {row.id}; {row.status}; '
                + (f'{row.completed} of {row.total} {row.unit}' if row.total is not None and row.completed is not None
                   else f'{row.completed} {row.unit}; total unknown' if row.completed is not None
                   else f'progress unknown ({row.unit})')
                + ('; live reservation' if row.live is True else '; live state unknown')
                for row in rows])
            self._displayed_jobs = rows
            # A vanished selection does not grant control of its former neighbor.
            self.listing.SetSelection(next((i for i, row in enumerate(rows) if row.key == selected), wx.NOT_FOUND))
            policy = self.controller.policy
            if policy is not None and policy.to_dict() != self._shown_policy:
                self._shown_policy = policy.to_dict()
                values = policy_fields(policy)
                self.cpu.SetValue(values['cpu'])
                self.memory.SetValue(values['memory_mib'])
                self.disk.SetValue(values['disk_gib'])
                self.free.SetValue(values['free_mib'])
                self.gpu.ChangeValue(values['gpu'])
                self.maximum.SetValue(values['max_leases'])
            self.reservations.ChangeValue(json.dumps(self.controller.snapshot, ensure_ascii=False, indent=2)
                                          if self.controller.snapshot is not None else '')
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
                    gpu=self.gpu.GetValue(), max_leases=self.maximum.GetValue(), previous=policy)
                self.controller.save(value)
            except (ValueError, TypeError) as exc:
                self.controller.report(exc, focus=True)

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



def show_operations(frame, *, on_ready=None):
    import wx
    dialog = create_operations_dialog(frame)
    try:
        dialog.CentreOnParent()
        wx.CallAfter(dialog.controller.refresh)
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog)
        return dialog.ShowModal()
    finally:
        dialog.controller.dispose()
        dialog.Destroy()


class OperationsActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        return show_operations(self.frame)


def install_operations_actions(frame):
    """Use the existing Tools menu and configurable application command map."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'operations_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before Tasks and resources.')
    actions = OperationsActions(frame)
    item = menu.Append(wx.ID_ANY, 'Tasks and &resources...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('operations.tasks', 'Tasks and resources', 'main'), item)
    frame.operations_actions = actions
    return actions
