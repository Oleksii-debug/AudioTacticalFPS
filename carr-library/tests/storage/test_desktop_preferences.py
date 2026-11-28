"""Passive choices, exact CAS, real backup/restore; no desktop authority in state."""
import json
from pathlib import Path
import os

import pytest

from carr_library.application import LibraryApp
from carr_library.desktop_preferences import DesktopPreferences, DesktopPreferenceStore, FILENAME, validate_desktop_preferences
from carr_library.library_backup import restore_backup


def raw(**updates):
    value = {'version': 1, 'tray_icon': True, 'global_shortcuts': {'voice.clipboard': 'Ctrl+Shift+K'}}
    value.update(updates)
    return json.dumps(value).encode('utf-8')


def test_missing_profile_is_inert_and_saved_choices_have_no_activation_fields(tmp_path):
    store = DesktopPreferenceStore(tmp_path)
    assert store.load() == DesktopPreferences()
    assert not store.path.exists()
    choice = DesktopPreferences(True, {'voice.clipboard': 'ctrl+shift+k'})
    store.save(choice)
    assert DesktopPreferenceStore(tmp_path).load() == choice
    assert set(json.loads(store.path.read_bytes())) == {'version', 'tray_icon', 'global_shortcuts'}
    with pytest.raises(TypeError):
        choice.global_shortcuts['voice.stop'] = 'Ctrl+Shift+J'


@pytest.mark.parametrize('payload', [
    raw(version=True), raw(tray_icon=1), raw(global_shortcuts=[]), raw(clipboard_watch=True),
    raw(global_enabled=True), raw(global_shortcuts={'voice.stop': 'Ctrl+Shift+F12'}),
    raw(global_shortcuts={'voice.stop': 'Ctrl+Shift+K', 'voice.reader': 'Ctrl+Shift+K'}),
    b'{"version":1,"version":1,"tray_icon":false,"global_shortcuts":{}}', b' ' * 8193,
])
def test_invalid_or_authorizing_preference_fields_are_refused(payload):
    with pytest.raises(ValueError):
        validate_desktop_preferences(payload)


def test_unknown_future_commands_are_valid_inert_data_not_runtime_admission():
    value = validate_desktop_preferences(raw(global_shortcuts={'future.command': 'Ctrl+Shift+F9'}))
    assert value.global_shortcuts == {'future.command': 'Ctrl+Shift+F9'}


def test_actual_compare_and_swap_does_not_overwrite_another_window(tmp_path):
    a, b = DesktopPreferenceStore(tmp_path), DesktopPreferenceStore(tmp_path)
    a.load(); b.load()
    a.save(DesktopPreferences(True))
    before = a.path.read_bytes()
    with pytest.raises(ValueError, match='another window'):
        b.save(DesktopPreferences(False))
    assert a.path.read_bytes() == before
    b.load(); b.save(DesktopPreferences(False))
    assert a.load() == DesktopPreferences(False)


def test_explicit_malformed_profile_repair_preserves_changed_bytes_and_refuses_hardlinks(tmp_path):
    store = DesktopPreferenceStore(tmp_path)
    store.path.write_bytes(b'broken')
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_bytes() == b'broken'
    store.save(DesktopPreferences())
    linked = tmp_path / 'linked'
    os.link(store.path, linked)
    before = linked.read_bytes()
    with pytest.raises(ValueError):
        store.load()
    assert linked.read_bytes() == before


def test_actual_backup_restore_preserves_only_passive_choices(tmp_path):
    app = LibraryApp(tmp_path / 'original')
    app.create_workspace('Desktop choices')
    store = DesktopPreferenceStore(app.root)
    store.load()
    saved = DesktopPreferences(True, {'voice.clipboard': 'Ctrl+Shift+K'})
    store.save(saved)
    before = store.path.read_bytes()
    backup = tmp_path / 'backup'
    receipt = app.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', receipt))
    assert (restored.root / FILENAME).read_bytes() == before
    assert DesktopPreferenceStore(restored.root).load() == saved
    assert not (restored.root / '.desktop-preferences.lock').exists()
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert any(row['filename'] == FILENAME for row in manifest['sidecars'])
    assert not any('clipboard' in row['filename'] for row in manifest['sidecars'])


def test_invalid_desktop_sidecar_cannot_complete_a_backup(tmp_path):
    app = LibraryApp(tmp_path / 'original')
    app.create_workspace('Invalid state')
    (app.root / FILENAME).write_bytes(raw(clipboard_watch=True))
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'backup')
    assert not (tmp_path / 'backup' / 'manifest.json').exists()
