"""Real threaded task-view initialization plus queued callback lifecycle checks."""
from concurrent.futures import ThreadPoolExecutor
import ast
from pathlib import Path
from queue import Queue
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import Mock

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


def _probe_callbacks(environment):
    """Execute exact probe closures headlessly; this is not native acceptance."""
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_operations_smoke.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    class ClosureBindings(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            return ast.copy_location(ast.Global(names=node.names), node)
    names = {'check', 'advance', 'abort', 'tick'}
    callbacks = [ClosureBindings().visit(node) for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in callbacks} == names
    exec(compile(ast.fix_missing_locations(ast.Module(body=callbacks, type_ignores=[])),
                 str(path), 'exec'), environment)
    return environment


@pytest.mark.parametrize('phase,expected', [('policy_page', 'stale_policy'),
                                          ('refreshed_policy', 'saved_policy')])
def test_native_probe_policy_review_is_not_repeated_while_save_waits_for_focus(phase, expected):
    value, edits, pressed, focused = [3], [], [], [False]
    def set_value(current):
        value[0] = current
        edits.append(current)
    cpu = SimpleNamespace(GetValue=lambda: value[0], SetValue=set_value, GetName=lambda: 'Allowed CPU slots')
    save = object()
    dialog = SimpleNamespace(cpu=cpu, buttons={'save': save},
        pages=SimpleNamespace(GetSelection=lambda: 1),
        max_leases=SimpleNamespace(GetName=lambda: 'Maximum active reservations'),
        usage=SimpleNamespace(GetWindowStyleFlag=lambda: 1),
        controller=SimpleNamespace(busy=False, policy=ResourceLimits(cpu_slots=3)))
    broker = SimpleNamespace(configure=Mock())
    owner_loop = object()
    env = {'phase': [phase, 0], 'deadline': 100, 'monotonic': lambda: 0,
           'held': [None], 'aborted': False, 'in_tick': False, 'main_loop': None,
           'application': SimpleNamespace(GetMainLoop=lambda: owner_loop),
           'wx': SimpleNamespace(WXK_SPACE=32, TE_READONLY=1), 'checks': [], 'errors': [],
           'traceback': traceback, 'ResourceLimits': ResourceLimits, 'broker': broker,
           'window': lambda title: dialog if title == 'Tasks and resources' else None}
    _probe_callbacks(env)
    env['abort'] = lambda detail: pytest.fail(detail)
    def key(control, code, next_phase):
        assert control is save and code == 32
        if not focused[0]:
            return False
        pressed.append(next_phase)
        env['advance'](next_phase)
        return True
    env['key'] = key
    for _ in range(4):
        env['tick']()
    assert edits == [2]
    assert broker.configure.call_count == (1 if phase == 'policy_page' else 0)
    assert not pressed and not env['errors']
    focused[0] = True
    env['tick']()
    assert pressed == [expected] and env['phase'][0] == expected
    if phase == 'refreshed_policy':
        assert env['checks'].count('native Refresh shows the actual current policy') == 1


@pytest.mark.parametrize('broken', ['none', 'timer_stop', 'key_release', 'controller_dispose', 'gate_release'])
def test_native_probe_abort_exits_owned_outer_loop_and_keeps_cleanup_after_errors(broken):
    calls, errors = [], []
    frame = SimpleNamespace(cancel_event=threading.Event())
    def action(name):
        def invoke(*args):
            calls.append((name, args))
            if name == broken:
                raise RuntimeError('Synthetic ' + name)
        return invoke
    class Dialog:
        def __init__(self, parent):
            self.parent = parent
            self.controller = SimpleNamespace(dispose=action('controller_dispose'))
        def GetParent(self): return self.parent
        def IsModal(self): return True
        def EndModal(self, code): calls.append(('end_modal', (self.parent, code)))
    owned, foreign = Dialog(frame), Dialog(object())
    outer = SimpleNamespace(ScheduleExit=action('outer_exit'))
    active = SimpleNamespace(Exit=action('wrong_nested_exit'))
    env = {'errors': errors, 'aborted': False, 'in_tick': False, 'main_loop': outer,
           'frame': frame, 'timer': SimpleNamespace(Stop=action('timer_stop')),
           'gate': SimpleNamespace(held=True, release=action('gate_release')),
           'release': action('key_release'), 'traceback': traceback,
           'application': SimpleNamespace(GetMainLoop=lambda: outer, ExitMainLoop=lambda: active.Exit(1)),
           'wx': SimpleNamespace(Dialog=Dialog, ID_CANCEL=17,
               GetTopLevelWindows=lambda: (foreign, owned), CallAfter=lambda callback: callback())}
    _probe_callbacks(env)
    env['abort']('Original failed native assertion')
    assert errors[0] == 'Original failed native assertion'
    assert ('outer_exit', (1,)) in calls
    assert not any(name == 'wrong_nested_exit' for name, _ in calls)
    assert ('end_modal', (frame, 17)) in calls
    assert not any(name == 'end_modal' and args[0] is foreign.parent for name, args in calls)
    assert {'timer_stop', 'key_release', 'controller_dispose', 'gate_release'} <= {name for name, _ in calls}
    if broken != 'none':
        assert any('Synthetic ' + broken in value for value in errors)
    before = list(calls)
    env['abort']('Late callback after abort')
    env['tick']()
    assert calls == before and errors[0] == 'Original failed native assertion'
