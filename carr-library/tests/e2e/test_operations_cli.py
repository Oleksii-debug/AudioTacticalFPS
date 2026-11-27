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
