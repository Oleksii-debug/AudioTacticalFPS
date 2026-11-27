"""Current format 5 and canonical flat ASR preserve bytes, not execution trust."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService
from carr_library.state_recovery import validate_resource_policy


def fixture_module():
    path = Path(__file__).parents[1] / 'transcription/fixture_runtime.py'
    spec = importlib.util.spec_from_file_location('composite_asr_fixture', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepared(tmp_path):
    fixture = fixture_module()
    engine = fixture.make_runtime(tmp_path / 'reviewed-runtime')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Immutable library scope')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    checkpoint = TranscriptionService(app.root / 'state/transcription', broker=broker)
    profile = fixture.register(checkpoint, engine)
    source = tmp_path / 'local-file.wav'
    raw = b'Original local-file protocol input.'
    source.write_bytes(raw)
    job = checkpoint.create_job(profile['id'], [source])
    return app, workspace, checkpoint, profile, source, raw, job


def test_current_format5_restores_both_protocols_without_cross_decode_or_authority(tmp_path, monkeypatch):
    app, workspace, checkpoint, profile, source, raw, job = prepared(tmp_path)
    strict_source = tmp_path / 'strict.wav'
    strict_raw = b'Original strict receipt input.'
    strict_source.write_bytes(strict_raw)
    strict = TranscriptionService(app, workspace)
    strict_id = strict.submit([strict_source])
    strict_record = strict.get(strict_id)
    # A genuine existing typed sidecar composes with native state as format 5.
    atomic_json(app.root / 'connections.json',
                {'version': 1, 'kind': 'provider-connections', 'profiles': []})
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    assert manifest['version'] == 5 and manifest['schema_version'] == 6
    original_profiles = checkpoint.list_runtimes()
    source.unlink()
    strict_source.unlink()
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored_app = LibraryApp(destination)
    restored_strict = TranscriptionService(restored_app, workspace)
    restored_checkpoint = TranscriptionService(destination / 'state/transcription')
    strict_saved = restored_strict.get(strict_id)
    checkpoint_saved = restored_checkpoint.get_job(job['id'])
    assert strict_saved['restore_requires_review'] is True
    assert {key: value for key, value in strict_saved.items() if key != 'restore_requires_review'} == strict_record
    assert checkpoint_saved['restored'] is True and checkpoint_saved['recovery_review_required'] is True
    assert checkpoint_saved['plan_fingerprint'] == job['plan_fingerprint']
    assert (restored_strict._path(strict_id) / 'input' /
            strict_saved['sources'][0]['filename']).read_bytes() == strict_raw
    assert (restored_checkpoint._job_root(job['id']) /
            checkpoint_saved['sources'][0]['retained_path']).read_bytes() == raw
    assert restored_checkpoint.list_runtimes()[0] == {**profile, 'trusted': False, 'restored': True}
    assert checkpoint.list_runtimes() == original_profiles
    def forbidden(*args, **kwargs):
        raise AssertionError('Restoration cannot authorize an external process.')
    import carr_library.transcription as receipts
    from carr_library.transcription import service as local_files
    monkeypatch.setattr(receipts, 'run_process', forbidden)
    monkeypatch.setattr(local_files, 'run_process', forbidden)
    with pytest.raises(PermissionError, match='Restored'):
        restored_strict.run(strict_id)
    assert restored_checkpoint.run(job['id'])['status'] == 'failed'


def flat_backup(app, path):
    create_backup(app, path)
    value = read_json(path / 'manifest.json')
    assert 'sidecars' not in value
    value['version'] = 4
    value['sidecars'] = [{**row, 'filename': 'state/' + row['filename']}
                        for row in value.pop('state')]
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (path / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest(), value


def test_foreign_flat_pending_job_and_empty_optional_cache_restore_with_exact_sources(tmp_path):
    app, _, checkpoint, profile, source, raw, job = prepared(tmp_path)
    backup = tmp_path / 'flat-backup'
    checksum, manifest = flat_backup(app, backup)
    cache = backup / 'state/transcription/jobs' / job['id'] / 'worker/logs/cache'
    cache.mkdir(parents=True)
    destination = restore_backup(backup, tmp_path / 'restored-flat', checksum)
    restored = TranscriptionService(destination / 'state/transcription')
    saved = restored.get_job(job['id'])
    assert saved['restored'] is True
    assert (restored._job_root(job['id']) / saved['sources'][0]['retained_path']).read_bytes() == raw
    assert restored.list_runtimes()[0]['trusted'] is False
    assert read_json(restored._job_root(job['id']) / 'control.json') == {'action': 'run'}
    assert source.read_bytes() == raw
    # Empty recomputable directories do not authorize undeclared file bytes.
    (cache / 'undeclared.bin').write_bytes(b'Not in the manifest')
    with pytest.raises(ValueError, match='unexpected'):
        restore_backup(backup, tmp_path / 'must-not-publish', checksum)
    assert not (tmp_path / 'must-not-publish').exists()


@pytest.mark.parametrize('directory', ['profiles', 'jobs'])
def test_new_flat_reserved_namespace_alias_refuses_backup_before_publication(tmp_path, directory):
    app, _, service, _, _, _, job = prepared(tmp_path)
    original = service.root / directory
    intermediate = service.root / 'case-change'
    original.rename(intermediate)
    mixed = service.root / directory.title()
    intermediate.rename(mixed)
    with pytest.raises(ValueError, match='ambiguous'):
        create_backup(app, tmp_path / 'must-not-publish')
    assert mixed.is_dir() and not (tmp_path / 'must-not-publish').exists()


@pytest.mark.parametrize('change', [
    {'cpu_slots': True}, {'max_leases': 0}, {'disk_bytes': -1},
    {'memory_bytes': 2**63}, {'gpu_devices': [['cuda:0', False]]},
])
def test_restored_resource_policy_preserves_exact_numeric_bounds(change):
    policy = {'schema': 'carr-resource-policy/1', 'limits': {
        'cpu_slots': 2, 'memory_bytes': 1024, 'disk_bytes': 1024,
        'min_free_disk_bytes': 0, 'gpu_devices': [], 'max_leases': 8}}
    policy['limits'].update(change)
    with pytest.raises(ValueError):
        validate_resource_policy(policy)


def test_resource_policy_validation_does_not_discover_hardware_or_activate_leases(monkeypatch):
    from carr_library.operations import models
    policy = {'schema': 'carr-resource-policy/1', 'limits': {
        'cpu_slots': 2, 'memory_bytes': 1024, 'disk_bytes': 1024,
        'min_free_disk_bytes': 0, 'gpu_devices': [], 'max_leases': 8}}
    monkeypatch.setattr(models.os, 'cpu_count',
        lambda: (_ for _ in ()).throw(AssertionError('No hardware lookup during restoration')))
    assert validate_resource_policy(policy) is policy
