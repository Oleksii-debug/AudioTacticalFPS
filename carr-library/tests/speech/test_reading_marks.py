"""Bookmarks survive real projects/recovery/backup without becoming source authority."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import shutil
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.marks import MAX_MARKS, ReadingBookmark, join_next_break, split_paragraph
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.tabs import ReadingTab, ReadingTabs, ReadingTabsStore
from carr_library.ui.reader_tabs import TabState


def test_real_project_round_trip_preserves_bookmark_identity_history_and_exact_original(tmp_path):
    doc = ReadingDocument('😀 Original\r\nДругий рядок.', title='My copy')
    initial = doc.to_dict()
    assert initial['schema'] == 'carr.reading-copy.v1'
    doc.set_text('😀 Revised\r\nДругий рядок.')
    mark = doc.add_bookmark('Повернутися сюди', 12)
    path = tmp_path / 'copy.reading.json'
    doc.save(path)
    assert not doc.dirty
    fresh = ReadingDocument.load(path)
    assert fresh.to_dict() == doc.to_dict()
    assert fresh.bookmark_position(mark.id) == 12
    assert fresh.source_text == initial['source_text']
    assert fresh.source_sha256 == initial['source_sha256']
    assert fresh.source_anchor is None
    assert fresh.undo() and fresh.text == initial['source_text']
    with pytest.raises(ValueError, match='text changed'):
        fresh.bookmark_position(mark.id)
    assert fresh.redo() and fresh.bookmark_position(mark.id) == 12
    fresh.remove_bookmark(mark.id)
    assert fresh.dirty and not fresh.bookmarks
    assert fresh.to_dict()['schema'] == 'carr.reading-copy.v1'
    fresh.save(path)
    assert not ReadingDocument.load(path).bookmarks


def test_bookmark_is_bound_to_exact_text_and_undo_restores_it():
    doc = ReadingDocument('First exact text.')
    mark = doc.add_bookmark('Beginning', 3)
    before = doc._text_signature()
    assert doc._signature() != before
    doc.set_text('Other exact text.')
    with pytest.raises(ValueError, match='text changed'):
        doc.bookmark_position(mark.id)
    assert doc.undo() and doc.bookmark_position(mark.id) == 3
    restored = ReadingDocument.from_dict(doc.to_dict())
    assert restored.bookmark_position(mark.id) == 3
    assert restored.redo()
    with pytest.raises(ValueError, match='text changed'):
        restored.bookmark_position(mark.id)
    with pytest.raises(ValueError, match='existing'):
        doc.remove_bookmark('missing')


@pytest.mark.parametrize(('field', 'value'), [
    ('id', '../path'), ('id', 'A' * 32), ('id', 'a' * 31),
    ('label', ''), ('label', ' '), ('label', 'x' * 81),
    ('label', 'New\nline'), ('label', '\ud800'), ('label', '\x7f'),
    ('text_sha256', 'a' * 63), ('text_sha256', 'A' * 64),
    ('offset', True), ('offset', -1), ('offset', 2_000_001), ('offset', 1.5),
])
def test_invalid_passive_bookmarks_are_rejected(field, value):
    doc = ReadingDocument('Text')
    doc.add_bookmark('Here', 1)
    payload = doc.to_dict()
    payload['bookmarks'][0][field] = value
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(payload)


@pytest.mark.parametrize('change', ['extra', 'duplicate', 'many', 'mapping', 'execute'])
def test_bookmark_wire_is_strict_and_bounded(change):
    doc = ReadingDocument('Text')
    doc.add_bookmark('Here', 1)
    payload = doc.to_dict()
    if change == 'extra': payload['bookmarks'][0]['path'] = '../outside'
    elif change == 'duplicate': payload['bookmarks'].append(dict(payload['bookmarks'][0]))
    elif change == 'many':
        payload['bookmarks'] = [dict(payload['bookmarks'][0], id=f'{i:032x}') for i in range(MAX_MARKS + 1)]
    elif change == 'mapping': payload['bookmarks'] = {}
    elif change == 'execute': payload['activate_engine'] = True
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(payload)


def test_bookmark_limit_and_invalid_cursor_do_not_change_previous_marks():
    doc = ReadingDocument('Text')
    for offset in (-1, 5, True):
        with pytest.raises(ValueError):
            doc.add_bookmark('Here', offset)
    assert not doc.bookmarks
    for _ in range(MAX_MARKS):
        doc.add_bookmark('Here', 1)
    before = doc.bookmarks
    with pytest.raises(ValueError, match='256'):
        doc.add_bookmark('Too many', 2)
    assert doc.bookmarks == before


@pytest.mark.parametrize('source', ['😀 Alpha beta\nGamma.', '😀 Alpha beta\r\nGamma.'])
def test_paragraph_split_and_join_are_independent_undoable_reading_edits(source):
    doc = ReadingDocument(source)
    newline = '\r\n' if '\r\n' in source else '\n'
    original_digest = doc.source_sha256
    position = split_paragraph(doc, 5)
    assert position == 5 + 2 * len(newline)
    assert doc.text == source[:5] + newline * 2 + source[5:]
    split = doc.text
    join_next_break(doc, 0)
    assert doc.text == source[:5] + ' ' + source[5:]
    assert doc.undo() and doc.text == split
    assert doc.undo() and doc.text == source
    assert doc.source_text == source and doc.source_sha256 == original_digest
    assert doc.redo() and doc.redo()


@pytest.mark.parametrize(('text', 'cursor'), [('Text', 0), ('Text', 4), ('One\nTwo', 3), ('One\r\nTwo', 4)])
def test_invalid_split_keeps_history_and_original(text, cursor):
    doc = ReadingDocument(text)
    before = doc.to_dict()
    with pytest.raises(ValueError):
        split_paragraph(doc, cursor)
    assert doc.to_dict() == before


@pytest.mark.parametrize(('text', 'cursor'), [('Text', 0), ('Text\n', 0), ('\nText', 0), ('One\r\nTwo', 4)])
def test_invalid_join_keeps_history_and_original(text, cursor):
    doc = ReadingDocument(text)
    before = doc.to_dict()
    with pytest.raises(ValueError):
        join_next_break(doc, cursor)
    assert doc.to_dict() == before


def test_join_mixed_blank_lines_and_spaces_changes_only_the_next_boundary():
    doc = ReadingDocument('One  \r\n \t\r\n  Two\nThree')
    join_next_break(doc, 0)
    assert doc.text == 'One Two\nThree'
    assert doc.undo() and doc.text == doc.source_text


def test_adding_passive_bookmarks_does_not_invalidate_saved_audio_position(tmp_path):
    from carr_library.ui.reader_tabs import NativeReaderTabs
    doc = ReadingDocument('Original exact text')
    state = TabState(ReadingTab('primary', 'Copy'),
        ReaderResume(doc, 'missing:voice', 0, 80, 1, 4.25),
        ReaderDraftStore(tmp_path), None)
    tabs = NativeReaderTabs.__new__(NativeReaderTabs)
    tabs.states, tabs.active = [state], 0
    doc.add_bookmark('Here', 1)
    assert tabs.saved_audio_position(doc) == 4.25
    doc.set_text('Changed text')
    assert tabs.saved_audio_position(doc) == 0


def test_bookmarks_survive_actual_backup_delete_and_relocated_recovery(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    app = LibraryApp(root)
    workspace = app.create_workspace('Bookmark recovery')
    app.add_text(workspace, 'Retained source', 'Immutable source.', edition_id='original')
    doc = ReadingDocument('Original reading copy.')
    doc.set_text('Revised reading copy.')
    mark = doc.add_bookmark('Reviewed location', 8)
    store = ReadingTabsStore(root)
    tab = ReadingTab('primary', 'My copy')
    store.draft(tab).save_with_token(ReaderResume(doc, 'missing:exact-voice', 1, 67, 8, 2.5),
                                    expected_token=None)
    store.save(ReadingTabs((tab,), tab.id), expected_token=None)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Passive bookmarks executed a process.'))
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    fresh = ReadingTabsStore(restored).draft(tab).load()
    assert fresh.document.to_dict() == doc.to_dict()
    assert fresh.document.bookmark_position(mark.id) == 8
    assert (fresh.voice_id, fresh.cursor, fresh.audio_seconds) == ('missing:exact-voice', 8, 2.5)
    assert fresh.document.undo() and fresh.document.text == doc.source_text
