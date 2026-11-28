"""Actual compound source maps, stale grants, persistence and complete recovery."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import shutil

import pytest

from carr_library.speech.reading import ReadingChange, ReadingDocument, PronunciationRule
from carr_library.speech.preparation import capture_speech, prepare_selection, apply_speech, undo_speech
from carr_library.reader.speech_normalization import SpeechOptions
from carr_library.speech.session import ReaderDraftStore, ReaderResume


def prepared(document, selection=None, **options):
    bounds = selection or (0, len(document.text))
    snapshot = capture_speech(document, bounds, language='en-US', voice_id='fixture:voice')
    proposal = prepare_selection(snapshot, bounds, SpeechOptions(mode='speech', locale='en-US', **options))
    return snapshot, proposal


def test_one_history_action_keeps_unchanged_interior_exact_and_undo_redo(tmp_path):
    source = '😀 12 untouched e\u0301 interior 5 kg.\r\n'
    document = ReadingDocument(source)
    snapshot, proposal = prepared(document, numbers=True, units=True)
    assert apply_speech(document, proposal, snapshot)
    expected = '😀 twelve untouched e\u0301 interior five kilograms.\r\n'
    assert document.text == expected and document.source_text == source and document.revision == 1
    interior = ' untouched e\u0301 interior '
    start = expected.index(interior)
    span = document.source_span(start, start + len(interior))
    assert span.exact and source[span.start:span.end] == interior
    changed = document.source_span(2, 8)
    assert not changed.exact and source[changed.start:changed.end] == '12'
    assert document.to_dict()['schema'] == 'carr.reading-copy.v3'
    saved = tmp_path / 'prepared.json'
    document.save(saved)
    reopened = ReadingDocument.load(saved)
    assert reopened.to_dict() == document.to_dict()
    assert reopened.source_span(start, start + len(interior)) == span
    assert reopened.undo() and reopened.text == source
    assert reopened.redo() and reopened.text == expected
    assert document.undo() and document.text == source and document.redo()


def test_v1_flat_history_output_and_mixed_replay_keep_the_existing_fields():
    legacy = ReadingDocument('12 unchanged')
    legacy.replace(0, 2, 'twelve', label='Old writer')
    assert legacy.to_dict() == {'schema': 'carr.reading-copy.v1', 'title': 'Reading copy',
        'source_text': '12 unchanged', 'source_sha256': sha256(b'12 unchanged').hexdigest(),
        'edits': [{'start': 0, 'end': 2, 'replacement': 'twelve', 'label': 'Old writer'}], 'revision': 1}
    assert ReadingDocument.from_dict(legacy.to_dict()).to_dict() == legacy.to_dict()
    document = ReadingDocument('12 keep 5')
    snapshot, proposal = prepared(document, numbers=True)
    apply_speech(document, proposal, snapshot)
    document.replace(0, 6, 'TWELVE', label='Later manual edit')
    reopened = ReadingDocument.from_dict(document.to_dict())
    assert reopened.text == document.text and reopened.undo() and reopened.text == 'twelve keep five'
    assert reopened.undo() and reopened.text == '12 keep 5'


def test_previously_accepted_manual_and_voice_pronunciations_win_without_another_store():
    document = ReadingDocument('12 keep 5 kg')
    document.replace(0, 2, '12 MANUAL')
    snapshot, proposal = prepared(document, numbers=True, units=True)
    assert proposal.result.text == '12 MANUAL keep five kilograms'
    apply_speech(document, proposal, snapshot)
    assert document.text.startswith('12 MANUAL') and document.revision == 2
    document.apply_pronunciations((PronunciationRule('five', 'general'),
        PronunciationRule('general', 'VOICE', language='en-US', voice_id='fixture:voice')),
        language='en-US', voice_id='fixture:voice')
    assert document.text == '12 MANUAL keep VOICE kilograms'
    assert document.source_text == '12 keep 5 kg'


@pytest.mark.parametrize('change', ['text', 'history', 'selection', 'language', 'voice', 'identity'])
def test_current_document_selection_voice_and_history_are_required_at_apply(change):
    document = ReadingDocument('12 keep 5')
    snapshot, proposal = prepared(document, numbers=True)
    current = snapshot
    if change == 'text':
        document.replace(3, 7, 'KEPT')
        current = capture_speech(document, snapshot.selection, language=snapshot.language, voice_id=snapshot.voice_id)
    elif change == 'history':
        document.replace(3, 7, 'KEPT')
        document.undo()
        current = capture_speech(document, snapshot.selection, language=snapshot.language, voice_id=snapshot.voice_id)
    elif change == 'selection':
        current = replace(snapshot, selection=(0, 2))
    elif change == 'language':
        current = replace(snapshot, language='sk-SK')
    elif change == 'voice':
        current = replace(snapshot, voice_id='different')
    else:
        document = ReadingDocument(document.text)
    before = document.to_dict()
    with pytest.raises(ValueError, match='changed'):
        apply_speech(document, proposal, current)
    assert document.to_dict() == before


@pytest.mark.parametrize('damage', [
    'overlap', 'order', 'before', 'output_hash', 'before_hash', 'boolean_start',
    'empty', 'unknown_field', 'unknown_schema', 'missing_anchor', 'legacy_compound', 'empty_compound',
])
def test_saved_compound_replay_rejects_malformed_history_without_fabricating_maps(damage):
    document = ReadingDocument('12 keep 5')
    snapshot, proposal = prepared(document, numbers=True)
    apply_speech(document, proposal, snapshot)
    data = document.to_dict()
    entry = data['edits'][0]
    if damage == 'overlap':
        entry['changes'].append(deepcopy(entry['changes'][0]))
    elif damage == 'order':
        entry['changes'].reverse()
    elif damage == 'before':
        entry['changes'][0]['before'] = '13'
    elif damage == 'output_hash':
        entry['after_sha256'] = '0' * 64
    elif damage == 'before_hash':
        entry['before_sha256'] = '0' * 64
    elif damage == 'boolean_start':
        entry['changes'][0]['start'] = False
    elif damage == 'empty':
        entry['changes'][0]['after'] = entry['changes'][0]['before']
    elif damage == 'unknown_field':
        entry['changes'][0]['authority'] = True
    elif damage == 'unknown_schema':
        data['schema'] = 'carr.reading-copy.v4'
    elif damage == 'missing_anchor':
        del data['source_anchor']
    elif damage == 'legacy_compound':
        data['schema'] = 'carr.reading-copy.v1'
        del data['source_anchor']
    else:
        data['edits'] = []
        data['revision'] = 0
    with pytest.raises((ValueError, TypeError)):
        ReadingDocument.from_dict(data)


def test_history_overflow_invalid_later_change_and_oversized_output_are_atomic():
    document = ReadingDocument('12 keep 5')
    for index in range(500):
        document.replace(3, 7, 'KEEP' if index % 2 == 0 else 'keep')
    before = document.to_dict()
    with pytest.raises(ValueError, match='history'):
        document.apply_changes((ReadingChange(0, 2, '12', 'twelve'),))
    assert document.to_dict() == before
    other = ReadingDocument('12 keep 5')
    before = other.to_dict()
    with pytest.raises(ValueError):
        other.apply_changes((ReadingChange(0, 2, '12', 'twelve'), ReadingChange(8, 9, 'wrong', 'five')))
    assert other.to_dict() == before
    with pytest.raises(ValueError):
        other.apply_changes((ReadingChange(0, 2, '12', 'x' * 1_000_001),))
    assert other.to_dict() == before


def test_exported_history_cannot_mutate_in_memory_compound_entries():
    document = ReadingDocument('12 keep 5')
    snapshot, proposal = prepared(document, numbers=True)
    apply_speech(document, proposal, snapshot)
    before = document._signature()
    exported = document.to_dict()
    exported['edits'][0]['changes'][0]['after'] = 'foreign'
    assert document._signature() == before and document.text == 'twelve keep five'


def test_preparation_cancel_and_late_scope_never_create_partial_history():
    document = ReadingDocument(' '.join(str(value) for value in range(100)))
    snapshot = capture_speech(document, (0, len(document.text)))
    calls = []
    def stop():
        calls.append(True)
        return len(calls) == 15
    with pytest.raises(InterruptedError):
        prepare_selection(snapshot, snapshot.selection, SpeechOptions(mode='speech', locale='en-US', numbers=True),
                          cancelled=stop)
    assert len(calls) == 15 and document.revision == 0 and document.text == document.source_text


def test_actual_source_bound_draft_complete_backup_fresh_restore_and_undo(tmp_path, monkeypatch):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual prepared copy')
    original = tmp_path / 'original.txt'
    raw = 'First 12 intact e\u0301.\r\n\fSecond 5 kg.\r\n'.encode('utf-8')
    original.write_bytes(raw)
    book = app.import_file(workspace, original)
    captured = read_source_range(ReadingDesk(app, workspace, book['edition']), 1, 2)
    document = captured.reading_document()
    anchor = document.source_anchor
    legacy = document.to_dict()
    assert legacy['schema'] == 'carr.reading-copy.v2'
    assert ReadingDocument.from_dict(legacy).to_dict() == legacy
    snapshot, proposal = prepared(document, numbers=True, units=True)
    apply_speech(document, proposal, snapshot)
    assert document.source_anchor == anchor and anchor['quotation_authority'] is False
    assert document.to_dict()['schema'] == 'carr.reading-copy.v3'
    store = ReaderDraftStore(app.root, slot='prepared-fixture')
    store.save(ReaderResume(document, 'fixture:voice', cursor=0))
    exact = store.path.read_bytes()
    names = {path.name: path.read_bytes() for path in app.assets_dir.iterdir() if path.is_file()}
    summaries = ReaderDraftStore(app.root).list_saved()
    selected = next(item for item in summaries.copies if item.key == store.key)
    assert selected.source_recorded and selected.reading_revision == 1
    reopened, _ = ReaderDraftStore(app.root).open_saved(selected)
    assert reopened.document.to_dict() == document.to_dict()
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    assert json.loads((folder / 'manifest.json').read_bytes())['version'] == 4
    verify_backup(folder, digest)
    assert app.close_search()
    original.unlink()
    shutil.rmtree(app.root)
    from carr_library.reader import speech_normalization
    monkeypatch.setattr(speech_normalization, 'prepare_speech',
                        lambda *a, **k: pytest.fail('Restore activated preparation'))
    restored_root = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(restored_root)
    try:
        recovered_store = ReaderDraftStore(restored.root, slot='prepared-fixture')
        assert recovered_store.path.read_bytes() == exact
        resumed = recovered_store.load()
        assert resumed.document.to_dict() == document.to_dict()
        assert resumed.document.source_anchor == anchor
        assert {path.name: path.read_bytes() for path in restored.assets_dir.iterdir() if path.is_file()} == names
        assert read_source_range(ReadingDesk(restored, workspace, book['edition']), 1, 2).text == captured.text
        copy = resumed.document
        location = copy.text.index(' intact e\u0301.')
        assert copy.source_span(location, location + len(' intact e\u0301.')).exact
        assert copy.undo() and copy.text == captured.text
        assert copy.redo() and copy.text == document.text
    finally:
        assert restored.close_search()
