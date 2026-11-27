"""Actual reading copies, source ranges and passive recovery for Read/Skip."""
from copy import deepcopy
from hashlib import sha256
import shutil
import subprocess
from threading import Event, Thread, get_ident

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech import narration
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.source import read_source_range
from carr_library.speech.tabs import ReadingTab, ReadingTabs, ReadingTabsStore
from carr_library.ui.reader_tabs import NativeReaderTabs, TabState


@pytest.mark.parametrize('category', narration.CATEGORIES)
def test_only_explicit_reviewed_category_is_skipped_and_read_restores_every_byte(category):
    original = '😀 Before [reviewed content] після.'
    doc = ReadingDocument(original)
    first, last = original.index('['), original.index(']') + 1
    region = doc.mark_narration_region(first, last, category, 'Explicit selected content')
    before = doc.to_dict()
    assert doc.narration_selection().text == original
    assert doc.revision == 0 and not doc.can_undo
    doc.set_narration_read(category, False)
    selected = doc.narration_selection()
    assert selected.text == original[:first] + '\n' + original[last:]
    assert selected.skipped == (region.id,) and selected.filtered and not selected.empty
    assert selected.text_sha256 == sha256(selected.text.encode()).hexdigest()
    assert doc.source_text == doc.text == original
    assert doc.to_dict()['edits'] == before['edits'] == []
    doc.set_narration_read(category, True)
    assert doc.narration_selection().text == original
    assert doc.narration_selection().skipped == ()


def test_each_kept_slice_and_inserted_join_has_honest_source_mapping():
    doc = ReadingDocument('First NOTE second.')
    doc.mark_narration_region(6, 10, 'footnote', 'Reviewed note')
    doc.set_narration_read('footnote', False)
    selection = doc.narration_selection()
    left, join, right = selection.slices
    assert selection.text == 'First \n second.'
    assert (left.reading_start, left.reading_end, left.source_start, left.source_end) == (0, 6, 0, 6)
    assert (right.reading_start, right.reading_end, right.source_start, right.source_end) == (10, 18, 10, 18)
    assert left.source_exact and right.source_exact
    assert join.derived_separator and not join.source_exact
    assert join.reading_start is join.reading_end is join.source_start is join.source_end is None
    record = selection.provenance()
    assert record['quotation_authority'] is False and record['classification'] == 'explicit-user-selection'
    assert record['source_sha256'] == doc.source_sha256 and record['source_anchor'] is None
    for item in (left, right):
        assert selection.text[item.start:item.end] == doc.text[item.reading_start:item.reading_end]


def test_selection_clipping_and_all_skipped_text_are_explicit_and_never_fabricated():
    doc = ReadingDocument('Before code after')
    doc.mark_narration_region(7, 11, 'code', 'Code')
    doc.set_narration_read('code', False)
    empty = doc.narration_selection(7, 11)
    assert empty.empty and empty.text == '' and empty.slices == () and empty.filtered
    assert doc.narration_selection(8, 14).text == ' af'
    assert doc.narration_selection(0, 6).text == 'Before'
    assert doc.text == doc.source_text == 'Before code after'


def test_changed_text_refuses_old_skip_until_explicit_read_or_exact_undo():
    doc = ReadingDocument('One note two.')
    doc.mark_narration_region(4, 8, 'footnote', 'Note')
    doc.set_narration_read('footnote', False)
    old = doc.narration_selection()
    doc.replace(0, 3, 'Changed')
    with pytest.raises(ValueError, match='changed'):
        old.check(doc)
    with pytest.raises(ValueError, match='changed reading text'):
        doc.narration_selection()
    doc.set_narration_read('footnote', True)
    assert doc.narration_selection().text == 'Changed note two.'
    assert doc.undo() and doc.text == doc.source_text
    doc.set_narration_read('footnote', False)
    assert doc.narration_selection().text == 'One \n two.'
    assert doc.redo()
    with pytest.raises(ValueError, match='changed reading text'):
        doc.narration_selection()


def test_policy_change_alone_revokes_preview_and_saved_audio_position(tmp_path):
    doc = ReadingDocument('One note two.')
    doc.mark_narration_region(4, 8, 'footnote', 'Note')
    selection = doc.narration_selection()
    state = TabState(ReadingTab('primary', 'Copy'),
        ReaderResume(doc, 'missing:voice', 0, 80, 0, 3.25), ReaderDraftStore(tmp_path), None)
    tabs = NativeReaderTabs.__new__(NativeReaderTabs); tabs.states, tabs.active = [state], 0
    assert tabs.saved_audio_position(doc) == 3.25
    signature = doc._signature()
    doc.set_narration_read('footnote', False)
    with pytest.raises(ValueError, match='policy changed'):
        selection.check(doc)
    assert tabs.saved_audio_position(doc) == 0
    before = doc.to_dict()
    with pytest.raises(ValueError, match='policy changed'):
        doc.set_narration_policy(None, expected_signature=signature)
    assert doc.to_dict() == before


def test_old_no_policy_versions_keep_their_exact_serialized_shape_and_identity(tmp_path):
    legacy = ReadingDocument('Original text.')
    rows = [legacy.to_dict()]
    legacy.add_bookmark('Original location', 2)
    rows.append(legacy.to_dict())
    for raw in rows:
        original = encode(raw)
        doc = ReadingDocument.from_dict(raw)
        signature = doc._text_signature()
        assert encode(doc.to_dict()) == original
        region = doc.mark_narration_region(0, 8, 'heading', 'Reviewed heading')
        assert doc.to_dict()['schema'] == 'carr.reading-copy.v4'
        doc.remove_narration_region(region.id)
        assert encode(doc.to_dict()) == original and doc._text_signature() == signature
    assert [row['schema'] for row in rows] == ['carr.reading-copy.v1', 'carr.reading-copy.v3']


def test_project_save_load_preserves_policy_and_text_history_without_opening_engines(tmp_path, monkeypatch):
    doc = ReadingDocument('Original note end.')
    doc.replace(0, 8, 'Edited')
    doc.mark_narration_region(7, 11, 'footnote', 'Manual note')
    doc.set_narration_read('footnote', False)
    doc.add_bookmark('Independent bookmark', 0)
    expected = doc.narration_selection().text
    path = tmp_path / 'reading.json'
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Reading metadata started an engine.'))
    doc.save(path)
    assert not doc.dirty
    loaded = ReadingDocument.load(path)
    assert encode(loaded.to_dict()) == encode(doc.to_dict())
    assert loaded.narration_selection().text == expected and not loaded.dirty
    assert loaded.undo() and loaded.text == 'Original note end.'
    with pytest.raises(ValueError, match='changed reading text'):
        loaded.narration_selection()
    assert loaded.redo() and loaded.narration_selection().text == expected


@pytest.mark.parametrize('damage', ['unknown', 'range', 'digest', 'duplicate', 'skip', 'many', 'authority'])
def test_typed_policy_and_current_text_identity_are_validated_on_restore(damage):
    doc = ReadingDocument('One note two.')
    doc.mark_narration_region(4, 8, 'footnote', 'Note')
    raw = doc.to_dict()
    if damage == 'unknown': raw['narration']['regions'][0]['path'] = '../outside'
    elif damage == 'range': raw['narration']['regions'][0]['end'] = len(doc.text) + 1
    elif damage == 'digest': raw['narration']['regions'][0]['selected_sha256'] = '0' * 64
    elif damage == 'duplicate': raw['narration']['regions'].append(deepcopy(raw['narration']['regions'][0]))
    elif damage == 'skip': raw['narration']['skip'] = ['code']
    elif damage == 'many': raw['narration']['regions'] *= narration.MAX_REGIONS + 1
    else: raw['narration']['quotation_authority'] = True
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(raw)


def test_overlap_and_byte_bounds_preserve_previous_reviewed_policy(monkeypatch):
    doc = ReadingDocument('One note two.')
    doc.mark_narration_region(4, 8, 'footnote', 'Note')
    before = doc.to_dict()
    with pytest.raises(ValueError, match='overlap'):
        doc.mark_narration_region(6, 10, 'table', 'Overlapping table')
    assert doc.to_dict() == before
    monkeypatch.setattr(narration, 'MAX_POLICY_BYTES', 1)
    with pytest.raises(ValueError, match='byte allowance'):
        doc.set_narration_policy(doc.narration_policy)
    assert doc.to_dict() == before


def test_cancel_during_mapping_publishes_no_projection_or_policy_change(monkeypatch):
    doc = ReadingDocument('One note two.')
    doc.mark_narration_region(4, 8, 'footnote', 'Note')
    doc.set_narration_read('footnote', False)
    before = doc.to_dict()
    stop, real = Event(), doc.source_span
    def mapped(first, last):
        result = real(first, last)
        stop.set()
        return result
    monkeypatch.setattr(doc, 'source_span', mapped)
    with pytest.raises(InterruptedError):
        doc.narration_selection(cancelled=stop.is_set)
    assert doc.to_dict() == before


def test_actual_source_recovery_backup_delete_restore_preserves_policy_and_not_quote_authority(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    app = LibraryApp(root); workspace = app.create_workspace('Reading policy recovery')
    original = 'First [note] sentence.\fSecond source position.'
    imported = app.add_text(workspace, 'Source', original)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 2)
    doc = source.reading_document()
    assert doc.to_dict()['schema'] == 'carr.reading-copy.v2'
    original_anchor = deepcopy(doc.source_anchor)
    doc.mark_narration_region(6, 12, 'footnote', 'Explicit footnote')
    doc.set_narration_read('footnote', False)
    selection = doc.narration_selection()
    tab = ReadingTab('primary', 'Bound copy'); store = ReadingTabsStore(root)
    store.draft(tab).save_with_token(ReaderResume(doc, 'missing:exact', 0, 67, 0, 0), expected_token=None)
    store.save(ReadingTabs((tab,), tab.id), expected_token=None)
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('Passive restore executed a process.'))
    folder = tmp_path / 'backup'; checksum = create_backup(app, folder)
    shutil.rmtree(root)
    restored = restore_backup(folder, tmp_path / 'restored', checksum)
    fresh = ReadingTabsStore(restored).draft(tab).load().document
    assert encode(fresh.to_dict()) == encode(doc.to_dict())
    assert fresh.source_anchor == original_anchor and fresh.source_anchor['quotation_authority'] is False
    assert fresh.narration_selection().provenance() == selection.provenance()
    restored_app = LibraryApp(restored)
    assert restored_app.assets.resolve(imported['asset']).read_bytes() == original.encode()
    fresh.set_narration_read('footnote', True)
    assert fresh.narration_selection().text == source.text


def test_edit_between_snapshot_text_and_policy_cannot_bind_old_text_to_new_revision(monkeypatch):
    doc = ReadingDocument('First unchanged text.')
    observed, release = Event(), Event()
    identity, result = [], []
    original = ReadingDocument.text.fget

    def text(self):
        value = original(self)
        if identity and get_ident() == identity[0] and not observed.is_set():
            observed.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(ReadingDocument, 'text', property(text))
    def project():
        identity.append(get_ident())
        try:
            result.append(doc.narration_selection())
        except BaseException as exc:
            result.append(exc)

    worker = Thread(target=project)
    worker.start()
    try:
        assert observed.wait(5)
        # Same length keeps range validation from hiding the snapshot-order race.
        doc.replace(0, 5, 'Other')
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(result) == 1
    assert isinstance(result[0], ValueError), result
    assert 'changed' in str(result[0])
    assert doc.text == 'Other unchanged text.' and doc.source_text == 'First unchanged text.'
