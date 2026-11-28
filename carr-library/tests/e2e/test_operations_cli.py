"""Installed command dispatch reaches the same saved policy and job views."""
import json

from carr_library.__main__ import main
from carr_library.operations import ResourceBroker, ResourceLimits


def test_cli_resources_and_empty_jobs_are_local_and_durable(tmp_path, capsys):
    root = tmp_path / 'library'
    assert main(['operations', '--data-dir', str(root), 'list']) == 0
    assert json.loads(capsys.readouterr().out) == []
    policy = ResourceLimits(cpu_slots=2, memory_bytes=1024**3, disk_bytes=2*1024**3,
                            min_free_disk_bytes=0)
    path = tmp_path / 'resource-policy.json'
    path.write_text(json.dumps(policy.to_dict()))
    assert main(['operations','--data-dir',str(root),'configure-resources',str(path)]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated['leases'] == []
    assert ResourceBroker.for_app(root).limits == policy
    assert main(['operations','--data-dir',str(root),'resources']) == 0
    assert json.loads(capsys.readouterr().out)['leases'] == []


def test_cli_rejects_invalid_policy_preserving_last_good_limits(tmp_path, capsys):
    root = tmp_path / 'library'
    assert main(['operations', '--data-dir', str(root), 'resources']) == 0
    capsys.readouterr()
    before = ResourceBroker.for_app(root).limits
    path = tmp_path / 'invalid.json'
    path.write_text('{"cpu_slots": 1000, "arbitrary_command": "must not run"}')
    assert main(['operations','--data-dir',str(root),'configure-resources',str(path)]) == 1
    assert 'Unsupported resource limit fields' in capsys.readouterr().err
    assert ResourceBroker.for_app(root).limits == before


import pytest


@pytest.mark.parametrize('changed', [False, True], ids=['stored-details', 'tampered-plan'])
def test_cli_projects_redacted_studio_details_from_the_existing_checkpoint(
        tmp_path, capsys, monkeypatch, changed):
    from dataclasses import asdict
    from pathlib import Path
    from types import SimpleNamespace
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioBudget, AudioRuntime
    from carr_library.durable_state import atomic_json
    from carr_library.operations import OperationFacade
    from carr_library.speech import VoiceRegistry
    from carr_library.studio.render import ENGINE_SCHEMA, ProductionService

    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'PRIVATE_OUTPUT_PATH'
    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    project = service.create('[Pause: 0.2]', title='PRIVATE_PROJECT_TITLE')
    identity = service.submit(project, output, stems=False, format='flac')
    saved = service.get(identity)
    _, plan = service._resource_plan(project, saved,
                                    SimpleNamespace(budget=AudioBudget(threads=1)))
    saved.update(resource_plan=plan,
                 runtime={'credential_ref': 'PRIVATE_CREDENTIAL_REF'},
                 progress='PRIVATE_PROGRESS_BODY', error='PRIVATE_DIAGNOSTIC_BODY')
    if changed:
        saved['format'] = 'wav'  # Preserve the old saved digest to expose tampering.
    checkpoint = service.jobs_root / identity / 'job.json'
    atomic_json(checkpoint, saved, replace=True)
    original = checkpoint.read_bytes()
    expected = asdict(OperationFacade(app).list()[0])

    def forbidden(*args, **kwargs):
        pytest.fail('Listing saved operations started a producer or audio runtime.')
    monkeypatch.setattr(ProductionService, '__init__', forbidden)
    monkeypatch.setattr(AudioRuntime, '__init__', forbidden)
    capsys.readouterr()
    assert main(['operations', '--data-dir', str(app.root), 'list']) == 0
    captured = capsys.readouterr()
    row, = json.loads(captured.out)
    assert captured.err == ''
    # JSON arrays represent the facade's frozen tuples without changing content.
    assert row == json.loads(json.dumps(expected))
    assert row['status'] == ('attention' if changed else 'pending') and row['controls'] == []
    details = row['details']
    assert details['adapter'] is details['settings_sha256'] is None
    assert details['completed_artifact_count'] is details['omitted_artifact_count'] is None
    if changed:
        assert details['operation_plan_sha256'] is details['resource_plan'] is None
    else:
        assert details['handler_schema'] == ENGINE_SCHEMA
        assert details['operation_plan_sha256'] == saved['operation_sha256']
        assert details['resource_plan']['cpu_slots'] == plan['request']['cpu_slots']
    for secret in ('PRIVATE_', str(app.root), str(output), str(service.cache)):
        assert secret not in captured.out and secret not in captured.err
    assert checkpoint.read_bytes() == original
    assert not list(output.iterdir()) and not tuple(service.cache.iterdir())
    assert not ResourceBroker.for_app(app.root).snapshot()['leases']
