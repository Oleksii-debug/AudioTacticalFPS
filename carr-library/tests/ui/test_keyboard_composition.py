"""Composed Main/Reader commands and explicit legacy-profile transfer.

Real profile files and production installers, with native menu doubles. Native
OS dispatch remains a separate installed-Windows gate.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from carr_library.keybindings import encode_preferences
from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry
from carr_library.ui.keyboard_settings import KeyboardManager, import_keymap, install_keyboard_settings
from carr_library.ui.productivity import install_productivity_actions
from test_keyboard_manager import DynamicMenuBar, Menu


class ComposedMenu(Menu):
    def AppendSeparator(self):
        pass


class ComposedBar(DynamicMenuBar):
    def FindMenu(self, title):
        return next((i for i, (label, _, _) in enumerate(self.entries)
                     if label.replace('&', '') == title), -1)

    def Insert(self, index, menu, label):
        self.entries.insert(index, (label, menu, True))


@pytest.fixture
def composed(tmp_path, monkeypatch):
    # These captions come from the composed product menus, not the registry
    # table: a missing or stale stable-ID adapter must fail this integration.
    captions = {
        '&Library': ['New library\tCtrl+N', 'Import books\tCtrl+I',
                     'Portable snapshots and transfer…', 'Exit\tAlt+F4'],
        '&Research': ['Build advanced query\tCtrl+Alt+Q', 'Research Search Center\tCtrl+Alt+F'],
        '&Academic': ['Academic library...\tCtrl+Shift+B',
                      'Compare sources in active topic\tCtrl+Alt+M',
                      'DOI lookup and open-access sources...'],
        '&Reading': ['Read selected book\tCtrl+R'],
        'A&udio': ['Open Audio Studio...\tCtrl+Alt+A', 'Prepare selected book for narration...'],
        'Se&ttings': ['AI and API &connections...'],
    }
    entries = []
    for title, labels in captions.items():
        menu = ComposedMenu()
        for label in labels:
            menu.Append(-1, label)
        entries.append((title, menu, True))
    bar = ComposedBar(entries)
    handlers = {}
    wx = SimpleNamespace(Menu=ComposedMenu, ID_ANY=-1, EVT_MENU=object(),
                         CallAfter=lambda callback, *args: callback(*args))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple', lambda *args: args)
    frame = SimpleNamespace(menu_bar=bar, library=SimpleNamespace(root=tmp_path),
                            report=lambda message: None, IsEnabled=lambda: True)
    frame.GetMenuBar = lambda: bar
    frame.Bind = lambda event, callback, item: handlers.update({item.GetId(): callback})
    install_productivity_actions(frame)
    manager = install_keyboard_settings(frame)
    return frame, manager, handlers


def test_one_registry_keeps_composed_native_menu_actions(composed):
    frame, manager, _ = composed
    assert not hasattr(frame, 'keyboard_actions')
    assert frame.menu_bar.FindMenu('File') == 0
    assert sum(label.replace('&', '') == 'Options' for label, _, _ in frame.menu_bar.entries) == 1
    assert install_productivity_actions(frame) is frame.narration_actions
    assert install_keyboard_settings(frame) is manager
    expected = {
        'file.save': 'Ctrl+S', 'file.save_all': 'Ctrl+Shift+S',
        'file.new_reading_copy': '', 'file.import_reading_copy': '', 'file.open_reading_copy': '',
        'reading.new_copy': '', 'reading.open_copy': '', 'reading.open': 'Ctrl+R',
        'research.advanced_query': 'Ctrl+Alt+Q', 'research.search_center': 'Ctrl+Alt+F',
        'academic.open': 'Ctrl+Shift+B', 'academic.compare_sources': 'Ctrl+Alt+M',
        'academic.doi_lookup': '', 'library.portable_sync': '',
        'audio.open': 'Ctrl+Alt+A', 'audio.prepare_book': '', 'settings.connections': '',
    }
    for identity, shortcut in expected.items():
        assert identity in manager.bindings
        assert manager.shortcut(identity) == shortcut
        assert manager.available(identity)
    assigned = [manager.shortcut(identity) for identity in manager.bindings if manager.shortcut(identity)]
    assert len(assigned) == len(set(assigned))
    reader = {c.command_id for c in manager.registry.commands('reader')}
    assert 'reader.reading_copy' in reader
    assert manager.registry.shortcut('reader.bookmarks', {}) == 'Ctrl+Shift+B'
    assert not manager.load_notice


def test_explicit_legacy_import_keeps_original_bytes_and_waits_for_save(composed, tmp_path):
    frame, manager, _ = composed
    old = tmp_path / 'keybindings.json'
    raw = encode_preferences({'File/Save current changes': 'Ctrl+Alt+S',
                              'Library/Portable snapshots and transfer…': 'Ctrl+Alt+Y'})
    old.write_bytes(raw)
    manager.change_root(tmp_path)
    assert 'Import keymap' in manager.load_notice
    assert manager.shortcut('file.save') == 'Ctrl+S'
    assert not manager.store.path.exists()
    draft = import_keymap(old, manager)
    assert draft == {'file.save': 'Ctrl+Alt+S', 'library.portable_sync': 'Ctrl+Alt+Y'}
    assert manager.shortcut('file.save') == 'Ctrl+S' and old.read_bytes() == raw
    manager.apply_overrides(draft)
    assert manager.shortcut('file.save') == 'Ctrl+Alt+S'
    assert KeymapStore(manager.store.path).load() == draft
    assert old.read_bytes() == raw
    assert frame.narration_actions is not None


@pytest.mark.parametrize('overrides', [
    {'Missing module/Unknown': 'Ctrl+J'},
    {'File/Save current changes': 'Ctrl+I'},
    {'File/Save current changes': 'F24'},
    {'File/Save current changes': '', 'file/save current changes': 'Ctrl+J'},
])
def test_incompatible_legacy_import_changes_neither_profile_nor_live_commands(composed, tmp_path, overrides):
    _, manager, _ = composed
    manager.apply_overrides({'file.save': 'Ctrl+Alt+S'})
    saved = manager.store.path.read_bytes()
    old = tmp_path / 'keybindings.json'
    raw = encode_preferences(overrides)
    old.write_bytes(raw)
    with pytest.raises(ValueError):
        import_keymap(old, manager)
    assert manager.store.path.read_bytes() == saved and old.read_bytes() == raw
    assert manager.shortcut('file.save') == 'Ctrl+Alt+S'


def test_new_profile_import_is_complete_and_does_not_silently_apply(composed, tmp_path):
    _, manager, _ = composed
    profile = tmp_path / 'selected-profile.json'
    profile.write_text(json.dumps({'version': 1, 'bindings': {'reader.reading_copy': 'Ctrl+Alt+Y'}}))
    assert import_keymap(profile, manager) == {'reader.reading_copy': 'Ctrl+Alt+Y'}
    assert manager.shortcut('reader.reading_copy') == ''
    assert not manager.store.path.exists()


def test_missing_selected_import_does_not_reset_existing_keymap(composed, tmp_path):
    _, manager, _ = composed
    manager.apply_overrides({'file.save': 'Ctrl+Alt+S'})
    with pytest.raises(ValueError, match='no longer exists'):
        import_keymap(tmp_path / 'missing.json', manager)
    assert manager.shortcut('file.save') == 'Ctrl+Alt+S'


def test_legacy_import_uses_its_bounded_snapshot_and_new_profile_keeps_its_limit(composed, tmp_path):
    _, manager, _ = composed
    old = tmp_path / 'large-old.json'
    old.write_bytes(encode_preferences({'File/Save current changes': 'Ctrl+Alt+S'}) + b' ' * 70000)
    assert import_keymap(old, manager) == {'file.save': 'Ctrl+Alt+S'}
    new = tmp_path / 'large-new.json'
    new.write_bytes(b'{"version":1,"bindings":{}}' + b' ' * 70000)
    with pytest.raises(ValueError, match='64 KiB'):
        import_keymap(new, manager)


def test_restored_profile_remains_inert_until_current_commands_validate(tmp_path):
    path = tmp_path / 'keyboard-shortcuts.json'
    raw = b'{"version":1,"bindings":{"future.module":"Ctrl+J"}}'
    path.write_bytes(raw)
    manager = KeyboardManager(store=KeymapStore(path), registry=CommandRegistry(builtin_commands()))
    assert manager.overrides == {} and manager.load_notice
    assert path.read_bytes() == raw


@pytest.mark.parametrize('change', ['closing', 'destroyed', 'library', 'root', 'profile', 'repaired'])
def test_queued_profile_notice_never_targets_stale_or_destroyed_host(composed, tmp_path, change):
    frame, manager, _ = composed
    queued, reported = [], []
    sys.modules['wx'].CallAfter = lambda callback: queued.append(callback)
    frame.report = reported.append
    damaged = tmp_path / 'damaged'
    damaged.mkdir()
    (damaged / 'keyboard-shortcuts.json').write_bytes(b'not JSON')
    frame.library.root = damaged
    manager.change_root(damaged)
    assert manager.load_notice and len(queued) == 1
    if change == 'closing':
        frame._closing = True
    elif change == 'destroyed':
        frame.IsBeingDeleted = lambda: True
    elif change == 'library':
        frame.library = SimpleNamespace(root=damaged)
    elif change == 'root':
        frame.library.root = tmp_path / 'changed-root'
    elif change == 'profile':
        manager.change_root(tmp_path)
    else:
        manager.apply_overrides({})
    queued[0]()
    assert reported == []


def test_current_profile_notice_is_delivered_after_native_queue_dispatch(composed, tmp_path):
    frame, manager, _ = composed
    queued, reported = [], []
    sys.modules['wx'].CallAfter = lambda callback: queued.append(callback)
    frame.report = reported.append
    (tmp_path / 'keyboard-shortcuts.json').write_bytes(b'not JSON')
    manager.change_root(tmp_path)
    assert reported == []
    queued[0]()
    assert reported == [manager.load_notice]
