"""Actual native and supported flat backups retain production option identity."""
from hashlib import sha256
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.render import ProductionService, _operation_sha256


FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


class InventoryOnly:
    def voices(self):
        return (SimpleNamespace(id='test:inventory'),)


def prepared(tmp_path, legacy=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Production option recovery')
    app.add_text(workspace, 'Original', 'Exact original source for production recovery.')
    KeybindingStore(app.root).save(
        [Command('fixture.production', 'Synthetic production recovery command')],
        {'fixture.production': 'Ctrl+F9'})
    output = tmp_path / 'outputs'
    output.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=InventoryOnly())
    project = service.create('Narrator: Preserve the reviewed options.',
                             voice_ids=('test:inventory',))
    job = service.submit(project, output, format='flac', stems=True)
    path = service.jobs_root / job / 'job.json'
    state = service.get(job)
    assert state['status'] == 'pending'
    assert state['operation_sha256'] == _operation_sha256(state)
    if legacy:
        del state['operation_sha256']
        atomic_json(path, state, replace=True)
    return app, workspace, service, job, path, state


def snapshot(app, folder, flat):
    if flat:
        return FLAT['_flat_backup'](app, folder)
    digest = create_backup(app, folder)
    return read_json(folder / 'manifest.json'), digest


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('legacy', [False, True], ids=['bound-operation', 'historical-operation'])
def test_actual_backup_preserves_operation_identity_and_revokes_output_authority(
        tmp_path, monkeypatch, flat, legacy):
    app, workspace, service, job, path, saved = prepared(tmp_path, legacy)
    raw, keys = path.read_bytes(), KeybindingStore(app.root).path.read_bytes()
    relative = path.relative_to(app.root)
    folder = tmp_path / 'backup'
    manifest, digest = snapshot(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5) and manifest['schema_version'] == 6
    assert (folder / relative).read_bytes() == raw
    target = restore_backup(folder, tmp_path / 'restored', digest)
    expected = {**saved, 'restore_requires_new_output': True}
    reopened = ProductionService(target / 'state/studio', voices=InventoryOnly())
    assert read_json(target / relative) == reopened.get(job) == expected
    assert ('operation_sha256' in expected) is (not legacy)
    if not legacy:
        assert expected['operation_sha256'] == _operation_sha256(expected)
    assert KeybindingStore(target).path.read_bytes() == keys
    assert LibraryApp(target).search(workspace, 'original')
    monkeypatch.setattr(reopened, '_runtime',
                        lambda: pytest.fail('Passive restored output started an audio runtime.'))
    with pytest.raises(ValueError, match='output'):
        reopened.run(job)
    assert read_json(target / relative) == expected
    assert path.read_bytes() == (folder / relative).read_bytes() == raw


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('change', [{'format': 'wav'}, {'stems': False},
                                   {'operation_sha256': '0' * 64},
                                   {'operation_sha256': None}])
def test_rehashed_invalid_operation_options_refuse_restore_without_mutating_backup(
        tmp_path, flat, change):
    app, _, service, job, path, saved = prepared(tmp_path)
    folder = tmp_path / 'invalid-backup'
    manifest, _ = snapshot(app, folder, flat)
    relative = path.relative_to(app.root)
    damaged = {**saved, **change}
    raw = (json.dumps(damaged, ensure_ascii=False) + '\n').encode('utf-8')
    (folder / relative).write_bytes(raw)
    entries = manifest['sidecars'] if flat else manifest['state']
    name = relative.as_posix() if flat else relative.relative_to('state').as_posix()
    entry, = [item for item in entries if item['filename'] == name]
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(manifest_raw)
    digest = sha256(manifest_raw).hexdigest()
    target = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, RestoreError), match='operation options|digest'):
        restore_backup(folder, target, digest)
    assert not target.exists()
    assert (folder / relative).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == manifest_raw
    assert service.get(job) == saved


@pytest.mark.parametrize('change', [{'format': 'wav'}, {'stems': False}])
def test_native_snapshot_rejects_changed_operation_before_publication(tmp_path, change):
    app, _, service, job, path, saved = prepared(tmp_path)
    atomic_json(path, {**saved, **change}, replace=True)
    raw = path.read_bytes()
    folder = tmp_path / 'must-not-create'
    with pytest.raises(ValueError, match='operation options'):
        create_backup(app, folder)
    assert not folder.exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('legacy', [False, True], ids=['bound-operation', 'historical-operation'])
def test_restored_facade_details_are_passive_and_keep_new_output_review(
        tmp_path, monkeypatch, flat, legacy):
    from carr_library.audio import AudioBudget
    from carr_library.operations import OperationFacade, ResourceBroker
    from carr_library.studio.project import Project
    from carr_library.ui.operations_dialog import operation_details_text

    app, _, service, identity, path, saved = prepared(tmp_path, legacy)
    project = Project.from_dict(saved['project'])
    _, plan = service._resource_plan(project, saved,
                                    SimpleNamespace(budget=AudioBudget(threads=1)))
    saved['resource_plan'] = plan
    service._save(saved)
    original = path.read_bytes()
    folder = tmp_path / 'details-backup'
    manifest, digest = snapshot(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5) and manifest['schema_version'] == 6
    target = restore_backup(folder, tmp_path / 'details-restored', digest)
    relative = path.relative_to(app.root)
    restored_bytes = (target / relative).read_bytes()
    restored_app = LibraryApp(target)
    reopened = ProductionService(target / 'state/studio', voices=InventoryOnly())
    def forbidden(*args, **kwargs):
        pytest.fail('Passive restored detail projection acquired runtime authority.')
    monkeypatch.setattr(reopened, '_runtime', forbidden)
    monkeypatch.setattr(reopened, '_verify_artifacts', forbidden)
    with monkeypatch.context() as guard:
        guard.setattr(ResourceBroker, '__init__', forbidden)
        view = OperationFacade(restored_app, studio=reopened).get('studio', identity)
        rendered = operation_details_text(view)
    details = view.details
    assert view.status == 'pending' and view.live is None
    assert details.operation_plan_sha256 == (None if legacy else saved['operation_sha256'])
    assert details.project_fingerprint == saved['fingerprint']
    assert details.adapter is details.settings_sha256 is None
    assert details.resource_plan.cpu_slots == plan['request']['cpu_slots']
    assert details.restore_requires_new_output is True
    assert details.completed_artifact_count is details.omitted_artifact_count is None
    assert 'requires review of a new output' in details.note
    assert 'Saved restore marker requires a new output: Yes' in rendered
    assert str(target) not in rendered and str(Path(saved['destination'])) not in rendered
    with pytest.raises(ValueError, match='requires a new output'):
        reopened.run(identity)
    assert (target / relative).read_bytes() == restored_bytes
    assert path.read_bytes() == (folder / relative).read_bytes() == original
    assert not Path(saved['destination']).exists()
