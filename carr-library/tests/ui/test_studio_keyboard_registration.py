"""Primary speech, studio and assistant menus use one shared keyboard registry."""
from types import SimpleNamespace
from itertools import count
from unittest.mock import Mock
import sys

from carr_library.ui.assistant_dialog import install_assistant_actions
from carr_library.ui.library_assistant import install_library_assistant_actions
from carr_library.ui.operations_dialog import install_operations_actions
from carr_library.ui.speech_dialog import install_speech_actions
from carr_library.ui.studio_dialog import install_studio_actions
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry
from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.keyboard_settings import install_keyboard_settings


def test_studio_queues_named_commands_before_final_keyboard_install(monkeypatch, tmp_path):
    identifiers = count(1000)
    class Item:
        def __init__(self, label):
            self.identifier, self.label = next(identifiers), label
        def GetId(self): return self.identifier
        def GetItemLabel(self): return self.label
        def SetItemLabel(self, label): self.label = label
        def IsSeparator(self): return False
        def GetSubMenu(self): return None
        def IsEnabled(self): return True

    class Menu:
        def __init__(self):
            self.items = []

        def Append(self, identity, label):
            item = Item(label)
            self.items.append(item)
            return item

        def GetMenuItems(self):
            return tuple(self.items)

    class MenuBar:
        def __init__(self):
            self.menus = []

        def Append(self, menu, label):
            self.menus.append((menu, label))

        def GetMenuCount(self):
            return len(self.menus)

        def GetMenuLabel(self, index):
            return self.menus[index][1]

        def GetMenu(self, index):
            return self.menus[index][0]

        def IsEnabledTop(self, index):
            return True

    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU='menu',
        AcceleratorTable=tuple, CallAfter=lambda callback: callback()))
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda key, identifier: (0, key, identifier))
    frame = SimpleNamespace(Bind=Mock(), menu_bar=MenuBar(), library=SimpleNamespace(root=tmp_path),
        _closing=False, IsBeingDeleted=lambda: False, IsEnabled=lambda: True,
        SetAcceleratorTable=Mock(), report=Mock(), choice=Mock(), status=Mock())
    frame.GetMenuBar = lambda: frame.menu_bar
    install_speech_actions(frame)
    install_studio_actions(frame)
    install_assistant_actions(frame)
    install_library_assistant_actions(frame)
    install_operations_actions(frame)
    pending = frame._pending_keyboard_commands
    assert [spec.command_id for spec, _ in pending] == [
        'voice.reader', 'voice.clipboard', 'voice.book', 'audio.studio', 'audio.clips',
        'audio.pronunciation', 'audio.recording', 'audio.publishing', 'audio.convert',
        'audio.metadata', 'audio.transcription', 'audio.engines',
        'assistant.local', 'assistant.providers', 'library.workflows', 'operations.tasks']
    registry = CommandRegistry((*builtin_commands(), *(spec for spec, _ in pending)))
    assert registry.shortcut('audio.studio', {}) == 'Ctrl+Alt+A'
    assert registry.shortcut('voice.reader', {}) == 'Ctrl+Alt+V'
    assert registry.shortcut('voice.book', {}) == 'Ctrl+Alt+R'
    assert registry.shortcut('assistant.local', {}) == 'Ctrl+Alt+J'
    assert registry.shortcut('research.search_center', {}) == 'Ctrl+Alt+F'
    assert registry.shortcut('audio.studio', {'audio.studio': 'Ctrl+Alt+U'}) == 'Ctrl+Alt+U'
    assert [label for _, label in frame.menu_bar.menus] == ['&Voice', 'Audio St&udio', '&Tools']
    assert registry.shortcut('operations.tasks', {}) == ''
    assert registry.shortcut('operations.tasks', {'operations.tasks': 'Ctrl+Alt+F12'}) == 'Ctrl+Alt+F12'
    assert len(frame.menu_bar.menus[-1][0].items) == 4
    assert frame.Bind.call_count == 16
    for (_, item), call in zip(pending, frame.Bind.call_args_list):
        assert call.args[2] is item

    # A saved canonical voice binding must survive actual menu capture, rather
    # than being dropped or renamed to a generated caption-hash identity.
    saved = {'voice.reader': 'Ctrl+Shift+V', 'voice.book': 'Ctrl+Shift+R',
             'operations.tasks': 'Ctrl+Alt+F12'}
    path = tmp_path / 'keyboard-shortcuts.json'
    KeymapStore(path).save(saved, registry)
    before = path.read_bytes()
    manager = install_keyboard_settings(frame)
    assert manager.overrides == saved and path.read_bytes() == before
    assert manager.shortcut('voice.reader') == saved['voice.reader']
    assert manager.shortcut('voice.book') == saved['voice.book']
    assert manager.shortcut('operations.tasks') == saved['operations.tasks']
    assert manager.shortcut('audio.studio') == 'Ctrl+Alt+A'
    assert set(spec.command_id for spec, _ in pending) <= set(manager.bindings)
    assert len({binding.item.GetId() for binding in manager.bindings.values()}) == len(manager.bindings)
    manager.guard_native()
    frame.keyboard_settings._guard()
