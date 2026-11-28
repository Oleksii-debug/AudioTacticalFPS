"""Actual assistant preview callbacks keep local voice review off the GUI thread."""
import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.workflow_access import workflow_name
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library.studio.render import ProductionService
from carr_library.ui import assistant_dialog


def _actual_callbacks(state):
    """Compile the production closure, retaining its real module import context."""
    parsed = ast.parse(Path(assistant_dialog.__file__).read_text(encoding='utf-8'))
    owner = next(node for node in parsed.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'create_assistant_dialog')
    names = {'guard', 'report', 'input_identity', 'invalidate', 'preview', 'run', 'review_task', 'close'}
    wrapper = ast.parse("""def build(state):
    frame, library = state.frame, state.library
    active_scope = frame.active
    production, service = state.production, state.service
    executor, cancel, wx, dialog = state.executor, state.cancel, state.wx, state.dialog
    workspaces, scope_choice = state.workspaces, state.scope_choice
    input_folder, output_folder = state.input_folder, state.output_folder
    full_backup, saved_name, instruction = state.full_backup, state.saved_name, state.instruction
    planning_source, profiles = state.planning_source, state.profiles
    review, buttons, folder_buttons = state.review, state.buttons, ()
    alive, busy, running, plan, job_id = True, False, False, None, None
    preview_generation = 0
    approved_inputs = None
""").body[0]
    selected = [deepcopy(node) for node in owner.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in selected} == names
    wrapper.body.extend(selected)
    wrapper.body.extend(ast.parse("""def snapshot():
    return dict(alive=alive, busy=busy, running=running, plan=plan, job_id=job_id)
return SimpleNamespace(preview=preview, run=run, close=close,
                       invalidate=invalidate, snapshot=snapshot)
""").body)
    namespace = dict(vars(assistant_dialog))
    namespace.update(Scope=Scope, Plan=Plan, Step=Step, workflow_name=workflow_name,
                     SimpleNamespace=SimpleNamespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 str(assistant_dialog.__file__), 'exec'), namespace)
    return namespace['build'](state)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    main_thread = get_ident()
    state = SimpleNamespace(main_thread=main_thread, closed=False, queue=deque(),
        release=Event(), entered=Event(), provider_closed=Event(), cancel=Event(),
        inventory_threads=[], chooser_threads=[], close_threads=[], chooser_hook=None,
        answer=1, selection=0, voices_empty=False, inventory_error=None, active=Event())
    state.library = LibraryApp(tmp_path / 'library')
    workspace = state.library.create_workspace('Preview scope')
    output = tmp_path / 'output'
    output.mkdir()
    state.frame = SimpleNamespace(library=state.library, active=(workspace, None), _closing=False)

    class Control:
        def __init__(self, value='', selection=0):
            self.value, self.selection, self.enabled = value, selection, True
        def touch(self):
            assert get_ident() == main_thread, 'Native controls were touched by a worker.'
            assert not state.closed, 'A closed native dialog was touched.'
        def GetValue(self):
            self.touch()
            return self.value
        def GetSelection(self):
            self.touch()
            return self.selection
        def Enable(self, value=True):
            self.touch()
            self.enabled = bool(value)
        def Disable(self):
            self.Enable(False)
        def SetLabel(self, value):
            self.touch()
            self.label = value
        def ChangeValue(self, value):
            self.touch()
            self.value = value
        def SetFocus(self):
            self.touch()

    class Choice:
        def __init__(self, *args):
            assert get_ident() == main_thread and not state.closed
            state.chooser_threads.append(get_ident())
        def ShowModal(self):
            if state.chooser_hook is not None:
                state.chooser_hook()
            return state.answer
        def GetSelection(self):
            return state.selection
        def Destroy(self):
            pass

    class Provider:
        name = 'Synthetic inventory fixture'
        def voices(self):
            return () if state.voices_empty else (
                Voice('fixture:voice', 'Fixture local voice', 'fixture', 'en', ('volume',)),)
        def close(self):
            assert not state.active.is_set(), 'Registry closed during an active probe/render.'
            state.close_threads.append(get_ident())
            state.provider_closed.set()

    def registry_factory(root):
        assert root == state.library.root / 'state'
        state.inventory_threads.append(get_ident())
        state.active.set()
        state.entered.set()
        try:
            assert state.release.wait(5), 'Fixture voice inventory was never released.'
            if state.inventory_error:
                raise state.inventory_error
            return VoiceRegistry([Provider()])
        finally:
            state.active.clear()

    monkeypatch.setattr('carr_library.runtime_settings.voice_registry', registry_factory)
    state.production = ProductionService(state.library.root / 'state' / 'studio')
    state.service = AssistantService(state.library, production=state.production.assistant_callback)
    state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tested-assistant-preview')
    state.workspaces = [SimpleNamespace(id=workspace)]
    state.scope_choice = Control(selection=1)
    state.input_folder = Control('')
    state.output_folder = Control(str(output))
    state.full_backup = Control(False)
    state.saved_name = Control('')
    state.planning_source = Control(selection=0)
    state.profiles = ()
    state.instruction = Control(json.dumps({'steps': [
        {'tool': 'audio.produce', 'arguments': {'text': 'Synthetic narration.'}}]}))
    state.review = Control()
    state.buttons = {key: Control() for key in ('preview', 'run', 'jobs', 'save', 'reuse', 'pause')}
    state.buttons['run'].Disable()
    state.buttons['save'].Disable()
    def end_modal(_):
        assert get_ident() == main_thread
        state.closed = True
    state.dialog = SimpleNamespace(EndModal=end_modal)
    state.wx = SimpleNamespace(ID_OK=1, ID_CANCEL=2, ID_CLOSE=3,
        SingleChoiceDialog=Choice, CallAfter=lambda function, *args: state.queue.append((function, args)))

    def pump(predicate=lambda: True):
        deadline = monotonic() + 5
        while True:
            while state.queue:
                function, args = state.queue.popleft()
                function(*args)
            if predicate():
                return
            assert monotonic() < deadline, 'Assistant callback did not finish.'
            sleep(.005)
    state.pump = pump
    state.callbacks = _actual_callbacks(state)
    try:
        yield state
    finally:
        state.release.set()
        if not state.closed:
            state.callbacks.close()
        state.executor.shutdown(wait=True, cancel_futures=False)
        state.pump()


def _finished(state):
    state.release.set()
    state.pump(lambda: not state.callbacks.snapshot()['busy'])


def test_actual_voice_factory_runs_off_gui_thread_and_preview_executes_nothing(scenario):
    s = scenario
    s.callbacks.preview()
    assert s.entered.wait(2)
    assert len(s.inventory_threads) == 1 and s.inventory_threads[0] != s.main_thread
    assert s.callbacks.snapshot()['busy'] and s.buttons['pause'].enabled
    assert not s.buttons['run'].enabled and not s.buttons['save'].enabled
    assert not s.chooser_threads
    _finished(s)
    plan = s.callbacks.snapshot()['plan']
    assert plan.steps[0].arguments['voice_ids'] == ['fixture:voice']
    assert s.chooser_threads == [s.main_thread]
    assert s.service.store.identities() == ()
    assert list(Path(s.output_folder.value).iterdir()) == []


def test_default_choice_preserves_other_steps_explicit_voice_assignments(scenario):
    s = scenario
    s.instruction.value = json.dumps({'steps': [
        {'tool': 'audio.produce', 'arguments': {'text': 'Assigned.', 'voice_ids': ['chosen:explicit']}},
        {'tool': 'audio.produce', 'arguments': {'text': 'Needs a default.'}}]})
    s.callbacks.preview()
    _finished(s)
    plan = s.callbacks.snapshot()['plan']
    assert plan.steps[0].arguments['voice_ids'] == ['chosen:explicit']
    assert plan.steps[1].arguments['voice_ids'] == ['fixture:voice']
    assert s.service.store.identities() == ()


@pytest.mark.parametrize('change', ['pause', 'source_scope', 'instruction'])
def test_cancelled_or_changed_preview_never_opens_voice_chooser_or_authorizes(scenario, change):
    s = scenario
    s.callbacks.preview()
    assert s.entered.wait(2)
    if change == 'pause':
        s.cancel.set()
    elif change == 'source_scope':
        s.frame.active = ('different-workspace', None)
    else:
        s.instruction.value = 'list libraries'
        s.callbacks.invalidate()
    _finished(s)
    assert s.callbacks.snapshot()['plan'] is None
    assert not s.buttons['run'].enabled and not s.buttons['save'].enabled
    assert not s.chooser_threads and s.service.store.identities() == ()


def test_close_during_inventory_returns_then_closes_owned_registry_exactly_once(scenario):
    s = scenario
    s.callbacks.preview()
    assert s.entered.wait(2)
    started = monotonic()
    s.callbacks.close()
    s.callbacks.close()
    assert monotonic() - started < .3
    assert s.closed and not s.provider_closed.is_set()
    s.release.set()
    assert s.provider_closed.wait(3)
    s.pump()
    assert len(s.close_threads) == 1 and s.close_threads[0] != s.main_thread
    assert not s.chooser_threads and s.callbacks.snapshot()['plan'] is None


def test_completed_inventory_queued_after_close_is_disposed_without_touching_ui(scenario):
    s = scenario
    s.callbacks.preview()
    s.release.set()
    deadline = monotonic() + 3
    while not s.queue:
        assert monotonic() < deadline
        sleep(.005)
    assert not s.chooser_threads
    s.callbacks.close()
    assert s.provider_closed.wait(3)
    s.pump()
    assert len(s.close_threads) == 1 and not s.chooser_threads


@pytest.mark.parametrize('change', ['pause', 'source_scope', 'instruction', 'close', 'decline'])
def test_voice_chooser_completion_requires_current_uncancelled_review(scenario, change):
    s = scenario
    def changed():
        if change == 'pause':
            s.cancel.set()
        elif change == 'source_scope':
            s.frame.active = ('different-workspace', None)
        elif change == 'instruction':
            s.callbacks.invalidate()
        elif change == 'close':
            s.callbacks.close()
        else:
            s.answer = s.wx.ID_CANCEL
    s.chooser_hook = changed
    s.callbacks.preview()
    _finished(s)
    assert s.callbacks.snapshot()['plan'] is None
    assert s.service.store.identities() == ()
    if change != 'close':
        assert not s.buttons['run'].enabled and not s.buttons['save'].enabled


@pytest.mark.parametrize('failure', ['empty', 'unavailable'])
def test_voice_inventory_failure_cannot_reuse_a_previous_approved_plan(scenario, failure):
    s = scenario
    s.instruction.value = 'list libraries'
    s.callbacks.preview()
    assert s.callbacks.snapshot()['plan'] is not None
    s.instruction.value = json.dumps({'steps': [
        {'tool': 'audio.produce', 'arguments': {'text': 'Synthetic narration.'}}]})
    if failure == 'empty':
        s.voices_empty = True
    else:
        s.inventory_error = ValueError('Selected local voice registration changed.')
    s.callbacks.preview()
    _finished(s)
    assert s.callbacks.snapshot()['plan'] is None
    assert not s.buttons['run'].enabled and not s.buttons['save'].enabled
    assert not s.chooser_threads


def test_owned_registry_closes_after_an_active_render_callback_not_on_gui_thread(scenario, monkeypatch):
    s = scenario
    s.callbacks.preview()
    _finished(s)
    render_entered, render_release = Event(), Event()
    def render(identity, **kwargs):
        s.active.set()
        render_entered.set()
        try:
            assert render_release.wait(5)
            assert kwargs['cancelled']()
            return None
        finally:
            s.active.clear()
    monkeypatch.setattr(s.service, 'resume', render)
    try:
        s.callbacks.run()
        assert render_entered.wait(2)
        s.callbacks.close()
        assert not s.provider_closed.is_set()
    finally:
        render_release.set()
    assert s.provider_closed.wait(3)
    s.pump()
    assert len(s.close_threads) == 1 and s.close_threads[0] != s.main_thread


def test_non_audio_preview_needs_no_registry_and_keeps_synchronous_review(scenario):
    s = scenario
    s.instruction.value = 'list libraries'
    s.callbacks.preview()
    assert s.callbacks.snapshot()['plan'].steps[0].tool == 'library.list'
    assert s.buttons['run'].enabled and s.buttons['save'].enabled
    assert not s.inventory_threads
    s.callbacks.close()
    s.executor.shutdown(wait=True)
    assert not s.close_threads


def test_a_borrowed_production_registry_is_never_closed_by_the_modal(scenario):
    s = scenario
    s.release.set()
    calls = []
    class BorrowedProvider:
        name = 'Borrowed provider'
        def voices(self):
            return ()
        def close(self):
            calls.append('closed')
    borrowed = VoiceRegistry([BorrowedProvider()])
    # Use the existing constructor's ownership contract, rather than assigning
    # a borrowed registry to a service that was constructed as its owner.
    s.production = ProductionService(s.library.root / 'state' / 'studio', voices=borrowed)
    s.service = AssistantService(s.library, production=s.production.assistant_callback)
    s.callbacks = _actual_callbacks(s)
    s.callbacks.close()
    s.executor.shutdown(wait=True)
    assert len(borrowed._providers) == 1 and calls == []
    assert not s.inventory_threads
    borrowed.close()
    assert calls == ['closed']

