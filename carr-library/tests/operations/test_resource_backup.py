"""Existing complete-library backup retains policy, never active reservations."""
from dataclasses import replace
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceRequest


@pytest.mark.parametrize('include_generated', [False, True])
def test_complete_backup_restores_exact_policy_without_replaying_live_leases(tmp_path, include_generated):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Resource policy recovery')
    app.add_text(workspace, 'Retained original', 'Synthetic unchanged original text.')
    broker = ResourceBroker.for_app(app.root)
    policy = replace(broker.limits, cpu_slots=2, memory_bytes=1024**3,
                     gpu_devices=(('cuda', 512 * 1024**2),), min_free_disk_bytes=0)
    broker.configure(policy)
    original = broker.policy_path.read_bytes()
    request = ResourceRequest(cpu_slots=2, gpu_device='cuda', gpu_memory_bytes=512 * 1024**2,
                              devices=('accelerator:cuda',))
    with broker.reserve(request, operation_id='asr:retained-source-fixture', timeout=0):
        folder = tmp_path / 'backup'
        checksum = create_backup(app, folder, include_generated=include_generated)
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['version'] == 4 and 'state' not in manifest
        names = {entry['filename'] for entry in manifest['sidecars']}
        assert 'state/operations/resources.json' in names
        assert not any('runtime' in name.split('/') or 'leases' in name.split('/') for name in names)
        assert (folder / 'state/operations/resources.json').read_bytes() == original
        destination = restore_backup(folder, tmp_path / 'restored', checksum)
        assert (destination / 'state/operations/resources.json').read_bytes() == original
        assert not (destination / 'runtime').exists()
        restored = ResourceBroker.for_app(destination)
        assert restored.limits == policy
        assert not restored.snapshot()['leases']
        assert len(broker.snapshot()['leases']) == 1
        with restored.reserve(request, operation_id='asr:new-restored-operation', timeout=0):
            assert len(restored.snapshot()['leases']) == 1
        assert not restored.snapshot()['leases']
    assert not broker.snapshot()['leases']
