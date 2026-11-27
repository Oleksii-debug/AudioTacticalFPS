"""Shared native-menu model composition; actual wx is a separate probe."""
from carr_library.ui import language_matrix
from test_keyboard_composition import composed


def test_language_command_joins_existing_audio_menu_and_shared_registry(composed, monkeypatch):
    frame, manager, handlers = composed
    calls = []
    monkeypatch.setattr(language_matrix, 'show_language_matrix', calls.append)
    for _label, menu, _enabled in frame.menu_bar.entries:
        monkeypatch.setattr(menu, 'GetMenuItemCount', lambda menu=menu: len(menu.GetMenuItems()), raising=False)
    before = frame.menu_bar.GetMenuCount()
    item = language_matrix.install_language_actions(frame)
    assert language_matrix.install_language_actions(frame) is item
    assert frame.menu_bar.GetMenuCount() == before
    assert manager.registry.get('language.capabilities').label == 'Languages and capability evidence'
    assert manager.available('language.capabilities') and manager.shortcut('language.capabilities') == ''
    handlers[item.GetId()](None)
    assert calls == [frame]
    audio = frame.menu_bar.GetMenu(frame.menu_bar.FindMenu('Audio'))
    assert sum(menu_item.GetId() == item.GetId() for menu_item in audio.GetMenuItems()) == 1
    assert 'audio.open' in manager.bindings and 'reader.reading_copy' in manager.registry.specs
