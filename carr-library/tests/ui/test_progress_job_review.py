"""Independent regression checks for truthful desktop job completion."""
import ast
from concurrent.futures import Future
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, ProgressState, ProgressTask, bind_progress, operation_outcome
from carr_library.ui.progress_panel import NativeProgressPanel



def progress_panel():
    """Use production tasks; only native device/control observation is doubled."""
    panel = object.__new__(NativeProgressPanel)
    panel.hub, panel.active_task = ProgressHub(), None
    panel._last_revision = panel._reporting_failure = None
    panel.notice = ''
    panel.feedback, panel.set_busy = Mock(), Mock()
    panel.observed_snapshots = []
    def refresh():
        if panel.active_task is not None:
            panel.observed_snapshots.append(panel.active_task.snapshot())
    panel.refresh = Mock(side_effect=refresh)
    return panel


class CleanupQueue:
    """Run the production serial-worker finalizer after modal destruction."""
    def __init__(self):
        self.pending = []
        self.submit = Mock(side_effect=self._submit)
        self.shutdown = Mock()
    def _submit(self, action):
        future = Future()
        self.pending.append((action, future))
        return future
    def finish(self):
        action, future = self.pending.pop(0)
        assert future.set_running_or_notify_cancel()
        try:
            future.set_result(action())
        except BaseException as exc:
            future.set_exception(exc)
        return future


@pytest.fixture
def desktop():
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    functions = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in ('report', 'run_job', '_finish_job_controls')]
    queued = []
    env = dict(__package__='carr_library.ui', wx=SimpleNamespace(CallAfter=queued.append))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), env)
    frame = SimpleNamespace(_closing=False, working=False, busy=False, cancel_event=threading.Event(),
        choice=Mock(), book=Mock(), cancel_button=Mock(), executor=Mock(), progress_panel=progress_panel())
    frame.menu_bar = Mock()
    frame.menu_bar.GetMenuCount.return_value = 2
    frame.menu_bar.GetMenuLabel.side_effect = lambda index: ('&Library', '&Help')[index]
    text, reports = ['Ready'], []
    def set_value(message):
        text[0] = message
        reports.append(message)
    def focus_status():
        frame.focus = frame.status
    frame.status = SimpleNamespace(GetValue=lambda: text[0], SetValue=set_value, SetFocus=focus_status)
    frame.focus = None
    frame.report = lambda message: env['report'](frame, message)
    frame._finish_job_controls = lambda: env['_finish_job_controls'](frame)
    frame.run_job = lambda *args, **kwargs: env['run_job'](frame, *args, **kwargs)
    return frame, queued, reports


@pytest.mark.parametrize(('receipt', 'outcome'), [
    (({'state': 'complete'}, {'state': 'quarantined'}), 'paused'),
    ([{'status': 'complete'}, {'state': 'failed'}], 'failed'),
    ({'state': 'attention'}, 'paused'), ({'state': 'pending'}, 'paused'),
    ({'state': 'running'}, 'paused'), ({'state': 'queued'}, 'paused'),
    ({'state': 'conflict'}, 'paused'), ({'state': 'superseded'}, 'paused'),
    ({'status': None, 'state': 'failed'}, 'failed'),
    ({'status': 'complete', 'state': 'failed'}, 'failed'),
    ({'state': 'complete', 'rows': [{'state': 'complete'}, {'state': 'cancelled'}]}, 'cancelled'),
    ({'state': 'complete', 'rows': [{'state': 'duplicate'}, {'state': 'excluded'}]}, 'succeeded'),
    ([{'raw_text': 'failed'}, {'raw_text': 'cancelled'}], 'succeeded'),
    ({'state': 'unrecognized-future-status'}, 'paused'),
])
def test_batch_receipts_require_actual_completion(receipt, outcome):
    assert operation_outcome(receipt) == outcome


def test_callback_publication_failure_never_announces_100_percent(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    def publish(result):
        assert frame.progress_panel.active_task.snapshot().percent is None
        frame.focus = object()
        raise OSError('Final publication did not commit')
    frame.run_job('Export', lambda: 'prepared data', publish)
    future.set_result('prepared data')
    queued.pop()()
    assert frame.progress_panel.active_task.snapshot().state == 'failed'
    assert not any(event.percent == 100 for event in frame.progress_panel.observed_snapshots)
    assert 'Final publication did not commit' in reports[-1]
    assert frame.focus is frame.status
    assert not frame.working


@pytest.mark.parametrize(('result', 'state'), [(({'state': 'quarantined'},), 'paused'),
    (({'state': 'complete'}, {'state': 'failed'}), 'failed'),
    ({'state': 'attention'}, 'paused'), ({'status': None, 'state': 'cancelled'}, 'cancelled')])
def test_desktop_uses_aggregate_outcome_instead_of_success_for_every_return(desktop, result, state):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    frame.run_job('Import', lambda: result)
    future.set_result(result)
    queued.pop()()
    assert frame.progress_panel.active_task.snapshot().state == state
    assert not any(event.percent == 100 for event in frame.progress_panel.observed_snapshots)
    assert not reports[-1].endswith(': complete.')


@pytest.mark.parametrize('scenario,state', [
    ('missing', 'failed'), ('cancelled', 'cancelled'),
    ('imported', 'succeeded'), ('resumed', 'succeeded'),
])
def test_actual_library_import_and_resume_receipts_keep_their_outcome(desktop, tmp_path, scenario, state):
    from concurrent.futures import ThreadPoolExecutor
    from carr_library.application import LibraryApp

    frame, queued, reports = desktop
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
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame.executor = executor
        frame.run_job('Actual source import', operation, receipts.append)
    assert len(queued) == 1 and not receipts
    queued.pop()()
    assert len(receipts) == 1
    actual = receipts[0] if scenario == 'resumed' else receipts[0][0]
    assert actual['state'] == {'failed': 'failed', 'cancelled': 'cancelled', 'succeeded': 'complete'}[state]
    event = frame.progress_panel.active_task.snapshot()
    assert event.state == state and not frame.working
    assert (event.percent == 100) == (state == 'succeeded')
    assert bool(library.search(workspace, 'manuscript')) == (state == 'succeeded')


def test_help_progress_query_and_quiet_mode_remain_available_during_jobs(desktop):
    frame, _, _ = desktop
    panel = frame.progress_panel
    def item(identity):
        value = Mock()
        value.GetId.return_value = identity
        value.GetSubMenu.return_value = None
        value.IsSeparator.return_value = False
        value.IsEnabled.return_value = True
        return value
    library_action, query, preferences = item(1), item(2), item(3)
    library, help_menu = Mock(), Mock()
    library.GetMenuItems.return_value = [library_action]
    help_menu.GetMenuItems.return_value = [query, preferences]
    frame.menu_bar.GetMenu.side_effect = lambda index: (library, help_menu)[index]
    frame.menu_bar.IsEnabledTop.return_value = True
    frame.GetMenuBar = lambda: frame.menu_bar
    panel.frame, panel.query_item = frame, query
    panel._busy_menus = panel._busy_items = None
    panel.settings_button, panel.mute = Mock(), Mock()
    panel.set_busy = lambda busy: NativeProgressPanel.set_busy(panel, busy)
    frame.executor.submit.return_value = Future()
    frame.run_job('Long render', lambda: None)
    assert frame.menu_bar.EnableTop.call_args_list[0].args == (0, False)
    assert frame.menu_bar.EnableTop.call_args_list[1].args == (1, True)
    query.Enable.assert_called_once_with(True)
    preferences.Enable.assert_called_once_with(False)
    panel.settings_button.Disable.assert_called_once_with()
    panel.mute.Disable.assert_not_called()


def test_success_preserves_callback_output_location_and_waits_for_it(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    def published(result):
        assert frame.progress_panel.active_task.snapshot().state == 'running'
        frame.report('Saved verified output to the selected private directory.')
    frame.run_job('Export', lambda: None, published)
    future.set_result(None)
    queued.pop()()
    assert frame.progress_panel.active_task.snapshot().percent == 100
    assert reports[-1] == 'Saved verified output to the selected private directory.'


@pytest.mark.parametrize(('result', 'state', 'label'), [
    ({'state': 'complete'}, 'succeeded', 'complete'),
    ({'state': 'attention'}, 'paused', 'needs attention'),
    ({'state': 'failed'}, 'failed', 'failed'),
    ({'state': 'cancelled'}, 'cancelled', 'cancelled'),
])
def test_completion_preserves_callback_navigation_focus_and_updates_outcome(desktop, result, state, label):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    destination = object()
    def navigate(value):
        assert value is result
        frame.focus = destination
    frame.run_job('Open selected evidence', lambda: result, navigate)
    future.set_result(result)
    queued.pop()()
    assert frame.focus is destination
    assert frame.status.GetValue() == 'Open selected evidence: ' + label + '.'
    assert frame.progress_panel.active_task.snapshot().state == state
    assert not frame.working


def test_completion_without_callback_still_focuses_the_status_report(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    frame.run_job('Refresh', lambda: None)
    frame.focus = object()
    future.set_result(None)
    queued.pop()()
    assert frame.focus is frame.status
    assert reports[-1] == 'Refresh: complete.'


def test_generic_completion_preserves_callback_owned_focus(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    frame.report = Mock(wraps=frame.report)
    callback = Mock()
    frame.run_job('Open', lambda: None, callback)
    future.set_result(None)
    queued.pop()()
    callback.assert_called_once_with(None)
    frame.report.assert_called_once_with('Open…')
    assert frame.status.GetValue() == 'Open: complete.'


@pytest.mark.parametrize('target_name', [None, 'Search status', 'Search results'])
def test_completion_summary_preserves_the_callback_focus_target(desktop, target_name):
    frame, queued, reports = desktop
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
        reports.append(message)

    frame.report = report
    future = Future()
    frame.executor.submit.return_value = future

    def display_result(result):
        # The real source-open/search handlers own a panel control, not the
        # frame status used by run_job's generic completion summary.
        target.SetValue(result)
        target.SetFocus()

    frame.run_job('Open source', lambda: 'Readable source result',
                  display_result if target_name is not None else None)
    future.set_result('Readable source result')
    queued.pop()()

    assert frame.status.GetValue() == 'Open source: complete.'
    assert focused[-1] is target
    if target_name is not None:
        assert target.GetValue() == 'Readable source result'
    assert frame.progress_panel.active_task.snapshot().percent == 100


@pytest.mark.parametrize('callback_owns_focus', [False, True])
def test_completion_preserves_callback_focus_and_keeps_default_feedback(desktop, callback_owns_focus):
    frame, queued, reports = desktop
    text, focus = ['Ready'], ['initial']
    frame.status = SimpleNamespace(GetValue=lambda: text[0],
                                   SetValue=lambda value: text.__setitem__(0, value))
    def report(message):
        text[0] = message
        focus[0] = 'frame status'
        reports.append(message)
    frame.report = report
    def source_ready(result):
        focus[0] = 'search status'
    future = Future()
    frame.executor.submit.return_value = future
    frame.run_job('Preparing original source file', lambda: None,
                  source_ready if callback_owns_focus else None)
    assert focus[0] == 'frame status'
    future.set_result(None)
    queued.pop()()
    assert text[0] == 'Preparing original source file: complete.'
    assert focus[0] == ('search status' if callback_owns_focus else 'frame status')
    assert frame.progress_panel.active_task.snapshot().state == 'succeeded'
    assert not frame.working


def test_old_completion_does_not_overwrite_a_callback_started_job(desktop):
    frame, queued, reports = desktop
    first, second = Future(), Future()
    frame.executor.submit.side_effect = [first, second]
    frame.run_job('Prepare', lambda: None, lambda value: frame.run_job('Render', lambda: None))
    first.set_result(None)
    queued.pop()()
    assert frame.working and frame.progress_panel.active_task.snapshot().title == 'Render'
    assert frame.progress_panel.active_task.snapshot().state == 'running'
    assert reports[-1] == 'Render…'


def test_generic_completion_updates_notice_without_overriding_callback_focus(desktop):
    frame, queued, reports = desktop
    focused = ['query']
    report_calls = []
    report = frame.report
    def focusing_report(message):
        report_calls.append(message)
        focused[0] = 'host-status'
        report(message)
    frame.report = focusing_report
    future = Future()
    frame.executor.submit.return_value = future
    def display_results(result):
        focused[0] = 'result-grid'
    frame.run_job('Search', lambda: ['source result'], display_results)
    future.set_result(['source result'])
    queued.pop()()
    assert focused[0] == 'result-grid'
    assert frame.status.GetValue() == 'Search: complete.'
    assert report_calls == ['Search…']
    assert frame.progress_panel.active_task.snapshot().state == 'succeeded'


@pytest.mark.parametrize('observer', ['begin', 'update', 'finish', 'refresh'])
def test_progress_observer_failure_cannot_reclassify_committed_work(desktop, observer, monkeypatch):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    broken = RuntimeError('Progress device unavailable')
    if observer == 'update':
        monkeypatch.setattr(ProgressTask, 'update', Mock(side_effect=broken))
    elif observer == 'refresh':
        # Begin succeeds. The native completion refresh fails after the durable
        # outcome; the reporting-failure marker must not change that outcome.
        frame.progress_panel.refresh = Mock(side_effect=[None, broken, None])
    else:
        setattr(frame.progress_panel, observer, Mock(side_effect=broken))
    def operation(report):
        report(1, 2, 'Importing files')
        return {'state': 'complete', 'destination': 'committed output'}
    frame.run_job('Import', operation, done, failed, with_progress=True)
    future.set_result(frame.executor.submit.call_args.args[0]())
    queued.pop()()
    done.assert_called_once_with({'state': 'complete', 'destination': 'committed output'})
    failed.assert_not_called()
    assert not frame.working and not frame.busy
    assert 'Progress reporting is unavailable' in frame.progress_panel.notice
    if frame.progress_panel.active_task is not None:
        assert frame.progress_panel._reporting_failure[1] == 'succeeded'
    else:
        assert frame.progress_panel._reporting_failure is None


def test_receipt_classifier_failure_preserves_successful_publication(desktop, monkeypatch):
    import carr_library.progress as progress_module
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    monkeypatch.setattr(progress_module, 'operation_outcome', Mock(side_effect=ValueError('Unclassifiable receipt')))
    frame.run_job('Export', lambda: 'committed', done, failed)
    future.set_result('committed')
    queued.pop()()
    done.assert_called_once_with('committed')
    failed.assert_not_called()
    assert not frame.working
    assert 'Progress reporting is unavailable' in frame.progress_panel.notice
    assert not any(event.percent == 100 for event in frame.progress_panel.observed_snapshots)


@pytest.mark.parametrize('worker_error', [InterruptedError('Worker cancelled'), OSError('Commit failed')],
                         ids=['acknowledged-cancel', 'real-commit-failure'])
def test_observer_failure_and_late_cancel_keep_the_real_business_error(desktop, worker_error):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    finish = frame.progress_panel.finish
    def failed_observer(*args):
        finish(*args)
        raise RuntimeError('Closed progress control')
    frame.progress_panel.finish = failed_observer
    frame.run_job('Export', lambda: None, done, failed)
    frame.cancel_event.set()
    future.set_exception(worker_error)
    queued.pop()()
    done.assert_not_called()
    failed.assert_called_once_with(worker_error)
    assert frame.progress_panel.active_task.snapshot().state == (
        'cancelled' if isinstance(worker_error, InterruptedError) else 'failed')
    assert str(worker_error) in reports[-1] and not frame.working


def test_executor_failure_still_unlocks_controls_when_progress_start_fails(desktop):
    frame, queued, _ = desktop
    frame.progress_panel.begin = Mock(side_effect=RuntimeError('Progress unavailable'))
    error, done, failed = RuntimeError('Executor unavailable'), Mock(), Mock()
    frame.executor.submit.side_effect = error
    frame.run_job('Export', lambda: None, done, failed)
    done.assert_not_called()
    failed.assert_called_once_with(error)
    assert not frame.working and not frame.busy and not queued


def test_measurements_use_captured_token_and_late_updates_do_not_touch_next_job(desktop):
    frame, queued, _ = desktop
    first, second, observers = Future(), Future(), []
    frame.executor.submit.side_effect = [first, second]
    def operation(report):
        observers.append(report)
        report(1, 2, 'Processing files')
        return {'state': 'complete'}
    frame.run_job('First', operation,
                  lambda value: frame.run_job('Second', lambda: None), with_progress=True)
    first.set_result(frame.executor.submit.call_args.args[0]())
    queued.pop()()
    observers[0](2, 2, 'Old operation finished')
    event = frame.progress_panel.active_task.snapshot()
    assert event.state == 'running' and event.title == 'Second'
    assert event.percent is None and event.completed == 0


@pytest.fixture
def studio():
    """Exercise the actual nested Studio job handler without an audio device."""
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    window = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    run = next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
    factory = ast.parse('''
def make(progress, executor, wx, report, service):
    busy = False
    alive = True
    dialog = SimpleNamespace(working=False)
    current_job = 'saved-production'
    cancel = threading.Event()
    controls = lambda: None
    return SimpleNamespace(run=run, cancel=cancel, is_busy=lambda: busy)
''')
    factory.body[0].body.insert(-1, run)
    env = dict(threading=threading, SimpleNamespace=SimpleNamespace, operation_outcome=operation_outcome,
               bind_progress=bind_progress, ProgressState=ProgressState)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    queued, reports = [], []
    progress, executor, service = progress_panel(), Mock(), Mock()
    def after(callback, *args):
        queued.append(lambda: callback(*args))
    handler = env['make'](progress, executor, SimpleNamespace(CallAfter=after),
                          lambda message, focus=False: reports.append(message), service)
    return SimpleNamespace(handler=handler, progress=progress, executor=executor, service=service,
                           queued=queued, reports=reports)


def test_studio_reports_measured_phases_without_fabricated_percent_and_commits_before_success(studio):
    future = Future()
    studio.executor.submit.return_value = future
    def operation(report):
        report('Mixing scene 2 of 3')
        assert studio.progress.active_task.snapshot().total is None
        assert studio.progress.active_task.snapshot().percent is None
        return {'status': 'complete', 'destination': 'private output'}
    def complete(result):
        assert studio.progress.active_task.snapshot().state == 'running'
        assert studio.progress.active_task.snapshot().percent is None
        studio.reports.append('Verified and saved ' + result['destination'])
    studio.handler.run(operation, complete, phase='Rendering audio production', with_progress=True)
    future.set_result(studio.executor.submit.call_args.args[0]())
    while studio.queued:
        studio.queued.pop(0)()
    assert studio.progress.active_task.snapshot().state == 'succeeded'
    assert studio.progress.active_task.snapshot().percent == 100
    assert all(event.percent is None for event in studio.progress.observed_snapshots[:-1])
    assert studio.reports == ['Mixing scene 2 of 3', 'Verified and saved private output']
    assert not studio.handler.is_busy()


@pytest.mark.parametrize(('state', 'exception'), [('paused', InterruptedError('Paused by user')),
    ('cancelled', InterruptedError('Cancelled by user')), ('failed', OSError('Output verification failed'))])
def test_studio_pause_cancel_and_failure_follow_saved_job_receipt_without_success(studio, state, exception):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': state}
    complete = Mock()
    studio.handler.run(lambda report: None, complete, with_progress=True)
    future.set_exception(exception)
    studio.queued.pop(0)()
    complete.assert_not_called()
    if isinstance(exception, InterruptedError):
        studio.service.get.assert_called_once_with('saved-production')
    else:
        # A real worker error remains failed; a stale saved receipt cannot
        # reclassify it as an acknowledged interruption.
        studio.service.get.assert_not_called()
    assert studio.progress.active_task.snapshot().state == state
    assert not any(event.percent == 100 for event in studio.progress.observed_snapshots)
    assert studio.reports == [str(exception)] and not studio.handler.is_busy()


def test_studio_final_publication_callback_failure_never_reaches_100_percent(studio):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': 'complete'}
    def failed_publication(result):
        raise OSError('Publication callback failed')
    studio.handler.run(lambda report: None, failed_publication, with_progress=True)
    future.set_result({'status': 'complete'})
    studio.queued.pop(0)()
    assert studio.progress.active_task.snapshot().state == 'failed'
    assert not any(event.percent == 100 for event in studio.progress.observed_snapshots)
    assert studio.reports == ['Publication callback failed']


def test_studio_executor_start_failure_is_terminal_and_unlocks_controls(studio):
    studio.executor.submit.side_effect = RuntimeError('Worker unavailable')
    complete = Mock()
    studio.handler.run(lambda: None, complete)
    complete.assert_not_called()
    assert studio.progress.active_task.snapshot().state == 'failed'
    assert not studio.handler.is_busy() and not studio.queued
    assert studio.reports == ['Worker unavailable']


@pytest.mark.parametrize('device_failure', [False, True])
@pytest.mark.parametrize('registry_failure', [False, True])
def test_reader_close_always_unregisters_playback_and_finishes_resource_cleanup(device_failure, registry_failure):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/speech_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    cleanup = next(node.finalbody for node in reversed(reader.body) if isinstance(node, ast.Try) and node.finalbody)
    env = {name: Mock() for name in ('unregister', 'autosave_timer', 'playback_timer',
        'cancel', 'player', 'progress', 'parent', 'audio_files', 'dialog')}
    env.update(__package__='carr_library.ui', executor=CleanupQueue(),
               wx=SimpleNamespace(CallAfter=lambda action: action()))
    registries, providers = [Mock(), Mock()], [Mock()]
    env['owned_registries'], env['owned_progress_voices'] = registries, providers
    if registry_failure:
        registries[0].close.side_effect = RuntimeError('Voice provider disconnected')
    if device_failure:
        env['player'].close.side_effect = RuntimeError('Audio device disconnected')
    exec(compile(ast.Module(body=cleanup, type_ignores=[]), str(path), 'exec'), env)
    env['player'].close.assert_called_once_with()
    env['progress'].unregister_player.assert_called_once_with(env['player'], silence_confirmed=not device_failure)
    env['unregister'].assert_called_once_with()
    env['autosave_timer'].Stop.assert_called_once_with()
    env['playback_timer'].Stop.assert_called_once_with()
    env['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=False)
    env['dialog'].Destroy.assert_called_once_with()
    assert env['alive'] is False
    # The UI returns without closing a registry beneath its active worker.
    for resource in (*registries, *providers):
        resource.close.assert_not_called()
    env['audio_files'].cleanup.assert_not_called()
    finished = env['executor'].finish()
    if registry_failure:
        with pytest.raises(RuntimeError, match='Voice provider disconnected'):
            finished.result()
    else:
        finished.result()
    for resource in (*registries, *providers):
        resource.close.assert_called_once_with()
    if device_failure:
        env['audio_files'].cleanup.assert_not_called()
    else:
        env['audio_files'].cleanup.assert_called_once_with()
    if device_failure or registry_failure:
        retained = env['parent']._reader_playback_retained
        assert retained and all(entry[:3] == (env['player'], env['audio_files'], env['executor'])
                                for entry in retained)
    assert env['parent'].report.call_count == int(device_failure) + int(registry_failure)


@pytest.mark.parametrize('device_failure', [False, True])
def test_studio_close_releases_device_and_both_observers_even_after_device_failure(device_failure):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    studio = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    close = next(node for node in studio.body if isinstance(node, ast.FunctionDef) and node.name == 'close')
    factory = ast.parse("""
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor, owned_progress_voices):
    alive = True
    busy = True
    current_job = 'saved-production'
    return close
""")
    factory.body[0].body.insert(-1, close)
    env = dict(__package__='carr_library.ui', wx=SimpleNamespace(ID_CLOSE=123))
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    autosave, cancel, service, timer, dialog, progress, parent_progress, report = [Mock() for _ in range(8)]
    executor, providers = CleanupQueue(), [Mock(), Mock()]
    if device_failure:
        dialog._audio_player.close.side_effect = RuntimeError('Audio device disconnected')
    callback = env['make'](autosave, cancel, service, timer, dialog, progress,
                          parent_progress, report, executor, providers)
    callback()
    dialog._audio_player.close.assert_called_once_with()
    progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=not device_failure)
    parent_progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=not device_failure)
    service.request_pause.assert_called_once_with('saved-production')
    timer.Stop.assert_called_once_with()
    progress.request_cancel.assert_called_once_with()
    progress.close.assert_called_once_with()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=False)
    dialog.EndModal.assert_called_once_with(123)
    service.close.assert_not_called()
    for provider in providers:
        provider.close.assert_not_called()
    executor.finish().result()
    service.close.assert_called_once_with()
    for provider in providers:
        provider.close.assert_called_once_with()
    assert report.call_count == int(device_failure)
