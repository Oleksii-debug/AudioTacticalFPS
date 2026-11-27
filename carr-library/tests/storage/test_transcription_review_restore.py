"""Actual retained-job restore revokes execution without touching private runtimes."""
from hashlib import sha256
import json
from pathlib import Path
import runpy
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library import transcription
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup

_helpers = runpy.run_path(str(Path(__file__).parents[1] / 'transcription' / 'test_runtime_review.py'))
make_peer = _helpers['make_peer']


def test_completed_named_job_restores_passively_without_external_files(tmp_path, monkeypatch):
    peer = make_peer(tmp_path / 'peer')
    profile = peer.service.configuration()['profile_id']
    identity = peer.service.submit([peer.source], profile_id=profile,
        options={'model': 'kept-selector', 'language': 'uk', 'beam_size': 2})
    result = peer.service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    source = result['sources'][0]['filename']
    revision = peer.service.save_edit(identity, source, 'Separate retained human reading copy.')
    before = peer.service.configuration()
    text = peer.service.text(identity, source)
    peer.source.unlink()
    peer.worker.unlink()
    shutil.rmtree(peer.models)
    shutil.rmtree(peer.environment)
    def forbidden(*args, **kwargs):
        raise AssertionError('Passive backup/restore/read opened an external execution identity.')
    for name in ('inspect_python', 'inspect_models', '_python_runtime', 'run_process'):
        monkeypatch.setattr(transcription, name, forbidden)
    backup = tmp_path / 'backup'
    checksum = peer.app.backup(backup)
    restored = restore_backup(backup, tmp_path / 'restored', expected_manifest_sha256=checksum)
    service = LibraryApp(restored).transcription(peer.workspace)
    after = service.configuration()
    assert after['execution_review_required'] is True
    assert {key: value for key, value in after.items() if key != 'execution_review_required'} == {
        key: value for key, value in before.items() if key != 'execution_review_required'}
    row = service.list_runtimes()[0]
    assert row['trusted'] is False and row['restored'] is True
    assert row['configuration']['models_dir'] == before['models_dir']
    assert row['configuration']['models_inventory'] == before['models_inventory']
    assert service.get(identity)['settings'] == result['settings']
    assert service.get(identity)['execution_review_required'] is True
    assert service.text(identity, source) == text
    assert service.edits(identity)[0]['id'] == revision
    assert service.run(identity, timeout=30)['status'] == 'completed'
    service.export(identity, tmp_path / 'exported', formats=('txt',))
    second = tmp_path / 'second-backup'
    assert service.app.backup(second)


def test_restored_ready_job_keeps_options_but_needs_review_and_rebind(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    profile = peer.service.configuration()['profile_id']
    options = {'model': 'selected-local-model', 'language': 'uk', 'beam_size': 3,
               'vad_filter': False, 'condition_on_previous_text': False}
    identity = peer.service.submit([peer.source], profile_id=profile, options=options)
    original_job = peer.service.get(identity)
    checksum = peer.app.backup(tmp_path / 'backup')
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=checksum)
    service = LibraryApp(root).transcription(peer.workspace)
    assert service.get(identity)['settings'] == original_job['settings']
    assert service.configuration()['models_dir'] == str(peer.models)
    assert service.get(identity)['execution_review_required'] is True
    with pytest.raises(transcription.RuntimeReviewError, match='[Rr]eview'):
        service.run(identity, timeout=30)
    with pytest.raises(transcription.RuntimeReviewError):
        service.rebind_runtime(identity, profile)
    assert not (service._path(identity) / 'job_status.json').exists()
    inspected = service.inspect_runtime(peer.python, peer.worker, peer.models)
    accepted = service.register_runtime('Reviewed after restore', peer.python, peer.worker, peer.models,
        owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    rebound = service.rebind_runtime(identity, accepted['id'])
    assert rebound['settings'] == original_job['settings']
    assert rebound['execution_review_required'] is False
    result = service.resume(identity, timeout=30)
    assert result['status'] == 'completed' and result['done'] == 1, result['last_error']


def test_legacy_engine_restore_is_readable_but_never_grants_new_execution(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    old = {key: value for key, value in peer.configured.items() if key not in (
        'runtime_inventory', 'models_inventory', 'fingerprint', 'profile_id',
        'execution_review_required')}
    atomic_json(peer.service.root / 'engine.json', old, replace=True)
    identity = peer.service.submit([peer.source], options={'model': 'legacy-selector'})
    checksum = peer.app.backup(tmp_path / 'backup')
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=checksum)
    service = LibraryApp(root).transcription(peer.workspace)
    config = service.configuration()
    assert config['models_dir'] == old['models_dir'] and config['python'] == old['python']
    assert config['execution_review_required'] is True
    assert service.get(identity)['settings']['model'] == 'legacy-selector'
    with pytest.raises(transcription.RuntimeReviewError):
        service.run(identity, timeout=30)
    assert not (service._path(identity) / 'job_status.json').exists()


def test_resigned_outer_backup_cannot_change_profile_inventory(tmp_path):
    peer = make_peer(tmp_path / 'peer')
    identity = peer.service.submit([peer.source], profile_id=peer.configured['profile_id'])
    destination = tmp_path / 'backup'
    peer.app.backup(destination)
    manifest = read_json(destination / 'manifest.json')
    record = next(row for row in manifest['sidecars'] if '/transcription/profiles/' in row['filename'])
    profile = read_json(destination / record['filename'])
    profile['configuration']['runtime_inventory']['python_identity']['sha256'] = '0' * 64
    raw = (json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (destination / record['filename']).write_bytes(raw)
    record.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (destination / 'manifest.json').write_bytes(encoded)
    with pytest.raises((ValueError, OSError), match='runtime|inventory|fingerprint|profile|sidecar'):
        restore_backup(destination, tmp_path / 'refused', expected_manifest_sha256=sha256(encoded).hexdigest())
    assert not (tmp_path / 'refused').exists()
    assert peer.service.get(identity)['status'] == 'ready'

def test_pending_host_review_restores_as_veto_never_as_completed_execution(tmp_path, monkeypatch):
    import errno
    peer = make_peer(tmp_path / 'peer')
    identity = peer.service.submit([peer.source])
    original = peer.payloads['av'].read_bytes()
    save, write = peer.service._save, transcription.atomic_json
    def after_candidate(path, value):
        result = save(path, value)
        if value['status'] == 'completed':
            peer.payloads['av'].write_bytes(original + b'# Drift at actual publication.\n')
        return result
    def no_space(target, value, **kwargs):
        if Path(target).name == 'rejected-execution.json':
            raise OSError(errno.ENOSPC, 'No space for the rejection.')
        return write(target, value, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(peer.service, '_save', after_candidate)
        patch.setattr(transcription, 'atomic_json', no_space)
        with pytest.raises(OSError) as error:
            peer.service.run(identity, timeout=30)
        assert error.value.errno == errno.ENOSPC
    veto = peer.service._review_path(identity).read_bytes()
    assert read_json(peer.service._path(identity) / 'job.json')['status'] == 'completed'
    checksum = peer.app.backup(tmp_path / 'backup')
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored',
                          expected_manifest_sha256=checksum)
    restored = LibraryApp(root).transcription(peer.workspace)
    assert restored._review_path(identity).read_bytes() == veto
    state = restored.get(identity)
    assert state['status'] == 'interrupted' and state['recovery_review_required'] is True
    assert state['execution_review_required'] is True
    with pytest.raises(transcription.RuntimeReviewError):
        restored.verify(identity)
    peer.payloads['av'].write_bytes(original)
    inspected = restored.inspect_runtime(peer.python, peer.worker, peer.models)
    profile = restored.register_runtime('New local review', peer.python, peer.worker, peer.models,
        owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    with pytest.raises(transcription.RuntimeReviewError):
        restored.rebind_runtime(identity, profile['id'])
    restored.acknowledge_interruption(identity, worker_stopped=True)
    copied = restored.copy_job(identity, profile_id=profile['id'])
    assert copied['status'] == 'ready' and copied['id'] != identity
    assert restored._review_path(identity).read_bytes() == veto
