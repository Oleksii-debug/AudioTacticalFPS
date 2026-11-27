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
