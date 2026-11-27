"""Queued native-handler admission uses the shared host, not a second worker.

The wx shell is a small explicit fixture. Actual controls are exercised by the
separate installed native probe; these cases assert real handler source.
"""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.security._files import TrustError


SOURCE = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/trust_status.py'


def handlers(*names, **extra):
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    selected = []
    requested = {'_scope_current', '_run_job', *names}
    for item in tree.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in requested:
            selected.append(item)
        elif isinstance(item, ast.ClassDef) and item.name == 'TrustStatusDialog':
            selected.extend(node for node in item.body
                            if isinstance(node, ast.FunctionDef) and node.name in requested)
    assert {item.name for item in selected} == requested
    namespace = dict(TrustError=TrustError, **extra)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    return namespace


def host(tmp_path):
    queued, reports = [], []
    controller = object()
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path / 'library'),
        trust_controller=controller, _closing=False, cancel_event=threading.Event(),
        report=reports.append)
    def run_job(title, operation, done=None, failed=None):
        frame.cancel_event.clear()
        queued.append((title, operation, done, failed))
    frame.run_job = run_job
    return frame, controller, queued, reports


def change(frame, kind):
    if kind == 'library-object':
        frame.library = SimpleNamespace(root=frame.library.root)
    elif kind == 'library-root':
        frame.library.root = frame.library.root.parent / 'other-library'
    elif kind == 'controller':
        frame.trust_controller = object()
    else:
        frame._closing = True


@pytest.mark.parametrize('kind', ['library-object', 'library-root', 'controller', 'closing'])
def test_queued_trust_work_refuses_changed_host_before_controller_access(tmp_path, kind):
    frame, controller, queued, reports = host(tmp_path)
    api = handlers()
    touched = []
    api['_run_job'](frame, controller, 'Verify explicit review',
                    lambda cancelled: touched.append(cancelled()))
    change(frame, kind)
    with pytest.raises(TrustError, match='cancelled or its host changed'):
        queued[0][1]()
    assert touched == [] and reports == []


@pytest.mark.parametrize('kind', ['library-object', 'controller', 'closing'])
def test_late_trust_callbacks_cannot_reopen_or_report_into_another_host(tmp_path, kind):
    frame, controller, queued, reports = host(tmp_path)
    api = handlers()
    completed = object()
    seen = []
    api['_run_job'](frame, controller, 'Read explicit client',
                    lambda cancelled: completed,
                    done=seen.append, failed=lambda error: reports.append('failed'))
    assert queued[0][1]() is completed
    change(frame, kind)
    queued[0][2](completed)
    queued[0][3](TrustError('synthetic failure'))
    assert seen == reports == []


def test_current_trust_completion_preserves_committed_result_after_late_cancel(tmp_path):
    frame, controller, queued, reports = host(tmp_path)
    api = handlers()
    committed, seen = object(), []
    api['_run_job'](frame, controller, 'Import approved grant',
                    lambda cancelled: committed if not cancelled() else None,
                    done=seen.append)
    result = queued[0][1]()
    frame.cancel_event.set()
    queued[0][2](result)
    assert seen == [committed]


def test_active_trust_operation_receives_current_scope_and_cancel_predicate(tmp_path):
    frame, controller, queued, reports = host(tmp_path)
    api = handlers()
    observations = []
    def operation(cancelled):
        observations.append(cancelled())
        frame.cancel_event.set()
        observations.append(cancelled())
        frame.cancel_event.clear()
        change(frame, 'library-object')
        observations.append(cancelled())
    api['_run_job'](frame, controller, 'Selected signed renewal', operation)
    queued[0][1]()
    assert observations == [False, True, True]


def test_permission_picker_library_switch_never_queues_selected_file(tmp_path):
    frame, controller, queued, reports = host(tmp_path)
    dialogs = []
    class Chooser:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def ShowModal(self):
            change(frame, 'library-object')
            return 1
        def GetPath(self):
            return str(tmp_path / 'explicit-permission.json')
    wx = SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_OPEN=1, FD_FILE_MUST_EXIST=2)
    api = handlers('on_import', wx=wx)
    dialog = SimpleNamespace(frame=frame, controller=controller,
        _current=api['_scope_current'](frame, controller), EndModal=dialogs.append)
    api['on_import'](dialog)
    assert queued == dialogs == reports == []


def test_rejected_permission_picker_keeps_dialog_and_never_starts_a_job(tmp_path):
    frame, controller, queued, reports = host(tmp_path)
    class Chooser:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def ShowModal(self):
            return 0
        def GetPath(self):
            pytest.fail('A cancelled picker has no approved path.')
    wx = SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_OPEN=1, FD_FILE_MUST_EXIST=2)
    api = handlers('on_import', wx=wx)
    dialog = SimpleNamespace(frame=frame, controller=controller,
        _current=api['_scope_current'](frame, controller),
        EndModal=lambda result: pytest.fail('Cancel must keep the status dialog open.'))
    api['on_import'](dialog)
    assert queued == reports == []
