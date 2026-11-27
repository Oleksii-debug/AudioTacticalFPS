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


@pytest.mark.parametrize('damage', [None, 'boolean-schema', 'relative-primary', 'relative-additional'],
                         ids=['attention-roundtrip', 'boolean-schema', 'relative-primary', 'relative-additional'])
def test_studio_attention_resource_estimates_remain_passive_and_typed_on_restore(
        tmp_path, monkeypatch, damage):
    from pathlib import Path
    from carr_library.durable_state import atomic_json, read_json
    from carr_library.studio.project import Project, Scene
    from carr_library.studio.render import ProductionService

    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state' / 'studio')
    output = tmp_path / 'selected-output'
    output.mkdir()
    project = Project('canonical-attention', 'Pending local work', (),
        (Scene('quiet', 'Retained silent scene', (), minimum_duration=1.25),),
        original_script='An explicit silent scene, not an inferred transcript.')
    identity = service.submit(project, output, stems=False)
    job_path = service.jobs_root / identity / 'job.json'
    job = service.get(identity)
    future_cache = tmp_path / 'unavailable-future-cache'
    request = ResourceRequest(cpu_slots=1, disk_bytes=4096,
        disk_path=Path(job['destination']), additional_disks=((future_cache, 4096),))
    plan = {'schema': 1, 'request': request.to_dict(),
            'cpu_basis': 'explicit synthetic estimate', 'memory_basis': 'no inference',
            'disk_basis': 'retained data declaration', 'enforcement': 'recompute before run'}
    if damage == 'boolean-schema':
        plan['schema'] = True
    elif damage == 'relative-primary':
        plan['request']['disk_path'] = 'relative-output'
    elif damage == 'relative-additional':
        plan['request']['additional_disks'][0][0] = 'relative-cache'
    job.update(status='attention', resource_plan=plan, total_disk_bytes=1024**2)
    atomic_json(job_path, job, replace=True)
    before = job_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail('Restoring a resource estimate cannot reserve resources or run a job.')

    monkeypatch.setattr(ResourceBroker, 'reserve', forbidden)
    monkeypatch.setattr(ProductionService, 'run', forbidden)
    folder = tmp_path / 'attention-backup'
    try:
        if damage is not None:
            with pytest.raises(ValueError):
                create_backup(app, folder)
            assert not folder.exists()
        else:
            checksum = create_backup(app, folder)
            relative = job_path.relative_to(app.root)
            assert (folder / relative).read_bytes() == before
            restored_root = restore_backup(folder, tmp_path / 'restored-attention', checksum)
            restored = read_json(restored_root / relative)
            assert restored['status'] == 'waiting-input' and restored['destination'] == ''
            assert restored['recovery']['previous']['status'] == 'attention'
            assert restored['resource_plan'] == plan
            assert restored['total_disk_bytes'] == 1024**2
            assert restored['runtime'] is None and restored['artifacts'] == []
        assert job_path.read_bytes() == before
        assert not future_cache.exists()
    finally:
        service.close()
