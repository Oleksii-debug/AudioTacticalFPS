"""Real existing assistant CLI and named-settings commands, with current authority."""
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.cli import main
from carr_library.assistant import template_cli


@pytest.fixture
def cli_environment(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Current workspace')
    app.add_text(workspace, 'Source', 'Shared current source.')
    service = AssistantService(app)
    plan = service.preview('Search "Shared"', Scope(workspace))
    draft = tmp_path / 'draft.json'
    draft.write_text(json.dumps(plan.to_dict(), ensure_ascii=False), encoding='utf-8')
    def invoke(*arguments, expected=0):
        code = main(['--data-dir', str(app.root), *map(str, arguments)])
        out, err = capsys.readouterr()
        assert code == expected, (out, err)
        return json.loads(out) if expected == 0 else err
    return app, workspace, service, plan, draft, invoke


def test_cli_named_lifecycle_load_rebind_and_separate_run(cli_environment, tmp_path):
    app, workspace, service, plan, draft, invoke = cli_environment
    saved = invoke('--templates', 'save', 'Пошук', '--draft', draft)
    identity = saved['id']
    assert 'scope' not in saved['draft'] and saved['revision'] == 1
    listed = invoke('--templates', 'list')
    assert listed == [{'id': identity, 'name': 'Пошук', 'revision': 1, 'steps': 1}]
    loaded = invoke('--templates', 'load', identity)
    assert set(loaded) == {'schema', 'steps', 'summary'} and service.jobs() == ()
    rebound = invoke('--templates', 'rebind', identity, '--workspace', workspace)
    assert rebound['scope']['workspace_id'] == workspace and service.jobs() == ()
    second = invoke('--templates', 'update', identity, '--draft', draft, '--name', 'Оновлений пошук')
    assert second['revision'] == 2 and second['id'] == identity
    renamed = invoke('--templates', 'rename', identity, 'Reviewed current search')
    assert renamed['revision'] == 3 and renamed['draft'] == saved['draft']
    instructions = tmp_path / 'loaded.json'
    instructions.write_text(json.dumps(loaded), encoding='utf-8')
    result = invoke('--instructions', instructions, '--workspace', workspace, '--run')
    assert result['status'] == 'complete' and len(service.jobs()) == 1
    assert all(row['workspace'] == workspace for row in result['results'][0]['results'])
    removed = invoke('--templates', 'remove', identity)
    assert removed['removed_template'] == identity and invoke('--templates', 'list') == []
    assert len(service.jobs()) == 1


def test_cli_import_export_and_explicit_exact_replacement(cli_environment, tmp_path):
    _, _, service, _, draft, invoke = cli_environment
    saved = invoke('--templates', 'save', 'Original', '--draft', draft)
    target = tmp_path / 'export.json'
    result = invoke('--templates', 'export', saved['id'], target)
    assert result['status'] == 'complete'
    original = target.read_bytes()
    assert json.loads(original) == saved
    error = invoke('--templates', 'export', saved['id'], target, expected=1)
    assert '--replace-existing' in error and target.read_bytes() == original
    renamed = invoke('--templates', 'rename', saved['id'], 'Current revision')
    invoke('--templates', 'export', saved['id'], target, '--replace-existing')
    assert json.loads(target.read_bytes()) == renamed
    imported = invoke('--templates', 'import', target, '--name', 'Imported new identity')
    assert imported['id'] != saved['id'] and imported['revision'] == 1
    assert imported['draft'] == saved['draft'] and service.jobs() == ()


def test_cli_copies_only_saved_job_steps_without_restoring_approval(cli_environment):
    app, _, service, plan, _, invoke = cli_environment
    identity = service.submit(plan)
    record = service.store.load(identity)
    record['data_dir'] = str(app.root.parent / 'old-data-location')
    service.store.save(record)
    before = service.store.path(identity).read_bytes()
    saved = invoke('--templates', 'save', 'Restored settings', '--from-job', identity)
    assert 'scope' not in saved['draft'] and service.store.path(identity).read_bytes() == before
    assert service.get(identity).status == 'attention'
    with pytest.raises(PermissionError):
        service.resume(identity)
    assert len(service.jobs()) == 1


def test_cli_rebind_requires_current_input_permission_and_never_imports(cli_environment, tmp_path):
    _, workspace, service, _, _, invoke = cli_environment
    source = tmp_path / 'explicit-input.txt'
    source.write_text('Selected source bytes.', encoding='utf-8')
    plan = service.preview('Import "' + str(source) + '"', Scope(workspace, (tmp_path,)))
    saved = service.workflow_templates().save('Literal input request', plan)
    error = invoke('--templates', 'rebind', saved.id, '--workspace', workspace, expected=1)
    assert 'outside' in error and service.jobs() == ()
    reviewed = invoke('--templates', 'rebind', saved.id, '--workspace', workspace,
                      '--input-root', tmp_path)
    assert reviewed['scope']['input_roots'] == [str(tmp_path)]
    assert service.jobs() == () and len(service.app.browse(workspace)) == 1


@pytest.mark.parametrize('placement', ['before', 'after'])
@pytest.mark.parametrize('command', ['list', 'load', 'rebind'])
def test_cli_template_commands_reject_execution_flag(cli_environment, capsys, placement, command):
    app, _, service, _, draft, invoke = cli_environment
    saved = invoke('--templates', 'save', 'Inert', '--draft', draft)
    action = [command] + ([] if command == 'list' else [saved['id']])
    arguments = ['--data-dir', str(app.root)]
    if placement == 'before':
        arguments += ['--run', '--templates', *action]
    else:
        arguments += ['--templates', *action, '--run']
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
    capsys.readouterr()
    assert service.jobs() == ()


@pytest.mark.parametrize('scope', [
    ['--workspace', 'old-scope'],
    ['--allow-full-backup'],
    ['--max-bytes', '123'],
])
def test_cli_refuses_permissions_outside_explicit_rebind(cli_environment, capsys, scope):
    app, _, service, _, _, _ = cli_environment
    with pytest.raises(SystemExit) as error:
        main(['--data-dir', str(app.root), *scope, '--templates', 'list'])
    assert error.value.code == 2
    assert 'after --templates rebind' in capsys.readouterr().err
    assert service.jobs() == ()


def test_cli_template_reads_never_contact_model_or_submit_job(cli_environment, monkeypatch):
    from carr_library.providers import ProviderGateway
    _, _, service, _, draft, invoke = cli_environment
    saved = invoke('--templates', 'save', 'Passive', '--draft', draft)
    def forbidden(*args, **kwargs):
        pytest.fail('A named settings read contacted a provider or submitted a plan.')
    monkeypatch.setattr(ProviderGateway, 'perform', forbidden)
    monkeypatch.setattr(AssistantService, 'submit', forbidden)
    assert invoke('--templates', 'list')[0]['id'] == saved['id']
    assert invoke('--templates', 'load', saved['id'])['steps'] == saved['draft']['steps']
    assert service.jobs() == ()


def test_cli_default_export_cannot_clobber_a_new_concurrent_destination(
        cli_environment, tmp_path, monkeypatch):
    _, _, service, plan, _, _ = cli_environment
    saved = service.workflow_templates().save('Selected', plan)
    target = tmp_path / 'race.json'
    capture = template_cli.capture_export_destination
    def raced(*args, **kwargs):
        approval = capture(*args, **kwargs)
        target.write_text('Independent creation after selection.', encoding='utf-8')
        return approval
    monkeypatch.setattr(template_cli, 'capture_export_destination', raced)
    with pytest.raises((ValueError, FileExistsError)):
        template_cli.run_templates(['export', saved.id, str(target)], service)
    assert target.read_text(encoding='utf-8') == 'Independent creation after selection.'


def test_cli_cancelled_settings_operation_never_creates_a_record(cli_environment):
    _, _, service, _, draft, _ = cli_environment
    with pytest.raises(InterruptedError):
        template_cli.run_templates(['save', 'Cancelled', '--draft', str(draft)],
                                  service, cancelled=lambda: True)
    assert service.workflow_templates().list() == () and service.jobs() == ()
