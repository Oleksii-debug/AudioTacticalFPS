"""Real completed primary productions; synthetic tones prove packaging, not speech."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import wave
from zipfile import ZipFile

import pytest
from filelock import FileLock

from carr_library.audio.director import ProductionDirector
from carr_library.audio.models import AudioCancelled
from carr_library.audio.narration import cue_derivative, set_cue_derivative, spoken_cue
from carr_library.audio.projects import ProjectStore, Settings, atomic_json, parse_script
from carr_library.durable_state import encode
from carr_library.studio import publishing
from carr_library.studio.publishing import PublicationService, inspect_epub
from carr_library.studio.publishing_source import DirectorPublicationSource, LibraryPublicationService


SCRIPT = '# Source chapter\r\nNarrator: Dr. Grey says <yes> & more.\r\n@pause 0.2\r\n# Ending\r\nReader: Exact final line.\r\n'


class ExplicitToneFixture:
    def list_voices(self):
        return (SimpleNamespace(id='synthetic-tone'),)

    def fingerprint(self):
        return sha256(b'publication synthetic tone fixture 1').hexdigest()

    def synthesize(self, text, output, *, voice_id, language=None, rate=1, volume=1, cancelled=None):
        assert voice_id == 'synthetic-tone'
        if cancelled and cancelled():
            raise InterruptedError('Synthetic fixture cancelled.')
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 24000, 0, 'NONE', 'synthetic'))
            stream.writeframes(struct.pack('<h', 1800) * 4800)


def create_job(root, output, *, format='wav'):
    settings = Settings(sample_rate=24000, channels=2, stems=False, format=format, chapter_gap_seconds=.1)
    project = parse_script(SCRIPT, ['synthetic-tone'], title='Primary original with reading copy', settings=settings,
        source={'workspace': 'fixture-workspace', 'edition': 7, 'revision': 'fixture-source-v1',
                'text_sha256': sha256(SCRIPT.encode()).hexdigest()})
    store = ProjectStore(root)
    store.create(project)
    cue = next(cue for scene in project.scenes for cue in scene.cues if cue.kind == 'speech')
    derivative = cue_derivative(project, cue.id).apply(0, 3, 'Doctor', reason='Explicit synthetic pronunciation review')
    project = set_cue_derivative(project, cue.id, derivative)
    store.save(project)
    director = ProductionDirector(root, registry=ExplicitToneFixture(), music_options={})
    result = director.render(project.id, output)
    assert result['state'] == 'complete'
    return director, project, result


@pytest.fixture(scope='module')
def primary_completed(tmp_path_factory):
    root = tmp_path_factory.mktemp('primary-publication-fixture')
    return create_job(root / 'library', root / 'rendered')


@pytest.fixture
def primary(tmp_path, primary_completed):
    original, project, result = primary_completed
    root, destination = tmp_path / 'library', tmp_path / 'rendered'
    shutil.copytree(original.store.root, root)
    shutil.copytree(result['output_dir'], destination)
    source = DirectorPublicationSource(root, project.id)
    state = source.get(result['job_id'])
    state['output_dir'] = str(destination.resolve())
    state['result'].update(output_dir=state['output_dir'], master=str(destination.resolve() / 'master.wav'))
    atomic_json(source.folder / 'jobs' / (state['id'] + '.json'), state)
    return source, state, project


def rewrite_timeline(source, state, mutation):
    path = Path(state['output_dir']) / 'production.json'
    metadata = json.loads(path.read_text(encoding='utf-8'))
    mutation(metadata)
    atomic_json(path, metadata)
    receipt = {'file': 'production.json', 'sha256': sha256(path.read_bytes()).hexdigest(),
               'size_bytes': path.stat().st_size}
    state['outputs'] = [receipt if row['file'] == 'production.json' else row for row in state['outputs']]
    state['result']['outputs'] = state['outputs']
    atomic_json(source.folder / 'jobs' / (state['id'] + '.json'), state)


def test_primary_publication_preserves_exact_source_and_accepted_reading_copy(primary, tmp_path):
    source, state, project = primary
    before = {row['file']: sha256((Path(state['output_dir']) / row['file']).read_bytes()).hexdigest()
              for row in state['outputs']}
    target = tmp_path / 'primary.epub'
    receipt = PublicationService(source).export_epub(state['id'], target, language='en')
    provenance = inspect_epub(target)
    assert receipt['source'] == provenance['source']
    assert provenance['source']['family'] == 'director'
    assert provenance['source']['project_snapshot'] == json.loads(encode(project.to_dict()))
    assert provenance['source']['project_sha256'] == project.fingerprint
    assert provenance['source']['source_reference'] == project.source
    assert provenance['source']['job_sha256'] == sha256(encode(state)).hexdigest()
    assert provenance['source']['cache_receipts_sha256'] == sha256(encode(state['steps'])).hexdigest()
    assert provenance['original_script'] == SCRIPT
    cue = next(cue for scene in project.scenes for cue in scene.cues if cue.kind == 'speech')
    spoken = spoken_cue(project, cue)
    first = next(row for row in provenance['intervals'] if row['kind'] == 'line')
    assert first['line'] == cue.id and first['source_details']['original_text'] == cue.text
    assert first['source_details']['source_start'] == cue.source_start
    assert first['source_details']['source_mapping'] == spoken['mapping']
    assert first['source_details']['reading_copy_sha256'] == spoken['derivative_sha256']
    assert first['text_sha256'] == sha256(spoken['text'].encode()).hexdigest()
    assert 'Doctor' in spoken['text'] and SCRIPT[cue.source_start:cue.source_end] == cue.text
    assert before == {name: sha256((Path(state['output_dir']) / name).read_bytes()).hexdigest() for name in before}
    assert source.get(state['id']) == state
    assert PublicationService(source).verify(receipt['id'])['artifact'] == receipt['artifact']


@pytest.mark.parametrize('mutation', [
    lambda value: value['timings'][0].update(source_start=0),
    lambda value: value['timings'][0].update(source_mapping=[]),
    lambda value: value['timings'][0].update(original_text='Different original'),
    lambda value: value['timings'][0].update(reading_copy_sha256='0' * 64),
    lambda value: value['timings'][0].update(text='Unapproved reading copy'),
    lambda value: value['timings'][0].update(role='Different role'),
    lambda value: value['chapters'][0].update(start_frame=1),
])
def test_rehashed_primary_timeline_cannot_rebind_original_semantics(primary, tmp_path, mutation, monkeypatch):
    source, state, _ = primary
    rewrite_timeline(source, state, mutation)
    monkeypatch.setattr('carr_library.runtime_settings.audio_runtime',
                        lambda *a, **k: pytest.fail('Invalid source reached an audio executable.'))
    with pytest.raises(ValueError, match='Primary Studio timing|primary Studio chapter'):
        PublicationService(source).export_epub(state['id'], tmp_path / 'rejected.epub')
    assert not (tmp_path / 'rejected.epub').exists()


def test_primary_publication_uses_real_render_lock(primary, tmp_path):
    source, state, _ = primary
    with FileLock(source.folder / '.render.lock', timeout=0):
        with pytest.raises(RuntimeError, match='active render'):
            PublicationService(source).export_epub(state['id'], tmp_path / 'locked.epub')
    assert not (tmp_path / 'locked.epub').exists()


@pytest.mark.parametrize('marker', [True, 1, 'true'])
def test_restored_primary_job_cannot_reopen_former_external_output(primary, tmp_path, marker, monkeypatch):
    source, state, _ = primary
    state.update(restore_requires_new_output=marker, output_dir='//former-machine/share/output')
    atomic_json(source.folder / 'jobs' / (state['id'] + '.json'), state)
    monkeypatch.setattr(publishing, '_read', lambda *a, **k: pytest.fail('Restored job opened external output.'))
    with pytest.raises(ValueError, match='restored'):
        PublicationService(source).export_epub(state['id'], tmp_path / 'restored.epub')


@pytest.mark.parametrize('phase', ['Verifying primary Studio source artifact', 'Packaging EPUB', 'Prepared publication receipt'])
def test_primary_cancel_keeps_originals_and_never_publishes_partial_epub(primary, tmp_path, phase):
    source, state, _ = primary
    stopped = False
    def progress(message):
        nonlocal stopped
        if message.startswith(phase):
            stopped = True
    target = tmp_path / 'cancelled.epub'
    with pytest.raises(AudioCancelled):
        PublicationService(source).export_epub(state['id'], target, cancelled=lambda: stopped, progress=progress)
    assert not target.exists()
    assert source.get(state['id']) == state


def test_primary_existing_mp3_is_reused_without_a_second_encode(tmp_path, monkeypatch):
    director, project, result = create_job(tmp_path / 'library', tmp_path / 'rendered', format='mp3')
    source = DirectorPublicationSource(director.store.root, project.id)
    from carr_library.audio import AudioRuntime
    monkeypatch.setattr(AudioRuntime, 'convert', lambda *a, **k: pytest.fail('Existing verified MP3 was re-encoded.'))
    target = tmp_path / 'reused.epub'
    PublicationService(source).export_epub(result['job_id'], target)
    with ZipFile(target) as archive:
        assert archive.read('EPUB/audio/master.mp3') == (Path(result['output_dir']) / 'audio.mp3').read_bytes()
    assert inspect_epub(target)['audio']['reused'] is True


def test_primary_no_clobber_and_output_owner_identity(primary, tmp_path):
    source, state, _ = primary
    target = tmp_path / 'occupied.epub'; target.write_bytes(b'Previous user file.')
    with pytest.raises(FileExistsError):
        PublicationService(source).export_epub(state['id'], target)
    assert target.read_bytes() == b'Previous user file.'
    atomic_json(Path(state['output_dir']) / '.production-job.json', {'job_id': 'wrong', 'project_fingerprint': '0' * 64})
    with pytest.raises(ValueError, match='another production'):
        PublicationService(source).export_epub(state['id'], tmp_path / 'wrong-owner.epub')


def test_menu_router_requires_explicit_family_and_preserves_original_job_id(primary, tmp_path):
    source, state, project = primary
    router = LibraryPublicationService(source.private_root)
    rows = router.jobs()
    assert len(rows) == 1 and rows[0]['family'] == 'Audio Studio'
    assert rows[0]['id'] == f'director:{project.id}:{state["id"]}'
    target = tmp_path / 'routed.epub'
    receipt = router.export_epub(rows[0]['id'], target)
    assert receipt['source']['job_id'] == state['id']
    with pytest.raises(ValueError):
        router.export_epub(state['id'], tmp_path / 'implicit.epub')


def test_utf16_doctype_cannot_bypass_entity_rejection(primary, tmp_path):
    source, state, _ = primary
    good, malicious = tmp_path / 'source.epub', tmp_path / 'utf16.epub'
    PublicationService(source).export_epub(state['id'], good)
    raw = ('<?xml version="1.0" encoding="UTF-16"?>'
           '<!DOCTYPE container [<!ENTITY external SYSTEM "file:///must-not-open">]>'
           '<container>&external;</container>').encode('utf-16')
    with ZipFile(good) as original, ZipFile(malicious, 'x') as modified:
        for entry in original.infolist():
            modified.writestr(entry, raw if entry.filename == 'META-INF/container.xml' else original.read(entry.filename))
    with pytest.raises(ValueError, match='without entity declarations'):
        inspect_epub(malicious)


def test_primary_provenance_map_cannot_be_changed_after_export(primary, tmp_path):
    source, state, _ = primary
    good, changed = tmp_path / 'source.epub', tmp_path / 'changed.epub'
    PublicationService(source).export_epub(state['id'], good)
    from defusedxml import ElementTree as SafeET
    import xml.etree.ElementTree as ET
    with ZipFile(good) as original, ZipFile(changed, 'x') as modified:
        for entry in original.infolist():
            raw = original.read(entry.filename)
            if entry.filename == 'META-INF/metadata.xml':
                document = SafeET.fromstring(raw)
                node = document.find('{' + publishing.PROVENANCE + '}provenance')
                provenance = json.loads(node.text)
                next(row for row in provenance['intervals'] if row['kind'] == 'line')['source_details']['source_mapping'] = []
                node.text = json.dumps(provenance, ensure_ascii=False)
                raw = ET.tostring(document, encoding='utf-8', xml_declaration=True)
            modified.writestr(entry, raw)
    with pytest.raises(ValueError, match='source mapping'):
        inspect_epub(changed)
