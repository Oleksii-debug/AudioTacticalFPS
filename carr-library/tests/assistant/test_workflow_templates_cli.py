"""Actual CLI capture, one catalogue CAS, passive requests and new scope preview."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant.templates import TemplateStore
from carr_library.assistant.templates_cli import main
from carr_library.durable_state import encode
from carr_library.studio.project import parse_script


def call(capsys, root, *args):
    code = main(['--data-dir', str(root), *map(str, args)])
    output = capsys.readouterr()
    return code, json.loads(output.out) if output.out.strip().startswith('{') else output.out, output.err


def test_cli_typed_values_items_and_fresh_preview_leave_job_store_empty(tmp_path, capsys):
    root = tmp_path / 'library'
    app = LibraryApp(root); workspace = app.create_workspace('CLI recipe scope')
    steps = tmp_path / 'steps.json'
    steps.write_bytes(encode([{'tool': 'library.search', 'arguments': {'query': {'$parameter': 'query'}}}]))
    parameters = tmp_path / 'parameters.json'
    parameters.write_bytes(encode({'query': {'kind': 'text', 'label': 'Query', 'example': 'railway'}}))
    code, saved, error = call(capsys, root, 'save', '--name', 'CLI reusable search', '--steps', steps,
                              '--parameters', parameters, '--kind', 'batch-preset')
    assert code == 0 and not error and saved['executed'] is False
    identity = saved['template']['id']
    assert saved['command_id'] == 'assistant.template.' + identity
    values = tmp_path / 'values.json'; values.write_bytes(encode({'query': 'default'}))
    items = tmp_path / 'items.json'; items.write_bytes(encode([{'query': 'one'}, {'query': 'two'}]))
    code, request, error = call(capsys, root, 'request', identity, '--values', values, '--items', items)
    assert code == 0 and not error
    assert 'scope' not in request and request['items'] == [{'query': 'one'}, {'query': 'two'}]
    code, preview, error = call(capsys, root, 'preview', identity, '--values', values,
                               '--items', items, '--workspace', workspace)
    assert code == 0, error
    assert [step['arguments']['query'] for step in preview['steps']] == ['one', 'two']
    assert preview['scope']['workspace_id'] == workspace
    assert preview['scope']['input_roots'] == [] and preview['scope']['output_root'] is None
    assert AssistantService(app).jobs() == ()


def test_cli_saved_plan_capture_does_not_open_or_copy_old_scope(tmp_path, capsys):
    root = tmp_path / 'library'
    original = tmp_path / 'old-plan.json'
    original.write_bytes(encode({'schema': 'carr-assistant-plan/1',
        'scope': {'input_roots': ['/unavailable/old/permission'], 'allow_full_backup': True},
        'steps': [{'tool': 'library.list', 'arguments': {}}], 'summary': 'Retained recipe'}))
    code, saved, error = call(capsys, root, 'from-plan', '--name', 'Scope free', '--plan', original)
    assert code == 0, error
    assert 'scope' not in saved['template']
    assert '/unavailable' not in TemplateStore(root).path.read_text('utf-8')
    assert not (root / 'state/workflows').exists()


@pytest.mark.parametrize('tool', ['audio.produce', 'audio.render'])
def test_cli_project_capture_never_starts_audio_and_drops_saved_lineage(tmp_path, capsys, monkeypatch, tool):
    root = tmp_path / 'library'
    project = parse_script('Exact selected template text.', project_id='old-performance')
    value = project.to_dict(); value['revision'] = 8
    value['scenes'][0]['lines'][0]['source_ref'] = 'old-source:physical-3'
    path = tmp_path / 'project.json'; path.write_bytes(encode(value))
    monkeypatch.setattr('carr_library.studio.render.ProductionService',
        lambda *a, **k: pytest.fail('Project capture must not start a production owner.'))
    code, saved, error = call(capsys, root, 'from-project', '--name', 'New project recipe',
                              '--project', path, '--tool', tool)
    assert code == 0, error
    script = saved['template']['steps'][0]['arguments']['script']
    assert script['id'] == 'template' and script['revision'] == 1
    assert script['scenes'][0]['lines'][0]['source_ref'] == ''
    assert json.loads(path.read_bytes()) == value


def test_cli_replace_and_delete_require_exact_catalogue_review(tmp_path, capsys):
    root = tmp_path / 'library'
    steps = tmp_path / 'steps.json'
    steps.write_bytes(encode([{'tool': 'library.list', 'arguments': {}}]))
    code, saved, error = call(capsys, root, 'save', '--name', 'One', '--steps', steps)
    assert code == 0
    before = TemplateStore(root).path.read_bytes()
    identity = saved['template']['id']
    code, _, error = call(capsys, root, 'save', '--name', 'One', '--steps', steps,
                         '--replace', identity, '--expected-catalog-sha256', '0'*64)
    assert code == 1 and 'changed' in error
    code, _, error = call(capsys, root, 'delete', identity, '--expected-catalog-sha256', '0'*64)
    assert code == 1 and 'changed' in error
    assert TemplateStore(root).path.read_bytes() == before
    code, deleted, error = call(capsys, root, 'delete', identity,
                                '--expected-catalog-sha256', saved['catalog_sha256'])
    assert code == 0 and deleted['executed'] is False
    assert TemplateStore(root).list()[0] == ()


def test_cli_has_no_execute_command(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(['--data-dir', str(tmp_path), 'run'])
    assert error.value.code == 2
