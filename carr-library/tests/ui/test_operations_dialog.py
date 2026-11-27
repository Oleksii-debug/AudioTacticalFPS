"""Real threaded task-view initialization plus queued callback lifecycle checks."""
from concurrent.futures import ThreadPoolExecutor
import ast
from pathlib import Path
from queue import Queue
import threading
import time
from types import SimpleNamespace

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


def test_native_destruction_with_a_recreated_wrapper_detaches_pending_initialization(tmp_path):
    from carr_library.ui import operations_dialog

    source = Path(operations_dialog.__file__)
    module = ast.parse(source.read_text(encoding='utf-8'))
    factory = next(node for node in module.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'create_operations_dialog')
    view = next(node for node in factory.body if isinstance(node, ast.ClassDef))
    handler = next(node for node in view.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'destroyed')
    namespace = {}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), namespace)

    frame = QueuedFrame(LibraryApp(tmp_path / 'data'))
    changes, skipped = [], []
    controller = OperationsController(frame, changed=lambda focus=False: changes.append(focus))
    assert controller.refresh()
    operation, done, failed = frame.pending[0]
    before_status, before_changes = controller.status, list(changes)

    def invalid_native_call():
        raise AssertionError('Destruction must not call through an invalid native wrapper.')

    window = SimpleNamespace(controller=controller, _native_id=4201, GetId=invalid_native_call)
    for identity in (4202, 4201):
        event = SimpleNamespace(GetId=lambda: identity, GetEventObject=lambda: object(),
                                Skip=lambda: skipped.append(identity))
        namespace['destroyed'](window, event)
        assert controller.closed is (identity == 4201)
    assert skipped == [4202, 4201]
    assert controller.cancelled() and controller._token is None
    with pytest.raises(ValueError, match='closed'):
        operation()
    done({'never': 'render a destroyed native view'})
    failed(RuntimeError('late failure'))
    controller.report('Late report after native parent destruction.')
    assert controller.status == before_status and changes == before_changes
    assert not frame.cancel_event.is_set()


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


def test_changed_selected_workspace_blocks_queued_view_before_state_io(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    first, second = app.create_workspace('First'), app.create_workspace('Second')
    frame = QueuedFrame(app)
    frame.active = (first, None)
    controller = OperationsController(frame)
    assert controller.refresh()
    frame.active = (second, None)
    frame.finish()
    assert not (app.root / 'runtime').exists()
    assert 'selected library changed' in controller.status
    assert not controller.busy


def test_actual_asr_handoff_selects_saved_job_without_inference_and_rechecks_scope(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Selected ASR library')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Synthetic retained input; no model.')
    service = app.transcription(workspace)
    identity = service.submit([source])
    frame = QueuedFrame(app)
    frame.active = (workspace, None)
    controller = OperationsController(frame)
    assert controller.refresh()
    frame.finish()
    selected = next(row for row in controller.jobs if row.kind == 'asr')
    assert selected.id == identity and selected.workspace == workspace
    assert selected.controls == ()
    opened = []
    assert controller.open_transcription(selected.key, opened.append)
    assert opened == []  # Current authorization is rechecked on the host executor.
    frame.finish()
    assert opened == [identity]
    assert service.get(identity)['status'] == 'ready'
    assert not (service._path(identity) / 'job_config.json').exists()
    assert controller.open_transcription(selected.key, opened.append)
    app.archive_workspace(workspace)
    frame.finish()
    assert opened == [identity] and 'archived' in controller.status
    assert not controller.busy


def test_all_libraries_view_requires_active_scope_before_transcription_handoff(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('ASR library')
    source = tmp_path / 'synthetic.wav'
    source.write_bytes(b'Synthetic local input.')
    app.transcription(workspace).submit([source])
    frame = QueuedFrame(app)
    controller = OperationsController(frame)
    controller.refresh()
    frame.finish()
    row = next(value for value in controller.jobs if value.kind == 'asr')
    opened = []
    assert controller.open_transcription(row.key, opened.append) is False
    assert not opened and not frame.pending
    assert 'selected active library' in controller.status
