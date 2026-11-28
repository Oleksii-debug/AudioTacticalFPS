"""Current CLI/template paths never infer a local ASR profile from saved plans."""
import json

import pytest

from carr_library.assistant import AssistantService
from carr_library.assistant import cli
from carr_library.assistant.transcription_tools import SelectedTranscription
from carr_library.runtime_settings import EngineProfileStore
from carr_library import transcription_service as domain
from tests.assistant.transcription_fixture import make_environment, protocol_interpreter


@pytest.fixture
def environment(tmp_path):
    with protocol_interpreter():
        yield make_environment(tmp_path / 'selected')


def instructions(e):
    path = e.root / 'instructions.json'
    plan = e.plan().to_dict()
    plan.pop('scope')
    path.write_text(json.dumps(plan), encoding='utf-8')
    return path


def arguments(e):
    return ['--data-dir', str(e.app.root), '--instructions', str(instructions(e)),
            '--input-root', str(e.source.parent), '--output-root', str(e.outputs)]


def forbidden(*args, **kwargs):
    raise AssertionError('A passive settings action tried to activate or execute ASR.')


def test_cli_profile_listing_is_passive_and_has_no_automatic_selection(environment, capsys, monkeypatch):
    e = environment
    monkeypatch.setattr(domain, 'review_runtime', forbidden)
    monkeypatch.setattr(EngineProfileStore, 'admit', forbidden)
    assert cli.main(['--data-dir', str(e.app.root), '--list-transcription-profiles']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]['id'] == e.profile.id
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()


def test_cli_preview_requires_explicit_selected_profile(environment, capsys):
    e = environment
    assert cli.main(arguments(e)) == 1
    assert 'Explicitly select' in capsys.readouterr().err
    assert e.service.jobs() == ()


def test_cli_preview_displays_complete_settings_without_executing(environment, capsys):
    e = environment
    assert cli.main([*arguments(e), '--transcription-profile', e.profile.id]) == 0
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan['steps'][0]['arguments']['settings'] == e.settings
    assert e.profile.manifest_sha256 in captured.err
    assert e.service.jobs() == () and not tuple(e.outputs.iterdir())


def test_cli_actual_protocol_run_and_completed_resume_use_current_checkpoint(environment, capsys, monkeypatch):
    e = environment
    assert cli.main([*arguments(e), '--transcription-profile', e.profile.id, '--run']) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['status'] == 'complete' and len(first['results'][0]['artifacts']) == 5
    monkeypatch.setattr(domain.TranscriptionService, 'run', forbidden)
    assert cli.main(['--data-dir', str(e.app.root), '--resume', first['id'], '--run']) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['status'] == 'complete'
    assert resumed['results'] == first['results']


@pytest.mark.parametrize('action', ['--jobs', '--list-transcription-profiles', '--list-local-profiles'])
def test_cli_profile_authority_is_rejected_for_passive_unrelated_actions(environment, action):
    with pytest.raises(SystemExit) as error:
        cli.main(['--data-dir', str(environment.app.root), action,
                  '--transcription-profile', environment.profile.id])
    assert error.value.code == 2


def test_named_transcription_template_retains_only_inert_typed_options(environment):
    e = environment
    saved = e.service.workflow_templates().save('Explicit local transcription', e.plan())
    document = json.loads(saved.instruction())
    assert set(document) == {'schema', 'steps', 'summary'}
    assert document['steps'][0]['arguments']['settings'] == e.settings
    assert e.profile.id not in saved.instruction()
    assert e.profile.manifest_sha256 not in saved.instruction()
    assert 'scope' not in saved.to_dict()['draft']
    unconfigured = AssistantService(e.app)
    with pytest.raises(PermissionError, match='Explicitly select'):
        unconfigured.preview(saved.instruction(), e.scope)
    assert e.service.jobs() == () and not (e.app.root / 'transcription-jobs').exists()
