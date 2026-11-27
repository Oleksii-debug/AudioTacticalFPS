"""Real substitution subprocess, immutable source, dictionary and recovery checks."""
from dataclasses import replace
from pathlib import Path
import json
import os
import shutil
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech import ReadingDocument
from carr_library.speech.reading import PronunciationRule, export_dictionary, import_dictionary
from carr_library.speech.source import read_source_range
from carr_library.ui.pronunciation_dialog import (
    DictionaryStore, ReadingSnapshot, apply_pronunciation_preview,
    export_reviewed_dictionary, preview_pronunciations, read_dictionary,
)


def test_preview_real_regex_and_scopes_then_one_undo_preserves_source_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pronunciation source')
    topic = app.create_topic(workspace, 'Evidence')
    original = 'Exact railway evidence.\r\nDr. Jones carries 12 kg. Dr. Smith.'
    imported = app.add_text(workspace, 'Synthetic source', original)
    hit, = app.search(workspace, 'railway evidence')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this note.')
    evidence = app.evidence(workspace, topic)
    doc = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1).reading_document()
    anchor = doc.source_anchor
    snapshot = ReadingSnapshot.capture(doc, (0, 0), language='en', voice_id='fixture:one')
    rules = (PronunciationRule('Dr.', 'Doctor'),
             PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True, language='en'),
             PronunciationRule('Doctor', 'Doktor', language='de'),
             PronunciationRule('Jones', 'JONES', voice_id='fixture:two'))
    preview = preview_pronunciations(snapshot, rules, language='en')
    assert doc.text == original and doc.revision == 0
    assert preview.after == 'Exact railway evidence.\r\nDoctor Jones carries 12 kilograms. Doctor Smith.'
    assert apply_pronunciation_preview(doc, preview, snapshot)
    assert doc.revision == 1 and doc.source_anchor == anchor and doc.source_text == original
    assert doc.source_span(0, len('Exact railway evidence.')).exact
    assert app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    assert app.evidence(workspace, topic) == evidence
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway evidence'
    assert doc.undo() and doc.text == original
    assert doc.redo() and doc.text == preview.after


def test_selected_unicode_range_is_the_only_changed_text():
    original = '😀 Dr. One\r\nDr. Two'
    doc = ReadingDocument(original)
    start = original.index('Dr. Two')
    snapshot = ReadingSnapshot.capture(doc, (start, len(original)), voice_id='voice')
    preview = preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'),), selection_only=True)
    assert preview.before == 'Dr. Two' and preview.after == 'Doctor Two'
    apply_pronunciation_preview(doc, preview, snapshot)
    assert doc.text == '😀 Dr. One\r\nDoctor Two'
    assert doc.source_span(0, start).exact
    assert doc.source_text == original
    assert doc.undo() and doc.text == original


@pytest.mark.parametrize('changed', ['text','selection','voice','language','identity','signature'])
def test_review_cannot_apply_after_scope_changes(changed):
    doc = ReadingDocument('Dr. One')
    snapshot = ReadingSnapshot.capture(doc, (0, 0), language='en', voice_id='voice')
    preview = preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'),))
    if changed == 'text':
        doc.set_text('Changed reading copy')
        current = ReadingSnapshot.capture(doc, (0, 0), language='en', voice_id='voice')
    else:
        values = {'selection': (0, 1), 'voice': 'other', 'language': 'uk', 'identity': -1, 'signature': ()}
        current = replace(snapshot, **{('voice_id' if changed == 'voice' else changed): values[changed]})
    before = doc.to_dict()
    with pytest.raises(ValueError, match='changed'):
        apply_pronunciation_preview(doc, preview, current)
    assert doc.to_dict() == before


def test_noop_preview_cannot_create_an_undo_for_an_unrelated_edit():
    doc = ReadingDocument('Original')
    doc.set_text('Existing reading edit')
    snapshot = ReadingSnapshot.capture(doc, (0, 0))
    preview = preview_pronunciations(snapshot, (PronunciationRule('absent', 'replacement'),))
    with pytest.raises(ValueError, match='no reading-text changes'):
        apply_pronunciation_preview(doc, preview, snapshot)
    assert doc.revision == 1 and doc.text == 'Existing reading edit'


def test_cancelled_and_invalid_regex_preview_never_changes_live_reading_copy():
    doc = ReadingDocument('Dr. One')
    snapshot = ReadingSnapshot.capture(doc, (0, 0))
    with pytest.raises(InterruptedError):
        preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'),), cancelled=lambda: True)
    with pytest.raises(Exception):
        preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'), PronunciationRule('(', 'X', regex=True)))
    assert doc.text == 'Dr. One' and doc.revision == 0


def test_actual_catastrophic_regex_deadline_leaves_live_copy_unchanged():
    doc = ReadingDocument('a' * 2000 + '!')
    snapshot = ReadingSnapshot.capture(doc, (0, 0))
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        preview_pronunciations(snapshot, (PronunciationRule('!', '?'), PronunciationRule('(a+)+$', 'X', regex=True)))
    assert time.monotonic() - started < 5
    assert doc.text == snapshot.text and doc.revision == 0


def test_dictionary_import_save_export_interoperates_without_execution(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    store = DictionaryStore(root)
    assert store.load() == ((), None)
    assert not store.root.exists()
    rules = (PronunciationRule('Київ', 'Kyiv', language='uk'), PronunciationRule('(', 'X', regex=True))
    selected = tmp_path / 'selected.json'; export_dictionary(rules, selected)
    imported, original_token = read_dictionary(selected)
    assert imported == rules  # Invalid regex is data until explicit Preview.
    token = store.save(imported, expected=None)
    assert token == original_token and store.load() == (rules, token)
    target = tmp_path / 'new.json'
    assert export_reviewed_dictionary(rules, target) == token
    assert import_dictionary(target) == rules
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        export_reviewed_dictionary((PronunciationRule('new', 'other'),), target)
    assert target.read_bytes() == before


def test_dictionary_concurrent_change_cannot_be_overwritten(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    first, other = DictionaryStore(root), DictionaryStore(root)
    initial = (PronunciationRule('Dr.', 'Doctor'),)
    token = first.save(initial, expected=None)
    other.save((PronunciationRule('St.', 'Street'),), expected=token)
    before = first.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        first.save((PronunciationRule('Mr.', 'Mister'),), expected=token)
    assert first.path.read_bytes() == before


@pytest.mark.parametrize('damage', ['duplicate','unknown','invalid_flag','oversized','hardlink'])
def test_bad_dictionary_import_keeps_saved_rules(tmp_path, damage):
    root = tmp_path / 'library'; root.mkdir()
    store = DictionaryStore(root)
    token = store.save((PronunciationRule('Dr.', 'Doctor'),), expected=None)
    before = store.path.read_bytes()
    source = tmp_path / 'unsafe.json'
    if damage == 'duplicate':
        source.write_text('{"schema":"carr.pronunciations.v1","schema":"other","rules":[]}', encoding='utf-8')
    elif damage == 'unknown':
        source.write_text('{"schema":"carr.pronunciations.v1","rules":[],"execute":true}', encoding='utf-8')
    elif damage == 'invalid_flag':
        source.write_text(json.dumps({'schema':'carr.pronunciations.v1','rules':[
            {'pattern':'x','replacement':'y','regex':1}]}), encoding='utf-8')
    elif damage == 'hardlink':
        os.link(store.path, source)
    else:
        with source.open('wb') as stream:
            stream.truncate(8_000_001)
    with pytest.raises(ValueError):
        read_dictionary(source)
    if damage == 'hardlink':
        source.unlink()
    assert store.path.read_bytes() == before and store.load()[1] == token


def test_dictionary_cancel_preserves_previous_library_state_and_exports(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    store = DictionaryStore(root)
    rules = (PronunciationRule('Dr.', 'Doctor'),)
    token = store.save(rules, expected=None)
    before = store.path.read_bytes()
    with pytest.raises(InterruptedError):
        store.save((), expected=token, cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        export_reviewed_dictionary(rules, tmp_path / 'cancelled.json', cancelled=lambda: True)
    assert not (tmp_path / 'cancelled.json').exists() and store.path.read_bytes() == before


def test_dictionary_is_restored_after_original_state_is_deleted(tmp_path):
    app = LibraryApp(tmp_path / 'library'); app.create_workspace('Dictionary backup')
    rules = (PronunciationRule('Kyiv', 'Київ', language='uk', voice_id='reviewed:local'),)
    store = DictionaryStore(app.root); token = store.save(rules, expected=None)
    backup = tmp_path / 'backup'; manifest = app.backup(backup)
    shutil.rmtree(app.root)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=manifest)
    assert DictionaryStore(restored).load() == (rules, token)
