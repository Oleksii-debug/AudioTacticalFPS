"""Actual canonical snapshots, retained originals and typed recovery through deltas."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_incremental as incremental
from carr_library.backup_incremental import (BackupReference, create_incremental_backup,
    verify_incremental_backup, restore_incremental_backup)
from carr_library.library_backup import create_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def bytes_in(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')) + '\n').encode('utf-8')


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Incremental recovery')
    app.add_text(workspace, 'Exact retained original',
                 'Незмінний оригінал. Source evidence.\n' * 12000, edition_id='original')
    document = ReadingDocument('Original reading copy. 😀')
    drafts = ReaderDraftStore(app.root)
    drafts.save(ReaderResume(document, 'missing:exact-voice', 1, 67, 0, 0))
    base = tmp_path / 'full'
    digest = create_backup(app, base)
    return app, workspace, document, drafts, BackupReference(base, digest)


def changed(source, tmp_path, *, name='delta'):
    app, workspace, document, drafts, base = source
    document.set_text('Revised reading copy. 😀')
    document.add_bookmark('Reviewed location', 8)
    drafts.save(ReaderResume(document, 'missing:exact-voice', 1, 67, 8, 2.5))
    receipt = create_incremental_backup(app, tmp_path / name, base=base)
    return receipt


def test_changed_only_storage_restores_after_actual_source_deletion_without_execution(
        source, tmp_path, monkeypatch):
    app, workspace, document, drafts, base = source
    base_bytes = bytes_in(base.path)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Passive backup started a process.'))
    receipt = changed(source, tmp_path)
    layer = json.loads((receipt.path / 'manifest.json').read_bytes())
    assert all(not name.startswith('originals/') for name in layer['stored'])
    assert drafts.path.relative_to(app.root).as_posix() in layer['stored']
    assert receipt.stored_files < receipt.total_files
    assert receipt.stored_bytes < receipt.logical_bytes
    assert bytes_in(base.path) == base_bytes
    assert all(path.stat().st_nlink == 1 for path in base.path.rglob('*') if path.is_file())
    assert all(path.stat().st_nlink == 1 for path in receipt.path.rglob('*') if path.is_file())
    proof = verify_incremental_backup(receipt.path, receipt.manifest_sha256, bases=(base,))
    assert proof['chain'] == [receipt.manifest_sha256, base.sha256]
    assert proof['snapshot_sha256'] == receipt.snapshot_sha256
    expected_draft = document.to_dict()
    shutil.rmtree(app.root)
    destination = restore_incremental_backup(receipt.path, tmp_path / 'restored',
                                             receipt.manifest_sha256, bases=(base,))
    restored = LibraryApp(destination)
    assert restored.edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir()} == {
        name.split('/', 1)[1]: raw for name, raw in base_bytes.items() if name.startswith('originals/')}
    resume = ReaderDraftStore(destination).load()
    assert resume.document.to_dict() == expected_draft
    assert (resume.voice_id, resume.cursor, resume.audio_seconds) == ('missing:exact-voice', 8, 2.5)
    assert resume.document.bookmark_position(document.bookmarks[0].id) == 8
    assert resume.document.undo() and resume.document.text == document.source_text
    assert bytes_in(base.path) == base_bytes


def test_multiple_layers_keep_explicit_parent_identity_and_latest_database(source, tmp_path):
    app, workspace, document, drafts, base = source
    first = changed(source, tmp_path)
    first_ref = BackupReference(first.path, first.manifest_sha256)
    app.add_text(workspace, 'New exact original', 'A later original.', edition_id='later')
    second = create_incremental_backup(app, tmp_path / 'second', base=first_ref, ancestors=(base,))
    before = bytes_in(base.path), bytes_in(first.path)
    with pytest.raises(ValueError, match='required base'):
        restore_incremental_backup(second.path, tmp_path / 'missing-base', second.manifest_sha256,
                                   bases=(base,))
    assert not (tmp_path / 'missing-base').exists()
    root = restore_incremental_backup(second.path, tmp_path / 'restored', second.manifest_sha256,
                                     bases=(base, first_ref))
    restored = LibraryApp(root)
    assert restored.edition_metadata(workspace, 'later').title == 'New exact original'
    assert restored.edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert (bytes_in(base.path), bytes_in(first.path)) == before


@pytest.mark.parametrize('damage', ['stored', 'inherited', 'snapshot', 'manifest', 'extra', 'missing', 'hardlink'])
def test_corrupt_required_bytes_never_publish_a_restored_library(source, tmp_path, damage):
    app, _, _, _, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    if damage == 'stored':
        path = receipt.path / 'files' / descriptor['stored'][0]
        path.write_bytes(b'changed')
    elif damage == 'inherited':
        path = next((base.path / 'originals').iterdir())
        path.write_bytes(b'changed')
    elif damage == 'snapshot':
        (receipt.path / 'snapshot.json').write_bytes(b'{}')
    elif damage == 'manifest':
        (receipt.path / 'manifest.json').write_bytes(b'{}')
    elif damage == 'extra':
        (receipt.path / 'unexpected').write_bytes(b'unreviewed')
    elif damage == 'missing':
        (receipt.path / 'files' / descriptor['stored'][0]).unlink()
    else:
        os.link(receipt.path / 'snapshot.json', tmp_path / 'shared-snapshot')
    before = bytes_in(base.path), bytes_in(receipt.path)
    destination = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, OSError)):
        restore_incremental_backup(receipt.path, destination, receipt.manifest_sha256, bases=(base,))
    assert not destination.exists()
    assert (bytes_in(base.path), bytes_in(receipt.path)) == before


def test_resigned_invalid_typed_reader_data_is_still_rejected(source, tmp_path):
    app, _, _, drafts, base = source
    receipt = changed(source, tmp_path)
    name = drafts.path.relative_to(app.root).as_posix()
    path = receipt.path / 'files' / name
    document = json.loads(path.read_bytes())
    document['execute'] = 'never accepted'
    raw = encoded(document)
    path.write_bytes(raw)
    snapshot = json.loads((receipt.path / 'snapshot.json').read_bytes())
    row = next(item for item in snapshot['sidecars'] if item['filename'] == name)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    snapshot_raw = encoded(snapshot)
    (receipt.path / 'snapshot.json').write_bytes(snapshot_raw)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    descriptor['snapshot_sha256'] = sha256(snapshot_raw).hexdigest()
    manifest_raw = encoded(descriptor)
    (receipt.path / 'manifest.json').write_bytes(manifest_raw)
    with pytest.raises(ValueError):
        restore_incremental_backup(receipt.path, tmp_path / 'rejected',
            sha256(manifest_raw).hexdigest(), bases=(base,))
    assert not (tmp_path / 'rejected').exists()
    assert path.read_bytes() == raw


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'files/../../escape', 'C:/outside'])
def test_resigned_traversal_cannot_read_or_publish_outside_the_layer(source, tmp_path, name):
    _, _, _, _, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    descriptor['stored'] = [name]
    raw = encoded(descriptor)
    (receipt.path / 'manifest.json').write_bytes(raw)
    sentinel = tmp_path / 'outside'
    sentinel.write_bytes(b'Keep outside bytes')
    with pytest.raises(ValueError):
        restore_incremental_backup(receipt.path, tmp_path / 'rejected', sha256(raw).hexdigest(), bases=(base,))
    assert sentinel.read_bytes() == b'Keep outside bytes'
    assert not (tmp_path / 'rejected').exists()


def test_chain_bound_prevents_creation_of_an_unrestorable_new_layer(source, tmp_path, monkeypatch):
    app, _, _, _, base = source
    monkeypatch.setattr(incremental, 'MAX_CHAIN', 1)
    first = changed(source, tmp_path)
    selected = BackupReference(first.path, first.manifest_sha256)
    with pytest.raises(ValueError, match='new full backup'):
        create_incremental_backup(app, tmp_path / 'too-deep', base=selected, ancestors=(base,))
    assert not (tmp_path / 'too-deep').exists()
    assert verify_incremental_backup(first.path, first.manifest_sha256, bases=(base,))['chain'] == [
        first.manifest_sha256, base.sha256]


def test_selected_checksum_and_ancestor_identity_are_required(source, tmp_path):
    app, _, _, _, base = source
    with pytest.raises(ValueError, match='digest mismatch'):
        create_incremental_backup(app, tmp_path / 'bad-base',
                                  base=BackupReference(base.path, '0' * 64))
    with pytest.raises(ValueError, match='only once'):
        create_incremental_backup(app, tmp_path / 'duplicate', base=base, ancestors=(base, base))
    assert not (tmp_path / 'bad-base').exists() and not (tmp_path / 'duplicate').exists()


@pytest.mark.parametrize('phase', ['before', 'copy', 'space'])
def test_cancel_or_insufficient_space_keeps_bases_and_publishes_nothing(source, tmp_path, monkeypatch, phase):
    app, _, document, drafts, base = source
    document.set_text('A changed draft.')
    drafts.save(ReaderResume(document, '', 0, 80, 0, 0))
    before = bytes_in(base.path)
    stopped = phase == 'before'
    original_copy = incremental._copy
    def copy(*args, **kwargs):
        nonlocal stopped
        result = original_copy(*args, **kwargs)
        stopped = True
        return result
    if phase == 'copy':
        monkeypatch.setattr(incremental, '_copy', copy)
    elif phase == 'space':
        monkeypatch.setattr(incremental.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=0))
    with pytest.raises((ValueError, InterruptedError, OSError)):
        create_incremental_backup(app, tmp_path / 'cancelled', base=base, cancelled=lambda: stopped)
    assert not (tmp_path / 'cancelled').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


def test_competing_destination_is_never_overwritten(source, tmp_path, monkeypatch):
    app, _, _, _, base = source
    destination = tmp_path / 'destination'
    publish = incremental._publish
    def competing(stage, selected, stored):
        selected.mkdir()
        (selected / 'keep').write_bytes(b'Competitor owns this destination.')
        return publish(stage, selected, stored)
    monkeypatch.setattr(incremental, '_publish', competing)
    with pytest.raises(FileExistsError):
        create_incremental_backup(app, destination, base=base)
    assert bytes_in(destination) == {'keep': b'Competitor owns this destination.'}


def test_cli_create_verify_restore_reuses_the_same_canonical_services(source, tmp_path, capsys):
    from carr_library.backup_incremental_cli import main
    app, workspace, _, _, base = source
    layer = tmp_path / 'cli-layer'
    assert main(['create', '--data-dir', str(app.root), '--output', str(layer),
                 '--base', str(base.path), base.sha256]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['path'] == str(layer) and receipt['base_required'] is True
    common = ['--source', str(layer), '--sha256', receipt['manifest_sha256'],
              '--base', str(base.path), base.sha256]
    assert main(['verify', *common]) == 0
    assert json.loads(capsys.readouterr().out)['chain'] == [receipt['manifest_sha256'], base.sha256]
    destination = tmp_path / 'cli-restored'
    assert main(['restore', *common, '--output', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['restored_root'] == str(destination)
    assert LibraryApp(destination).edition_metadata(workspace, 'original').title == 'Exact retained original'
    assert main(['restore', *common, '--output', str(destination)]) == 1
    assert 'Incremental backup:' in capsys.readouterr().err



def test_base_corruption_during_current_snapshot_work_is_detected_before_publication(
        source, tmp_path, monkeypatch):
    app, _, document, drafts, base = source
    document.set_text('Changed draft to ensure an actual delta copy.')
    drafts.save(ReaderResume(document, '', 0, 80, 0, 0))
    original_copy = incremental._copy
    damaged = False
    def copy(*args, **kwargs):
        nonlocal damaged
        result = original_copy(*args, **kwargs)
        if not damaged:
            damaged = True
            original = next((base.path / 'originals').iterdir())
            original.write_bytes(b'External modification during backup.')
        return result
    monkeypatch.setattr(incremental, '_copy', copy)
    with pytest.raises(ValueError):
        create_incremental_backup(app, tmp_path / 'rejected', base=base)
    assert damaged and not (tmp_path / 'rejected').exists()
    assert not list(tmp_path.glob('.carr-incremental-*'))
