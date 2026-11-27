"""Actual DesktopFrame job flow with real progress state and explicit UI doubles."""
import ast
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, current_progress
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


def finish(callbacks):
    callback = callbacks.get(timeout=3)
    callback()


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


@pytest.mark.parametrize('payload, expected', [
    ({'status': 'paused', 'completed': 1, 'total': 2}, 'paused'),
    ({'state': 'quarantined'}, 'paused'),
    ({'rows': [{'state': 'complete'}, {'state': 'failed'}]}, 'failed'),
    ({'status': 'cancelled'}, 'cancelled'),
])
def test_returned_job_receipt_never_claims_success_for_incomplete_work(host, payload, expected):
    frame, callbacks = host
    done = Mock()
    frame.run_job('Reported task outcome', lambda: payload, done)
    finish(callbacks)
    done.assert_called_once_with(payload)
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == expected
    assert snapshot.percent != 100


def test_typed_assistant_report_preserves_failed_state(host):
    from carr_library.assistant.models import JobReport
    frame, callbacks = host
    payload = JobReport('synthetic', 'failed', 0, 1, (), 'Synthetic failure')
    frame.run_job('Assistant outcome', lambda: payload, Mock())
    finish(callbacks)
    assert frame.progress_panel.active_task.snapshot().state == 'failed'
    assert frame.progress_panel.active_task.snapshot().percent != 100


def test_callback_failure_never_announces_generic_completion(host):
    frame, callbacks = host
    def rejected(result):
        assert not any('complete.' in call.args[0] for call in frame.report.call_args_list)
        raise ValueError('Final publication validation failed')
    frame.run_job('Publication', lambda: 'staged result', rejected)
    finish(callbacks)
    assert frame.progress_panel.active_task.snapshot().state == 'failed'
    assert not any('complete.' in call.args[0] for call in frame.report.call_args_list)


def test_result_callback_owns_focus_and_detailed_status(host):
    frame, callbacks = host
    messages = ['Operation in progress']
    frame.status.GetValue.side_effect = lambda: messages[-1]
    selected_source = Mock()
    def selected(result):
        messages.append('Verified source page selected')
        selected_source.SetFocus()
    frame.run_job('Open source', lambda: 'verified page', selected)
    frame.report.reset_mock()
    finish(callbacks)
    selected_source.SetFocus.assert_called_once_with()
    frame.report.assert_not_called()
    frame.status.SetValue.assert_not_called()
    assert frame.progress_panel.active_task.snapshot().state == 'succeeded'


@pytest.mark.parametrize('target_name', [None, 'Search status', 'Search results'])
def test_completion_summary_preserves_the_callback_focus_target(host, target_name):
    """Retain the canonical focus regression on the composed progress service."""
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

    frame.report = report

    def display_result(result):
        target.SetValue(result)
        target.SetFocus()

    frame.run_job('Open source', lambda: 'Readable source result',
                  display_result if target_name is not None else None)
    finish(callbacks)

    assert frame.status.GetValue() == 'Open source: complete.'
    assert focused[-1] is target
    if target_name is not None:
        assert target.GetValue() == 'Readable source result'
    assert frame.progress_panel.active_task.snapshot().percent == 100


@pytest.mark.parametrize('scenario,state', [
    ('missing', 'failed'), ('cancelled', 'cancelled'),
    ('imported', 'succeeded'), ('resumed', 'succeeded'),
])
def test_actual_library_import_and_resume_receipts_keep_their_outcome(host, tmp_path, scenario, state):
    """Retain actual import/replay evidence while using the canonical reporter."""
    from carr_library.application import LibraryApp

    frame, callbacks = host
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Actual progress outcomes')
    source = tmp_path / 'manuscript.txt'
    if scenario != 'missing':
        source.write_text('Exact retained manuscript for progress verification.')
    if scenario == 'resumed':
        from carr_library.extract.page_ocr import Config
        from carr_library.ingest.worker import prepare_request, record_progress
        metadata = dict(title='Retained manuscript', authors=[], language=None,
                        edition=None, volume=None, collections=[])
        request = prepare_request(library.root, workspace, source, metadata, Config())
        stopped = library._import_file(workspace, source, **request['metadata'],
            _request_id=request['request_id'], cancelled=lambda: True)
        assert stopped['state'] == 'cancelled'
        record_progress(library.root, request['request_id'], state='cancelled', result=stopped)
        source.unlink()
        operation = lambda: library.resume_import(workspace, request['request_id'])
    else:
        operation = lambda: library.import_files(workspace, [source], cancelled=lambda: scenario == 'cancelled')
    receipts = []
    frame.run_job('Actual source import', operation, receipts.append)
    callbacks.get(timeout=30)()
    assert len(receipts) == 1
    actual = receipts[0] if scenario == 'resumed' else receipts[0][0]
    assert actual['state'] == {'failed': 'failed', 'cancelled': 'cancelled', 'succeeded': 'complete'}[state]
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == state and not frame.working
    assert (snapshot.percent == 100) == (state == 'succeeded')
    assert bool(library.search(workspace, 'manuscript')) == (state == 'succeeded')


def test_receipt_classifier_failure_preserves_successful_publication(host, monkeypatch):
    import carr_library.progress as progress_module
    frame, callbacks = host
    done, failed = Mock(), Mock()
    monkeypatch.setattr(progress_module, 'operation_outcome',
        Mock(side_effect=ValueError('Unclassifiable receipt')))
    frame.run_job('Export', lambda: 'committed', done, failed)
    finish(callbacks)
    done.assert_called_once_with('committed')
    failed.assert_not_called()
    assert not frame.working and not frame.busy
    snapshot = frame.progress_panel.active_task.snapshot()
    assert snapshot.state == 'unavailable' and snapshot.percent != 100
    assert 'Progress reporting is unavailable' in frame.progress_panel._reporting_failure[2]


def test_late_measurements_do_not_replace_the_callback_started_job(host):
    frame, callbacks = host
    release = Event()
    observers = []
    def first(report):
        observers.append(report)
        report(1, 1, 'First retained result')
        return {'state': 'complete'}
    def second():
        assert release.wait(3), 'Fixture release timed out.'
    frame.run_job('First', first,
        lambda result: frame.run_job('Second', second), with_progress=True)
    try:
        finish(callbacks)
        current = frame.progress_panel.active_task
        assert frame.working and current.snapshot().title == 'Second'
        before = current.snapshot()
        observers[0](1, 1, 'Late first-operation observer')
        assert frame.progress_panel.active_task is current
        assert current.snapshot() == before
        assert before.state == 'running' and before.percent is None
    finally:
        release.set()
    finish(callbacks)
    assert frame.progress_panel.active_task is current
    assert current.snapshot().state == 'succeeded'
