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


def test_empty_malformed_choices_can_only_be_repaired_by_explicit_save(tmp_path):
    store = DesktopPreferenceStore(tmp_path)
    store.path.write_bytes(b'')
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_bytes() == b''
    store.save(DesktopPreferences())
    assert store.load() == DesktopPreferences()


@pytest.mark.parametrize('operation', ['load', 'save'])
def test_same_path_library_replacement_refuses_old_store_without_new_files(tmp_path, operation):
    root = tmp_path / 'library'
    root.mkdir()
    store = DesktopPreferenceStore(root)
    store.load()
    store.save(DesktopPreferences(True))
    before = store.path.read_bytes()
    retained = tmp_path / 'old-library'
    root.rename(retained)
    root.mkdir()
    with pytest.raises(ValueError, match='folder changed'):
        if operation == 'load':
            store.load()
        else:
            store.save(DesktopPreferences(False))
    assert not list(root.iterdir())
    assert (retained / FILENAME).read_bytes() == before


def test_visible_save_with_failed_directory_sync_requires_real_explicit_retry(tmp_path, monkeypatch):
    from carr_library import durable_state
    from carr_library.desktop_preferences import DesktopPreferenceSaveUnconfirmed
    store = DesktopPreferenceStore(tmp_path)
    store.load()
    original = durable_state.sync_directory
    calls = []
    def failed_once(path):
        calls.append(Path(path))
        assert store.path.is_file()
        if len(calls) == 1:
            raise OSError('injected directory durability failure')
        return original(path)
    monkeypatch.setattr(durable_state, 'sync_directory', failed_once)
    choice = DesktopPreferences(True, {'voice.stop': 'Ctrl+Shift+J'})
    with pytest.raises(DesktopPreferenceSaveUnconfirmed, match='durable completion'):
        store.save(choice)
    assert DesktopPreferenceStore(tmp_path).load() == choice
    visible = store.path.read_bytes()
    assert store.save(choice) == choice
    assert len(calls) == 2 and store.path.read_bytes() == visible
    assert not list(tmp_path.glob('.pending-*'))


def test_later_competing_save_is_not_adopted_after_unconfirmed_visible_save(tmp_path, monkeypatch):
    from carr_library import durable_state
    from carr_library.desktop_preferences import DesktopPreferenceSaveUnconfirmed
    store = DesktopPreferenceStore(tmp_path)
    store.load()
    original = durable_state.sync_directory
    monkeypatch.setattr(durable_state, 'sync_directory',
                        lambda path: (_ for _ in ()).throw(OSError('directory sync')))
    with pytest.raises(DesktopPreferenceSaveUnconfirmed):
        store.save(DesktopPreferences(True))
    monkeypatch.setattr(durable_state, 'sync_directory', original)
    competitor = DesktopPreferenceStore(tmp_path)
    competitor.load()
    competitor.save(DesktopPreferences(False, {'voice.reader': 'Ctrl+Shift+K'}))
    competing_bytes = competitor.path.read_bytes()
    with pytest.raises(ValueError, match='another window'):
        store.save(DesktopPreferences(True))
    assert competitor.path.read_bytes() == competing_bytes


def test_actual_preference_read_refuses_oversized_regular_file_without_rewriting(tmp_path):
    store = DesktopPreferenceStore(tmp_path)
    before = b' ' * 8193
    store.path.write_bytes(before)
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_bytes() == before


def test_backup_validator_refuses_private_or_live_desktop_authority_fields(tmp_path):
    from carr_library.backup_sidecars import sidecar_limit, validate_sidecar
    for payload in (raw(clipboard_text='private selection'),
                    raw(native_handle=123), raw(watch_enabled=True)):
        with pytest.raises(ValueError):
            validate_sidecar(None, FILENAME, payload)
    for name in ('Desktop-preferences.json', 'desktop-preferences.json/extra',
                 '.desktop-preferences.lock'):
        with pytest.raises(ValueError):
            sidecar_limit(name)
