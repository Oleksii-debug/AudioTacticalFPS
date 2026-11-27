"""Pure offline planning guards and CLI parsing; no loopback listener is opened."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, ProfileStore, ProviderProfile, Scope
from carr_library.assistant.llm import propose_local_plan
from carr_library.assistant.providers import Gateway


@pytest.fixture
def planner_server(monkeypatch):
    state = {'requests': []}
    def forbidden(*args, **kwargs):
        state['requests'].append((args, kwargs))
        raise AssertionError('An offline planning guard attempted a provider request.')
    monkeypatch.setattr(Gateway, 'request', forbidden)
    return state


@pytest.fixture
def configured(tmp_path, planner_server):
    app = LibraryApp(tmp_path / 'library')
    store = ProfileStore(app.root / 'state' / 'providers')
    profile = ProviderProfile('chosen_local', 'Chosen local model', 'openai-compatible',
        'http://127.0.0.1:9/v1', 'synthetic-protocol-model', local=True, enabled=True)
    store.save(profile)
    return app, store, profile


def cli(root, *arguments):
    # Follow the package already selected by the source/installed test runner.
    # Never prepend this checkout to an installed-wheel acceptance subprocess.
    import bootstrap
    import carr_library
    roots = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                          for module in (carr_library, bootstrap))
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=os.pathsep.join(roots))
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'assistant',
        '--data-dir', str(root), *map(str, arguments)], env=env, cwd=root.parent,
        text=True, encoding='utf-8', capture_output=True, timeout=20)


def test_eligible_listing_has_no_implicit_model_selection(configured, planner_server):
    app, store, profile = configured
    store.save(replace(profile, id='disabled', enabled=False))
    store.save(replace(profile, id='remote', local=False, endpoint='https://example.test/v1'))
    assert AssistantService(app).local_planner_profiles() == (profile,)
    assert not planner_server['requests']



@pytest.mark.parametrize('kind', ['disabled', 'remote', 'unknown'])
def test_noneligible_profile_cannot_open_connection(configured, planner_server, kind):
    app, store, profile = configured
    if kind != 'unknown':
        store.save(replace(profile, enabled=False) if kind == 'disabled' else
                   replace(profile, local=False, endpoint='https://example.test/v1'))
    with pytest.raises((PermissionError, FileNotFoundError)):
        AssistantService(app).propose_local('Create a library.', Scope(allow_create=True),
                                           profile_id='missing' if kind == 'unknown' else profile.id)
    assert not planner_server['requests'] and app.workspaces() == ()



def test_unavailable_selected_workspace_rejected_before_request(configured, planner_server):
    app, _, profile = configured
    with pytest.raises((ValueError, KeyError)):
        AssistantService(app).propose_local('List books.', Scope('absent'), profile_id=profile.id)
    assert not planner_server['requests']



def test_shared_gateway_receives_bounded_response_and_selected_deadline(configured):
    _, _, profile = configured
    calls = []
    class SyntheticGateway:
        def request(self, *args, **kwargs):
            calls.append(kwargs)
            return {'text': json.dumps({'steps': [{'tool': 'library.list'}]})}
    plan = propose_local_plan(SyntheticGateway(), profile, 'List libraries.', Scope(max_seconds=11))
    assert plan.steps[0].tool == 'library.list'
    assert calls[0]['timeout'] == 11 and calls[0]['max_response_bytes'] == 8 * 1024**2



@pytest.mark.parametrize('arguments', [
    ['--instructions', 'missing.txt', '--local-profile', 'chosen', '--run'],
    ['--resume', 'saved', '--local-profile', 'chosen'],
    ['--list-local-profiles', '--run'],
    ['--list-local-profiles', '--instructions', 'missing.txt'],
])
def test_invalid_cli_modes_have_no_application_side_effects(tmp_path, arguments):
    root = tmp_path / 'not-created'
    result = cli(root, *arguments)
    assert result.returncode == 2 and not root.exists()



def test_actual_cli_lists_only_local_profiles_without_contact(configured, planner_server):
    app, store, profile = configured
    store.save(replace(profile, id='disabled', enabled=False))
    listed = cli(app.root, '--list-local-profiles')
    assert listed.returncode == 0, listed.stderr
    assert [row['id'] for row in json.loads(listed.stdout)] == [profile.id]
    assert not planner_server['requests']



@pytest.mark.parametrize('identity', ['', '../outside', 'profile with spaces'])
def test_invalid_profile_identity_cannot_silently_fall_back_to_guided_mode(tmp_path, identity):
    root = tmp_path / 'not-created'
    instruction = tmp_path / 'goal.txt'; instruction.write_text('List libraries.')
    result = cli(root, '--instructions', instruction, '--local-profile', identity)
    assert result.returncode == 1 and not result.stdout and not root.exists()


# Pure JSON boundary checks; these do not execute plans or contact a model.
from carr_library.assistant.models import Plan
from carr_library.assistant.llm import LocalPlanClarification, _parse_local_proposal
from carr_library.assistant.state import JobStore, read_json, atomic_json


SCHEMA = 'carr-assistant-plan/1'


def wire(steps=None, summary='', **extra):
    return json.dumps({'schema':SCHEMA,'steps':[{'tool':'library.list'}] if steps is None else steps,'summary':summary,**extra}, ensure_ascii=True)


@pytest.mark.parametrize('raw', [
    '{"steps":[{"tool":"shell","tool":"library.list"}]}',
    '{"steps":[{"tool":"library.search","arguments":{"query":"one","query":"two"}}]}',
    '{"schema":"bad","schema":"carr-assistant-plan/1","steps":[{"tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list","arguments":{},"arguments":{}}]}',
    '{"steps":[{"tool":"library.list","arguments":{"nested":{"x":1,"x":2}}}]}',
    '{"steps":[{"tool":"library.list"}],"st\\u0065ps":[{"tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list"}],"summary":"a","summary":"a"}',
    '{"steps":[{"tool":"library.list","arguments":{"nested":[{"x":1,"x":2}]}}]}',
    '{"steps":[{"tool":"library.list","arguments":{"\\u0430":1,"а":2}}]}',
    '{"scope":{},"scope":null,"steps":[{"tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list"}],"schema":"carr-assistant-plan/1","schema":"carr-assistant-plan/1"}',
    '{"schema":"carr-assistant-plan/1","steps":[],"summary":"a","summary":"b"}',
])
def test_duplicate_rejected(raw):
    with pytest.raises(ValueError,match='Duplicate JSON keys'):
        _parse_local_proposal(raw,Scope())


@pytest.mark.parametrize('message', ['Which book?', 'Яку книгу імпортувати?', 'Ktorú knihu?', 'أي كتاب؟', '哪本书？', 'Quelle édition ?', 'Which source?\nWhich edition?', 'x'*2048])
def test_clarification_preserves_exact_message(message):
    with pytest.raises(LocalPlanClarification) as exc:
        _parse_local_proposal(wire([],message),Scope())
    assert message in str(exc.value) and 'No plan was submitted' in str(exc.value)


@pytest.mark.parametrize('summary', ['', '   ', 'x'*2049, '\x1b[31m', '\x00', '\r', '\t', '\u202efoo', '\u200bfoo', '\ud800', None, [], {}, 2])
def test_invalid_clarification_is_not_a_question(summary):
    with pytest.raises((ValueError,UnicodeError)) as exc:
        _parse_local_proposal(wire([],summary),Scope())
    assert not isinstance(exc.value,LocalPlanClarification)


@pytest.mark.parametrize('raw', [
    '', '[]', 'null', 'true', '{', '{"steps":42}', '{"steps":[null]}',
    '{"steps":[{"tool":"shell"}]}', '{"steps":[{"tool":"delete"}]}',
    '{"steps":[{"tool":"library.list","arguments":[]}]}',
    '{"steps":[{"tool":"library.list","arguments":{"x":NaN}}]}',
    '{"steps":[{"tool":"library.list","arguments":{"x":Infinity}}]}',
    '{"steps":[{"tool":"library.list","arguments":{"x":-Infinity}}]}',
    '{"steps":[{"tool":"library.list"}],"scope":{"allow_create":true}}',
    '{"schema":"wrong","steps":[{"tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list","extra":true}]}',
    '{"steps":[{"arguments":{}}]}',
    '{"steps":[{"tool":"library.create","arguments":{"name":"new"}}]}',
    '{"schema":"carr-assistant-plan/1","steps":[],"summary":"Clarify","extra":0}',
    '{"schema":"carr-assistant-plan/1","steps":[],"summary":"Clarify","scope":null}',
])
def test_invalid_proposal_fails_closed(raw):
    with pytest.raises((ValueError,PermissionError,TypeError)) as exc:
        _parse_local_proposal(raw,Scope())
    assert not isinstance(exc.value,LocalPlanClarification)


def test_step_budget_preserved():
    with pytest.raises(ValueError,match='budget'):
        _parse_local_proposal(wire([{'tool':'library.list'}]*2),Scope(max_steps=1))


def test_scope_is_original_not_model_owned():
    scope=Scope('selected', max_steps=2)
    plan=_parse_local_proposal(wire([{'tool':'library.search','arguments':{'query':'джерело'}}]),scope)
    assert plan.scope is scope and plan.steps[0].arguments=={'query':'джерело'}
    assert Plan.from_json(plan.to_json()).digest==plan.digest


def test_authorized_creation_is_typed_only():
    scope=Scope(allow_create=True)
    plan=_parse_local_proposal(wire([{'tool':'library.create','arguments':{'name':'History'}}]),scope)
    assert plan.scope is scope and plan.steps[0].tool=='library.create'


def test_json_size_bound():
    with pytest.raises(ValueError,match='plan JSON'):
        _parse_local_proposal(' '* (8*1024*1024+1),Scope())


@pytest.mark.parametrize('raw', [
    '{"steps":[{"tool":"shell","tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list","arguments":{"a":1,"a":2}}]}',
    '{"steps":[{"tool":"library.list"}],"st\\u0065ps":[{"tool":"library.list"}]}',
    '{"steps":[{"tool":"library.list"}],"summary":"one","summary":"two"}',
])
def test_direct_plan_rejects_duplicate_keys(raw):
    with pytest.raises(ValueError, match='Duplicate JSON keys'):
        Plan.from_json(raw, scope=Scope())


@pytest.mark.parametrize('raw', [
    b'{"command":"cancel","command":"run"}',
    b'{"plan":{"steps":[{"tool":"shell","tool":"library.list"}]}}',
    b'{"model":"reviewed","m\\u006fdel":"changed"}',
    b'{"local":false,"local":true}',
    b'{"meta":NaN}', b'{"meta":Infinity}', b'{"meta":-Infinity}',
    b'{"meta":1e999}', b'{"meta":-1e999}',
])
def test_saved_state_rejects_ambiguous_or_nonfinite_json(tmp_path, raw):
    path = tmp_path / 'state.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='Duplicate JSON keys|Non-finite'):
        read_json(path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999'])
def test_nonfinite_values_rejected_before_local_plan_validation(value):
    raw = '{"steps":[{"tool":"library.list"}],"summary":' + value + '}'
    with pytest.raises(ValueError, match='Non-finite'):
        _parse_local_proposal(raw, Scope())


def test_control_cancel_cannot_silently_become_run(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    identity = '1' * 32
    path = store.path(identity).with_suffix('.control.json')
    raw = b'{"command":"cancel","command":"run"}'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='Duplicate JSON keys'):
        store.control(identity)
    assert path.read_bytes() == raw
    assert store.control(identity, 'cancel') == 'cancel'


def test_job_duplicate_status_cannot_become_queued(tmp_path):
    store = JobStore(tmp_path / 'jobs')
    identity = '2' * 32
    raw = ('{"schema":"carr-assistant-job/1","id":"'+identity+'",'
           '"status":"cancelled","status":"queued"}').encode()
    store.path(identity).write_bytes(raw)
    with pytest.raises(ValueError, match='Duplicate JSON keys'):
        store.load(identity)
    assert store.path(identity).read_bytes() == raw


def test_finite_unicode_state_roundtrip(tmp_path):
    path = tmp_path / 'checkpoint.json'
    original = {'name': 'Дослідження — výskum', 'seconds': 1.25, 'count': 5,
                'nested': [{'status': 'paused'}, {'result': None}]}
    atomic_json(path, original)
    assert read_json(path) == original


def test_finite_plan_roundtrip():
    scope = Scope('workspace')
    value = {'steps': [{'tool':'library.search','arguments':{'query':'Книга','threshold':0.5}}]}
    plan = _parse_local_proposal(json.dumps(value), scope)
    assert Plan.from_json(plan.to_json()).digest == plan.digest
