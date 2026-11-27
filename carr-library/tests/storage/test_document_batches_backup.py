"""Real common4 recovery of document batches, without running saved actions."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from carr_library import backup_sidecars
from carr_library.application import LibraryApp
from carr_library.audio.cleanup import CleanupOwner, retained_state_lock
from carr_library.audio.models import AudioCleanupPending
from carr_library.conversion import batches, planning
from carr_library.conversion.batches import DocumentBatches
from carr_library.conversion.planning import SplitOptions
from carr_library.conversion.service import ConversionService
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.source import read_source_range
from carr_library.state_backup import snapshot_state
from carr_library.studio.render import ProductionService


PREFIX = 'state/conversion/batches/'
TEXT = 'Exact Ukrainian: оригінал.\r\n\r\nSecond part remains exact.\n\nLast part.'


def selected_batch(tmp_path, *, kind='reading-copy', complete=True):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Local batch library')
    service = DocumentBatches(app.root)
    source = tmp_path / 'selected.txt'
    if kind == 'document':
        source.write_bytes(TEXT.encode('utf-8'))
        identity = service.create([source])
        expected = TEXT
    else:
        # The imported reading copy really belongs to a different library.
        # Keeping its source map is not authority over this backup's catalog.
        foreign = LibraryApp(tmp_path / 'foreign-library')
        workspace = foreign.create_workspace('Explicit imported source')
        imported = foreign.add_text(workspace, 'Imported source', TEXT)
        document = read_source_range(
            ReadingDesk(foreign, workspace, imported['edition']), 1, 1).reading_document()
        document.replace(0, 5, 'Reviewed')
        source = tmp_path / 'selected.reading.json'
        document.save(source)
        identity = service.create_reading_copies([source])
        expected = document.text
    service.prepare(identity)
    output = tmp_path / 'external-output'
    output.mkdir()
    service.approve(service.preview(identity, output, SplitOptions('blank-lines')))
    if complete:
        assert service.run(identity)['status'] == 'complete'
    return app, service, identity, source, output, expected


def forbid_active_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Backup may validate saved rows, but must not activate a saved action or runtime.')
    monkeypatch.setattr(ConversionService, '__init__', forbidden)
    monkeypatch.setattr(ProductionService, '__init__', forbidden)
    monkeypatch.setattr(planning, 'plan_split', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def batch_files(root, identity):
    prefix = Path(PREFIX)
    result = {str(prefix / (identity + '.json')).replace('\\', '/'):
              (root / prefix / (identity + '.json')).read_bytes()}
    control = root / prefix / (identity + '.control.json')
    if control.exists():
        result[control.relative_to(root).as_posix()] = control.read_bytes()
    for path in (root / prefix / identity).rglob('*'):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


@pytest.mark.parametrize('kind', ('document', 'reading-copy'))
def test_actual_batch_common4_restore_is_passive_and_requires_new_output_review(
        tmp_path, monkeypatch, kind):
    app, service, identity, source, output, expected = selected_batch(tmp_path, kind=kind)
    before = batch_files(app.root, identity)
    old_outputs = {path.name: path.read_bytes() for path in output.iterdir()}
    backup = tmp_path / 'backup'
    with monkeypatch.context() as passive:
        forbid_active_services(passive)
        checksum = create_backup(app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        assert manifest['version'] == 4
        selected = {entry['filename'] for entry in manifest['sidecars']
                    if entry['filename'].startswith(PREFIX)}
        assert selected == set(before)
        assert all((backup / name).read_bytes() == raw for name, raw in before.items())
        # Historical source selection and output paths are no longer available.
        source.unlink()
        shutil.rmtree(output)
        restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert batch_files(restored_root, identity) == before
    restored = DocumentBatches(restored_root)
    assert restored.get(identity)['status'] == 'waiting-input'
    with pytest.raises(ValueError, match='Restored outputs'):
        restored.run(identity)
    assert not output.exists()
    replacement = tmp_path / 'explicit-new-output'
    replacement.mkdir()
    preview = restored.preview(identity, replacement, SplitOptions('blank-lines'))
    restored.approve(preview)
    assert restored.run(identity)['status'] == 'complete'
    state = restored.get(identity)
    assert len(state['history']) == 1 and state['history'][0]['outputs']
    actual = b''.join((replacement / row['name']).read_bytes() for row in state['outputs'])
    assert actual.decode('utf-8') == expected
    assert len(old_outputs) == len(state['outputs']) * 2
    assert not output.exists()
    for row in state['outputs']:
        receipt = json.loads((replacement / row['provenance']['name']).read_bytes())
        assert receipt['quotation_authority'] is False
    # Neither restore nor a derivative export changes the current catalog.
    assert restored_root != app.root and batch_files(app.root, identity) == before


@pytest.mark.parametrize('declaration', ('/owner/previous-library', r'C:\Owner\Previous-Library'))
def test_document_batch_foreign_host_paths_are_inert_portable_declarations(
        tmp_path, monkeypatch, declaration):
    app, service, identity, _, output, _ = selected_batch(tmp_path)
    record = read_json(service.store.path(identity))
    record['library_root'] = declaration
    atomic_json(service.store.path(identity), record, replace=True)
    raw = service.store.path(identity).read_bytes()
    shutil.rmtree(output)
    with monkeypatch.context() as passive:
        forbid_active_services(passive)
        checksum = create_backup(app, tmp_path / 'backup')
        restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    resumed = DocumentBatches(restored)
    assert resumed.store.path(identity).read_bytes() == raw
    assert resumed.get(identity)['status'] == 'waiting-input'
    with pytest.raises(ValueError, match='Restored outputs'):
        resumed.run(identity)


def resign_archive(backup):
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    for entry in manifest['sidecars']:
        raw = (backup / entry['filename']).read_bytes()
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    payload = encode(manifest)
    manifest_path.write_bytes(payload)
    return sha256(payload).hexdigest()


@pytest.mark.parametrize('damage', ('source-bytes', 'reading-bytes', 'review-ranges', 'unowned-attempt'))
def test_resigned_batch_cannot_bypass_its_actual_retained_graph(tmp_path, damage):
    app, service, identity, _, _, _ = selected_batch(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    job_path = backup / (PREFIX + identity + '.json')
    record = read_json(job_path)
    row = record['sources'][0]
    if damage == 'source-bytes':
        path = backup / (PREFIX + row['path'])
        path.write_bytes(path.read_bytes().replace(b'Exact', b'Other'))
    elif damage == 'reading-bytes':
        path = backup / (PREFIX + row['prepared']['reading'])
        document = ReadingDocument.from_dict(read_json(path))
        document.replace(0, 1, 'X')
        path.write_bytes(encode(document.to_dict()))
    elif damage == 'review-ranges':
        record['review']['plans'][0]['parts'][0]['start'] = 1
        record['review_sha256'] = sha256(encode(record['review'])).hexdigest()
        job_path.write_bytes(encode(record))
    else:
        name = PREFIX + identity + '/derived/0001/' + 'a' * 32 + '.txt'
        path = backup / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not owned by the declared attempt')
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        manifest['sidecars'].append(dict(filename=name, size_bytes=path.stat().st_size,
                                        sha256=sha256(path.read_bytes()).hexdigest()))
        manifest['sidecars'].sort(key=lambda entry: entry['filename'])
        (backup / 'manifest.json').write_bytes(encode(manifest))
    checksum = resign_archive(backup)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(backup, destination, checksum)
    assert not destination.exists()


@pytest.mark.parametrize('bad', (
    'unknown-field', 'foreign-input', 'undeclared-attempt', 'missing-prepared-artifact',
    'completion-count', 'unknown-sibling',
))
def test_invalid_document_state_blocks_complete_backup_without_omission(tmp_path, bad):
    app, service, identity, _, _, _ = selected_batch(tmp_path)
    path = service.store.path(identity)
    record = read_json(path)
    if bad == 'unknown-field':
        record['execute_command'] = ['unexpected']
    elif bad == 'foreign-input':
        record['sources'][0]['path'] = 'b' * 32 + '/inputs/0001.reading.json'
    elif bad == 'undeclared-attempt':
        record['sources'][0]['attempt'] = 'a' * 32
    elif bad == 'missing-prepared-artifact':
        record['sources'][0]['prepared']['artifacts'].pop()
    elif bad == 'completion-count':
        record['outputs'].pop()
    else:
        (service.store.directory / 'unrecognized-state.json').write_bytes(b'{}')
    atomic_json(path, record, replace=True)
    before = path.read_bytes()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and path.read_bytes() == before


@pytest.mark.parametrize('retained_pair', (0, 1, 2))
def test_unacknowledged_active_conversion_files_remain_uncompleted_after_restore(
        tmp_path, retained_pair):
    app, service, identity, _, _, _ = selected_batch(tmp_path, kind='document', complete=False)
    record = read_json(service.store.path(identity))
    row = record['sources'][0]
    converted = next(item['path'] for item in row['prepared']['artifacts']
                     if item['path'].endswith('.txt'))
    for index, relative in enumerate((converted, converted + '.provenance.json')):
        if index >= retained_pair:
            (service.store.directory / relative).unlink()
    # Model the private metadata checkpoint immediately before preparation was
    # acknowledged. These exact artifacts are data, never completion authority.
    row['prepared'] = None
    record.update(status='preparing', review=None, review_sha256=None,
                  outputs=[], history=[], message='Preparation was not acknowledged.')
    atomic_json(service.store.path(identity), record, replace=True)
    before = batch_files(app.root, identity)
    checksum = create_backup(app, tmp_path / 'backup')
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert batch_files(restored, identity) == before
    state = DocumentBatches(restored).get(identity)
    assert state['status'] == 'preparing' and state['sources'][0]['prepared'] is None
    assert state['outputs'] == [] and state['review'] is None


@pytest.mark.parametrize('target', ('common4', 'historical-state'))
def test_pending_converter_owner_excludes_snapshot_until_its_exact_lock_releases(
        tmp_path, target):
    app, service, identity, _, _, _ = selected_batch(tmp_path)
    ready = [False]
    owner = CleanupOwner(lambda: ready[0])
    pending = AudioCleanupPending('Synthetic retained owner', cleanup_owner=owner)
    try:
        with pytest.raises(AudioCleanupPending) as caught:
            with retained_state_lock(service.root, 'document-batches'):
                raise pending
        assert caught.value is pending and owner.close() is False
        destination = tmp_path / 'rejected'
        with pytest.raises(RuntimeError, match='already being changed'):
            if target == 'common4':
                create_backup(app, destination)
            else:
                snapshot_state(app.root / 'state', destination)
        assert not destination.exists()
    finally:
        ready[0] = True
        assert owner.close() is True
    checksum = create_backup(app, tmp_path / 'after-quiescence')
    assert checksum and service.get(identity)['status'] == 'complete'


def test_actual_batch_control_change_invalidates_snapshot(tmp_path, monkeypatch):
    app, service, identity, _, _, _ = selected_batch(tmp_path)
    name = PREFIX + identity + '.json'
    original, changed = backup_sidecars._write, []

    def control_after_job_copy(stage, relative, raw):
        original(stage, relative, raw)
        if relative == name:
            assert not changed
            service.pause(identity)
            changed.append(True)

    monkeypatch.setattr(backup_sidecars, '_write', control_after_job_copy)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, destination)
    assert changed == [True] and not destination.exists()
    assert service._control(identity) == 'pause'
