"""Actual library backup/restore of compact canonical reading provenance."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os

from filelock import FileLock

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.reader.legacy_reading import (
    DIRECTORY, LegacyReadingStore, authorize_snapshot, decode_snapshot,
    project_from_snapshot, snapshot_import_files,
)
from carr_library.reader.narration import (
    NarrationStore, decode_project, encode_project, revise, save_as,
)
from carr_library.reader.reading_desk import ReadingDesk


@pytest.fixture
def imported_history(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Historical reading source')
    book = app.add_text(workspace, 'Two exact segments', 'First source.\fКиїв — другий розділ.')
    desk = ReadingDesk(app, workspace, book['edition'])
    pages = [desk.page(index) for index in (1, 2)]
    source = '\n\n'.join(page.raw_text for page in pages)
    rows, cursor = [], 0
    for page in pages:
        end = cursor + len(page.raw_text)
        rows.append(dict(ordinal=page.ordinal, kind=page.location.kind,
            index=page.location.index, start=cursor, end=end,
            text_sha256=page.location.text_sha256, text_verified_at_capture=page.verified))
        cursor = end + 2
    value = dict(schema='carr.reading-copy.v2', title='Earlier full reading history',
        source_text=source, source_sha256=sha256(source.encode()).hexdigest(),
        edits=[dict(start=0, end=5, replacement='Chosen', label='Historical edit'),
               dict(start=0, end=6, replacement='First', label='Retained redo')], revision=1,
        source_anchor=dict(workspace=workspace, edition=book['edition'],
            revision=desk.revision, asset_sha256=desk.identity.asset_sha256,
            positions=rows, quotation_authority=False))
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    snapshot = decode_snapshot(raw)
    authorize_snapshot(app, workspace, snapshot)
    retained = LegacyReadingStore(app.root)
    retained.retain(raw)
    project = project_from_snapshot(snapshot, title='Imported current copy')
    store = NarrationStore(app.root)
    digest = store.save(project)
    return app, workspace, book, snapshot, project, digest


def test_historical_positions_survive_actual_backup_restore_and_rebackup(imported_history, tmp_path):
    app, workspace, book, snapshot, project, _ = imported_history
    current = app.update_metadata(workspace, book['edition'], title='Later metadata revision')
    assert current != snapshot.source_anchor.revision
    authorize_snapshot(app, workspace, snapshot)
    store = NarrationStore(app.root)
    fork = save_as(project, title='Another current copy')
    store.save(fork)
    draft = revise(project, 'An unsaved recoverable reading edit')
    store.save(draft, draft=True)
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    verify_backup(folder, digest)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    imported = snapshot_import_files(restored.root)
    name = DIRECTORY + '/' + snapshot.archive_sha256 + '.json'
    assert imported == {name: snapshot.raw}
    assert (restored.root / name).stat().st_nlink == 1
    assert NarrationStore(restored.root).load(project.id)[0] == project
    assert NarrationStore(restored.root).load(project.id, draft=True)[0] == draft
    assert NarrationStore(restored.root).load(fork.id)[0] == fork
    old = LegacyReadingStore(restored.root).load(snapshot.archive_sha256, revision=2)
    assert old.text == snapshot.source_text and old.runs[0].exact is False
    authorize_snapshot(restored, workspace, old)
    again = tmp_path / 'second-backup'
    digest2 = restored.backup(again)
    second = LibraryApp(restore_backup(again, tmp_path / 'second-restore', digest2))
    assert snapshot_import_files(second.root) == imported
    assert len(list((second.root / DIRECTORY).glob('*.json'))) == 1


def canonical_session(app, snapshot, *, slot='manual'):
    value = dict(schema='carr.reader-session.v1', document=json.loads(snapshot.raw),
        voice_id='historical-local-voice', rate=2, volume=71, cursor=4, audio_seconds=3.5)
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\r\n').encode('utf-8')
    key = sha256(slot.encode('utf-8')).hexdigest()
    path = app.root / 'state' / 'speech' / (key + '.reader.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path, raw, key


def test_unimported_canonical_reader_session_is_preserved_through_backup_restore_and_import(imported_history, tmp_path):
    from carr_library.reader.legacy_reading import snapshot_session_files, project_from_snapshot
    app, workspace, _, snapshot, _, _ = imported_history
    path, raw, _ = canonical_session(app, snapshot)
    app.update_metadata(workspace, snapshot.source_anchor.edition, title='New current metadata')
    folder = tmp_path / 'session-backup'
    digest = app.backup(folder)
    verify_backup(folder, digest)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'session-restored', digest))
    relative = path.relative_to(app.root).as_posix()
    assert snapshot_session_files(restored.root) == {relative: raw}
    assert path.read_bytes() == raw
    recovered = decode_snapshot(raw)
    authorize_snapshot(restored, workspace, recovered)
    LegacyReadingStore(restored.root).retain(raw)
    project = project_from_snapshot(recovered, title='Explicit session recovery')
    NarrationStore(restored.root).save(project)
    assert project.legacy_origin.archive_sha256 == sha256(raw).hexdigest()
    second_folder = tmp_path / 'second-session-backup'
    second_digest = restored.backup(second_folder)
    second = LibraryApp(restore_backup(second_folder, tmp_path / 'second-session-restored', second_digest))
    assert snapshot_session_files(second.root) == {relative: raw}
    assert LegacyReadingStore(second.root).load(sha256(raw).hexdigest()).session == recovered.session
    assert NarrationStore(second.root).load(project.id)[0] == project
    assert not (second.root / 'state' / 'audio' / 'engines.json').exists()


def test_active_canonical_reader_writer_prevents_complete_backup(imported_history, tmp_path):
    app, _, _, snapshot, _, _ = imported_history
    path, raw, key = canonical_session(app, snapshot)
    with FileLock(path.parent / ('.' + key + '.lock'), timeout=0):
        with pytest.raises(RuntimeError, match='draft is being saved'):
            app.backup(tmp_path / 'reader-busy')
    assert not (tmp_path / 'reader-busy').exists() and path.read_bytes() == raw


@pytest.mark.parametrize('damage', ['source-anchor', 'envelope', 'hardlink', 'unknown-file'])
def test_invalid_or_foreign_canonical_session_cannot_enter_complete_backup(imported_history, tmp_path, damage):
    app, _, _, snapshot, _, _ = imported_history
    path, raw, _ = canonical_session(app, snapshot)
    if damage in ('source-anchor', 'envelope'):
        value = json.loads(raw)
        if damage == 'source-anchor':
            value['document']['source_anchor']['asset_sha256'] = '0' * 64
        else:
            value['runtime'] = 'not-authority'
        path.write_text(json.dumps(value), encoding='utf-8')
    elif damage == 'hardlink':
        os.link(path, tmp_path / 'external-session.json')
    else:
        (path.parent / 'unregistered-runtime.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'bad-session')
    assert not (tmp_path / 'bad-session').exists()


def test_new_canonical_session_during_copy_prevents_partial_complete_snapshot(imported_history, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, _, _, snapshot, _, _ = imported_history
    path, raw, _ = canonical_session(app, snapshot)
    copy = library_backup._copy_sidecar
    changed = False
    def interleave(root, relative, target, identity, cancelled):
        nonlocal changed
        result = copy(root, relative, target, identity, cancelled)
        if root == app.root and target is not None and relative.endswith('.reader.json') and not changed:
            changed = True
            canonical_session(app, snapshot, slot='second-slot')
        return result
    monkeypatch.setattr(library_backup, '_copy_sidecar', interleave)
    with pytest.raises(ValueError, match='changed'):
        app.backup(tmp_path / 'incomplete-session-set')
    assert changed and not (tmp_path / 'incomplete-session-set').exists()
    assert path.read_bytes() == raw


def test_old_project_format_survives_beside_imported_provenance(imported_history, tmp_path):
    from carr_library.reader.narration import new_project
    app, _, _, snapshot, _, _ = imported_history
    project = new_project('Old literal format', title='Existing project')
    original = encode_project(project)
    assert json.loads(original)['version'] == 2
    NarrationStore(app.root).save(project)
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert (restored.root / 'narration-projects' / (project.id + '.json')).read_bytes() == original
    assert LegacyReadingStore(restored.root).load(snapshot.archive_sha256).raw == snapshot.raw


def test_missing_history_fails_before_backup_publication(imported_history, tmp_path):
    app, _, _, snapshot, _, _ = imported_history
    (app.root / DIRECTORY / (snapshot.archive_sha256 + '.json')).unlink()
    with pytest.raises(ValueError, match='reading state|history'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_valid_hashes_cannot_rebind_a_history_to_another_library(imported_history, tmp_path):
    app, workspace, _, snapshot, project, _ = imported_history
    foreign = app.create_workspace('Separate same-text library')
    value = json.loads(snapshot.raw)
    value['source_anchor']['workspace'] = foreign
    raw = json.dumps(value, ensure_ascii=False).encode()
    forged = decode_snapshot(raw)
    with pytest.raises(ValueError, match='different library'):
        authorize_snapshot(app, workspace, forged)
    # Retention preserves data but never supplies source authority. A backup
    # must reject the missing edition/revision binding even with fresh hashes.
    LegacyReadingStore(app.root).retain(raw)
    with pytest.raises(ValueError, match='reading state|source'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_project_origin_must_match_retained_selected_revision(imported_history, tmp_path):
    app, _, _, snapshot, project, _ = imported_history
    value = json.loads(encode_project(project))
    value['legacy_origin']['selected_sha256'] = '0' * 64
    forged = json.dumps(value, ensure_ascii=False).encode()
    assert decode_project(forged).legacy_origin.selected_sha256 == '0' * 64
    path = app.root / 'narration-projects' / (project.id + '.json')
    path.write_bytes(forged)
    with pytest.raises(ValueError, match='reading state|history'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_source_bytes_are_reauthenticated_before_historical_activation(imported_history):
    app, workspace, _, snapshot, _, _ = imported_history
    source = app.assets.resolve(snapshot.source_anchor.asset_sha256)
    source.write_bytes(b'Changed retained bytes')
    with pytest.raises(ValueError):
        authorize_snapshot(app, workspace, snapshot)


def test_archived_workspace_is_retained_but_not_silently_activated(imported_history, tmp_path):
    app, workspace, _, snapshot, project, _ = imported_history
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        authorize_snapshot(app, workspace, snapshot)
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert NarrationStore(restored.root).load(project.id)[0] == project
    with pytest.raises(ValueError, match='archived'):
        authorize_snapshot(restored, workspace, snapshot)
    restored.restore_workspace(workspace)
    authorize_snapshot(restored, workspace, snapshot)


def test_managed_audio_handoff_and_backup_preserve_the_same_compact_history(imported_history, tmp_path):
    from carr_library.ui.audio_studio import StudioController
    app, _, _, snapshot, project, saved_digest = imported_history
    studio = StudioController(private_root=app.root)
    studio.from_reading_project(project, saved_digest=saved_digest)
    studio.save_in_library()
    identity = studio.project_id
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    payload = json.loads((restored.root / 'audio-projects' / identity / 'studio' / 'project.json').read_text(
        encoding='utf-8'))
    assert payload['source_reference']['legacy_origin']['archive_sha256'] == snapshot.archive_sha256
    assert payload['source_reference']['legacy_origin']['source_anchor']['revision'] == snapshot.source_anchor.revision
    assert snapshot_import_files(restored.root) == {
        DIRECTORY + '/' + snapshot.archive_sha256 + '.json': snapshot.raw}


def test_a_forged_audio_handoff_cannot_supply_a_different_retained_history(imported_history, tmp_path):
    from carr_library.ui.audio_studio import StudioController
    app, _, _, snapshot, project, saved_digest = imported_history
    studio = StudioController(private_root=app.root)
    studio.from_reading_project(project, saved_digest=saved_digest)
    studio.save_in_library()
    path = app.root / 'audio-projects' / studio.project_id / 'studio' / 'project.json'
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['source_reference']['legacy_origin']['selected_sha256'] = '0' * 64
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='history'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
