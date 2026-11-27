"""Independent regression checks for truthful desktop job completion."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.progress import ProgressHub, ProgressState, ProgressTask, bind_progress, operation_outcome
from carr_library.ui.progress_panel import NativeProgressPanel


class Progress(NativeProgressPanel):
    """Use the production observer/model with only native painting replaced."""

    def __init__(self, frame=None):
        self.frame = frame
        self.hub = ProgressHub()
        self.active_task = None
        self.history = []
        self.closed = False
        self._last_revision = self._reporting_failure = None
        self._busy_menus = self._busy_items = None
        self.query_item = None
        self.notice = ''
        self.feedback, self.settings_button, self.mute = Mock(), Mock(), Mock()

    @property
    def event(self):
        return self.active_task.snapshot() if self.active_task is not None else None

    def refresh(self, event=None):
        value = self.event
        if value is not None and (not self.history or self.history[-1] != value):
            self.history.append(value)


def native_menu_item(identity, enabled=True):
    item = Mock()
    state = [enabled]
    item.GetId.return_value = identity
    item.IsSeparator.return_value = False
    item.GetSubMenu.return_value = None
    item.IsEnabled.side_effect = lambda: state[0]
    item.Enable.side_effect = lambda value=True: state.__setitem__(0, bool(value))
    return item


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
        choice=Mock(), book=Mock(), cancel_button=Mock(), executor=Mock())
    frame.progress_panel = Progress(frame)
    frame.menu_bar = Mock()
    frame.GetMenuBar = lambda: frame.menu_bar
    menus = [Mock(), Mock()]
    library_action, query, settings = (native_menu_item(identity) for identity in (101, 102, 103))
    menus[0].GetMenuItems.return_value = [library_action]
    menus[1].GetMenuItems.return_value = [query, settings]
    frame.progress_panel.query_item = query
    frame.menu_items = SimpleNamespace(query=query, settings=settings, library=library_action)
    enabled = [True, True]
    frame.menu_bar.GetMenuCount.return_value = 2
    frame.menu_bar.GetMenu.side_effect = lambda index: menus[index]
    frame.menu_bar.GetMenuLabel.side_effect = lambda index: ('&Library', '&Options')[index]
    frame.menu_bar.IsEnabledTop.side_effect = lambda index: enabled[index]
    frame.menu_bar.EnableTop.side_effect = lambda index, value: enabled.__setitem__(index, bool(value))
    text, reports = ['Ready'], []
    frame.status = SimpleNamespace(GetValue=lambda: text[0])
    def report(message):
        text[0] = message
        reports.append(message)
    frame.report = report
    frame.status.SetValue = report
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
        assert frame.progress_panel.event.percent is None
        raise OSError('Final publication did not commit')
    frame.run_job('Export', lambda: 'prepared data', publish)
    future.set_result('prepared data')
    queued.pop()()
    assert frame.progress_panel.event.state == 'failed'
    assert not any(event.percent == 100 for event in frame.progress_panel.history)
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
    assert frame.progress_panel.event.state == state
    assert not any(event.percent == 100 for event in frame.progress_panel.history)
    assert not reports[-1].endswith(': complete.')


def test_help_progress_query_and_quiet_mode_remain_available_during_jobs(desktop):
    frame, _, _ = desktop
    frame.executor.submit.return_value = Future()
    frame.run_job('Long render', lambda: None)
    assert frame.menu_bar.EnableTop.call_args_list[0].args == (0, False)
    assert frame.menu_bar.EnableTop.call_args_list[1].args == (1, True)
    # Query moved into the canonical Options menu. Its real native binding and
    # the separate mute control remain available; editing settings stays disabled.
    assert frame.menu_items.query.IsEnabled()
    assert not frame.menu_items.settings.IsEnabled()
    frame.progress_panel.settings_button.Disable.assert_called_once_with()
    frame.progress_panel.mute.Disable.assert_not_called()


def test_success_preserves_callback_output_location_and_waits_for_it(desktop):
    frame, queued, reports = desktop
    future = Future()
    frame.executor.submit.return_value = future
    def published(result):
        assert frame.progress_panel.event.state == 'running'
        frame.report('Saved verified output to the selected private directory.')
    frame.run_job('Export', lambda: None, published)
    future.set_result(None)
    queued.pop()()
    assert frame.progress_panel.event.percent == 100
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
    assert frame.progress_panel.event.percent == 100


def test_old_completion_does_not_overwrite_a_callback_started_job(desktop):
    frame, queued, reports = desktop
    first, second = Future(), Future()
    frame.executor.submit.side_effect = [first, second]
    frame.run_job('Prepare', lambda: None, lambda value: frame.run_job('Render', lambda: None))
    first.set_result(None)
    queued.pop()()
    assert frame.working and frame.progress_panel.event.title == 'Render'
    assert frame.progress_panel.event.state == 'running'
    assert reports[-1] == 'Render…'


@pytest.mark.parametrize('observer', ['begin', 'update', 'finish', 'refresh'])
def test_progress_observer_failure_cannot_reclassify_committed_work(desktop, observer, monkeypatch):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    failure = Mock(side_effect=RuntimeError('Progress device unavailable'))
    if observer == 'update':
        monkeypatch.setattr(ProgressTask, 'update', failure)
    elif observer == 'refresh':
        refresh = frame.progress_panel.refresh
        def final_refresh():
            if frame.progress_panel.event is not None and frame.progress_panel.event.terminal:
                return failure()
            return refresh()
        monkeypatch.setattr(frame.progress_panel, 'refresh', final_refresh)
    else:
        monkeypatch.setattr(frame.progress_panel, observer, failure)
    def operation(report):
        report(2, 2, 'Importing files')
        return {'state': 'complete', 'destination': 'committed output'}
    frame.run_job('Import', operation, done, failed, with_progress=True)
    future.set_result(frame.executor.submit.call_args.args[0]())
    queued.pop()()
    done.assert_called_once_with({'state': 'complete', 'destination': 'committed output'})
    failed.assert_not_called()
    assert not frame.working and not frame.busy
    assert 'Progress reporting is unavailable' in reports[-1]
    failure.assert_called()


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
    assert 'Progress reporting is unavailable' in reports[-1]
    assert not any(event.percent == 100 for event in frame.progress_panel.history)


@pytest.mark.parametrize('worker_error', [InterruptedError('Worker cancelled'), OSError('Commit failed')],
                         ids=['acknowledged-cancel', 'real-commit-failure'])
def test_observer_failure_and_late_cancel_keep_the_real_business_error(desktop, worker_error):
    frame, queued, reports = desktop
    future, done, failed = Future(), Mock(), Mock()
    frame.executor.submit.return_value = future
    refresh = frame.progress_panel.refresh
    def final_refresh():
        if frame.progress_panel.event is not None and frame.progress_panel.event.terminal:
            raise RuntimeError('Closed progress control')
        return refresh()
    frame.progress_panel.refresh = final_refresh
    frame.run_job('Export', lambda: None, done, failed)
    frame.cancel_event.set()
    future.set_exception(worker_error)
    queued.pop()()
    done.assert_not_called()
    failed.assert_called_once_with(worker_error)
    assert frame.progress_panel.event.state == (
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
    event = frame.progress_panel.event
    assert event.state == 'running' and event.title == 'Second'
    assert event.percent is None and event.completed == 0 and event.total is None


@pytest.fixture
def studio():
    """Exercise the actual nested Studio job handler without an audio device."""
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    window = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = ('dispose', 'transfer_progress_owner', 'release_progress_owner', 'run')
    helpers = [next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == name)
               for name in names]
    factory = ast.parse('''
def make(progress, executor, wx, report, service):
    busy = False
    alive = True
    current_job = 'saved-production'
    cancel = threading.Event()
    controls = lambda: None
    dialog = SimpleNamespace(working=False)
    frame = SimpleNamespace(_closing=False, report=report, _studio_progress_voice_retained=[])
    owned_progress_voices = set()
    progress_ownership_lock = threading.RLock()
    return SimpleNamespace(run=run, cancel=cancel, is_busy=lambda: busy, dispose=dispose,
        owned=owned_progress_voices, release_owner=release_progress_owner, frame=frame, dialog=dialog)
''')
    factory.body[0].body[-1:-1] = helpers
    env = dict(threading=threading, SimpleNamespace=SimpleNamespace, operation_outcome=operation_outcome,
               bind_progress=bind_progress, ProgressState=ProgressState)
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
    studio.service.get.assert_called_once_with('saved-production')
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


@pytest.mark.parametrize('cleanup_pending', [False, True])
def test_reader_host_delegates_audio_cleanup_without_blocking_native_close(cleanup_pending):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/speech_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    cleanup = next(node.finalbody for node in reversed(reader.body) if isinstance(node, ast.Try) and node.finalbody)
    names = ('detach_reader', 'transfer_owner', 'release_owner', 'close_resources')
    helpers = [next(node for node in reader.body
                    if isinstance(node, ast.FunctionDef) and node.name == name) for name in names]
    factory = ast.parse('''
def make(autosave_timer, cancel, live, executor, parent, owner, appearance, unregister):
    alive, resources_closed = True, False
    clipboard_controls = None
    ownership_lock = threading.RLock()
    owned_registries = {owner}
    return close_resources, lambda: alive, release_owner, owned_registries
''')
    factory.body[0].body[-1:-1] = helpers
    factory_environment = dict(threading=threading)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), factory_environment)
    env = {name: Mock() for name in ('autosave_timer', 'cancel', 'live', 'executor', 'dialog', 'parent')}
    env['parent']._reader_registry_retained = []
    owner = Mock()
    owner.close.return_value = not cleanup_pending
    env['appearance'] = Mock(spec=['close'])
    env['unregister'] = Mock()
    completion = Future()
    if not cleanup_pending:
        completion.set_result(None)
    env['live'].close.return_value = completion
    close_resources, is_alive, release_owner, owned_registries = factory_environment['make'](
        *(env[name] for name in ('autosave_timer', 'cancel', 'live', 'executor', 'parent')), owner, env['appearance'], env['unregister'])
    env['close_resources'] = close_resources
    close_resources()  # The native Close callback may precede the modal's final unwind.
    exec(compile(ast.Module(body=cleanup, type_ignores=[]), str(path), 'exec'), env)
    env['live'].close.assert_called_once_with()
    env['executor'].shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    env['cancel'].set.assert_called_once_with()
    env['autosave_timer'].Stop.assert_called_once_with()
    env['appearance'].close.assert_called_once_with()
    env['unregister'].assert_called_once_with()
    env['dialog'].Destroy.assert_called_once_with()
    assert is_alive() is False
    assert completion.done() is not cleanup_pending
    owner.close.assert_called_once_with()
    assert owned_registries == ({owner} if cleanup_pending else set())
    assert env['parent']._reader_registry_retained == ([owner] if cleanup_pending else [])
    if cleanup_pending:
        # Actual failed ownership remains reachable after the dialog's unwind.
        # A confirmed retry, not the native close event, removes that owner.
        owner.close.return_value = True
        release_owner(owner)
        assert owner.close.call_count == 2
        assert not owned_registries and not env['parent']._reader_registry_retained


@pytest.mark.parametrize('device_failure', [False, True])
def test_studio_close_releases_device_and_both_observers_even_after_device_failure(device_failure):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    studio = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = ('dispose', 'transfer_progress_owner', 'release_progress_owner', 'release_service', 'cleanup', 'close')
    helpers = [next(node for node in studio.body
                    if isinstance(node, ast.FunctionDef) and node.name == name) for name in names]
    factory = ast.parse('''
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor, unregister):
    alive = True
    busy = True
    cleanup_started = False
    current_job = 'saved-production'
    frame = SimpleNamespace(report=report)
    owned_progress_voices = set()
    progress_ownership_lock = threading.RLock()
    return close, cleanup, lambda: alive
''')
    factory.body[0].body[-1:-1] = helpers
    env = dict(__package__='carr_library.ui', wx=SimpleNamespace(ID_CLOSE=123, CallAfter=lambda callback: callback()),
               SimpleNamespace=SimpleNamespace, threading=threading)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    autosave, cancel, service, timer, dialog, progress, parent_progress, report = [Mock() for _ in range(8)]
    unregister = Mock()
    dialog._playback_generation = 0
    dialog._playback_navigation = Mock()
    if device_failure:
        dialog._audio_player.close.side_effect = RuntimeError('Audio device disconnected')
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    released = threading.Event()
    owned_executor = ThreadPoolExecutor(max_workers=1)
    executor = Mock(wraps=owned_executor)

    def owned_work():
        started.set()
        assert release.wait(10), 'The test did not release the owned Studio work.'
        finished.set()

    def release_service_after_work():
        assert finished.is_set(), 'The voice registry was closed before its owned work ended.'
        released.set()
        return True

    service.close.side_effect = release_service_after_work
    working = owned_executor.submit(owned_work)
    try:
        assert started.wait(5)
        callback, cleanup, is_alive = env['make'](
            autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor, unregister)
        callback()
        # A duplicate native-close callback and final unwind do not repeat cleanup.
        callback()
        cleanup()
        assert is_alive() is False
        autosave.assert_called_once_with()
        timer.Stop.assert_called_once_with()
        service.request_pause.assert_called_once_with('saved-production')
        dialog._audio_player.close.assert_called_once_with()
        progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=not device_failure)
        parent_progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=not device_failure)
        progress.request_cancel.assert_called_once_with()
        progress.pause.assert_not_called()  # Cancellation request is not a worker pause acknowledgement.
        unregister.assert_called_once_with()
        progress.close.assert_called_once_with()
        dialog._playback_navigation.close.assert_called_once_with()
        assert dialog._playback_generation == 1
        executor.submit.assert_called_once()
        assert executor.submit.call_args.args[0].__name__ == 'release_service'
        # The queued real source helper must survive shutdown behind active work.
        executor.shutdown.assert_called_once_with(wait=False, cancel_futures=False)
        service.close.assert_not_called()
        assert not working.done()
        dialog.EndModal.assert_called_once_with(123)
        assert report.call_count == int(device_failure)
    finally:
        release.set()
        owned_executor.shutdown(wait=True, cancel_futures=False)
    working.result()
    service.close.assert_called_once_with()
    assert released.is_set()


def test_studio_late_voice_result_retains_failed_owner_without_touching_closed_controls(studio):
    future, adopted = Future(), Mock()
    studio.executor.submit.return_value = future
    provider = Mock()
    provider.close.return_value = False
    studio.handler.run(lambda: provider, adopted, phase='Selecting progress voice')
    studio.handler.dispose()
    future.set_result(provider)
    assert not studio.queued and not studio.handler.is_busy()
    assert not studio.handler.dialog.working
    adopted.assert_not_called()
    provider.close.assert_called_once_with()
    assert studio.handler.owned == {provider}
    assert studio.handler.frame._studio_progress_voice_retained == [provider]
    provider.close.return_value = True
    assert studio.handler.release_owner(provider)
    assert not studio.handler.owned and not studio.handler.frame._studio_progress_voice_retained


@pytest.mark.parametrize('failure', ['false', 'raises'])
def test_studio_service_owner_survives_modal_collection_until_actual_cleanup_acknowledges(monkeypatch, failure):
    import gc
    import weakref
    from carr_library.audio import cleanup as cleanup_module

    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    studio = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = ('transfer_progress_owner', 'release_progress_owner', 'release_service')
    helpers = [next(node for node in studio.body if isinstance(node, ast.FunctionDef) and node.name == name)
               for name in names]
    factory = ast.parse("""
def make(service, frame):
    owned_progress_voices = set()
    progress_ownership_lock = threading.RLock()
    return release_service
""")
    factory.body[0].body[-1:-1] = helpers
    env = dict(__package__='carr_library.ui', threading=threading,
               wx=SimpleNamespace(CallAfter=lambda callback: callback()))
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), env)
    # Even rejection of the retry thread must retain the production CleanupOwner
    # for a bounded explicit retry after the modal/executor closure is gone.
    monkeypatch.setattr(cleanup_module.threading.Thread, 'start',
                        Mock(side_effect=RuntimeError('Synthetic retry-thread admission failure')))

    class Service:
        def __init__(self):
            self.ready = False
            self.calls = 0
        def close(self):
            self.calls += 1
            if not self.ready:
                if failure == 'raises':
                    raise OSError('Native voice worker is still shutting down')
                return False
            return True

    service = Service()
    reference = weakref.ref(service)
    frame = SimpleNamespace(_closing=False, report=Mock())
    release = env['make'](service, frame)
    release()
    assert len(frame._studio_service_cleanup_owners) == 1
    owner = frame._studio_service_cleanup_owners[0]
    assert not owner.quiescent
    assert service.calls == 1
    assert 'cleanup' in frame.report.call_args.args[0].casefold()
    del service, release
    gc.collect()
    assert reference() is not None
    try:
        reference().ready = True
        assert owner.close() is True
        assert owner.quiescent and not frame._studio_service_cleanup_owners
        assert reference().calls == 2
    finally:
        if reference() is not None:
            reference().ready = True
        owner.close()
    del owner
    gc.collect()
    assert reference() is None
