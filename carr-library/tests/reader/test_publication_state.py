"""Typed external-publication cursors and complete-library backup boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.media_overlay import PreparedOverlay
from carr_library.reader.publication_state import (
    FILENAME, PublicationPositions, decode_positions,
)


def document(tmp_path):
    # State-only fixture. No native playback/format qualification is inferred.
    source = b'exact synthetic external publication bytes'
    return PreparedOverlay('State fixture', 'epub', sha256(source).hexdigest(), {},
        ({'id': 'chapter', 'title': 'Chapter', 'first_interval': 0},),
        ({'id': 'line', 'chapter': 'chapter', 'start': 0.0, 'text': 'Exact text.'},),
        tmp_path / 'not-retained-audio.wav', (), '0' * 64, 10.0, 8000, 80000, None)


def test_exact_publication_cursor_roundtrips_without_path_text_or_audio(tmp_path):
    selected = document(tmp_path)
    store = PublicationPositions(tmp_path)
    assert store.load(selected) is None
    store.remember(selected, 2.5)
    raw = store.path.read_bytes()
    assert store.load(selected) == 2.5
    assert str(tmp_path).encode() not in raw and b'Exact text.' not in raw
    assert set(decode_positions(raw)) == {selected.publication_sha256}
    assert store.load(replace(selected, publication_sha256='1' * 64)) is None
    assert store.load(replace(selected, kind='daisy')) is None
    assert store.load(replace(selected, duration=11.0)) is None


@pytest.mark.parametrize('field,value', [
    ('publication_sha256', '../wrong'), ('publication_sha256', True),
    ('kind', 'html'), ('duration', False), ('duration', 0), ('duration', float('inf')),
    ('position', True), ('position', -1), ('position', 10.1),
    ('position', float('nan')), ('updated_at', 'tomorrow'),
])
def test_invalid_position_fields_never_replace_valid_state(tmp_path, field, value):
    selected = document(tmp_path)
    store = PublicationPositions(tmp_path)
    store.remember(selected, 1.5)
    before = store.path.read_bytes()
    envelope = json.loads(before)
    envelope['positions'][0][field] = value
    with pytest.raises((ValueError, TypeError)):
        decode_positions(json.dumps(envelope).encode())
    assert store.path.read_bytes() == before and store.load(selected) == 1.5


def test_duplicate_identity_and_ambiguous_json_are_rejected(tmp_path):
    selected = document(tmp_path)
    store = PublicationPositions(tmp_path)
    store.remember(selected, 1.0)
    envelope = json.loads(store.path.read_bytes())
    envelope['positions'].append(dict(envelope['positions'][0]))
    with pytest.raises(ValueError):
        decode_positions(json.dumps(envelope).encode())
    store.path.write_text('{"schema":"carr-publication-reading/1","schema":"carr-publication-reading/1","positions":[]}')
    with pytest.raises(ValueError):
        store.load(selected)
    with pytest.raises(ValueError):
        store.remember(selected, 2)
    assert store.path.read_text().count('"schema"') == 2


@pytest.mark.parametrize('failure', [PermissionError, NotADirectoryError])
def test_unreadable_optional_cursor_is_never_treated_as_empty_or_overwritten(tmp_path, monkeypatch, failure):
    selected = document(tmp_path)
    store = PublicationPositions(tmp_path)
    store.remember(selected, 3)
    before = store.path.read_bytes()
    real = Path.lstat
    def refused(path, *args, **kwargs):
        if path == store.path:
            raise failure('Cannot inspect selected reading state.')
        return real(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'lstat', refused)
        with pytest.raises(RestoreError) as loaded:
            store.load(selected)
        assert type(loaded.value.__cause__) is failure
        with pytest.raises(RestoreError) as saved:
            store.remember(selected, 4)
        assert type(saved.value.__cause__) is failure
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('reject_at', [1, 2])
def test_revoked_save_guard_preserves_existing_exact_cursor(tmp_path, reject_at):
    selected = document(tmp_path)
    store = PublicationPositions(tmp_path)
    store.remember(selected, 1)
    before, calls = store.path.read_bytes(), []
    def guard():
        calls.append(True)
        if len(calls) == reject_at:
            raise PermissionError('The reader library was revoked.')
    with pytest.raises(PermissionError):
        store.remember(selected, 8, guard=guard)
    assert store.path.read_bytes() == before and store.load(selected) == 1


def test_complete_backup_restores_external_publication_positions_without_reopening_audio(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recorded reading')
    app.add_text(workspace, 'Unchanged original', 'Original source.')
    selected = document(tmp_path)
    store = PublicationPositions(app.root)
    store.remember(selected, 7.25)
    before = store.path.read_bytes()
    backup = tmp_path / 'snapshot'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    target = PublicationPositions(restored.root)
    assert target.path.read_bytes() == before and target.path.stat().st_nlink == 1
    assert target.load(selected) == 7.25
    manifest = json.loads((backup / 'manifest.json').read_text())
    assert FILENAME in (backup / 'manifest.json').read_text()
    assert b'not-retained-audio.wav' not in (backup / 'manifest.json').read_bytes()
    assert not selected.audio.exists()


def test_complete_backup_refuses_invalid_cursor_before_completion_marker(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Recorded reading')
    store = PublicationPositions(app.root)
    store.remember(document(tmp_path), 2)
    value = json.loads(store.path.read_bytes())
    value['positions'][0]['position'] = 20
    store.path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected' / 'manifest.json').exists()
