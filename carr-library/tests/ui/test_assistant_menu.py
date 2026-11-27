"""Actual shared registry composition with native menu doubles."""
import sys

from carr_library.ui.assistant_dialog import install_assistant_actions
from test_keyboard_composition import composed


def test_assistant_joins_existing_command_registry_without_duplicate_menus(composed):
    frame, manager, handlers = composed
    sys.modules['wx'].NOT_FOUND = -1
    item = install_assistant_actions(frame)
    assert install_assistant_actions(frame) is item
    assert manager.shortcut('assistant.open') == 'Ctrl+Alt+J'
    assert manager.available('assistant.open')
    assert manager.bindings['assistant.open'].item is item
    assert item.GetId() in handlers
    assert sum(label.replace('&', '') == 'Options' for label, _, _ in frame.menu_bar.entries) == 1
    assert sum(label.replace('&', '') == 'Tools' for label, _, _ in frame.menu_bar.entries) == 1
    assert manager.shortcut('research.search_center') == 'Ctrl+Alt+F'
    assert manager.shortcut('audio.open') == 'Ctrl+Alt+A'
