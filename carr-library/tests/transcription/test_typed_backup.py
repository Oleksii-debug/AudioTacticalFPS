"""Actual product backup of synthetic worker state; no ASR-quality claim."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json, state_lock
from carr_library.library_backup import create_backup, restore_backup, declared_backup_files
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService
from carr_library.transcription.models import fingerprint
from carr_library.transcription import backup as component


@pytest.fixture
def prepared(synthetic_asr_runtime, tmp_path):
    runtime = synthetic_asr_runtime
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state/transcription')
    inspected = service.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    profile = service.register_runtime('Synthetic protocol fixture', runtime.python,
        runtime.worker, runtime.model, owner_trusted=True,
        expected_fingerprint=inspected['fingerprint'])
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Purpose-written protocol input, not decoded speech.')
    job = service.create_job(profile['id'], [source])
    assert service.run(job['id'])['status'] == 'complete'
    return app, service, runtime, profile, source, job


def manifest(folder):
    return json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))


def resign(folder, mutate):
    value = manifest(folder)
    mutate(value)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_version_six_preserves_retained_original_results_and_edit_history_without_runtime(prepared, tmp_path):
    app, service, runtime, profile, source, job = prepared
    source_id = job['sources'][0]['id']
    original = service.read_transcript(job['id'])
    first = service.save_edit(job['id'], source_id, 'First separately saved reading edit.')
    latest = service.save_edit(job['id'], source_id, 'Окрема остання правка.')
    original_bytes = source.read_bytes()
    source.unlink()
    # A completed backup reads only historical metadata for these external paths.
    shutil.rmtree(runtime.model)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    value = manifest(backup)
    assert value['version'] == 6
    assert value['transcription']['restore_mode'] == 'review-required'
    assert value['transcription']['excluded'] == component.EXCLUDED
    names = {row['filename'] for row in value['transcription']['files']}
    assert any(name.endswith(first['path']) for name in names)
    assert any(name.endswith(latest['path']) for name in names)
    assert not any('/worker/' in name or '/worker-history/' in name for name in names)
    assert all(name in declared_backup_files(value) for name in ['transcription/' + n for n in names])
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranscriptionService(restored_root / 'state/transcription')
    assert restored.list_runtimes()[0]['trusted'] is False
    assert restored.list_runtimes()[0]['restored'] is True
    assert restored.get_job(job['id'])['recovery_review_required'] is True
    assert restored.read_transcript(job['id']) == original
    assert restored.read_transcript(job['id'], edited=True)['text'] == latest['text']
    assert (restored._job_root(job['id']) / job['sources'][0]['retained_path']).read_bytes() == original_bytes
    assert not (restored._job_root(job['id']) / 'worker').exists()
    assert not (restored_root / 'runtime').exists()
    assert restored.run(job['id'])['status'] == 'complete'  # accepted results require no new process
    assert service._profile(profile['id'])['trusted'] is True


def test_library_without_asr_state_retains_exact_version_five_shape(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    value = manifest(backup)
    assert value['version'] == 5 and 'transcription' not in value
    assert not (backup / 'transcription').exists()
    assert restore_backup(backup, tmp_path / 'restored', checksum).exists()


def test_resource_policy_alone_is_typed_and_live_leases_are_never_restored(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    policy = ResourceLimits(cpu_slots=3, min_free_disk_bytes=0)
    broker.configure(policy)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    value = manifest(backup)
    assert value['version'] == 6
    assert [row['filename'] for row in value['transcription']['files']] == ['resource-policy.json']
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (restored / 'runtime').exists()
    assert read_json(restored / 'state/operations/resources.json')['limits'] == policy.to_dict()


@pytest.mark.parametrize('lock', ['run', 'inventory'])
def test_active_run_or_retention_transaction_blocks_complete_snapshot(prepared, tmp_path, lock):
    app, service, _, _, _, job = prepared
    directory = service._job_root(job['id']) if lock == 'run' else service.root
    with state_lock(directory, lock):
        with pytest.raises(RuntimeError, match='already being changed'):
            create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_shared_total_budget_accounts_for_asr_inputs_and_results(prepared, tmp_path):
    app, _, _, _, _, _ = prepared
    reference = tmp_path / 'reference'
    create_backup(app, reference)
    value = manifest(reference)
    base_files = 1 + len(declared_backup_files(value))
    with pytest.raises((ValueError, RuntimeError), match='budget|limit'):
        create_backup(app, tmp_path / 'too-few-files', max_files=base_files - 1)
    assert not (tmp_path / 'too-few-files').exists()
    payload_bytes = sum(row['size_bytes'] for row in declared_backup_files(value).values())
    with pytest.raises((ValueError, RuntimeError), match='budget|limit'):
        create_backup(app, tmp_path / 'too-small', max_bytes=payload_bytes - 1)
    assert not (tmp_path / 'too-small').exists()


def test_missing_accepted_result_fails_before_destination_publication(prepared, tmp_path):
    app, _, _, _, _, _ = prepared
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    entry = next(row for row in manifest(backup)['transcription']['files']
                 if row['filename'].endswith('/transcript.txt'))
    (backup / 'transcription' / entry['filename']).unlink()
    with pytest.raises((ValueError, RuntimeError), match='incomplete|missing|differs'):
        restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('bad_name', ['../outside.wav', 'jobs/abc/job.json',
    'jobs/' + 'a' * 32 + '/worker/job_config.json',
    'jobs/' + 'a' * 32 + '/inputs/' + 'b' * 32 + '/source.wav:stream'])
def test_resigned_outer_manifest_cannot_expand_typed_path_authority(prepared, tmp_path, bad_name):
    app, _, _, _, _, _ = prepared
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    def mutate(value):
        value['transcription']['files'][0]['filename'] = bad_name
    checksum = resign(backup, mutate)
    with pytest.raises((ValueError, RuntimeError), match='transcription'):
        restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()


def test_resigned_file_digest_cannot_forge_cross_source_result_binding(prepared, tmp_path):
    app, _, _, _, _, _ = prepared
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    value = manifest(backup)
    entry = next(row for row in value['transcription']['files']
                 if row['filename'].endswith('/receipt.json'))
    path = backup / 'transcription' / entry['filename']
    receipt = read_json(path)
    receipt['provenance']['source_sha256'] = '0' * 64
    atomic_json(path, receipt, replace=True)
    raw = path.read_bytes()
    def mutate(value):
        record = next(row for row in value['transcription']['files']
                      if row['filename'] == entry['filename'])
        record.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
    checksum = resign(backup, mutate)
    with pytest.raises((ValueError, RuntimeError), match='transcription|receipt changed'):
        restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()


def test_copied_payload_hardlink_is_rejected_before_restore(prepared, tmp_path):
    import os
    app, _, _, _, _, _ = prepared
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    entry = manifest(backup)['transcription']['files'][0]
    path = backup / 'transcription' / entry['filename']
    os.link(path, tmp_path / 'external-alias')
    with pytest.raises((ValueError, RuntimeError), match='links|transcription'):
        restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()


def test_profile_schema_or_plan_change_is_not_silently_exported(prepared, tmp_path):
    app, service, _, profile, _, job = prepared
    path = service._job_root(job['id']) / 'job.json'
    value = read_json(path)
    value['sources'][0]['retained_path'] = '../../outside.wav'
    value['plan_fingerprint'] = fingerprint({name: value[name] for name in
        ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')})
    atomic_json(path, value, replace=True)
    with pytest.raises((ValueError, RuntimeError), match='retained-source path'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_cancelled_snapshot_never_publishes_completion_manifest(prepared, tmp_path, monkeypatch):
    app, _, _, _, _, _ = prepared
    copied = []
    original = component._copy
    def copy(*args, **kwargs):
        result = original(*args, **kwargs)
        copied.append(True)
        return result
    monkeypatch.setattr(component, '_copy', copy)
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'backup', cancelled=lambda: bool(copied))
    assert not (tmp_path / 'backup/manifest.json').exists()
