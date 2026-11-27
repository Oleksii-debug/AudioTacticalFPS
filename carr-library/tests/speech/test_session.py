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


def test_canonical_anchor_is_the_only_saved_source_lineage(bound_session):
    store, source, _legacy = bound_session
    document = source.reading_document()
    document.replace(0, 5, 'Edited')
    resume = ReaderResume(document, cursor=6)
    assert resume.source_locations == source.locations
    store.save(resume)
    record = json.loads(store.path.read_bytes())
    assert record['schema'] == 'carr.reader-session.v1'
    assert record['document']['schema'] == 'carr.reading-copy.v2'
    assert 'source_locations' not in record
    recovered = store.load()
    assert recovered.source_locations == source.locations
    assert recovered.document.to_dict() == document.to_dict()
    assert recovered.document.undo() and recovered.document.text == source.text


def test_interim_envelope_cannot_override_canonical_anchor(bound_session):
    from dataclasses import asdict
    store, source, _legacy = bound_session
    store.save(ReaderResume(source.reading_document()))
    record = json.loads(store.path.read_bytes())
    record.update(schema='carr.reader-session.v2',
                  source_locations=[asdict(item) for item in source.locations])
    assert decode_reader_resume(record).source_locations == source.locations
    for location in record['source_locations']:
        location['workspace'] = 'unrelated-workspace'
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='canonical reading-copy lineage'):
        decode_reader_resume(record)
    assert store.path.read_bytes() == before


def test_seed_draft_keeps_existing_authored_copy_and_candidate_identity(tmp_path):
    store = ReaderDraftStore(tmp_path, slot='one-transcript')
    candidate = ReaderResume(ReadingDocument('First transcript'))
    assert store.save_if_absent(candidate) is candidate
    edited = store.load()
    edited.document.replace(0, 5, 'Authored')
    store.save(edited)
    before = store.path.read_bytes()
    other = ReaderResume(ReadingDocument('A different transcription attempt'))
    recovered = ReaderDraftStore(tmp_path, slot='one-transcript').save_if_absent(other)
    assert recovered is not other
    assert recovered.document.text == 'Authored transcript'
    assert store.path.read_bytes() == before


def test_seed_draft_preserves_malformed_existing_state(tmp_path):
    store = ReaderDraftStore(tmp_path)
    store.path.write_bytes(b'{invalid state kept for diagnosis')
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save_if_absent(ReaderResume(ReadingDocument('New transcript')))
    assert store.path.read_bytes() == before


def test_bound_reading_draft_retains_exact_lineage_through_complete_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bound reading recovery')
    original = 'First original.\r\nОригінал.\fSecond original.'
    imported = app.add_text(workspace, 'Source', original)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 2)
    document = source.reading_document()
    document.replace(0, 5, 'Narrated')
    store = ReaderDraftStore(app.root, slot='source-fixture')
    store.save(ReaderResume(document, 'fixture:voice', volume=60, cursor=3))
    checksum = app.backup(tmp_path / 'backup')
    restored_root = tmp_path / 'restored'
    restore_backup(tmp_path / 'backup', restored_root, expected_manifest_sha256=checksum)
    restored_app = LibraryApp(restored_root)
    recovered = ReaderDraftStore(restored_root, slot='source-fixture').load()
    current = read_source_range(ReadingDesk(restored_app, workspace, imported['edition']), 1, 2)
    assert recovered.document.source_anchor == document.source_anchor == current.reading_document().source_anchor
    assert recovered.document.source_text == source.text
    assert recovered.document.text == document.text and recovered.document.can_undo
    assert restored_app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    first, second = recovered.document.source_anchor['positions']
    # Undo restores exact source text but never turns the inserted join into evidence.
    assert recovered.document.undo()
    assert not recovered.document.source_span(first['end'], second['start']).exact
    assert recovered.document.source_anchor['quotation_authority'] is False
