"""CLI recipes return review data and reuse existing local services only."""
import json
from pathlib import Path

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant.templates import TemplateStore, REQUEST_SCHEMA
from carr_library.assistant.templates_cli import main


def test_cli_save_request_and_preview_do_not_execute(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    definition = tmp_path / 'definition.json'
    definition.write_text(json.dumps({'steps': [{'tool': 'library.create',
        'arguments': {'name': 'Only after explicit Run'}}]}), encoding='utf-8')
    base = ['--data-dir', str(app.root)]
    assert main([*base, 'save', '--name', 'New library macro', '--kind', 'assistant',
                 '--definition', str(definition)]) == 0
    record = json.loads(capsys.readouterr().out)
    identity = record['id']
    assert app.workspaces() == () and AssistantService(app).jobs() == ()
    assert main([*base, 'request', identity]) == 0
    request = json.loads(capsys.readouterr().out)
    assert request['schema'] == REQUEST_SCHEMA and 'scope' not in request
    assert main([*base, 'preview', identity]) == 1
    assert 'permit creating' in capsys.readouterr().err
    assert app.workspaces() == ()
    assert main([*base, 'preview', identity, '--allow-create']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['scope']['allow_create'] is True and plan['steps'][0]['tool'] == 'library.create'
    assert app.workspaces() == () and AssistantService(app).jobs() == ()


def test_cli_update_requires_current_exact_template_identity(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    store = TemplateStore(app.root)
    first = store.save('Current', 'assistant', {'steps': [{'tool': 'library.list', 'arguments': {}}]})
    definition = tmp_path / 'definition.json'
    definition.write_text(json.dumps(first.definition), encoding='utf-8')
    args = ['--data-dir', str(app.root), 'save', '--name', 'Updated', '--kind', 'assistant',
            '--definition', str(definition), '--replace', first.id]
    assert main([*args, '--expected-sha256', '0' * 64]) == 1
    assert store.get(first.id) == first
    assert main([*args, '--expected-sha256', first.fingerprint]) == 0
    assert store.get(first.id).revision == 2
    assert AssistantService(app).jobs() == ()
