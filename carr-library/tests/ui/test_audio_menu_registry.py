"""Audio/API commands use the real shared registry and preserve user bindings."""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui import keyboard_settings
from carr_library.ui.command_registry import CommandRegistry, ShortcutError
from carr_library.ui.audio_studio import install_audio_studio_actions
from carr_library.ui.api_connections import install_api_connection_actions


class Item:
    serial = 0
    def __init__(self, label):
        Item.serial += 1
        self.identity, self.label = Item.serial, label
    def GetId(self): return self.identity
    def GetItemLabel(self): return self.label
    def SetItemLabel(self, label): self.label = label
    def IsSeparator(self): return False
    def GetSubMenu(self): return None


class Menu:
    def __init__(self): self.items = []
    def Append(self, identity, label):
        item = Item(label)
        self.items.append(item)
        return item
    def GetMenuItems(self): return self.items


class Bar:
    def __init__(self): self.menus = []
    def Append(self, menu, label): self.menus.append((menu, label))
    def GetMenuCount(self): return len(self.menus)
    def GetMenu(self, index): return self.menus[index][0]
    def GetMenuLabel(self, index): return self.menus[index][1]


@pytest.mark.parametrize('manager_first', [True, False])
def test_audio_and_connections_register_before_or_after_keyboard_manager(monkeypatch, manager_first):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object()))
    monkeypatch.setattr(keyboard_settings, 'accelerator_tuple', lambda shortcut, identity: (0, 0, identity))
    bar, handlers = Bar(), []
    frame = SimpleNamespace(menu_bar=bar, GetMenuBar=lambda: bar,
                            Bind=lambda event, handler, item: handlers.append((handler, item)))
    if manager_first:
        frame.keyboard_commands = keyboard_settings.KeyboardManager(frame, registry=CommandRegistry())
    install_audio_studio_actions(frame)
    install_api_connection_actions(frame)
    if not manager_first:
        pending = frame._pending_keyboard_commands
        frame.keyboard_commands = keyboard_settings.KeyboardManager(frame, registry=CommandRegistry())
        for spec, item in pending:
            frame.keyboard_commands.register_menu_command(spec, item)
    manager = frame.keyboard_commands
    assert set(manager.bindings) == {'audio.studio.open', 'audio.narration.prepare_selected', 'api.connections.open'}
    assert len(handlers) == 3
    assert [bar.GetMenuLabel(i) for i in range(2)] == ['A&udio', '&Connections']
    manager.apply_overrides({'audio.studio.open': 'Ctrl+Alt+J', 'api.connections.open': 'Ctrl+Shift+K'})
    assert manager.bindings['audio.studio.open'].item.GetItemLabel().endswith('\tCtrl+Alt+J')
    assert manager.bindings['api.connections.open'].item.GetItemLabel().endswith('\tCtrl+Shift+K')
    with pytest.raises(ShortcutError):
        manager.apply_overrides({'audio.studio.open': 'Ctrl+Alt+J', 'api.connections.open': 'Ctrl+Alt+J'})
    assert manager.shortcut('api.connections.open') == 'Ctrl+Shift+K'
