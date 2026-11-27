"""Full library backup retains ASR sources/results but revokes external execution."""
import importlib.util
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService


@pytest.fixture
def prepared(tmp_path):
    path = Path(__file__).parents[1] / 'transcription' / 'fixture_runtime.py'
    spec = importlib.util.spec_from_file_location('backup_synthetic_runtime', path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    engine = fixture.make_runtime(tmp_path / 'external-engine')
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Existing research')
    app.add_text(workspace, 'Source book', 'Збережений точний оригінал.')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    service = TranscriptionService(app.root / 'state' / 'transcription', broker=broker)
    profile = fixture.register(service, engine)
    return app, workspace, service, profile, engine, fixture


def test_complete_backup_preserves_pending_sources_after_original_is_lost_and_requires_review(prepared, tmp_path):
    app, workspace, service, profile, engine, fixture = prepared
    audio = tmp_path / 'Оригінальний запис.wav'
    raw = b'Private synthetic audio transport bytes'
    audio.write_bytes(raw)
    job = service.create_job(profile['id'], [audio])
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    audio.unlink()
    destination = restore_backup(backup, tmp_path / 'restored-library', digest)
    restored_app = LibraryApp(destination)
    assert restored_app.search(workspace, 'точний')[0].quote == 'точний'
    restored = TranscriptionService(destination / 'state' / 'transcription',
        broker=ResourceBroker.for_app(destination))
    reviewed = restored.get_job(job['id'])
    assert reviewed['status'] == 'interrupted' and reviewed['restored']
    assert restored.list_runtimes()[0]['trusted'] is False
    retained = restored.jobs_root / job['id'] / reviewed['sources'][0]['retained_path']
    assert retained.read_bytes() == raw
    rejected = restored.run(job['id'])
    assert rejected['status'] == 'failed' and rejected['done'] == 0
    assert not list((restored.jobs_root / job['id']).rglob('job_status.json'))
    trusted = fixture.register(restored, engine)
    restored.rebind_runtime(job['id'], trusted['id'])
    complete = restored.resume(job['id'])
    assert complete['status'] == 'complete', complete['error']
    assert restored.read_transcript(job['id'])['text'] == 'Синтетичний протокольний результат.'
    assert service.get_job(job['id'])['status'] == 'pending'


def test_completed_transcript_and_edits_restore_without_external_runtime_or_cache(prepared, tmp_path):
    app, _, service, profile, engine, _ = prepared
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'Synthetic audio transport fixture')
    job = service.create_job(profile['id'], [audio],
        settings={'output_txt':True,'output_json':True,'output_srt':True,'output_vtt':True})
    complete = service.run(job['id'])
    assert complete['status'] == 'complete', complete['error']
    recognized = service.read_transcript(job['id'])
    service.save_edit(job['id'], job['sources'][0]['id'], 'Окрема правка; оригінал збережено.')
    backup = tmp_path / 'complete-backup'
    digest = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    assert not any('/cache/' in row['filename'] for row in manifest['state'])
    assert any('/inputs/' in row['filename'] for row in manifest['state'])
    assert not any(row['filename'].endswith(('model.bin','protocol_worker.py','python.exe')) for row in manifest['state'])
    audio.unlink()
    shutil.rmtree(engine.root)
    destination = restore_backup(backup, tmp_path / 'complete-restored', digest)
    restored = TranscriptionService(destination / 'state' / 'transcription')
    assert restored.read_transcript(job['id']) == recognized
    assert restored.read_transcript(job['id'], edited=True)['text'] == 'Окрема правка; оригінал збережено.'
    assert restored.run(job['id'])['status'] == 'complete'
    export = tmp_path / 'restored-exports'
    export.mkdir()
    assert restored.export(job['id'], export)['status'] == 'complete'


def test_tampered_retained_input_cannot_publish_restore(prepared, tmp_path):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'source.wav'
    audio.write_bytes(b'Original synthetic input')
    service.create_job(profile['id'], [audio])
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    retained, = backup.glob('state/transcription/jobs/*/inputs/*/*')
    retained.write_bytes(b'Altered input')
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, tmp_path / 'must-not-exist', digest)
    assert not (tmp_path / 'must-not-exist').exists()
