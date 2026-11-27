"""Real background services behind headless named-view controller boundaries."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant
from carr_library.ui.library_assistant import LibraryWorkflowController, install_library_assistant_actions


def _checked_child_program(program):
    """Keep the crash fixture on the parent's exact package origins."""
    from pathlib import Path
    import carr_library
    import bootstrap
    expected = [str(Path(module.__file__).resolve()) for module in (carr_library, bootstrap)]
    roots = [str(Path(path).parents[1]) for path in expected]
    return ('import sys\nassert sys.dont_write_bytecode\n'
            f'sys.path[:0] = {roots!r}\n'
            'import pathlib,carr_library,bootstrap\n'
            'assert [str(pathlib.Path(module.__file__).resolve()) for module in '
            f'(carr_library,bootstrap)] == {expected!r}\n' + program)


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


def test_load_review_and_resume_reconcile_only_the_recorded_export_publication(state):
    """The UI must carry the service's checked crash checkpoint into its plan."""
    outputs = state.tmp_path / 'outputs'
    outputs.mkdir()
    topic = state.library.create_topic(state.workspace, 'Exportable research')
    service = LocalLibraryAssistant(state.library, state.workspace, output_root=outputs)
    plan = service.plan(dict(steps=[dict(tool='export_topic',
        arguments=dict(topic=topic, filename='outline.md'))]))
    job = service.start(plan, approved=True)
    child = '''import os, sys
from carr_library.application import LibraryApp
from carr_library.library_assistant import LocalLibraryAssistant
original_link = os.link
def crash_after_publication(source, destination, *args, **kwargs):
    original_link(source, destination, *args, **kwargs)
    os._exit(73)
os.link = crash_after_publication
app = LibraryApp(sys.argv[1])
LocalLibraryAssistant.open_job(app, sys.argv[2], sys.argv[3]).run(sys.argv[3])
'''
    stopped = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', _checked_child_program(child),
        str(state.library.root), state.workspace, job['id']], timeout=20, check=False)
    assert stopped.returncode == 73
    target = outputs / 'outline.md'
    before = target.read_bytes()
    entries = tuple(sorted(path.name for path in outputs.iterdir()))
    assert target.stat().st_nlink == 2 and len(entries) == 2

    state.controller.load_job(job['id'])

    assert state.controller.job_id == job['id'] and state.view.can_run
    assert 'Saved plan loaded' in state.view.messages[-1][0]
    assert 'Completed steps: 0/1' in state.view.result
    assert target.read_bytes() == before and target.stat().st_nlink == 2
    assert tuple(sorted(path.name for path in outputs.iterdir())) == entries
    assert len(state.library.topics(state.workspace)) == 1
    state.controller.approve_run()
    assert 'Workflow complete' in state.view.messages[-1][0]
    assert not state.view.can_run and 'Completed steps: 1/1' in state.view.result
    assert target.read_bytes() == before and target.stat().st_nlink == 1
    assert tuple(path.name for path in outputs.iterdir()) == ('outline.md',)
    assert state.frame.unsaved_note == 'Keep this exact unsaved notebook draft.'


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
