"""803 dialog calls against the F749 registry, with explicit wx event doubles.

This is controller/registration coverage, not native Windows key-dispatch proof.
"""
from itertools import count
from types import SimpleNamespace
import sys

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry
from carr_library.ui.keyboard_settings import (
    CommandBinding, KeyboardManager, bind_context_shortcuts,
)


class Window:
    def __init__(self):
        self.table = ('previous native table',)
        self.handlers = {}
        self.enabled = True
        self.deleting = False

    def GetAcceleratorTable(self):
        return self.table

    def SetAcceleratorTable(self, table):
        self.table = table

    def Bind(self, event, handler, *, id=None):
        self.handlers[event, id] = handler

    def Unbind(self, event, *, id=None, handler=None):
        self.handlers.pop((event, id), None)

    def IsBeingDeleted(self):
        return self.deleting

    def IsEnabled(self):
        return self.enabled


@pytest.fixture
def manager(monkeypatch):
    ids = count(1000)
    wx = SimpleNamespace(NewIdRef=lambda: next(ids), EVT_MENU='menu',
                         EVT_WINDOW_DESTROY='destroy', AcceleratorTable=tuple)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda shortcut, identity: (shortcut, 0, identity))
    return KeyboardManager()


def save_binding(handler):
    return CommandBinding('reading_copy.save', 'Save reading copy', 'Ctrl+S', handler)


def test_actual_editor_call_dispatches_original_handler_and_remaps_live(manager):
    window, seen = Window(), []
    original_table = window.table
    unregister = manager.bind_window(window, 'reading_copy', [save_binding(seen.append)])
    assert window.table[0][0] == 'Ctrl+S'
    native_id = window.table[0][2]
    event = object()
    window.handlers['menu', native_id](event)
    assert seen == [event]
    manager.apply_overrides({'reading_copy.save': 'Ctrl+Shift+J'})
    assert window.table == (('Ctrl+Shift+J', 0, native_id),)
    unregister()
    unregister()
    assert window.table == original_table and ('menu', native_id) not in window.handlers
    assert not tuple(manager._adapters)
    # Closing a window does not remove persistent semantic command identities.
    assert manager.shortcut('reading_copy.save') == 'Ctrl+Shift+J'


def test_two_editor_windows_do_not_dispatch_or_unregister_each_other(manager):
    first, second, seen = Window(), Window(), []
    close_first = manager.bind_window(first, 'reading_copy', [save_binding(lambda event: seen.append('first'))])
    manager.bind_window(second, 'reading_copy', [save_binding(lambda event: seen.append('second'))])
    second.handlers['menu', second.table[0][2]](None)
    close_first()
    manager.apply_overrides({'reading_copy.save': 'Ctrl+Shift+J'})
    second.handlers['menu', second.table[0][2]](None)
    assert seen == ['second', 'second']
    assert second.table[0][0] == 'Ctrl+Shift+J'


def test_disabled_closed_and_destroyed_windows_cannot_invoke_editor(manager):
    window, seen = Window(), []
    close = manager.bind_window(window, 'reading_copy', [save_binding(seen.append)])
    callback = window.handlers['menu', window.table[0][2]]
    window.enabled = False
    assert callback(None) is False and not seen
    window.enabled = True
    close()
    assert callback(None) is False and not seen


def test_child_destruction_does_not_remove_parent_context(manager):
    window, seen = Window(), []
    manager.bind_window(window, 'reading_copy', [save_binding(seen.append)])
    destroyed = window.handlers['destroy', None]
    event = SimpleNamespace(GetEventObject=lambda: object(), Skip=lambda: None)
    destroyed(event)
    assert len(tuple(manager._adapters)) == 1
    window.deleting = True
    destroyed(SimpleNamespace(GetEventObject=lambda: window, Skip=lambda: None))
    assert not tuple(manager._adapters)
    manager.apply_overrides({'reading_copy.save': 'Ctrl+Shift+J'})


def test_parent_lookup_accepts_canonical_and_compatibility_host_names(manager):
    for attribute in ('keyboard_commands', 'keyboard'):
        window = Window()
        host = SimpleNamespace(**{attribute: manager})
        child = SimpleNamespace(GetParent=lambda: host)
        close = bind_context_shortcuts(window, child, 'reading_copy', [save_binding(lambda event: None)])
        assert window.table[0][0] == 'Ctrl+S'
        close()


def test_existing_editor_keymap_loads_before_any_editor_is_open(tmp_path, manager):
    store = KeymapStore(tmp_path / 'keyboard-shortcuts.json')
    store.load()
    store.save({'reading_copy.save': 'Ctrl+Shift+J', 'speech_reader.save': 'Ctrl+Shift+J'},
               manager.registry)
    reopened = KeyboardManager(store=KeymapStore(store.path))
    assert reopened.notice == ''
    assert reopened.shortcut('reading_copy.save') == 'Ctrl+Shift+J'
    assert reopened.shortcut('speech_reader.save') == 'Ctrl+Shift+J'


@pytest.mark.parametrize('context,bindings', [
    ('reading_copy', [CommandBinding('reading_copy.save', 'Changed definition', 'Ctrl+S', lambda e: None)]),
    ('main', [save_binding(lambda e: None)]),
    ('reading_copy', [save_binding(None)]),
    ('reading_copy', [save_binding(lambda e: None), save_binding(lambda e: None)]),
])
def test_invalid_context_registration_never_changes_native_or_registry_state(manager, context, bindings):
    window = Window()
    before = manager.registry.commands(), window.table
    with pytest.raises(ValueError):
        manager.bind_window(window, context, bindings)
    assert (manager.registry.commands(), window.table) == before
    assert not window.handlers


def test_second_binding_on_same_window_is_rejected_without_replacing_first(manager):
    window = Window()
    manager.bind_window(window, 'reading_copy', [save_binding(lambda e: None)])
    table = window.table
    with pytest.raises(ValueError, match='already'):
        manager.bind_window(window, 'reading_copy', [save_binding(lambda e: None)])
    assert window.table == table


def test_save_all_has_stable_main_id_and_keeps_editor_save_context_separate(manager):
    registry = CommandRegistry(builtin_commands())
    assert registry.get('library.save_all').context == 'main'
    assert registry.shortcut('library.save_all', {}) == 'Ctrl+S'
    assert registry.get('reading_copy.save').context == 'reading_copy'
    assert registry.shortcut('reading_copy.save', {}) == 'Ctrl+S'
    assert registry.validate({'reading_copy.save': 'Ctrl+Shift+J'})


def test_save_all_discovery_and_invocation_keep_original_native_menu_id(manager, monkeypatch):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    # Installed acceptance uses pytest importlib mode and an empty pythonpath.
    # Load only this sibling test fixture; never expose the product source tree.
    spec = spec_from_file_location('_carr_keyboard_test_fixtures',
                                   Path(__file__).with_name('test_keyboard_manager.py'))
    fixtures = module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    Frame, MenuItem = fixtures.Frame, fixtures.MenuItem
    frame, seen = Frame(), []
    frame.items.append(MenuItem(77, 'Save All\tCtrl+S'))
    frame.GetEventHandler = lambda: SimpleNamespace(
        ProcessEvent=lambda event: seen.append(event.identifier) or True)
    wx = sys.modules['wx']
    wx.EVT_MENU = SimpleNamespace(typeId=123)
    wx.CommandEvent = lambda event_type, identifier: SimpleNamespace(
        identifier=identifier, SetEventObject=lambda value: None)
    current = KeyboardManager(frame)
    current.refresh()
    assert current.bindings['library.save_all'].item.GetId() == 77
    assert 'Ctrl+S' not in current._unmanaged
    assert current.invoke('library.save_all')
    assert seen == [77]


def test_803_method_names_delegate_to_the_one_manager(manager, monkeypatch):
    seen = []
    monkeypatch.setattr(manager, 'change_root', lambda root: seen.append(root))
    monkeypatch.setattr('carr_library.ui.keyboard_settings.show_keyboard_settings',
                        lambda frame, chosen: seen.append(chosen))
    manager.retarget('/new/library')
    manager.show_settings()
    assert seen == ['/new/library', manager]
