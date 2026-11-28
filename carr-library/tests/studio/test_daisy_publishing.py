"""Actual Studio/FFmpeg DAISY interchange and durable publication boundaries."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from carr_library.audio import AudioRuntime
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import read_json
from carr_library.studio import daisy, publishing
from carr_library.studio.project import Project
from carr_library.studio.render import ProductionService
from carr_library.studio.publishing import PublicationService

_spec = importlib.util.spec_from_file_location('daisy_studio_fixtures', Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job, source_job = _fixtures.actual_job, _fixtures.source_job


def export(service, state, output, **kwargs):
    return PublicationService(service).export_daisy(state['id'], output, publisher='Synthetic fixture publisher',
                                                    language='uk', **kwargs)


def test_actual_daisy_ncc_smil_audio_source_and_same_receipt(source_job, tmp_path):
    service, state = source_job
    original = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in state['artifacts']}
    output = tmp_path / 'talking-book.zip'
    record = export(service, state, output, creator='Synthetic fixture creator')
    assert record['format'] == 'daisy-2.02' and record['status'] == 'complete'
    provenance = daisy.inspect_daisy(output)
    assert provenance['original_script'] == _fixtures.SCRIPT
    assert provenance['publisher'] == 'Synthetic fixture publisher'
    assert provenance['creator'] == 'Synthetic fixture creator'
    assert provenance['validation']['independent_daisy_validator'] == 'NOT_RUN'
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == daisy.NAMES
        ncc = ET.fromstring(archive.read('ncc.html'))
        ns = '{http://www.w3.org/1999/xhtml}'
        meta = {node.get('name'): node.get('content') for node in ncc.findall(ns + 'head/' + ns + 'meta')}
        assert meta['dc:format'] == 'Daisy 2.02' and meta['dc:publisher'] == provenance['publisher']
        assert meta['ncc:totalTime'] == daisy._ncc_clock(provenance['duration'])
        assert meta['ncc:totalTime'].count(':') == 2 and not meta['ncc:totalTime'].endswith('s')
        navigation = ncc.find(ns + 'body')
        assert len(navigation) == 3 and meta['ncc:tocItems'] == '3'
        assert navigation[0].find(ns + 'a').get('href') == 'book.smil#heading'
        assert all(node.find(ns + 'a').get('href').startswith('book.smil#par-') for node in list(navigation)[1:])
        assert meta['ncc:pageNormal'] == meta['ncc:pageFront'] == meta['ncc:pageSpecial'] == '0'
        smil = ET.fromstring(archive.read('book.smil'))
        heading, *intervals = smil.findall('body/seq/par')
        assert heading.get('dur') == '0s' and heading.find('audio') is None
        assert len(intervals) == len(provenance['intervals']) > len(state['result']['lines'])
        assert all(par.get('endsync') == 'last' and par.find('text').get('id')
                   and par.find('audio').get('clip-begin').startswith('npt=') for par in intervals)
        audio = tmp_path / 'extracted.mp3'; audio.write_bytes(archive.read('master.mp3'))
    decoded = AudioRuntime(tmp_path, input_roots=(tmp_path,)).inspect(audio)
    assert decoded.codec == 'mp3' and abs(decoded.duration - state['result']['duration']) < .15
    assert original == {name: sha256(Path(name).read_bytes()).hexdigest() for name in original}
    assert service.get(state['id']) == state
    assert PublicationService(service).verify(record['id']) == record
    assert not list(tmp_path.glob('.pending-daisy-*'))


def test_actual_daisy_reuses_verified_mp3_without_reencoding(source_job, tmp_path, monkeypatch):
    service, state = source_job
    destination = tmp_path / 'mp3'; destination.mkdir()
    identity = service.submit(Project.from_dict(state['project']), destination, format='mp3', stems=False)
    state = service.run(identity)
    expected = (Path(state['destination']) / 'audiobook.mp3').read_bytes()
    monkeypatch.setattr(AudioRuntime, 'convert', lambda *args, **kwargs: pytest.fail('Verified MP3 must be reused'))
    output = tmp_path / 'reused.zip'; export(service, state, output)
    with ZipFile(output) as archive:
        assert archive.read('master.mp3') == expected
    assert daisy.inspect_daisy(output)['audio']['reused']


@pytest.mark.parametrize('publisher,language,filename', [('', 'uk', 'book.zip'), (' ', 'en', 'book.zip'),
    ('Publisher', 'en-US-script', 'book.zip'), ('Publisher', 'uk', 'book.epub'),
    ('Publisher\x00', 'uk', 'book.zip')])
def test_invalid_daisy_metadata_cannot_publish(source_job, tmp_path, publisher, language, filename):
    service, state = source_job
    with pytest.raises(ValueError):
        PublicationService(service).export_daisy(state['id'], tmp_path / filename,
                                                 publisher=publisher, language=language)
    assert not (tmp_path / filename).exists()
    assert not (service.root / 'publications').exists()


def test_daisy_no_clobber_and_cancel_preserve_previous_files(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'existing.zip'; output.write_bytes(b'Owner bytes')
    with pytest.raises(FileExistsError):
        export(service, state, output)
    assert output.read_bytes() == b'Owner bytes'
    cancelled = tmp_path / 'cancelled.zip'
    with pytest.raises(AudioCancelled):
        export(service, state, cancelled, cancelled=lambda: True)
    assert not cancelled.exists() and not list(tmp_path.glob('.pending-daisy-*'))


def test_daisy_source_mutation_before_commit_refuses_publication(source_job, tmp_path):
    service, state = source_job
    output = tmp_path / 'changed-source.zip'
    source = Path(state['destination']) / 'master.wav'
    def progress(message):
        if message.startswith('Rechecking source receipts'):
            with source.open('r+b') as stream:
                stream.seek(-1, 2); old = stream.read(1)
                stream.seek(-1, 2); stream.write(bytes([old[0] ^ 1]))
    with pytest.raises(ValueError, match='checksum'):
        export(service, state, output, progress=progress)
    assert not output.exists() and not (service.root / 'publications').exists()


def test_daisy_prepared_receipt_survives_failure_and_explicit_relocation(source_job, tmp_path, monkeypatch):
    service, state = source_job
    original_write = publishing.atomic_json
    def fail_final(path, record, **kwargs):
        if record.get('schema') == 'resonance-publication/1' and record.get('status') == 'complete':
            raise OSError('Synthetic final receipt storage failure')
        return original_write(path, record, **kwargs)
    monkeypatch.setattr(publishing, 'atomic_json', fail_final)
    output = tmp_path / 'retained.zip'
    with pytest.raises(RuntimeError, match='Publication receipt:'):
        export(service, state, output)
    receipt_path = next((service.root / 'publications').glob('*.json'))
    pending = read_json(receipt_path)
    assert output.exists() and pending['status'] == 'prepared' and pending['format'] == 'daisy-2.02'
    monkeypatch.setattr(publishing, 'atomic_json', original_write)
    moved = tmp_path / 'moved.zip'; output.rename(moved)
    restored = ProductionService(tmp_path / 'restored' / 'state' / 'studio')
    shutil.copytree(service.root / 'publications', restored.root / 'publications')
    with pytest.raises(ValueError, match='explicitly select'):
        PublicationService(restored).verify(pending['id'])
    result = PublicationService(restored).verify(pending['id'], output=moved)
    assert result['status'] == 'complete' and result['output'] == str(moved)
    moved.write_bytes(b'Later external change')
    with pytest.raises(ValueError, match='receipt'):
        PublicationService(restored).verify(pending['id'])


@pytest.mark.parametrize('name,before,after', [
    ('book.smil', b'npt=0.000000s', b'npt=900.000000s'),
    ('book.smil', b'src="master.mp3"', b'src="https://example.invalid/audio.mp3"'),
    ('book.smil', b'<smil>', b'<!ENTITY unsafe SYSTEM "file:///outside"><smil>'),
    ('book.smil', b'<body>', b'<body xml:base="https://example.invalid/">'),
    ('book.smil', b'<head>', b'<head id="unapproved">'),
    ('book.smil', b'<region id="text-view" />', b'<region id="text-view"><text src="https://example.invalid/" /></region>'),
    ('content.html', b'<head>', b'<head><script>unsafe()</script>'),
    ('ncc.html', b'content="Daisy 2.02"', b'content="Daisy 3"'),
    ('ncc.html', b'book.smil#par-interval-1', b'book.smil#absent'),
    ('source.html', b'Exact &lt;original&gt;', b'Altered &lt;original&gt;'),
    ('provenance.json', b'"resonance-daisy-2.02/1"', b'"other-profile"'),
])
def test_daisy_corrupted_or_external_resource_graph_is_rejected(source_job, tmp_path, name, before, after):
    service, state = source_job
    original = tmp_path / 'original.zip'; export(service, state, original)
    altered = tmp_path / 'altered.zip'
    with ZipFile(original) as source, ZipFile(altered, 'x') as destination:
        for entry in source.infolist():
            raw = source.read(entry)
            if entry.filename == name:
                assert before in raw
                raw = raw.replace(before, after, 1)
            destination.writestr(entry, raw)
    with pytest.raises((ValueError, ET.ParseError)):
        daisy.inspect_daisy(altered)


def test_daisy_final_observer_failure_cannot_change_verified_success(source_job, tmp_path):
    service, state = source_job
    def observer(message):
        if message.startswith('DAISY saved'):
            raise RuntimeError('Synthetic observer failure')
    result = export(service, state, tmp_path / 'finished.zip', progress=observer)
    assert result['status'] == 'complete'
    assert PublicationService(service).verify(result['id']) == result


@pytest.mark.parametrize('seconds,expected', [
    (0, '00:00:00'), (.8, '00:00:00.8'), (9.123456, '00:00:09.123456'),
    (59.9999996, '00:01:00'), (3599.9999996, '01:00:00'),
    (3661.25, '01:01:01.25'), (360000, '100:00:00'),
])
def test_ncc_full_clock_preserves_measured_fraction_and_carries_rounding(seconds, expected):
    assert daisy._ncc_clock(seconds) == expected
