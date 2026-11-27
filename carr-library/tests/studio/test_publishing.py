"""Actual FFmpeg/Studio/EPUB acceptance using explicitly synthetic tone voices.

The fixtures prove measured audio packaging and source binding, not speech
quality, actual model qualification, EPUBCheck or third-party reader behavior.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
import subprocess
import sys
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import wave
from copy import deepcopy
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.audio import AudioRuntime
from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.durable_state import atomic_json, read_json
from carr_library.studio.project import Project, parse_script
from carr_library.studio.render import ProductionService
from carr_library.studio import publishing
from carr_library.studio.publishing import PublicationService, inspect_epub, validate_publication_receipt


SCRIPT = ('[Scene: Архів & letters]\r\n'
          'Narrator: Exact <original> & preserved words.\r\n'
          '[Pause: 0.2]\r\n'
          'Reader: Другий рядок «цитати».\r\n'
          '[Scene: Closing]\r\n'
          'Narrator: The final line.\r\n')


@pytest.fixture
def saved_receipt():
    return {'schema': 'resonance-publication/1', 'id': 'a' * 32, 'status': 'complete',
        'studio_root': 'C:\\Research\\state\\studio', 'output': 'D:\\Publications\\book.epub',
        'artifact': {'size': 1234, 'sha256': 'b' * 64},
        'source': {'job_id': 'fixture-job', 'job_sha256': 'c' * 64,
            'project_id': 'fixture-project', 'project_sha256': 'd' * 64,
            'source_script_sha256': 'e' * 64,
            'artifacts': [{'name': name, 'size': 100, 'sha256': 'f' * 64}
                          for name in ('master.wav', 'project.json', 'timeline.json')]},
        'created': '2026-11-27T12:00:00Z', 'notice': publishing.NOTICE}


def test_saved_receipt_validator_is_pure_and_keeps_foreign_paths_as_declarations(saved_receipt, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Pure receipt validation must not inspect any external path.')
    monkeypatch.setattr(publishing, 'local_path', forbidden)
    monkeypatch.setattr(publishing, 'regular', forbidden)
    before = deepcopy(saved_receipt)
    assert validate_publication_receipt(saved_receipt, publication_id='a' * 32) is saved_receipt
    assert saved_receipt == before
    saved_receipt['status'] = 'prepared'
    saved_receipt['studio_root'] = '/relocated/library/state/studio'
    saved_receipt['output'] = '/separately-selected/book.epub'
    assert validate_publication_receipt(saved_receipt) is saved_receipt


@pytest.mark.parametrize('damage', ['extra-field', 'boolean-size', 'bad-digest', 'relative-output',
    'remote-output', 'duplicate-source', 'escape-source', 'dot-source', 'missing-original', 'invalid-date', 'wrong-id'])
def test_saved_receipt_rejects_ambiguous_typed_metadata_without_mutation(saved_receipt, damage):
    if damage == 'extra-field':
        saved_receipt['approved'] = True
    elif damage == 'boolean-size':
        saved_receipt['artifact']['size'] = True
    elif damage == 'bad-digest':
        saved_receipt['source']['project_sha256'] = 'g' * 64
    elif damage == 'relative-output':
        saved_receipt['output'] = 'book.epub'
    elif damage == 'remote-output':
        saved_receipt['output'] = '\\\\server\\share\\book.epub'
    elif damage == 'duplicate-source':
        saved_receipt['source']['artifacts'].append(
            {'name': 'MASTER.WAV', 'size': 100, 'sha256': 'f' * 64})
    elif damage == 'escape-source':
        saved_receipt['source']['artifacts'][0]['name'] = '../master.wav'
    elif damage == 'dot-source':
        saved_receipt['source']['artifacts'].append({'name': '.', 'size': 1, 'sha256': 'f' * 64})
    elif damage == 'missing-original':
        saved_receipt['source']['artifacts'][0]['name'] = 'unrelated.wav'
    elif damage == 'invalid-date':
        saved_receipt['created'] = '2026-02-30T12:00:00Z'
    else:
        saved_receipt['id'] = 'b' * 32
    before = deepcopy(saved_receipt)
    with pytest.raises(ValueError):
        validate_publication_receipt(saved_receipt, publication_id='a' * 32)
    assert saved_receipt == before


class ToneVoices:
    def voices(self):
        return (SimpleNamespace(id='synthetic-tone', name='Synthetic protocol tone',
                                language='und', engine='test fixture'),)

    def synthesize(self, text, voice_id, destination, *, settings, cancelled):
        assert voice_id == 'synthetic-tone'
        if cancelled():
            raise AudioCancelled('Synthetic tone production cancelled.')
        with wave.open(str(destination), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * index / 24000)))
                                       for index in range(4800)))


@pytest.fixture(scope='module')
def actual_job(tmp_path_factory):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the reviewed test FFmpeg/ffprobe runtime.'
    root = tmp_path_factory.mktemp('actual-epub-production')
    output = root / 'rendered'; output.mkdir()
    service = ProductionService(root / 'library' / 'state' / 'studio', voices=ToneVoices())
    project = service.create(SCRIPT, title='Measured publication — джерело', voice_ids=('synthetic-tone',))
    identity = service.submit(project, output, stems=False)
    complete = service.run(identity)
    assert complete['status'] == 'complete'
    return service, complete


@pytest.fixture
def source_job(tmp_path, actual_job):
    original, original_state = actual_job
    private = tmp_path / 'library' / 'state' / 'studio'
    shutil.copytree(original.root, private)
    destination = tmp_path / 'rendered'
    shutil.copytree(original_state['destination'], destination)
    service = ProductionService(private, voices=ToneVoices())
    state = service.get(original_state['id'])
    old = Path(state['destination'])
    state['destination'] = str(destination)
    for artifact in state['artifacts']:
        artifact['path'] = str(destination / Path(artifact['path']).relative_to(old))
    atomic_json(service.jobs_root / state['id'] / 'job.json', state, replace=True)
    return service, state


def checkpoint(service, state):
    atomic_json(service.jobs_root / state['id'] / 'job.json', state, replace=True)


def altered_timeline(service, state, mutate):
    path = Path(state['destination']) / 'timeline.json'
    value = read_json(path); mutate(value)
    atomic_json(path, value, replace=True)
    receipt = next(row for row in state['artifacts'] if Path(row['path']) == path)
    receipt.update(size=path.stat().st_size, sha256=sha256(path.read_bytes()).hexdigest())
    state['result'] = value
    checkpoint(service, state)


def test_actual_ffmpeg_completed_job_to_verified_epub(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'synchronized.epub'
    original = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in state['artifacts']}
    messages = []
    result = PublicationService(service).export_epub(state['id'], output, language='uk', creator='Synthetic acceptance',
                                                     progress=messages.append)
    assert result['status'] == 'complete' and result['artifact']['sha256'] == sha256(output.read_bytes()).hexdigest()
    assert original == {name: sha256(Path(name).read_bytes()).hexdigest() for name in original}
    assert service.get(state['id']) == state
    provenance = inspect_epub(output)
    assert provenance['original_script'] == SCRIPT and provenance['source']['project_sha256'] == state['fingerprint']
    assert provenance['audio']['codec'] == 'mp3' and not provenance['audio']['reused']
    assert provenance['validation']['epubcheck'] == 'NOT_RUN'
    assert 'not word alignment' in provenance['timing_notice']
    with ZipFile(output) as archive:
        first = archive.infolist()[0]
        assert first.filename == 'mimetype' and first.compress_type == ZIP_STORED and first.extra == b''
        assert archive.read('mimetype') == b'application/epub+zip'
        package = ET.fromstring(archive.read('EPUB/package.opf'))
        ns = {'p':publishing.OPF, 'd':publishing.DC, 'h':publishing.XHTML, 's':publishing.SMIL}
        assert package.get('version') == '3.0'
        assert package.find('p:metadata/d:language', ns).text == 'uk'
        assert package.find('p:metadata/d:creator', ns).text == 'Synthetic acceptance'
        assert package.find('p:manifest/p:item[@id="audio"]', ns).get('media-type') == 'audio/mpeg'
        content = ET.fromstring(archive.read('EPUB/content.xhtml'))
        spans = {node.get('id'): node.text for node in content.findall('.//h:span[@id]', ns)}
        actual_lines = [row for row in provenance['intervals'] if row['kind'] == 'line']
        assert [spans[row['id']] for row in actual_lines] == [row['text'] for row in state['result']['lines']]
        roles = {role['id']: role['name'] for role in state['project']['roles']}
        assert [node.text for node in content.findall('.//h:span[@class="role"]', ns)] == [
            roles[row['role']] + ':' for row in state['result']['lines']]
        assert [row['source_ref'] for row in actual_lines] == [row['source_ref'] for row in state['result']['lines']]
        assert len(provenance['intervals']) > len(actual_lines)  # Deliberate pauses remain audible.
        smil = ET.fromstring(archive.read('EPUB/overlay.smil'))
        intervals = smil.findall('.//s:audio', ns)
        assert len(intervals) == len(provenance['intervals'])
        assert all(float(node.get('clipEnd')[:-1]) <= provenance['audio']['duration'] for node in intervals)
        assert not content.findall('.//h:script', ns) and not content.findall('.//h:img', ns)
        decoded = tmp_path / 'packaged.mp3'; decoded.write_bytes(archive.read('EPUB/audio/master.mp3'))
    runtime = AudioRuntime(tmp_path, input_roots=(tmp_path,))
    actual = runtime.inspect(decoded)
    assert actual.codec == 'mp3' and abs(actual.duration - state['result']['duration']) < .15
    assert PublicationService(service).verify(result['id']) == result
    assert any('Rechecking source receipts' in message for message in messages)
    assert not list(tmp_path.glob('.pending-epub-*'))


@pytest.mark.parametrize('phase,error', [
    ('Verifying source artifact', RuntimeError),
    ('EPUB saved and its exact bytes verified', RuntimeError),
    ('EPUB saved and its exact bytes verified', AudioCancelled),
])
def test_observer_failure_cannot_replace_verified_epub_success(source_job, tmp_path, phase, error):
    service, state = source_job
    publisher = PublicationService(service)
    output = tmp_path / 'completed-despite-observer.epub'
    observed = []
    def progress(message):
        if message.startswith(phase):
            observed.append(message)
            raise error('Synthetic notification failure; no cancellation request')
    result = publisher.export_epub(state['id'], output, progress=progress)
    assert observed and result['status'] == 'complete'
    assert result['artifact']['sha256'] == sha256(output.read_bytes()).hexdigest()
    assert read_json(publisher.receipts / (result['id'] + '.json')) == result
    assert inspect_epub(output)['source']['project_sha256'] == state['fingerprint']
    assert publisher.verify(result['id']) == result
    assert service.get(state['id']) == state and not list(tmp_path.glob('.pending-epub-*'))


def test_explicit_cancel_still_stops_after_a_failed_observer(source_job, tmp_path):
    service, state = source_job
    stopping = False
    def progress(message):
        nonlocal stopping
        if message.startswith('Rechecking source receipts'):
            stopping = True
            raise RuntimeError('Synthetic broken notification')
    with pytest.raises(AudioCancelled, match='cancelled'):
        PublicationService(service).export_epub(state['id'], tmp_path / 'cancelled.epub',
            progress=progress, cancelled=lambda: stopping)
    assert not (tmp_path / 'cancelled.epub').exists()
    assert not list(tmp_path.glob('.pending-epub-*')) and service.get(state['id']) == state


def test_existing_verified_job_mp3_is_reused_byte_for_byte(source_job, tmp_path, monkeypatch):
    service, state = source_job
    destination = tmp_path / 'actual-mp3-production'; destination.mkdir()
    identity = service.submit(Project.from_dict(state['project']), destination, format='mp3', stems=False)
    state = service.run(identity)
    assert state['status'] == 'complete' and state['format'] == 'mp3'
    target = Path(state['destination']) / 'audiobook.mp3'
    def forbidden(*args, **kwargs):
        raise AssertionError('An existing verified MP3 must not be encoded again.')
    monkeypatch.setattr(AudioRuntime, 'convert', forbidden)
    output = tmp_path / 'reused.epub'
    PublicationService(service).export_epub(state['id'], output)
    with ZipFile(output) as archive:
        assert archive.read('EPUB/audio/master.mp3') == target.read_bytes()
    assert inspect_epub(output)['audio']['reused'] is True


@pytest.mark.parametrize('mutate', [
    lambda value: value['lines'][0].update(text='Changed transcript'),
    lambda value: value['lines'][0].update(start=-1),
    lambda value: value['lines'][0].update(end=10000),
    lambda value: value['lines'][1].update(start=0),
    lambda value: value['lines'].reverse(),
    lambda value: value['chapters'][0].update(title='Different source scene'),
    lambda value: value['chapters'][1].update(start=0),
])
def test_even_receipted_timeline_changes_cannot_rebind_source_semantics(source_job, tmp_path, mutate):
    service, state = source_job
    altered_timeline(service, state, mutate)
    output = tmp_path / 'rejected.epub'
    with pytest.raises(ValueError):
        PublicationService(service).export_epub(state['id'], output)
    assert not output.exists() and not list(tmp_path.glob('.pending-epub-*'))


@pytest.mark.parametrize('change', ['missing','duplicate','outside','changed'])
def test_completed_artifact_graph_must_be_exact(source_job, tmp_path, change):
    service, state = source_job
    artifact = state['artifacts'][0]
    if change == 'missing':
        Path(artifact['path']).unlink()
    elif change == 'duplicate':
        state['artifacts'].append(dict(artifact)); checkpoint(service, state)
    elif change == 'outside':
        outside = tmp_path / 'outside.wav'; outside.write_bytes(Path(artifact['path']).read_bytes())
        artifact['path'] = str(outside); checkpoint(service, state)
    else:
        Path(artifact['path']).write_bytes(b'Changed source bytes')
    with pytest.raises((ValueError, OSError)):
        PublicationService(service).export_epub(state['id'], tmp_path / 'invalid.epub')
    assert not (tmp_path / 'invalid.epub').exists()


def test_output_no_clobber_and_partial_job_fail_before_audio_runtime(source_job, tmp_path, monkeypatch):
    service, state = source_job
    import carr_library.runtime_settings as settings
    monkeypatch.setattr(settings, 'audio_runtime', lambda *a, **k: (_ for _ in ()).throw(AssertionError('runtime must remain idle')))
    output = tmp_path / 'existing.epub'; output.write_bytes(b'Keep this original')
    with pytest.raises(FileExistsError):
        PublicationService(service).export_epub(state['id'], output)
    assert output.read_bytes() == b'Keep this original'
    state['status'] = 'paused'; checkpoint(service, state)
    with pytest.raises(ValueError, match='completed'):
        PublicationService(service).export_epub(state['id'], tmp_path / 'partial.epub')
    assert not (tmp_path / 'partial.epub').exists()


@pytest.mark.parametrize('phase', ['Verifying source artifact','Encoding MP3','Rechecking source receipts'])
def test_cancel_at_source_encoding_or_publication_boundary_leaves_no_partial_epub(source_job, tmp_path, phase):
    service, state = source_job
    cancelled = False
    def progress(message):
        nonlocal cancelled
        if message.startswith(phase): cancelled = True
    with pytest.raises(AudioCancelled):
        PublicationService(service).export_epub(state['id'], tmp_path / 'cancelled.epub',
            cancelled=lambda: cancelled, progress=progress)
    assert not (tmp_path / 'cancelled.epub').exists()
    assert not list(tmp_path.glob('.pending-epub-*'))
    assert service.get(state['id']) == state


def test_changed_source_after_archive_creation_blocks_publication(source_job, tmp_path):
    service, state = source_job
    original = Path(state['destination']) / 'master.wav'
    def mutate(message):
        if message.startswith('Rechecking source receipts'):
            with original.open('ab') as stream: stream.write(b'changed before publication')
    with pytest.raises(ValueError, match='grew|checksum'):
        PublicationService(service).export_epub(state['id'], tmp_path / 'race.epub', progress=mutate)
    assert not (tmp_path / 'race.epub').exists()


def test_prepared_receipt_recovers_completed_bytes_after_finalization_failure(source_job, tmp_path, monkeypatch):
    service, state = source_job
    original_write = publishing.atomic_json
    def fail_final(path, record, **kwargs):
        if record.get('schema') == 'resonance-publication/1' and record.get('status') == 'complete':
            raise OSError('Synthetic final receipt interruption')
        return original_write(path, record, **kwargs)
    monkeypatch.setattr(publishing, 'atomic_json', fail_final)
    output = tmp_path / 'retained.epub'
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], output)
    assert result['status'] == 'complete' and result['receipt_status'] == 'prepared'
    assert result['receipt_finalized'] is False and result['warnings']
    assert output.exists() and result['artifact']['sha256'] == sha256(output.read_bytes()).hexdigest()
    receipt = next(publisher.receipts.glob('*.json'))
    pending = read_json(receipt); assert pending['status'] == 'prepared'
    monkeypatch.setattr(publishing, 'atomic_json', original_write)
    # Reopen the service, as after restart. Recovery verifies existing bytes
    # without parsing the original job or rendering/encoding another artifact.
    restarted = PublicationService(ProductionService(service.root))
    monkeypatch.setattr(AudioRuntime, 'convert',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('Completed output must not be encoded again.')))
    completed = restarted.verify(pending['id'])
    assert completed['status'] == 'complete' and read_json(receipt)['status'] == 'complete'
    output.write_bytes(b'Externally edited')
    with pytest.raises(ValueError, match='receipt'):
        publisher.verify(pending['id'])
    assert output.read_bytes() == b'Externally edited'


def test_explicit_byte_budget_rejects_source_graph_before_publication(source_job, tmp_path):
    service, state = source_job
    budget = AudioBudget(max_output_bytes=4096, reserve_disk_bytes=0)
    with pytest.raises(ValueError, match='byte budget'):
        PublicationService(service, budget=budget).export_epub(state['id'], tmp_path / 'oversize.epub')
    assert not (tmp_path / 'oversize.epub').exists()


def test_restored_receipt_requires_explicit_epub_selection(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'portable.epub'
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], output)
    restored = ProductionService(tmp_path / 'restored' / 'state' / 'studio')
    shutil.copytree(publisher.receipts, restored.root / 'publications')
    new = PublicationService(restored)
    with pytest.raises(ValueError, match='explicitly select'):
        new.verify(result['id'])
    moved = tmp_path / 'moved.epub'; output.rename(moved)
    assert new.verify(result['id'], output=moved)['output'] == str(moved)


def test_file_change_between_receipt_hash_and_zip_parse_is_rejected(source_job, tmp_path, monkeypatch):
    service, state = source_job
    output = tmp_path / 'verified-then-changed.epub'
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], output)
    original = publishing._inspect_epub_archive
    def changed_after_parse(archive, check, maximum):
        provenance = original(archive, check, maximum)
        with output.open('ab') as stream:
            stream.write(b'Changed after the first receipt hash')
        return provenance
    monkeypatch.setattr(publishing, '_inspect_epub_archive', changed_after_parse)
    with pytest.raises(ValueError, match='changed during structural'):
        publisher.verify(result['id'])


def test_job_change_during_export_prevents_old_scope_publication(source_job, tmp_path):
    service, state = source_job
    def change_checkpoint(message):
        if message.startswith('Rechecking source receipts'):
            state['status'] = 'paused'; checkpoint(service, state)
    with pytest.raises(ValueError, match='job changed'):
        PublicationService(service).export_epub(state['id'], tmp_path / 'changed-job.epub', progress=change_checkpoint)
    assert not (tmp_path / 'changed-job.epub').exists()


def test_empty_original_script_and_untranscribed_audio_remain_explicit(source_job, tmp_path):
    service, state = source_job
    project = parse_script('[Scene: Audio interval]\n[Duration: 0.25]', title='Untranscribed audio')
    project = replace(project, original_script='')
    destination = tmp_path / 'interval-output'; destination.mkdir()
    identity = service.submit(project, destination, stems=False)
    completed = service.run(identity)
    assert not completed['result']['lines']
    output = tmp_path / 'interval.epub'
    PublicationService(service).export_epub(identity, output)
    provenance = inspect_epub(output)
    assert provenance['original_script'] == ''
    assert all(row['kind'] == 'audio-interval' for row in provenance['intervals'])
    with ZipFile(output) as archive:
        assert b'synchronizedAudioText' not in archive.read('EPUB/package.opf')


@pytest.mark.parametrize('language,creator', [('English language',''), ('en','bad\x01control')])
def test_invalid_publication_metadata_does_not_create_output(source_job, tmp_path, language, creator):
    service, state = source_job
    with pytest.raises(ValueError):
        PublicationService(service).export_epub(state['id'], tmp_path / 'invalid.epub', language=language, creator=creator)
    assert not (tmp_path / 'invalid.epub').exists()

def test_verify_reads_current_receipt_only_after_acquiring_its_lock(source_job, tmp_path, monkeypatch):
    from contextlib import contextmanager
    service, state = source_job
    publisher = PublicationService(service)
    output = tmp_path / 'locked-snapshot.epub'
    original = publisher.export_epub(state['id'], output)
    receipt = publisher.receipts / (original['id'] + '.json')
    acquire = publishing.state_lock
    changed = deepcopy(original)
    changed['source']['project_sha256'] = '0' * 64

    @contextmanager
    def competing_writer(root, name='state'):
        with acquire(root, name):
            if Path(root) == publisher.receipts and name == original['id']:
                atomic_json(receipt, changed, replace=True)
            yield

    monkeypatch.setattr(publishing, 'state_lock', competing_writer)
    with pytest.raises(ValueError, match='source identity differs'):
        publisher.verify(original['id'])
    assert read_json(receipt) == changed
    assert sha256(output.read_bytes()).hexdigest() == original['artifact']['sha256']


def test_epub_native_inspection_and_encoding_share_current_resource_admission(source_job, tmp_path, monkeypatch):
    service, state = source_job
    observed = []
    inspect = AudioRuntime.inspect
    def admitted(runtime, path, **options):
        snapshot = service.broker.snapshot()
        assert len(snapshot['leases']) == 1
        assert snapshot['leases'][0]['operation_id'] == 'studio-publication:' + state['id']
        assert snapshot['usage']['cpu_slots'] >= 1
        assert snapshot['usage']['memory_bytes'] >= 512 * 1024 ** 2
        assert snapshot['usage']['disk_bytes'] > 0
        observed.append(str(path))
        return inspect(runtime, path, **options)
    monkeypatch.setattr(AudioRuntime, 'inspect', admitted)
    result = PublicationService(service).export_epub(state['id'], tmp_path / 'admitted.epub')
    assert result['status'] == 'complete' and len(observed) >= 2
    assert service.broker.snapshot()['leases'] == []


def test_cancelled_epub_admission_creates_no_staging_and_launches_no_decoder(source_job, tmp_path, monkeypatch):
    from carr_library.operations import ResourceCancelled, ResourceRequest

    service, state = source_job
    stopped = []
    monkeypatch.setattr(AudioRuntime, 'inspect',
                        lambda *a, **k: pytest.fail('Waiting publication launched a decoder.'))
    with service.broker.reserve(ResourceRequest(cpu_slots=service.broker.limits.cpu_slots),
            operation_id='other-native-work', timeout=0):
        with pytest.raises(ResourceCancelled):
            PublicationService(service).export_epub(state['id'], tmp_path / 'cancelled.epub',
                cancelled=lambda: bool(stopped),
                progress=lambda message: stopped.append(True) if message.startswith('Waiting for shared') else None)
        assert len(service.broker.snapshot()['leases']) == 1
    assert not (tmp_path / 'cancelled.epub').exists()
    assert not list(tmp_path.glob('.pending-epub-*'))
    assert service.get(state['id']) == state


@pytest.mark.parametrize('kind', ('epub', 'daisy-2.02'))
def test_actual_pending_epub_child_retains_stage_job_lock_and_broker(source_job, tmp_path, monkeypatch, kind):
    import os
    import sys
    import threading

    from filelock import FileLock, Timeout
    from carr_library.audio import process
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.audio.windows_job import JobProcess
    from carr_library.operations import ResourceRequest, ResourceTimeout

    service, state = source_job
    ready, allowed, attempts = tmp_path / 'epub-child-ready', threading.Event(), []
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_terminate')
    actual = getattr(target, name)
    def deny(child):
        if not allowed.is_set():
            attempts.append(child)
            raise OSError('injected EPUB native cleanup failure')
        return actual(child)
    monkeypatch.setattr(target, name, deny)
    def pending(runtime, path, **options):
        return process.run_process([str(Path(sys.executable).resolve()), '-I', '-c',
            'import pathlib,time;pathlib.Path(' + repr(str(ready)) + ').write_text("ready");time.sleep(30)'],
            budget=AudioBudget(timeout=20, reserve_disk_bytes=0),
            owned_tree=True, cancelled=ready.exists)
    monkeypatch.setattr(AudioRuntime, 'inspect', pending)
    output = tmp_path / ('not-accepted.epub' if kind == 'epub' else 'not-accepted.zip')
    competitor = FileLock(service.jobs_root / state['id'] / '.render.lock', timeout=0)
    owner, stages = None, []
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            publisher = PublicationService(service)
            if kind == 'epub':
                publisher.export_epub(state['id'], output)
            else:
                publisher.export_daisy(state['id'], output, publisher='Synthetic explicit publisher')
        owner = failure.value.cleanup_owner
        released = threading.Event()
        owner.add_quiescent_callback(released.set)
        assert ready.exists() and attempts and attempts[0].poll() is None
        assert not owner.quiescent
        stages = list(tmp_path.glob('.pending-epub-*'))
        assert len(stages) == 1 and any(stages[0].iterdir())
        assert not output.exists()
        assert len(service.broker.snapshot()['leases']) == 1
        with pytest.raises(Timeout):
            competitor.acquire()
        with pytest.raises(ResourceTimeout):
            with service.broker.reserve(ResourceRequest(cpu_slots=service.broker.limits.cpu_slots),
                    operation_id='blocked-epub-competitor', timeout=0):
                pytest.fail('Publication cleanup released native capacity prematurely.')
        assert service.get(state['id']) == state
    finally:
        allowed.set()
        if owner is not None:
            owner.close()
            assert released.wait(10) and owner.quiescent
    assert all(not stage.exists() for stage in stages)
    assert service.broker.snapshot()['leases'] == []
    with competitor:
        pass


@pytest.mark.parametrize('cancel', [False, True])
def test_publication_inventory_closes_bounded_scandir_without_materializing_directory(
        source_job, tmp_path, monkeypatch, cancel):
    service, _ = source_job
    publisher = PublicationService(service)
    publisher.receipts.mkdir(parents=True, exist_ok=True)
    seen, closed = [], []
    class Entries:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
        def __iter__(self):
            for number in range(10002):
                seen.append(number)
                yield SimpleNamespace(path=str(publisher.receipts / f'ignored-{number}.txt'))
            pytest.fail('Publication inventory exceeded its streaming limit.')
    monkeypatch.setattr(publishing.os, 'scandir', lambda path: Entries())
    expected = AudioCancelled if cancel else ValueError
    with pytest.raises(expected):
        publisher.publications(cancelled=lambda: cancel and len(seen) == 2)
    assert len(seen) == (2 if cancel else 10001)
    assert closed == [True]


@pytest.mark.parametrize('stop', ('cancel', 'timeout'))
def test_late_stop_after_actual_publication_retains_verified_success(source_job, tmp_path, monkeypatch, stop):
    service, state = source_job
    target = tmp_path / 'late-stop.epub'
    linked = False
    original_publish, original_clock = publishing.publish_file, publishing.monotonic

    def publish_then_stop(staged, output, **kwargs):
        nonlocal linked
        original_publish(staged, output, **kwargs)
        assert os.path.samefile(staged, output)
        linked = True

    monkeypatch.setattr(publishing, 'publish_file', publish_then_stop)
    if stop == 'timeout':
        monkeypatch.setattr(publishing, 'monotonic',
            lambda: original_clock() + (7200 if linked else 0))
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], target, cancelled=lambda: linked and stop == 'cancel')
    assert linked and result['status'] == 'complete'
    assert result['artifact']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert read_json(publisher.receipts / (result['id'] + '.json')) == result
    assert inspect_epub(target)['publication_id'] == result['id']
    assert not list(tmp_path.glob('.pending-epub-*'))


@pytest.mark.parametrize('interruption', (OSError, KeyboardInterrupt))
def test_writer_error_after_actual_link_cannot_replace_verified_publication(
        source_job, tmp_path, monkeypatch, interruption):
    service, state = source_job
    target = tmp_path / 'linked-before-error.epub'
    original = publishing.publish_file

    def publish_then_report_error(staged, output, **kwargs):
        original(staged, output, **kwargs)
        assert os.path.samefile(staged, output)
        raise interruption('Synthetic post-link directory acknowledgement failure')

    monkeypatch.setattr(publishing, 'publish_file', publish_then_report_error)
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], target)
    assert result['status'] == 'complete' and result['warnings']
    assert result['artifact']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert inspect_epub(target)['publication_id'] == result['id']
    assert publisher.verify(result['id'])['status'] == 'complete'
    assert not list(tmp_path.glob('.pending-epub-*'))


@pytest.mark.parametrize('cleanup', ('staging-link', 'temporary-directory'))
def test_owned_cleanup_failure_does_not_report_a_completed_epub_as_failed(
        source_job, tmp_path, monkeypatch, cleanup):
    service, state = source_job
    target = tmp_path / 'cleanup-warning.epub'
    observed = []
    if cleanup == 'staging-link':
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == 'publication.epub' and target.exists() and not observed:
                observed.append(path)
                raise PermissionError('Synthetic delayed staging-link cleanup')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'unlink', unlink)
    else:
        original = publishing.TemporaryDirectory.cleanup

        def remove(temporary):
            original(temporary)
            if Path(temporary.name).name.startswith('.pending-epub-') and not observed:
                observed.append(temporary.name)
                raise OSError('Synthetic cleanup acknowledgement failure')

        monkeypatch.setattr(publishing.TemporaryDirectory, 'cleanup', remove)
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], target)
    assert observed and result['status'] == 'complete' and result['warnings']
    assert result['artifact']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert inspect_epub(target)['publication_id'] == result['id']
    assert PublicationService(ProductionService(service.root)).verify(result['id'])['status'] == 'complete'
    assert not list(tmp_path.glob('.pending-epub-*'))


def test_complete_receipt_written_before_error_is_reported_from_actual_readback(source_job, tmp_path, monkeypatch):
    service, state = source_job
    original_write = publishing.atomic_json

    def write_then_report_error(path, record, **kwargs):
        result = original_write(path, record, **kwargs)
        if record.get('schema') == 'resonance-publication/1' and record.get('status') == 'complete':
            raise OSError('Synthetic receipt acknowledgement failure after replacement')
        return result

    monkeypatch.setattr(publishing, 'atomic_json', write_then_report_error)
    publisher = PublicationService(service)
    target = tmp_path / 'completed-receipt.epub'
    result = publisher.export_epub(state['id'], target)
    saved = read_json(publisher.receipts / (result['id'] + '.json'))
    assert result['status'] == result['receipt_status'] == saved['status'] == 'complete'
    assert result['receipt_finalized'] is True and result['warnings']
    assert result['artifact'] == saved['artifact']
    assert result['artifact']['sha256'] == sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize('writer_outcome', ('occupied', 'false-return'))
def test_equal_competing_bytes_are_not_this_attempts_publication(
        source_job, tmp_path, monkeypatch, writer_outcome):
    service, state = source_job
    target = tmp_path / 'foreign-copy.epub'
    foreign = []

    def different_inode(staged, output, **kwargs):
        shutil.copyfile(staged, output)
        assert not os.path.samefile(staged, output)
        foreign.append(Path(output).read_bytes())
        if writer_outcome == 'occupied':
            raise FileExistsError('Synthetic competitor published before this writer')

    monkeypatch.setattr(publishing, 'publish_file', different_inode)
    publisher = PublicationService(service)
    with pytest.raises((FileExistsError, RuntimeError), match='Publication receipt:'):
        publisher.export_epub(state['id'], target)
    assert foreign and target.read_bytes() == foreign[0]
    receipt, = publisher.publications()
    assert receipt['status'] == 'prepared'
    assert not list(tmp_path.glob('.pending-epub-*'))


def test_actual_post_link_corruption_is_not_misclassified_as_late_cancellation(
        source_job, tmp_path, monkeypatch):
    service, state = source_job
    target = tmp_path / 'changed-after-link.epub'
    original = publishing.publish_file
    changed = []

    def publish_then_change(staged, output, **kwargs):
        original(staged, output, **kwargs)
        with Path(output).open('ab') as stream:
            stream.write(b'External mutation after publication')
        changed.append(Path(output).read_bytes())

    monkeypatch.setattr(publishing, 'publish_file', publish_then_change)
    publisher = PublicationService(service)
    with pytest.raises(RuntimeError, match='Publication receipt:'):
        publisher.export_epub(state['id'], target, cancelled=lambda: bool(changed))
    assert target.read_bytes() == changed[0]
    pending, = publisher.publications()
    assert pending['status'] == 'prepared'
    with pytest.raises(ValueError, match='receipt'):
        publisher.verify(pending['id'])
    assert target.read_bytes() == changed[0]


def test_verify_success_is_retained_when_only_receipt_update_fails(source_job, tmp_path, monkeypatch):
    service, state = source_job
    publisher = PublicationService(service)
    target = tmp_path / 'verified-with-receipt-warning.epub'
    exported = publisher.export_epub(state['id'], target)
    original = target.read_bytes()

    def failed_write(*args, **kwargs):
        raise OSError('Synthetic receipt-only update failure')

    monkeypatch.setattr(publishing, 'atomic_json', failed_write)
    verified = PublicationService(ProductionService(service.root)).verify(exported['id'])
    assert verified['status'] == 'complete' and verified['warnings']
    assert verified['artifact'] == exported['artifact']
    assert target.read_bytes() == original


def test_resource_release_error_is_visible_without_undoing_verified_output(
        source_job, tmp_path, monkeypatch):
    from carr_library.operations import ResourceLease
    service, state = source_job
    original = ResourceLease.release
    observed = []

    def release_then_report(lease):
        original(lease)
        if lease.operation_id.startswith('studio-publication:') and not observed:
            observed.append(lease.id)
            raise OSError('Synthetic resource release acknowledgement failure')

    monkeypatch.setattr(ResourceLease, 'release', release_then_report)
    result = PublicationService(service).export_epub(state['id'], tmp_path / 'released.epub')
    assert observed and result['status'] == 'complete'
    assert any('Synthetic resource release acknowledgement failure' in item for item in result['warnings'])
    assert service.broker.snapshot()['leases'] == []


def test_publication_cannot_write_inside_another_private_library_state_root(source_job):
    service, state = source_job
    target = service.root.parent / 'untracked-export.epub'
    with pytest.raises(ValueError, match='outside private library state'):
        PublicationService(service).export_epub(state['id'], target)
    assert not target.exists()


@pytest.mark.parametrize('boundary', ('before-link', 'after-link'))
def test_real_child_exit_requires_exact_saved_epub_and_recovers_residual_link(
        source_job, tmp_path, monkeypatch, boundary):
    service, state = source_job
    target = tmp_path / 'native-process-publication.epub'
    marker = tmp_path / 'publication-child.json'
    originals = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                 for row in state['artifacts']}
    child = r'''
import json, os, sys
from pathlib import Path
from carr_library.studio import publishing
from carr_library.studio.render import ProductionService

root, job_id, destination, marker, boundary, expected_origin = sys.argv[1:]
origin = Path(publishing.__file__).resolve()
assert origin == Path(expected_origin).resolve(), (origin, expected_origin)
original = publishing.publish_file
def exit_at_publication(staged, output, **options):
    if boundary == 'after-link':
        original(staged, output, **options)
        assert os.path.samefile(staged, output)
    Path(marker).write_text(json.dumps({'module': str(origin), 'staged': str(staged)}),
                            encoding='utf-8')
    os._exit(73)
publishing.publish_file = exit_at_publication
publishing.PublicationService(ProductionService(root)).export_epub(job_id, destination)
raise AssertionError('The native publication boundary was not reached.')
'''
    # Ordinary Python import resolution from the already selected package root:
    # source runs use src; installed runs use site-packages. No sys.path mutation
    # or process-launch interception can substitute another product here.
    process = subprocess.run(
        [sys.executable, '-B', '-c', child, str(service.root), state['id'], str(target),
         str(marker), boundary, str(Path(publishing.__file__).resolve())],
        cwd=Path(publishing.__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=90, check=False)
    assert process.returncode == 73, (process.returncode, process.stdout, process.stderr)
    observed = json.loads(marker.read_text(encoding='utf-8'))
    assert Path(observed['module']) == Path(publishing.__file__).resolve()
    publisher = PublicationService(ProductionService(service.root))
    saved, = publisher.publications()
    assert saved['status'] == 'prepared'
    receipt = publisher.receipts / (saved['id'] + '.json')

    def no_encoding(*args, **kwargs):
        raise AssertionError('Saved publication verification must not encode audio.')

    monkeypatch.setattr(AudioRuntime, 'convert', no_encoding)
    if boundary == 'before-link':
        assert not target.exists()
        with pytest.raises(FileNotFoundError):
            publisher.verify(saved['id'])
        assert read_json(receipt)['status'] == 'prepared'
    else:
        staged = Path(observed['staged'])
        assert target.stat().st_nlink == 2 and os.path.samefile(target, staged)
        original_bytes = target.read_bytes()
        with pytest.raises(ValueError, match='without links'):
            inspect_epub(target)
        result = publisher.verify(saved['id'])
        assert result['status'] == 'complete' and result['artifact'] == saved['artifact']
        assert any('additional hard link' in item for item in result['warnings'])
        assert read_json(receipt)['status'] == 'complete'
        assert target.read_bytes() == original_bytes
        assert target.stat().st_nlink == 2 and os.path.samefile(target, staged)

        # Explicit saved-receipt verification also accepts an identical copied
        # EPUB. This proves its bytes/source identity, not a new export attempt.
        copied = tmp_path / 'explicit-identical-copy.epub'
        shutil.copyfile(target, copied)
        assert not os.path.samefile(target, copied) and copied.stat().st_nlink == 1
        copy_result = publisher.verify(saved['id'], output=copied)
        assert copy_result['status'] == 'complete' and copy_result['artifact'] == saved['artifact']
        assert copy_result['output'] == str(copied)
        assert read_json(receipt) == copy_result

        # A surviving alias can mutate the published bytes. Such a file no
        # longer satisfies the receipt and must not be relabeled complete.
        with staged.open('ab') as stream:
            stream.write(b'Changed through the residual hard link')
        with pytest.raises(ValueError, match='receipt'):
            publisher.verify(saved['id'], output=target)
        assert read_json(receipt) == copy_result
        assert copied.read_bytes() == original_bytes
        assert target.stat().st_nlink == 2 and staged.exists()
    assert originals == {name: sha256(Path(name).read_bytes()).hexdigest() for name in originals}


def test_late_keyboard_interrupt_in_observer_returns_verified_output_after_resource_release(
        source_job, tmp_path):
    service, state = source_job
    target = tmp_path / 'late-observer-interruption.epub'

    def late_observer(message):
        if message == 'EPUB saved and its exact bytes verified':
            raise KeyboardInterrupt('Synthetic notification interruption after publication')

    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], target, progress=late_observer)
    assert result['status'] == 'complete'
    assert result['artifact']['sha256'] == sha256(target.read_bytes()).hexdigest()
    assert read_json(publisher.receipts / (result['id'] + '.json'))['status'] == 'complete'
    assert any('later notification or cleanup' in item for item in result['warnings'])
    assert 'resource_release_confirmed' not in result
    assert service.broker.snapshot()['leases'] == []
