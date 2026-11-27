"""Execute the real nested Studio job handler with actual common progress tasks.

Native control creation is covered by windows_v3_smoke; this finite harness
covers receipts, completion timing and closed-dialog callback cleanup.
"""
import ast
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressError, ProgressHub, ProgressState, bind_progress, operation_outcome
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
    namespace = dict(ProgressError=ProgressError, ProgressState=ProgressState,
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
        service=SimpleNamespace(get=lambda identity: saved))
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
def test_interruption_uses_saved_job_outcome_including_late_cancel(status, expected):
    run, env = harness(saved={'status': status})
    def operation(measured):
        env.cancel.set()
        raise InterruptedError('Synthetic cancellation after checkpoint')
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
