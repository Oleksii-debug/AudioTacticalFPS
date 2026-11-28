"""Atomic valid-draft recovery, not a simulated native screen-reader test."""
import json

import pytest

from carr_library.speech import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


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


LEGACY_FIELDS = {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}


def test_legacy_seven_key_record_keeps_default_requests_and_canonical_bytes(tmp_path):
    store = ReaderDraftStore(tmp_path)
    state = ReaderResume(ReadingDocument('Immutable legacy original'), 'missing:legacy', -1, 61, 2, 3.5)
    store.save(state)
    original = store.path.read_bytes()
    assert set(json.loads(original)) == LEGACY_FIELDS
    loaded = store.load()
    assert (loaded.pitch, loaded.punctuation) == (None, False)
    assert (loaded.voice_id, loaded.rate, loaded.volume, loaded.cursor, loaded.audio_seconds) == (
        'missing:legacy', -1, 61, 2, 3.5)
    store.save(loaded)
    assert store.path.read_bytes() == original


@pytest.mark.parametrize('pitch,punctuation,extra', [
    (0, False, {'pitch'}), (99, False, {'pitch'}),
    (None, True, {'punctuation'}), (37, True, {'pitch', 'punctuation'}),
])
def test_optional_native_settings_roundtrip_without_changing_document_or_cas(tmp_path, pitch, punctuation, extra):
    from dataclasses import replace
    store = ReaderDraftStore(tmp_path)
    document = ReadingDocument('Original! Незмінний оригінал.')
    document.replace(0, 8, 'Reading')
    state = ReaderResume(document, 'unavailable:voice', 3, 62, 2, 1.25,
                         pitch=pitch, punctuation=punctuation)
    token = store.save_with_token(state, expected_token=None)
    raw = store.path.read_bytes()
    assert set(json.loads(raw)) == LEGACY_FIELDS | extra
    restored, actual_token = ReaderDraftStore(tmp_path).load_with_token()
    assert actual_token == token and (restored.pitch, restored.punctuation) == (pitch, punctuation)
    assert restored.document.to_dict() == document.to_dict()
    assert (restored.voice_id, restored.rate, restored.volume) == ('unavailable:voice', 3, 62)
    default = replace(restored, pitch=None, punctuation=False)
    next_token = store.save_with_token(default, expected_token=token)
    assert next_token != token and set(json.loads(store.path.read_bytes())) == LEGACY_FIELDS
    with pytest.raises(ValueError, match='Another window'):
        store.save_with_token(restored, expected_token=token)
    assert store.load().pitch is None and store.load().punctuation is False
    assert document.source_text == 'Original! Незмінний оригінал.'


@pytest.mark.parametrize('updates', [
    {'pitch': True}, {'pitch': -1}, {'pitch': 100}, {'pitch': 20.5}, {'pitch': '20'},
    {'punctuation': 1}, {'punctuation': None}, {'punctuation': 'false'},
    {'playback_pitch': 3}, {'activate': True},
])
def test_optional_settings_decoder_rejects_malformed_or_unknown_fields_without_rewriting(tmp_path, updates):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Retained original'), pitch=21, punctuation=True))
    before = store.path.read_bytes()
    value = json.loads(before)
    value.update(updates)
    with pytest.raises(ValueError):
        ReaderDraftStore.decode(value)
    assert store.path.read_bytes() == before


def test_explicit_optional_defaults_decode_and_resave_as_legacy_shape(tmp_path):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Legacy-compatible defaults')))
    value = json.loads(store.path.read_bytes())
    value.update(pitch=None, punctuation=False)
    restored = ReaderDraftStore.decode(value)
    store.save(restored)
    assert set(json.loads(store.path.read_bytes())) == LEGACY_FIELDS


@pytest.mark.parametrize('pauses', [(1, 0, 0), (0, 250, 0), (125, 750, 30000)])
def test_reading_pauses_roundtrip_passively_with_exact_default_bytes_and_cas(tmp_path, pauses):
    from dataclasses import replace
    names = ('sentence_pause_ms', 'paragraph_pause_ms', 'section_pause_ms')
    store = ReaderDraftStore(tmp_path)
    document = ReadingDocument('First.\n\nOriginal.\fFinal.')
    default = ReaderResume(document, 'unavailable:exact', -2, 61, 0, 3.25)
    token = store.save_with_token(default, expected_token=None)
    original = store.path.read_bytes()
    selected = replace(default, **dict(zip(names, pauses)))
    current = store.save_with_token(selected, expected_token=token)
    loaded, actual = ReaderDraftStore(tmp_path).load_with_token()
    assert actual == current != token
    assert tuple(getattr(loaded, name) for name in names) == pauses
    assert set(json.loads(store.path.read_bytes())) == LEGACY_FIELDS | {
        name for name, value in zip(names, pauses) if value}
    assert loaded.document.to_dict() == document.to_dict()
    assert loaded.voice_id == 'unavailable:exact' and loaded.audio_seconds == 3.25
    with pytest.raises(ValueError, match='Another window'):
        store.save_with_token(default, expected_token=token)
    store.save_with_token(replace(loaded, **dict.fromkeys(names, 0)), expected_token=current)
    assert store.path.read_bytes() == original


@pytest.mark.parametrize('updates', [
    {'sentence_pause_ms': True}, {'sentence_pause_ms': -1}, {'paragraph_pause_ms': 30001},
    {'paragraph_pause_ms': 1.5}, {'section_pause_ms': '500'}, {'section_pause_ms': None},
    {'section_pause_seconds': 1}, {'sentence_pause_ms': 0, 'autoplay': True},
])
def test_malformed_pause_recovery_cannot_rewrite_saved_reader(tmp_path, updates):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Exact original.'), sentence_pause_ms=125))
    original = store.path.read_bytes()
    candidate = json.loads(original)
    candidate.update(updates)
    with pytest.raises(ValueError):
        ReaderDraftStore.decode(candidate)
    assert store.path.read_bytes() == original


def test_explicit_zero_pause_record_resaves_as_exact_legacy_bytes(tmp_path):
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument('Legacy original.'), 'missing:voice'))
    original = store.path.read_bytes()
    candidate = json.loads(original)
    candidate.update(sentence_pause_ms=0, paragraph_pause_ms=0, section_pause_ms=0)
    recovered = ReaderDraftStore.decode(candidate)
    store.save(recovered)
    assert store.path.read_bytes() == original
