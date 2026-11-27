"""Actual local-model worker ownership with no live model or network."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import Scope
from carr_library.assistant.local_planner import LocalPlanner
from carr_library.providers import ProviderProfile, ProfileStore
from carr_library.ui.assistant_actions import AssistantActions
from carr_library.ui.local_planner_actions import LocalPlanningActions
from test_portable_sync import DeferredExecutor


class Transport:
    def __init__(self):
        self.calls = []
        self.after = lambda:None
    def post(self, profile, route, body, headers, **kwargs):
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before local-model transmission')
        self.calls.append(json.loads(body))
        self.after()
        plan = {'schema':'carr-assistant-plan/1','steps':[{'tool':'library.list','arguments':{}}],
                'summary':'Proposed list only'}
        return json.dumps({'choices':[{'message':{'content':json.dumps(plan)}}]}).encode()


@pytest.fixture
def state(tmp_path):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Model draft scope')
    frame = SimpleNamespace(library=app,active=(workspace,None),_closing=False)
    parent = AssistantActions(frame)
    transport = Transport()
    profile = ProfileStore(app.root/'api-connections.json').save(ProviderProfile(
        'local','Local fixture','openai-compatible','http://127.0.0.1:8080/v1',
        'synthetic-no-real-model',('llm',),local=True))
    planner = LocalPlanner(app,assistant=parent.service,transport=transport)
    actions = LocalPlanningActions(parent,planner=planner)
    return SimpleNamespace(app=app,workspace=workspace,frame=frame,parent=parent,
        transport=transport,profile=profile,actions=actions,scope=Scope(workspace))


def prepare(state):
    state.parent.invalidate()
    return state.actions.prepare('List my libraries.',state.scope,state.profile.id)


def test_model_drafting_does_not_submit_or_run_an_assistant_job(state):
    selected = prepare(state)
    assert selected.data['goal']=='List my libraries.' and not state.transport.calls
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation,future = state.actions.start(executor)
        result = future.result(timeout=5)
    assert state.actions.finish(generation)
    assert state.parent.plan is None and not state.parent.service.jobs()
    state.parent.accept(result.plan)  # Existing typed Preview; execution stays separate.
    assert not state.parent.service.jobs()
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation,future = state.parent.start(executor)
        report = future.result(timeout=5)
    assert state.parent.finish(generation) and report.status=='complete'
    assert len(state.parent.service.jobs())==1 and len(state.transport.calls)==1


@pytest.mark.parametrize('change',['edit','scope','library','close'])
def test_queued_local_model_work_cannot_outlive_its_parent_review(state,tmp_path,change):
    prepare(state)
    executor = DeferredExecutor()
    generation,future = state.actions.start(executor)
    if change=='edit':
        state.parent.invalidate(); state.actions.invalidate()
    elif change=='scope':
        state.frame.active=None
    elif change=='library':
        state.frame.library=LibraryApp(tmp_path/'replacement')
    else:
        state.parent.close(); state.actions.close()
    executor.run()
    with pytest.raises(InterruptedError):
        future.result()
    assert not state.actions.finish(generation) and not state.actions.busy
    assert state.transport.calls==[] and not state.parent.service.jobs()


def test_parent_edit_alone_withdraws_an_unstarted_model_review(state):
    prepare(state)
    state.parent.invalidate()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(PermissionError):
            state.actions.start(executor)
    assert not state.transport.calls


def test_real_inflight_model_finishes_before_closed_owner_releases_busy(state):
    prepare(state)
    entered,release = Event(),Event()
    def held():
        entered.set()
        assert release.wait(5)
    state.transport.after = held
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation,future = state.actions.start(executor)
        assert entered.wait(5)
        state.parent.close(); state.actions.close()
        assert state.actions.busy and not future.done()
        release.set()
        with pytest.raises(InterruptedError):
            future.result(timeout=5)
    assert not state.actions.finish(generation) and not state.actions.busy
    assert not state.parent.service.jobs() and state.parent.plan is None


def test_rejected_real_executor_restores_idle_without_request_or_approval(state):
    prepare(state)
    transitions = []
    executor = ThreadPoolExecutor(max_workers=1)
    executor.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        state.actions.start(executor,set_running=transitions.append)
    assert transitions==[True,False] and not state.actions.busy
    assert not state.transport.calls and state.actions.review is None
    prepare(state)
    with ThreadPoolExecutor(max_workers=1) as replacement:
        generation,future=state.actions.start(replacement)
        assert future.result(timeout=5).plan
    assert state.actions.finish(generation)


def test_previous_model_completion_cannot_clear_a_new_worker_owner(state):
    executor = DeferredExecutor()
    prepare(state)
    old,first = state.actions.start(executor)
    executor.run()
    assert state.actions.finish(old)
    prepare(state)
    current,second = state.actions.start(executor)
    assert not state.actions.finish(old) and state.actions.busy
    executor.run()
    assert state.actions.finish(current) and second.result().plan
    assert len(state.transport.calls)==2


def test_injected_planner_cannot_use_another_library(state,tmp_path):
    other=LibraryApp(tmp_path/'unrelated')
    with pytest.raises(PermissionError):
        LocalPlanningActions(state.parent,planner=LocalPlanner(other))
    assert not state.transport.calls
