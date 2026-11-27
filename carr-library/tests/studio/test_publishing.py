"""Actual FFmpeg/Studio/EPUB acceptance using explicitly synthetic tone voices.

The fixtures prove measured audio packaging and source binding, not speech
quality, actual model qualification, EPUBCheck or third-party reader behavior.
"""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import wave
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.audio import AudioRuntime
from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.durable_state import atomic_json, read_json
from carr_library.studio.project import Project, parse_script
from carr_library.studio.render import ProductionService
from carr_library.studio import publishing
from carr_library.studio.publishing import PublicationService, inspect_epub


SCRIPT = ('[Scene: Архів & letters]\r\n'
          'Narrator: Exact <original> & preserved words.\r\n'
          '[Pause: 0.2]\r\n'
          'Reader: Другий рядок «цитати».\r\n'
          '[Scene: Closing]\r\n'
          'Narrator: The final line.\r\n')


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
    with pytest.raises(RuntimeError, match='Publication receipt:'):
        publisher.export_epub(state['id'], output)
    assert output.exists()
    receipt = next(publisher.receipts.glob('*.json'))
    pending = read_json(receipt); assert pending['status'] == 'prepared'
    monkeypatch.setattr(publishing, 'atomic_json', original_write)
    completed = publisher.verify(pending['id'])
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
    original = publishing.inspect_epub
    def changed_after_parse(path, **kwargs):
        provenance = original(path, **kwargs)
        with Path(path).open('ab') as stream:
            stream.write(b'Changed after the first receipt hash')
        return provenance
    monkeypatch.setattr(publishing, 'inspect_epub', changed_after_parse)
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


def test_scene_router_blocks_entire_private_library_destination(source_job, monkeypatch):
    service, state = source_job
    from carr_library.studio.publishing_source import LibraryPublicationService
    router = LibraryPublicationService(service.root.parent.parent)
    monkeypatch.setattr(PublicationService, '_source',
                        lambda *a, **k: pytest.fail('Private destination reached source verification.'))
    with pytest.raises(ValueError, match='outside private'):
        router.export_epub('scene:' + state['id'], router.library_root / 'private.epub')
    assert not (router.library_root / 'private.epub').exists()


def test_nul_truncated_zip_name_cannot_masquerade_as_fixed_member(source_job, tmp_path):
    service, state = source_job
    good, changed = tmp_path / 'good.epub', tmp_path / 'raw-nul.epub'
    PublicationService(service).export_epub(state['id'], good)
    from zipfile import ZipInfo
    class RawNulName(ZipInfo):
        def _encodeFilenameFlags(self):
            return b'mimetype\x00hidden', self.flag_bits
    with ZipFile(good) as original, ZipFile(changed, 'x') as modified:
        for entry in original.infolist():
            target = RawNulName('mimetype') if entry.filename == 'mimetype' else entry
            modified.writestr(target, original.read(entry.filename))
    with ZipFile(changed) as archive:
        assert archive.infolist()[0].filename == 'mimetype'
        assert archive.infolist()[0].orig_filename != 'mimetype'
    with pytest.raises(ValueError, match='container graph'):
        inspect_epub(changed)
