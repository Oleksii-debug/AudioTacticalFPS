"""Primary speech, studio and assistant menus use one shared keyboard registry."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

from carr_library.ui.assistant_dialog import install_assistant_actions
from carr_library.ui.library_assistant import install_library_assistant_actions
from carr_library.ui.operations_dialog import install_operations_actions
from carr_library.ui.speech_dialog import install_speech_actions
from carr_library.ui.studio_dialog import install_studio_actions
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry


def test_studio_queues_named_commands_before_final_keyboard_install(monkeypatch):
    class Menu:
        def __init__(self):
            self.items = []

        def Append(self, identity, label):
            item = SimpleNamespace(label=label)
            self.items.append(item)
            return item

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

    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU='menu'))
    frame = SimpleNamespace(Bind=Mock(), menu_bar=MenuBar())
    frame.GetMenuBar = lambda: frame.menu_bar
    install_speech_actions(frame)
    install_studio_actions(frame)
    install_assistant_actions(frame)
    install_library_assistant_actions(frame)
    install_operations_actions(frame)
    pending = frame._pending_keyboard_commands
    assert [spec.command_id for spec, _ in pending] == [
        'voice.reader', 'voice.book', 'audio.studio', 'audio.convert', 'audio.engines',
        'assistant.local', 'assistant.providers', 'library.workflows', 'operations.tasks']
    registry = CommandRegistry((*builtin_commands(), *(spec for spec, _ in pending)))
    assert registry.shortcut('audio.studio', {}) == 'Ctrl+Alt+A'
    assert registry.shortcut('voice.reader', {}) == 'Ctrl+Alt+V'
    assert registry.shortcut('voice.book', {}) == 'Ctrl+Alt+R'
    assert registry.shortcut('assistant.local', {}) == 'Ctrl+Alt+J'
    assert registry.shortcut('research.search_center', {}) == 'Ctrl+Alt+F'
    assert registry.shortcut('audio.studio', {'audio.studio': 'Ctrl+Alt+U'}) == 'Ctrl+Alt+U'
    assert registry.shortcut('operations.tasks', {}) == ''
    assert registry.shortcut('operations.tasks', {'operations.tasks': 'Ctrl+Alt+F12'}) == 'Ctrl+Alt+F12'
    assert [label for _, label in frame.menu_bar.menus] == ['&Voice', 'Audio St&udio', '&Tools']
    assert len(frame.menu_bar.menus[-1][0].items) == 4
    assert frame.Bind.call_count == 9
    for (_, item), call in zip(pending, frame.Bind.call_args_list):
        assert call.args[2] is item
