"""Full library backup retains ASR sources/results but revokes external execution."""
import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.transcription import TranscriptionService
from carr_library import backup_compat as backup_sidecars, backup_transcription
from carr_library.transcription.models import fingerprint


def state_files(manifest):
    # This host preserves its format-5/3 native producer. Only the test's
    # inventory projection changes; every original path/hash assertion remains.
    return [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]



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
    assert not any('/cache/' in row['filename'] for row in state_files(manifest))
    assert any('/inputs/' in row['filename'] for row in state_files(manifest))
    assert not any(row['filename'].endswith(('model.bin','protocol_worker.py','python.exe')) for row in state_files(manifest))
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


@pytest.mark.parametrize('include_generated', [False, True])
def test_typed_cache_inclusion_keeps_source_and_result_authority(prepared, tmp_path, include_generated):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio])
    assert service.run(job['id'])['status'] == 'complete'
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup, include_generated=include_generated)
    manifest = read_json(backup / 'manifest.json')
    assert any('/cache/' in row['filename'] for row in state_files(manifest)) is include_generated
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranscriptionService(destination / 'state/transcription')
    assert restored.read_transcript(job['id']) == service.read_transcript(job['id'])
    assert restored.list_runtimes()[0]['trusted'] is False
    assert not list((destination / 'state/transcription').rglob('job_config.json'))


def test_consistent_manifest_cannot_hide_mismatched_source_binding(prepared, tmp_path):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio])
    job['sources'][0]['sha256'] = '0' * 64
    job['plan_fingerprint'] = fingerprint({name: job[name] for name in
        ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')})
    atomic_json(service.jobs_root / job['id'] / 'job.json', job, replace=True)
    with pytest.raises(ValueError, match='Missing or changed transcription source'):
        create_backup(app, tmp_path / 'must-not-publish')
    assert not (tmp_path / 'must-not-publish').exists()


@pytest.mark.parametrize('mutation', ['receipt', 'unknown_file', 'unknown_profile_field'])
def test_untyped_or_forged_asr_data_refuses_backup(prepared, tmp_path, mutation):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio])
    assert service.run(job['id'])['status'] == 'complete'
    if mutation == 'receipt':
        job = service.get_job(job['id'])
        receipt = service.jobs_root / job['id'] / job['results'][0]['receipt']
        value = read_json(receipt)
        value['provenance']['citation_authority'] = True
        atomic_json(receipt, value, replace=True)
    elif mutation == 'unknown_profile_field':
        target = service.profiles_root / (profile['id'] + '.json')
        value = read_json(target)
        value['execute_after_restore'] = 'not a supported field'
        atomic_json(target, value, replace=True)
    else:
        (service.jobs_root / job['id'] / 'worker' / 'unknown.py').write_text('# inert unknown file\n')
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'must-not-publish')
    assert not (tmp_path / 'must-not-publish').exists()


def test_silent_empty_exports_and_historical_edits_survive_typed_backup(prepared, tmp_path):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio], settings={'output_srt': True})
    assert service.run(job['id'])['status'] == 'complete'
    state = service._load(job['id'])
    old_receipt = service._receipt(state, state['results'][0])
    state['results'] = []
    silent = {'language': 'uk', 'duration': 61.0, 'segments': [], 'text': ''}
    service._publish_result(state, state['sources'][0], silent, old_receipt['provenance'])
    state.update(status='complete', done=1)
    service._save(state)
    first = service.save_edit(job['id'], job['sources'][0]['id'], 'Перша окрема правка.')
    second = service.save_edit(job['id'], job['sources'][0]['id'], 'Друга окрема правка.')
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    empty = [row for row in state_files(manifest) if row['size_bytes'] == 0]
    assert {Path(row['filename']).suffix for row in empty} == {'.txt', '.srt'}
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranscriptionService(destination / 'state/transcription')
    assert restored.read_transcript(job['id'])['text'] == ''
    assert restored.read_transcript(job['id'], edited=True)['text'] == second['text']
    assert read_json(restored.jobs_root / job['id'] / first['path'])['text'] == first['text']


def test_input_streaming_has_separate_budget_and_honors_whole_backup_limit(prepared, tmp_path, monkeypatch):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'larger-than-metadata.wav'
    audio.write_bytes(b'S' * (1024 * 1024))
    service.create_job(profile['id'], [audio])
    monkeypatch.setattr(backup_transcription, 'MAX_METADATA_BYTES', 512 * 1024)
    # A source larger than the entire metadata allowance remains streamable.
    create_backup(app, tmp_path / 'successful')
    from carr_library import state_backup
    snapshot = state_backup.snapshot_state
    with monkeypatch.context() as bound:
        def bounded(*args, **kwargs):
            return snapshot(*args, **kwargs, max_bytes=512 * 1024)
        bound.setattr(state_backup, 'snapshot_state', bounded)
        with pytest.raises(ValueError):
            create_backup(app, tmp_path / 'bounded')
    monkeypatch.setattr(backup_transcription, 'MAX_INPUT_BYTES', audio.stat().st_size - 1)
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'excessive-input')
    assert not (tmp_path / 'bounded').exists()
    assert not (tmp_path / 'excessive-input').exists()


def test_legacy_state_manifest_keeps_typed_asr_and_revokes_execution(prepared, tmp_path):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio])
    assert service.run(job['id'])['status'] == 'complete'
    service._archive_worker(service._load(job['id']), profile['id'])
    backup = tmp_path / 'legacy-backup'
    create_backup(app, backup, include_generated=True)
    manifest = read_json(backup / 'manifest.json')
    entries = state_files(manifest)
    assert all(entry['filename'].startswith('state/') for entry in entries)
    # Exercise the accepted foreign flat-v4 envelope from exact native copied
    # bytes. Both shapes retain source hashes and must revoke execution trust.
    manifest['version'] = 4
    manifest.pop('state')
    assert 'sidecars' not in manifest
    manifest['sidecars'] = entries
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (backup / 'manifest.json').write_bytes(raw)
    destination = restore_backup(backup, tmp_path / 'restored-legacy', sha256(raw).hexdigest())
    restored = TranscriptionService(destination / 'state/transcription')
    assert restored.get_job(job['id'])['restored']
    assert restored.list_runtimes()[0]['trusted'] is False
    assert list((destination / 'state/transcription').rglob('*.cache.json'))
    assert not list((destination / 'state/transcription').rglob('job_config.json'))


def test_cancellation_during_final_asr_restore_review_prevents_publication(prepared, tmp_path, monkeypatch):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    service.create_job(profile['id'], [audio])
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    original = backup_transcription.validate_collection
    cancelled = False
    def finish_review(entries, records):
        nonlocal cancelled
        original(entries, records)
        if any(record.get('schema') == 'resonance-transcription-job/1' and record.get('restored')
               for record in records.values()):
            cancelled = True
    monkeypatch.setattr(backup_transcription, 'validate_collection', finish_review)
    with pytest.raises(InterruptedError):
        restore_backup(backup, tmp_path / 'never-published', checksum, cancelled=lambda: cancelled)
    assert cancelled and not (tmp_path / 'never-published').exists()


def test_asr_paths_and_sizes_are_narrowly_bounded():
    job, source, key = '1' * 32, '2' * 32, '3' * 64
    retained = f'state/transcription/jobs/{job}/inputs/{source}/source.wav'
    assert backup_sidecars.sidecar_limit(retained) == 32 * 1024**3
    cache = f'state/transcription/jobs/{job}/worker-history/{job}-{source}/logs/cache/verified-v1/33/{key}.cache.json'
    assert backup_sidecars.sidecar_limit(cache) == 32 * 1024**2
    for invalid in (retained.replace('.wav', '.exe'), retained.replace('/inputs/', '/../'),
                    cache.replace('/33/', '/33/deeper/'), 'state/operations/leases.json'):
        with pytest.raises(ValueError):
            backup_sidecars.sidecar_limit(invalid)


@pytest.mark.parametrize('worker', ['worker', 'worker-history/' + '1'*32 + '-' + '2'*32])
@pytest.mark.parametrize('suffix', ['cache.json', 'receipt.json', 'json', 'txt'])
@pytest.mark.parametrize('layout', ['', '33/', 'verified-v1/', 'verified-v1/33/'])
@pytest.mark.parametrize('shape', ['v3-implicit', 'v3-state', 'v4-sidecars', 'v5-state'])
def test_manifest_requires_explicit_optional_asr_cache_inclusion(worker, suffix, layout, shape):
    job, source, key = '1'*32, '2'*32, '3'*64
    root = f'state/transcription/jobs/{job}/'
    cache = root + f'{worker}/logs/cache/{layout}{key}.{suffix}'
    retained = root + f'inputs/{source}/source.wav'
    accepted = root + f'artifacts/{source}/{job}/transcript.txt'
    worker_result = root + f'{worker}/results/{source}/source.txt'
    names = [retained, accepted, worker_result, cache]
    legacy = shape in ('v3-state', 'v5-state')
    field = 'state' if legacy else 'sidecars'
    entries = [dict(filename=name[6:] if legacy else name,
                    size_bytes=0 if name == cache and suffix == 'txt' else 1,
                    sha256='4'*64) for name in names]
    version = {'v3-implicit': 3, 'v3-state': 3, 'v4-sidecars': 4, 'v5-state': 5}[shape]
    manifest = dict(version=version, kind='carr-library',
                    schema_version=5, schema_fingerprint='5'*64,
                    database={}, assets=[])
    manifest[field] = entries
    extra_names = set()
    if shape == 'v5-state':
        manifest['pending'] = None
        manifest['reading_state'] = dict(filename='reading-state.json',
                                         size_bytes=1, sha256='4'*64)
        extra_names.add('reading-state.json')
    if shape != 'v3-implicit':
        manifest['generated_audio_included'] = True
        inv = backup_sidecars.normalize_manifest(manifest)
        assert inv.generated_audio_included is True
        assert {row['filename'] for row in inv.entries} == set(names) | extra_names
        manifest['generated_audio_included'] = False
    with pytest.raises(ValueError, match='Transcription cache requires explicit manifest inclusion'):
        backup_sidecars.normalize_manifest(manifest)
    manifest[field] = entries[:-1]
    inv = backup_sidecars.normalize_manifest(manifest)
    assert inv.generated_audio_included is False
    assert {row['filename'] for row in inv.entries} == set(names[:-1]) | extra_names


def test_restore_rejects_cache_contradicting_manifest_inclusion(prepared, tmp_path):
    app, _, service, profile, _, _ = prepared
    audio = tmp_path / 'input.wav'
    audio.write_bytes(b'Synthetic retained input')
    job = service.create_job(profile['id'], [audio])
    assert service.run(job['id'])['status'] == 'complete'
    backup = tmp_path / 'backup'
    create_backup(app, backup, include_generated=True)
    manifest = read_json(backup / 'manifest.json')
    assert any(backup_transcription.is_cache_file(row['filename']) for row in state_files(manifest))
    manifest['generated_audio_included'] = False
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (backup / 'manifest.json').write_bytes(raw)
    destination = tmp_path / 'never-published'
    with pytest.raises(ValueError, match='Transcription cache requires explicit manifest inclusion'):
        restore_backup(backup, destination, sha256(raw).hexdigest())
    assert not destination.exists()
