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
