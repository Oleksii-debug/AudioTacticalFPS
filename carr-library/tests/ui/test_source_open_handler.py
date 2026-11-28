"""Native launch policy with actual worker/event-thread handoff, no device claim."""
import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import carr_library

from carr_library.ui.source_launch import call_on_ui


def viewing_copy():
    return SimpleNamespace(path=Path('/viewing/source.txt'), ref='selected',
        identity=SimpleNamespace(workspace='library', revision='historical'), title='Book',
        locator=SimpleNamespace(display=lambda: 'PDF page 7 (printed page unverified)'))


@pytest.fixture
def panel():
    path = Path(carr_library.__file__).resolve().parent / 'ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'open_original')
    queue, event_thread = deque(), get_ident()
    source = viewing_copy()
    def launch(app, value, opener, *, native_dispatch, **kwargs):
        assert get_ident() != event_thread
        return native_dispatch(lambda: opener(str(value.path)))
    scope = {'prepare_source_file': Mock(return_value=source),
             'launch_source_file': Mock(side_effect=launch), 'call_on_ui': call_on_ui,
             'wx': SimpleNamespace(CallAfter=queue.append,
                                   LaunchDefaultApplication=Mock(return_value=True))}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
    hit = SimpleNamespace(workspace='library', ref='selected', revision='historical')
    obj = SimpleNamespace(frame=SimpleNamespace(working=False, library=object(), run_job=Mock(),
                       report=Mock(), cancel_event=Event(), active=('library', 'topic'), _closing=False),
        selected=Mock(return_value=hit), _context_epoch=5, status=Mock(),
        bound_workspace='library', bound_topic='topic', model=SimpleNamespace(workspace='library'))
    obj.bound_library = obj.frame.library
    executor = ThreadPoolExecutor(max_workers=1)
    state = SimpleNamespace(scope=scope, obj=obj, queue=queue, executor=executor,
                            source=source, event_thread=event_thread, future=None)
    yield state
    obj.frame.cancel_event.set()
    # Abandoned queued closures must be inert even when delivered during shutdown.
    while queue:
        queue.popleft()()
    executor.shutdown(wait=True, cancel_futures=True)


def start(panel):
    panel.scope['open_original'](panel.obj, None)
    _, operation, panel.done = panel.obj.frame.run_job.call_args.args
    panel.failed = panel.obj.frame.run_job.call_args.kwargs['failed']
    panel.future = panel.executor.submit(operation)
    deadline = monotonic() + 5
    while not panel.queue and not panel.future.done():
        if monotonic() >= deadline:
            raise AssertionError('The actual source worker did not queue its native handoff.')
        sleep(.001)
    return panel.future


def settle(panel):
    deadline = monotonic() + 5
    while not panel.future.done():
        if monotonic() >= deadline:
            raise AssertionError('The actual source worker did not settle.')
        if panel.queue:
            panel.queue.popleft()()
        else:
            sleep(.001)
    while panel.queue:
        panel.queue.popleft()()
    try:
        source = panel.future.result()
    except Exception as error:
        panel.failed(error)
        return error
    else:
        panel.done(source)
        return source


def test_source_is_only_prepared_and_launched_after_explicit_action(panel):
    scope, obj = panel.scope, panel.obj
    scope['prepare_source_file'].assert_not_called()
    start(panel)
    scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert not panel.future.done()
    assert settle(panel) is panel.source
    scope['prepare_source_file'].assert_called_once_with(obj.frame.library, 'library', 'selected', 'historical',
        authorized_topic='topic', cancelled=obj.frame.cancel_event.is_set)
    call = scope['launch_source_file'].call_args
    assert call.args[:2] == (obj.frame.library, panel.source)
    assert callable(call.kwargs['native_dispatch'])
    assert call.kwargs['authorized_topic'] == 'topic'
    scope['wx'].LaunchDefaultApplication.assert_called_once_with(str(panel.source.path))
    text = obj.status.ChangeValue.call_args.args[0]
    assert 'viewing copy' in text and 'PDF page 7' in text and 'external viewer to navigate' in text
    obj.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('change', ['epoch', 'workspace', 'revision', 'ref'])
def test_changed_selection_never_launches_a_prepared_file(panel, change):
    start(panel)
    if change == 'epoch':
        panel.obj._context_epoch += 1
    else:
        setattr(panel.obj.selected.return_value, change, 'changed')
    assert isinstance(settle(panel), ValueError)
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'changed' in panel.obj.status.ChangeValue.call_args.args[0]


def test_busy_application_does_not_prepare_or_launch_files(panel):
    panel.obj.frame.working = True
    panel.scope['open_original'](panel.obj, None)
    panel.obj.frame.run_job.assert_not_called()
    panel.scope['prepare_source_file'].assert_not_called()
    panel.scope['launch_source_file'].assert_not_called()


def test_no_selection_reports_readable_error_without_launch(panel):
    panel.obj.selected.side_effect = ValueError('Choose a search result first.')
    panel.scope['open_original'](panel.obj, None)
    panel.obj.frame.run_job.assert_not_called()
    assert 'Choose a search result' in panel.obj.status.ChangeValue.call_args.args[0]
    panel.obj.status.SetFocus.assert_called_once()


def test_launch_authority_failure_is_reported_and_focus_is_restored(panel):
    panel.scope['launch_source_file'].side_effect = ValueError('The selected library is archived.')
    start(panel)
    assert isinstance(settle(panel), ValueError)
    assert 'archived' in panel.obj.status.ChangeValue.call_args.args[0]
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()
    panel.obj.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('changed', ['bound', 'model', 'active', 'root'])
def test_foreign_workspace_never_prepares_original(panel, changed):
    obj = panel.obj
    if changed == 'bound':
        obj.bound_workspace = 'other'
    elif changed == 'model':
        obj.model.workspace = 'other'
    elif changed == 'active':
        obj.frame.active = ('library', 'other-topic')
    else:
        obj.frame.library = object()
    panel.scope['open_original'](obj, None)
    obj.frame.run_job.assert_not_called()
    panel.scope['prepare_source_file'].assert_not_called()


def test_cancel_after_preparation_never_launches_external_viewer(panel):
    start(panel)
    panel.obj.frame.cancel_event.set()
    assert isinstance(settle(panel), InterruptedError)
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'cancelled' in panel.obj.status.ChangeValue.call_args.args[0]


def test_preparation_receives_live_cancellation_callback(panel):
    start(panel)
    cancelled = panel.scope['prepare_source_file'].call_args.kwargs['cancelled']
    assert not cancelled()
    panel.obj.frame.cancel_event.set()
    assert cancelled() and isinstance(settle(panel), InterruptedError)


@pytest.mark.parametrize('phase', ['worker', 'queued-native', 'native-launch'])
@pytest.mark.parametrize('change', ['root', 'bound-library', 'active-topic',
                                     'bound-topic', 'bound-workspace', 'model'])
def test_changed_library_or_topic_never_retargets_or_launches_a_source(panel, phase, change):
    obj = panel.obj
    library = obj.frame.library
    def change_scope():
        if change == 'root':
            obj.frame.library = object()
        elif change == 'bound-library':
            obj.bound_library = object()
        elif change == 'active-topic':
            obj.frame.active = ('library', 'another-topic')
        elif change == 'bound-topic':
            obj.bound_topic = 'another-topic'
        elif change == 'bound-workspace':
            obj.bound_workspace = 'another-library'
        else:
            obj.model.workspace = 'another-library'
    if phase == 'worker':
        panel.scope['open_original'](obj, None)
        _, operation, panel.done = obj.frame.run_job.call_args.args
        panel.failed = obj.frame.run_job.call_args.kwargs['failed']
        change_scope()
        panel.future = panel.executor.submit(operation)
        assert isinstance(settle(panel), ValueError)
        panel.scope['prepare_source_file'].assert_not_called()
    else:
        if phase == 'native-launch':
            def after_validation(app, source, opener, *, native_dispatch, **kwargs):
                assert app is library
                def action():
                    change_scope()
                    return opener(str(source.path))
                return native_dispatch(action)
            panel.scope['launch_source_file'].side_effect = after_validation
        start(panel)
        if phase == 'queued-native':
            change_scope()
        assert isinstance(settle(panel), ValueError)
        assert panel.scope['prepare_source_file'].call_args.args[0] is library
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'changed' in obj.status.ChangeValue.call_args.args[0]


@pytest.mark.parametrize('field', ['workspace', 'revision', 'ref'])
def test_prepared_source_must_match_selected_historical_identity_before_validation(panel, field):
    setattr(panel.source if field == 'ref' else panel.source.identity, field, 'different')
    start(panel)
    assert isinstance(settle(panel), ValueError)
    panel.scope['launch_source_file'].assert_not_called()
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'prepared source changed' in panel.obj.status.ChangeValue.call_args.args[0]


def test_source_worker_never_reads_native_selection_controls_or_opens_the_viewer(panel):
    hit = panel.obj.selected.return_value
    reads = []
    def selected():
        reads.append(get_ident())
        assert get_ident() == panel.event_thread
        return hit
    panel.obj.selected.side_effect = selected
    def open_native(path):
        assert get_ident() == panel.event_thread
        return True
    panel.scope['wx'].LaunchDefaultApplication.side_effect = open_native
    start(panel)
    assert settle(panel) is panel.source
    assert len(reads) >= 2 and set(reads) == {panel.event_thread}


def test_cancellation_is_rechecked_immediately_before_external_launch(panel):
    def cancelled_at_native(app, source, opener, *, native_dispatch, **kwargs):
        def action():
            panel.obj.frame.cancel_event.set()
            return opener(str(source.path))
        return native_dispatch(action)
    panel.scope['launch_source_file'].side_effect = cancelled_at_native
    start(panel)
    assert isinstance(settle(panel), InterruptedError)
    panel.scope['wx'].LaunchDefaultApplication.assert_not_called()


def test_stop_after_native_acknowledgement_does_not_report_that_nothing_opened(panel):
    start(panel)
    deadline = monotonic() + 5
    while not panel.future.done():
        if monotonic() >= deadline:
            raise AssertionError('The native handoff did not settle before completion review.')
        if panel.queue:
            panel.queue.popleft()()
        else:
            sleep(.001)
    source = panel.future.result()
    panel.obj.frame.cancel_event.set()
    panel.done(source)
    assert panel.scope['wx'].LaunchDefaultApplication.call_count == 1
    assert panel.obj.status.ChangeValue.call_args.args[0].startswith('Opened a viewing copy')


def test_finished_native_action_cannot_overwrite_a_later_library_status(panel):
    start(panel)
    deadline = monotonic() + 5
    while not panel.future.done():
        if monotonic() >= deadline:
            raise AssertionError('The native handoff did not settle before completion review.')
        if panel.queue:
            panel.queue.popleft()()
        else:
            sleep(.001)
    source = panel.future.result()
    panel.obj.frame.library = object()
    panel.obj.status.reset_mock()
    panel.done(source)
    panel.obj.status.ChangeValue.assert_not_called()
