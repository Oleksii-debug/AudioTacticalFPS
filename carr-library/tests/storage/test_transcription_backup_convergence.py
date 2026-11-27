"""Mixed saved ASR codecs retain data without reactivating either engine.

Both fixtures execute authored synthetic protocol peers through the actual
selected product services. They do not load private models or qualify inference.
"""
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest

from carr_library import backup_transcription
from carr_library.application import LibraryApp
from carr_library.audio import transcription as legacy
from carr_library.audio.models import AudioBudget
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.transcription import TranscriptionService
from carr_library.transcription import service as canonical


def _canonical(tmp_path):
    fixture = runpy.run_path(str(Path(__file__).with_name('test_transcription_backup.py')))
    return fixture['prepared'].__wrapped__(tmp_path)


def _legacy(app, tmp_path, monkeypatch):
    selected = tmp_path / 'legacy-fixture'
    selected.mkdir()
    module = runpy.run_path(str(Path(__file__).parents[1] / 'audio' / 'test_supplied_transcription.py'))
    manifest = module['installation'].__wrapped__(selected, monkeypatch)
    service = legacy.TranscriptionService(
        app.root / 'state', legacy.SuppliedWhisperRuntime(manifest),
        budget=AudioBudget(timeout=10, reserve_disk_bytes=0, max_output_bytes=4 * 1024**2))
    source = selected / 'original.wav'
    source.write_text('Exact legacy synthetic transcript.', encoding='utf-8')
    identity = service.create([source], settings=legacy.TranscriptionSettings(language='uk'))
    return service, identity, source, manifest


def _saved_files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file() and not path.name.endswith('.lock')}


def _forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Backup/recovery must not activate an engine or inspect declared external assets.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(legacy.SuppliedWhisperRuntime, 'verify', forbidden)
    monkeypatch.setattr(canonical, 'inspect_runtime', forbidden)


@pytest.mark.parametrize('completed', (False, True))
def test_mixed_actual_jobs_restore_with_separate_codecs_and_revoked_execution(
        tmp_path, monkeypatch, completed):
    app, _, service, profile, engine, _ = _canonical(tmp_path)
    source = tmp_path / 'canonical-source.wav'
    source.write_bytes(b'Canonical synthetic transport input.')
    job = service.create_job(profile['id'], [source],
        settings={'output_txt': True, 'output_json': True, 'output_srt': True, 'output_vtt': True})
    old, old_id, old_source, manifest = _legacy(app, tmp_path, monkeypatch)
    assert len(job['id']) == 32 and len(old_id) == 64
    if completed:
        assert service.run(job['id'])['status'] == 'complete'
        assert old.run(old_id)['status'] == 'complete'
        service.save_edit(job['id'], job['sources'][0]['id'], 'Separate reviewed reading copy.')
        recognized = service.read_transcript(job['id'])
        edited = service.read_transcript(job['id'], edited=True)
    old_record = old.load(old_id)
    before = _saved_files(service.root)
    assert read_json(service.jobs_root / job['id'] / 'job.json')['schema'] == old_record['schema']
    source.unlink()
    old_source.unlink()
    shutil.rmtree(engine.root)
    shutil.rmtree(manifest.parent)
    backup = tmp_path / 'mixed-backup'
    selected_calls, legacy_calls = [], []
    check_canonical = backup_transcription.validate_collection
    check_legacy = legacy.validate_saved_snapshot
    def canonical_only(entries, records):
        entries = tuple(entries)
        assert entries and all(backup_transcription.limit(row['filename']) is not None for row in entries)
        assert not any('/' + old_id + '/' in row['filename'] for row in entries)
        selected_calls.append(tuple(row['filename'] for row in entries))
        return check_canonical(entries, records)
    def legacy_only(directory, record, **kwargs):
        assert Path(directory).name == old_id and record['id'] == old_id
        legacy_calls.append(record['id'])
        return check_legacy(directory, record, **kwargs)
    with monkeypatch.context() as passive:
        _forbid_execution(passive)
        passive.setattr(backup_transcription, 'validate_collection', canonical_only)
        passive.setattr(legacy, 'validate_saved_snapshot', legacy_only)
        checksum = create_backup(app, backup)
        envelope = read_json(backup / 'manifest.json')
        assert envelope['version'] == 4 and 'sidecars' in envelope and 'state' not in envelope
        inventory = {row['filename'] for row in envelope['sidecars']}
        assert 'state/transcription/profiles/' + profile['id'] + '.json' in inventory
        assert 'state/transcription/jobs/' + job['id'] + '/job.json' in inventory
        assert 'state/transcription/' + old_id + '/job.json' in inventory
        assert not any(name.endswith(('model.bin', 'protocol_worker.py', 'supplied-worker.py'))
                       for name in inventory)
        archived = _saved_files(backup)
        destination = restore_backup(backup, tmp_path / 'mixed-restored', checksum)
        restored = TranscriptionService(destination / 'state' / 'transcription')
        restored_profile, = restored.list_runtimes()
        assert restored_profile['id'] == profile['id']
        assert restored_profile['trusted'] is False and restored_profile['restored'] is True
        current = restored.get_job(job['id'])
        assert current['restored'] is True and current['recovery_review_required'] is True
        assert current['status'] == ('complete' if completed else 'interrupted')
        assert current['plan_fingerprint'] == job['plan_fingerprint']
        if completed:
            assert restored.read_transcript(job['id']) == recognized
            assert restored.read_transcript(job['id'], edited=True) == edited
        restored_old = destination / 'state' / 'transcription' / old_id
        old_value = read_json(restored_old / 'job.json')
        assert old_value == legacy.restored_job(old_record)
        assert read_json(restored_old / 'control.json') == {'command': 'pause'}
        check_legacy(restored_old, old_value)
        assert _saved_files(backup) == archived
    assert selected_calls and legacy_calls
    assert _saved_files(service.root) == before
    for name, raw in before.items():
        # The producer rewrites only its exact control/trust/recovery records.
        if (name.startswith(old_id + '/inputs/') or name.startswith(old_id + '/outputs/')
                or name.startswith('jobs/' + job['id'] + '/inputs/')
                or name.startswith('jobs/' + job['id'] + '/artifacts/')
                or name.startswith('jobs/' + job['id'] + '/edits/')):
            assert (destination / 'state' / 'transcription' / name).read_bytes() == raw


@pytest.mark.parametrize('target_codec', ('canonical', 'legacy'))
def test_shared_schema_label_cannot_substitute_the_other_job_codec(tmp_path, monkeypatch, target_codec):
    app, _, service, profile, _, _ = _canonical(tmp_path)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic canonical source')
    job = service.create_job(profile['id'], [source])
    old, old_id, _, _ = _legacy(app, tmp_path, monkeypatch)
    current_path = service.jobs_root / job['id'] / 'job.json'
    old_path = old.root / old_id / 'job.json'
    current_value, old_value = read_json(current_path), read_json(old_path)
    assert current_value['schema'] == old_value['schema']
    target, wrong = ((current_path, old_value) if target_codec == 'canonical'
                     else (old_path, current_value))
    atomic_json(target, wrong, replace=True)
    before = target.read_bytes()
    with monkeypatch.context() as passive:
        _forbid_execution(passive)
        with pytest.raises((ValueError, OSError)):
            create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert target.read_bytes() == before


def test_only_legacy_jobs_never_enter_canonical_restore_pass(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Legacy saved transcription')
    app.add_text(workspace, 'Retained source', 'Unchanged library text.')
    old, old_id, source, manifest = _legacy(app, tmp_path, monkeypatch)
    expected = old.load(old_id)
    source.unlink()
    shutil.rmtree(manifest.parent)
    def wrong_codec(*args, **kwargs):
        pytest.fail('A hash64-only archive has no canonical profiles/UUID jobs to rebind.')
    with monkeypatch.context() as passive:
        _forbid_execution(passive)
        passive.setattr(canonical, 'prepare_restored_state', wrong_codec)
        checksum = create_backup(app, tmp_path / 'backup')
        destination = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    recovered = destination / 'state' / 'transcription' / old_id
    assert read_json(recovered / 'job.json') == legacy.restored_job(expected)
    assert read_json(recovered / 'control.json') == {'command': 'pause'}
    legacy.validate_saved_snapshot(recovered, read_json(recovered / 'job.json'))
    assert not (destination / 'state' / 'transcription' / 'profiles').exists()
    assert not (destination / 'state' / 'transcription' / 'jobs').exists()


def test_captured_worker_and_pending_cache_trees_are_never_backup_inputs(tmp_path, monkeypatch):
    import os
    app, _, service, profile, _, _ = _canonical(tmp_path)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic canonical source')
    job = service.create_job(profile['id'], [source])
    worker = service.jobs_root / job['id'] / 'worker'
    worker.mkdir(exist_ok=True)
    captured = worker / '.pending-reviewed-worker.py'
    captured.write_text('raise AssertionError("must never run or restore")\n', encoding='utf-8')
    temporary = []
    for name in ('.pending-hf-cache', '.pending-cache', '.pending-bytecode'):
        tree = worker / name
        (tree / 'arbitrary' / 'nested').mkdir(parents=True)
        (tree / 'arbitrary' / 'nested' / 'untyped.bin').write_bytes(b'Not a saved result.')
        temporary.append(tree)
    log = worker / 'logs' / 'progress.log'
    log.parent.mkdir(exist_ok=True)
    log.write_text('Actual producer progress stays a normal text record.\n', encoding='utf-8')
    before = _saved_files(worker)
    scan = os.scandir
    def no_pending_scan(path='.'):
        # shutil's owned-stage cleanup may use an already-open directory fd.
        if isinstance(path, (str, os.PathLike)):
            selected = Path(path)
            assert not any(selected == tree or tree in selected.parents for tree in temporary)
        return scan(path)
    with monkeypatch.context() as passive:
        _forbid_execution(passive)
        passive.setattr(os, 'scandir', no_pending_scan)
        checksum = create_backup(app, tmp_path / 'backup')
        inventory = read_json(tmp_path / 'backup' / 'manifest.json')['sidecars']
        assert not any('/.pending-' in row['filename'] for row in inventory)
        assert any(row['filename'].endswith('/worker/logs/progress.log') for row in inventory)
        destination = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    restored_worker = destination / 'state' / 'transcription' / 'jobs' / job['id'] / 'worker'
    assert (restored_worker / 'logs' / 'progress.log').read_bytes() == log.read_bytes()
    assert not (restored_worker / captured.name).exists()
    assert not any((restored_worker / tree.name).exists() for tree in temporary)
    assert _saved_files(worker) == before


def test_cancel_after_canonical_revocation_cannot_publish_a_partial_mixed_restore(tmp_path, monkeypatch):
    app, _, service, profile, _, _ = _canonical(tmp_path)
    source = tmp_path / 'source.wav'
    source.write_bytes(b'Synthetic canonical source')
    job = service.create_job(profile['id'], [source])
    old, old_id, _, _ = _legacy(app, tmp_path, monkeypatch)
    old_record = old.load(old_id)
    original = _saved_files(service.root)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    archive = _saved_files(backup)
    prepare = canonical.prepare_restored_state
    stopped = False
    def revoke_then_stop(state_root, destination_root=None, **kwargs):
        nonlocal stopped
        result = prepare(state_root, destination_root, **kwargs)
        root = Path(state_root) / 'transcription'
        assert read_json(root / 'profiles' / (profile['id'] + '.json'))['trusted'] is False
        assert read_json(root / 'jobs' / job['id'] / 'job.json')['restored'] is True
        assert read_json(root / old_id / 'job.json') == old_record
        stopped = True
        return result
    with monkeypatch.context() as passive:
        _forbid_execution(passive)
        passive.setattr(canonical, 'prepare_restored_state', revoke_then_stop)
        with pytest.raises(InterruptedError):
            restore_backup(backup, tmp_path / 'unpublished', checksum, cancelled=lambda: stopped)
    assert stopped and not (tmp_path / 'unpublished').exists()
    assert _saved_files(backup) == archive
    assert _saved_files(service.root) == original
