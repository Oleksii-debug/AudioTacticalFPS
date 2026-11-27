"""Book plans survive complete backup without exporting output permission."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp, open_existing_library
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.book_batch import BookBatchService, select_books
from carr_library.audio.book_store import BookStore, state_paths, validate_state_tree
from carr_library.audio.jobs import project_lock, read_json, _sync_directory
from tests.audio.test_book_plans import library, make_plan


def prepared(library, tmp_path):
    app, workspace, *_ = library
    books = BookAudioService(app, workspace)
    groups = BookBatchService(books)
    plans = (make_plan(library), make_plan(library, item=library[3]))
    batch = select_books(plans)
    output = tmp_path / 'output'
    output.mkdir()
    reviewed = groups.review(batch, (output, output), plans=plans)
    groups.prepare(batch, (output, output), plans=plans, approval=reviewed.fingerprint)
    return books, groups, plans, batch


def inventory(root):
    return {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


def test_pending_book_parts_restore_rebackup_and_require_fresh_output_review(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    source = library[0]
    digest = create_backup(source, tmp_path / 'backup')
    before = inventory(tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    assert inventory(tmp_path / 'backup') == before
    target = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    current = open_existing_library(target)
    resumed = BookBatchService(BookAudioService(current, library[1]))
    restored = resumed.load(batch.id)
    assert restored == batch
    assert [plan.fingerprint for plan in resumed.plans(restored)] == [plan.fingerprint for plan in plans]
    assert resumed.status(batch.id)['verified_outputs'] is False
    output = tmp_path / 'new-output'
    output.mkdir()
    reviewed = resumed.review(restored, (output, output))
    assert reviewed.prepared
    assert str(tmp_path / 'output') not in json.dumps(restored.to_dict())
    second = create_backup(current, tmp_path / 'rebackup')
    verify_backup(tmp_path / 'rebackup', second)


def test_historical_workspace_can_backup_but_cannot_run_until_current_authorization(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    library[0].archive_workspace(library[1])
    digest = create_backup(library[0], tmp_path / 'backup')
    verify_backup(tmp_path / 'backup', digest)
    with pytest.raises(ValueError, match='archived|missing'):
        groups.load(batch.id)


def test_actual_native_book_lock_refuses_snapshot_without_modifying_plan(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    before = inventory(books.store.root)
    with project_lock(books.store.root, lock_name='.books.lock'):
        with pytest.raises((RuntimeError, ValueError)):
            create_backup(library[0], tmp_path / 'backup')
    assert inventory(books.store.root) == before
    assert not (tmp_path / 'backup').exists()


def test_forged_source_metadata_rehashed_envelope_still_fails_historical_db_binding(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    from carr_library.audio.book_plan import BookPlan
    path = books.store.path(plans[0].id, 'plans')
    raw = read_json(path, limit=32 * 1024**2)
    raw['value']['source']['source_metadata']['author'] = 'forged author'
    changed = BookPlan.from_dict(raw['value'])
    path.write_text(json.dumps({'value': changed.to_dict(), 'sha256': changed.fingerprint}), encoding='utf-8')
    with library[0].read() as db:
        with pytest.raises(ValueError, match='metadata'):
            validate_state_tree(db, library[0].root)


def test_unknown_durable_neighbor_and_missing_plan_fail_closed(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    extra = books.store.root / 'commands.json'
    extra.write_text('{}')
    with pytest.raises(ValueError, match='Unknown'):
        state_paths(library[0].root)
    extra.unlink()
    books.store.path(plans[0].id, 'plans').unlink()
    with library[0].read() as db:
        with pytest.raises(ValueError, match='missing'):
            validate_state_tree(db, library[0].root)


def test_book_review_retry_resyncs_identical_committed_record(library, tmp_path, monkeypatch):
    books, groups, plans, batch = prepared(library, tmp_path)
    import carr_library.audio.book_store as module
    monkeypatch.setattr(module, '_sync_directory',
                        lambda path: (_ for _ in ()).throw(OSError('directory sync refused')))
    with pytest.raises(OSError, match='sync refused'):
        books.save(plans[0])
    monkeypatch.setattr(module, '_sync_directory', _sync_directory)
    assert books.save(plans[0]).fingerprint == plans[0].fingerprint


def test_inventory_does_not_materialize_an_unbounded_base_directory(library, tmp_path, monkeypatch):
    books, groups, plans, batch = prepared(library, tmp_path)
    import carr_library.audio.book_store as module
    def forbidden(path):
        raise AssertionError('Unbounded Path.iterdir must not be used')
    monkeypatch.setattr(type(books.store.root), 'iterdir', forbidden)
    assert len(state_paths(library[0].root)) == 3


def test_historical_page_raw_bytes_are_checked_even_if_digest_is_unchanged(library, tmp_path):
    books, groups, plans, batch = prepared(library, tmp_path)
    from tests.reader.test_reading_desk import _mutate_page
    anchor = plans[0].source['source_anchor']
    _mutate_page(library[0], anchor['revision'], 'UPDATE page SET raw=? WHERE ordinal=1 AND revision=?',
                 ('forged exact page bytes',))
    with library[0].read() as db:
        with pytest.raises(ValueError, match='page bytes'):
            validate_state_tree(db, library[0].root)
