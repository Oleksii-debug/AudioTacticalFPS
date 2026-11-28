"""One portable library carries reading, Studio, ASR and interrupted import state.

The subprocess is the existing deliberately synthetic ASR protocol fixture. It
tests receipt and derivative portability, not speech recognition quality. Studio
input retention is exercised here without synthesizing or rendering audio; the
separate Studio recovery suite performs the actual FFmpeg render roundtrip.
"""
from hashlib import sha256
import os
from pathlib import Path
import runpy
import shutil
import sys
import venv
from uuid import uuid4
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.durable_state import atomic_json, read_json
from carr_library import backup_sidecars
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import load_request, prepare_request
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.speech import VoiceRegistry
from carr_library.studio.backup_inputs import RECEIPT, review_project_inputs
from carr_library.studio.project import Cue, Project, ProjectStore, Scene
from carr_library.studio.render import ProductionService


def synthetic_engine(folder):
    """Reuse the admitted test protocol, never ship or execute a private worker."""
    fixture = Path(__file__).parents[1] / 'transcription/test_supplied_worker_bridge.py'
    source = runpy.run_path(str(fixture))['SYNTHETIC_WORKER']
    folder.mkdir()
    worker = folder / 'synthetic-protocol-worker.py'
    worker.write_text(source.replace('__MODE__', repr('complete')), encoding='utf-8')
    models = folder / 'synthetic-models'
    models.mkdir()
    # This supplied protocol uses only stdlib. Inspect its real selected peer,
    # without coupling repeated receipt checks to the application's packages.
    runtime = folder / 'worker-python'
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(runtime)
    python = runtime / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    return python.absolute(), worker, models


def remove_library_fixture(root):
    # Managed ASR snapshots are intentionally read-only on Windows. Simulate
    # loss of this owned fixture explicitly; do not weaken the production mode.
    for child in root.rglob('*'):
        if child.is_file():
            child.chmod(0o600)
    shutil.rmtree(root)


@pytest.mark.parametrize('include_generated', [False, True])
def test_reading_studio_transcription_and_pending_html_survive_two_restores(
        tmp_path, include_generated):
    app = LibraryApp(tmp_path / 'Початкова бібліотека')
    workspace = app.create_workspace('Combined private recovery')
    book = app.add_text(workspace, 'Original source', 'Перший розділ.\fSecond section.')
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    reading = ReadingState(app.root)
    reading.remember(page.location)
    bookmark = reading.add_bookmark(page.location, 'Продовжити читання')
    reading_raw = reading.path.read_bytes()

    selected = tmp_path / 'Вибрані зовнішні файли'
    selected.mkdir()
    audio = selected / 'cue.wav'
    with wave.open(str(audio), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\x00\x00' * 6000)
    audio_raw = audio.read_bytes()
    audio_hash = sha256(audio_raw).hexdigest()
    studio = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]),
                               input_roots=(selected,))
    project = Project('combined-project', 'Preserved local cue', (),
        (Scene('scene-1', 'Scene', (), cues=(Cue('cue-1', str(audio)),),
               minimum_duration=.5),),
        original_script='Original script: жодне слово не змінювати.')
    output_parent = tmp_path / 'Previously selected outputs'
    output_parent.mkdir()
    studio_job = studio.submit(project, output_parent, operation_id='combined-pending-job')
    # Use the exact completed PCM format produced by Studio's cache node.
    # Generated inclusion never admits an arbitrary file without its receipt.
    cache_key = sha256(b'combined recovery synthetic PCM node').hexdigest()
    cache_name = f'studio/cache/{cache_key}-{audio_hash[:10]}.wav'
    cache_receipt = f'studio/cache/{cache_key}.json'
    (app.root / 'state' / cache_name).write_bytes(audio_raw)
    atomic_json(app.root / 'state' / cache_receipt, {
        'schema': 1, 'key': cache_key, 'file': Path(cache_name).name,
        'sha256': audio_hash, 'duration': .25, 'sample_rate': 24000, 'channels': 1})
    cache_receipt_raw = (app.root / 'state' / cache_receipt).read_bytes()

    # The same selected audio is independently retained by ASR. Moving the
    # library must not grant either service authority over an old output path.
    asr = app.transcription(workspace)
    engine_root = tmp_path / 'Explicit synthetic engine'
    asr.configure(*synthetic_engine(engine_root))
    asr_job = asr.submit([audio])
    complete = asr.run(asr_job)
    assert complete['status'] == 'completed'
    source_name = complete['sources'][0]['filename']
    original_text = asr.text(asr_job, source_name)
    edited_text = 'Окрема редакція; оригінальний результат залишається незмінним.'
    revision = asr.save_edit(asr_job, source_name, edited_text)
    original_receipts = {name: (asr._path(asr_job) / name).read_bytes()
                         for name in complete['receipts']}
    relative_input = (asr._path(asr_job) / 'input' / source_name).relative_to(app.root / 'state')

    # Interrupt after retaining this new-format original, then remove the
    # selected external file before backup. Its bytes exist only in the library.
    html = selected / 'pending.html'
    html_raw = '<!doctype html><meta charset="utf-8"><p>Recoverymarker — первинний текст.</p>'.encode()
    html.write_bytes(html_raw)
    html_hash = sha256(html_raw).hexdigest()
    metadata = dict(title='Pending HTML', authors=[], language=None, edition=None,
        volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(html.parent, html.name).manifest()
    request = prepare_request(app.root, workspace, html, metadata, Config(), expected_manifest=planned)
    retained_before_cancel = []
    def stop_after_retained_html():
        admitted = load_request(app.root, request['request_id'])
        if not admitted.get('asset_sha256'):
            return False
        assert admitted['asset_sha256'] == html_hash
        assert app.assets.resolve(html_hash).read_bytes() == html_raw
        retained_before_cancel.append(admitted['asset_sha256'])
        return True

    with pytest.raises(InterruptedError):
        app._import_file(workspace, html, **metadata, _request_id=request['request_id'],
            _expected_manifest=planned, cancelled=stop_after_retained_html)
    assert retained_before_cancel and set(retained_before_cancel) == {html_hash}
    assert app.assets.resolve(html_hash).read_bytes() == html_raw
    html.unlink()

    backup = tmp_path / 'combined-backup'
    digest = create_backup(app, backup, include_generated=include_generated)
    manifest = read_json(backup / 'manifest.json')
    # Canonical full-backup v4 unifies all typed producers in sidecars;
    # legacy v5's separate state/reading_state fields are read-only adapters.
    assert manifest['version'] == 4
    assert set(manifest) == {'version', 'kind', 'schema_version', 'schema_fingerprint',
                            'database', 'assets', 'sidecars', 'generated_audio_included', 'pending'}
    reading_entry, = [entry for entry in manifest['sidecars']
                      if entry['filename'] == 'reading-state.json']
    assert reading_entry == {'filename': 'reading-state.json', 'size_bytes': len(reading_raw),
                             'sha256': sha256(reading_raw).hexdigest()}
    assert (backup / reading_entry['filename']).read_bytes() == reading_raw
    assert manifest['generated_audio_included'] is include_generated
    pending, = [item for item in manifest['pending']['assets'] if item['sha256'] == html_hash]
    assert pending['filename'] == html_hash + '.html'
    assert (backup / 'pending/originals' / pending['filename']).read_bytes() == html_raw
    names = {item['filename'] for item in manifest['sidecars']}
    assert 'state/' + RECEIPT in names
    assert f'state/studio/inputs/{audio_hash}.audio' in names
    assert 'state/' + relative_input.as_posix() in names
    assert ('state/' + cache_name in names) is include_generated
    assert ('state/' + cache_receipt in names) is include_generated
    if include_generated:
        assert (backup / 'state' / cache_name).read_bytes() == audio_raw
        assert (backup / 'state' / cache_receipt).read_bytes() == cache_receipt_raw
    assert not any('.pending-' in name or name.endswith('.py') for name in names)

    original_root = app.root
    remove_library_fixture(original_root)
    shutil.rmtree(selected)
    shutil.rmtree(output_parent)
    shutil.rmtree(engine_root)

    def check_recovered(root, number):
        restored = LibraryApp(root)
        saved = ReadingState(root)
        assert saved.path.read_bytes() == reading_raw
        assert saved.resume(workspace, book['edition']) == page.location
        assert saved.bookmarks(workspace, book['edition']) == (bookmark,)
        assert ReadingDesk(restored, workspace, book['edition']).validate(page.location) == page

        recovered_studio = ProductionService(root / 'state/studio', voices=VoiceRegistry([]))
        copied_project = recovered_studio.projects.load(project.id)
        retained_audio = recovered_studio.inputs / (audio_hash + '.audio')
        assert Path(copied_project.scenes[0].cues[0].path) == retained_audio
        assert retained_audio.read_bytes() == audio_raw
        assert retained_audio.stat().st_nlink == 1
        assert copied_project.original_script == project.original_script
        assert copied_project.fingerprint != project.fingerprint
        job = recovered_studio.get(studio_job)
        assert job['status'] == 'waiting-input' and job['destination'] == ''
        assert job['recovery']['requires_output_directory'] is True
        with pytest.raises(ValueError, match='new output directory'):
            recovered_studio.run(studio_job)
        assert not (root / 'state' / RECEIPT).exists()
        assert (root / 'state' / cache_name).exists() is include_generated
        assert (root / 'state' / cache_receipt).exists() is include_generated
        if include_generated:
            assert (root / 'state' / cache_name).read_bytes() == audio_raw
            assert (root / 'state' / cache_receipt).read_bytes() == cache_receipt_raw

        recovered_asr = restored.transcription(workspace)
        with pytest.raises((ValueError, OSError)):
            recovered_asr.configuration(verify=True)
        copied_asr = recovered_asr.verify(asr_job)
        assert copied_asr['status'] == 'completed'
        assert copied_asr['receipts'] == complete['receipts']
        assert {name: (recovered_asr._path(asr_job) / name).read_bytes()
                for name in copied_asr['receipts']} == original_receipts
        assert recovered_asr.text(asr_job, source_name) == original_text
        copied_input = root / 'state' / relative_input
        assert copied_input.read_bytes() == audio_raw
        assert copied_input != retained_audio
        exported = recovered_asr.export(asr_job, tmp_path / f'recovered-derivative-{number}')
        assert (exported / 'edited' / (revision + '.txt')).read_text(encoding='utf-8') == edited_text
        assert not original_root.exists() and not selected.exists() and not output_parent.exists()
        return restored

    destination = restore_backup(backup, tmp_path / 'Відновлена бібліотека', digest)
    restored = check_recovered(destination, 1)
    copied_request = load_request(restored.root, request['request_id'])
    assert copied_request['data_dir'] == str(restored.root)
    assert copied_request['source'] == str(restored.assets.resolve(html_hash))
    assert copied_request['metadata'] == metadata and copied_request['expected_manifest'] == planned
    resumed = restored.resume_import(workspace, request['request_id'])
    assert resumed['state'] == 'complete', resumed
    assert resumed['edition'] == metadata['edition_id'] and resumed['verified'] is False
    assert resumed['request_id'] == request['request_id']
    assert restored.search(workspace, 'Recoverymarker')

    # Rebackup must regenerate the typed Studio receipt without looking for
    # generated-only entries in the live source inventory or reopening old cues.
    second_backup = tmp_path / 'backup-after-recovery'
    second_digest = create_backup(restored, second_backup, include_generated=include_generated)
    second_manifest = read_json(second_backup / 'manifest.json')
    assert second_manifest['version'] == 4
    assert second_manifest['generated_audio_included'] is include_generated
    assert reading_entry in second_manifest['sidecars']
    assert any(entry['filename'] == 'state/' + RECEIPT for entry in second_manifest['sidecars'])
    remove_library_fixture(destination)
    second_destination = restore_backup(second_backup, tmp_path / 'Друга відновлена бібліотека', second_digest)
    second = check_recovered(second_destination, 2)
    assert second.assets.resolve(html_hash).read_bytes() == html_raw
    hit, = second.search(workspace, 'Recoverymarker')
    assert hit.verified is False
    assert not destination.exists()


def test_cancel_source_revalidation_after_studio_input_snapshot_never_publishes(tmp_path, monkeypatch):
    """PR141's second-read cancellation also applies after typed augmentation."""
    app = LibraryApp(tmp_path / 'library')
    audio = tmp_path / 'selected.wav'
    with wave.open(str(audio), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\x00\x00' * 240)
    original_audio = audio.read_bytes()
    project = Project('cancel-project', 'Cancellation keeps source files', (),
        (Scene('scene-1', 'Scene', (), cues=(Cue('cue-1', str(audio)),)),))
    store = ProjectStore(app.root / 'state/studio/projects')
    store.save(project)
    review_project_inputs(app.root / 'state/studio', project, input_roots=(audio.parent,))
    source = store.directory(project.id) / 'project.json'
    original_project = source.read_bytes()
    source_reads = 0
    receipt_seen = False
    original_open = os.open

    def observe_source_read(path, flags, *args, **kwargs):
        nonlocal source_reads
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == source and (flags & (os.O_WRONLY | os.O_RDWR)) == os.O_RDONLY:
            source_reads += 1
        return descriptor

    def cancel_revalidation():
        nonlocal receipt_seen
        if source_reads >= 2:
            stages = list(tmp_path.glob('.carr-library-backup-*'))
            receipts = [stage / 'state' / RECEIPT for stage in stages
                        if (stage / 'state' / RECEIPT).is_file()]
            receipt_seen = len(receipts) == 1
            assert receipt_seen
            receipt = read_json(receipts[0])
            project_record, = receipt['projects']
            assert project_record['source_sha256'] == sha256(original_project).hexdigest()
            retained, = receipt['inputs'].values()
            assert (receipts[0].parents[1] / retained['filename']).read_bytes() == original_audio
            return True
        return False

    # The real sidecar reader opens a bounded descriptor with os.open/fdopen.
    # The second source open is its post-augmentation byte revalidation.
    monkeypatch.setattr(backup_sidecars.os, 'open', observe_source_read)
    destination = tmp_path / 'backup'
    with pytest.raises(InterruptedError, match='cancelled'):
        create_backup(app, destination, cancelled=cancel_revalidation)
    assert source_reads == 2 and receipt_seen
    assert not destination.exists()
    assert not list(tmp_path.glob('.carr-library-backup-*'))
    assert source.read_bytes() == original_project and audio.read_bytes() == original_audio


@pytest.mark.parametrize('damage', ['untyped-name', 'missing-receipt', 'wrong-receipt-hash'])
def test_generated_cache_requires_exact_completed_receipt(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    cache = app.root / 'state/studio/cache'
    cache.mkdir(parents=True)
    key = sha256(b'synthetic optional cache control').hexdigest()
    audio = cache / (key + '-0123456789.wav')
    with wave.open(str(audio), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\x00\x00' * 240)
    original = audio.read_bytes()
    if damage == 'untyped-name':
        audio = audio.rename(cache / 'optional-generated.wav')
    elif damage == 'wrong-receipt-hash':
        atomic_json(cache / (key + '.json'), {'schema': 1, 'key': key, 'file': audio.name,
            'sha256': '0' * 64, 'duration': .01, 'sample_rate': 24000, 'channels': 1})
    target = tmp_path / 'rejected-generated'
    with pytest.raises(ValueError, match='validated backup adapter|completed producer receipt|retained bytes disagree'):
        create_backup(app, target, include_generated=True)
    assert not target.exists()
    assert audio.read_bytes() == original
