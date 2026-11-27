"""Actual DesktopFrame job flow with real progress state and explicit UI doubles."""
import ast
import os
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, current_progress
from carr_library.import_progress import batch_outcome, import_outcome, report_batch
from carr_library.ui.progress_panel import NativeProgressPanel


@pytest.fixture
def host():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == 'DesktopFrame')
    callbacks = Queue()
    namespace = {
        '__name__': 'carr_library.ui._progress_job_probe', '__package__': 'carr_library.ui',
        'WorkspaceFrame': object, 'wx': SimpleNamespace(CallAfter=callbacks.put),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    frame = object.__new__(namespace['DesktopFrame'])
    frame._closing = frame.working = frame.busy = False
    frame.cancel_event = Event()
    frame.cancel_button = Mock()
    frame.choice = Mock()
    frame.book = Mock()
    frame.report = Mock()
    frame.status = Mock()
    frame.executor = ThreadPoolExecutor(max_workers=1)
    panel = object.__new__(NativeProgressPanel)
    panel.hub = ProgressHub()
    panel.active_task = None
    panel._last_revision = None
    panel._reporting_failure = None
    panel.feedback = Mock()
    panel.refresh = Mock()
    panel.set_busy = Mock()
    frame.progress_panel = panel
    try:
        yield frame, callbacks
    finally:
        frame.cancel_event.set()
        frame._closing = True
        frame.executor.shutdown(wait=True, cancel_futures=True)


def finish(callbacks, *, timeout=3):
    callback = callbacks.get(timeout=timeout)
    callback()


@pytest.mark.parametrize('target_name', [None, 'Search status', 'Search results'])
def test_completion_keeps_the_result_callback_focus_target(host, target_name):
    """A generic job summary must not steal focus from an opened result."""
    frame, callbacks = host
    focused = []

    def control(name):
        widget = SimpleNamespace(name=name, value='Ready')
        widget.GetValue = lambda: widget.value
        widget.SetValue = lambda text: setattr(widget, 'value', text)
        widget.SetFocus = lambda: focused.append(widget)
        return widget

    frame.status = control('Library operation status')
    target = control(target_name) if target_name is not None else frame.status

    def report(message):
        frame.status.SetValue(message)
        frame.status.SetFocus()

    def display_result(result):
        target.SetValue(result)
        target.SetFocus()

    frame.report = report
    frame.run_job('Open source', lambda: 'Exact readable source',
                  display_result if target_name is not None else None)
    finish(callbacks)
    assert frame.status.GetValue() == 'Open source: complete.'
    assert focused[-1] is target
    if target_name is not None:
        assert target.GetValue() == 'Exact readable source'
    assert frame.progress_panel.active_task.snapshot().percent == 100


def test_worker_binding_is_real_and_100_waits_for_successful_result_callback(host):
    frame, callbacks = host
    caller = get_ident()
    worker_identity = []
    callback_percent = []

    def operation():
        worker_identity.append(get_ident())
        reporter = current_progress()
        assert reporter is not None
        reporter.advance(2)
        return 'committed synthetic result'

    done = Mock(side_effect=lambda result: callback_percent.append(frame.progress_panel.active_task.snapshot().percent))
    failed = Mock()
    frame.run_job('Known successful job', operation, done, failed, progress_total=2)
    finish(callbacks)
    assert worker_identity and worker_identity[0] != caller
    assert callback_percent == [99]
    done.assert_called_once_with('committed synthetic result')
    failed.assert_not_called()
    assert frame.progress_panel.active_task.snapshot().state == 'succeeded'
    assert frame.progress_panel.active_task.snapshot().percent == 100
    assert not frame.working and not frame.busy and current_progress() is None


def test_cancel_request_does_not_relabel_a_real_commit_error_as_cancelled(host):
    frame, callbacks = host
    entered, release = Event(), Event()
    done, failed = Mock(), Mock()

    def operation():
        entered.set()
        if not release.wait(3):
            raise TimeoutError('Fixture release timed out.')
        raise OSError('Synthetic durable publication failed')

    frame.run_job('Failed publication despite cancellation request', operation, done, failed)
    try:
        assert entered.wait(1)
        frame.cancel_current_operation()
        assert frame.progress_panel.active_task.snapshot().state == 'cancelling'
        release.set()
        finish(callbacks)
        assert frame.progress_panel.active_task.snapshot().state == 'failed'
        assert frame.progress_panel.active_task.snapshot().percent != 100
        assert 'publication failed' in frame.progress_panel.active_task.snapshot().message
        done.assert_not_called()
        assert failed.call_count == 1 and isinstance(failed.call_args.args[0], OSError)
    finally:
        release.set()


@pytest.mark.parametrize('exception', [InterruptedError('Acknowledged cancellation'), CancelledError()])
def test_explicit_worker_interruption_is_acknowledged_as_cancelled(host, exception):
    frame, callbacks = host
    done, failed = Mock(), Mock()

    def operation():
        raise exception

    frame.run_job('Acknowledged cancellation', operation, done, failed)
    finish(callbacks)
    assert frame.progress_panel.active_task.snapshot().state == 'cancelled'
    assert frame.progress_panel.active_task.snapshot().percent != 100
    done.assert_not_called()
    failed.assert_called_once_with(exception)


def test_success_after_late_request_remains_successful_when_work_really_committed(host):
    frame, callbacks = host
    committed, release = Event(), Event()
    done, failed = Mock(), Mock()

    def operation():
        current_progress().advance()
        committed.set()
        if not release.wait(3):
            raise TimeoutError('Fixture release timed out.')
        return 'already committed'

    frame.run_job('Late cancellation', operation, done, failed, progress_total=1)
    try:
        assert committed.wait(1)
        frame.cancel_current_operation()
        release.set()
        finish(callbacks)
        done.assert_called_once_with('already committed')
        failed.assert_not_called()
        assert frame.progress_panel.active_task.snapshot().state == 'succeeded'
        assert frame.progress_panel.active_task.snapshot().percent == 100
        assert 'before cancellation' in frame.progress_panel.active_task.snapshot().message
    finally:
        release.set()


def test_late_cancel_between_durable_publication_and_unit_report_preserves_success(host, tmp_path):
    frame, callbacks = host
    committed, release = Event(), Event()
    output = tmp_path / 'committed-result.txt'
    done, failed = Mock(), Mock()

    def operation():
        with output.open('wb') as stream:
            stream.write(b'Durable real result')
            stream.flush()
            os.fsync(stream.fileno())
        committed.set()
        if not release.wait(3):
            raise TimeoutError('Fixture release timed out.')
        # Cancellation requests do not undo work or acknowledge cancellation.
        # A cooperative producer may reach this reporting boundary afterward.
        reporter = current_progress()
        reporter.advance()
        assert reporter.snapshot().state == 'cancelling'
        return output

    frame.run_job('Late cancellation after publication', operation, done, failed, progress_total=1)
    try:
        assert committed.wait(2)
        frame.cancel_current_operation()
        release.set()
        finish(callbacks)
        assert output.read_bytes() == b'Durable real result'
        done.assert_called_once_with(output)
        failed.assert_not_called()
        snapshot = frame.progress_panel.active_task.snapshot()
        assert snapshot.state == 'succeeded' and snapshot.percent == 100
        assert snapshot.completed == 1 and not frame.working and not frame.busy
    finally:
        release.set()


@pytest.mark.parametrize('failure', ['incomplete-units', 'observer-error'])
def test_progress_observer_failure_cannot_turn_a_successful_job_into_business_failure(host, failure):
    frame, callbacks = host
    done, failed = Mock(), Mock()
    if failure == 'observer-error':
        frame.progress_panel.finish = Mock(side_effect=RuntimeError('Synthetic observer closed'))

    frame.run_job('Successful operation with progress warning', lambda: 'committed result', done, failed,
                  progress_total=1 if failure == 'incomplete-units' else None)
    finish(callbacks)
    done.assert_called_once_with('committed result')
    failed.assert_not_called()
    assert not frame.working and not frame.busy
    reporting_failure = frame.progress_panel._reporting_failure
    assert reporting_failure is not None and reporting_failure[1] == 'succeeded'
    assert 'Progress reporting is unavailable' in reporting_failure[2]
    assert frame.progress_panel.active_task.snapshot().percent != 100
    assert frame.progress_panel.active_task.snapshot().state == 'unavailable'
    assert frame.progress_panel.hub.snapshots(active_only=True) == ()


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_text_import_returned_cancellation_cannot_be_reported_as_success(host, tmp_path, cancelled):
    from carr_library.application import LibraryApp
    frame, callbacks = host
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual import outcome')
    source = tmp_path / 'source.txt'
    original = b'Exact imported outcome text.\r\n'
    source.write_bytes(original)
    done, failed = Mock(), Mock()
    frame.run_job('Import actual text', lambda: app.import_file(
        workspace, source, cancelled=lambda: cancelled), done, failed, progress_result=import_outcome)
    finish(callbacks)
    result = done.call_args.args[0]
    assert result['state'] == ('cancelled' if cancelled else 'complete')
    assert done.call_count == 1 and not failed.called
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == ('cancelled' if cancelled else 'succeeded')
    assert (snapshot.percent == 100) is (not cancelled)
    assert bool(app.search(workspace, 'outcome')) is (not cancelled)
    assert source.read_bytes() == original


@pytest.mark.parametrize('domain_state', ['quarantined', 'conflict', 'timed_out', 'failed'])
def test_returned_attention_result_reaches_its_original_callback_without_false_100(host, domain_state):
    frame, callbacks = host
    value = {'state': domain_state, 'message': 'The original detailed import result.'}
    done, failed = Mock(), Mock()
    frame.run_job('Returned import status', lambda: value, done, failed, progress_result=import_outcome)
    finish(callbacks)
    done.assert_called_once_with(value)
    failed.assert_not_called()
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == 'failed' and snapshot.percent != 100
    assert value == {'state': domain_state, 'message': 'The original detailed import result.'}


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_saved_batch_reports_actual_units_and_terminal_result(host, tmp_path, cancelled):
    from carr_library.application import LibraryApp
    frame, callbacks = host
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual batch outcome')
    sources = []
    for index in range(2):
        source = tmp_path / f'source-{index}.txt'
        source.write_text(f'Exact tracked batch source {index}.', encoding='utf-8')
        sources.append(source)
    batch = app.plan_import_batch(workspace, sources)
    done, failed = Mock(), Mock()
    frame.run_job('Run actual saved batch', lambda: app.run_import_batch(
        workspace, batch['batch_id'], cancelled=lambda: cancelled, progress=report_batch),
        done, failed, progress_total=2, progress_unit='files', progress_result=batch_outcome)
    finish(callbacks, timeout=30)
    result = done.call_args.args[0]
    assert result['state'] == ('paused' if cancelled else 'complete')
    assert done.call_count == 1 and not failed.called
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == ('cancelled' if cancelled else 'succeeded')
    assert snapshot.completed == (0 if cancelled else 2)
    assert (snapshot.percent == 100) is (not cancelled)
    assert len(app.search(workspace, 'tracked')) == (0 if cancelled else 2)


def test_failed_import_result_observer_keeps_the_actual_domain_callback(host):
    frame, callbacks = host
    done, failed = Mock(), Mock()
    frame.run_job('Actual returned result', lambda: 'retained actual result', done, failed,
                  progress_result=lambda result: object())
    finish(callbacks)
    done.assert_called_once_with('retained actual result')
    failed.assert_not_called()
    assert frame.progress_panel.active_task.snapshot().state == 'unavailable'
    assert frame.progress_panel.active_task.snapshot().percent != 100
