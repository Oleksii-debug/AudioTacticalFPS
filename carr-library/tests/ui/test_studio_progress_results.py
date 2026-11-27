"""Execute the real nested Studio job handler with actual common progress tasks.

Native control creation is covered by windows_v3_smoke; this finite harness
covers receipts, completion timing and closed-dialog callback cleanup.
"""
import ast
from concurrent.futures import CancelledError, Future
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.progress import ProgressError, ProgressHub, ProgressState, ProgressTask, bind_progress, operation_outcome
from carr_library.ui import studio_dialog
from carr_library.ui.progress_panel import NativeProgressPanel


def harness(*, saved=None, closed=False, reject_submit=False):
    source = ast.parse(Path(studio_dialog.__file__).read_text(encoding='utf-8'))
    studio = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'show_studio')
    run = next(n for n in studio.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    # Compile the actual handler in a minimal closure, not a second algorithm.
    factory = ast.parse("""def create(environment):
    busy, alive = False, not environment.closed
    dialog, cancel, controls = environment.dialog, environment.cancel, environment.controls
    progress, executor, wx = environment.progress, environment.executor, environment.wx
    report, service, current_job = environment.report, environment.service, 'saved-job'
""").body[0]
    factory.body.extend([run, ast.parse('return run').body[0]])
    tree = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    namespace = dict(ProgressError=ProgressError, ProgressState=ProgressState, ProgressTask=ProgressTask,
                     AudioCancelled=AudioCancelled, CancelledError=CancelledError,
                     bind_progress=bind_progress, operation_outcome=operation_outcome)
    exec(compile(tree, str(Path(studio_dialog.__file__)), 'exec'), namespace)
    panel = object.__new__(NativeProgressPanel)
    panel.hub, panel.active_task = ProgressHub(), None
    panel.refresh, panel.reporting_unavailable = Mock(), Mock()
    class Executor:
        def submit(self, operation):
            if reject_submit:
                raise RuntimeError('Synthetic submission failure')
            future = Future()
            try:
                future.set_result(operation())
            except BaseException as error:
                future.set_exception(error)
            return future
    environment = SimpleNamespace(closed=closed, dialog=SimpleNamespace(working=False),
        cancel=Event(), controls=Mock(), progress=panel, executor=Executor(),
        wx=SimpleNamespace(CallAfter=lambda call, *args: call(*args)), report=Mock(),
        service=SimpleNamespace(get=Mock(return_value=saved)))
    return namespace['create'](environment), environment


@pytest.mark.parametrize('status,expected', [('complete','succeeded'), ('paused','paused'),
    ('failed','failed'), ('cancelled','cancelled'), ('queued','paused')])
def test_studio_uses_durable_receipt_and_never_announces_partial_completion(status, expected):
    run, env = harness()
    complete = Mock()
    def operation(measured):
        measured('Validating real publication phase')
        snapshot = env.progress.active_task.snapshot()
        assert snapshot.percent is None and snapshot.state == ProgressState.RUNNING
        return {'status': status, 'destination': 'synthetic-output'}
    run(operation, complete, phase='Rendering', with_progress=True)
    snapshot = env.progress.active_task.snapshot()
    assert snapshot.state == expected
    assert (snapshot.percent == 100) is (expected == 'succeeded')
    assert complete.call_count == int(expected == 'succeeded')
    assert env.dialog.working is False


@pytest.mark.parametrize('status,expected', [('paused','paused'), ('failed','failed'), ('complete','succeeded')])
@pytest.mark.parametrize('error', [InterruptedError, AudioCancelled, CancelledError])
def test_interruption_uses_saved_job_outcome_including_late_cancel(status, expected, error):
    run, env = harness(saved={'status': status})
    def operation(measured):
        env.cancel.set()
        raise error('Synthetic cancellation after checkpoint')
    complete = Mock()
    run(operation, complete, phase='Rendering', with_progress=True)
    assert env.progress.active_task.snapshot().state == expected
    assert (env.progress.active_task.snapshot().percent == 100) is (expected == 'succeeded')
    assert not complete.called


def test_closed_dialog_still_finishes_shared_task_without_touching_controls():
    run, env = harness(closed=True)
    complete = Mock()
    run(lambda: {'status':'complete'}, complete)
    assert env.progress.active_task.snapshot().state == ProgressState.SUCCEEDED
    assert not complete.called and not env.report.called
    assert env.controls.call_count == 1  # startup only, no dead-window completion


def test_executor_rejection_does_not_leave_work_running():
    run, env = harness(reject_submit=True)
    run(lambda: None, Mock())
    assert env.progress.active_task.snapshot().state == ProgressState.FAILED
    assert env.progress.active_task.snapshot().percent != 100
    assert env.dialog.working is False


@pytest.mark.parametrize('after_creation', [False, True])
def test_failed_progress_start_keeps_real_output_and_allows_next_operation(tmp_path, after_creation):
    run, env = harness()
    original_begin, original_refresh = env.progress.begin, env.progress.refresh
    if after_creation:
        env.progress.refresh = Mock(side_effect=RuntimeError('Unavailable native observer'))
    else:
        # A previous task must not be reused when creation of the new one fails.
        old_task = env.progress.hub.start('An earlier operation')
        old_task.succeed()
        env.progress.active_task = old_task
        env.progress.begin = Mock(side_effect=RuntimeError('Unavailable progress hub'))
    destination = tmp_path / 'finished.txt'
    complete = Mock()
    def operation(measured):
        measured('Writing the actual result')
        destination.write_text('durable result', encoding='utf-8')
        return {'status': 'complete', 'destination': str(destination)}
    future = run(operation, complete, with_progress=True)
    assert future.result()['status'] == 'complete'
    assert destination.read_text(encoding='utf-8') == 'durable result'
    complete.assert_called_once_with(future.result())
    assert env.dialog.working is False
    if after_creation:
        assert env.progress.active_task.snapshot().state == ProgressState.SUCCEEDED
    else:
        assert env.progress.active_task is old_task
        assert old_task.snapshot().title == 'An earlier operation'
    env.progress.begin, env.progress.refresh = original_begin, original_refresh
    assert run(lambda: {'status': 'complete'}, Mock()) is not None
    assert env.dialog.working is False


def test_failed_phase_observer_does_not_interrupt_real_output(tmp_path, monkeypatch):
    run, env = harness()
    monkeypatch.setattr(ProgressTask, 'update', Mock(side_effect=RuntimeError('Phase display failed')))
    destination = tmp_path / 'published.txt'
    def operation(measured):
        measured('Publishing')
        destination.write_text('published', encoding='utf-8')
        return {'status': 'complete'}
    complete = Mock()
    future = run(operation, complete, with_progress=True)
    assert future.result() == {'status': 'complete'}
    assert destination.read_text(encoding='utf-8') == 'published'
    assert env.progress.active_task.snapshot().state == ProgressState.SUCCEEDED
    complete.assert_called_once_with(future.result())
    assert env.dialog.working is False


def test_both_completion_observers_can_fail_without_losing_success(tmp_path):
    run, env = harness()
    env.progress.finish = Mock(side_effect=RuntimeError('Finish observer failed'))
    env.progress.reporting_unavailable = Mock(side_effect=RuntimeError('Fallback observer failed'))
    destination = tmp_path / 'published.txt'
    def operation():
        destination.write_text('published', encoding='utf-8')
        return {'status': 'complete'}
    complete = Mock()
    future = run(operation, complete)
    assert future.result() == {'status': 'complete'}
    assert destination.read_text(encoding='utf-8') == 'published'
    complete.assert_called_once_with(future.result())
    assert env.progress.active_task.snapshot().state == ProgressState.UNAVAILABLE
    assert env.progress.active_task.snapshot().percent != 100
    assert env.dialog.working is False


@pytest.mark.parametrize('saved_status', ['complete', 'paused'])
def test_real_worker_error_cannot_inherit_stale_success_or_pause(saved_status):
    run, env = harness(saved={'status': saved_status})
    def operation(measured):
        env.cancel.set()
        raise OSError('Actual publication failed')
    complete = Mock()
    run(operation, complete, with_progress=True)
    assert env.progress.active_task.snapshot().state == ProgressState.FAILED
    assert env.progress.active_task.snapshot().percent != 100
    env.service.get.assert_not_called()
    complete.assert_not_called()
    env.report.assert_called_once_with('Actual publication failed', True)
    assert env.dialog.working is False


@pytest.mark.parametrize('saved', [None, {}, {'status': None}])
def test_interruption_without_a_saved_status_cannot_claim_completion(saved):
    run, env = harness(saved=saved)
    def operation(measured):
        raise InterruptedError('Publication was interrupted')
    complete = Mock()
    run(operation, complete, with_progress=True)
    assert env.progress.active_task.snapshot().state == ProgressState.PAUSED
    assert env.progress.active_task.snapshot().percent != 100
    complete.assert_not_called()
    assert env.dialog.working is False


def test_invalid_saved_job_cannot_hide_the_actual_interruption():
    run, env = harness()
    env.service.get.side_effect = KeyError('Malformed saved project')
    def operation(measured):
        raise InterruptedError('Actual interruption')
    run(operation, Mock(), with_progress=True)
    assert env.progress.active_task.snapshot().state == ProgressState.PAUSED
    env.report.assert_called_once_with('Actual interruption', True)
    assert env.dialog.working is False


@pytest.mark.parametrize('broken_observer', [False, True])
def test_completion_error_remains_visible_even_if_progress_is_broken(tmp_path, broken_observer):
    run, env = harness(saved={'status': 'complete'})
    if broken_observer:
        env.progress.finish = Mock(side_effect=RuntimeError('Finish observer failed'))
        env.progress.reporting_unavailable = Mock(side_effect=RuntimeError('Fallback observer failed'))
    destination = tmp_path / 'retained.txt'
    def operation(measured):
        destination.write_text('retained result', encoding='utf-8')
        return {'status': 'complete'}
    complete = Mock(side_effect=ValueError('Actual completion failed'))
    run(operation, complete, with_progress=True)
    assert destination.read_text(encoding='utf-8') == 'retained result'
    expected = ProgressState.UNAVAILABLE if broken_observer else ProgressState.FAILED
    assert env.progress.active_task.snapshot().state == expected
    assert env.progress.active_task.snapshot().percent != 100
    env.service.get.assert_not_called()
    env.report.assert_called_once_with('Actual completion failed', True)
    assert env.dialog.working is False
