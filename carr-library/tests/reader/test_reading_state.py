"""Durable reader state: real files, scoped identities and atomic recovery.

The source-validation callback represents the application resolver. It must
raise when a stored revision/page is unavailable; these tests do not claim to
validate a screen-reader cursor or replace native accessibility acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer
from carr_library.reader import reading_state
from carr_library.reader.reading_state import ReadingLocation, ReadingState


def location(*, workspace='library-a', edition='edition-a', revision='revision-1',
             kind='physical_page', index=3):
    text = f'Original page {edition} / {revision} / {index}: Київ.'
    return ReadingLocation(workspace, edition, revision, kind, index,
                           sha256(text.encode('utf-8')).hexdigest(),
                           sha256(f'Original asset {edition}'.encode()).hexdigest())


def state_at(path):
    path.mkdir()
    return ReadingState(path)


def assert_no_temporary(state):
    assert not tuple(state.root.glob('.reading-state-*.tmp'))


def test_empty_reads_do_not_create_state_and_reopen_retains_unicode_and_scopes(tmp_path):
    state = ReadingState(tmp_path)
    a = location()
    b = location(workspace='library-b')
    c = location(edition='edition-b', kind='logical_segment', index=1)
    assert state.resume(a.workspace, a.edition) is None
    assert state.bookmarks(a.workspace, a.edition) == ()
    assert not state.path.exists()

    labels = ('Україна — čítanie e\u0301 📚', 'Інша бібліотека', 'Логічний розділ')
    saved = []
    for item, label in zip((a, b, c), labels):
        state.remember(item)
        saved.append(state.add_bookmark(item, label))

    reopened = ReadingState(tmp_path)
    for item, bookmark in zip((a, b, c), saved):
        assert reopened.resume(item.workspace, item.edition) == item
        assert reopened.bookmarks(item.workspace, item.edition) == (bookmark,)
    renamed = reopened.rename_bookmark(a.workspace, a.edition, saved[0].id,
                                      'Повернутися до časti 3')
    assert renamed.created_at == saved[0].created_at
    assert renamed.location == a
    assert ReadingState(tmp_path).bookmarks(a.workspace, a.edition) == (renamed,)
    reopened.delete_bookmark(a.workspace, a.edition, saved[0].id)
    assert ReadingState(tmp_path).bookmarks(a.workspace, a.edition) == ()
    assert reopened.resume(a.workspace, a.edition) == a
    assert reopened.bookmarks(b.workspace, b.edition) == (saved[1],)
    assert_no_temporary(state)


def test_reader_state_never_modifies_research_database_or_original_bytes(tmp_path):
    database = tmp_path / 'library.sqlite'
    connection = sqlite3.connect(database)
    try:
        connection.execute('CREATE TABLE source(id TEXT PRIMARY KEY, quotation TEXT)')
        connection.execute('INSERT INTO source VALUES(?, ?)', ('source-1', 'Незмінна цитата.'))
        connection.commit()
    finally:
        connection.close()
    originals = tmp_path / 'originals'
    originals.mkdir()
    original = originals / 'source.txt'
    original.write_bytes('Незмінна цитата.\r\n'.encode('utf-8'))
    before = {path: path.read_bytes() for path in (database, original)}
    state = ReadingState(tmp_path)
    item = location()
    state.remember(item)
    bookmark = state.add_bookmark(item, 'Мій коментар, не цитата')
    state.rename_bookmark(item.workspace, item.edition, bookmark.id, 'Нова назва')
    state.export_state(item.workspace)
    state.delete_bookmark(item.workspace, item.edition, bookmark.id)

    assert {path: path.read_bytes() for path in before} == before
    names = {path.name for path in tmp_path.iterdir()}
    assert {'library.sqlite', 'originals', 'reading-state.json'} <= names
    assert names <= {'library.sqlite', 'originals', 'reading-state.json',
                     '.carr-ingest-writer.lock'}


@pytest.mark.parametrize('operation', ['rename', 'delete'])
@pytest.mark.parametrize('workspace,edition', [
    ('library-b', 'edition-a'), ('library-a', 'edition-b')])
def test_bookmark_id_cannot_be_used_from_another_scope(tmp_path, operation, workspace, edition):
    state = ReadingState(tmp_path)
    bookmark = state.add_bookmark(location(), 'Private reading place')
    before = state.path.read_bytes()
    with pytest.raises(ValueError, match='Select a bookmark'):
        if operation == 'rename':
            state.rename_bookmark(workspace, edition, bookmark.id, 'Wrong scope')
        else:
            state.delete_bookmark(workspace, edition, bookmark.id)
    assert state.path.read_bytes() == before
    assert_no_temporary(state)


@pytest.mark.parametrize('damage', [
    'truncated', 'duplicate-key', 'nested-duplicate-key', 'unknown-version',
    'invalid-utf8', 'oversized', 'duplicate-position', 'duplicate-bookmark'])
def test_invalid_existing_state_is_preserved_and_never_silently_reset(tmp_path, damage):
    state = ReadingState(tmp_path)
    state.remember(location())
    state.add_bookmark(location(), 'Stored bookmark')
    raw = state.path.read_bytes()
    document = json.loads(raw)
    if damage == 'truncated':
        damaged = raw[:len(raw) // 2]
    elif damage == 'duplicate-key':
        damaged = raw.replace(b'"version": 1', b'"version": 1, "version": 1', 1)
    elif damage == 'nested-duplicate-key':
        damaged = raw.replace(b'"index": 3', b'"index": 3, "index": 3', 1)
    elif damage == 'unknown-version':
        document['version'] = 2
        damaged = json.dumps(document).encode()
    elif damage == 'invalid-utf8':
        damaged = b'\xff' + raw
    elif damage == 'oversized':
        damaged = b' ' * (reading_state.MAX_BYTES + 1)
    else:
        field = 'positions' if damage == 'duplicate-position' else 'bookmarks'
        document[field].append(document[field][0])
        damaged = json.dumps(document).encode()
    state.path.write_bytes(damaged)

    with pytest.raises(ValueError):
        ReadingState(tmp_path).resume('library-a', 'edition-a')
    with pytest.raises(ValueError):
        state.add_bookmark(location(index=4), 'Must not replace corrupted data')

    assert state.path.read_bytes() == damaged
    assert_no_temporary(state)


@pytest.mark.parametrize('operation', ['fsync', 'replace'])
def test_precommit_disk_failure_preserves_previous_state_and_cleans_temp(
        tmp_path, monkeypatch, operation):
    state = ReadingState(tmp_path)
    bookmark = state.add_bookmark(location(), 'Already saved')
    before = state.path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'simulated storage failure')

    with monkeypatch.context() as patch:
        patch.setattr(reading_state.os, operation, fail)
        with pytest.raises(OSError, match='simulated storage failure'):
            state.add_bookmark(location(index=4), 'Uncommitted bookmark')

    assert state.path.read_bytes() == before
    assert ReadingState(tmp_path).bookmarks('library-a', 'edition-a') == (bookmark,)
    assert_no_temporary(state)


def test_external_change_during_save_is_not_overwritten(tmp_path, monkeypatch):
    state = ReadingState(tmp_path)
    bookmark = state.add_bookmark(location(), 'Original name')
    changed = json.loads(state.path.read_text(encoding='utf-8'))
    changed['bookmarks'][0]['label'] = 'A different writer saved this name'
    competing_bytes = json.dumps(changed, ensure_ascii=False).encode('utf-8')
    real_fsync = reading_state.os.fsync

    def external_writer(descriptor):
        real_fsync(descriptor)
        state.path.write_bytes(competing_bytes)

    with monkeypatch.context() as patch:
        patch.setattr(reading_state.os, 'fsync', external_writer)
        with pytest.raises(ValueError, match='changed before saving'):
            state.rename_bookmark('library-a', 'edition-a', bookmark.id, 'Must not win')

    assert state.path.read_bytes() == competing_bytes
    assert_no_temporary(state)


@pytest.mark.parametrize('link_kind', ['hardlink', 'symlink'])
def test_linked_state_file_is_rejected_without_touching_its_target(tmp_path, monkeypatch, link_kind):
    state = state_at(tmp_path / 'library')
    state.add_bookmark(location(), 'Protected original state')
    original = tmp_path / 'external-state.json'
    original.write_bytes(state.path.read_bytes())
    before = original.read_bytes()
    state.path.unlink()
    actual_link = True
    if link_kind == 'hardlink':
        os.link(original, state.path)
    else:
        try:
            state.path.symlink_to(original)
        except OSError as exc:
            if os.name != 'nt' or getattr(exc, 'winerror', None) != 1314:
                raise
            # Unprivileged Windows cannot create a real symlink. Exercise the
            # explicit reparse-point rejection on a real file instead; do not
            # skip a required suite or mislabel this branch as a real symlink.
            actual_link = False
            state.path.write_bytes(before)
            real_lstat = Path.lstat

            def reparse_lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if path == state.path:
                    return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                           st_file_attributes=0x400)
                return info

            monkeypatch.setattr(Path, 'lstat', reparse_lstat)

    with pytest.raises(ValueError):
        state.bookmarks('library-a', 'edition-a')
    with pytest.raises(ValueError):
        state.remember(location())

    assert original.read_bytes() == before
    if actual_link:
        assert state.path.samefile(original)
    else:
        assert state.path.read_bytes() == before
    assert_no_temporary(state)


def test_two_live_instances_merge_updates_and_report_real_lock_contention(tmp_path):
    first, second = ReadingState(tmp_path), ReadingState(tmp_path)
    original = first.add_bookmark(location(), 'Initial bookmark')
    before = first.path.read_bytes()
    with exclusive_writer(tmp_path):
        with pytest.raises(WriterBusy):
            second.add_bookmark(location(index=4), 'Must report busy')
    assert first.path.read_bytes() == before
    start = Barrier(2)

    def add(state, index):
        start.wait(timeout=5)
        try:
            return state.add_bookmark(location(index=index), f'Concurrent {index}')
        except WriterBusy:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add, first, 4), pool.submit(add, second, 5)]
        results = [future.result(timeout=10) for future in futures]
    assert any(result is not None for result in results)
    for index, result in zip((4, 5), results):
        if result is None:
            second.add_bookmark(location(index=index), f'Concurrent {index}')
    first.rename_bookmark('library-a', 'edition-a', original.id, 'Renamed after other writer')
    second.remember(location(index=5))

    reopened = ReadingState(tmp_path)
    assert {item.label for item in reopened.bookmarks('library-a', 'edition-a')} == {
        'Renamed after other writer', 'Concurrent 4', 'Concurrent 5'}
    assert reopened.resume('library-a', 'edition-a') == location(index=5)
    assert_no_temporary(first)


def test_export_is_scoped_and_import_is_idempotent_without_rewinding_resume(tmp_path):
    donor = state_at(tmp_path / 'donor')
    target = state_at(tmp_path / 'target')
    a, b = location(), location(edition='edition-b', kind='logical_segment', index=1)
    foreign = location(workspace='library-b')
    for item in (a, b, foreign):
        donor.remember(item)
        donor.add_bookmark(item, f'Bookmark {item.workspace} / {item.edition}')
    current = replace(a, index=8, text_sha256=sha256(b'Current page eight').hexdigest())
    target.remember(current)
    local = target.add_bookmark(current, 'Already on this device')
    exported = donor.export_state('library-a')
    document = json.loads(exported)
    assert document['workspace'] == 'library-a'
    assert all(row['location']['workspace'] == 'library-a'
               for field in ('positions', 'bookmarks') for row in document[field])
    validated = []

    def validate(item):
        assert item in (a, b)
        validated.append(item)

    assert target.import_state('library-a', exported, validate) == 3
    assert set(validated) == {a, b}
    assert target.resume('library-a', 'edition-a') == current
    assert target.resume('library-a', 'edition-b') == b
    assert local in target.bookmarks('library-a', 'edition-a')
    before = target.path.read_bytes()
    validated.clear()
    assert target.import_state('library-a', exported, validate) == 0
    assert set(validated) == {a, b}, 'Even existing imported locations require revalidation'
    assert target.path.read_bytes() == before
    assert_no_temporary(target)


@pytest.mark.parametrize('wrong_scope', ['requested-library', 'embedded-location'])
def test_import_rejects_foreign_scope_before_calling_source_validator(tmp_path, wrong_scope):
    donor = state_at(tmp_path / 'donor')
    target = state_at(tmp_path / 'target')
    donor.add_bookmark(location(), 'A bookmark')
    target.remember(location(index=9))
    before = target.path.read_bytes()
    document = json.loads(donor.export_state('library-a'))
    workspace = 'library-b' if wrong_scope == 'requested-library' else 'library-a'
    if wrong_scope == 'embedded-location':
        document['bookmarks'][0]['location']['workspace'] = 'library-b'

    def must_not_validate(item):
        pytest.fail('A foreign-library import reached the source resolver')

    with pytest.raises(ValueError, match='another library'):
        target.import_state(workspace, json.dumps(document), must_not_validate)
    assert target.path.read_bytes() == before
    assert_no_temporary(target)


def test_import_content_rejection_is_atomic_and_revalidates_existing_bookmarks(tmp_path):
    donor = state_at(tmp_path / 'donor')
    target = state_at(tmp_path / 'target')
    a, b = location(), location(edition='edition-b')
    donor.remember(a)
    existing = donor.add_bookmark(a, 'Existing source')
    first_export = donor.export_state('library-a')
    assert target.import_state('library-a', first_export, lambda item: None) == 2
    donor.remember(b)
    donor.add_bookmark(b, 'New source, no longer valid')
    before = target.path.read_bytes()
    checked = []

    def source_changed(item):
        checked.append(item)
        if item == b:
            raise ValueError('Retained source digest differs from the saved location')

    with pytest.raises(ValueError, match='source digest differs'):
        target.import_state('library-a', donor.export_state('library-a'), source_changed)
    assert checked == [a, b]
    assert target.path.read_bytes() == before
    assert target.resume('library-a', 'edition-b') is None
    assert target.bookmarks('library-a', 'edition-a') == (existing,)

    def revoked_existing_source(item):
        assert item == a
        raise ValueError('Existing source authorization was revoked')

    with pytest.raises(ValueError, match='authorization was revoked'):
        target.import_state('library-a', first_export, revoked_existing_source)
    assert target.path.read_bytes() == before
    assert_no_temporary(target)


def test_import_id_conflict_cannot_partially_publish_other_new_items(tmp_path):
    donor = state_at(tmp_path / 'donor')
    target = state_at(tmp_path / 'target')
    a, b = location(), location(edition='edition-b')
    existing = donor.add_bookmark(a, 'Local name')
    target.import_state('library-a', donor.export_state('library-a'), lambda item: None)
    donor.rename_bookmark('library-a', 'edition-a', existing.id, 'Conflicting imported name')
    donor.remember(b)
    added = donor.add_bookmark(b, 'Must not be partially imported')
    document = json.loads(donor.export_state('library-a'))
    document['bookmarks'].sort(key=lambda row: row['id'] != added.id)
    before = target.path.read_bytes()

    with pytest.raises(ValueError, match='conflicts with an existing bookmark'):
        target.import_state('library-a', json.dumps(document), lambda item: None)

    assert target.path.read_bytes() == before
    assert target.resume('library-a', 'edition-b') is None
    assert target.bookmarks('library-a', 'edition-b') == ()
    assert target.bookmarks('library-a', 'edition-a') == (existing,)
    assert_no_temporary(target)


@pytest.mark.parametrize('bad_field,value', [
    ('index', True), ('kind', 'guessed_page'), ('text_sha256', 'not-a-digest'),
    ('revision', 'revision\nspoofed')])
def test_invalid_import_location_never_reaches_resolver_or_changes_disk(tmp_path, bad_field, value):
    state = ReadingState(tmp_path)
    state.add_bookmark(location(), 'Keep this bookmark')
    document = json.loads(state.export_state('library-a'))
    document['bookmarks'][0]['location'][bad_field] = value
    before = state.path.read_bytes()

    def must_not_validate(item):
        pytest.fail('Invalid location reached the source resolver')

    with pytest.raises(ValueError):
        state.import_state('library-a', json.dumps(document), must_not_validate)
    assert state.path.read_bytes() == before
    assert_no_temporary(state)


def test_oversized_import_is_rejected_before_source_validation(tmp_path):
    state = ReadingState(tmp_path)
    state.remember(location())
    before = state.path.read_bytes()

    def must_not_validate(item):
        pytest.fail('Oversized import reached the source resolver')

    with pytest.raises(ValueError, match='size limit'):
        state.import_state('library-a', ' ' * (reading_state.MAX_BYTES + 1), must_not_validate)
    assert state.path.read_bytes() == before
    assert_no_temporary(state)


def test_import_cancelled_by_final_source_validation_keeps_all_previous_state(tmp_path):
    donor = state_at(tmp_path / 'donor')
    target = state_at(tmp_path / 'target')
    donor.remember(location())
    donor.add_bookmark(location(), 'Incoming bookmark')
    existing = target.add_bookmark(location(index=9), 'Keep this bookmark')
    before = target.path.read_bytes()
    cancelled = False
    validated = []

    def final_validation(item):
        nonlocal cancelled
        validated.append(item)
        cancelled = True

    with pytest.raises(InterruptedError, match='cancelled'):
        target.import_state('library-a', donor.export_state('library-a'), final_validation,
                            cancelled=lambda: cancelled)

    assert validated == [location()]
    assert target.path.read_bytes() == before
    assert target.resume('library-a', 'edition-a') is None
    assert target.bookmarks('library-a', 'edition-a') == (existing,)
    assert_no_temporary(target)


def test_cancellation_after_temporary_flush_prevents_atomic_publication(tmp_path, monkeypatch):
    state = ReadingState(tmp_path)
    existing = state.add_bookmark(location(), 'Keep this bookmark')
    before = state.path.read_bytes()
    cancelled = False
    real_fsync = reading_state.os.fsync
    observed_temporary = []

    def cancel_after_flush(descriptor):
        nonlocal cancelled
        real_fsync(descriptor)
        observed_temporary.extend(state.root.glob('.reading-state-*.tmp'))
        cancelled = True

    with monkeypatch.context() as patch:
        patch.setattr(reading_state.os, 'fsync', cancel_after_flush)
        with pytest.raises(InterruptedError, match='cancelled'):
            state.add_bookmark(location(index=8), 'Must never commit',
                               cancelled=lambda: cancelled)

    assert len(observed_temporary) == 1
    assert not observed_temporary[0].exists()
    assert state.path.read_bytes() == before
    assert ReadingState(tmp_path).bookmarks('library-a', 'edition-a') == (existing,)
    assert_no_temporary(state)
