"""The standalone command reports safe current-store views and exact policies."""
import json

from carr_library.application import LibraryApp
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.operations_cli import main


def test_cli_list_does_not_create_a_job_store_or_coordinator(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    assert main(['--data-dir', str(app.root), 'list']) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not (app.root / 'runtime').exists()
    assert not (app.root / 'state').exists()


def test_cli_policy_update_is_explicit_and_resource_output_hides_private_paths(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    policy = ResourceLimits(cpu_slots=2, memory_bytes=1024**3, disk_bytes=2*1024**3, min_free_disk_bytes=0)
    source = tmp_path / 'resource-policy.json'
    source.write_text(json.dumps(policy.to_dict()), encoding='utf-8')
    assert main(['--data-dir', str(app.root), 'configure-resources', str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['limits'] == policy.to_dict() and result['leases'] == []
    broker = ResourceBroker.for_app(app.root)
    private = tmp_path / 'PRIVATE disk path'
    private.mkdir()
    with broker.reserve(ResourceRequest(disk_bytes=1024, disk_path=private),
                        operation_id='document:' + '1'*32, timeout=0):
        assert main(['--data-dir', str(app.root), 'resources']) == 0
        raw = capsys.readouterr().out
        assert str(private) not in raw and 'owner_token' not in raw
        assert json.loads(raw)['leases'][0]['operation_id'] == 'document:' + '1'*32


def test_invalid_policy_preserves_previous_configuration_and_redacts_diagnostics(tmp_path, capsys):
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    before = broker.limits
    path = tmp_path / 'PRIVATE-secret.json'
    path.write_text('{"cpu_slots": 1000, "arbitrary_command": "PRIVATE"}', encoding='utf-8')
    assert main(['--data-dir', str(app.root), 'configure-resources', str(path)]) == 1
    error = capsys.readouterr().err
    assert 'PRIVATE' not in error and str(path) not in error
    assert ResourceBroker.for_app(app.root).limits == before
