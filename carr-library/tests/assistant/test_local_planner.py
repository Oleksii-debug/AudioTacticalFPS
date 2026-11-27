"""Real caller scopes, gateway ledger and assistant; synthetic local model only."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.local_planner import LocalPlanner, LocalPlanReview
from carr_library.providers import ProfileStore, ProviderProfile, BudgetLedger


class Transport:
    def __init__(self):
        self.calls = []
        self.proposal = {'schema':'carr-assistant-plan/1','steps':[{'tool':'library.list','arguments':{}}],
                         'summary':'Suggested listing'}
        self.after = lambda:None
        self.fail = False
    def post(self, profile, route, body, headers, **kwargs):
        if kwargs['cancelled']():
            raise InterruptedError('Withdrawn before synthetic transmission')
        self.calls.append((profile,route,json.loads(body),dict(headers)))
        if self.fail:
            raise RuntimeError('private model diagnostic must not appear')
        self.after()
        content = self.proposal if isinstance(self.proposal,str) else json.dumps(self.proposal)
        return json.dumps({'choices':[{'message':{'content':content}}]}).encode()


@pytest.fixture
def state(tmp_path):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Selected library')
    inputs, outputs = tmp_path/'inputs', tmp_path/'outputs'
    inputs.mkdir(); outputs.mkdir()
    (inputs/'selected.txt').write_text('PRIVATE_CONTENT_NOT_FOR_MODEL_Україна',encoding='utf-8')
    profile = ProfileStore(app.root/'api-connections.json').save(ProviderProfile(
        'local', 'Explicit local model', 'openai-compatible', 'http://127.0.0.1:8080/v1',
        'caller-installed-model', ('llm',), local=True))
    remote = ProfileStore(app.root/'api-connections.json').save(ProviderProfile(
        'remote', 'Excluded remote', 'openai-compatible', 'https://example.com/v1',
        'remote-model', ('llm',)))
    transport = Transport()
    assistant = AssistantService(app)
    planner = LocalPlanner(app,assistant=assistant,transport=transport)
    scope = Scope(workspace,(inputs,),outputs)
    return SimpleNamespace(app=app, workspace=workspace, inputs=inputs, outputs=outputs,
        profile=profile,remote=remote, transport=transport,assistant=assistant,planner=planner,scope=scope)


def review(state, goal='List the libraries I already own.'):
    return state.planner.prepare(goal,state.scope,state.profile.id)


def draft(state, selected=None, **kwargs):
    selected = selected or review(state)
    return state.planner.draft(selected,approved_review_sha256=selected.digest,**kwargs)


def test_prepare_shows_exact_local_prompt_and_does_not_read_selected_documents_or_send(state):
    selected = review(state)
    data = selected.data
    assert data['goal'] in data['prompt'] and str(state.inputs) in data['prompt']
    assert 'PRIVATE_CONTENT_NOT_FOR_MODEL' not in data['prompt']
    assert data['mode']=='LOCAL_ONLY' and data['max_calls']==1 and data['budget_micros']==0
    assert not state.transport.calls and not state.assistant.jobs()
    assert state.planner.available_profiles() == (state.profile,)
    assert not (state.app.root/'api-operations.sqlite').exists()


def test_draft_uses_shared_gateway_and_ledger_without_running_tools(state):
    selected = review(state)
    result = draft(state,selected)
    profile,route,payload,headers = state.transport.calls[0]
    assert profile == state.profile and route=='/chat/completions'
    assert payload['messages'][0]['content']==selected.data['prompt']
    assert payload['model']==state.profile.model and headers=={}
    assert result.plan.scope == state.scope
    assert [step.tool for step in result.plan.steps]==['library.list']
    assert not state.assistant.jobs()
    records = BudgetLedger(state.app.root/'api-operations.sqlite').report(selected.id)
    assert len(records)==1 and records[0]['state']=='complete'
    assert result.request_sha256==selected.data['request_sha256']


def test_proposed_create_runs_only_with_separate_existing_plan_approval(state):
    state.scope = Scope(allow_create=True)
    state.transport.proposal = {'schema':'carr-assistant-plan/1',
        'steps':[{'tool':'library.create','arguments':{'name':'Explicit model proposal'}}],
        'summary':'Create the chosen library'}
    result = draft(state,review(state,'Create a library named Explicit model proposal.'))
    assert [row.name for row in state.app.workspaces()]==['Selected library']
    assert not state.assistant.jobs()
    with pytest.raises(PermissionError):
        state.assistant.submit(result.plan,approved_plan_sha256='0'*64)
    job = state.assistant.submit(result.plan,approved_plan_sha256=result.plan.digest)
    report = state.assistant.run(job,approved_plan_sha256=result.plan.digest)
    assert report.status=='complete'
    assert sorted(row.name for row in state.app.workspaces())==['Explicit model proposal','Selected library']


@pytest.mark.parametrize('change',['remote','profile','missing','review-copy','wrong-digest','cleared'])
def test_unapproved_profile_or_review_never_reaches_gateway(state,change):
    selected = review(state)
    digest = selected.digest
    if change=='remote':
        with pytest.raises(PermissionError):
            state.planner.prepare('List libraries',state.scope,state.remote.id)
        return
    if change=='profile':
        state.planner.profiles.save(replace(state.profile,model='changed-model'))
    elif change=='missing':
        state.planner.profiles.remove(state.profile.id)
    elif change=='review-copy':
        selected = LocalPlanReview(selected.id,selected.digest,selected.details)
    elif change=='wrong-digest':
        digest = '0'*64
    else:
        state.planner.clear()
    with pytest.raises((ValueError,PermissionError)):
        state.planner.draft(selected,approved_review_sha256=digest)
    assert state.transport.calls == [] and not state.assistant.jobs()


@pytest.mark.parametrize('kind',['scope','unknown-tool','unknown-argument','outside-path','duplicate','nonfinite','markdown','empty'])
def test_untrusted_model_output_never_expands_permissions_or_executes(state,tmp_path,kind):
    proposal = state.transport.proposal
    if kind=='scope':
        proposal['scope']={**state.scope.to_dict(),'allow_full_backup':True}
    elif kind=='unknown-tool':
        proposal['steps']=[{'tool':'shell.execute','arguments':{'command':'not allowed'}}]
    elif kind=='unknown-argument':
        proposal['steps'][0]['arguments']={'shell':'not allowed'}
    elif kind=='outside-path':
        outside = tmp_path/'unselected.txt'
        outside.write_text('Untouched outside source',encoding='utf-8')
        proposal['steps']=[{'tool':'library.import','arguments':{'path':str(outside)}}]
    elif kind=='duplicate':
        proposal='{"schema":"carr-assistant-plan/1","steps":[],"steps":[{"tool":"library.list"}]}'
    elif kind=='nonfinite':
        proposal='{"schema":"carr-assistant-plan/1","steps":[{"tool":"library.search","arguments":{"query":"x","limit":NaN}}]}'
    elif kind=='markdown':
        proposal=chr(96)*3+'json\n'+json.dumps(proposal)+'\n'+chr(96)*3
    else:
        proposal['steps']=[]
    state.transport.proposal = proposal
    with pytest.raises((ValueError,PermissionError)):
        draft(state)
    assert not state.assistant.jobs()
    assert [row.name for row in state.app.workspaces()]==['Selected library']
    assert len(state.transport.calls)==1


def test_cancelled_or_consumed_model_review_is_never_automatically_repeated(state):
    selected = review(state)
    state.transport.after = state.planner.clear
    with pytest.raises(InterruptedError):
        draft(state,selected)
    assert len(state.transport.calls)==1 and not state.assistant.jobs()
    with pytest.raises(PermissionError):
        draft(state,selected)
    assert len(state.transport.calls)==1


def test_uncertain_model_failure_requires_new_explicit_review(state):
    selected = review(state)
    state.transport.fail = True
    with pytest.raises(ValueError) as error:
        draft(state,selected)
    assert 'private model diagnostic' not in str(error.value)
    with pytest.raises(PermissionError):
        draft(state,selected)
    assert len(state.transport.calls)==1 and not state.assistant.jobs()


@pytest.mark.parametrize('change',['root','profile','clear'])
def test_reentrant_cancellation_cannot_retain_stale_authority(state,tmp_path,change):
    selected = review(state)
    replacement = LibraryApp(tmp_path/'replacement')
    def mutate():
        if change=='root':
            state.planner.app = replacement
        elif change=='profile':
            state.planner.profiles.save(replace(state.profile,model='new-model'))
        else:
            state.planner.clear()
        return False
    with pytest.raises((InterruptedError,PermissionError,ValueError)):
        draft(state,selected,cancelled=mutate)
    assert not state.transport.calls and not state.assistant.jobs()
    assert not (replacement.root/'api-operations.sqlite').exists()


def test_restart_rejects_another_planners_in_memory_review(state):
    selected = review(state)
    other = LocalPlanner(state.app,assistant=state.assistant,transport=state.transport)
    with pytest.raises(PermissionError):
        other.draft(selected,approved_review_sha256=selected.digest)
    assert state.transport.calls == []
