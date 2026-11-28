"""Accessible shared job view and explicit local resource policy."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

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


def operation_details_text(value):
    """Format only the facade's redacted snapshot, without refreshing a producer."""
    details = value.details
    unknown = 'Unknown; not recorded'
    lines = [
        'Saved operation metadata; current output files have not been checked.',
        'Operation: ' + value.key,
        'Stored status: ' + value.status,
        'Workspace: ' + (value.workspace or unknown),
        'Saved update time: ' + (value.updated or unknown),
        'Available controls: ' + (', '.join(value.controls) or 'None in the current context'),
        'Phase: ' + value.phase,
        'Recorded progress: ' + (str(value.completed) if value.completed is not None else 'Unknown') +
            (' of ' + str(value.total) if value.total is not None else ''),
        'Active broker reservation: ' + ('Observed' if value.live is True else 'Unknown'),
        'Current canonical handler: ' + (details.handler_schema or unknown),
        'Selected engine or model adapter: ' + (details.adapter or unknown),
        'Saved operation-plan SHA-256: ' + (details.operation_plan_sha256 or unknown),
        'Separate settings SHA-256: ' + (details.settings_sha256 or unknown),
        'Saved project fingerprint: ' + (details.project_fingerprint or unknown),
    ]
    plan = details.resource_plan
    if plan is None:
        lines.append('Declared resource plan: ' + unknown)
    else:
        lines.extend([
            'Declared CPU slots: ' + str(plan.cpu_slots),
            'Declared memory bytes: ' + str(plan.memory_bytes),
            'GPU requested: ' + ('Yes' if plan.gpu_requested else 'No'),
            'Declared GPU memory bytes: ' + str(plan.gpu_memory_bytes),
            'Declared disk reservations in bytes: ' +
                (', '.join(str(size) for size in plan.disk_reservation_bytes) or 'None requested'),
            'Exclusive device count: ' + str(plan.exclusive_device_count),
            'Recorded input count: ' + (str(plan.source_count) if plan.source_count is not None else unknown),
            'Estimated duration in seconds: ' + (str(plan.estimated_duration_seconds)
                if plan.estimated_duration_seconds is not None else unknown),
        ])
    if details.completed_artifact_count is None:
        lines.append('Completed artifact identities: Unavailable in this saved checkpoint')
    else:
        lines.append('Recorded completed artifact identities: showing ' +
            str(len(details.completed_artifacts)) + ' of ' + str(details.completed_artifact_count))
        for artifact in details.completed_artifacts:
            lines.append('Artifact ' + str(artifact.ordinal) + ': ' + str(artifact.size) +
                         ' bytes; SHA-256 ' + artifact.sha256)
        lines.append('Omitted artifact identities: ' + str(details.omitted_artifact_count))
    marker = details.restore_requires_new_output
    lines.append('Saved restore marker requires a new output: ' +
                 (unknown if marker is None else 'Yes' if marker else 'Recorded as no'))
    lines.extend(['Checkpoint: ' + value.checkpoint, details.note])
    return '\n'.join(lines)


def show_operations(frame):
    import wx
    from ..operations import OperationFacade
    from ..studio.render import ProductionService
    from ..transcription import TranscriptionService
    broker = ResourceBroker.for_app(frame.library.root)
    facade = OperationFacade(frame.library, broker=broker,
        studio=ProductionService(frame.library.root / 'state' / 'studio', broker=broker),
        transcription=TranscriptionService(frame.library.root / 'state' / 'transcription', broker=broker, protocol='local-file-job/1'))
    dialog = wx.Dialog(frame, title='Tasks and resources', size=(850, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    alive, busy, jobs, close_when_idle = True, False, (), False
    destroyed = False
    owner_id = dialog.GetId()
    opened_root = Path(frame.library.root).absolute()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='operation-dashboard')

    def field(label, factory=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
        return control

    listing = field('Saved platform &operations', wx.ListBox, proportion=1)
    detail = field('Operation details and resource status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY)
    limits = broker.limits
    cpu = field('Allowed CPU &slots', wx.SpinCtrl, min=1, max=65536, initial=limits.cpu_slots)
    memory = field('Declared &memory allowance in MiB', wx.SpinCtrl, min=0, max=2147483647,
                   initial=min(2147483647, limits.memory_bytes // MIB))
    disk = field('Declared temporary &disk allowance in GiB', wx.SpinCtrl, min=0, max=2147483647,
                 initial=min(2147483647, limits.disk_bytes // GIB))
    free = field('Keep free disk space in MiB', wx.SpinCtrl, min=0, max=2147483647,
                 initial=min(2147483647, limits.min_free_disk_bytes // MIB))
    gpu = field('GPU devices and MiB allowances; one per line, for example cuda:0=4096',
                value='\n'.join(f'{name}={capacity // MIB}' for name, capacity in limits.gpu_devices),
                style=wx.TE_MULTILINE)
    layout.Add(wx.StaticText(dialog, label='These are cooperative allowances for local jobs. '
        'Set them for your computer; no hardware capacity is inferred. Empty GPU settings disable GPU admission. '
        'Reading and saved results remain available while heavy work waits.'), 0, wx.EXPAND | wx.ALL, 4)
    buttons = {}
    row = wx.BoxSizer(wx.HORIZONTAL)
    for key, label in (('refresh', '&Refresh'), ('pause', '&Pause selected'), ('cancel', '&Cancel selected'),
                       ('save', '&Save allowances'), ('close', 'Cl&ose')):
        button = wx.Button(dialog, wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label)
        button.SetName(label.replace('&', ''))
        buttons[key] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CLOSE)

    def controls():
        if not alive:
            return
        for control in (cpu, memory, disk, free, gpu, listing, *buttons.values()):
            control.Enable(not busy)
        buttons['close'].Enable()
        index = listing.GetSelection()
        current = jobs[index] if 0 <= index < len(jobs) else None
        for action in ('pause', 'cancel'):
            buttons[action].Enable(not busy and current is not None and action in current.controls)

    def selected(event=None):
        if not alive:
            return
        index = listing.GetSelection()
        if 0 <= index < len(jobs):
            detail.ChangeValue(operation_details_text(jobs[index]))
        controls()

    def begin(title, operation, done):
        nonlocal busy
        if not alive:
            return
        if busy or getattr(frame, '_closing', False):
            detail.ChangeValue('The task view is updating. Please wait for its current request.')
            return
        def check_root():
            if Path(frame.library.root).absolute() != opened_root:
                raise ValueError('The library folder changed. Close and reopen Tasks and resources.')
        try:
            check_root()
        except ValueError as exc:
            detail.ChangeValue(str(exc))
            return
        busy = True
        controls()
        detail.ChangeValue(title + '…')
        def complete(result):
            nonlocal busy
            busy = False
            if alive:
                try:
                    check_root()
                    done(result)
                except Exception as exc:
                    detail.ChangeValue(str(exc))
                finally:
                    controls()
                if close_when_idle:
                    close()
        def failed(error):
            nonlocal busy
            busy = False
            if alive:
                detail.ChangeValue(str(error))
                controls()
                if close_when_idle:
                    close()
        def finished(future):
            try:
                result = future.result()
            except Exception as exc:
                wx.CallAfter(failed, str(exc))
            else:
                wx.CallAfter(complete, result)
        def checked():
            check_root()
            return operation()
        try:
            executor.submit(checked).add_done_callback(finished)
        except Exception as exc:
            failed(exc)

    def populated(result):
        nonlocal jobs
        jobs = result[0]
        listing.Set([f'{j.kind}: {j.phase or j.id}; {j.status}' for j in jobs])
        detail.ChangeValue(json.dumps(result[1], ensure_ascii=False, indent=2))
        if jobs:
            listing.SetSelection(0)

    def refresh(event=None):
        begin('Loading saved operations', lambda: (facade.list(), broker.snapshot()), populated)

    def control(action):
        index = listing.GetSelection()
        if not 0 <= index < len(jobs):
            return
        job = jobs[index]
        def invoke():
            facade.control(job.kind, job.id, action, workspace=job.workspace)
            return facade.list(), broker.snapshot()
        begin('Requesting ' + action, invoke, populated)

    def save(event=None):
        try:
            policy = policy_from_fields(cpu=cpu.GetValue(), memory_mib=memory.GetValue(),
                disk_gib=disk.GetValue(), free_mib=free.GetValue(), gpu=gpu.GetValue(),
                max_leases=limits.max_leases)
            def configured():
                broker.configure(policy)
                return broker.snapshot()
            begin('Saving resource allowances', configured,
                  lambda value: detail.ChangeValue('Resource allowances saved.\n' +
                      json.dumps(value, ensure_ascii=False, indent=2)))
        except (ValueError, OSError, RuntimeError) as exc:
            detail.ChangeValue(str(exc))

    def close(event=None):
        nonlocal alive, close_when_idle
        if busy:
            close_when_idle = True
            detail.ChangeValue('Waiting for the current task-view request before closing.')
            if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                event.Veto()
            return
        alive = False
        dialog.EndModal(wx.ID_CLOSE)

    listing.Bind(wx.EVT_LISTBOX, selected)
    for key, callback in (('refresh', refresh), ('save', save), ('close', close),
                          ('pause', lambda event: control('pause')),
                          ('cancel', lambda event: control('cancel'))):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    def destroyed_owner(event):
        nonlocal alive, destroyed
        if event.GetId() == owner_id:
            alive, destroyed = False, True
        event.Skip()
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed_owner)
    dialog.Bind(wx.EVT_CLOSE, close)
    controls()
    detail.ChangeValue('Refresh lists saved jobs across this data folder. Choose a job to inspect its '
        'actual checkpoint. Resume transcription or production from its own workstation.')
    buttons['refresh'].SetFocus()
    try:
        return dialog.ShowModal()
    finally:
        alive = False
        executor.shutdown(wait=False, cancel_futures=True)
        if not destroyed:
            dialog.Destroy()


def install_operations_actions(frame):
    import wx
    if getattr(frame, '_operations_menu_item', None) is not None:
        return frame._operations_menu_item
    menu = next((frame.menu_bar.GetMenu(i) for i in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(i).replace('&', '') == 'Tools'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    item = menu.Append(wx.ID_ANY, 'Tasks and resources...')
    frame.Bind(wx.EVT_MENU, lambda event: show_operations(frame), item)
    frame._operations_menu_item = item
    # The existing host captures this native menu into its one shortcut/palette registry.
    return item
