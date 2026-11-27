"""Task-view lifecycle uses the actual host executor and native admission lock."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from threading import Event, get_ident
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
        self.cancel_event = Event()
        self.pending, self.callbacks = [], []

    def dispatch(self, function, *arguments):
        self.callbacks.append(lambda: function(*arguments))

    def run_job(self, title, operation, done, failed, **kwargs):
        self.working = True
        self.cancel_event.clear()
        self.pending.append((operation, done, failed))

    def finish(self, *, suppress_result=False):
        operation, done, failed = self.pending.pop(0)
        try:
            value = operation()
        except Exception as exc:
            complete = lambda error=exc: failed(error)
        else:
            complete = lambda: done(value)
        self.working = False
        if not suppress_result:
            self.callbacks.append(complete)
        while self.callbacks:
            self.callbacks.pop(0)()


class ThreadedFrame(QueuedFrame):
    def __init__(self, library):
        super().__init__(library)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.queue = Queue()

    def dispatch(self, function, *arguments):
        self.queue.put(lambda: function(*arguments))

    def run_job(self, title, operation, done, failed, **kwargs):
        self.working = True
        self.cancel_event.clear()
        future = self.executor.submit(operation)
        def ready(future):
            try:
                value = future.result()
            except Exception as exc:
                result = lambda error=exc: failed(error)
            else:
                result = lambda: done(value)
            def complete():
                self.working = False
                result()
            self.queue.put(complete)
        future.add_done_callback(ready)

    def drain(self):
        deadline = time.monotonic() + 4
        while self.working or not self.queue.empty():
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'Host task did not settle.'
            self.queue.get(timeout=remaining)()


def controller(frame, **kwargs):
    return OperationsController(frame, dispatch=frame.dispatch, **kwargs)


def test_construction_is_io_free_and_frame_job_errors_are_already_redacted(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    frame = QueuedFrame(app)
    calls = []
    def factory(*arguments):
        calls.append(arguments)
        raise RuntimeError('PRIVATE /source/path original transcript')
    view = controller(frame, service_factory=factory)
    assert not calls and not (app.root / 'runtime').exists()
    assert view.refresh() and view.busy
    operation, done, failed = frame.pending[0]
    with pytest.raises(RuntimeError) as error:
        operation()
    assert 'PRIVATE' not in str(error.value) and '/source/path' not in str(error.value)
    frame.finish()
    assert not view.busy and 'PRIVATE' not in view.status


def test_real_executor_gate_wait_does_not_block_close_or_touch_unrelated_cancel_flag(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    gate = NativeLock(broker.root / '.admission.lock')
    assert gate.acquire()
    frame = ThreadedFrame(app)
    entered = Event()
    owner, threads, changes = get_ident(), [], []
    def factory(library, cancelled):
        threads.append(get_ident())
        entered.set()
        ResourceBroker.for_app(library.root, cancelled=cancelled)
        raise AssertionError('Initialization escaped a held native lock.')
    view = controller(frame, service_factory=factory,
                      changed=lambda focus=False: changes.append(get_ident()))
    try:
        started = time.monotonic()
        assert view.refresh()
        assert time.monotonic() - started < .5 and entered.wait(2)
        assert view.close() is False and view.close_pending
        assert not frame.cancel_event.is_set()
        frame.drain()
        assert not view.busy and gate.held
        assert threads and all(identity != owner for identity in threads)
        assert changes and set(changes) == {owner}
        assert not frame.cancel_event.is_set()
    finally:
        view.dispose()
        gate.release()
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_policy_load_and_save_use_the_existing_canonical_broker(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    frame = QueuedFrame(app)
    view = controller(frame)
    assert view.refresh()
    frame.finish()
    assert view.policy is not None and view.snapshot['schema'] == 'carr-resource-view/1'
    chosen = ResourceLimits(cpu_slots=1, memory_bytes=1000, disk_bytes=2000, min_free_disk_bytes=0)
    assert view.save(chosen)
    frame.finish()
    assert ResourceBroker.for_app(app.root).limits == chosen
    assert view.policy == chosen and view.status.startswith('Resource allowances saved.')


def test_changed_root_settles_even_when_host_suppresses_ordinary_completion(tmp_path):
    first, second = LibraryApp(tmp_path / 'first'), LibraryApp(tmp_path / 'second')
    frame = QueuedFrame(first)
    changes = []
    view = controller(frame, changed=lambda focus=False: changes.append(focus))
    assert view.refresh()
    frame.library = second
    frame.finish(suppress_result=True)
    assert view.operation_finished and view.close_pending and not view.busy
    assert view.policy is None and not (second.root / 'runtime').exists()
    assert not (first.root / 'runtime').exists()


def test_mutating_the_same_app_root_invalidates_queued_work(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    frame = QueuedFrame(app)
    view = controller(frame)
    view.refresh()
    app.root = tmp_path / 'different-root'
    frame.finish(suppress_result=True)
    assert view.close_pending and not view.busy and not app.root.exists()


def test_late_callbacks_cannot_touch_disposed_native_controls(tmp_path):
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    changes = []
    view = controller(frame, changed=lambda focus=False: changes.append(focus))
    view.refresh()
    operation, done, failed = frame.pending[0]
    before = list(changes)
    view.dispose()
    with pytest.raises(InterruptedError):
        operation()
    done({'not': 'a current view'})
    failed(ValueError('late'))
    while frame.callbacks:
        frame.callbacks.pop(0)()
    assert changes == before and view._token is None and not frame.cancel_event.is_set()


def test_durable_policy_save_remains_successful_if_refresh_is_cancelled(tmp_path, monkeypatch):
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    view = controller(frame)
    view.refresh()
    frame.finish()
    selected = ResourceLimits(cpu_slots=1, min_free_disk_bytes=0)
    def stopped():
        raise InterruptedError('PRIVATE post-save diagnostic')
    monkeypatch.setattr(view, '_load', stopped)
    assert view.save(selected)
    frame.finish()
    assert ResourceBroker.for_app(frame.library.root).limits == selected
    assert view.policy == selected and view.status.startswith('Resource allowances saved.')
    assert 'PRIVATE' not in view.status


@pytest.mark.parametrize('refusal', ['false', 'no-worker', 'exception'])
def test_host_refusal_does_not_leave_a_busy_modal(tmp_path, refusal):
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    def refuse(*args, **kwargs):
        if refusal == 'exception':
            raise RuntimeError('PRIVATE host failure')
        return False if refusal == 'false' else None
    frame.run_job = refuse
    view = controller(frame)
    assert view.refresh() is False
    assert not view.busy and 'PRIVATE' not in view.status and view.close()


def test_existing_foreground_operation_is_not_replaced_or_cancelled(tmp_path):
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    frame.working = True
    view = controller(frame)
    assert view.refresh() is False and frame.pending == []
    assert not frame.cancel_event.is_set() and 'current pause or cancel' in view.status


def test_close_during_initialization_prevents_completed_data_from_reaching_view(tmp_path):
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    view = controller(frame)
    view.refresh()
    assert not view.close()
    frame.finish()
    assert view.close_pending and not view.busy and view.policy is None


def test_explicit_control_stays_requested_if_optional_refresh_fails(tmp_path, monkeypatch):
    from carr_library.operations import OperationView
    frame = QueuedFrame(LibraryApp(tmp_path / 'library'))
    receipts = []
    class Facade:
        def control(self, *arguments, **kwargs):
            receipts.append((arguments, kwargs))
            return {'requested': True, 'acknowledged': False}
    view = controller(frame, service_factory=lambda *args: (None, Facade()))
    identity = '1' * 32
    view.jobs = (OperationView('asr', identity, None, 'running', 0, 1, '', '',
                              ('pause', 'cancel'), 'transcription-jobs/' + identity + '/job.json'),)
    monkeypatch.setattr(view, '_load', lambda: (_ for _ in ()).throw(InterruptedError('stopped')))
    assert view.control('asr:' + identity, 'cancel')
    frame.finish()
    assert len(receipts) == 1 and view.status.startswith('Cancel requested.')
    assert 'when it stops' in view.status


def _native_selection_methods():
    """Run the installed dialog methods against only the native-list boundary."""
    import ast
    from pathlib import Path
    from carr_library.ui import operations_dialog
    source = Path(operations_dialog.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'create_operations_dialog')
    dialog = next(node for node in factory.body if isinstance(node, ast.ClassDef)
                  and node.name == 'OperationsDialog')
    methods = [node for node in dialog.body if isinstance(node, ast.FunctionDef)
               and node.name in ('selection', 'selected', 'render_controls', 'render', 'control')]
    result = ast.ClassDef(name='NativePolicy', bases=[], keywords=[], body=methods, decorator_list=[])
    from dataclasses import asdict
    from types import SimpleNamespace
    import json
    namespace = {'asdict': asdict, 'json': json, 'wx': SimpleNamespace(NOT_FOUND=-1)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[result], type_ignores=[])),
                 str(source), 'exec'), namespace)
    return namespace['NativePolicy']


@pytest.mark.parametrize('change', ['insert', 'reorder', 'remove', 'same-id-other-kind', 'initial'])
def test_refresh_preserves_exact_displayed_operation_or_clears_selection(change):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from carr_library.operations import OperationView

    def row(digit, kind='asr'):
        return OperationView(kind, digit * 32, None, 'running', 0, 1, '', '',
                             ('pause', 'cancel'), 'transcription-jobs/' + digit * 32 + '/job.json')

    class NativeList:
        def __init__(self, selected):
            self.index = selected
        def GetSelection(self):
            return self.index
        def Set(self, labels):
            self.labels, self.index = tuple(labels), -1
        def SetSelection(self, index):
            self.index = index
        def Enable(self, enabled):
            self.enabled = enabled

    first, chosen, last, inserted = row('1'), row('2'), row('3'), row('4')
    old = (first, chosen, last) if change != 'initial' else ()
    new = {'insert': (inserted, first, chosen, last),
           'reorder': (last, first, chosen),
           'remove': (first, last),
           'same-id-other-kind': (first, row('2', 'assistant'), last),
           'initial': (first, chosen, last)}[change]
    native = _native_selection_methods()()
    policy = ResourceLimits(cpu_slots=1)
    native._displayed_jobs, native._shown_policy = old, policy.to_dict()
    native.listing = NativeList(1 if old else -1)
    native.controller = SimpleNamespace(jobs=new, policy=policy, closed=False, close_pending=False,
                                       busy=False, snapshot=None, status='Updated', control=Mock())
    for name in ('detail', 'cpu', 'memory', 'disk', 'free', 'gpu', 'maximum', 'reservations', 'status'):
        setattr(native, name, Mock())
    native.buttons = {key: Mock() for key in ('refresh', 'pause', 'cancel', 'save')}
    native.render()
    if change in ('insert', 'reorder'):
        assert native.selection().key == chosen.key
        native.control('cancel')
        native.controller.control.assert_called_once_with(chosen.key, 'cancel')
    else:
        assert native.listing.GetSelection() == -1 and native.selection() is None
        native.control('cancel')
        native.controller.control.assert_not_called()
        native.buttons['cancel'].Enable.assert_called_with(False)
        native.detail.ChangeValue.assert_called_with('')


@pytest.mark.parametrize('own_window', [True, False], ids=['native-dialog', 'child-control'])
def test_native_destroy_identity_does_not_require_a_live_derived_wrapper(own_window):
    """Exercise the installed handler; actual wx destruction stays in the native gate."""
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    from carr_library.ui import operations_dialog

    source = Path(operations_dialog.__file__)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'create_operations_dialog')
    dialog = next(node for node in factory.body if isinstance(node, ast.ClassDef)
                  and node.name == 'OperationsDialog')
    handler = next(node for node in dialog.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'destroyed')
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                 str(source), 'exec'), namespace)
    controller = Mock()
    native = SimpleNamespace(_window_id=41, controller=controller)
    event = Mock()
    event.GetId.return_value = 41 if own_window else 42
    event.GetEventObject.side_effect = AssertionError('Derived wrapper identity is not durable.')
    namespace['destroyed'](native, event)
    event.GetEventObject.assert_not_called()
    event.Skip.assert_called_once_with()
    if own_window:
        controller.dispose.assert_called_once_with()
    else:
        controller.dispose.assert_not_called()
