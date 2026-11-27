"""Actual Studio/FFmpeg DAISY interchange and durable publication boundaries."""
from hashlib import sha256
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from carr_library.audio import AudioRuntime
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import atomic_json, read_json
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
        assert all(node.find(ns + 'a').get('href').startswith('book.smil#par-') for node in navigation)
        assert meta['ncc:pageNormal'] == meta['ncc:pageFront'] == meta['ncc:pageSpecial'] == '0'
        smil = ET.fromstring(archive.read('book.smil'))
        intervals = smil.findall('body/seq/par')
        assert len(intervals) == len(provenance['intervals']) > len(state['result']['lines'])
        assert all(par.get('endsync') == 'last' and par.find('text').get('id')
                   and par.find('audio').get('clip-begin').startswith('npt=') for par in intervals)
        audio = tmp_path / 'extracted.mp3'; audio.write_bytes(archive.read('master.mp3'))
    decoded = AudioRuntime(tmp_path, input_roots=(tmp_path,)).inspect(audio)
    assert decoded.codec == 'mp3' and abs(decoded.duration - state['result']['duration']) < .15
    assert original == {name: sha256(Path(name).read_bytes()).hexdigest() for name in original}
    assert service.get(state['id']) == state
    assert PublicationService(service).verify(record['id']) == record
    assert not list(tmp_path.glob('.pending-*'))


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
    assert not cancelled.exists() and not list(tmp_path.glob('.pending-*'))


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
    completed = export(service, state, output)
    assert completed['status'] == 'complete' and completed['receipt_status'] == 'prepared'
    assert completed['warnings'] and completed['artifact']['sha256'] == sha256(output.read_bytes()).hexdigest()
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


@pytest.mark.parametrize('kind', [None, True, {}, 'unsupported'])
def test_saved_publication_kind_is_explicit_and_invalid_kind_cannot_mutate_output(
        source_job, tmp_path, kind):
    service, state = source_job
    target = tmp_path / 'typed-daisy.zip'
    result = export(service, state, target)
    publisher = PublicationService(service)
    path = publisher.receipts / (result['id'] + '.json')
    record = read_json(path)
    record['format'] = kind
    atomic_json(path, record, replace=True)
    before, content = path.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match='Unsupported synchronized publication format'):
        publisher.verify(result['id'])
    with pytest.raises(ValueError, match='Unsupported synchronized publication format'):
        publisher.publications()
    assert path.read_bytes() == before and target.read_bytes() == content


@pytest.mark.parametrize('kind', ('epub', 'daisy-2.02'))
def test_known_publication_kind_cannot_relabel_the_other_archive_graph(
        source_job, tmp_path, kind):
    service, state = source_job
    publisher = PublicationService(service)
    target = tmp_path / ('book.epub' if kind == 'epub' else 'book.zip')
    result = (publisher.export_epub(state['id'], target) if kind == 'epub'
              else export(service, state, target))
    path = publisher.receipts / (result['id'] + '.json')
    record = read_json(path)
    record['format'] = 'daisy-2.02' if kind == 'epub' else 'epub'
    # Use a syntactically valid declaration for the forged format, then select
    # the actual saved bytes explicitly. The archive graph must still reject it.
    record['output'] = str(target.with_suffix('.zip' if kind == 'epub' else '.epub'))
    atomic_json(path, record, replace=True)
    before, content = path.read_bytes(), target.read_bytes()
    with pytest.raises(ValueError, match='unexpected|Unexpected'):
        publisher.verify(result['id'], output=target)
    assert path.read_bytes() == before and target.read_bytes() == content


def test_original_epub_receipt_without_format_still_verifies_without_reencoding(
        source_job, tmp_path, monkeypatch):
    service, state = source_job
    publisher = PublicationService(service)
    target = tmp_path / 'legacy-epub.epub'
    result = publisher.export_epub(state['id'], target)
    path = publisher.receipts / (result['id'] + '.json')
    record = read_json(path)
    del record['format']
    atomic_json(path, record, replace=True)
    original = target.read_bytes()
    monkeypatch.setattr(AudioRuntime, 'convert',
        lambda *args, **kwargs: pytest.fail('Legacy receipt verification must not re-encode.'))
    assert publisher.publications() == (record,)
    verified = publisher.verify(result['id'])
    assert verified['status'] == 'complete' and verified['artifact'] == result['artifact']
    assert 'format' not in verified and read_json(path) == verified
    assert target.read_bytes() == original


def test_daisy_resource_denial_precedes_audio_or_publication_side_effects(
        source_job, tmp_path, monkeypatch):
    from dataclasses import replace
    from carr_library.operations import ResourceUnavailable
    import carr_library.runtime_settings as settings
    service, state = source_job
    service.broker.configure(replace(service.broker.limits, memory_bytes=0))
    monkeypatch.setattr(settings, 'audio_runtime',
        lambda *args, **kwargs: pytest.fail('Denied DAISY publication launched the audio runtime.'))
    target = tmp_path / 'denied-daisy.zip'
    with pytest.raises(ResourceUnavailable):
        export(service, state, target)
    assert not target.exists() and not (service.root / 'publications').exists()
    assert not list(tmp_path.glob('.pending-*'))
    assert service.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('boundary', ('before-link', 'after-link'))
def test_real_daisy_child_exit_uses_same_receipt_recovery_and_preserves_residual_alias(
        source_job, tmp_path, monkeypatch, boundary):
    service, state = source_job
    target, marker = tmp_path / 'native-daisy.zip', tmp_path / 'daisy-child.json'
    originals = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                 for row in state['artifacts']}
    child = r'''
import json, os, sys
from pathlib import Path
from carr_library.studio import publishing
from carr_library.studio.render import ProductionService

root, identity, output, marker, boundary, expected_origin = sys.argv[1:]
assert Path(publishing.__file__).resolve() == Path(expected_origin).resolve()
original = publishing.publish_file
def exit_at_boundary(staged, target, **options):
    if boundary == 'after-link':
        original(staged, target, **options)
        assert os.path.samefile(staged, target)
    Path(marker).write_text(json.dumps({'staged': str(staged)}), encoding='utf-8')
    os._exit(73)
publishing.publish_file = exit_at_boundary
publishing.PublicationService(ProductionService(root)).export_daisy(
    identity, output, publisher='Explicit process fixture publisher', language='uk')
raise AssertionError('The actual publication boundary was not reached.')
'''
    process = subprocess.run([sys.executable, '-B', '-c', child, str(service.root),
        state['id'], str(target), str(marker), boundary, str(Path(publishing.__file__).resolve())],
        cwd=Path(publishing.__file__).resolve().parents[2], capture_output=True,
        text=True, encoding='utf-8', timeout=90, check=False)
    assert process.returncode == 73, (process.returncode, process.stdout, process.stderr)
    observation = json.loads(marker.read_text(encoding='utf-8'))
    publisher = PublicationService(ProductionService(service.root))
    saved, = publisher.publications()
    assert saved['status'] == 'prepared' and saved['format'] == 'daisy-2.02'
    path = publisher.receipts / (saved['id'] + '.json')
    monkeypatch.setattr(AudioRuntime, 'convert',
        lambda *args, **kwargs: pytest.fail('Crash recovery must not encode audio again.'))
    if boundary == 'before-link':
        assert not target.exists()
        with pytest.raises(FileNotFoundError):
            publisher.verify(saved['id'])
        assert read_json(path) == saved
    else:
        staged = Path(observation['staged'])
        assert target.stat().st_nlink == 2 and os.path.samefile(staged, target)
        original = target.read_bytes()
        with pytest.raises(ValueError, match='without links'):
            daisy.inspect_daisy(target)
        verified = publisher.verify(saved['id'])
        assert verified['status'] == 'complete' and verified['format'] == 'daisy-2.02'
        assert verified['artifact'] == saved['artifact']
        assert any('additional hard link' in warning for warning in verified['warnings'])
        persisted = path.read_bytes()
        assert read_json(path)['status'] == 'complete'
        assert target.read_bytes() == original and os.path.samefile(staged, target)
        with staged.open('ab') as stream:
            stream.write(b'Actual mutation through the preserved alias')
        with pytest.raises(ValueError, match='receipt'):
            publisher.verify(saved['id'])
        assert path.read_bytes() == persisted
        assert target.stat().st_nlink == 2 and staged.exists()
    assert originals == {name: sha256(Path(name).read_bytes()).hexdigest() for name in originals}



@pytest.mark.parametrize('kind', ('legacy-epub', 'epub', 'daisy-2.02'))
@pytest.mark.parametrize('final_receipt', ('prepared', 'complete'))
def test_typed_media_backup_retains_each_publication_format_without_external_bytes(
        source_job, tmp_path, monkeypatch, kind, final_receipt):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    service, state = source_job
    app = LibraryApp(service.root.parent.parent)
    workspace = app.create_workspace('Typed publication backup')
    app.add_text(workspace, 'Original evidence', 'Publication metadata does not replace the original source.')
    suffix = '.zip' if kind == 'daisy-2.02' else '.epub'
    target = tmp_path / ('external-publication' + suffix)
    publisher = PublicationService(service)
    original_write = publishing.atomic_json

    def interrupted_finalization(path, record, **options):
        if (final_receipt == 'prepared' and record.get('schema') == 'resonance-publication/1'
                and record.get('status') == 'complete'):
            raise OSError('Synthetic final receipt interruption before typed backup.')
        return original_write(path, record, **options)

    monkeypatch.setattr(publishing, 'atomic_json', interrupted_finalization)
    result = (export(service, state, target) if kind == 'daisy-2.02'
              else publisher.export_epub(state['id'], target))
    assert result['status'] == 'complete'
    monkeypatch.setattr(publishing, 'atomic_json', original_write)
    receipt_path = publisher.receipts / (result['id'] + '.json')
    saved = read_json(receipt_path)
    if kind == 'legacy-epub':
        del saved['format']
        original_write(receipt_path, saved, replace=True)
    assert saved['status'] == final_receipt
    assert publishing.validate_publication_receipt(saved, publication_id=result['id']) == saved

    # The old path no longer exists: typed backup validates only this declaration
    # and must not open or adopt the external publication as a retained input.
    moved = tmp_path / ('owner-selected-existing' + suffix)
    target.rename(moved)
    original = moved.read_bytes()
    backup = tmp_path / 'typed-backup'
    checksum = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    relative = 'studio/publications/' + result['id'] + '.json'
    assert manifest['version'] == 4 and 'extensions' not in manifest
    assert 'state/' + relative in {row['filename'] for row in manifest['sidecars']}
    assert relative in manifest['studio_media']['studio_publications']
    assert {'component': 'state/' + relative, 'kind': 'publication-output',
            'reference': saved['output'], 'included': False} in manifest['studio_media']['required_inputs']
    assert read_json(backup / 'state' / relative) == saved
    assert manifest['generated_audio_included'] is False
    assert not any(path.suffix.lower() in ('.wav', '.mp3', '.epub', '.zip')
                   for path in backup.rglob('*') if path.is_file())
    restored_root = restore_backup(backup, tmp_path / 'restored-typed-library', checksum)
    restored = PublicationService(ProductionService(restored_root / 'state' / 'studio'))
    restored_receipt = restored.receipts / (result['id'] + '.json')
    assert read_json(restored_receipt) == saved
    with pytest.raises(ValueError, match='explicitly select'):
        restored.verify(result['id'])
    shutil.rmtree(state['destination'])
    monkeypatch.setattr(AudioRuntime, 'convert',
        lambda *args, **kwargs: pytest.fail('Restored publication verification must not re-encode.'))
    verified = restored.verify(result['id'], output=moved)
    assert verified['status'] == 'complete' and verified['artifact'] == saved['artifact']
    assert verified.get('format', 'epub') == ('epub' if kind == 'legacy-epub' else kind)
    assert ('format' in verified) is (kind != 'legacy-epub')
    assert moved.read_bytes() == original
    assert LibraryApp(restored_root).search(workspace, 'Publication metadata')[0].quote == 'Publication metadata'


@pytest.mark.parametrize('change', [
    {'format': None}, {'format': True}, {'format': {}}, {'format': 'unsupported'},
    {'format': 'daisy-2.02'}, {'unrecognized_authority': True},
])
def test_typed_publication_declaration_rejects_unknown_or_mismatched_format(
        source_job, tmp_path, change):
    service, state = source_job
    target = tmp_path / 'typed-original.epub'
    publisher = PublicationService(service)
    result = publisher.export_epub(state['id'], target)
    receipt_path = publisher.receipts / (result['id'] + '.json')
    raw, output = receipt_path.read_bytes(), target.read_bytes()
    altered = dict(read_json(receipt_path), **change)
    with pytest.raises(ValueError):
        publishing.validate_publication_receipt(altered, publication_id=result['id'])
    assert receipt_path.read_bytes() == raw and target.read_bytes() == output


@pytest.mark.parametrize('operation', ('replay', 'epub', 'daisy-2.02'))
def test_current_lrc_receipt_is_required_before_replay_or_publication(source_job, tmp_path, monkeypatch, operation):
    service, state = source_job
    assert state['artifact_version'] == 2
    lrc = Path(state['destination']) / 'script.lrc'
    original_lrc = lrc.read_bytes()
    state['artifacts'] = [row for row in state['artifacts'] if row['path'] != str(lrc)]
    path = service.jobs_root / state['id'] / 'job.json'
    atomic_json(path, state, replace=True)
    before = path.read_bytes()
    monkeypatch.setattr(AudioRuntime, 'inspect',
        lambda *args, **kwargs: pytest.fail('Incomplete receipt must not reach a native decoder.'))
    target = tmp_path / ('incomplete.epub' if operation == 'epub' else 'incomplete.zip')
    with pytest.raises(ValueError, match='missing required artifacts'):
        if operation == 'replay':
            service.run(state['id'])
        elif operation == 'epub':
            PublicationService(service).export_epub(state['id'], target)
        else:
            export(service, state, target)
    assert not target.exists() and path.read_bytes() == before
    assert lrc.read_bytes() == original_lrc


@pytest.mark.parametrize('change', [
    {'artifact_version': True}, {'artifact_version': 3}, {'artifact_version': '2'},
    {'warnings': 'not a list'}, {'warnings': [None]}, {'warnings': ['x' * 1801]},
    {'warnings': ['warning'] * 1001},
])
def test_typed_backup_rejects_unknown_artifact_contract_or_unbounded_warnings(source_job, tmp_path, change):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup
    service, state = source_job
    app = LibraryApp(service.root.parent.parent)
    path = service.jobs_root / state['id'] / 'job.json'
    altered = dict(state, **change)
    atomic_json(path, altered, replace=True)
    before = path.read_bytes()
    if 'artifact_version' in change:
        with pytest.raises(ValueError, match='artifact version'):
            service.get(state['id'])
        with pytest.raises(ValueError, match='artifact version'):
            service.run(state['id'])
    target = tmp_path / 'invalid-backup'
    with pytest.raises(ValueError):
        create_backup(app, target)
    assert path.read_bytes() == before and not target.exists()
