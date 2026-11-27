"""Execute the real nested Studio job handler with actual common progress tasks.

Native control creation is covered by windows_v3_smoke; this finite harness
covers receipts, completion timing and closed-dialog callback cleanup.
"""
import ast
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from threading import Event, get_ident
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressError, ProgressHub, ProgressState, bind_progress, operation_outcome
from carr_library.ui import studio_dialog
from carr_library.ui.progress_panel import NativeProgressPanel


def harness(*, saved=None, defer_worker=False, defer_ui=False, reject_submit=False):
    source = ast.parse(Path(studio_dialog.__file__).read_text(encoding='utf-8'))
    studio = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'show_studio')
    run = next(n for n in studio.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
    # Compile the actual handler in its actual compatibility-facade contract.
    # Native display is replaced; its ProgressTask and shared hub remain real.
    factory = ast.parse("""def create(environment):
    busy, alive = False, True
    dialog, cancel = environment.dialog, environment.cancel
    controls = lambda: environment.controls(busy)
    progress, executor, wx = environment.progress.legacy, environment.executor, environment.wx
    report, service, current_job = environment.report, environment.service, 'saved-job'
    owned_progress_voices = environment.owned_progress_voices
    retire_progress_voice = environment.retire_progress_voice
    def close_view():
        nonlocal alive
        alive = False
        environment.progress.closed = True
    environment.close_view = close_view
""").body[0]
    factory.body.extend([run, ast.parse('return run').body[0]])
    tree = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    namespace = dict(ProgressError=ProgressError, ProgressState=ProgressState,
                     bind_progress=bind_progress, operation_outcome=operation_outcome,
                     threading=SimpleNamespace(Event=Event))
    exec(compile(tree, str(Path(studio_dialog.__file__)), 'exec'), namespace)
    panel = object.__new__(NativeProgressPanel)
    panel.hub, panel.active_task = ProgressHub(), None
    panel.closed, panel.notice = False, ''
    panel._last_revision = panel._reporting_failure = None
    panel.feedback, panel.refresh = Mock(), Mock()
    panel.media = SimpleNamespace(ui_thread=get_ident())
    queued, pending = [], []

    class Executor:
        def submit(self, operation):
            if reject_submit:
                raise RuntimeError('Synthetic submission failure')
            future = Future()
            assert future.set_running_or_notify_cancel()
            pending.append((future, operation))
            if not defer_worker:
                self.complete()
            return future
        def complete(self):
            future, operation = pending.pop(0)
            try:
                future.set_result(operation())
            except BaseException as error:
                future.set_exception(error)
            return future

    def call_after(callback, *args):
        if defer_ui:
            queued.append(lambda: callback(*args))
        else:
            callback(*args)

    environment = SimpleNamespace(dialog=SimpleNamespace(working=False), cancel=Event(),
        progress=panel, executor=Executor(), wx=SimpleNamespace(CallAfter=call_after),
        report=Mock(), service=SimpleNamespace(get=lambda identity: saved),
        queued=queued, pending=pending, owned_progress_voices=set())
    panel.frame, panel.wx = environment.dialog, environment.wx
    environment.controls = Mock(side_effect=lambda working: setattr(environment.dialog, 'working', working))
    def retire(provider):
        if provider.close() is not False:
            environment.owned_progress_voices.discard(provider)
    environment.retire_progress_voice = retire
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


@pytest.mark.parametrize('status,expected', [('paused','paused'), ('failed','failed'), ('complete','paused')])
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


@pytest.mark.parametrize('delivery', ['worker-pending', 'ui-queued'])
def test_closed_dialog_still_finishes_shared_task_without_touching_controls(delivery):
    run, env = harness(defer_worker=delivery == 'worker-pending', defer_ui=delivery == 'ui-queued')
    complete = Mock()
    run(lambda: {'status':'complete'}, complete)
    assert env.progress.active_task.snapshot().state == ProgressState.RUNNING
    env.close_view()
    if env.pending:
        env.executor.complete()
    while env.queued:
        env.queued.pop(0)()
    assert env.progress.active_task.snapshot().state == ProgressState.SUCCEEDED
    assert not complete.called and not env.report.called
    assert env.controls.call_count == 1  # startup only, no dead-window completion
    assert env.progress.hub.snapshots(active_only=True) == ()


def test_executor_rejection_does_not_leave_work_running():
    run, env = harness(reject_submit=True)
    run(lambda: None, Mock())
    assert env.progress.active_task.snapshot().state == ProgressState.FAILED
    assert env.progress.active_task.snapshot().percent != 100
    assert env.dialog.working is False



def test_already_closed_studio_never_admits_a_new_shared_task():
    run, env = harness(defer_worker=True)
    env.close_view()
    operation, complete = Mock(), Mock()
    run(operation, complete)
    assert env.progress.active_task is None and env.pending == []
    operation.assert_not_called()
    complete.assert_not_called()
    env.controls.assert_not_called()


@pytest.mark.parametrize('result', ['prepared-but-unpublished', None, {'staged_path': 'unpublished.wav'}])
def test_unapplied_raw_result_after_close_cannot_claim_committed_success(result):
    run, env = harness(defer_worker=True)
    complete = Mock()
    run(lambda: result, complete)
    env.close_view()
    env.executor.complete()
    assert env.progress.active_task.snapshot().state == ProgressState.UNAVAILABLE
    assert env.progress.active_task.snapshot().percent != 100
    assert env.progress.hub.snapshots(active_only=True) == ()
    complete.assert_not_called()
    env.report.assert_not_called()


@pytest.mark.parametrize('failure,saved,expected', [
    (OSError('Actual worker publication failed'), {'status': 'complete'}, ProgressState.FAILED),
    (InterruptedError('Acknowledged worker cancellation'), {'status': 'cancelled'}, ProgressState.CANCELLED),
    (InterruptedError('Acknowledged worker pause'), {'status': 'paused'}, ProgressState.PAUSED),
])
def test_worker_error_after_view_death_keeps_the_real_receipt_outcome(failure, saved, expected):
    run, env = harness(saved=saved, defer_worker=True)
    def operation(_phase):
        raise failure
    run(operation, Mock(), with_progress=True)
    env.close_view()
    env.executor.complete()
    snapshot = env.progress.active_task.snapshot()
    assert snapshot.state == expected and snapshot.percent != 100
    assert env.progress.hub.snapshots(active_only=True) == (
        (snapshot,) if expected == ProgressState.PAUSED else ())
    env.report.assert_not_called()


def test_unapplied_closable_result_is_retained_before_shared_task_finalization():
    run, env = harness(defer_worker=True)
    states = []
    class Owner:
        def close(self):
            states.append(env.progress.active_task.snapshot().state)
            return False
    owner = Owner()
    run(lambda: owner, Mock())
    env.close_view()
    env.executor.complete()
    assert states == [ProgressState.RUNNING]
    assert env.owned_progress_voices == {owner}
    assert env.progress.active_task.snapshot().state == ProgressState.UNAVAILABLE


@pytest.mark.parametrize('callback_fails', [False, True])
def test_callback_that_closes_view_finishes_only_after_actual_publication(tmp_path, callback_fails):
    run, env = harness()
    destination = tmp_path / 'callback-result.txt'
    def publish(result):
        assert env.progress.active_task.snapshot().percent != 100
        destination.write_text(result, encoding='utf-8')
        env.close_view()
        if callback_fails:
            raise OSError('Final verification failed after callback detached its owner')
    run(lambda: 'reviewed result', publish)
    assert destination.read_text(encoding='utf-8') == 'reviewed result'
    assert env.progress.active_task.snapshot().state == (
        ProgressState.FAILED if callback_fails else ProgressState.SUCCEEDED)
    assert (env.progress.active_task.snapshot().percent == 100) is (not callback_fails)
    assert env.progress.hub.snapshots(active_only=True) == ()
    env.report.assert_not_called()
