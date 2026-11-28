"""Actual trust handlers with the existing shared executor and owned child.

The small native-window fixture controls only Close/veto and GUI delivery.
The prepared supervisor, Future, cleanup registry and process settlement are real.
"""
import ast
import importlib.util
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from carr_library.security._files import TrustError
from carr_library.ui.worker_lifetime import DesktopExecutor

from .test_update_handoff import configuration, owned_handoff


SOURCE = Path(importlib.util.find_spec('carr_library.ui.trust_status').origin)


def _handlers():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'_scope_current', '_update_scope_current', '_run_update_job', 'prepare_update_apply'}
    selected = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in names]
    assert {item.name for item in selected} == names and len(selected) == len(names)
    scope = {'TrustError': TrustError}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope


class Host:
    def __init__(self, configuration, *, close='accept'):
        self.library = SimpleNamespace(root=configuration.library)
        self.configuration = configuration
        self._closing, self.working = False, False
        self.close_mode, self.close_calls = close, 0
        self.cancel_event = threading.Event()
        self.executor = DesktopExecutor()
        self.callbacks, self.reports, self.futures, self.progress = queue.Queue(), [], [], []
        self.cleanup_entered = threading.Event()
        self.wait_for_cleanup_race = False
        self.trust_controller = SimpleNamespace(
            update_library=self.library, _update_current=configuration.check)

    def report(self, value):
        self.reports.append(value)

    def register_worker_cleanup(self, callback):
        def actual():
            self.cleanup_entered.set()
            return callback()
        return self.executor.register_cleanup(actual)

    def run_job(self, title, operation, done=None, failed=None, **options):
        if self._closing or self.working:
            return None
        self.cancel_event.clear()
        self.working = True
        def work():
            if options.get('with_progress'):
                return operation(lambda completed, total, phase: self.progress.append((completed, total, phase)))
            return operation()
        future = self.executor.submit(work)
        self.futures.append(future)
        def finished(value):
            def deliver():
                if self._closing:
                    return
                self.working = False
                try:
                    result = value.result()
                except Exception as error:
                    if failed is not None:
                        failed(error)
                else:
                    if done is not None:
                        done(result)
            self.callbacks.put(deliver)
        future.add_done_callback(finished)
        return future

    def Close(self):
        self.close_calls += 1
        if self.close_mode == 'veto':
            return False
        if self.close_mode == 'false-ack':
            return True
        self._closing = True
        self.cancel_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.wait_for_cleanup_race:
            assert self.cleanup_entered.wait(2), 'The real shared-worker finalizer must start before COMMIT.'
        return True

    def deliver(self, *, timeout=5):
        callback = self.callbacks.get(timeout=timeout)
        callback()

    def finish(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        assert not self.executor.cleanup_failures


@pytest.fixture
def host(configuration):
    value = Host(configuration)
    yield value
    value.finish()


def _prepare(host, owned_handoff):
    created = []
    def prepare(repository, package, *, restart, cancelled):
        assert repository == 'explicit reviewed repository' and package is not None
        assert not cancelled()
        owner = owned_handoff().wait_ready(timeout=5)
        created.append(owner)
        return owner
    host.trust_controller.prepare_apply = prepare
    return created


def _start(host, api):
    return api['prepare_update_apply'](host, host.trust_controller,
                                     'explicit reviewed repository', object(), restart=False)


def test_actual_host_close_and_registered_finalizer_cannot_race_before_commit(host, owned_handoff):
    created = _prepare(host, owned_handoff)
    host.wait_for_cleanup_race = True
    future = _start(host, _handlers())
    owner = future.result(timeout=6)
    assert owner is created[0]
    # Drain the actual Future's plain callbacks before forcing the separate
    # registered-finalizer versus native Close/COMMIT race.
    host.executor.submit(lambda: None).result(timeout=3)
    host.deliver()
    host.finish()
    owner.process.wait(timeout=5)
    assert host.close_calls == 1 and host.cleanup_entered.is_set()
    assert owner.transferred and (owner.path / 'fixture-applied').is_file()
    assert host.update_handoff_receipt['status'] == 'HANDED_OFF'
    assert host.update_handoff_receipt['activation'] == 'NOT_ESTABLISHED'


@pytest.mark.parametrize('mode', ['veto', 'false-ack'])
def test_native_close_veto_or_missing_closing_state_cancels_only_owned_handoff(host, owned_handoff, mode):
    host.close_mode = mode
    _prepare(host, owned_handoff)
    owner = _start(host, _handlers()).result(timeout=6)
    host.deliver()
    host.futures[-1].result(timeout=6)
    host.deliver()
    assert owner.closed and not owner.transferred and owner.process.poll() is not None
    assert not (owner.path / 'fixture-applied').exists()
    assert host.close_calls == 1 and not host._closing


@pytest.mark.parametrize('change', ['library', 'controller', 'cancel'])
def test_late_callback_never_closes_changed_host_or_applies_after_cancel(host, owned_handoff, change):
    _prepare(host, owned_handoff)
    owner = _start(host, _handlers()).result(timeout=6)
    if change == 'library':
        host.library = SimpleNamespace(root=host.library.root)
    elif change == 'controller':
        host.trust_controller = object()
    else:
        host.cancel_event.set()
    host.deliver()
    host.futures[-1].result(timeout=6)
    assert owner.closed and not owner.transferred and host.close_calls == 0
    assert not (owner.path / 'fixture-applied').exists()


def test_real_host_shutdown_settles_prepared_helper_without_any_gui_callback(host, owned_handoff):
    _prepare(host, owned_handoff)
    owner = _start(host, _handlers()).result(timeout=6)
    assert host.Close() is True
    host.finish()
    host.deliver()  # The actual GUI callback is suppressed after Close.
    assert owner.closed and not owner.transferred and owner.process.poll() is not None
    assert not (owner.path / 'fixture-applied').exists()


def test_refused_shared_host_submission_creates_no_supervisor(host, owned_handoff):
    created = _prepare(host, owned_handoff)
    host.working = True
    assert _start(host, _handlers()) is None
    assert created == [] and host.futures == [] and host.close_calls == 0


def test_close_between_preparation_and_cleanup_registration_reaps_real_helper(host, owned_handoff):
    created = []
    def prepare(repository, package, *, restart, cancelled):
        owner = owned_handoff().wait_ready(timeout=5)
        created.append(owner)
        # Model actual host destruction after work is admitted, before the
        # service returns. Executor shutdown is real and nonblocking.
        host.Close()
        return owner
    host.trust_controller.prepare_apply = prepare
    future = _start(host, _handlers())
    with pytest.raises(RuntimeError, match='closing'):
        future.result(timeout=6)
    host.finish()
    assert len(created) == 1 and created[0].closed and created[0].process.poll() is not None
    assert not (created[0].path / 'fixture-applied').exists()


def test_real_shared_progress_reports_received_bytes_without_premature_acceptance(host):
    api = _handlers()
    observed = []
    def operation(cancelled, progress):
        assert not cancelled()
        progress(5, 20, 'Downloading signed package; bytes are not yet verified')
        return 'only the final verified package capability belongs in completion'
    future = api['_run_update_job'](host, host.trust_controller, 'Explicit download',
                                    operation, done=observed.append)
    assert future.result(timeout=5).startswith('only')
    host.deliver()
    assert host.progress == [(5, 20, 'Downloading signed package; bytes are not yet verified')]
    assert observed == [future.result()] and host.close_calls == 0


def test_same_byte_library_root_replacement_refuses_new_update_job_before_worker(host):
    root = host.library.root
    root.rename(root.with_name('retained-research'))
    root.mkdir()
    api = _handlers()
    called = []
    assert api['_run_update_job'](host, host.trust_controller, 'Stale source',
        lambda cancelled, progress: called.append(True), done=lambda value: None) is None
    assert called == [] and host.futures == []


def test_uncertain_durable_commit_is_retained_through_actual_host_cleanup(host, owned_handoff, monkeypatch):
    import carr_library.security.update_handoff as handoff
    _prepare(host, owned_handoff)
    owner = _start(host, _handlers()).result(timeout=6)
    monkeypatch.setattr(handoff, 'atomic_write',
        lambda *args: (_ for _ in ()).throw(OSError('Synthetic uncertain COMMIT publication')))
    with pytest.raises(OSError):
        host.deliver()
    host.finish()
    assert owner.transferred and host.update_handoff_receipt['status'] == 'HANDOFF_UNCERTAIN'
    owner.process.wait(timeout=6)
    assert not (owner.path / 'fixture-applied').exists()


def _timer_handlers(host, monkeypatch):
    from .test_update_controller_actions import _online
    controller = _online(host.configuration)
    controller.update_library = host.library
    controller.set_background_update_checks(True, accepted=True)
    controller._next_update_check = 0
    host.trust_controller = controller
    bindings, deliveries = {}, queue.Queue()
    class Timer:
        def __init__(self, frame):
            assert frame is host
            self.interval, self.stopped = None, False
        def Start(self, interval):
            self.interval = interval
        def Stop(self):
            self.stopped = True
    ui = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda function, *args: deliveries.put((function, args)))
    host.GetId = lambda: 712
    host.Bind = lambda event, callback, *args: bindings.__setitem__(event, callback)
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'_scope_current', '_update_scope_current', 'install_update_check_timer'}
    selected = [item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name in names]
    assert {item.name for item in selected} == names and len(selected) == len(names)
    scope = {'TrustError': TrustError, 'wx': ui}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), scope)
    scope['install_update_check_timer'](host)
    assert host.update_check_timer.interval == 60000
    return controller, bindings, deliveries


def test_consented_idle_check_uses_existing_worker_without_focus_download_or_close(host, monkeypatch):
    controller, bindings, deliveries = _timer_handlers(host, monkeypatch)
    thread = host.executor.submit(threading.get_ident).result(timeout=3)
    seen = []
    def metadata(endpoint, *, cancelled):
        assert endpoint == controller.update_endpoint and not cancelled()
        seen.append(threading.get_ident())
        return SimpleNamespace(version='synthetic-reviewed', build=2)
    monkeypatch.setattr(controller, 'check_repository', metadata)
    monkeypatch.setattr(controller, 'download_review', lambda *a, **k: pytest.fail('Timer cannot download.'))
    monkeypatch.setattr(controller, 'prepare_apply', lambda *a, **k: pytest.fail('Timer cannot apply.'))
    bindings['timer']()
    host.update_check_future.result(timeout=5)
    function, args = deliveries.get(timeout=3)
    function(*args)
    assert seen == [thread] and not host.working and host.reports == []
    assert host.close_calls == 0 and not host.futures
    assert 'synthetic-reviewed' in controller._notice
    original = host.update_check_future
    bindings['timer']()
    assert host.update_check_future is original  # at most one hourly attempt


def test_idle_check_cooperatively_stops_for_new_foreground_work_on_same_worker(host, monkeypatch):
    controller, bindings, deliveries = _timer_handlers(host, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def metadata(endpoint, *, cancelled):
        entered.set()
        assert release.wait(3)
        if cancelled():
            raise InterruptedError('Actual host is no longer idle.')
        pytest.fail('A foreground admission must cancel idle metadata work.')
    monkeypatch.setattr(controller, 'check_repository', metadata)
    bindings['timer']()
    assert entered.wait(3)
    future = host.run_job('Actual foreground work', lambda: 'foreground outcome')
    release.set()
    assert future.result(timeout=5) == 'foreground outcome'
    with pytest.raises(InterruptedError, match='no longer idle'):
        host.update_check_future.result(timeout=3)
    function, args = deliveries.get(timeout=3)
    function(*args)
    host.deliver()
    assert not host.working and host.close_calls == 0
    assert 'unavailable' in controller._notice


def test_closed_host_stops_timer_and_drops_late_idle_completion_without_focus(host, monkeypatch):
    controller, bindings, deliveries = _timer_handlers(host, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def metadata(endpoint, *, cancelled):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(version='late-result', build=2)
    monkeypatch.setattr(controller, 'check_repository', metadata)
    bindings['timer']()
    assert entered.wait(3)
    assert host.Close() is True
    event = SimpleNamespace(GetId=lambda: 712, Skip=lambda: None)
    bindings['destroy'](event)
    release.set()
    host.finish()
    function, args = deliveries.get(timeout=3)
    function(*args)
    assert host.update_check_timer.stopped
    assert controller._notice is None and host.reports == []
