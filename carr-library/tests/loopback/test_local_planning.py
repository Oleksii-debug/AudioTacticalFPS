"""Actual local HTTP/gateway/ledger/CLI integration, with synthetic proposals.

The bound server is a test fixture, never an installed-model quality claim.
No request leaves loopback and no executable model is started.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.llm import local_plan_request, propose_local_plan
from carr_library.providers import BudgetLedger, JobApproval, ProfileStore, ProviderGateway, ProviderProfile


def cli(root, *arguments):
    # Preserve source versus installed-wheel origin selected by this test runner.
    import bootstrap
    import carr_library
    roots = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent)
                          for module in (carr_library, bootstrap))
    env = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=os.pathsep.join(roots))
    policy_file = Path(__file__).resolve().parents[2] / 'scripts/ci_check.py'
    # The CLI is a fresh interpreter: explicitly install the same numeric-only
    # test guard before importing the actual application. This is a Python
    # protocol fixture, not a claim about native executable network isolation.
    bootstrap = ("import sys,runpy; policy=sys.argv.pop(1); "
                 "namespace=runpy.run_path(policy); sys.argv.insert(1,'numeric-loopback'); "
                 "exec(namespace['NETWORK_POLICY']); "
                 "sys.argv[0]='carr_library'; runpy.run_module('carr_library',run_name='__main__')")
    return subprocess.run([sys.executable, '-B', '-c', bootstrap, str(policy_file), 'assistant',
        '--data-dir', str(root), *map(str, arguments)], env=env, cwd=root.parent,
        text=True, encoding='utf-8', capture_output=True, timeout=20)


@pytest.fixture
def planner_server():
    state = {'requests': [], 'status': 200, 'on_request': lambda: None,
             'plan': {'schema': 'carr-assistant-plan/1', 'summary': 'Synthetic local proposal',
                      'steps': [{'tool': 'library.create', 'arguments': {'name': 'Reviewed library'}}]}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers['Content-Length'])
            assert 0 < length <= 1024 * 1024
            state['requests'].append((self.path, json.loads(self.rfile.read(length))))
            state['on_request']()
            body = json.dumps({'choices': [{'message': {'content': json.dumps(state['plan'])}}]}).encode('utf-8')
            self.send_response(state['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A cancelled request may close its connection before reading.
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['endpoint'] = f'http://127.0.0.1:{server.server_port}/v1'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def configured(tmp_path, planner_server):
    app = LibraryApp(tmp_path / 'library')
    store = ProfileStore(app.root / 'api-connections.json')
    profile = ProviderProfile('chosen-local', 'Chosen local model', 'openai-compatible',
        planner_server['endpoint'], 'synthetic-protocol-model', ('llm',), local=True)
    store.save(profile)
    return app, store, profile


def propose(configured, instruction, scope, *, cancelled=lambda: False):
    app, store, profile = configured
    service = AssistantService(app)
    request = local_plan_request(instruction)
    approval = JobApproval(uuid4().hex, profile.fingerprint,
        (request.fingerprint(profile),), allow_loopback=True)
    gateway = ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'))
    return propose_local_plan(service, gateway, profile, instruction, scope, approval=approval,
        operation_id=uuid4().hex, cancelled=cancelled, profile_store=store)


def test_real_gateway_sends_only_the_selected_goal_and_never_submits_it(configured, planner_server, tmp_path):
    app, _, profile = configured
    selected = tmp_path / 'selected'
    selected.mkdir()
    (selected / 'private.txt').write_text('Never transmit this original source automatically.', encoding='utf-8')
    scope = Scope(input_roots=(selected,), allow_create=True)
    plan = propose(configured, 'Create one named library.', scope)
    assert plan.scope is scope and plan.steps[0].arguments == {'name': 'Reviewed library'}
    assert AssistantService(app).jobs() == () and app.workspaces() == ()
    route, payload = planner_server['requests'][0]
    assert route == '/v1/chat/completions' and payload['model'] == profile.model
    assert payload['stream'] is False
    assert payload['messages'][0]['content'].endswith('{"instruction": "Create one named library."}')
    assert 'Never transmit' not in json.dumps(payload) and str(selected) not in json.dumps(payload)
    service = AssistantService(app)
    result = service.run(service.submit(plan))
    assert result.status == 'complete'
    assert [row.name for row in app.workspaces()] == ['Reviewed library']
    assert len(planner_server['requests']) == 1


@pytest.mark.parametrize('change', [
    {'steps': [{'tool': 'shell.run', 'arguments': {'command': 'never execute'}}]},
    {'steps': [{'tool': 'library.list', 'arguments': {'delete': True}}]},
    {'scope': {'allow_create': True}},
    {'steps': [{'tool': 'library.create', 'arguments': {'name': 'Unapproved'}}]},
    {'steps': []},
])
def test_real_model_response_cannot_expand_scope(configured, planner_server, change):
    app, _, _ = configured
    planner_server['plan'] = {**planner_server['plan'], **change}
    with pytest.raises((ValueError, PermissionError)):
        propose(configured, 'List libraries.', Scope())
    assert AssistantService(app).jobs() == () and app.workspaces() == ()


def test_saved_profile_change_during_real_response_discards_proposal(configured, planner_server):
    app, store, profile = configured
    planner_server['on_request'] = lambda: store.save(replace(profile, model='edited-model'))
    with pytest.raises(PermissionError, match='profile changed'):
        propose(configured, 'Create a library.', Scope(allow_create=True))
    assert AssistantService(app).jobs() == () and app.workspaces() == ()


@pytest.mark.parametrize('when', ['before', 'during'])
def test_real_cancellation_does_not_create_an_approved_job(configured, planner_server, when):
    app, _, _ = configured
    cancelled = Event()
    if when == 'before':
        cancelled.set()
    else:
        planner_server['on_request'] = cancelled.set
    with pytest.raises((InterruptedError, RuntimeError)):
        propose(configured, 'Create a library.', Scope(allow_create=True), cancelled=cancelled.is_set)
    assert len(planner_server['requests']) == int(when == 'during')
    assert AssistantService(app).jobs() == () and app.workspaces() == ()


def test_real_cli_proposal_then_separate_explicit_review_and_run(configured, planner_server, tmp_path):
    app, _, profile = configured
    instructions = tmp_path / 'goal.txt'
    instructions.write_text('Create one named library.', encoding='utf-8')
    proposed = cli(app.root, '--instructions', instructions, '--allow-create', '--local-profile', profile.id)
    assert proposed.returncode == 0, proposed.stderr
    plan = json.loads(proposed.stdout)
    assert plan['scope']['allow_create'] is True and 'proposal only' in proposed.stderr
    assert app.workspaces() == () and AssistantService(app).jobs() == ()
    reviewed = tmp_path / 'reviewed-plan.json'
    reviewed.write_text(proposed.stdout, encoding='utf-8')
    wrong_scope = cli(app.root, '--instructions', reviewed, '--run')
    assert wrong_scope.returncode == 1 and 'permissions' in wrong_scope.stderr
    assert app.workspaces() == ()
    executed = cli(app.root, '--instructions', reviewed, '--allow-create', '--run')
    assert executed.returncode == 0, executed.stderr
    report = json.loads(executed.stdout)
    assert report['status'] == 'complete'
    assert [row.name for row in app.workspaces()] == ['Reviewed library']
    assert len(planner_server['requests']) == 1
    reopened = cli(app.root, '--resume', report['id'])
    assert reopened.returncode == 0 and json.loads(reopened.stdout)['status'] == 'complete'


def test_cli_cannot_request_and_execute_model_plan_in_one_invocation(configured, planner_server, tmp_path):
    app, _, profile = configured
    instructions = tmp_path / 'goal.txt'
    instructions.write_text('Create a library.', encoding='utf-8')
    result = cli(app.root, '--instructions', instructions, '--local-profile', profile.id, '--run')
    assert result.returncode == 2 and not result.stdout and 'cannot combine' in result.stderr
    assert planner_server['requests'] == [] and app.workspaces() == ()


def test_cli_provider_failure_returns_no_executable_plan(configured, planner_server, tmp_path):
    app, _, profile = configured
    planner_server['status'] = 503
    instructions = tmp_path / 'goal.txt'
    instructions.write_text('List libraries.', encoding='utf-8')
    result = cli(app.root, '--instructions', instructions, '--local-profile', profile.id)
    assert result.returncode == 1 and not result.stdout and 'did not complete' in result.stderr
    assert AssistantService(app).jobs() == () and app.workspaces() == ()


def test_cli_profile_listing_contacts_no_service(configured, planner_server):
    app, _, profile = configured
    result = cli(app.root, '--list-local-profiles')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {'id': profile.id, 'name': profile.name, 'endpoint': profile.endpoint, 'model': profile.model}]
    assert not planner_server['requests']


def test_cli_expired_explicit_execution_is_not_reported_as_success(configured, planner_server, tmp_path):
    app, _, _ = configured
    instructions = tmp_path / 'guided.txt'
    instructions.write_text('Create library "Must remain uncreated"', encoding='utf-8')
    result = cli(app.root, '--instructions', instructions, '--allow-create',
                 '--max-seconds', '0.000000000001', '--run')
    assert result.returncode == 130, result.stderr
    report = json.loads(result.stdout)
    assert report['status'] == 'paused' and report['completed'] == 0
    assert not app.workspaces() and not planner_server['requests']
    reviewed = cli(app.root, '--resume', report['id'])
    assert reviewed.returncode == 0 and json.loads(reviewed.stdout)['status'] == 'paused'
    forbidden_override = cli(app.root, '--resume', report['id'], '--max-seconds', '3600', '--run')
    assert forbidden_override.returncode == 2 and 'original approved scope' in forbidden_override.stderr
