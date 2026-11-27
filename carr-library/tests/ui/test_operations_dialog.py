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
