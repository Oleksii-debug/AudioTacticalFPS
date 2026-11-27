"""Real inert archival/restore/new-preparation boundaries, without ASR inference."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from carr_library.application import LibraryApp
from carr_library import legacy_transcription as recovery
from carr_library import legacy_transcription_schema as schema
from carr_library import backup_historical
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.transcription_service import TranscriptionService
from carr_library.transcription_receipt import validate_settings
from carr_library.transcription_state import read_definition
from tests.storage.legacy_transcription_fixture import canonical_data, current_runtime, put, digest


def app_at(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Existing research')
    app.add_text(workspace, 'Source', 'Точний оригінал збережено.')
    return app, workspace


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def common_backup(app, target, fixture):
    create_backup(app, target)
    manifest_path = target / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.pop('pending', None) is not None:
        shutil.rmtree(target / 'pending')
    manifest.pop('sidecars', None)
    if (target / 'sidecars').exists():
        shutil.rmtree(target / 'sidecars')
    entries = []
    for path in sorted(fixture.namespace.rglob('*')):
        if path.is_file():
            name = path.relative_to(fixture.root).as_posix()
            raw = path.read_bytes()
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            entries.append(dict(filename=name, size_bytes=len(raw), sha256=digest(raw)))
    manifest.update(version=4, generated_audio_included=False, sidecars=entries)
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    manifest_path.write_bytes(raw)
    return digest(raw), manifest


@pytest.mark.parametrize('complete', [False, True], ids=['pending', 'complete'])
def test_live_canonical_sources_restore_only_as_inert_history_and_rebackup(tmp_path, monkeypatch, complete):
    app, workspace = app_at(tmp_path)
    saved = canonical_data(app.root, complete=complete)
    original = tree(saved.namespace)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert all(not row['path'].startswith('state/transcription/') for row in manifest['sidecars'])
    assert any(row['path'].startswith(recovery.DIRECTORY) for row in manifest['sidecars'])
    shutil.rmtree(saved.namespace)
    before = tree(backup)
    monkeypatch.setattr('filelock.FileLock', lambda *a, **k: (_ for _ in ()).throw(AssertionError('verify must not lock')))
    verify_backup(backup, checksum)
    assert tree(backup) == before
    monkeypatch.undo()
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    assert not (restored / 'state/transcription').exists()
    assert not (restored / 'transcription-jobs').exists()
    reopened = LibraryApp(restored)
    assert 'оригінал' in reopened.search(workspace, 'оригінал')[0].quote
    row, = recovery.jobs(restored)
    assert row['historical'] and row['recorded_status'] == saved.job['status']
    entries, resolve = recovery._archive_source(restored, row['reference'])
    assert {entry['filename'].removeprefix('state/transcription/'): resolve(entry['filename']).read_bytes()
            for entry in entries} == original
    if complete:
        assert recovery.read_transcript(restored, row['reference'], saved.source['id'])['text'] == saved.text
        assert recovery.read_transcript(restored, row['reference'], saved.source['id'], edited=True)['text'] == 'Збережена окрема правка.'
        exported = tmp_path / 'historical.txt'
        receipt = recovery.export_text(restored, row['reference'], saved.source['id'], exported)
        assert exported.read_text(encoding='utf-8') == saved.text
        assert receipt.path == exported
    again = tmp_path / 'again'
    digest2 = create_backup(reopened, again)
    verify_backup(again, digest2)


def test_common_v4_keeps_exact_old_manifest_and_all_results_without_old_runtime(tmp_path):
    app, _ = app_at(tmp_path)
    old = canonical_data(tmp_path / 'old-namespace', complete=True, empty=True)
    backup = tmp_path / 'common-v4'
    checksum, manifest = common_backup(app, backup, old)
    before = tree(backup)
    verify_backup(backup, checksum)
    assert tree(backup) == before
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    raw = (restored / backup_historical.DIRECTORY / checksum / 'manifest.json').read_bytes()
    assert raw == before['manifest.json']
    assert not (restored / 'state/transcription').exists()
    row, = recovery.jobs(restored)
    assert row['reference']['kind'] == 'historical'
    assert recovery.read_transcript(restored, row['reference'], old.source['id'])['text'] == ''
    assert recovery.reading_copy(restored, row['reference'], old.source['id'], edited=True).original_text == 'Збережена окрема правка.'
    rebackup = tmp_path / 'again'
    again = create_backup(LibraryApp(restored), rebackup)
    verify_backup(rebackup, again)


def test_explicit_old_subset_uses_actual_current_prepare_and_never_adopts_old_completion(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root, complete=True)
    backup = tmp_path / 'before'
    checksum = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', checksum)
    service = TranscriptionService(restored)
    row, = recovery.jobs(restored)
    manifest = current_runtime(tmp_path / 'reviewed-current-runtime')
    settings = validate_settings(dict(language='uk'))
    monkeypatch.setattr(service, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no launch')))
    result = recovery.import_as_new(service, row['reference'], [saved.source['id']], manifest, settings)
    assert result['warning'] is None and result['historical_source']
    assert result['id'] != saved.job['id']
    job = service._job(result['id'])
    definition = read_definition(job)
    assert (job / definition['inputs'][0]['path']).read_bytes() == saved.raw
    assert definition['settings'] == settings
    assert not (job / 'execution.json').exists()
    assert not (job / 'completion-v1.json').exists()
    assert not list(restored.glob('.legacy-transcription-*'))
    link = restored / recovery.DIRECTORY / 'links' / (result['id'] + '.json')
    assert json.loads(link.read_bytes())['reference'] == row['reference']
    new_backup = tmp_path / 'after'
    new_checksum = create_backup(LibraryApp(restored), new_backup)
    verify_backup(new_backup, new_checksum)
    relocated = restore_backup(new_backup, tmp_path / 'relocated-again', new_checksum)
    assert read_definition(relocated / 'transcription-jobs' / result['id']) == definition
    assert recovery.read_transcript(relocated, row['reference'], saved.source['id'])['text'] == saved.text


@pytest.mark.parametrize('mutation', ['source', 'profile', 'receipt', 'unlisted'], ids=['source', 'profile', 'receipt', 'unlisted'])
def test_live_tampering_or_unregistered_files_cannot_publish_a_complete_backup(tmp_path, mutation):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root, complete=True)
    if mutation == 'source':
        saved.source_path.write_bytes(b'x' * len(saved.raw))
    elif mutation == 'profile':
        value = json.loads(saved.profile_path.read_bytes())
        value['python_identity']['sha256'] = '0' * 64
        put(saved.profile_path, value)
    elif mutation == 'receipt':
        value = json.loads((saved.job_root / saved.job['results'][0]['receipt']).read_bytes())
        value['provenance']['citation_authority'] = True
        put(saved.job_root / saved.job['results'][0]['receipt'], value)
    else:
        (saved.namespace / 'unknown-secret.bin').write_bytes(b'Unknown files must not be silently ignored.')
    target = tmp_path / 'backup'
    with pytest.raises((ValueError, RuntimeError, OSError)):
        create_backup(app, target)
    assert not (target / 'manifest.json').exists()


def test_authenticated_archive_cannot_forge_the_source_receipt_binding(tmp_path):
    app, _ = app_at(tmp_path)
    saved = canonical_data(tmp_path / 'old', complete=True)
    saved.job['sources'][0]['sha256'] = '0' * 64
    from carr_library.legacy_transcription_values import fingerprint
    saved.job['plan_fingerprint'] = fingerprint({key:saved.job[key] for key in
        ('id','profile_id','profile_fingerprint','sources','settings')})
    put(saved.job_root / 'job.json', saved.job)
    checksum, _ = common_backup(app, tmp_path / 'forged', saved)
    with pytest.raises(ValueError, match='Missing or changed|provenance'):
        verify_backup(tmp_path / 'forged', checksum)
    with pytest.raises(ValueError):
        restore_backup(tmp_path / 'forged', tmp_path / 'refused', checksum)
    assert not (tmp_path / 'refused').exists()


def test_current_preparation_refuses_missing_runtime_before_publishing_new_job(tmp_path):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    row, = recovery.jobs(app.root)
    service = TranscriptionService(app.root)
    with pytest.raises((ValueError, OSError)):
        recovery.import_as_new(service, row['reference'], [saved.source['id']],
                               tmp_path / 'missing-runtime.json', validate_settings({}))
    assert service.jobs() == ()
    assert not list(app.root.glob('.legacy-transcription-*'))


def test_cancellation_during_original_materialization_removes_only_owned_stage(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    row, = recovery.jobs(app.root)
    real = recovery.publish_new
    cancelled = [False]
    def copied(source, destination, **options):
        result = real(source, destination, **options)
        if Path(destination).parent.name.startswith('.legacy-transcription-'):
            cancelled[0] = True
        return result
    monkeypatch.setattr(recovery, 'publish_new', copied)
    service = TranscriptionService(app.root)
    with pytest.raises((InterruptedError, RuntimeError)):
        recovery.import_as_new(service, row['reference'], [saved.source['id']],
            current_runtime(tmp_path / 'runtime'), validate_settings({}), cancelled=lambda: cancelled[0])
    assert service.jobs() == ()
    assert not list(app.root.glob('.legacy-transcription-*'))
    assert saved.source_path.read_bytes() == saved.raw


def test_late_stop_after_actual_prepare_returns_created_job_and_inert_link(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    row, = recovery.jobs(app.root)
    service = TranscriptionService(app.root)
    actual = service.prepare
    stop = [False]
    def committed(*args, **kwargs):
        result = actual(*args, **kwargs)
        stop[0] = True
        return result
    monkeypatch.setattr(service, 'prepare', committed)
    result = recovery.import_as_new(service, row['reference'], [saved.source['id']],
        current_runtime(tmp_path / 'runtime'), validate_settings({}), cancelled=lambda: stop[0])
    assert stop[0] and result['warning'] is None
    assert read_definition(service._job(result['id'])) == result['definition']


def test_post_archive_move_sync_failure_retries_the_matching_durable_directory(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    canonical_data(app.root)
    actual = recovery.publish_noreplace
    def moved(source, destination, **options):
        actual(source, destination, **options)
        raise OSError('Injected post-move directory flush failure.')
    monkeypatch.setattr(recovery, 'publish_noreplace', moved)
    with pytest.raises(OSError):
        recovery.retain_live(app.root)
    syncs = []
    monkeypatch.setattr(recovery, 'publish_noreplace', actual)
    sync = recovery.sync_directory
    monkeypatch.setattr(recovery, 'sync_directory', lambda path: (syncs.append(Path(path)), sync(path))[1])
    identity = recovery.retain_live(app.root)
    assert app.root / recovery.DIRECTORY / 'archives' in syncs
    assert recovery.jobs(app.root)[0]['reference']['archive'] == identity


def test_actual_other_process_canonical_lock_refuses_backup_without_copying(tmp_path):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    ready = tmp_path / 'ready'
    script = ("from filelock import FileLock;from pathlib import Path;import sys,time;"
              "lock=FileLock(sys.argv[1]);lock.acquire();Path(sys.argv[2]).write_bytes(b'ready');time.sleep(20)")
    child = subprocess.Popen([sys.executable, '-c', script, str(saved.job_root / '.run.lock'), str(ready)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not ready.exists():
            if child.poll() is not None:
                raise AssertionError(child.stderr.read(2048).decode(errors='replace'))
            assert time.monotonic() < deadline
            time.sleep(.01)
        with pytest.raises(RuntimeError, match='active'):
            create_backup(app, tmp_path / 'refused')
        assert not (tmp_path / 'refused').exists()
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
        child.stderr.close()
    create_backup(app, tmp_path / 'allowed')


@pytest.mark.parametrize('case', ['ordinary-large', 'too-large-source', 'too-large-total', 'empty-json', 'unknown-asr', 'too-deep'])
def test_transcription_extension_does_not_raise_other_historical_limits(tmp_path, case):
    app, _ = app_at(tmp_path)
    saved = canonical_data(tmp_path / 'old')
    _, manifest = common_backup(app, tmp_path / 'archive', saved)
    source = next(row for row in manifest['sidecars'] if schema.is_input(row['filename']))
    source['size_bytes'] = 512 * 1024**2 + 1
    backup_historical.validate_manifest(manifest)  # Typed large source admitted, without allocating it.
    if case == 'ordinary-large':
        source['filename'] = 'state/ordinary.json'
    elif case == 'too-large-source':
        source['size_bytes'] = schema.MAX_SOURCE_BYTES + 1
    elif case == 'too-large-total':
        source['size_bytes'] = schema.MAX_SOURCE_BYTES
        for i in range(2):
            manifest['sidecars'].append(dict(source, filename=source['filename'].replace('2' * 32, str(i + 7) * 32)))
    elif case == 'empty-json':
        source.update(filename='state/transcription/profiles/' + 'f' * 32 + '.json', size_bytes=0)
    elif case == 'unknown-asr':
        source['filename'] = 'state/transcription/private-settings.json'
    else:
        source['filename'] = 'state/transcription/jobs/' + '1' * 32 + '/' + 'x/' * 10 + 'log.txt'
    with pytest.raises(ValueError):
        backup_historical.validate_manifest(manifest)


def test_large_old_job_requires_an_explicit_current_size_subset_before_copy(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    row, = recovery.jobs(app.root)
    from carr_library import transcription_state
    monkeypatch.setattr(transcription_state, 'MAX_JOB_BYTES', len(saved.raw) - 1)
    monkeypatch.setattr(recovery, 'publish_new', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no copy')))
    with pytest.raises(ValueError, match='subset'):
        recovery.import_as_new(TranscriptionService(app.root), row['reference'], [saved.source['id']],
            tmp_path / 'not-opened', validate_settings({}))


def test_actual_prepare_commit_survives_a_following_definition_read_failure(tmp_path, monkeypatch):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    recovery.retain_live(app.root)
    row, = recovery.jobs(app.root)
    service = TranscriptionService(app.root)
    monkeypatch.setattr(service, 'definition', lambda *a, **k: (_ for _ in ()).throw(
        OSError('Injected read failure after actual prepared publication.')))
    result = recovery.import_as_new(service, row['reference'], [saved.source['id']],
        current_runtime(tmp_path / 'runtime'), validate_settings({}))
    assert result['definition'] is None and 'prepared' in result['warning']
    assert read_definition(service._job(result['id']))['id'] == result['id']
    assert not list(app.root.glob('.legacy-transcription-*'))
    assert not (service._job(result['id']) / 'execution.json').exists()


def test_known_interrupted_archive_staging_never_hides_verified_jobs(tmp_path):
    app, _ = app_at(tmp_path)
    canonical_data(app.root)
    recovery.retain_live(app.root)
    expected = recovery.jobs(app.root)
    base = app.root / recovery.DIRECTORY / 'archives'
    (base / '.archive-abcd1234').mkdir()
    assert recovery.jobs(app.root) == expected
    recovery.validate_state_tree(None, app.root)
    (base / 'unknown-directory').mkdir()
    with pytest.raises(ValueError, match='Unknown'):
        recovery.jobs(app.root)


def test_stale_running_label_is_retained_but_never_becomes_a_current_job(tmp_path):
    app, _ = app_at(tmp_path)
    saved = canonical_data(app.root)
    saved.job['status'] = 'running'
    put(saved.job_root / 'job.json', saved.job)
    target = tmp_path / 'backup'
    checksum = create_backup(app, target)
    restored = restore_backup(target, tmp_path / 'restored', checksum)
    row, = recovery.jobs(restored)
    assert row['historical'] and row['recorded_status'] == 'running'
    assert not (restored / 'state/transcription').exists()
    assert TranscriptionService(restored).jobs() == ()
