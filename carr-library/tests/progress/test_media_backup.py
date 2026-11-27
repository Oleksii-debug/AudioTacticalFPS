"""Source-bound listening positions and inert, disabled restored voice settings."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.live_state import FILENAME as LIVE_STATE, LiveState, encode_live_state, validate_live_state
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.dordiai_settings import (
    FILENAME as VOICE_SETTINGS, default_settings, load_settings, save_settings,
)


def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact spoken source')
    text = 'Є український оригінал.\r\nSlovenský text. 😀 Last line.'
    imported = app.add_text(workspace, 'Source', text)
    page = ReadingDesk(app, workspace, imported['edition']).page(1)
    assert page.raw_text == text
    return app, workspace, imported, page


def remember(app, page):
    return LiveState(app.root).remember(page.location, 3, len(page.raw_text) - 1,
        len(page.raw_text), 'fixture:explicit-local-voice', rate=.8, volume=.4)


def reseal(folder, name, raw):
    (folder / name).write_bytes(raw)
    manifest_path = folder / 'manifest.json'
    value = json.loads(manifest_path.read_bytes())
    item = next(row for row in value['sidecars'] if row['filename'] == name)
    item.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = json.dumps(value).encode('utf-8')
    manifest_path.write_bytes(encoded)
    return sha256(encoded).hexdigest()


@pytest.mark.parametrize('scoped', (False, True))
def test_exact_live_position_roundtrip_including_library_only_scope(tmp_path, scoped):
    app, workspace, imported, page = source(tmp_path)
    saved = remember(app, page)
    raw = (app.root / LIVE_STATE).read_bytes()
    original = app.assets.resolve(imported['asset']).read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder, authorized_workspaces={workspace} if scoped else None)
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert (restored / LIVE_STATE).read_bytes() == raw
    assert LiveState(restored).resume(workspace, imported['edition']) == saved
    assert saved.offset == 3 and saved.text_length == len(page.raw_text)
    assert saved.text_length != len(page.raw_text.encode('utf-8'))
    assert app.assets.resolve(imported['asset']).read_bytes() == original


@pytest.mark.parametrize('damage', ('length', 'location', 'offset'))
def test_resealed_forged_live_position_fails_before_publication(tmp_path, damage):
    app, _, _, page = source(tmp_path)
    saved = remember(app, page)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    if damage == 'length':
        raw = encode_live_state((replace(saved, text_length=saved.text_length + 1),))
    elif damage == 'location':
        raw = encode_live_state((replace(saved, location=replace(saved.location, text_sha256='0' * 64)),))
    else:
        value = json.loads((folder / LIVE_STATE).read_bytes())
        value['positions'][0]['offset'] = saved.text_length + 1
        raw = json.dumps(value).encode('utf-8')
    digest = reseal(folder, LIVE_STATE, raw)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


def test_voice_declaration_is_preserved_but_disabled_without_runtime_probe(tmp_path, monkeypatch):
    app, _, _, _ = source(tmp_path)
    runtime = tmp_path / 'private-runtime-that-is-not-installed'
    settings = {**default_settings(), 'enabled': True, 'runtime_directory': str(runtime),
                'python_executable': str(runtime / 'python.exe'), 'expected_worker_sha256': 'a' * 64}
    save_settings(app.root, settings)
    original = (app.root / VOICE_SETTINGS).read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('Backup or restore tried to execute a private voice runtime')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    actual_stat, actual_lstat = Path.stat, Path.lstat
    def stat(path, *args, **kwargs):
        assert not path.is_relative_to(runtime), 'Backup probed a private runtime declaration'
        return actual_stat(path, *args, **kwargs)
    def lstat(path, *args, **kwargs):
        assert not path.is_relative_to(runtime), 'Backup probed a private runtime declaration'
        return actual_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(Path, 'lstat', lstat)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    recovered = load_settings(restored)
    assert recovered == {**settings, 'enabled': False}
    assert (folder / VOICE_SETTINGS).read_bytes() == original == (app.root / VOICE_SETTINGS).read_bytes()


def test_scoped_backup_omits_independent_voice_configuration(tmp_path):
    app, workspace, _, page = source(tmp_path)
    remember(app, page)
    save_settings(app.root, {**default_settings(), 'enabled': True,
                            'runtime_directory': str(tmp_path / 'private-runtime')})
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder, authorized_workspaces={workspace})
    assert not (folder / VOICE_SETTINGS).exists()
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert not (restored / VOICE_SETTINGS).exists()
    assert validate_live_state((restored / LIVE_STATE).read_bytes())


def test_resealed_malformed_voice_declaration_fails_before_destination(tmp_path):
    app, _, _, _ = source(tmp_path)
    save_settings(app.root, default_settings())
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    raw = json.dumps({**default_settings(), 'enabled': True, 'runtime_directory': '//untrusted/share'}).encode()
    digest = reseal(folder, VOICE_SETTINGS, raw)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, digest)
    assert not destination.exists()
