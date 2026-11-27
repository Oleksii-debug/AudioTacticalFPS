"""Atomic valid-draft recovery, not a simulated native screen-reader test."""
import json

import pytest

from carr_library.speech import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume, decode_reader_resume


def test_complete_reading_draft_survives_restart(tmp_path):
    store = ReaderDraftStore(tmp_path, slot='manual')
    document = ReadingDocument('Оригінал\r\nOriginal text.')
    document.replace(0, 8, 'Читання')
    state = ReaderResume(document, 'flite:slt', 0, 70, 5, 12.5)
    target = store.save(state)
    assert target.is_relative_to(tmp_path / 'state' / 'speech')
    recovered = ReaderDraftStore(tmp_path, slot='manual').load()
    assert recovered.document.source_text == document.source_text
    assert recovered.document.text == document.text
    assert (recovered.voice_id, recovered.volume, recovered.cursor, recovered.audio_seconds) == ('flite:slt', 70, 5, 12.5)
    assert recovered.document.undo()
    assert recovered.document.text == recovered.document.source_text


def test_distinct_sources_have_distinct_recovery_slots(tmp_path):
    first = ReaderDraftStore(tmp_path, slot='library-A/edition/revision/1-3')
    second = ReaderDraftStore(tmp_path, slot='library-B/edition/revision/1-3')
    first.save(ReaderResume(ReadingDocument('Private source A')))
    assert second.load() is None
    second.save(ReaderResume(ReadingDocument('Private source B')))
    assert first.load().document.source_text == 'Private source A'
    assert second.load().document.source_text == 'Private source B'


def test_invalid_recovery_fails_closed_and_preserves_previous(tmp_path):
    store = ReaderDraftStore(tmp_path)
    document = ReadingDocument('ABC')
    store.save(ReaderResume(document))
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(ReaderResume(document, cursor=4))
    assert store.path.read_bytes() == before
    record = json.loads(before)
    record['document']['source_text'] = 'Tampered'
    store.path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='checksum'):
        store.load()


def test_reader_draft_is_in_complete_library_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Reader recovery')
    store = ReaderDraftStore(app.root)
    document = ReadingDocument('Original unchanged')
    document.replace(0, 8, 'Spoken')
    store.save(ReaderResume(document, 'flite:awb', volume=60, cursor=3))
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    recovered_root = tmp_path / 'restored'
    restore_backup(backup, recovered_root, expected_manifest_sha256=checksum)
    restored = ReaderDraftStore(recovered_root).load()
    assert restored.document.source_text == 'Original unchanged'
    assert restored.document.text == 'Spoken unchanged'
    assert restored.voice_id == 'flite:awb' and restored.volume == 60


@pytest.fixture
def bound_session(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    app = LibraryApp(tmp_path / 'bound-library')
    workspace = app.create_workspace('Bound reading drafts')
    imported = app.add_text(workspace, 'Exact source', 'First 🚂.\r\n\fSecond Ω.\r\n')
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 2)
    document = ReadingDocument(source.text)
    document.replace(0, 5, 'Edited')
    store = ReaderDraftStore(app.root, slot='explicitly-bound')
    store.save(ReaderResume(document, 'explicit:voice', cursor=6, source_locations=source.locations))
    return store, source, document


def test_v2_draft_preserves_typed_source_bindings_exact_map_and_original_bytes(bound_session):
    store, source, document = bound_session
    record = json.loads(store.path.read_bytes())
    assert record['schema'] == 'carr.reader-session.v2'
    recovered = store.load()
    assert recovered.source_locations == source.locations
    assert recovered.document.to_dict() == document.to_dict()
    assert recovered.document.source_text == 'First 🚂.\r\n\n\nSecond Ω.\r\n'
    assert recovered.document.undo() and recovered.document.text == source.text


@pytest.mark.parametrize('damage', ['empty', 'duplicate', 'reversed', 'foreign', 'boolean_index', 'v1_with_binding'])
def test_v2_source_envelope_rejects_ambiguous_or_implicit_scope(bound_session, damage):
    store, _source, _document = bound_session
    record = json.loads(store.path.read_bytes())
    if damage == 'empty':
        record['source_locations'] = []
    elif damage == 'duplicate':
        record['source_locations'][1] = record['source_locations'][0]
    elif damage == 'reversed':
        record['source_locations'].reverse()
    elif damage == 'foreign':
        record['source_locations'][1]['workspace'] = 'unrelated-workspace'
    elif damage == 'boolean_index':
        record['source_locations'][0]['index'] = True
    else:
        record['schema'] = 'carr.reader-session.v1'
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        decode_reader_resume(record)
    assert store.path.read_bytes() == before


def test_unbound_legacy_v1_remains_explicitly_unbound(tmp_path):
    store = ReaderDraftStore(tmp_path, slot='workspace/edition/looks-bound-but-is-not')
    store.save(ReaderResume(ReadingDocument('Independent text')))
    record = json.loads(store.path.read_bytes())
    assert record['schema'] == 'carr.reader-session.v1' and 'source_locations' not in record
    assert decode_reader_resume(record).source_locations == ()
