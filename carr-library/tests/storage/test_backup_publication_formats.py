"""Typed format-4 backup of DAISY receipts and measured LRC checkpoints.

Actual FFmpeg fixtures exercise full create/verify/restore. Restored external
publication paths remain declarations until a person explicitly selects bytes.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.studio.clips import ClipEditor
from carr_library.studio.publishing import PublicationService, validate_publication_receipt
from carr_library.studio.render import ProductionService

_spec = importlib.util.spec_from_file_location('publication_backup_fixtures',
    Path(__file__).parents[1] / 'studio' / 'test_publishing.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
actual_job, source_job, saved_receipt = (
    _fixtures.actual_job, _fixtures.source_job, _fixtures.saved_receipt)


@pytest.mark.parametrize('book_format', [None, 'epub', 'daisy-2.02'])
def test_typed_publication_receipt_is_passive_and_keeps_explicit_relocated_names(
        saved_receipt, monkeypatch, book_format):
    from carr_library.studio import publishing
    if book_format is not None:
        saved_receipt['format'] = book_format
    saved_receipt['output'] = 'D:\\Retained\\explicitly-renamed.archive'
    before = deepcopy(saved_receipt)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup validation accessed a historical external path.')
    monkeypatch.setattr(publishing, 'local_path', forbidden)
    monkeypatch.setattr(publishing, 'regular', forbidden)
    assert validate_publication_receipt(saved_receipt) is saved_receipt
    assert saved_receipt == before


@pytest.mark.parametrize('book_format', ['epub', 'daisy-2.02'])
def test_complete_backup_restores_publication_receipt_without_old_output_authority(
        source_job, tmp_path, monkeypatch, book_format):
    service, state = source_job
    app = LibraryApp(service.root.parent.parent)
    app.create_workspace('Typed publication checkpoint')
    output = tmp_path / ('selected.epub' if book_format == 'epub' else 'selected.zip')
    publisher = PublicationService(service, private_root=app.root)
    if book_format == 'epub':
        receipt = publisher.export_epub(state['id'], output, language='uk')
    else:
        receipt = publisher.export_daisy(state['id'], output, language='uk',
                                        publisher='Explicit synthetic backup publisher')
    lrc = service.jobs_root / state['id'] / 'script.lrc'
    before_lrc = lrc.read_bytes()
    assert before_lrc and 'lrc_notice' in state['result']
    snapshot = tmp_path / 'full-backup'
    identity = create_backup(app, snapshot)
    assert read_json(snapshot / 'manifest.json')['version'] == 4
    verify_backup(snapshot, identity)
    new_root = restore_backup(snapshot, tmp_path / 'restored-library', identity)
    assert (new_root / 'state' / 'studio' / 'jobs' / state['id'] / 'script.lrc').read_bytes() == before_lrc
    selected = tmp_path / 'explicitly-relocated.archive'
    output.rename(selected)
    original_bytes = selected.read_bytes()
    old_library, old_output = app.root, Path(state['destination'])
    shutil.rmtree(old_library)
    shutil.rmtree(old_output)
    restored_production = ProductionService(new_root / 'state' / 'studio')
    restored_production.runtime_factory = lambda *a, **k: pytest.fail('Receipt restore started an audio engine.')
    restored = PublicationService(restored_production, private_root=new_root)
    original_open = Path.open
    def no_old_paths(path, *args, **kwargs):
        if path == old_library or path.is_relative_to(old_library) or path == old_output or path.is_relative_to(old_output):
            pytest.fail('Restored receipt reopened an old library/output path.')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', no_old_paths)
    record, = restored.publications()
    assert record == receipt
    with pytest.raises(ValueError, match='explicitly select'):
        restored.verify(receipt['id'])
    verified = restored.verify(receipt['id'], output=selected)
    assert verified['artifact'] == receipt['artifact'] and verified['source'] == receipt['source']
    assert verified.get('format', 'epub') == book_format
    assert selected.read_bytes() == original_bytes
    assert restored.verify(receipt['id']) == verified
    assert not list(new_root.rglob('*.epub')) and not list(new_root.rglob('*.zip'))


def test_audio_only_empty_lrc_and_managed_original_survive_complete_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Retained untranscribed clip')
    editor = ClipEditor(app.root)
    project = editor.create('Explicitly untranscribed audio')
    selected = tmp_path / 'selected.wav'
    _fixtures.ToneVoices().synthesize('', 'synthetic-tone', selected,
        settings=None, cancelled=lambda: False)
    original = selected.read_bytes()
    project = editor.add(project, project.scenes[0].id, selected, role='music', duration=.2)
    selected.unlink()
    output = tmp_path / 'output'; output.mkdir()
    job = editor.service.submit(project, output, stems=False)
    result = editor.service.run(job)
    assert result['status'] == 'complete' and result['result']['lines'] == []
    lrc = editor.service.jobs_root / job / 'script.lrc'
    assert lrc.read_bytes() == b''
    snapshot = tmp_path / 'snapshot'
    identity = create_backup(app, snapshot)
    verify_backup(snapshot, identity)
    restored = restore_backup(snapshot, tmp_path / 'restored', identity)
    retained = restored / 'state' / 'studio' / 'inputs' / (sha256(original).hexdigest() + '.audio')
    assert retained.read_bytes() == original
    assert (restored / 'state' / 'studio' / 'jobs' / job / 'script.lrc').read_bytes() == b''
    assert not (restored / 'state' / 'studio' / '.local-origin.json').exists()
    restored_service = ProductionService(restored / 'state' / 'studio')
    def forbidden(*args, **kwargs):
        pytest.fail('A restored portable job must require output approval before audio execution.')
    restored_service.runtime_factory = forbidden
    with pytest.raises(ValueError, match='output'):
        restored_service.run(job)


@pytest.mark.parametrize('damage', ['format', 'notice'])
def test_invalid_new_publication_state_refuses_backup_before_manifest(
        source_job, tmp_path, damage):
    service, state = source_job
    app = LibraryApp(service.root.parent.parent)
    app.create_workspace('Invalid publication must be visible')
    publisher = PublicationService(service, private_root=app.root)
    receipt = publisher.export_daisy(state['id'], tmp_path / 'valid.zip',
                                    publisher='Explicit synthetic publisher')
    if damage == 'format':
        receipt['format'] = 'unrecognized-daisy-edition'
        atomic_json(publisher.receipts / (receipt['id'] + '.json'), receipt, replace=True)
    else:
        job = read_json(service.jobs_root / state['id'] / 'job.json')
        job['result']['lrc_notice'] = {'unapproved': 'state'}
        atomic_json(service.jobs_root / state['id'] / 'job.json', job, replace=True)
    output = tmp_path / 'refused-backup'
    with pytest.raises(RestoreError):
        create_backup(app, output)
    assert not (output / 'manifest.json').exists()
