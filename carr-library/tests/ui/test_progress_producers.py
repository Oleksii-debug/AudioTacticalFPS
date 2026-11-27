"""Real progress state at host acceptance, cancellation and lifetime boundaries.

Native windows are explicit doubles; this suite is not Windows/NVDA evidence.
"""
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.sync.service import SyncPublishedError
from test_desktop_handlers import classes, frame
from test_operation_progress import panel
from test_portable_sync import state, defer, finish, TOKEN, PASSWORD


def tracked_frame(classes, panel):
    value = frame(classes)
    value.progress_actions = SimpleNamespace(panel=panel)
    value.menu_bar.GetMenuLabel.side_effect = ['&Library', '&Options', '&Research']
    return value


def test_main_job_waits_for_acceptance_and_keeps_query_menu_available(classes, panel):
    value = tracked_frame(classes, panel)
    future = Future()
    value.executor.submit.return_value = future
    observed = []
    value.run_job('Import books', lambda: None,
                  lambda result: observed.append(panel.hub.snapshot()[-1].state))
    event, = panel.hub.snapshot()
    assert event.total is None and event.percent is None
    assert value.menu_bar.EnableTop.call_args_list[1].args == (1, True)
    future.set_result({'state': 'complete'})
    # Completion queued on wx is not yet an accepted host result.
    assert panel.hub.snapshot()[-1].state == 'running'
    value.cancel_current_operation()
    classes[1].pop(0)()
    assert observed == ['running']
    assert panel.hub.snapshot()[-1].percent == 100
    assert not value.working


@pytest.mark.parametrize('has_callback', [False, True])
def test_main_result_callback_owns_focus_without_transient_generic_focus(classes, panel, has_callback):
    value = tracked_frame(classes, panel)
    value.status = Mock()
    value.report = classes[0]['DesktopFrame'].report.__get__(value, type(value))
    future, events = Future(), []
    value.executor.submit.return_value = future
    done = (lambda result: events.append('callback focus')) if has_callback else None
    value.run_job('Query', lambda: None, done)
    value.status.SetValue.side_effect = lambda text: events.append('status text')
    value.status.SetFocus.side_effect = lambda: events.append('status focus')
    future.set_result(())
    classes[1].pop(0)()
    assert events == ['status text', 'callback focus' if has_callback else 'status focus']
    assert panel.hub.snapshot()[-1].percent == 100


@pytest.mark.parametrize('error,state', [(InterruptedError('Cancelled'), 'cancelled'),
                                        (OSError('Storage failure'), 'failed')])
def test_main_worker_failures_preserve_callback_and_never_finish_100(classes, panel, error, state):
    value = tracked_frame(classes, panel)
    future = Future()
    value.executor.submit.return_value = future
    failed = Mock()
    value.run_job('Save', lambda: None, failed=failed)
    future.set_exception(error)
    classes[1].pop(0)()
    event, = panel.hub.snapshot()
    assert event.state == state and event.percent is None
    failed.assert_called_once_with(error)


def test_main_start_failure_and_rejected_result_are_failed_tasks(classes, panel):
    value = tracked_frame(classes, panel)
    value.executor.submit.side_effect = RuntimeError('Worker unavailable')
    value.run_job('Save', lambda: None)
    assert panel.hub.snapshot()[-1].state == 'failed' and not value.working
    value.executor.submit.side_effect = None
    value.menu_bar.GetMenuLabel.side_effect = ['&Library', '&Options', '&Research']
    future = Future()
    value.executor.submit.return_value = future
    value.run_job('Save', lambda: None, Mock(side_effect=ValueError('Receipt rejected')))
    future.set_result({'status': 'complete'})
    classes[1].pop(0)()
    assert panel.hub.snapshot()[-1].state == 'failed'
    assert all(event.percent is None for event in panel.hub.snapshot())


def test_forced_main_destroy_withdraws_progress_before_queued_result(classes, panel):
    value = tracked_frame(classes, panel)
    future = Future()
    value.executor.submit.return_value = future
    done = Mock()
    value.run_job('Check sources', lambda: None, done)
    future.set_result(())
    event = SimpleNamespace(GetEventObject=lambda: value, Skip=Mock())
    value._progress_destroyed(event)
    calls = value.report.call_count
    for control in (panel.window, panel.text, panel.gauge):
        control.alive = False
    classes[1].pop(0)()
    assert not panel.alive and panel.hub.snapshot() == ()
    assert value.report.call_count == calls and not done.called
    assert value.cancel_event.is_set()


def test_main_data_root_change_reloads_progress_with_the_new_owner(classes, tmp_path):
    value = frame(classes)
    value.library = SimpleNamespace(semantic_ranker=None)
    value.remember_data_dir = None
    value._confirm, value._drop = Mock(return_value=True), Mock()
    value.scopes = []
    target = tmp_path / 'new-root'
    target.mkdir()
    (target / 'library.sqlite').write_bytes(b'fixture inspected by the real candidate loader mock')
    candidate = SimpleNamespace(root=target, list_scopes=Mock())
    classes[0]['LibraryApp'].return_value = candidate
    owners = []
    value.progress_actions = SimpleNamespace(reload=lambda: owners.append(value.library))
    assert value._open_data_root(target)
    assert owners == [candidate]


def test_portable_measured_counts_finish_only_after_receipt_validation(state, panel):
    state.frame.progress_actions = SimpleNamespace(panel=panel)
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    state.service.on_call = lambda kind, options: options['on_progress'](
        phase='Checking chunks', completed=1024, total=1024)
    executor.run()
    event, = panel.hub.snapshot()
    assert event.awaiting_verification and event.percent is None
    finish(state)
    assert state.view.verified and panel.hub.snapshot()[-1].percent == 100


def test_portable_cancellation_and_partial_commit_never_become_completed(state, panel):
    state.frame.progress_actions = SimpleNamespace(panel=panel)
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    state.controller.cancel()
    executor.run()
    finish(state)
    assert panel.hub.snapshot()[-1].state == 'cancelled'
    state.controller.history_folder = state.folder
    state.service.fail = SyncPublishedError(dict(id=TOKEN, token=TOKEN, head=TOKEN, encrypted=False))
    state.controller.publish(state.folder, 'Snapshot', confirmed=True)
    executor.run()
    finish(state)
    assert TOKEN in state.view.preview
    assert panel.hub.snapshot()[-1].state == 'failed'
    assert all(event.percent is None for event in panel.hub.snapshot())


def test_portable_old_producer_cannot_update_progress_after_input_change(state, panel):
    state.frame.progress_actions = SimpleNamespace(panel=panel)
    executor = defer(state)
    callbacks = []
    state.service.on_call = lambda kind, options: callbacks.append(options['on_progress'])
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    executor.run()
    before = panel.hub.snapshot()
    state.controller.inputs_changed()
    callbacks[0](phase='Late ' + PASSWORD, completed=99, total=100)
    assert panel.hub.snapshot() == before
    finish(state)
    assert panel.hub.snapshot()[-1].state == 'cancelled'
    assert PASSWORD not in panel.hub.snapshot()[-1].describe()
