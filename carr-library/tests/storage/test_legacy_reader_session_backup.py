"""Historical reader sessions retain exact envelopes through full current backup."""
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.reader import legacy_reading as legacy
from carr_library.reader.narration import NarrationStore, encode_project, revise
from tests.storage.test_legacy_reading_backup import imported_history


def retain_session(app, document, *, revision=1):
    value = dict(schema='carr.reader-session.v1', document=document,
                 voice_id='sapi5-32:inert-old-token', rate=-2, volume=61,
                 cursor=3, audio_seconds=16.5)
    data = (json.dumps(value, ensure_ascii=False, indent=3) + '\n').encode()
    snapshot = legacy.decode_snapshot(data, revision=revision)
    legacy.LegacyReadingStore(app.root).retain(data)
    project = legacy.project_from_snapshot(snapshot, title='Recovered session revision')
    store = NarrationStore(app.root)
    digest = store.save(project)
    return snapshot, project, digest


@pytest.mark.parametrize('revision', [0, 1, 2])
def test_actual_backup_restore_rebackup_retains_envelope_and_exact_source(imported_history, tmp_path, monkeypatch, revision):
    app, workspace, book, original, _, _ = imported_history
    snapshot, project, digest = retain_session(app, json.loads(original.raw), revision=revision)
    legacy.authorize_snapshot(app, workspace, snapshot)
    draft = revise(project, 'Pending current derivative')
    NarrationStore(app.root).save(draft, draft=True)
    monkeypatch.setattr('carr_library.audio.engines.default_registry',
                        lambda **kwargs: pytest.fail('Restoring history activated an old voice.'))
    before = legacy.snapshot_import_files(app.root)
    backup = tmp_path / 'session-backup'
    expected = app.backup(backup)
    verify_backup(backup, expected)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', expected))
    assert legacy.snapshot_import_files(restored.root) == before
    reopened = legacy.LegacyReadingStore(restored.root).load(snapshot.archive_sha256, revision=revision)
    assert reopened == snapshot and asdict(reopened.session) == asdict(snapshot.session)
    assert NarrationStore(restored.root).load(project.id) == (project, digest)
    assert NarrationStore(restored.root).load(project.id, draft=True)[0] == draft
    legacy.authorize_snapshot(restored, workspace, reopened)
    assert not (restored.root / 'state' / 'reading-speech.json').exists()
    second = tmp_path / 'rebackup'
    again = restored.backup(second)
    verify_backup(second, again)
    reread = LibraryApp(restore_backup(second, tmp_path / 'again', again))
    assert legacy.snapshot_import_files(reread.root) == before
    assert legacy.LegacyReadingStore(reread.root).load(snapshot.archive_sha256).raw == snapshot.raw


def test_inner_document_digest_cannot_replace_original_envelope_identity(imported_history, tmp_path):
    app, _, _, original, _, _ = imported_history
    snapshot, project, _ = retain_session(app, json.loads(original.raw))
    path = app.root / legacy.DIRECTORY / (snapshot.archive_sha256 + '.json')
    # Keeping the envelope's filename while substituting the otherwise valid
    # inner history must not discard saved session observations silently.
    path.write_bytes(original.raw)
    with pytest.raises(ValueError, match='reading state|history|filename'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_valid_envelope_hash_does_not_bypass_current_source_binding(imported_history, tmp_path):
    app, workspace, _, original, _, _ = imported_history
    document = json.loads(original.raw)
    document['source_anchor']['workspace'] = app.create_workspace('Different same-text workspace')
    snapshot, _, _ = retain_session(app, document)
    with pytest.raises(ValueError, match='different library'):
        legacy.authorize_snapshot(app, workspace, snapshot)
    with pytest.raises(ValueError, match='reading state|source'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_envelope_remains_required_when_a_current_pending_project_uses_it(imported_history, tmp_path):
    app, _, _, original, _, _ = imported_history
    snapshot, project, _ = retain_session(app, json.loads(original.raw))
    NarrationStore(app.root).save(revise(project, 'Unsaved current edit'), draft=True)
    (app.root / legacy.DIRECTORY / (snapshot.archive_sha256 + '.json')).unlink()
    with pytest.raises(ValueError, match='reading state|history'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
