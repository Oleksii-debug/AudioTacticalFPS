"""Real pending import backup/restore, source integrity and explicit recovery.

These tests execute existing LibraryApp, request journals and import workers.
No extractor or backup engine is replaced by a fake implementation.
"""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.worker import load_request, prepare_request
from carr_library.extract.page_ocr import Config
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.sync.service import SyncService
from carr_library import pending_backup as producer


TEXT = 'Перший незмінений уривок.\fДругий точний уривок.'


def setup(tmp_path, *, content=TEXT):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Selected library')
    source = tmp_path / 'Збережене джерело.txt'
    source.write_bytes(content.encode('utf-8'))
    return app, workspace, source


def backup(app, destination):
    return app.backup(destination)


def restore(source, target, checksum):
    restore_backup(source, target, checksum)
    return LibraryApp(target)


def approve(app, workspace, kind, identity):
    report = app.review_import_recovery(workspace, kind, identity)
    receipt = app.approve_import_recovery(workspace, kind, identity,
        expected_checksum=report['checksum'], confirmed=True)
    assert receipt['approved'] is True
    return report, receipt


def checkpoint_rows(root):
    with closing(sqlite3.connect(root / 'imports.sqlite')) as db:
        return tuple(db.execute('SELECT job,ordinal,payload,digest FROM checkpoint ORDER BY job,ordinal'))


def stop_after_checkpoint(root):
    def cancelled():
        journal = root / 'imports.sqlite'
        if not journal.exists():
            return False
        with closing(sqlite3.connect(journal)) as db:
            try:
                return db.execute('SELECT count(*) FROM checkpoint').fetchone()[0] >= 1
            except sqlite3.OperationalError:
                return False
    return cancelled


@pytest.mark.parametrize('version', [1, 2])
def test_queued_v1_v2_restores_owned_sources_and_requires_exact_review(tmp_path, version):
    app, workspace, source = setup(tmp_path)
    batch = app.plan_import_batch(workspace, (source,))
    if version == 1:
        batch['version'] = 1
        batch.pop('scan')
        (app.root / 'import-batches' / (batch['batch_id'] + '.json')).write_text(
            canonical(batch) + '\n', encoding='utf-8')
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    manifest = verify_backup(bundle, checksum)
    assert manifest['version'] == 4
    assert any(entry['filename'].startswith('pending-imports/originals/') for entry in manifest['sidecars'])
    source.unlink()
    restored = restore(bundle, tmp_path / 'restored', checksum)
    saved = restored.import_batch(workspace, batch['batch_id'])
    assert saved['version'] == version and saved['rows'][0]['edition_id'] == batch['rows'][0]['edition_id']
    captured = Path(saved['rows'][0]['source'])
    assert captured.is_relative_to(restored.root / 'pending-imports' / 'originals')
    assert captured.read_bytes() == TEXT.encode('utf-8')
    with pytest.raises(PermissionError, match='Review restored imports'):
        restored.run_import_batch(workspace, batch['batch_id'])
    review = restored.review_import_recovery(workspace, 'batch', batch['batch_id'])
    with pytest.raises(PermissionError, match='explicitly confirm'):
        restored.approve_import_recovery(workspace, 'batch', batch['batch_id'],
            expected_checksum=review['checksum'])
    with pytest.raises(ValueError, match='changed'):
        restored.approve_import_recovery(workspace, 'batch', batch['batch_id'],
            expected_checksum='0' * 64, confirmed=True)
    approve(restored, workspace, 'batch', batch['batch_id'])
    result = restored.run_import_batch(workspace, batch['batch_id'])
    assert result['state'] == 'complete'
    hit, = restored.search(workspace, 'Другий точний уривок.')
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Другий точний уривок.'
    assert hit.edition_id == batch['rows'][0]['edition_id']


def test_inline_txt_saves_known_intent_before_checkpoint_and_resumes_exact_job(tmp_path):
    app, workspace, source = setup(tmp_path)
    collection = app.create_collection(workspace, 'Chosen collection')
    result = app.import_file(workspace, source, title='Original title', authors=('Actual entered author',),
                             collections=(collection,), cancelled=stop_after_checkpoint(app.root))
    assert result['state'] == 'cancelled' and result['completed'] == 1
    request = load_request(app.root, result['request_id'])
    assert request['metadata']['title'] == 'Original title'
    assert request['metadata']['collections'] == [collection]
    assert request['metadata']['authors'] == ['Actual entered author']
    before = checkpoint_rows(app.root)
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    source.unlink()
    restored = restore(bundle, tmp_path / 'restored', checksum)
    recovered = load_request(restored.root, result['request_id'])
    assert recovered['job'] == request['job'] and recovered['metadata'] == request['metadata']
    assert checkpoint_rows(restored.root) == before
    with pytest.raises(PermissionError, match='Review restored imports'):
        restored.resume_import(workspace, result['request_id'])
    approve(restored, workspace, 'request', result['request_id'])
    done = restored.resume_import(workspace, result['request_id'])
    assert done['state'] == 'complete' and done['id'] == request['job']
    assert checkpoint_rows(restored.root)[:1] == before
    hit, = restored.search(workspace, 'Другий точний уривок.', collection=collection)
    assert hit.title == 'Original title'
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Другий точний уривок.'


@pytest.mark.parametrize('already_approved', [False, True])
def test_paused_capture_can_be_backed_up_again_before_any_resume(tmp_path, already_approved):
    app, workspace, source = setup(tmp_path)
    paused = app.import_file(workspace, source, title='Captured pending source',
                             cancelled=stop_after_checkpoint(app.root))
    assert paused['state'] == 'cancelled'
    before = checkpoint_rows(app.root)
    first = tmp_path / 'first'
    first_token = backup(app, first)
    source.unlink()
    recovered = restore(first, tmp_path / 'first-restored', first_token)
    request = load_request(recovered.root, paused['request_id'])
    assert not (recovered.assets.root / (request['asset_sha256'] + '.txt')).exists()
    captured = Path(request['source'])
    assert captured.read_bytes() == TEXT.encode('utf-8')
    if already_approved:
        approve(recovered, workspace, 'request', paused['request_id'])
    second = tmp_path / 'second'
    second_token = backup(recovered, second)
    again = restore(second, tmp_path / 'second-restored', second_token)
    assert checkpoint_rows(again.root) == before
    assert Path(load_request(again.root, paused['request_id'])['source']).read_bytes() == captured.read_bytes()
    report = again.review_import_recovery(workspace, 'request', paused['request_id'])
    assert report['already_approved'] is False
    with pytest.raises(PermissionError, match='Review restored imports'):
        again.resume_import(workspace, paused['request_id'])
    approve(again, workspace, 'request', paused['request_id'])
    result = again.resume_import(workspace, paused['request_id'])
    assert result['state'] == 'complete' and result['id'] == paused['id']
    hit, = again.search(workspace, 'Другий точний уривок.')
    assert again.copy(workspace, hit.ref, hit.revision, 'quote') == 'Другий точний уривок.'


def test_historical_inline_txt_without_intent_creates_new_reviewed_plan(tmp_path):
    app, workspace, source = setup(tmp_path)
    # This is the exact historical inline producer: it wrote a journal without
    # a request. Missing work/title/collection intent must not be inferred.
    old = app._import_file(workspace, source, title='Unrecorded old title',
                           cancelled=stop_after_checkpoint(app.root))
    assert old['state'] == 'cancelled'
    assert not app.import_requests(workspace)
    before = checkpoint_rows(app.root)
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    source.unlink()
    restored = restore(bundle, tmp_path / 'restored', checksum)
    selection, = restored.import_recovery_items(workspace)
    assert selection['kind'] == 'source' and selection['identity'] == old['id']
    report, receipt = approve(restored, workspace, 'source', old['id'])
    assert report['new_import_required'] is True and receipt['batch_id']
    assert not restored.browse(workspace)  # Approval only plans; it does not import.
    done = restored.run_import_batch(workspace, receipt['batch_id'])
    assert done['state'] == 'complete'
    assert done['rows'][0]['edition_id'] != old['edition']
    assert checkpoint_rows(restored.root)[:1] == before or before[0] in checkpoint_rows(restored.root)
    with restored.read(workspace) as db:
        assert db.execute('SELECT 1 FROM revision WHERE id=?', (old['id'],)).fetchone() is None


def test_unscanned_tail_is_reported_and_old_cursor_is_never_followed(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 1)
    app, workspace, source = setup(tmp_path, content='First captured source.')
    folder = tmp_path / 'selected'
    folder.mkdir()
    source.rename(folder / 'a.txt')
    (folder / 'b.txt').write_text('Never captured by the first page.', encoding='utf-8')
    batch = app.plan_import_batch(workspace, (folder,))
    assert batch['scan']['next_offset'] == 1
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    restored = restore(bundle, tmp_path / 'restored', checksum)
    saved = restored.import_batch(workspace, batch['batch_id'])
    assert saved['scan']['next_offset'] is None and saved['scan']['next_batch_id'] is None
    assert saved['scan']['roots'] == []
    report, _ = approve(restored, workspace, 'batch', batch['batch_id'])
    assert report['previous_scan_incomplete'] is True and report['file_count'] == 1
    with pytest.raises(ValueError, match='no continuation'):
        restored.next_import_scan_page(workspace, batch['batch_id'])
    assert restored.run_import_batch(workspace, batch['batch_id'])['total'] == 1
    assert not restored.search(workspace, 'Never captured')
    second_bundle = tmp_path / 'second'
    second_checksum = backup(restored, second_bundle)
    second = restore(second_bundle, tmp_path / 'second-restore', second_checksum)
    second_report = second.review_import_recovery(workspace, 'batch', batch['batch_id'])
    assert second_report['previous_scan_incomplete'] is True
    with pytest.raises(PermissionError, match='Review restored imports'):
        second.run_import_batch(workspace, batch['batch_id'])


def test_changed_planned_source_refuses_backup_without_skipping_the_queue(tmp_path):
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    source.write_text('Changed after the reviewed file manifest.', encoding='utf-8')
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError, match='source changed'):
        backup(app, destination)
    assert not destination.exists()
    assert source.read_text(encoding='utf-8') == 'Changed after the reviewed file manifest.'


def test_active_batch_lock_refuses_snapshot_and_preserves_live_queue(tmp_path):
    app, workspace, source = setup(tmp_path)
    batch = app.plan_import_batch(workspace, (source,))
    before = (app.root / 'import-batches' / (batch['batch_id'] + '.json')).read_bytes()
    destination = tmp_path / 'must-not-publish'
    with ImportBatchStore(app)._lock(batch['batch_id']):
        with pytest.raises(ValueError, match='Pause import jobs'):
            backup(app, destination)
    assert not destination.exists()
    assert (app.root / 'import-batches' / (batch['batch_id'] + '.json')).read_bytes() == before


def test_review_cannot_cross_workspace_or_approve_tampered_captured_bytes(tmp_path):
    app, workspace, source = setup(tmp_path)
    other = app.create_workspace('Other library')
    batch = app.plan_import_batch(workspace, (source,))
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    restored = restore(bundle, tmp_path / 'restored', checksum)
    assert restored.import_recovery_items(other) == ()
    with pytest.raises(PermissionError, match='this library'):
        restored.review_import_recovery(other, 'batch', batch['batch_id'])
    shown = restored.review_import_recovery(workspace, 'batch', batch['batch_id'])
    saved = restored.import_batch(workspace, batch['batch_id'])
    captured = Path(saved['rows'][0]['source'])
    raw = captured.read_bytes()
    captured.write_bytes(b'XX' + raw[2:])  # Keep UTF-8 valid; exercise the pinned byte digest.
    with pytest.raises(ValueError, match='source changed'):
        restored.approve_import_recovery(workspace, 'batch', batch['batch_id'],
            expected_checksum=shown['checksum'], confirmed=True)
    with pytest.raises(PermissionError, match='Review restored imports'):
        restored.run_import_batch(workspace, batch['batch_id'])
    assert not restored.browse(workspace)


def test_cancelled_source_revalidation_does_not_grant_recovery_approval(tmp_path, monkeypatch):
    app, workspace, source = setup(tmp_path)
    batch = app.plan_import_batch(workspace, (source,))
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    restored = restore(bundle, tmp_path / 'restored', checksum)
    shown = restored.review_import_recovery(workspace, 'batch', batch['batch_id'])
    stopped = Event()
    original = producer.planned_snapshot
    def stop_after_real_read(path):
        value = original(path)
        stopped.set()
        return value
    monkeypatch.setattr(producer, 'planned_snapshot', stop_after_real_read)
    with pytest.raises(InterruptedError):
        restored.approve_import_recovery(workspace, 'batch', batch['batch_id'],
            expected_checksum=shown['checksum'], confirmed=True, cancelled=stopped.is_set)
    with pytest.raises(PermissionError, match='Review restored imports'):
        restored.run_import_batch(workspace, batch['batch_id'])


def test_cancelled_backup_capture_does_not_publish_a_partial_component(tmp_path, monkeypatch):
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    original, stopped = producer.planned_snapshot, Event()
    def stop_after_real_read(path):
        value = original(path)
        stopped.set()
        return value
    monkeypatch.setattr(producer, 'planned_snapshot', stop_after_real_read)
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(InterruptedError):
        app.backup(destination, cancelled=stopped.is_set)
    assert not destination.exists() and source.read_bytes() == TEXT.encode('utf-8')


def test_backup_tampering_is_rejected_before_destination_creation(tmp_path):
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    captured, = (bundle / 'pending-imports' / 'originals').iterdir()
    raw = captured.read_bytes()
    captured.write_bytes(b'X' + raw[1:])
    destination = tmp_path / 'must-not-create'
    with pytest.raises(ValueError):
        restore_backup(bundle, destination, checksum)
    assert not destination.exists()


def rehash_component(bundle):
    component_path = bundle / 'pending-imports' / 'manifest.json'
    component = json.loads(component_path.read_bytes())
    for entry in component['files']:
        raw = (bundle / 'pending-imports' / entry['filename']).read_bytes()
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    component_path.write_text(canonical(component) + '\n', encoding='utf-8')
    outer_path = bundle / 'manifest.json'
    outer = json.loads(outer_path.read_bytes())
    for entry in outer['sidecars']:
        if entry['filename'].startswith('pending-imports/'):
            raw = (bundle / entry['filename']).read_bytes()
            entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = (canonical(outer) + '\n').encode('utf-8')
    outer_path.write_bytes(raw)
    return sha256(raw).hexdigest()


def test_rehashed_false_txt_checkpoint_still_fails_exact_source_validation(tmp_path):
    app, workspace, source = setup(tmp_path)
    result = app.import_file(workspace, source, cancelled=stop_after_checkpoint(app.root))
    assert result['state'] == 'cancelled'
    bundle = tmp_path / 'bundle'
    backup(app, bundle)
    with closing(sqlite3.connect(bundle / 'pending-imports' / 'imports.sqlite')) as db:
        job, ordinal, raw = db.execute('SELECT job,ordinal,payload FROM checkpoint').fetchone()
        value = json.loads(raw)
        value['raw_text'] = 'Forged text with a recomputed checksum.'
        payload = canonical(value)
        db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=?',
                   (payload, sha256(payload.encode('utf-8')).hexdigest(), job, ordinal))
        db.commit()
    checksum = rehash_component(bundle)
    destination = tmp_path / 'must-not-create'
    with pytest.raises(ValueError, match='exact original bytes'):
        restore_backup(bundle, destination, checksum)
    assert not destination.exists()


def test_prepared_request_must_match_its_batch_even_after_outer_rehash(tmp_path):
    app, workspace, source = setup(tmp_path)
    other = app.create_workspace('Other library')
    batch = app.plan_import_batch(workspace, (source,))
    row = batch['rows'][0]
    prepare_request(app.root, workspace, source, ImportBatchStore._metadata(row, batch),
                    Config(**batch['config']), request_id=row['request_id'],
                    expected_manifest=row['manifest'])
    bundle = tmp_path / 'bundle'
    backup(app, bundle)
    path = bundle / 'pending-imports' / 'import-batches' / (batch['batch_id'] + '.json')
    value = json.loads(path.read_bytes())
    value['workspace'] = other
    value['scan']['options_sha256'] = producer._options_digest(value)
    path.write_text(canonical(value) + '\n', encoding='utf-8')
    checksum = rehash_component(bundle)
    destination = tmp_path / 'must-not-create'
    with pytest.raises(ValueError, match='differs from its batch'):
        restore_backup(bundle, destination, checksum)
    assert not destination.exists()


def test_unknown_neighbor_is_not_copied_or_silently_discarded(tmp_path):
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    neighbor = app.root / 'import-batches' / 'credentials.json'
    neighbor.write_text('{"password":"synthetic-test-only"}', encoding='utf-8')
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError, match='Unrecognized'):
        backup(app, destination)
    assert not destination.exists() and neighbor.exists()


def test_metadata_aggregate_budget_fails_before_unbounded_decode(tmp_path, monkeypatch):
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    monkeypatch.setattr(producer, 'MAX_METADATA', 10)
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError, match='aggregate memory budget'):
        backup(app, destination)
    assert not destination.exists()


def test_inline_committed_receipt_survives_lost_reply_and_full_restoration(tmp_path, monkeypatch):
    from carr_library.application import _CatalogPublisher

    app, workspace, source = setup(tmp_path)
    original = _CatalogPublisher.publish_revision
    def committed_then_lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError('Synthetic lost inline reply after the real SQLite commit.')
    with monkeypatch.context() as fault:
        fault.setattr(_CatalogPublisher, 'publish_revision', committed_then_lost)
        result = app.import_file(workspace, source, title='Recorded before execution')
    assert result['state'] == 'complete' and result['publication_recovered'] is True
    assert result['isolated'] is False
    record = load_request(app.root, result['request_id'])
    with app.read(workspace) as db:
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    bundle = tmp_path / 'bundle'
    checksum = backup(app, bundle)
    source.unlink()
    restored = restore(bundle, tmp_path / 'restored', checksum)
    current = load_request(restored.root, result['request_id'])
    assert 'attempt' not in current and 'publication_candidate' not in current
    assert current['result'] == result
    with restored.read(workspace) as db:
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt
    with pytest.raises(PermissionError, match='Review restored imports'):
        restored.resume_import(workspace, result['request_id'])
    approve(restored, workspace, 'request', result['request_id'])
    assert restored.resume_import(workspace, result['request_id']) == result
    hit, = restored.search(workspace, 'Другий точний уривок.')
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Другий точний уривок.'
    second = tmp_path / 'second-bundle'
    assert verify_backup(second, backup(restored, second))['schema_version'] == 5


def test_changed_candidate_cannot_claim_its_immutable_owned_receipt(tmp_path):
    from carr_library.ingest.worker import _store

    app, workspace, source = setup(tmp_path)
    result = app.import_file(workspace, source)
    record = load_request(app.root, result['request_id'])
    assert record['publication_candidate'] is not None
    record['publication_candidate']['sha256'] = '0' * 64
    _store(app.root, result['request_id'], record)
    destination = tmp_path / 'must-not-publish'
    with pytest.raises(ValueError, match='owned database receipt'):
        backup(app, destination)
    assert not destination.exists()


@pytest.mark.parametrize('kind', ['batch', 'request'])
def test_portable_staging_binds_final_root_and_stays_unapproved(tmp_path, monkeypatch, kind):
    from carr_library.sync import vault
    app, workspace, source = setup(tmp_path)
    if kind == 'batch':
        queued = app.plan_import_batch(workspace, (source,))
        identity = queued['batch_id']
    else:
        queued = app.import_file(workspace, source, title='Paused portable source',
                                 cancelled=stop_after_checkpoint(app.root))
        assert queued['state'] == 'cancelled'
        identity = queued['request_id']
    service, folder = SyncService(app), tmp_path / 'vault'
    published = service.publish(folder, 'Portable pending work', None)
    assert any('new review' in message for message in service.inspect(folder, published['token'])['warnings'])
    source.unlink()
    destination = tmp_path / 'final-library'
    actual_publish = vault._publish_directory
    observed = []

    def inspect_before_commit(stage, target):
        if Path(target) == destination:
            marker = json.loads((Path(stage) / 'import-recovery.json').read_bytes())
            assert marker['data_dir'] == str(destination)
            assert all(entry['approved'] is False for entry in marker['items'].values())
            if kind == 'batch':
                record = json.loads((Path(stage) / 'import-batches' / (identity + '.json')).read_bytes())
                assert record['scan']['roots'] == [] and record['scan']['next_offset'] is None
                paths = [row['source'] for row in record['rows']]
            else:
                record = json.loads((Path(stage) / 'import-requests' / (identity + '.json')).read_bytes())
                paths = [record['source']]
            assert record['data_dir'] == str(destination)
            assert all(Path(path).is_relative_to(destination / 'pending-imports' / 'originals') for path in paths)
            assert not destination.exists()
            observed.append(True)
        return actual_publish(stage, target)

    monkeypatch.setattr(vault, '_publish_directory', inspect_before_commit)
    service.restore(folder, published['token'], destination)
    assert observed == [True]
    recovered = LibraryApp(destination)
    with pytest.raises(PermissionError, match='Review restored imports'):
        if kind == 'batch':
            recovered.run_import_batch(workspace, identity)
        else:
            recovered.resume_import(workspace, identity)
    approve(recovered, workspace, kind, identity)
    done = (recovered.run_import_batch(workspace, identity) if kind == 'batch'
            else recovered.resume_import(workspace, identity))
    assert done['state'] == 'complete'
    hit, = recovered.search(workspace, 'Другий точний уривок.')
    assert recovered.copy(workspace, hit.ref, hit.revision, 'quote') == 'Другий точний уривок.'
    second = tmp_path / 'second-backup'
    assert verify_backup(second, backup(recovered, second))['version'] == 4


def test_portable_cancellation_after_rebinding_does_not_publish(tmp_path, monkeypatch):
    from carr_library.ingest import pending_sidecars
    app, workspace, source = setup(tmp_path)
    batch = app.plan_import_batch(workspace, (source,))
    service, folder = SyncService(app), tmp_path / 'vault'
    receipt = service.publish(folder, 'Cancelled recovery', None)
    source_bytes = source.read_bytes()
    original = (app.root / 'import-batches' / (batch['batch_id'] + '.json')).read_bytes()
    stop = Event()
    actual = pending_sidecars.rebind_for_publication

    def rebind(*args, **kwargs):
        actual(*args, **kwargs)
        stop.set()

    monkeypatch.setattr(pending_sidecars, 'rebind_for_publication', rebind)
    destination = tmp_path / 'never-published'
    with pytest.raises(InterruptedError):
        service.restore(folder, receipt['token'], destination, cancelled=stop.is_set)
    assert not destination.exists()
    assert source.read_bytes() == source_bytes
    assert (app.root / 'import-batches' / (batch['batch_id'] + '.json')).read_bytes() == original


def test_portable_refuses_staging_with_changed_approval(tmp_path, monkeypatch):
    from carr_library.sync import service as transport
    app, workspace, source = setup(tmp_path)
    app.plan_import_batch(workspace, (source,))
    service, folder = SyncService(app), tmp_path / 'vault'
    receipt = service.publish(folder, 'Inert recovery', None)
    actual = transport._copy_verified_tree

    def copy_then_change(source, destination, *args, **kwargs):
        actual(source, destination, *args, **kwargs)
        path = Path(destination) / 'import-recovery.json'
        marker = json.loads(path.read_bytes())
        for entry in marker['items'].values():
            entry['approved'] = True
        path.write_text(canonical(marker) + '\n', encoding='utf-8')

    monkeypatch.setattr(transport, '_copy_verified_tree', copy_then_change)
    destination = tmp_path / 'never-published'
    with pytest.raises(ValueError, match='already approved'):
        service.restore(folder, receipt['token'], destination)
    assert not destination.exists()
