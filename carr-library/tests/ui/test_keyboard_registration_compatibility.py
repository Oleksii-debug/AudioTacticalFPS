"""Retain canonical late keyboard registration and live warning behavior."""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui.command_registry import CommandRegistry, CommandSpec
from carr_library.ui.keyboard_settings import install_keyboard_settings, register_menu_command
from carr_library.keyboard_preferences import KeymapStore
from tests.ui.test_keyboard_manager import (
    DynamicMenuBar, Frame, Menu, MenuItem, manager, registry, snapshot,
)


def test_optional_late_shortcut_conflict_preserves_stable_default_and_user_file(manager):
    manager.apply_overrides({'library.import': 'Ctrl+Shift+Q'})
    before = manager.store.path.read_bytes()
    item = MenuItem(91, 'Query current progress\tCtrl+Shift+Q')
    manager.frame.items.append(item)
    spec = CommandSpec('progress.query', 'Query current progress', default_shortcut='Ctrl+Shift+Q')
    manager.register_menu_command(spec, item, fallback_unassigned=True)
    assert manager.registry.get('progress.query') == spec
    assert manager.shortcut('library.import') == 'Ctrl+Shift+Q'
    assert manager.shortcut('progress.query') == ''
    assert item.GetItemLabel() == 'Query current progress'
    assert manager.load_notice and 'new command' in manager.load_notice
    assert manager.store.path.read_bytes() == before
    # Reset remains predictable: the declared default is unchanged.
    manager.apply_overrides({})
    assert manager.shortcut('library.import') == 'Ctrl+I'
    assert manager.shortcut('progress.query') == 'Ctrl+Shift+Q'


def test_optional_fallback_does_not_hide_an_unrelated_existing_conflict(manager):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    manager.frame.items.append(MenuItem(91, 'Unrelated unregistered action\tCtrl+K'))
    item = MenuItem(92, 'New extension\tCtrl+J')
    manager.frame.items.append(item)
    before = snapshot(manager)
    with pytest.raises(ValueError, match='Unrelated'):
        manager.register_menu_command(CommandSpec('extension.action', 'New extension',
                                                  default_shortcut='Ctrl+J'), item, fallback_unassigned=True)
    assert snapshot(manager) == before


def test_late_profile_recovery_does_not_announce_a_stale_startup_warning(tmp_path, monkeypatch):
    from carr_library.ui.builtin_keyboard_commands import builtin_commands
    frame = Frame()
    frame.library = SimpleNamespace(root=tmp_path)
    frame.menu = DynamicMenuBar([('&Library', Menu(frame.items), True)])
    frame.Bind = lambda *args, **kwargs: None
    spec = CommandSpec('extension.action', 'Extension action', default_shortcut='Ctrl+J')
    path = tmp_path / 'keyboard-shortcuts.json'
    KeymapStore(path).save({'extension.action': 'Ctrl+Shift+J'},
                          CommandRegistry((*builtin_commands(), spec)))
    deferred = []
    fake_wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object(),
                              CallAfter=lambda callback, *args: deferred.append(lambda: callback(*args)))
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    manager = install_keyboard_settings(frame)
    assert manager.load_notice and deferred
    item = frame.menu.GetMenu(0).Append(-1, 'Extension action\tCtrl+J')
    register_menu_command(frame, spec, item)
    assert not manager.load_notice
    for callback in deferred:
        callback()
    assert frame.notices == []
