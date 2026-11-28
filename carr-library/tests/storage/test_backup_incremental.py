"""Actual canonical snapshots, retained originals and typed recovery through deltas."""
from dataclasses import replace
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
from carr_library.backup_budget import BackupBudget, MIB, GIB
from carr_library.reader.reading_state import ReadingLocation, ReadingState
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
    with app.read(workspace) as db:
        row = db.execute('SELECT r.id,r.asset,p.digest FROM revision r '
                         'JOIN page p ON p.revision=r.id '
                         'WHERE r.workspace=? AND r.edition=? AND p.ordinal=1',
                         (workspace, 'original')).fetchone()
    assert row is not None
    ReadingState(app.root).add_bookmark(ReadingLocation(workspace, 'original', row[0],
        'logical_segment', 1, row[2], row[1]), 'Reviewed location')
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
    expected_bookmarks = ReadingState(app.root).bookmarks(workspace, 'original')
    assert len(expected_bookmarks) == 1
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
    assert ReadingState(destination).bookmarks(workspace, 'original') == expected_bookmarks
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
    def competing(stage, selected, stored, cancelled):
        selected.mkdir()
        (selected / 'keep').write_bytes(b'Competitor owns this destination.')
        return publish(stage, selected, stored, cancelled)
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


@pytest.mark.parametrize('operation', ['create', 'recheck'])
def test_full_base_budget_is_checked_before_any_payload_verification(source, tmp_path, monkeypatch, operation):
    app, _, _, _, base = source
    # This is an actual complete backup with its independently retained digest.
    # A one-byte budget cannot admit its real database and retained originals.
    before = bytes_in(base.path)
    def forbidden(*args, **kwargs):
        pytest.fail('The over-budget backup reached payload verification.')
    monkeypatch.setattr(incremental.full, 'verify_backup', forbidden)
    with pytest.raises(ValueError, match='budget'):
        if operation == 'create':
            create_incremental_backup(app, tmp_path / 'over-budget', base=base, max_bytes=1)
        else:
            incremental._recheck_chain((base,), lambda: False, 1,
                incremental._Workspace(BackupBudget()))
    assert not (tmp_path / 'over-budget').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


def selected_policy(**changes):
    return replace(BackupBudget(memory_bytes=GIB, staging_bytes=128*MIB, seconds=300), **changes)


def test_eight_actual_layers_restore_and_a_ninth_is_refused_before_new_snapshot(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Finite actual chain')
    assert app.add_text(workspace, 'One retained source', 'Exact source.', edition_id='one')['state'] == 'complete'
    document = ReadingDocument('Reading source.')
    drafts = ReaderDraftStore(app.root)
    drafts.save(ReaderResume(document))
    full = tmp_path / 'base'
    base = BackupReference(full, create_backup(app, full))
    parent, ancestors = base, ()
    selected = selected_policy()
    for index in range(8):
        document.set_text('Reviewed copy ' + str(index))
        drafts.save(ReaderResume(document))
        receipt = create_incremental_backup(app, tmp_path / ('layer-' + str(index)),
            base=parent, ancestors=ancestors, backup_budget=selected)
        ancestors = (*ancestors, parent)
        parent = BackupReference(receipt.path, receipt.manifest_sha256)
    assert len(ancestors) == 8
    proof = verify_incremental_backup(parent.path, parent.sha256, bases=ancestors,
                                       backup_budget=selected)
    assert len(proof['chain']) == 9 and proof['chain'][-1] == base.sha256
    target = restore_incremental_backup(parent.path, tmp_path / 'restored', parent.sha256,
                                         bases=ancestors, backup_budget=selected)
    assert ReaderDraftStore(target).load().document.to_dict() == document.to_dict()
    def no_ninth_snapshot(*args, **kwargs):
        pytest.fail('A ninth generation reached the full-backup writer.')
    monkeypatch.setattr(incremental.full, 'create_backup', no_ninth_snapshot)
    with pytest.raises(ValueError, match='new full backup'):
        create_incremental_backup(app, tmp_path / 'ninth', base=parent, ancestors=ancestors,
                                  backup_budget=selected)
    assert not (tmp_path / 'ninth').exists()


def test_changed_deleted_and_unchanged_passive_files_project_the_latest_exact_inventory(
        source, tmp_path):
    app, workspace, _, drafts, base = source
    obsolete = ReaderDraftStore(app.root, slot='obsolete')
    obsolete.save(ReaderResume(ReadingDocument('This draft will be removed.')))
    first = create_incremental_backup(app, tmp_path / 'first', base=base,
                                      backup_budget=selected_policy())
    first_ref = BackupReference(first.path, first.manifest_sha256)
    removed_name = obsolete.path.relative_to(app.root).as_posix()
    obsolete.path.unlink()
    drafts.save(ReaderResume(ReadingDocument('A new current draft.')))
    assert app.add_text(workspace, 'Additional original', 'Later bytes.',
                        edition_id='second')['state'] == 'complete'
    second = create_incremental_backup(app, tmp_path / 'second', base=first_ref,
                                       ancestors=(base,), backup_budget=selected_policy())
    snapshot = json.loads((second.path / 'snapshot.json').read_bytes())
    assert removed_name not in {row['filename'] for row in snapshot['sidecars']}
    descriptor = json.loads((second.path / 'manifest.json').read_bytes())
    base_original = next((base.path / 'originals').iterdir()).name
    assert 'originals/' + base_original not in descriptor['stored']
    target = restore_incremental_backup(second.path, tmp_path / 'restored', second.manifest_sha256,
                                         bases=(base, first_ref), backup_budget=selected_policy())
    assert not (target / removed_name).exists()
    assert ReaderDraftStore(target).load().document.text == 'A new current draft.'
    assert LibraryApp(target).edition_metadata(workspace, 'second').title == 'Additional original'
    assert (target / 'originals' / base_original).read_bytes() == (base.path / 'originals' / base_original).read_bytes()


def test_final_link_cancellation_leaves_no_accepted_completion_marker(source, tmp_path, monkeypatch):
    app, _, document, drafts, base = source
    document.set_text('Changed data for a real stored file.')
    drafts.save(ReaderResume(document))
    before = bytes_in(base.path)
    destination = tmp_path / 'interrupted'
    real_link = incremental.os.link
    stopped = False
    def link(source, target, *args, **kwargs):
        nonlocal stopped
        result = real_link(source, target, *args, **kwargs)
        if Path(target).is_relative_to(destination / 'files'):
            stopped = True
        return result
    monkeypatch.setattr(incremental.os, 'link', link)
    with pytest.raises(InterruptedError):
        create_incremental_backup(app, destination, base=base, cancelled=lambda: stopped,
                                  backup_budget=selected_policy())
    assert stopped and destination.exists()
    assert not (destination / 'manifest.json').exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


@pytest.mark.parametrize('resource', ['staging', 'memory'])
def test_combined_materialization_resources_are_checked_before_publication(
        source, tmp_path, resource):
    app, _, _, _, base = source
    before = bytes_in(base.path)
    if resource == 'staging':
        # One real full snapshot fits, but the current full + verified copy +
        # changed files cannot all occupy this single reviewed allowance.
        logical = sum(len(raw) for raw in before.values())
        policy = selected_policy(staging_bytes=logical + 4096)
    else:
        # The policy itself is valid. Its full manifest allowance leaves only
        # 1 KiB for held layer dictionaries before entering the canonical reader.
        policy = selected_policy(memory_bytes=512*MIB + 8*BackupBudget().manifest_bytes + 1024)
    destination = tmp_path / 'over-budget'
    with pytest.raises(ValueError, match='allowance|memory|budget|staging'):
        create_incremental_backup(app, destination, base=base, backup_budget=policy)
    assert not destination.exists()
    assert bytes_in(base.path) == before
    assert not list(tmp_path.glob('.carr-incremental-*'))


@pytest.mark.parametrize('name', ['State/speech/alias.json', 'state/speech/CON.json', 'state/speech/./alias.json'])
def test_resealed_nonportable_layer_names_are_refused_before_external_read(
        source, tmp_path, monkeypatch, name):
    _, _, _, _, base = source
    receipt = changed(source, tmp_path)
    descriptor = json.loads((receipt.path / 'manifest.json').read_bytes())
    descriptor['stored'].append(name)
    descriptor['stored'].sort()
    raw = encoded(descriptor)
    (receipt.path / 'manifest.json').write_bytes(raw)
    with pytest.raises(ValueError):
        verify_incremental_backup(receipt.path, sha256(raw).hexdigest(), bases=(base,),
                                   backup_budget=selected_policy())


def test_removed_original_is_absent_while_equal_bytes_are_inherited_from_selected_base(tmp_path):
    # Two independently valid catalogs give the delta an actual smaller/different
    # original inventory without modifying SQL or pretending a source was deleted.
    old = LibraryApp(tmp_path / 'old-library')
    old_scope = old.create_workspace('Earlier captured inventory')
    old.add_text(old_scope, 'Removed', 'Only the old inventory contains these bytes.', edition_id='removed')
    old.add_text(old_scope, 'Stable', 'Unchanged exact source.', edition_id='stable')
    base_path = tmp_path / 'full'
    base = BackupReference(base_path, create_backup(old, base_path))
    before = bytes_in(base.path)
    current = LibraryApp(tmp_path / 'current-library')
    scope = current.create_workspace('Current captured inventory')
    assert current.add_text(scope, 'Stable', 'Unchanged exact source.', edition_id='stable')['state'] == 'complete'
    assert current.add_text(scope, 'Added', 'New current original.', edition_id='added')['state'] == 'complete'
    current_originals = {path.name: path.read_bytes() for path in current.assets_dir.iterdir()}
    old_originals = {path.name: path.read_bytes() for path in old.assets_dir.iterdir()}
    inherited = set(current_originals) & set(old_originals)
    removed = set(old_originals) - set(current_originals)
    added = set(current_originals) - set(old_originals)
    assert len(inherited) == len(removed) == len(added) == 1
    receipt = create_incremental_backup(current, tmp_path / 'layer', base=base,
                                       backup_budget=selected_policy())
    wire = json.loads((receipt.path / 'manifest.json').read_bytes())
    assert all('originals/' + name not in wire['stored'] for name in inherited | removed)
    assert all('originals/' + name in wire['stored'] for name in added)
    shutil.rmtree(current.root)
    restored = restore_incremental_backup(receipt.path, tmp_path / 'restored-current',
        receipt.manifest_sha256, bases=(base,), backup_budget=selected_policy())
    assert {path.name: path.read_bytes() for path in (restored / 'originals').iterdir()} == current_originals
    assert all(not (restored / 'originals' / name).exists() for name in removed)
    reopened = LibraryApp(restored)
    assert reopened.edition_metadata(scope, 'added').title == 'Added'
    assert reopened.edition_metadata(scope, 'stable').title == 'Stable'
    assert bytes_in(base.path) == before


def test_incremental_pending_pages_preserve_checkpoint_bytes_but_revoke_execution_and_scan_authority(
        tmp_path, monkeypatch):
    from carr_library.ingest.resource_budget import ImportBudget
    from carr_library.ingest import worker
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending incremental recovery')
    selected = tmp_path / 'selected'
    selected.mkdir()
    for index in range(3):
        (selected / ('book-%d.txt' % index)).write_text(
            'First original %d.\r\n\fSecond original %d.' % (index, index), encoding='utf-8')
    budget = ImportBudget(source_bytes=MIB, segment_bytes=MIB, segments=8,
        steps=1, seconds=120, memory_bytes=512*MIB,
        library_bytes=64*MIB, temporary_bytes=128*MIB)
    first = app.plan_import_page(workspace, [selected], max_files=1, import_budget=budget)
    pending = app.run_import_batch(workspace, first['batch_id'], max_items=1)
    row, = pending['rows']
    assert row['result']['state'] == 'pending' and row['result']['completed'] == 1
    request_id = row['request_id']
    request = worker.load_request(app.root, request_id)
    assert request['attempt'] and request['asset_sha256']
    base_path = tmp_path / 'full-pending'
    base = BackupReference(base_path, create_backup(app, base_path, backup_budget=selected_policy()))
    second = app.continue_import_scan(workspace, first['batch_id'], max_files=1)
    assert second['scan']['cursor']['complete'] is False
    saved = [app.import_batch(workspace, page['batch_id']) for page in (first, second)]
    expected = [[(r['row_id'], r['request_id'], r['work_id'], r['edition_id'], r['manifest'])
                 for r in page['rows']] for page in saved]
    receipt = create_incremental_backup(app, tmp_path / 'delta-pending',
        base=base, backup_budget=selected_policy())
    retained = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    # The common4 pending inventory also copies reviewed, still-queued inputs.
    # Derive its exact expected union from the original bytes while they exist,
    # not from the restored tree or from an assumed already-retained-only set.
    expected_retained = dict(retained)
    for page in saved:
        for planned in page['rows']:
            manifest = planned['manifest']
            raw = Path(planned['source']).read_bytes()
            assert len(raw) == manifest['size']
            assert sha256(raw).hexdigest() == manifest['sha256']
            expected_retained[manifest['sha256'] + '.txt'] = raw
    assert len(retained) == 1 and len(expected_retained) == 2
    assert set(retained) < set(expected_retained)
    before = bytes_in(base.path), bytes_in(receipt.path)
    shutil.rmtree(selected)
    shutil.rmtree(app.root)
    def forbidden(*args, **kwargs):
        pytest.fail('Incremental restoration activated a worker or external scan.')
    with monkeypatch.context() as passive:
        passive.setattr(worker, '_invoke', forbidden)
        passive.setattr(subprocess, 'Popen', forbidden)
        target = restore_incremental_backup(receipt.path, tmp_path / 'restored-pending',
            receipt.manifest_sha256, bases=(base,), backup_budget=selected_policy())
        reopened = LibraryApp(target)
        for index, page in enumerate(saved):
            current = reopened.import_batch(workspace, page['batch_id'])
            assert current['scan']['review_required'] is True
            assert current['import_budget'] == budget.to_dict()
            assert [(r['row_id'], r['request_id'], r['work_id'], r['edition_id'], r['manifest'])
                    for r in current['rows']] == expected[index]
        current_request = worker.load_request(target, request_id)
        assert current_request['job'] == request['job']
        assert current_request['asset_sha256'] == request['asset_sha256']
        assert not {'attempt', 'publication_candidate', 'publication_watermark'} & set(current_request)
        with pytest.raises(ValueError, match='Restored scan roots require a fresh explicit selection'):
            reopened.continue_import_scan(workspace, second['batch_id'], max_files=1)
        assert {path.name: path.read_bytes() for path in reopened.assets_dir.iterdir()} == expected_retained
    assert (bytes_in(base.path), bytes_in(receipt.path)) == before
