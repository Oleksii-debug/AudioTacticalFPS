"""Actual gateway/ledger validation with a synthetic local response transport."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.llm import (
    PlanNeedsInput, local_plan_request, parse_local_proposal, propose_local_plan,
)
from carr_library.providers import (
    BudgetLedger, JobApproval, ProviderError, ProviderGateway, ProviderProfile,
)


def proposal(tool='library.list', arguments=None):
    return {'schema': 'carr-assistant-plan/1', 'summary': 'Proposed only',
            'steps': [{'tool': tool, 'arguments': arguments or {}}]}


@pytest.fixture
def setup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    profile = ProviderProfile('local-fixture', 'Synthetic local endpoint',
        'openai-compatible', 'http://127.0.0.1:8080/v1', 'fixture-model', ('llm',), local=True)
    calls = []
    result = {'document': proposal()}
    class Transport:
        def post(self, selected, route, body, headers, **kwargs):
            calls.append((selected, route, json.loads(body), dict(headers)))
            return json.dumps({'choices': [{'message': {
                'content': json.dumps(result['document'])}}]}).encode('utf-8')
    gateway = ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'), transport=Transport())
    return SimpleNamespace(app=app, service=service, profile=profile,
        gateway=gateway, calls=calls, result=result, scope=Scope())


def approve(value, instruction='List my libraries'):
    request = local_plan_request(instruction)
    return JobApproval(uuid4().hex, value.profile.fingerprint,
        (request.fingerprint(value.profile),), allow_loopback=True)


def test_proposal_uses_current_gateway_but_never_submits_or_executes(setup, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A model proposal executed or submitted a job.')
    monkeypatch.setattr(setup.service, 'submit', forbidden)
    monkeypatch.setattr(setup.service, 'run', forbidden)
    plan = propose_local_plan(setup.service, setup.gateway, setup.profile,
        'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex)
    assert plan.scope is setup.scope and plan.steps[0].tool == 'library.list'
    assert len(setup.calls) == 1 and setup.calls[0][1] == '/chat/completions'
    body = setup.calls[0][2]
    assert body['model'] == 'fixture-model' and body['stream'] is False
    assert 'USER INSTRUCTION JSON:' in body['messages'][0]['content']
    assert setup.service.jobs() == ()


@pytest.mark.parametrize('change', [
    {'scope': {}}, {'shell': 'ignored'}, {'schema': 'other'},
])
def test_response_cannot_supply_scope_or_unknown_fields(change):
    data = {**proposal(), **change}
    with pytest.raises(ValueError):
        parse_local_proposal(json.dumps(data), Scope())


def test_duplicate_keys_nonfinite_and_empty_steps_are_not_approved():
    values = [
        '{"schema":"carr-assistant-plan/1","summary":"ok","steps":[],"steps":[]}',
        '{"schema":"carr-assistant-plan/1","summary":"ok","steps":[{"tool":"library.list","arguments":{"x":NaN}}]}',
    ]
    for value in values:
        with pytest.raises(ValueError):
            parse_local_proposal(value, Scope())
    with pytest.raises(PlanNeedsInput, match='clarification'):
        parse_local_proposal(json.dumps({**proposal(), 'steps': [], 'summary': 'Choose a library'}), Scope())


def test_current_argument_validator_rejects_hidden_authority(setup):
    setup.result['document'] = proposal(arguments={'shell': 'do not execute'})
    with pytest.raises(ValueError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex)
    assert len(setup.calls) == 1 and setup.service.jobs() == ()


def test_unknown_tool_and_out_of_scope_input_rejected(setup, tmp_path):
    setup.result['document'] = proposal('shell.run', {'command': 'anything'})
    with pytest.raises(ValueError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex)
    source = tmp_path / 'unapproved.txt'
    source.write_text('Synthetic source', encoding='utf-8')
    workspace = setup.app.create_workspace('Synthetic scope')
    workspace_id = workspace.id if hasattr(workspace, 'id') else workspace
    setup.result['document'] = proposal('library.import', {'path': str(source)})
    with pytest.raises(PermissionError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', Scope(workspace_id), approval=approve(setup), operation_id=uuid4().hex)
    assert setup.service.jobs() == ()


def test_remote_or_unapproved_service_never_receives_instruction(setup):
    remote = ProviderProfile('remote-fixture', 'Remote fixture', 'openai-compatible',
        'https://example.invalid/v1', 'fixture', ('llm',))
    with pytest.raises(PermissionError):
        propose_local_plan(setup.service, setup.gateway, remote,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex)
    with pytest.raises(ProviderError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'Changed instruction', setup.scope, approval=approve(setup), operation_id=uuid4().hex)
    assert not setup.calls


def test_cancellation_and_repeated_operation_do_not_resend(setup):
    approval, operation = approve(setup), uuid4().hex
    with pytest.raises(ProviderError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approval, operation_id=operation,
            cancelled=lambda: True)
    assert not setup.calls
    propose_local_plan(setup.service, setup.gateway, setup.profile,
        'List my libraries', setup.scope, approval=approval, operation_id=operation)
    with pytest.raises(ProviderError):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approval, operation_id=operation)
    assert len(setup.calls) == 1


@pytest.mark.parametrize('when', ['before', 'during'])
@pytest.mark.parametrize('change', ['removed', 'model'])
def test_saved_profile_changes_discard_proposal_without_submitting(setup, when, change):
    from dataclasses import replace
    from carr_library.providers import ProfileStore
    store = ProfileStore(setup.app.root / 'api-connections.json')
    store.save(setup.profile)
    def changed():
        if change == 'removed':
            store.remove(setup.profile.id)
        else:
            store.save(replace(setup.profile, model='changed-model'))
    if when == 'before':
        changed()
    else:
        original = setup.gateway.transport.post
        def response(*args, **kwargs):
            raw = original(*args, **kwargs)
            changed()
            return raw
        setup.gateway.transport.post = response
    with pytest.raises(PermissionError, match='profile changed or was removed'):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex,
            profile_store=store)
    assert len(setup.calls) == int(when == 'during')
    assert setup.service.jobs() == ()


def test_live_profile_cannot_borrow_another_library_ledger(setup, tmp_path):
    from carr_library.providers import ProfileStore
    store = ProfileStore(setup.app.root / 'api-connections.json')
    store.save(setup.profile)
    other = tmp_path / 'other'
    other.mkdir()
    setup.gateway.ledger = BudgetLedger(other / 'api-operations.sqlite')
    with pytest.raises(PermissionError, match='this library'):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex,
            profile_store=store)
    assert not setup.calls and setup.service.jobs() == ()


def test_replaced_ledger_during_response_cannot_expose_a_proposal(setup, monkeypatch):
    from carr_library.providers import ProfileStore
    store = ProfileStore(setup.app.root / 'api-connections.json')
    store.save(setup.profile)
    original = setup.gateway.perform
    def replaced(*args, **kwargs):
        result = original(*args, **kwargs)
        path = setup.gateway.ledger.path
        path.rename(path.with_name('retained-original-ledger.sqlite'))
        BudgetLedger(path)
        return result
    monkeypatch.setattr(setup.gateway, 'perform', replaced)
    with pytest.raises(PermissionError, match='request ledger changed'):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex,
            profile_store=store)
    assert len(setup.calls) == 1 and setup.service.jobs() == ()


def test_profile_changed_by_validation_cannot_escape_as_a_reviewable_plan(setup, monkeypatch):
    from carr_library.providers import ProfileStore
    store = ProfileStore(setup.app.root / 'api-connections.json')
    store.save(setup.profile)
    original = setup.service.validate
    def changed(plan):
        result = original(plan)
        store.remove(setup.profile.id)
        return result
    monkeypatch.setattr(setup.service, 'validate', changed)
    with pytest.raises(PermissionError, match='profile changed or was removed'):
        propose_local_plan(setup.service, setup.gateway, setup.profile,
            'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex,
            profile_store=store)
    assert len(setup.calls) == 1 and setup.service.jobs() == ()


@pytest.mark.parametrize('alias', ['relative', 'dot-parent'])
def test_saved_local_authorities_accept_only_the_same_canonical_library(setup, tmp_path, monkeypatch, alias):
    from carr_library.providers import ProfileStore
    root = setup.app.root
    if alias == 'relative':
        monkeypatch.chdir(root.parent)
        directory = Path(root.name)
    else:
        child = root / 'existing-child'
        child.mkdir()
        directory = child / '..'
    store = ProfileStore(directory / 'api-connections.json')
    store.save(setup.profile)
    setup.gateway.ledger = BudgetLedger(directory / 'api-operations.sqlite')
    plan = propose_local_plan(setup.service, setup.gateway, setup.profile,
        'List my libraries', setup.scope, approval=approve(setup), operation_id=uuid4().hex,
        profile_store=store)
    assert plan.scope is setup.scope and plan.steps[0].tool == 'library.list'
    assert len(setup.calls) == 1 and setup.service.jobs() == ()
