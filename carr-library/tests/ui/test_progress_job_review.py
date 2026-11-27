"""Independent regression checks for truthful desktop job completion."""
import ast
from concurrent.futures import Future
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, ProgressTask, ProgressState, bind_progress, operation_outcome
from carr_library.ui.progress_panel import NativeProgressPanel


class Progress(NativeProgressPanel):
    """Actual shared progress authority with only native controls doubled."""
    def __init__(self):
        self.hub, self.active_task, self.closed = ProgressHub(), None, False
        self.history, self.notice = [], ''
        self._last_revision = self._reporting_failure = None
        self._busy_menus = self._busy_items = None
        self.feedback, self.settings_button = Mock(), Mock()
        self.query_item = None
    def refresh(self, event=None):
        if not self.closed and self.active_task is not None:
            self.history.append(self.event)


@pytest.fixture
def desktop():
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    functions = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in ('run_job', '_finish_job_controls')]
    queued = []
    env = dict(__package__='carr_library.ui', wx=SimpleNamespace(CallAfter=queued.append))
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), env)
    frame = SimpleNamespace(_closing=False, working=False, busy=False, cancel_event=threading.Event(),
        choice=Mock(), book=Mock(), cancel_button=Mock(), executor=Mock(), operation_progress=Progress())
    frame.menu_bar = Mock()
    frame.menu_bar.GetMenuCount.return_value = 2
    frame.menu_bar.GetMenuLabel.side_effect = lambda index: ('&Library', '&Options')[index]
    frame.GetMenuBar = lambda: frame.menu_bar
    frame.progress_panel = frame.operation_progress
    frame.progress_panel.frame = frame
    query = SimpleNamespace(GetId=lambda: 17, IsEnabled=lambda: True, Enable=Mock(),
        IsSeparator=lambda: False, GetSubMenu=lambda: None)
    frame.progress_panel.query_item = query
    menus = [SimpleNamespace(GetMenuItems=lambda: []), SimpleNamespace(GetMenuItems=lambda: [query])]
    frame.menu_bar.GetMenu.side_effect = lambda index: menus[index]
    frame.menu_bar.IsEnabledTop.return_value = True
    text, reports = ['Ready'], []
    frame.status = SimpleNamespace(GetValue=lambda: text[0], SetValue=lambda value: text.__setitem__(0, value))
    def report(message):
        text[0] = message
        reports.append(message)
    frame.report = report
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
        assert frame.operation_progress.event.percent is None
        raise OSError('Final publication did not commit')
    frame.run_job('Export', lambda: 'prepared data', publish)
    future.set_result('prepared data')
    queued.pop()()
    assert frame.operation_progress.event.state == 'failed'
    assert not any(event.percent == 100 for event in frame.operation_progress.history)
    assert 'Final publication did not commit' in reports[-1]
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
    assert frame.operation_progress.event.state == state
    assert not any(event.percent == 100 for event in frame.operation_progress.history)
    assert not reports[-1].endswith(': complete.')


def test_help_progress_query_and_quiet_mode_remain_available_during_jobs(desktop):
    frame, _, _ = desktop
    frame.executor.submit.return_value = Future()
    frame.run_job('Long render', lambda: None)
    assert frame.menu_bar.EnableTop.call_args_list[0].args == (0, False)
    assert frame.menu_bar.EnableTop.call_args_list[1].args == (1, True)


def test_success_preserves_callback_output_location_and_waits_for_it(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    def published(result):
        assert frame.operation_progress.event.state == 'running'
        frame.report('Saved verified output to the selected private directory.')
    frame.run_job('Export', lambda: None, published)
    future.set_result(None)
    queued.pop()()
    assert frame.operation_progress.event.percent == 100
    assert reports[-1] == 'Saved verified output to the selected private directory.'


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
    assert frame.operation_progress.event.percent == 100


def test_old_completion_does_not_overwrite_a_callback_started_job(desktop):
    frame, queued, reports = desktop
    first, second = Future(), Future()
    frame.executor.submit.side_effect = [first, second]
    frame.run_job('Prepare', lambda: None, lambda value: frame.run_job('Render', lambda: None))
    first.set_result(None)
    queued.pop()()
    assert frame.working and frame.operation_progress.event.phase == 'Render'
    assert frame.operation_progress.event.state == 'running'
    assert reports[-1] == 'Render…'


def test_generic_completion_updates_notice_without_overriding_callback_focus(desktop):
    frame, queued, reports = desktop
    focused = ['query']
    report = frame.report
    def focusing_report(message):
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
    assert reports == ['Search…']
    assert frame.operation_progress.event.state == 'succeeded'


@pytest.mark.parametrize('observer', ['begin', 'update', 'finish', 'refresh'])
def test_progress_observer_failure_cannot_reclassify_committed_work(desktop, observer, monkeypatch):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    failing = Mock(side_effect=RuntimeError('Progress device unavailable'))
    if observer == 'update':
        monkeypatch.setattr(ProgressTask, 'update', failing)
    else:
        setattr(frame.operation_progress, observer, failing)
    def operation(report):
        report(1, 2, 'Importing files')
        return {'state': 'complete', 'destination': 'committed output'}
    frame.run_job('Import', operation, done, failed, with_progress=True)
    future.set_result(frame.executor.submit.call_args.args[0]())
    queued.pop()()
    done.assert_called_once_with({'state': 'complete', 'destination': 'committed output'})
    failed.assert_not_called()
    assert not frame.working and not frame.busy
    assert 'Progress reporting is unavailable' in frame.status.GetValue()
    assert reports == ['Import…']


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
    assert 'Progress reporting is unavailable' in frame.status.GetValue()
    assert reports == ['Export…']
    assert not any(event.percent == 100 for event in frame.operation_progress.history)


@pytest.mark.parametrize('worker_error', [InterruptedError('Worker cancelled'), OSError('Commit failed')],
                         ids=['acknowledged-cancel', 'real-commit-failure'])
def test_observer_failure_and_late_cancel_keep_the_real_business_error(desktop, worker_error):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    frame.run_job('Export', lambda: None, done, failed)
    frame.operation_progress.refresh = Mock(side_effect=RuntimeError('Closed progress control'))
    frame.cancel_event.set()
    future.set_exception(worker_error)
    queued.pop()()
    done.assert_not_called()
    failed.assert_called_once_with(worker_error)
    assert frame.operation_progress.event.state == (
        'cancelled' if isinstance(worker_error, InterruptedError) else 'failed')
    assert str(worker_error) in reports[-1] and not frame.working


def test_executor_failure_still_unlocks_controls_when_progress_start_fails(desktop):
    frame, queued, _ = desktop
    frame.operation_progress.begin = Mock(side_effect=RuntimeError('Progress unavailable'))
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
    event = frame.operation_progress.event
    assert event.state == 'running' and event.phase == 'Second'
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
    current_job = 'saved-production'
    cancel = threading.Event()
    controls = lambda: None
    dialog = SimpleNamespace(working=False)
    return SimpleNamespace(run=run, cancel=cancel, is_busy=lambda: busy)
''')
    factory.body[0].body.insert(-1, run)
    env = dict(threading=threading, SimpleNamespace=SimpleNamespace, operation_outcome=operation_outcome,
               ProgressState=ProgressState, bind_progress=bind_progress, __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    queued, reports = [], []
    progress, executor, service = Progress(), Mock(), Mock()
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
        assert studio.progress.event.total is None
        assert studio.progress.event.percent is None
        return {'status': 'complete', 'destination': 'private output'}
    def complete(result):
        assert studio.progress.event.state == 'running'
        assert studio.progress.event.percent is None
        studio.reports.append('Verified and saved ' + result['destination'])
    studio.handler.run(operation, complete, phase='Rendering audio production', with_progress=True)
    future.set_result(studio.executor.submit.call_args.args[0]())
    while studio.queued:
        studio.queued.pop(0)()
    assert studio.progress.event.state == 'succeeded'
    assert studio.progress.event.percent == 100
    assert all(event.percent is None for event in studio.progress.history[:-1])
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
        studio.service.get.assert_not_called()
    assert studio.progress.event.state == state
    assert not any(event.percent == 100 for event in studio.progress.history)
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
    assert studio.progress.event.state == 'failed'
    assert not any(event.percent == 100 for event in studio.progress.history)
    assert studio.reports == ['Publication callback failed']


def test_studio_executor_start_failure_is_terminal_and_unlocks_controls(studio):
    studio.executor.submit.side_effect = RuntimeError('Worker unavailable')
    complete = Mock()
    studio.handler.run(lambda: None, complete)
    complete.assert_not_called()
    assert studio.progress.event.state == 'failed'
    assert not studio.handler.is_busy() and not studio.queued
    assert studio.reports == ['Worker unavailable']


@pytest.mark.parametrize('device_failure', [False, True])
def test_reader_close_always_unregisters_playback_and_finishes_resource_cleanup(device_failure):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/speech_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    cleanup = next(node.finalbody for node in reversed(reader.body) if isinstance(node, ast.Try) and node.finalbody)
    env = {name: Mock() for name in ('autosave_timer', 'cancel', 'player', 'progress', 'parent',
                                      'executor', 'audio_files', 'dialog', 'unregister')}
    env.update(__package__='carr_library.ui', owned_registries=set(), owned_progress_voices=set(),
               wx=SimpleNamespace(CallAfter=lambda callback: callback()))
    future = Future()
    env['executor'].submit.return_value = future
    if device_failure:
        env['player'].close.side_effect = RuntimeError('Audio device disconnected')
    exec(compile(ast.Module(body=cleanup, type_ignores=[]), str(path), 'exec'), env)
    env['player'].close.assert_called_once_with()
    env['progress'].unregister_player.assert_called_once_with(
        env['player'], silence_confirmed=not device_failure)
    env['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=False)
    env['audio_files'].cleanup.assert_not_called()
    future.set_result(env['executor'].submit.call_args.args[0]())
    if device_failure:
        env['audio_files'].cleanup.assert_not_called()
        assert any(entry[0] is env['player'] and entry[1] is env['audio_files']
                   for entry in env['parent']._reader_playback_retained)
    else:
        env['audio_files'].cleanup.assert_called_once_with()
    env['dialog'].Destroy.assert_called_once_with()
    assert env['alive'] is False
    assert env['parent'].report.call_count == int(device_failure)


@pytest.mark.parametrize('device_failure', [False, True])
def test_studio_close_releases_device_and_both_observers_even_after_device_failure(device_failure, monkeypatch):
    from carr_library.audio import cleanup
    retained = []
    class RetryThread:
        def __init__(self, **options):
            retained.append(options['target'].__self__)
        def start(self):
            pass
    monkeypatch.setattr(cleanup.threading, 'Thread', RetryThread)
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    studio = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    callbacks = [node for node in studio.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('close', 'retire_worker', 'retire_playback')]
    factory = ast.parse("""
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor):
    alive = True
    busy = True
    cleanup_started = False
    owned_progress_voices = set()
    playback_owner = [dialog._audio_player]
    current_job = 'saved-production'
    return close
""")
    factory.body[0].body[-1:-1] = callbacks
    env = dict(wx=SimpleNamespace(ID_CLOSE=123), __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor = [Mock() for _ in range(9)]
    cleanup_future = Future()
    executor.submit.return_value = cleanup_future
    if device_failure:
        dialog._audio_player.close.side_effect = RuntimeError('Audio device disconnected')
    callback = env['make'](autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor)
    callback()
    player = dialog._audio_player
    player.close.assert_called_once_with()
    progress.unregister_player.assert_called_once_with(player, silence_confirmed=not device_failure)
    parent_progress.unregister_player.assert_called_once_with(player, silence_confirmed=not device_failure)
    service.request_pause.assert_called_once_with('saved-production')
    progress.request_cancel.assert_called_once_with()
    progress.close.assert_called_once_with()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=False)
    service.close.assert_not_called()
    cleanup_future.set_result(executor.submit.call_args.args[0]())
    service.close.assert_called_once()
    dialog.EndModal.assert_called_once_with(123)
    assert report.call_count == int(device_failure)
    if device_failure:
        assert len(retained) == 1 and retained[0] in cleanup._RETAINED
        player.close.side_effect = None
        assert retained[0].close()
        assert retained[0] not in cleanup._RETAINED
