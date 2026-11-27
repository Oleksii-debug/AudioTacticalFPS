"""Complete backup includes retained PCM and passive queue/cursor semantics."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import playlist as module
from carr_library.audio.playlist import PlaylistStore, SavedPlaylist, snapshot_guard
from carr_library.audio.playlist_session import PlaylistSession
from carr_library.durable_state import encode
from carr_library import library_backup as backup
from carr_library.library_backup import create_backup, verify_backup, restore_backup, RestoreError
from tests.audio.playlist_fixture import Player, retained


@pytest.mark.parametrize('kind', ['empty', 'saved', 'unreferenced'])
def test_backup_restore_rebackup_preserves_bytes_and_never_autoplays(tmp_path, kind):
    app, sources, rows, value, receipt = retained(tmp_path,
        count=0 if kind == 'empty' else 2, save=kind != 'unreferenced')
    if kind == 'saved':
        value = replace(value, index=1, frame=2468, accuracy='completed_buffers')
        receipt = PlaylistStore(app.root).save(value, expected_token=receipt.token)
    exact = {row.path.name: row.path.read_bytes() for row in rows}
    for path in sources:
        path.unlink()
    destination, snapshot = tmp_path / 'restored', tmp_path / 'snapshot'
    proof = create_backup(app, snapshot)
    verify_backup(snapshot, proof)
    restore_backup(snapshot, destination, proof)
    reopened = PlaylistStore(destination)
    assert reopened.load() == ((None, None) if kind == 'unreferenced' else (value, receipt.token))
    for name, data in exact.items():
        assert (destination / module.DIRECTORY / 'media' / name).read_bytes() == data
    player = Player()
    session = PlaylistSession(destination, player)
    try:
        assert not player.calls and session.admission is None
        if kind == 'saved':
            session.adopt(session.review())
            assert session.value.frame == 2468 and session.value.index == 1
            assert not any(call[0] == 'play' for call in player.calls)
    finally:
        session.close()
    second = tmp_path / 'rebackup'
    again = create_backup(LibraryApp(destination), second)
    verify_backup(second, again)


def test_active_namespace_refuses_backup_until_the_same_writer_is_idle(tmp_path):
    app, *_ = retained(tmp_path, count=1)
    with snapshot_guard(app.root):
        with pytest.raises((ValueError, RuntimeError, RestoreError)):
            create_backup(app, tmp_path / 'busy')
    assert not (tmp_path / 'busy').exists()
    proof = create_backup(app, tmp_path / 'idle')
    verify_backup(tmp_path / 'idle', proof)


@pytest.mark.parametrize('damage', ['unknown', 'pcm-changed', 'pcm-missing', 'queue-cross-namespace'])
def test_invalid_owned_state_is_not_silently_excluded_from_complete_backup(tmp_path, damage):
    app, _, rows, value, receipt = retained(tmp_path, count=1)
    if damage == 'unknown':
        (app.root / module.DIRECTORY / 'unknown.bin').write_bytes(b'Keep unknown user bytes')
    elif damage == 'pcm-changed':
        raw = bytearray(rows[0].path.read_bytes())
        raw[-1] ^= 1
        rows[0].path.write_bytes(raw)
    elif damage == 'pcm-missing':
        rows[0].path.unlink()
    else:
        forged = value.to_dict()
        forged['items'][0]['filename'] = 'audio-projects/' + '0' * 32 + '/audio/assets/other.wav'
        PlaylistStore(app.root).path.write_bytes(encode(forged))
    with pytest.raises((ValueError, RuntimeError, OSError, RestoreError)):
        create_backup(app, tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


def test_manifest_rehash_does_not_authorize_changed_retained_pcm(tmp_path):
    app, _, rows, _, _ = retained(tmp_path, count=1)
    snapshot = tmp_path / 'snapshot'
    create_backup(app, snapshot)
    manifest_path = snapshot / backup.MANIFEST
    manifest = json.loads(manifest_path.read_bytes())
    relative = rows[0].path.relative_to(app.root).as_posix()
    selected = next(row for row in manifest['sidecars'] if row['path'] == relative)
    target = snapshot / backup.SIDECARS / relative
    raw = bytearray(target.read_bytes())
    raw[-1] ^= 1
    target.write_bytes(raw)
    selected['sha256'] = sha256(raw).hexdigest()
    raw_manifest = encode(manifest)
    manifest_path.write_bytes(raw_manifest)
    with pytest.raises((ValueError, RuntimeError, RestoreError)):
        verify_backup(snapshot, sha256(raw_manifest).hexdigest())


def test_completed_copy_cannot_hide_later_source_media_changes(tmp_path, monkeypatch):
    app, _, rows, _, _ = retained(tmp_path, count=1)
    original = backup._copy_sidecar
    changed = False
    def change_after_copy(root, relative, target, identity, cancelled):
        nonlocal changed
        result = original(root, relative, target, identity, cancelled)
        if not changed and relative == rows[0].path.relative_to(app.root).as_posix():
            changed = True
            raw = bytearray(rows[0].path.read_bytes())
            raw[-1] ^= 1
            rows[0].path.write_bytes(raw)
        return result
    monkeypatch.setattr(backup, '_copy_sidecar', change_after_copy)
    with pytest.raises((ValueError, RuntimeError, RestoreError)):
        create_backup(app, tmp_path / 'changed')
    assert changed and not (tmp_path / 'changed').exists()


def test_private_interrupted_stages_are_not_restored_as_completed_media(tmp_path):
    app, _, rows, value, _ = retained(tmp_path, count=1)
    stage = rows[0].path.parent / ('.capture-' + 'a' * 32)
    stage.write_bytes(b'incomplete private copy')
    snapshot, restored = tmp_path / 'snapshot', tmp_path / 'restored'
    proof = create_backup(app, snapshot)
    restore_backup(snapshot, restored, proof)
    assert stage.exists()
    assert not tuple((restored / module.DIRECTORY).rglob('.capture-*'))
    assert module.validate_state_tree(restored) == value


def test_unreadable_optional_namespace_refuses_backup_instead_of_dropping_queue(tmp_path, monkeypatch):
    app, *_ = retained(tmp_path, count=1)
    original = module.optional_present
    def denied(path):
        if Path(path) == app.root / module.DIRECTORY:
            raise PermissionError('Retained queue access denied.')
        return original(path)
    monkeypatch.setattr(module, 'optional_present', denied)
    with pytest.raises((PermissionError, RestoreError)):
        create_backup(app, tmp_path / 'denied')
    assert not (tmp_path / 'denied').exists()
