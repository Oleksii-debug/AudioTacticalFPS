"""Actual completed Book PCM partitions; synthetic tones prove bytes, not voice quality."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace
import wave
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioBudget, AudioCancelled
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import restore_backup
from carr_library.operations import ResourceRequest, ResourceCancelled
from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
from carr_library.studio import measured_audio, publishing
from carr_library.studio.publishing import PublicationService
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('measured_book_fixtures', Path(__file__).with_name('test_book_audio.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
book = _fixtures.book


@pytest.fixture
def rendered(book):
    e = book
    plan = _fixtures.plan(e, 'document')
    _, ready = _fixtures.prepared(e, plan)
    result = e.service.run(plan.id, e.output, approval=ready.fingerprint)
    assert result[0]['status'] == 'complete'
    state = e.production.get(plan.job_id(1))
    return SimpleNamespace(e=e, plan=plan, state=state,
        output=e.output.parent / 'measured-parts.zip', publisher=PublicationService(e.production))


def review(r, milliseconds=70, **options):
    return r.e.service.review_duration(r.plan.id, 1, r.output, maximum_ms=milliseconds, **options)


def publish(r, reviewed, milliseconds=70, **options):
    return r.e.service.publish_duration(r.plan.id, 1, r.output, maximum_ms=milliseconds,
                                       approval=reviewed['approval_sha256'], **options)


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        return reader.getparams(), reader.readframes(reader.getnframes())


def test_actual_pcm_parts_rejoin_exact_master_and_keep_approximate_whole_line_scope(rendered):
    r = rendered
    calls = tuple(r.e.voices.received)
    before = {row['path']: (sha256(Path(row['path']).read_bytes()).hexdigest(), Path(row['path']).stat().st_mtime_ns)
              for row in r.state['artifacts']}
    candidate = review(r)
    assert not r.output.exists() and not r.publisher.publications()
    assert candidate['content']['maximum_ms'] == 70
    assert len(candidate['content']['parts']) >= 3
    assert all(part['source_lines'] and all(row['scope'] == 'whole-line' and row['partial_line']
               for row in part['source_lines']) for part in candidate['content']['parts'])
    receipt = publish(r, candidate)
    assert receipt['format'] == 'pcm-parts' and receipt['status'] == 'complete'
    manifest = measured_audio.inspect(r.output)
    assert manifest['source'] == receipt['source']
    original_params, original_pcm = pcm(Path(r.state['destination']) / 'master.wav')
    actual, cursor = [], 0
    with ZipFile(r.output) as archive:
        assert json.loads(archive.read('source/book-source.json')) == r.plan.provenance(1)
        assert json.loads(archive.read('source/project.json')) == r.plan.project(1).to_dict()
        for part in manifest['content']['parts']:
            assert part['start_frame'] == cursor
            with wave.open(BytesIO(archive.read(part['name'])), 'rb') as audio:
                assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
                    original_params.framerate, original_params.nchannels, original_params.sampwidth)
                assert audio.getnframes() == part['frames']
                actual.append(audio.readframes(audio.getnframes()))
            cursor = part['end_frame']
    assert b''.join(actual) == original_pcm and cursor == original_params.nframes
    assert tuple(r.e.voices.received) == calls
    assert r.e.production.get(r.plan.job_id(1)) == r.state
    assert before == {path: (sha256(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns) for path in before}
    assert r.publisher.verify(receipt['id']) == receipt


def test_whole_measured_line_is_not_labelled_as_a_character_alignment(rendered):
    candidate = review(rendered, 1000)
    assert len(candidate['content']['parts']) == 1
    line, = candidate['content']['parts'][0]['source_lines']
    assert line['partial_line'] is False and line['scope'] == 'whole-line'
    assert 'no character or word alignment' in line['alignment']
    assert set(line) == {'scene', 'line', 'source_ref', 'scope', 'partial_line', 'alignment'}


@pytest.mark.parametrize('change', ['length', 'name', 'output', 'bad-hash'])
def test_changed_review_cannot_publish_or_resynthesize(rendered, change):
    r = rendered
    candidate = review(r)
    calls = tuple(r.e.voices.received)
    options = {}
    ms = 70
    if change == 'length':
        ms = 50
    elif change == 'name':
        options['name_template'] = '{index:04d} - Different reviewed name'
    elif change == 'output':
        r.output = r.output.with_name('different.zip')
    else:
        candidate['approval_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Review|review'):
        publish(r, candidate, ms, **options)
    assert not r.output.exists() and not r.publisher.publications()
    assert tuple(r.e.voices.received) == calls


@pytest.mark.parametrize('phase', ['Packaging exact', 'Prepared publication receipt'])
def test_source_change_at_actual_publication_boundary_keeps_original_job_complete(rendered, phase):
    r = rendered
    candidate = review(r)
    changed = []
    def progress(message):
        if message.startswith(phase) and not changed:
            changed.append(True)
            r.e.app.update_metadata(r.e.workspace, r.e.imported['edition'], title='Changed after duration review')
    with pytest.raises((ValueError, RuntimeError), match='revision|source|Source'):
        publish(r, candidate, progress=progress)
    assert changed and not r.output.exists()
    assert r.e.production.get(r.plan.job_id(1)) == r.state
    assert not list(r.output.parent.glob('.pending-measured*'))


def test_changed_master_after_prepare_notice_refuses_final_publication(rendered):
    r = rendered
    candidate = review(r)
    changed = []
    def progress(message):
        if message.startswith('Prepared publication receipt') and not changed:
            changed.append(True)
            master = Path(r.state['destination']) / 'master.wav'
            data = bytearray(master.read_bytes())
            data[-4] ^= 1
            master.write_bytes(data)
    with pytest.raises(RuntimeError, match='checksum'):
        publish(r, candidate, progress=progress)
    assert changed and not r.output.exists()
    assert r.e.production.get(r.plan.job_id(1)) == r.state


def test_actual_cancellation_during_pcm_partition_cleans_stage_and_retains_completed_job(rendered, monkeypatch):
    r = rendered
    candidate = review(r)
    entered, release = threading.Event(), threading.Event()
    original_package, original_frames = measured_audio.package, measured_audio._frames
    in_package = []
    def package(*args, **kwargs):
        in_package.append(True)
        try:
            return original_package(*args, **kwargs)
        finally:
            in_package.clear()
    def frames(reader, count, check):
        for block in original_frames(reader, count, check):
            if in_package and not entered.is_set():
                entered.set()
                assert release.wait(10)
            yield block
    monkeypatch.setattr(measured_audio, 'package', package)
    monkeypatch.setattr(measured_audio, '_frames', frames)
    result = []
    def worker():
        try:
            result.append(publish(r, candidate))
        except Exception as exc:
            result.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(10)
        r.e.service.close()
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and len(result) == 1 and isinstance(result[0], (AudioCancelled, InterruptedError))
    assert not r.output.exists() and not r.publisher.publications()
    assert not list(r.output.parent.glob('.pending-measured*'))
    assert r.e.production.get(r.plan.job_id(1)) == r.state


def test_resource_wait_cancellation_does_not_publish_or_reopen_speech(rendered):
    r = rendered
    candidate = review(r)
    waiting, cancel = threading.Event(), threading.Event()
    result = []
    calls = tuple(r.e.voices.received)
    def worker():
        try:
            result.append(publish(r, candidate, cancelled=cancel.is_set,
                progress=lambda message: waiting.set() if message.startswith('Waiting for shared') else None))
        except Exception as exc:
            result.append(exc)
    with r.e.production.broker.reserve(ResourceRequest(cpu_slots=1), operation_id='test:held-publication-slot'):
        thread = threading.Thread(target=worker)
        thread.start()
        assert waiting.wait(10)
        cancel.set()
        thread.join(10)
    assert not thread.is_alive() and len(result) == 1
    assert isinstance(result[0], (ResourceCancelled, AudioCancelled, InterruptedError))
    assert not r.output.exists() and not r.publisher.publications()
    assert tuple(r.e.voices.received) == calls
    cancel.clear()
    assert publish(r, candidate)['status'] == 'complete'
    assert tuple(r.e.voices.received) == calls


def test_generic_publication_cannot_bypass_book_source_guard(rendered):
    r = rendered
    with pytest.raises(ValueError, match='original completed Book plan'):
        r.publisher.review_pcm_parts(r.state['id'], r.output, maximum_ms=70)
    with pytest.raises(ValueError, match='Review'):
        r.publisher.export_pcm_parts(r.state['id'], r.output, maximum_ms=70, review_sha256='0' * 64)
    assert not r.output.exists() and not r.publisher.publications()


@pytest.mark.parametrize('maximum', [0, -1, True, 1.5, 86_400_001])
def test_invalid_measured_limit_never_guesses_duration_or_starts_another_job(rendered, maximum):
    with pytest.raises(ValueError):
        review(rendered, maximum)
    assert not rendered.output.exists() and len(rendered.e.production.jobs()) == 1


def test_original_source_budget_and_total_staging_budget_remain_bounded(rendered):
    r = rendered
    candidate = review(r)
    budget = AudioBudget(max_output_bytes=4096, reserve_disk_bytes=0)
    with pytest.raises(ValueError, match='budget'):
        publish(r, candidate, budget=budget)
    assert not r.output.exists() and not r.publisher.publications()
    assert not list(r.output.parent.glob('.pending-measured*'))


@pytest.mark.parametrize('change', ['range', 'alignment-type', 'source-text', 'audio', 'extra-file'])
def test_archive_verifier_rejects_changed_ranges_source_pcm_and_unreviewed_files(rendered, change):
    r = rendered
    publish(r, review(r))
    with ZipFile(r.output) as archive:
        files = {info.filename: archive.read(info) for info in archive.infolist()}
    manifest = json.loads(files['manifest.json'])
    if change == 'range':
        manifest['content']['parts'][0]['end_frame'] += 1
    elif change == 'alignment-type':
        manifest['content']['parts'][0]['source_lines'][0]['partial_line'] = 1
    elif change == 'source-text':
        project = json.loads(files['source/project.json'])
        project['original_script'] += ' Another unreviewed sentence.'
        files['source/project.json'] = encode(project)
    elif change == 'audio':
        name = manifest['parts'][0]['name']
        raw = bytearray(files[name])
        raw[-4] ^= 1
        files[name] = bytes(raw)
    else:
        files['unreviewed.txt'] = b'Unreviewed attachment.'
    files['manifest.json'] = encode(manifest)
    changed = r.output.with_name('changed.zip')
    with ZipFile(changed, 'w', compression=ZIP_STORED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    with pytest.raises(ValueError):
        measured_audio.inspect(changed)


def test_existing_receipt_finalization_recovery_does_not_partition_again(rendered, monkeypatch):
    r = rendered
    candidate = review(r)
    original = publishing.atomic_json
    failed = []
    def write(path, value, **kwargs):
        if value.get('schema') == 'resonance-publication/1' and value.get('status') == 'complete' and not failed:
            failed.append(True)
            raise OSError('Synthetic final receipt publication failure')
        return original(path, value, **kwargs)
    monkeypatch.setattr(publishing, 'atomic_json', write)
    with pytest.raises(RuntimeError, match='was published'):
        publish(r, candidate)
    assert r.output.exists() and failed
    saved, = r.publisher.publications()
    assert saved['status'] == 'prepared'
    before = r.output.stat().st_mtime_ns
    verified = r.publisher.verify(saved['id'])
    assert verified['status'] == 'complete' and r.output.stat().st_mtime_ns == before


def test_measured_receipt_typed_backup_delete_restore_has_no_output_or_render_authority(rendered, tmp_path):
    r = rendered
    receipt = publish(r, review(r))
    before = (sha256(r.output.read_bytes()).hexdigest(), r.output.stat().st_mtime_ns)
    backup = tmp_path / 'backup'
    archive = r.e.app.backup(backup)
    r.e.service.close()
    r.e.production.close()
    shutil.rmtree(r.e.app.root)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', archive))
    production = ProductionService(restored.root / 'state' / 'studio', voices=r.e.voices)
    books = BookAudioService(restored, r.e.workspace, production=production)
    try:
        publisher = PublicationService(production)
        assert publisher.publications() == (receipt,)
        with pytest.raises(ValueError, match='explicitly select'):
            publisher.verify(receipt['id'])
        with pytest.raises(ValueError, match='Run and verify'):
            books.review_duration(r.plan.id, 1, tmp_path / 'unprepared.zip', maximum_ms=70)
        calls = tuple(r.e.voices.received)
        assert publisher.verify(receipt['id'], output=r.output)['status'] == 'complete'
        assert tuple(r.e.voices.received) == calls
        assert production.get(r.plan.job_id(1))['recovery']['requires_output_directory']
        assert (sha256(r.output.read_bytes()).hexdigest(), r.output.stat().st_mtime_ns) == before
    finally:
        books.close()
        production.close()
