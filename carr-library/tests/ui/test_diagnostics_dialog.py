"""Actual single-executor callbacks and durable export; no native/NVDA claim."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
import sys
import threading
from types import SimpleNamespace as N

import pytest

from carr_library import support_diagnostics as diagnostics
from carr_library.research import export_file
from carr_library.ui import diagnostics_dialog as ui
from carr_library.ui.command_registry import CommandRegistry


class Host:
    def __init__(self, root):
        self.library = N(root=root)
        self._closing = self.working = False
        self.cancel_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = Queue()
        self.futures, self.calls, self.messages = [], [], []
        self.duplicate_callback = False
        self.admission_error = None

    def report(self, message):
        self.messages.append(message)

    def run_job(self, title, operation, done=None, failed=None):
        if self.admission_error is not None:
            raise self.admission_error
        assert not self.working and not self._closing
        self.working = True
        self.cancel_event.clear()
        self.calls.append(title)
        future = self.executor.submit(operation)
        self.futures.append(future)
        def complete(result):
            def apply():
                self.working = False
                if self._closing:
                    return
                try:
                    value = result.result()
                except Exception as exc:
                    if failed is not None:
                        failed(exc)
                else:
                    if done is not None:
                        done(value)
                        if self.duplicate_callback:
                            done(value)
            self.pending.put(apply)
        future.add_done_callback(complete)
        return None

    def finish(self):
        self.pending.get(timeout=5)()

    def close(self):
        self.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def session(tmp_path):
    root = tmp_path / 'private-library'
    root.mkdir()
    (root / 'private.txt').write_text('never read this user text', encoding='utf-8')
    host, notifications = Host(root), []
    controller = ui.DiagnosticsSession(host,
        changed=lambda value: notifications.append((value.busy, value.preview, value.message)))
    try:
        yield N(host=host, controller=controller, notifications=notifications, root=root)
    finally:
        controller.close()
        host.close()


def test_genuine_report_and_exact_export_use_same_none_returning_executor(session, tmp_path, monkeypatch):
    original, threads = diagnostics.prepare_report, []
    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(diagnostics, 'prepare_report', observed)
    main = threading.get_ident()
    assert session.controller.prepare()
    session.host.finish()
    report = session.controller.preview
    assert report is not None and not session.controller.busy
    assert threads and threads[0] != main
    destination = tmp_path / 'diagnostics.json'
    session.host.duplicate_callback = True
    assert session.controller.save(destination, reviewed=report)
    session.host.finish()
    assert destination.read_bytes() == report.payload
    assert len(session.host.calls) == 2
    assert sum('was saved' in message for _, _, message in session.notifications) == 1
    assert (session.root / 'private.txt').read_text(encoding='utf-8') == 'never read this user text'


def test_busy_host_never_starts_another_diagnostic_worker(session):
    session.host.working = True
    assert session.controller.prepare() is False
    assert not session.host.calls and session.controller.preview is None
    session.host.working = False


def test_admission_failure_is_sanitized_and_can_retry(session):
    session.host.admission_error = RuntimeError('private-token-and-path')
    assert session.controller.prepare()
    assert not session.controller.busy
    assert 'private-token' not in session.controller.message
    session.host.admission_error = None
    assert session.controller.prepare()
    session.host.finish()
    assert session.controller.preview is not None


def test_new_library_object_revokes_existing_preview_before_any_save(session, tmp_path):
    assert session.controller.prepare()
    session.host.finish()
    report = session.controller.preview
    session.host.library = N(root=session.root)
    assert session.controller.save(tmp_path / 'refused.json', reviewed=report) is False
    assert len(session.host.calls) == 1 and not (tmp_path / 'refused.json').exists()


def test_refresh_revokes_old_preview_even_when_payload_could_match(session, tmp_path):
    assert session.controller.prepare()
    session.host.finish()
    old = session.controller.preview
    assert session.controller.prepare()
    session.host.finish()
    assert session.controller.preview is not old
    assert session.controller.save(tmp_path / 'refused.json', reviewed=old) is False
    assert not (tmp_path / 'refused.json').exists()


@pytest.mark.parametrize('withdraw', ['dialog_close', 'parent_close', 'library_rebind'])
def test_actual_completed_snapshot_is_not_delivered_to_withdrawn_owner(session, monkeypatch, withdraw):
    original = diagnostics.prepare_report
    reached, release = threading.Event(), threading.Event()
    def held(*args, **kwargs):
        report = original(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return report
    monkeypatch.setattr(diagnostics, 'prepare_report', held)
    try:
        assert session.controller.prepare()
        assert reached.wait(5)
        if withdraw == 'dialog_close':
            session.controller.close()
        elif withdraw == 'parent_close':
            session.host._closing = True
        else:
            session.host.library = N(root=session.root)
        prior = list(session.notifications)
        def poisoned(value):
            raise AssertionError('A stale completion touched destroyed native peers.')
        session.controller.changed = poisoned
        release.set()
        session.host.finish()
        assert session.controller.preview is None
        assert session.notifications == prior
    finally:
        release.set()


def test_cancelled_real_preparation_returns_no_preview_and_allows_retry(session, monkeypatch):
    original = diagnostics.prepare_report
    reached, release = threading.Event(), threading.Event()
    def held(*args, **kwargs):
        report = original(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return report
    monkeypatch.setattr(diagnostics, 'prepare_report', held)
    try:
        assert session.controller.prepare()
        assert reached.wait(5)
        session.controller.cancel()
        release.set()
        session.host.finish()
        assert session.controller.preview is None and not session.controller.busy
        assert 'cancelled' in session.controller.message.lower()
        monkeypatch.setattr(diagnostics, 'prepare_report', original)
        assert session.controller.prepare()
        session.host.finish()
        assert session.controller.preview is not None
    finally:
        release.set()


def test_close_before_publication_cancels_genuine_flushed_stage(session, tmp_path, monkeypatch):
    assert session.controller.prepare()
    session.host.finish()
    report = session.controller.preview
    reached, release = threading.Event(), threading.Event()
    original = export_file.os.fsync
    def held(descriptor):
        result = original(descriptor)
        reached.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(export_file.os, 'fsync', held)
    destination = tmp_path / 'cancelled.json'
    try:
        assert session.controller.save(destination, reviewed=report)
        assert reached.wait(5)
        assert list(tmp_path.glob('.research-export-*'))
        session.controller.close()
        prior = list(session.notifications)
        release.set()
        session.host.finish()
        assert not destination.exists() and not list(tmp_path.glob('.research-export-*'))
        assert session.notifications == prior
    finally:
        release.set()


def test_close_after_actual_commit_preserves_receipt_without_late_ui(session, tmp_path):
    assert session.controller.prepare()
    session.host.finish()
    report = session.controller.preview
    destination = tmp_path / 'already-saved.json'
    assert session.controller.save(destination, reviewed=report)
    receipt = session.host.futures[-1].result(timeout=5)
    assert receipt.path == destination and destination.read_bytes() == report.payload
    session.controller.close()
    prior = list(session.notifications)
    session.host.finish()
    assert session.notifications == prior
    assert destination.read_bytes() == report.payload


def test_help_command_uses_shared_registration_and_is_idempotent(monkeypatch):
    bindings = []
    class Menu:
        def __init__(self):
            self.items = []
        def Append(self, identity, label):
            item = N(label=label)
            self.items.append(item)
            return item
    menu = Menu()
    bar = N(GetMenuCount=lambda: 1, GetMenuLabel=lambda index: '&Help',
            GetMenu=lambda index: menu)
    frame = N(GetMenuBar=lambda: bar, Bind=lambda event, handler, item: bindings.append((handler, item)))
    monkeypatch.setitem(sys.modules, 'wx', N(ID_ANY=-1, EVT_MENU=object()))
    ui.install_diagnostics_actions(frame)
    ui.install_diagnostics_actions(frame)
    assert len(menu.items) == len(bindings) == 1
    spec, item = frame._pending_keyboard_commands[0]
    registry = CommandRegistry([spec])
    assert registry.get('help.diagnostics') is spec and item is menu.items[0]
    called = []
    monkeypatch.setattr(ui, 'show_diagnostics', lambda selected, event: called.append(selected))
    bindings[0][0](None)
    assert called == [frame]

@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_actual_admission_interruption_settles_only_own_session_and_can_retry(session, interruption):
    failure = interruption('private admission detail')
    session.host.cancel_event.set()
    session.host.admission_error = failure
    with pytest.raises(interruption) as caught:
        session.controller.prepare()
    assert caught.value is failure
    assert not session.controller.busy and session.controller.cancelled.is_set()
    assert session.host.cancel_event.is_set()
    assert not session.host.calls and not session.host.futures
    assert 'private admission detail' not in session.controller.message
    session.host.admission_error = None
    assert session.controller.prepare()
    session.host.finish()
    assert session.controller.preview is not None


def test_rewrapped_native_destroy_withdraws_actual_held_worker_by_cached_identity(session, monkeypatch):
    # Execute the exact native handler with a replacement event wrapper. The
    # ordinary CI peer is not a native-window or screen-reader qualification.
    import ast
    import inspect
    source = ast.parse(inspect.getsource(ui.create_diagnostics_dialog))
    dialog_class = next(node for node in ast.walk(source)
                        if isinstance(node, ast.ClassDef) and node.name == 'DiagnosticsDialog')
    handler = next(node for node in dialog_class.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'on_destroy')
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                 '<actual diagnostics native destruction handler>', 'exec'), namespace)
    instance = N(_native_id=48172, session=session.controller)
    skipped = []
    event = N(GetId=lambda: 48172, GetEventObject=lambda: N(),
              Skip=lambda: skipped.append(True))
    original = diagnostics.prepare_report
    reached, release = threading.Event(), threading.Event()
    def held(*args, **kwargs):
        report = original(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return report
    monkeypatch.setattr(diagnostics, 'prepare_report', held)
    try:
        assert session.controller.prepare()
        assert reached.wait(5)
        prior = list(session.notifications)
        foreign = N(GetId=lambda: 48173, GetEventObject=lambda: instance, Skip=lambda: None)
        namespace['on_destroy'](instance, foreign)
        assert not session.controller.closed
        namespace['on_destroy'](instance, event)
        assert skipped == [True] and session.controller.closed
        assert session.controller.cancelled.is_set()
        release.set()
        session.host.finish()
        assert session.controller.preview is None and session.notifications == prior
    finally:
        release.set()
