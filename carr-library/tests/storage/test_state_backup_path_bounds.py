"""Reject oversized paths before portable ancestor expansion or publication."""
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library import state_backup


def entry(name):
    return {'filename': name, 'size_bytes': 1, 'sha256': sha256(b'x').hexdigest()}


@pytest.mark.parametrize('name', [
    'settings.json',
    'speech/documents/читання.json',
    'transcription/jobs/' + 'a' * 32 + '/inputs/selected.wav',
    '/'.join(['part'] * 64),
    '/'.join(['a' * 63] * 63 + ['b' * 64]),
])
def test_normal_paths_and_inclusive_length_depth_boundaries(name):
    assert len(name) <= 4096 and name.count('/') < 64
    assert state_backup.relative_name(name).as_posix() == name
    state_backup.validate_state([entry(name)])


@pytest.mark.parametrize('name,message', [
    ('a' * 4097, '4096-character'),
    ('/'.join(['a'] * 65), '64-component'),
    ('/'.join(['a'] * 10000), '4096-character'),
    ('a' * 20000, '4096-character'),
])
def test_oversized_paths_are_rejected_before_path_or_ancestor_construction(monkeypatch, name, message):
    def forbidden(*args, **kwargs):
        pytest.fail('An oversized manifest path reached PurePosixPath.')

    monkeypatch.setattr(state_backup, 'PurePosixPath', forbidden)
    with pytest.raises(ValueError, match=message):
        state_backup.validate_state([entry(name)])


def test_full_backup_refuses_overdeep_state_without_changing_originals(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Source preservation')
    app.add_text(workspace, 'Original', 'Точний збережений текст.')
    originals = {path.name: path.read_bytes() for path in app.assets_dir.iterdir()}
    source = app.root / 'state'
    for _ in range(64):
        source /= 'd'
    source /= 'v.json'
    source.parent.mkdir(parents=True)
    raw = b'{"original":true}\n'
    source.write_bytes(raw)
    destination = tmp_path / 'backup'

    with pytest.raises(ValueError, match='64-component'):
        create_backup(app, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob('.carr-library-backup-*'))
    assert source.read_bytes() == raw
    assert {path.name: path.read_bytes() for path in app.assets_dir.iterdir()} == originals
    assert app.browse(workspace)[0].title == 'Original'


def test_full_backup_roundtrip_keeps_normal_unicode_state(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Normal state')
    app.add_text(workspace, 'Original', 'Точний збережений текст.')
    relative = 'speech/documents/читання.json'
    source = app.root / 'state' / relative
    source.parent.mkdir(parents=True)
    raw = '{"text":"Точний текст"}\n'.encode('utf-8')
    source.write_bytes(raw)
    backup = tmp_path / 'backup'

    identity = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', identity)

    assert source.read_bytes() == raw
    assert (restored / 'state' / relative).read_bytes() == raw
    assert LibraryApp(restored).browse(workspace)[0].title == 'Original'
