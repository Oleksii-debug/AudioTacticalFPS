"""Native action handoff protocol; headless tests are not NVDA acceptance."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def panel():
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'open_original')
    scope = {'prepare_source_file': Mock(), 'launch_source_file': Mock(),
             'wx': SimpleNamespace(LaunchDefaultApplication=Mock(return_value=True))}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
    hit = SimpleNamespace(workspace='library', ref='selected', revision='historical')
    obj = SimpleNamespace(frame=SimpleNamespace(working=False, library=object(), run_job=Mock(), report=Mock()),
                          selected=Mock(return_value=hit), _context_epoch=5, status=Mock())
    return scope, obj


def test_source_is_only_prepared_and_launched_after_explicit_action(panel):
    scope, obj = panel
    scope['open_original'](obj, None)
    scope['prepare_source_file'].assert_not_called()
    scope['launch_source_file'].assert_not_called()
    _, operation, done = obj.frame.run_job.call_args.args
    operation()
    scope['prepare_source_file'].assert_called_once_with(obj.frame.library, 'library', 'selected', 'historical')
    source = SimpleNamespace(title='Book', locator=SimpleNamespace(display=lambda: 'PDF page 7 (printed page unverified)'))
    done(source)
    scope['launch_source_file'].assert_called_once_with(obj.frame.library, source, scope['wx'].LaunchDefaultApplication)
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
    obj.frame.run_job.call_args.args[2](object())
    assert 'archived' in obj.status.ChangeValue.call_args.args[0]
    obj.status.SetFocus.assert_called_once()
