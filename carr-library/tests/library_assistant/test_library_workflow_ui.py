"""Real background services behind headless named-view controller boundaries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant
from carr_library.ui.library_assistant import LibraryWorkflowController, install_library_assistant_actions


class Frame:
    def __init__(self, library, workspace):
        self.library, self.active = library, (workspace, None)
        self.working = self.busy = self._closing = False
        self.cancel_event = Event()
        self.pending, self.threads = [], []
        self.deferred = False
        self.unsaved_note = 'Keep this exact unsaved notebook draft.'
        self.refresh_scopes = Mock(side_effect=AssertionError('Do not replace the notebook.'))

    def run_job(self, title, operation, done, failed):
        assert not self.working
        self.working = self.busy = True
        self.cancel_event.clear()
        self.pending.append((operation, done, failed))
        if not self.deferred:
            self.finish()

    def finish(self):
        operation, done, failed = self.pending.pop(0)
        def execute():
            self.threads.append(get_ident())
            return operation()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(execute).result()
        except Exception as exc:
            self.working = self.busy = False
            failed(exc)
        else:
            self.working = self.busy = False
            done(result)


class View:
    def __init__(self):
        self.messages, self.busy_history = [], []
        self.can_run = False
        self.plan = self.result = ''
        self.closed = False
        self.rows = ()
        self.on_progress = None

    def enable_run(self, enabled):
        self.can_run = enabled

    def set_busy(self, busy):
        self.busy_history.append(busy)

    def report(self, text, *, focus=False):
        self.messages.append((text, focus))

    def show_plan(self, text):
        self.plan = text

    def show_result(self, text):
        self.result = text

    def show_jobs(self, rows):
        self.rows = rows

    def show_configuration(self, plan):
        self.configuration = plan

    def progress(self, text):
        if self.on_progress:
            self.on_progress(text)

    def finish_close(self):
        self.closed = True


@pytest.fixture
def state(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Selected')
    frame, view = Frame(library, workspace), View()
    return SimpleNamespace(library=library, workspace=workspace, frame=frame, view=view,
        controller=LibraryWorkflowController(frame, view), tmp_path=tmp_path)


def test_preview_approve_and_results_use_worker_and_keep_notebook(state):
    state.controller.preview('topic: Research review\ntopics')
    assert state.library.topics(state.workspace) == ()
    assert state.view.can_run and 'Review every step' in state.view.plan
    state.controller.approve_run()
    assert len(state.library.topics(state.workspace)) == 1
    assert 'Workflow complete' in state.view.messages[-1][0]
    assert state.view.messages[-1][1] is True
    assert 'Completed steps: 2/2' in state.view.result and not state.view.can_run
    assert all(identity != get_ident() for identity in state.frame.threads)
    assert state.view.busy_history == [True, False, True, False]
    assert state.frame.unsaved_note == 'Keep this exact unsaved notebook draft.'
    state.frame.refresh_scopes.assert_not_called()


def test_editing_the_goal_invalidates_approval(state):
    state.controller.preview('topic: Prior goal')
    state.controller.changed()
    state.controller.approve_run()
    assert state.library.topics(state.workspace) == ()
    assert not state.view.can_run
    assert 'Preview or load' in state.view.messages[-1][0]


def test_scope_change_before_execution_prevents_all_mutations(state):
    state.controller.preview('topic: Never publish')
    other = state.library.create_workspace('Other')
    state.frame.active = (other, None)
    state.controller.approve_run()
    assert state.library.topics(state.workspace) == state.library.topics(other) == ()
    assert 'changed' in state.view.messages[-1][0]


def test_pause_after_first_checkpoint_and_load_saved_plan(state):
    state.controller.preview('topic: First\ntopic: Second')
    state.view.on_progress = lambda message: state.frame.cancel_event.set() if message.startswith('Step 2') else None
    state.controller.approve_run()
    assert len(state.library.topics(state.workspace)) == 1
    assert 'Workflow paused' in state.view.messages[-1][0]
    identity = state.controller.job_id
    state.controller.refresh_jobs()
    assert len(state.view.rows) == 1 and state.view.rows[0]['id'] == identity
    state.view.on_progress = None
    state.controller.load_job(identity)
    assert state.view.can_run and 'Saved plan loaded' in state.view.messages[-1][0]
    state.controller.approve_run()
    assert len(state.library.topics(state.workspace)) == 2
    assert 'Workflow complete' in state.view.messages[-1][0]


def test_close_requests_checkpoint_pause_and_waits_for_worker(state):
    state.controller.preview('topic: Never start')
    state.frame.deferred = True
    state.controller.approve_run()
    assert state.controller.close() is False
    assert not state.view.closed
    state.frame.finish()
    assert state.view.closed and not state.controller.alive
    assert state.library.topics(state.workspace) == ()


def test_scope_changed_during_plan_preview_does_not_authorize_returned_plan(state):
    state.frame.deferred = True
    state.controller.preview('topic: Never publish')
    state.frame.active = (state.library.create_workspace('Another'), None)
    state.frame.finish()
    assert state.controller.plan is None
    assert not state.view.can_run
    assert state.library.topics(state.workspace) == ()


def test_failed_source_recheck_retains_goal_without_claiming_completion(state):
    source = state.tmp_path / 'source'
    source.mkdir()
    file = source / 'book.txt'
    file.write_text('Before preview')
    state.controller.preview('import: book.txt', input_root=source)
    file.write_text('After preview')
    state.controller.approve_run()
    assert 'not completed' in state.view.messages[-1][0]
    assert 'changed after preview' in state.view.messages[-1][0]
    assert state.controller.plan is not None and state.controller.job_id is None
    assert state.frame.unsaved_note == 'Keep this exact unsaved notebook draft.'


def test_install_hook_adds_one_tools_action_and_preserves_existing_menu(monkeypatch):
    import sys
    class Menu:
        def __init__(self):
            self.items = []
        def Append(self, identity, label):
            item = SimpleNamespace(id=identity, label=label)
            self.items.append(item)
            return item
    class MenuBar:
        def __init__(self):
            self.menus = [('Research', Menu()), ('&Tools', Menu())]
        def GetMenuCount(self):
            return len(self.menus)
        def GetMenuLabel(self, index):
            return self.menus[index][0]
        def GetMenu(self, index):
            return self.menus[index][1]
        def Append(self, menu, label):
            self.menus.append((label, menu))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object()))
    frame = SimpleNamespace(menu_bar=MenuBar(), Bind=Mock())
    first = install_library_assistant_actions(frame)
    assert install_library_assistant_actions(frame) is first
    assert len(frame.menu_bar.menus) == 2
    assert frame.menu_bar.menus[0][1].items == []
    assert len(frame.menu_bar.menus[1][1].items) == 1
    assert 'library workflow' in first.label
    frame.Bind.assert_called_once()



def _library_native_driver(state, *, constructor_error=None):
    """Run the actual probe's owner/readiness closures without native windows."""
    import ast
    import json
    from pathlib import Path
    import traceback

    path = Path(__file__).resolve().parents[1] / 'ui' / 'native_library_workflow_smoke.py'
    module = ast.parse(path.read_text(encoding='utf-8'))
    names = {'_workflow_host_ready', '_workflow_launch_observation', '_require_new_workflow',
             'wait_for', 'open_when_ready', 'ObservedNativeWorkflow'}
    nodes = [node for node in ast.walk(module)
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in nodes} == names and len(nodes) == len(names)
    frame = state.frame
    topic = state.library.create_topic(state.workspace, 'Existing research')
    frame.active = state.workspace, topic
    frame.book = object()
    frame.evidence = SimpleNamespace(displayed=None)
    status = ['Research library opened.']
    frame.status = SimpleNamespace(GetValue=lambda: status[0])
    frame.report = lambda text: status.__setitem__(0, text)
    observation = dict(frame=frame, view=None, phase='new')
    scheduled, opened = [], Mock()

    class NativeOwner:
        def __init__(self, owner):
            assert owner is frame
            if constructor_error is not None:
                raise constructor_error
            self.controller = SimpleNamespace(alive=True)
        def show(self):
            self.controller.alive = False

    context = dict(json=json, traceback=traceback, frame=frame, library=state.library,
        original_root=state.library.root, workspace=state.workspace, existing_topic=topic,
        original_book=frame.book, state=observation, original_view=NativeOwner,
        deadline=40, monotonic=lambda: 0,
        later=lambda callback, delay=25: scheduled.append(callback),
        open_from_menu=opened, prepare_new=Mock(), prepare_resume=Mock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(path), 'exec'), context)
    return context, scheduled, opened, status


def test_native_library_menu_waits_for_real_initial_evidence_worker_completion(state, monkeypatch):
    from carr_library.ui import library_assistant as ui

    context, scheduled, opened, status = _library_native_driver(state)
    frame = state.frame
    monkeypatch.setattr(ui, 'NativeLibraryWorkflow', context['ObservedNativeWorkflow'])
    opened.side_effect = lambda: ui.show_library_assistant(frame)
    original_root, original_scope, original_book = frame.library.root, frame.active, frame.book
    # Idle before the scheduled refresh has started is not ready evidence.
    context['open_when_ready']()
    opened.assert_not_called()
    assert len(scheduled) == 1 and not frame.working and not frame.busy
    frame.deferred = True
    def loaded(rows):
        row, = rows
        frame.evidence.displayed = row
    frame.run_job('Loading saved evidence', lambda: state.library.topics(state.workspace),
                  loaded, lambda error: pytest.fail(str(error)))
    scheduled.pop(0)()
    opened.assert_not_called()
    assert frame.working and frame.busy and len(scheduled) == 1
    frame.finish()  # An actual background library read, then its owner callback.
    scheduled.pop(0)()
    opened.assert_called_once_with()
    assert frame.threads and all(identity != get_ident() for identity in frame.threads)
    assert frame.evidence.displayed.id == original_scope[1]
    assert not frame.working and not frame.busy
    assert frame.library.root == original_root and frame.active == original_scope and frame.book is original_book
    assert context['state']['constructor_entered'] and not context['state']['view'].controller.alive
    context['_require_new_workflow'](frame, context['state'], None)
    assert len(scheduled) == 1  # The real observation class scheduled prepare_new.


@pytest.mark.parametrize('changed', ['library', 'root', 'scope', 'notebook', 'closing'])
def test_native_library_menu_refuses_changed_host_ownership_before_dispatch(state, monkeypatch, changed):
    context, scheduled, opened, status = _library_native_driver(state)
    frame = state.frame
    frame.evidence.displayed = object()
    if changed == 'library':
        frame.library = LibraryApp(state.tmp_path / 'other-library-root')
    elif changed == 'root':
        monkeypatch.setattr(state.library, 'root', state.tmp_path / 'moved-library-root')
    elif changed == 'scope':
        frame.active = state.workspace, state.library.create_topic(state.workspace, 'Another topic')
    elif changed == 'notebook':
        frame.book = object()
    else:
        frame._closing = True
    with pytest.raises(AssertionError, match='changed|closing'):
        context['open_when_ready']()
    opened.assert_not_called()
    assert not scheduled and context['state']['view'] is None


def test_native_library_initial_refresh_keeps_the_original_total_deadline_and_host_status(state):
    context, scheduled, opened, status = _library_native_driver(state)
    state.frame.working = state.frame.busy = True
    status[0] = 'Loading saved evidence remains active.'
    context['monotonic'] = lambda: 40
    with pytest.raises(AssertionError, match='before its deadline') as error:
        context['open_when_ready']()
    assert 'Loading saved evidence remains active.' in str(error.value)
    assert '"working": true' in str(error.value) and '"busy": true' in str(error.value)
    opened.assert_not_called()
    assert not scheduled and state.frame.working and state.frame.busy


@pytest.mark.parametrize('refusal', ['no-scope', 'working', 'busy', 'closing'])
def test_native_library_actual_launcher_refusal_is_reported_without_accepting_missing_owner(
        state, monkeypatch, refusal):
    from carr_library.ui import library_assistant as ui

    context, scheduled, opened, status = _library_native_driver(state)
    monkeypatch.setattr(ui, 'NativeLibraryWorkflow', context['ObservedNativeWorkflow'])
    if refusal == 'no-scope':
        state.frame.active = None
    else:
        setattr(state.frame, '_closing' if refusal == 'closing' else refusal, True)
    ui.show_library_assistant(state.frame)
    assert status[0].startswith('Library workflow assistant unavailable:')
    with pytest.raises(AssertionError, match='without a fresh library workflow owner') as error:
        context['_require_new_workflow'](state.frame, context['state'], None)
    assert status[0] in str(error.value)
    assert '"constructor_entered": false' in str(error.value)
    assert not scheduled and context['state']['view'] is None


def test_native_library_actual_constructor_error_retains_primary_trace_and_status(state, monkeypatch):
    from carr_library.ui import library_assistant as ui

    context, scheduled, opened, status = _library_native_driver(
        state, constructor_error=RuntimeError('native constructor boundary'))
    monkeypatch.setattr(ui, 'NativeLibraryWorkflow', context['ObservedNativeWorkflow'])
    ui.show_library_assistant(state.frame)
    assert context['state']['constructor_entered'] and context['state']['view'] is None
    assert 'RuntimeError: native constructor boundary' in context['state']['constructor_error']
    assert status[0] == 'Library workflow assistant unavailable: native constructor boundary'
    with pytest.raises(AssertionError, match='without a fresh library workflow owner') as error:
        context['_require_new_workflow'](state.frame, context['state'], None)
    assert 'RuntimeError: native constructor boundary' in str(error.value)
    assert status[0] in str(error.value)
    assert not scheduled


def test_native_library_reopen_requires_a_fresh_owner_and_never_closes_it_in_the_observer(state):
    context, scheduled, opened, status = _library_native_driver(state)
    previous = SimpleNamespace(controller=SimpleNamespace(alive=False))
    context['state']['view'] = previous
    with pytest.raises(AssertionError, match='without a fresh library workflow owner'):
        context['_require_new_workflow'](state.frame, context['state'], previous)
    current = SimpleNamespace(controller=SimpleNamespace(alive=True))
    context['state']['view'] = current
    context['_require_new_workflow'](state.frame, context['state'], previous)
    assert current.controller.alive and not previous.controller.alive
    assert not scheduled
