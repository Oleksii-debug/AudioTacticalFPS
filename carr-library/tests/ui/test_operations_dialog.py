"""Real threaded task-view initialization plus queued callback lifecycle checks."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import threading
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.operations._locks import NativeLock
from carr_library.ui.operations_dialog import OperationsController


class QueuedFrame:
    def __init__(self, library):
        self.library = library
        self.working = self._closing = False
        self.cancel_event = threading.Event()
        self.pending = []

    def run_job(self, title, operation, done, failed):
        self.working = True
        self.cancel_event.clear()
        self.pending.append((operation, done, failed))

    def finish(self):
        operation, done, failed = self.pending.pop(0)
        try:
            result = operation()
        except Exception as exc:
            self.working = False
            failed(exc)
        else:
            self.working = False
            done(result)


class ThreadedFrame(QueuedFrame):
    def __init__(self, library):
        super().__init__(library)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.events = Queue()

    def run_job(self, title, operation, done, failed):
        self.working = True
        self.cancel_event.clear()
        future = self.executor.submit(operation)

        def ready(future):
            try:
                result = future.result()
            except Exception as exc:
                callback = lambda error=exc: failed(error)
            else:
                callback = lambda: done(result)
            self.events.put(callback)
        future.add_done_callback(ready)

    def dispatch(self, timeout=3):
        callback = self.events.get(timeout=timeout)
        self.working = False
        callback()


def test_construction_is_io_free_and_policy_load_uses_existing_host_job(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    frame = QueuedFrame(app)
    calls = []
    def factory(*args):
        calls.append(args)
        raise RuntimeError('Synthetic initializer')
    controller = OperationsController(frame, service_factory=factory)
    assert calls == [] and not (app.root / 'runtime').exists()
    assert controller.refresh()
    assert controller.busy and calls == [] and len(frame.pending) == 1
    frame.finish()
    assert len(calls) == 1 and not controller.busy
    assert 'Synthetic initializer' in controller.status


def test_real_executor_waiting_on_native_admission_lock_keeps_dispatch_and_close_responsive(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(min_free_disk_bytes=0))
    gate = NativeLock(broker.root / '.admission.lock')
    assert gate.acquire()
    frame = ThreadedFrame(app)
    entered = threading.Event()
    caller = threading.get_ident()
    threads, changes = [], []

    def factory(library, cancelled):
        threads.append(threading.get_ident())
        entered.set()
        ResourceBroker.for_app(library.root, cancelled=cancelled)
        raise AssertionError('Cancelled initialization passed the held admission lock')

    controller = OperationsController(frame, service_factory=factory,
                                      changed=lambda focus=False: changes.append(threading.get_ident()))
    try:
        started = time.monotonic()
        assert controller.refresh()
        assert time.monotonic() - started < .5
        assert entered.wait(3) and controller.busy
        # This is the same foreground callback path used by the native close
        # event; it executes while the existing worker is blocked on a real lock.
        assert controller.close() is False
        assert controller.close_pending and not controller.closed
        assert frame.cancel_event.is_set() is False
        frame.dispatch()
        assert not controller.busy
        assert threads == [threads[0]] and threads[0] != caller
        assert set(changes) == {caller}
        assert 'cancelled' in controller.status.lower()
        assert gate.held
    finally:
        controller.dispose()
        gate.release()
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_refresh_and_policy_save_use_one_canonical_broker(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    assert controller.refresh()
    frame.finish()
    assert controller.policy is not None and not controller.busy
    chosen = ResourceLimits(cpu_slots=1, memory_bytes=1024**3, disk_bytes=2*1024**3,
                            min_free_disk_bytes=0)
    assert controller.save(chosen)
    frame.finish()
    assert ResourceBroker.for_app(app.root).limits == chosen
    assert controller.policy == chosen
    assert 'allowances saved' in controller.status


def test_late_initialization_completion_cannot_touch_disposed_view(tmp_path):
    frame = QueuedFrame(LibraryApp(tmp_path / 'data'))
    changes = []
    controller = OperationsController(frame, changed=lambda focus=False: changes.append(focus))
    controller.refresh()
    operation, done, failed = frame.pending[0]
    before = list(changes)
    controller.dispose()
    assert controller.cancelled()
    with pytest.raises(ValueError, match='closed'):
        operation()
    done({'never': 'render a dead native view'})
    failed(RuntimeError('late failure'))
    assert changes == before
    assert frame.cancel_event.is_set() is False and controller._token is None


def test_replaced_library_rejects_pending_initializer_without_io(tmp_path):
    first, second = LibraryApp(tmp_path / 'one'), LibraryApp(tmp_path / 'two')
    frame = QueuedFrame(first)
    controller = OperationsController(frame)
    controller.refresh()
    frame.library = second
    frame.finish()
    assert not (first.root / 'runtime').exists()
    assert not (second.root / 'runtime').exists()
    assert 'data folder changed' in controller.status.lower()


def test_policy_commit_remains_saved_when_followup_refresh_is_cancelled(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    controller.refresh()
    frame.finish()
    chosen = ResourceLimits(cpu_slots=1, min_free_disk_bytes=0)
    def stopped():
        raise InterruptedError('Synthetic cancelled post-save refresh')
    monkeypatch.setattr(controller, '_load', stopped)
    controller.save(chosen)
    frame.finish()
    assert ResourceBroker.for_app(app.root).limits == chosen
    assert controller.policy == chosen
    assert controller.status.startswith('Resource allowances saved.')
    assert 'refresh' in controller.status.lower()


def test_policy_changed_after_review_is_not_overwritten(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    controller.refresh()
    frame.finish()
    owner = ResourceLimits(cpu_slots=3, memory_bytes=1024**3, min_free_disk_bytes=0)
    ResourceBroker.for_app(app.root).configure(owner)
    assert controller.save(ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    frame.finish()
    assert ResourceBroker.for_app(app.root).limits == owner
    assert 'changed' in controller.status and not controller.busy
    controller.dispose()


def test_policy_cancel_while_waiting_preserves_last_committed_allowances(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    frame = ThreadedFrame(app)
    controller = OperationsController(frame)
    gate = None
    try:
        controller.refresh()
        frame.dispatch()
        broker = ResourceBroker.for_app(app.root)
        before = broker.limits
        gate = NativeLock(broker.root / '.admission.lock')
        assert gate.acquire()
        assert controller.save(ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
        assert controller.close() is False
        frame.dispatch()
        assert broker.limits == before
        assert 'cancelled' in controller.status.casefold()
        assert gate.held and not frame.cancel_event.is_set()
    finally:
        controller.dispose()
        if gate is not None:
            gate.release()
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_mutated_root_of_same_library_object_is_rejected_before_service_initialization(tmp_path):
    app, other = LibraryApp(tmp_path / 'one'), LibraryApp(tmp_path / 'two')
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    controller.refresh()
    app.root = other.root
    frame.finish()
    assert 'data folder changed' in controller.status.casefold()
    assert not (other.root / 'runtime').exists()


def test_disposal_after_worker_before_callback_closes_owned_service_once(tmp_path, monkeypatch):
    from carr_library.studio.render import ProductionService
    app = LibraryApp(tmp_path / 'data')
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    closed, original = [], ProductionService.close
    def observed(self):
        closed.append(self)
        return original(self)
    monkeypatch.setattr(ProductionService, 'close', observed)
    controller.refresh()
    operation, done, _ = frame.pending.pop()
    value = operation()
    assert not closed and controller.busy and not controller._inflight
    controller.dispose()
    controller.dispose()
    done(value)
    assert len(closed) == 1 and controller._services_pair is None
    assert closed[0]._closed.is_set() and controller.closed


def test_accepted_control_remains_requested_when_refresh_is_cancelled(tmp_path, monkeypatch):
    from carr_library.assistant import AssistantService, Scope
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Task request receipt')
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    controller.refresh()
    frame.finish()
    monkeypatch.setattr(controller, '_load', lambda: (_ for _ in ()).throw(InterruptedError('Cancelled refresh')))
    assert controller.control('assistant:' + identity, 'pause')
    frame.finish()
    assert assistant.store.control(identity) == 'pause'
    assert controller.status.startswith('Pause requested.') and 'Refresh' in controller.status
    assert assistant.get(identity).status == 'queued'
    controller.dispose()



def test_open_existing_canonical_transcription_keeps_selected_library_and_never_starts_worker(tmp_path, monkeypatch):
    from carr_library.ui.operations_dialog import open_transcription_job
    from carr_library.ui import transcription_dialog
    app = LibraryApp(tmp_path / 'data')
    selected, other = app.create_workspace('Selected library'), app.create_workspace('Other library')
    source = tmp_path / 'retained.wav'
    source.write_bytes(b'Synthetic retained source')
    service = app.transcription(selected)
    identity = service.submit([source])
    frame = QueuedFrame(app)
    frame.active = (selected, None)
    opened = []
    monkeypatch.setattr(transcription_dialog, 'show_transcription',
                        lambda owner, **options: opened.append((owner, options)))
    monkeypatch.setattr(type(service), 'run', lambda *args, **kwargs: pytest.fail('Opening started inference'))
    open_transcription_job(frame, identity, selected, library=app, library_root=app.root)
    assert opened == [(frame, {'selected_job': identity})]
    assert service.get(identity)['status'] == 'ready'
    frame.active = (other, None)
    with pytest.raises(ValueError, match='original library'):
        open_transcription_job(frame, identity, selected, library=app, library_root=app.root)
    frame.active = (selected, None)
    frame.library = LibraryApp(tmp_path / 'another-data')
    with pytest.raises(ValueError, match='data folder changed'):
        open_transcription_job(frame, identity, selected, library=app, library_root=app.root)
    assert len(opened) == 1


def test_native_tasks_destroy_observer_uses_captured_id_for_a_different_wrapper():
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_operations_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    observer, = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == 'destroyed']
    module = ast.fix_missing_locations(ast.Module(body=[observer], type_ignores=[]))
    state, skipped = {'native_id': -31994}, []
    environment = {'detached_state': state}
    exec(compile(module, str(script), 'exec'), environment)

    class Event:
        def __init__(self, identity): self.identity = identity
        def GetId(self): return self.identity
        def GetEventObject(self):
            pytest.fail('Deleted native task view cannot depend on its Python wrapper.')
        def Skip(self): skipped.append(self.identity)

    environment['destroyed'](Event(-31993))
    assert 'destroyed' not in state
    environment['destroyed'](Event(-31994))
    assert state == {'native_id': -31994, 'destroyed': True}
    assert skipped == [-31993, -31994]
    assignment, = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id == 'detached_state' and isinstance(target.slice, ast.Constant)
                and target.slice.value == 'native_id' for target in node.targets)]
    bind, = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'Bind'
        and any(isinstance(arg, ast.Name) and arg.id == 'destroyed' for arg in node.args)]
    assert assignment.lineno < bind.lineno and ast.unparse(assignment.value) == 'detached.GetId()'


@pytest.mark.parametrize('missing', ['event', 'dead-wrapper', 'late-callback', None])
def test_native_tasks_completion_requires_event_deleted_wrapper_and_unchanged_job(tmp_path, missing):
    import ast
    from dataclasses import asdict
    from carr_library.assistant import AssistantService, Scope
    from pathlib import Path
    from types import SimpleNamespace
    import traceback

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_operations_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    tick, = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'tick']
    factory = ast.parse('def make_tick(gate, detached):\n    return None\n').body[0]
    factory.body = [tick, ast.Return(value=ast.Name(id='tick', ctx=ast.Load()))]
    module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    app = LibraryApp(tmp_path / 'native-owner-control')
    workspace = app.create_workspace('Native destruction fixture')
    assistant = AssistantService(app)
    identity = assistant.submit(assistant.preview('List books', Scope(workspace)))
    original = asdict(assistant.get(identity))
    checks, errors, stopped, exits, advanced = [], [], [], [], []
    phase, clock = ['destroyed', 0.0], [0.0]
    state = {'native_id': -31994, 'before': 'Retained task view',
             'destroyed': missing != 'event', 'late': missing != 'late-callback'}

    class Detached:
        controller = SimpleNamespace(status='Retained task view')
        def __bool__(self): return missing == 'dead-wrapper'
        def Destroy(self): pytest.fail('Observation must not force native destruction.')

    def check(label, value):
        assert value, label
        checks.append(label)

    def advance(name):
        advanced.append(name)
        phase[:] = [name, clock[0]]

    environment = dict(monotonic=lambda: clock[0], deadline=120.0, phase=phase, held=[None],
        window=lambda title: None, detached_state=state, check=check,
        assistant=assistant, identity=identity, advance=advance,
        timer=SimpleNamespace(Stop=lambda: stopped.append(True)),
        application=SimpleNamespace(ExitMainLoop=lambda: exits.append(True)),
        abort=errors.append, traceback=traceback)
    exec(compile(module, str(script), 'exec'), environment)
    callback = environment['make_tick'](None, Detached())
    callback()
    if missing is not None:
        assert not checks and not errors and not stopped and not exits and not advanced
        clock[0] = 16.0
        callback()
        assert len(errors) == 1 and 'Native Tasks/resources timed out at destroyed' in errors[0]
        assert not stopped and not exits and not advanced
    else:
        assert len(checks) == 3 and not errors
        assert stopped == [True] and exits == [True] and advanced == ['complete']
    assert asdict(assistant.get(identity)) == original and len(assistant.jobs()) == 1


@pytest.mark.parametrize('blocked, changed', [
    (False, None), (True, None), (False, 'wrong-row'), (False, 'duplicate-row'),
    (False, 'missing-search'), (False, 'duplicate-search'),
])
def test_native_tasks_probe_uses_current_semantic_registry_and_real_launcher_rebuild(
        blocked, changed):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    import traceback

    from carr_library.ui import keyboard_settings as keyboard
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec

    manager = keyboard.KeyboardManager(registry=CommandRegistry((
        CommandSpec('operations.open', 'Tasks and resources'),
        CommandSpec('unrelated.open', 'Unrelated command'),
        CommandSpec('reader.same-label', 'Tasks and resources', context='reader'),
    )))
    item = SimpleNamespace(GetItemLabel=lambda: 'Tasks and &resources...')
    manager.bindings['operations.open'] = keyboard._MenuBinding(item, 'Tasks and &resources...', 0)
    frame = SimpleNamespace(working=False, keyboard_commands=manager, status=object())
    rows, errors, keys, checks = [], [], [], []
    phase = ['start', 0.0]

    class Search:
        value = ''
        def GetName(self): return 'Search commands by name'
        def GetValue(self): return self.value
        def SetValue(self, value):
            self.value = value
            launcher['rebuild']()

    class Results:
        values, index = [], -1
        def GetName(self): return 'Matching commands and shortcuts'
        def Set(self, values): self.values = list(values)
        def SetSelection(self, index): self.index = index
        def GetSelection(self): return self.index
        def GetCount(self): return len(self.values)
        def GetString(self, index): return self.values[index]

    search, results = Search(), Results()
    enabled_values, status_values = [], []
    launcher = dict(manager=manager, context='main', rows=rows, search=search,
        results=results, enabled={'operations.open': True, 'unrelated.open': True},
        run=SimpleNamespace(Enable=enabled_values.append),
        status=SimpleNamespace(ChangeValue=status_values.append))
    source = ast.parse(Path(keyboard.__file__).read_text(encoding='utf-8'))
    actual_launcher, = [node for node in source.body if isinstance(node, ast.FunctionDef)
                         and node.name == 'show_command_launcher']
    methods = [node for node in actual_launcher.body if isinstance(node, ast.FunctionDef)
               and node.name in {'selected', 'rebuild'}]
    assert {node.name for node in methods} == {'selected', 'rebuild'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 keyboard.__file__, 'exec'), launcher)
    controls = [search, results]
    palette = SimpleNamespace(GetChildren=lambda: tuple(controls))

    def check(label, condition):
        assert condition, label
        checks.append(label)

    def advance(name):
        phase[:] = [name, 0.0]

    def key(control, code, next_phase, modifiers=0):
        keys.append((control, code, modifiers))
        advance(next_phase)
        return True

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_operations_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    names = {'named', 'open_palette', 'tick'}
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name in names]
    assert {node.name for node in methods} == names
    tick = next(node for node in methods if node.name == 'tick')
    factory = ast.parse('def make_tick(gate, detached):\n    return None\n').body[0]
    factory.body = [tick, ast.Return(value=ast.Name(id='tick', ctx=ast.Load()))]
    native_module = ast.fix_missing_locations(ast.Module(
        body=[node for node in methods if node is not tick] + [factory], type_ignores=[]))
    native = dict(monotonic=lambda: 0.0, deadline=120.0, phase=phase, held=[None],
        frame=frame, owner_loop=[None], application=SimpleNamespace(GetMainLoop=lambda: object()),
        wx=SimpleNamespace(WXK_RETURN=13, MOD_CONTROL=2, MOD_SHIFT=4),
        window=lambda title: palette if title == 'Find a command' else None,
        check=check, advance=advance, key=key, abort=errors.append, traceback=traceback)
    exec(compile(native_module, str(script), 'exec'), native)
    callback = native['make_tick'](None, None)
    callback()
    assert not errors and len(checks) == 2 and phase[0] == 'open'
    if blocked:
        phase[0] = 'open_blocked'
    callback()
    assert keys == [(frame.status, ord('P'), 2 | 4)]
    assert phase[0] == ('palette_blocked' if blocked else 'palette')
    callback()
    assert not errors and search.GetValue() == 'Tasks and resources'
    assert [row.command_id for row in rows] == ['operations.open']
    assert enabled_values[-1] is True and status_values[-1] == 'Ready to run.'
    if changed == 'wrong-row':
        results.values[0] = 'Unrelated command — Not assigned'
    elif changed == 'duplicate-row':
        results.values.append(results.values[0])
    elif changed == 'missing-search':
        controls.remove(search)
    elif changed == 'duplicate-search':
        controls.append(search)
    callback()
    if changed:
        assert len(errors) == 1 and len(keys) == 1 and len(checks) == 2
        assert phase[0] == 'select'
    else:
        assert not errors and len(checks) == 3
        assert keys[-1] == (search, 13, 0)
        assert phase[0] == ('blocked' if blocked else 'loaded')
    assert not hasattr(frame, 'keyboard_settings')
    assert not hasattr(palette, 'filter') and not hasattr(palette, 'rows')


@pytest.mark.parametrize('target, code, permission', [
    ('search', 13, 'allowed'), ('results', 13, 'allowed'),
    ('search', 370, 'allowed'), ('results', 370, 'allowed'),
    ('search', 13, 'initially-disabled'), ('results', 13, 'initially-disabled'),
    ('search', 13, 'revoked-on-close'), ('results', 13, 'revoked-on-close'),
    ('search', 9, 'allowed'), ('results', 27, 'allowed'),
])
def test_real_command_launcher_enter_routes_existing_handler_and_rechecks_availability(
        monkeypatch, target, code, permission):
    import sys
    from types import SimpleNamespace
    from carr_library.ui import keyboard_settings as keyboard
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec

    controls, order, events, skipped = [], [], [], []
    enabled = [permission != 'initially-disabled']
    frame = SimpleNamespace(_closing=False, IsEnabled=lambda: True,
        GetMenuBar=lambda: SimpleNamespace(IsEnabledTop=lambda index: True))
    item = SimpleNamespace(IsEnabled=lambda: enabled[0], GetId=lambda: 711)
    manager = keyboard.KeyboardManager(frame, registry=CommandRegistry((
        CommandSpec('operations.open', 'Tasks and resources'),
        CommandSpec('unrelated.open', 'Unrelated command'),
    )))
    manager.bindings['operations.open'] = keyboard._MenuBinding(item, 'Tasks and resources', 0)
    manager.bindings['unrelated.open'] = keyboard._MenuBinding(
        SimpleNamespace(IsEnabled=lambda: True, GetId=lambda: 712), 'Unrelated command', 0)

    def dispatched(event):
        assert order[-2:] == ['destroyed', 'focus-restored']
        assert event.owner is frame
        events.append(event.identifier)
        return True
    frame.GetEventHandler = lambda: SimpleNamespace(ProcessEvent=dispatched)

    class Control:
        def __init__(self, parent=None, identifier=None, label='', **options):
            self.parent, self.name, self.value = parent, '', ''
            self.bindings, self.values, self.selection = {}, [], -1
            controls.append(self)
        def SetName(self, name): self.name = name
        def GetName(self): return self.name
        def Bind(self, event, callback): self.bindings[event] = callback
        def GetValue(self): return self.value
        def ChangeValue(self, value): self.value = value
        def SetValue(self, value):
            self.value = value
            self.bindings['text'](None)
        def Set(self, values): self.values = list(values)
        def SetSelection(self, selected): self.selection = selected
        def GetSelection(self): return self.selection
        def Enable(self, enabled): self.enabled = enabled
        def SetFocus(self): pass

    class Dialog(Control):
        def SetSizer(self, sizer): pass
        def SetEscapeId(self, identifier): pass
        def EndModal(self, value):
            order.append('accepted')
            self.result = value
        def Destroy(self):
            order.append('destroyed')
            if permission == 'revoked-on-close':
                enabled[0] = False
        def ShowModal(self):
            search, = [value for value in controls if value.name == 'Search commands by name']
            results, = [value for value in controls if value.name == 'Matching commands and shortcuts']
            search.SetValue('Tasks and resources')
            assert results.values and len(results.values) == 1
            selected = search if target == 'search' else results
            event = SimpleNamespace(GetKeyCode=lambda: code, Skip=lambda: skipped.append(code))
            selected.bindings['key-down'](event)
            return getattr(self, 'result', 5101)

    class CommandEvent:
        def __init__(self, event_type, identifier):
            assert event_type == 1001
            self.identifier = identifier
        def SetEventObject(self, owner): self.owner = owner

    origin = SimpleNamespace(SetFocus=lambda: order.append('focus-restored'))
    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, StaticText=Control,
        ListBox=Control, Button=Control, BoxSizer=lambda *args: SimpleNamespace(Add=lambda *args: None),
        Window=SimpleNamespace(FindFocus=lambda: origin), CommandEvent=CommandEvent,
        EVT_MENU=SimpleNamespace(typeId=1001), EVT_TEXT='text', EVT_KEY_DOWN='key-down',
        EVT_LISTBOX='listbox', EVT_LISTBOX_DCLICK='double-click', EVT_BUTTON='button',
        ID_OK=5100, ID_CANCEL=5101, WXK_RETURN=13, WXK_NUMPAD_ENTER=370,
        VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8, ALIGN_RIGHT=16, TE_READONLY=32)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    accepted = keyboard.show_command_launcher(frame, manager)
    expected = code in (13, 370) and permission == 'allowed'
    assert accepted is expected
    assert events == ([711] if expected else [])
    assert skipped == ([code] if code not in (13, 370) else [])
    assert order[-2:] == ['destroyed', 'focus-restored']
    if code in (13, 370) and permission != 'initially-disabled':
        assert order[0] == 'accepted'
    else:
        assert 'accepted' not in order


def native_tasks_methods(names, environment):
    import ast
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / 'scripts/windows_operations_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    methods = [node for node in main.body
               if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == set(names)
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 str(path), 'exec'), environment)
    return environment


@pytest.mark.parametrize('change', [
    None, 'wrong-id', 'wrong-handle', 'hidden', 'not-modal', 'deleting', 'duplicate',
])
def test_native_tasks_selector_uses_exact_live_native_parent_not_python_identity(change):
    from types import SimpleNamespace
    frame = object()

    class Dialog:
        def GetParent(self):
            # wx may provide a distinct Python proxy for the same native parent.
            return SimpleNamespace(GetId=lambda: 72 if change == 'wrong-id' else 71,
                GetHandle=lambda: 902 if change == 'wrong-handle' else 901)
        def GetTitle(self): return 'Tasks and resources'
        def IsShown(self): return change != 'hidden'
        def IsModal(self): return change != 'not-modal'
        def IsBeingDeleted(self): return change == 'deleting'

    dialog = Dialog()
    assert dialog.GetParent() is not frame
    environment = native_tasks_methods({'owned_dialogs', 'window'}, dict(
        wx=SimpleNamespace(Dialog=Dialog,
            GetTopLevelWindows=lambda: [dialog, Dialog()] if change == 'duplicate' else [dialog]),
        frame=frame, frame_identity=dict(id=71, handle=901)))
    if change == 'duplicate':
        with pytest.raises(AssertionError, match='More than one'):
            environment['window']('Tasks and resources')
    else:
        assert environment['window']('Tasks and resources') is (dialog if change is None else None)


@pytest.mark.parametrize('failure', [None, 'snapshot', 'timer', 'key', 'controller', 'modal', 'lock'])
def test_native_tasks_failure_unwinds_owned_modal_and_schedules_captured_outer_loop(
        tmp_path, failure, capsys):
    import json
    import traceback
    from types import SimpleNamespace

    calls, errors, queued = [], [], []
    frame = QueuedFrame(LibraryApp(tmp_path / 'failure-controls'))
    actual = OperationsController(frame)
    unrelated = OperationsController(frame)
    lock = NativeLock(tmp_path / 'actual-cleanup.lock')
    assert lock.acquire()

    def fail_if(selected):
        if failure == selected:
            raise RuntimeError('controlled ' + selected + ' failure')

    class Gate:
        @property
        def held(self): return lock.held
        def release(self):
            calls.append('lock-release')
            fail_if('lock')
            lock.release()

    class Controller:
        def dispose(self):
            calls.append('dispose')
            fail_if('controller')
            actual.dispose()

    parent = SimpleNamespace(GetId=lambda: 71, GetHandle=lambda: 901)
    other_parent = SimpleNamespace(GetId=lambda: 72, GetHandle=lambda: 902)

    class Dialog:
        def __init__(self, parent, title, controller=None, shown=True):
            self.parent, self.title, self.controller = parent, title, controller
            self.shown, self.modal, self.result = shown, True, 5100 if not shown else None
        def GetParent(self): return self.parent
        def GetTitle(self): return self.title
        def GetId(self): return 90
        def GetHandle(self): return 903
        def IsShown(self): return self.shown
        def IsModal(self): return self.modal
        def EndModal(self, value):
            calls.append(('end-modal', self.title))
            fail_if('modal')
            self.result, self.shown, self.modal = value, False, False

    owned = Dialog(parent, 'Tasks and resources', Controller())
    closing_palette = Dialog(parent, 'Find a command', shown=False)
    active_palette = Dialog(parent, 'Find a command')
    outside = Dialog(other_parent, 'Tasks and resources', unrelated)

    class Loop:
        def IsRunning(self): return False
        def ScheduleExit(self, value): calls.append(('outer-exit', value))
    owner = Loop()
    active = SimpleNamespace(IsRunning=lambda: True)

    def snapshot():
        calls.append('snapshot')
        fail_if('snapshot')
    def stop():
        calls.append('timer-stop')
        fail_if('timer')
    def release():
        calls.append('key-release')
        fail_if('key')

    environment = native_tasks_methods({'owned_dialogs', 'abort'}, dict(
        errors=errors, aborting=[False], traceback=traceback, failure_state=snapshot,
        frame_identity=dict(id=71, handle=901), timer=SimpleNamespace(Stop=stop),
        release=release, gate=Gate(), owner_loop=[owner],
        application=SimpleNamespace(
            GetMainLoop=lambda: pytest.fail('Captured outer loop must remain the exit target.'),
            ExitMainLoop=lambda: pytest.fail('The active modal loop is not the owned main loop.')),
        wx=SimpleNamespace(Dialog=Dialog, ID_CANCEL=5101, GetTopLevelWindows=lambda: [
            closing_palette, active_palette, owned, outside], CallAfter=queued.append)))
    try:
        environment['abort']('original loaded-stage failure')
        assert errors[0] == 'original loaded-stage failure'
        assert calls[0] == 'snapshot' and 'dispose' not in calls
        assert len(queued) == 1
        queued.pop()()
        assert calls[-1] == ('outer-exit', 1)
        assert calls.count('dispose') == calls.count('lock-release') == 1
        assert ('end-modal', 'Tasks and resources') in calls
        assert ('end-modal', 'Find a command') in calls
        assert active_palette.modal is (failure == 'modal')
        assert closing_palette.result == 5100 and closing_palette.modal
        assert outside.result is None and outside.modal and not unrelated.closed
        assert actual.closed is (failure != 'controller')
        assert lock.held is (failure == 'lock')
        if failure:
            assert any('controlled ' + failure + ' failure' in value for value in errors[1:])
        else:
            assert len(errors) == 1
        assert owned.modal is (failure == 'modal')
        before = list(calls)
        environment['abort']('second failure remains visible')
        assert errors[-1] == 'second failure remains visible'
        assert calls == before and not queued
        assert 'original loaded-stage failure' in capsys.readouterr().out
    finally:
        actual.dispose()
        unrelated.dispose()
        if lock.held:
            lock.release()


def test_native_tasks_precleanup_snapshot_reports_actual_windows_and_active_loop(capsys):
    import json
    from types import SimpleNamespace
    parent = SimpleNamespace(GetId=lambda: 71, GetHandle=lambda: 901)
    controller = SimpleNamespace(busy=True, closed=False, policy=None, status='Loading exact owner')
    class Dialog:
        def GetParent(self): return parent
        def GetId(self): return 90
        def GetHandle(self): return 903
        def GetTitle(self): return 'Tasks and resources'
        def IsShown(self): return True
        def IsModal(self): return True
    dialog = Dialog()
    dialog.controller = controller
    active = SimpleNamespace(IsRunning=lambda: True)
    owner = SimpleNamespace(IsRunning=lambda: False)
    environment = native_tasks_methods({'failure_state'}, dict(
        json=json, monotonic=lambda: 16.0, phase=['loaded', 1.0],
        frame=SimpleNamespace(working=True, _closing=False), owner_loop=[owner],
        frame_identity=dict(id=71, handle=901),
        wx=SimpleNamespace(Dialog=Dialog, EventLoopBase=SimpleNamespace(GetActive=lambda: active),
            Window=SimpleNamespace(FindFocus=lambda: SimpleNamespace(
                GetId=lambda: 91, GetName=lambda: 'Search commands by name')),
            GetTopLevelWindows=lambda: [dialog])))
    environment['failure_state']()
    prefix, raw = capsys.readouterr().out.split(': ', 1)
    assert prefix == 'TASKS_FAILURE_STATE'
    value = json.loads(raw)
    assert value['stage'] == 'loaded' and value['stage_seconds'] == 15.0
    assert value['frame_working'] and not value['frame_closing']
    assert value['active_loop_running'] and not value['owner_loop_running']
    assert value['focus_id'] == 91 and value['focus_name'] == 'Search commands by name'
    selected, = value['windows']
    assert selected['title'] == 'Tasks and resources' and selected['modal'] and selected['shown']
    assert selected['parent_id'] == 71 and selected['parent_handle'] == 901
    assert selected['busy'] and not selected['closed'] and not selected['policy_loaded']
    assert selected['status'] == 'Loading exact owner'
    assert controller.busy and not controller.closed and dialog.IsModal()
