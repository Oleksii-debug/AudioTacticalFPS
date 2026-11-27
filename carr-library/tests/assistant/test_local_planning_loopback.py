"""Actual service/CLI protocol integration with synthetic loopback responses.

These tests exercise the real Gateway. They do not qualify an installed LLM.
"""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, ProfileStore, ProviderProfile, Scope


pytestmark = pytest.mark.requires_loopback


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


@pytest.fixture
def planner_server():
    state = {'requests': [], 'status': 200, 'on_request': lambda: None,
             'plan': {'schema': 'carr-assistant-plan/1', 'summary': 'Synthetic local proposal',
                      'steps': [{'tool': 'library.create', 'arguments': {'name': 'Reviewed library'}}]}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state['requests'].append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            state['on_request']()
            body = json.dumps({'choices': [{'message': {'content': json.dumps(state['plan'])}}]}).encode()
            self.send_response(state['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
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
    store = ProfileStore(app.root / 'state' / 'providers')
    profile = ProviderProfile('chosen_local', 'Chosen local model', 'openai-compatible',
        planner_server['endpoint'], 'synthetic-protocol-model', local=True, enabled=True)
    store.save(profile)
    return app, store, profile


def test_service_proposal_is_validated_scoped_and_never_submitted(configured, planner_server, tmp_path):
    app, _, profile = configured
    inputs = tmp_path / 'selected'; inputs.mkdir()
    (inputs / 'private.txt').write_text('Never automatically send folder contents.')
    scope = Scope(input_roots=(inputs,), allow_create=True)
    service = AssistantService(app)
    plan = service.propose_local('Create one named library.', scope, profile_id=profile.id)
    assert plan.scope == scope and plan.steps[0].arguments == {'name': 'Reviewed library'}
    assert service.jobs() == () and app.workspaces() == ()
    route, payload = planner_server['requests'][0]
    assert route == '/v1/chat/completions' and payload['model'] == profile.model
    assert payload['messages'][1] == {'role': 'user', 'content': 'Create one named library.'}
    assert 'Never automatically' not in json.dumps(payload) and str(inputs) not in json.dumps(payload)
    result = service.run(service.submit(plan))
    assert result.status == 'complete' and [row.name for row in app.workspaces()] == ['Reviewed library']
    assert len(planner_server['requests']) == 1






@pytest.mark.parametrize('malicious', [
    {'steps': [{'tool': 'shell', 'arguments': {'command': 'execute'}}]},
    {'steps': [{'tool': 'library.list', 'arguments': {'delete': True}}]},
    {'scope': Scope(allow_create=True).to_dict(), 'steps': [{'tool': 'library.list'}]},
    {'steps': [{'tool': 'library.create', 'arguments': {'name': 'Unapproved'}}]},
    {'steps': []},
])
def test_model_output_cannot_expand_exact_permissions(configured, planner_server, malicious):
    app, _, profile = configured
    planner_server['plan'] = malicious
    service = AssistantService(app)
    with pytest.raises((ValueError, PermissionError)):
        service.propose_local('List libraries.', Scope(), profile_id=profile.id)
    assert service.jobs() == () and app.workspaces() == ()


def test_tool_path_validation_runs_before_proposal_is_exposed(configured, planner_server, tmp_path):
    app, _, profile = configured
    workspace = app.create_workspace('Selected')
    inputs = tmp_path / 'inputs'; inputs.mkdir()
    outside = tmp_path / 'unselected.txt'; outside.write_text('Private original')
    planner_server['plan'] = {'steps': [{'tool': 'library.import', 'arguments': {'path': str(outside)}}]}
    service = AssistantService(app)
    with pytest.raises(PermissionError, match='outside'):
        service.propose_local('Import a document.', Scope(workspace, (inputs,)), profile_id=profile.id)
    assert not app.browse(workspace) and service.jobs() == ()


def test_profile_change_during_response_discards_proposal(configured, planner_server):
    app, store, profile = configured
    planner_server['on_request'] = lambda: store.save(replace(profile, model='edited-model'))
    with pytest.raises(PermissionError, match='profile changed'):
        AssistantService(app).propose_local('Create a library.', Scope(allow_create=True), profile_id=profile.id)
    assert app.workspaces() == ()


@pytest.mark.parametrize('when', ['before', 'during'])
def test_cancellation_never_creates_an_approved_job(configured, planner_server, when):
    app, _, profile = configured
    cancelled = Event()
    if when == 'before':
        cancelled.set()
    else:
        planner_server['on_request'] = cancelled.set
    service = AssistantService(app)
    with pytest.raises(InterruptedError):
        service.propose_local('Create a library.', Scope(allow_create=True),
                              profile_id=profile.id, cancelled=cancelled.is_set)
    assert len(planner_server['requests']) == int(when == 'during')
    assert service.jobs() == () and app.workspaces() == ()








def test_actual_cli_proposal_then_separate_reviewed_json_run(configured, planner_server, tmp_path):
    app, _, profile = configured
    instruction = tmp_path / 'goal.txt'; instruction.write_text('Create one named library.')
    proposed = cli(app.root, '--instructions', instruction, '--allow-create', '--local-profile', profile.id)
    assert proposed.returncode == 0, proposed.stderr
    plan = json.loads(proposed.stdout)
    assert plan['scope']['allow_create'] is True and 'proposal only' in proposed.stderr
    assert app.workspaces() == () and AssistantService(app).jobs() == ()
    reviewed = tmp_path / 'reviewed-plan.json'; reviewed.write_text(proposed.stdout, encoding='utf-8')
    wrong_scope = cli(app.root, '--instructions', reviewed, '--run')
    assert wrong_scope.returncode == 1 and 'permissions' in wrong_scope.stderr
    assert app.workspaces() == ()
    executed = cli(app.root, '--instructions', reviewed, '--allow-create', '--run')
    assert executed.returncode == 0, executed.stderr
    report = json.loads(executed.stdout)
    assert report['status'] == 'complete' and [row.name for row in app.workspaces()] == ['Reviewed library']
    assert len(planner_server['requests']) == 1
    reopened = cli(app.root, '--resume', report['id'])
    assert json.loads(reopened.stdout)['status'] == 'complete'






def test_actual_cli_provider_failure_returns_no_executable_plan(configured, planner_server, tmp_path):
    app, _, profile = configured
    planner_server['status'] = 503
    instruction = tmp_path / 'goal.txt'; instruction.write_text('List libraries.')
    result = cli(app.root, '--instructions', instruction, '--local-profile', profile.id)
    assert result.returncode == 1 and not result.stdout and 'HTTP 503' in result.stderr
    assert AssistantService(app).jobs() == () and app.workspaces() == ()
