"""Native action handoff protocol; headless tests are not NVDA acceptance."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def viewing_copy():
    return SimpleNamespace(path=Path('/viewing/source.txt'),ref='selected',
        identity=SimpleNamespace(workspace='library',revision='historical'),title='Book',
        locator=SimpleNamespace(display=lambda: 'PDF page 7 (printed page unverified)'))


@pytest.fixture
def panel():
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'open_original')
    scope = {'prepare_source_file': Mock(return_value=viewing_copy()),
             'launch_source_file': Mock(side_effect=lambda app,source,opener,**kwargs: opener(str(source.path))),
             'wx': SimpleNamespace(LaunchDefaultApplication=Mock(return_value=True))}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
    hit = SimpleNamespace(workspace='library', ref='selected', revision='historical')
    obj = SimpleNamespace(frame=SimpleNamespace(working=False, library=object(),run_job=Mock(),
                                               report=Mock(),cancel_event=Event(),active=('library','topic')),
                          selected=Mock(return_value=hit),_context_epoch=5,status=Mock(),
                          bound_workspace='library',bound_topic='topic',model=SimpleNamespace(workspace='library'))
    obj.bound_library = obj.frame.library
    return scope, obj


def test_source_is_only_prepared_and_launched_after_explicit_action(panel):
    scope, obj = panel
    scope['open_original'](obj, None)
    scope['prepare_source_file'].assert_not_called()
    scope['launch_source_file'].assert_not_called()
    _, operation, done = obj.frame.run_job.call_args.args
    source = operation()
    scope['prepare_source_file'].assert_called_once_with(obj.frame.library,'library','selected','historical',
        authorized_topic='topic',cancelled=obj.frame.cancel_event.is_set)
    done(source)
    call = scope['launch_source_file'].call_args
    scope['launch_source_file'].assert_called_once_with(obj.frame.library,source,call.args[2],
        authorized_topic='topic',cancelled=obj.frame.cancel_event.is_set)
    scope['wx'].LaunchDefaultApplication.assert_called_once_with(str(source.path))
    text = obj.status.ChangeValue.call_args.args[0]
    assert 'viewing copy' in text and 'PDF page 7' in text and 'external viewer to navigate' in text
    obj.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('change', ['epoch', 'workspace', 'revision', 'ref'])
def test_changed_selection_never_launches_a_prepared_file(panel, change):
    scope, obj = panel
    scope['open_original'](obj, None)
    done = obj.frame.run_job.call_args.args[2]
    if change == 'epoch':
        obj._context_epoch += 1
    else:
        setattr(obj.selected.return_value, change, 'changed')
    done(object())
    scope['launch_source_file'].assert_not_called()
    assert 'selection changed' in obj.status.ChangeValue.call_args.args[0]


def test_busy_application_does_not_prepare_or_launch_files(panel):
    scope, obj = panel
    obj.frame.working = True
    scope['open_original'](obj, None)
    obj.frame.run_job.assert_not_called()
    scope['prepare_source_file'].assert_not_called()
    scope['launch_source_file'].assert_not_called()


def test_no_selection_reports_readable_error_without_launch(panel):
    scope, obj = panel
    obj.selected.side_effect = ValueError('Choose a search result first.')
    scope['open_original'](obj, None)
    obj.frame.run_job.assert_not_called()
    assert 'Choose a search result' in obj.status.ChangeValue.call_args.args[0]
    obj.status.SetFocus.assert_called_once()


def test_launch_authority_failure_is_reported_and_focus_is_restored(panel):
    scope, obj = panel
    scope['open_original'](obj, None)
    scope['launch_source_file'].side_effect = ValueError('The selected library is archived.')
    obj.frame.run_job.call_args.args[2](viewing_copy())
    assert 'archived' in obj.status.ChangeValue.call_args.args[0]
    obj.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('changed',['bound','model','active','root'])
def test_foreign_workspace_never_prepares_original(panel,changed):
    scope,obj = panel
    if changed == 'bound':
        obj.bound_workspace = 'other'
    elif changed == 'model':
        obj.model.workspace = 'other'
    elif changed == 'active':
        obj.frame.active = ('library','other-topic')
    else:
        obj.frame.library = object()
    scope['open_original'](obj,None)
    obj.frame.run_job.assert_not_called()
    scope['prepare_source_file'].assert_not_called()


def test_cancel_after_preparation_never_launches_external_viewer(panel):
    scope,obj = panel
    scope['open_original'](obj,None)
    _,operation,done = obj.frame.run_job.call_args.args
    prepared = operation()
    obj.frame.cancel_event.set()
    done(prepared)
    scope['launch_source_file'].assert_not_called()
    assert 'cancelled' in obj.status.ChangeValue.call_args.args[0]


def test_preparation_receives_live_cancellation_callback(panel):
    scope,obj = panel
    scope['open_original'](obj,None)
    operation = obj.frame.run_job.call_args.args[1]
    operation()
    cancelled = scope['prepare_source_file'].call_args.kwargs['cancelled']
    assert not cancelled()
    obj.frame.cancel_event.set()
    assert cancelled()


@pytest.mark.parametrize('phase',['worker','completion','native-launch'])
@pytest.mark.parametrize('change',['root','bound-library','active-topic','bound-topic','bound-workspace','model'])
def test_changed_library_or_topic_never_retargets_or_launches_a_source(panel,phase,change):
    scope,obj = panel
    library = obj.frame.library
    scope['open_original'](obj,None)
    _,operation,done = obj.frame.run_job.call_args.args

    def change_scope():
        if change == 'root':
            # Identical workspace/topic IDs in another data root remain distinct.
            obj.frame.library = object()
        elif change == 'bound-library':
            obj.bound_library = object()
        elif change == 'active-topic':
            obj.frame.active = ('library','another-topic')
        elif change == 'bound-topic':
            obj.bound_topic = 'another-topic'
        elif change == 'bound-workspace':
            obj.bound_workspace = 'another-library'
        else:
            obj.model.workspace = 'another-library'

    if phase == 'worker':
        change_scope()
        with pytest.raises(ValueError,match='changed'):
            operation()
        scope['prepare_source_file'].assert_not_called()
        scope['launch_source_file'].assert_not_called()
    else:
        source = operation()
        assert scope['prepare_source_file'].call_args.args[0] is library
        if phase == 'completion':
            change_scope()
        else:
            def launch_after_scope_change(app,source,opener,**kwargs):
                assert app is library
                change_scope()
                return opener(str(source.path))
            scope['launch_source_file'].side_effect = launch_after_scope_change
        done(source)
        if phase == 'completion':
            scope['launch_source_file'].assert_not_called()
        assert 'changed' in obj.status.ChangeValue.call_args.args[0]
        obj.status.SetFocus.assert_called_once()
    scope['wx'].LaunchDefaultApplication.assert_not_called()


@pytest.mark.parametrize('field',['workspace','revision','ref'])
def test_prepared_source_must_match_the_selected_historical_identity(panel,field):
    scope,obj = panel
    scope['open_original'](obj,None)
    _,operation,done = obj.frame.run_job.call_args.args
    source = operation()
    setattr(source if field == 'ref' else source.identity,field,'different')
    done(source)
    scope['launch_source_file'].assert_not_called()
    scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'prepared source changed' in obj.status.ChangeValue.call_args.args[0]


def test_source_worker_does_not_read_native_selection_controls(panel):
    scope,obj = panel
    scope['open_original'](obj,None)
    obj.selected.reset_mock()
    obj.frame.run_job.call_args.args[1]()
    obj.selected.assert_not_called()


def test_cancellation_is_rechecked_immediately_before_external_launch(panel):
    scope,obj = panel
    scope['open_original'](obj,None)
    _,operation,done = obj.frame.run_job.call_args.args
    source = operation()
    def cancel_before_native_launch(app,source,opener,**kwargs):
        obj.frame.cancel_event.set()
        return opener(str(source.path))
    scope['launch_source_file'].side_effect = cancel_before_native_launch
    done(source)
    scope['wx'].LaunchDefaultApplication.assert_not_called()
    assert 'cancelled' in obj.status.ChangeValue.call_args.args[0]
