"""Controller transactions with real keymap files and explicit wx/menu doubles.

Actual OS key dispatch is covered by the separate native keyboard probe. These
tests exercise persistence failure, rejected imports and late module wiring.
"""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapError, KeymapStore
from carr_library.ui.command_registry import CommandRegistry, CommandSpec, ShortcutError
from carr_library.ui.keyboard_settings import (
    KeyboardManager, KeyboardSettingsDialog, install_keyboard_settings, register_menu_command,
)


class MenuItem:
    def __init__(self, identifier, label):
        self.identifier, self.label = identifier, label
        self.enabled = True
        self.fail_once = False

    def GetId(self):
        return self.identifier

    def GetItemLabel(self):
        return self.label

    def SetItemLabel(self, label):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('Synthetic native menu failure')
        self.label = label

    def IsSeparator(self):
        return False

    def GetSubMenu(self):
        return None

    def IsEnabled(self):
        return self.enabled


class Menu:
    _next_id = 1000

    def __init__(self, items=None):
        self.items = items if items is not None else []

    def GetMenuItems(self):
        return tuple(self.items)

    def Append(self, identifier, label):
        if identifier < 0:
            Menu._next_id += 1
            identifier = Menu._next_id
        item = MenuItem(identifier, label)
        self.items.append(item)
        return item


class DynamicMenuBar:
    def __init__(self, entries):
        self.entries = entries

    def GetMenuCount(self):
        return len(self.entries)

    def GetMenuLabel(self, index):
        return self.entries[index][0]

    def GetMenu(self, index):
        return self.entries[index][1]

    def IsEnabledTop(self, index):
        return self.entries[index][2]

    def Append(self, menu, label):
        self.entries.append((label, menu, True))


class MenuBar:
    def __init__(self, items):
        self.menu = Menu(items)
        self.enabled = True

    def GetMenuCount(self):
        return 1

    def GetMenuLabel(self, index):
        assert index == 0
        return '&Library'

    def GetMenu(self, index):
        assert index == 0
        return self.menu

    def IsEnabledTop(self, index):
        return self.enabled


class Frame:
    def __init__(self):
        self.items = [MenuItem(11, 'New library\tCtrl+N'), MenuItem(12, 'Import books\tCtrl+I')]
        self.menu = MenuBar(self.items)
        self._closing = False
        self.enabled = True
        self.notices = []
        self.report = self.notices.append

    def GetMenuBar(self):
        return self.menu

    def IsEnabled(self):
        return self.enabled


@pytest.fixture
def registry():
    return CommandRegistry((
        CommandSpec('library.new', 'New library', default_shortcut='Ctrl+N'),
        CommandSpec('library.import', 'Import books', default_shortcut='Ctrl+I'),
        CommandSpec('reader.next', 'Next source location', 'reader', 'Ctrl+PageDown'),
    ))


@pytest.fixture
def manager(tmp_path, registry, monkeypatch):
    # Deliberate parser double, not native accelerator evidence.
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple', lambda key, identifier: (0, 0, identifier))
    frame = Frame()
    manager = KeyboardManager(frame, store=KeymapStore(tmp_path / 'keyboard-shortcuts.json'), registry=registry)
    manager.refresh()
    return manager


def snapshot(manager):
    path = manager.store.path
    return (dict(manager.overrides), path.read_bytes() if path.exists() else None,
            tuple(item.GetItemLabel() for item in manager.frame.items), manager.registry.commands(),
            tuple(manager.bindings))


@pytest.mark.parametrize('builtin', [True, False])
def test_one_native_menu_item_cannot_have_two_command_ids(manager, builtin):
    if builtin:
        item = manager.frame.items[1]
    else:
        item = MenuItem(91, 'Extension action')
        manager.frame.items.append(item)
        manager.register_menu_command(CommandSpec('extension.one', 'Extension action',
                                                  default_shortcut='Ctrl+J'), item)
    manager.apply_overrides({'library.new': 'Ctrl+Shift+N'})
    before = snapshot(manager)
    with pytest.raises(ValueError, match='multiple command identities'):
        manager.register_menu_command(CommandSpec('extension.alias', 'Other action',
                                                  default_shortcut='Ctrl+K'), item)
    assert snapshot(manager) == before
    assert 'extension.alias' not in {row.command_id for row in manager.registry.commands()}


def test_stable_command_cannot_retarget_another_live_item(manager):
    first, second = MenuItem(91, 'First action'), MenuItem(92, 'Second action')
    manager.frame.items.extend((first, second))
    spec = CommandSpec('extension.one', 'First action', default_shortcut='Ctrl+J')
    manager.register_menu_command(spec, first)
    manager.apply_overrides({'extension.one': ''})
    before = snapshot(manager)
    with pytest.raises(ValueError, match='already bound'):
        manager.register_menu_command(spec, second)
    assert snapshot(manager) == before
    assert manager.bindings[spec.command_id].item is first


def test_same_identity_same_item_registration_is_idempotent(manager):
    item = MenuItem(91, 'Extension action')
    manager.frame.items.append(item)
    spec = CommandSpec('extension.one', 'Extension action', default_shortcut='Ctrl+J')
    manager.register_menu_command(spec, item)
    before = snapshot(manager)
    manager.register_menu_command(spec, item)
    assert snapshot(manager) == before


@pytest.mark.parametrize('destroyed', [False, True])
def test_removed_native_item_can_be_replaced(manager, monkeypatch, destroyed):
    first, second = MenuItem(91, 'First action'), MenuItem(92, 'First action')
    manager.frame.items.extend((first, second))
    spec = CommandSpec('extension.one', 'First action', default_shortcut='Ctrl+J')
    manager.register_menu_command(spec, first)
    manager.frame.items.remove(first)
    if destroyed:
        def deleted_native_item():
            raise RuntimeError('Synthetic deleted wx.MenuItem wrapper')
        monkeypatch.setattr(first, 'GetId', deleted_native_item)
    manager.register_menu_command(spec, second)
    assert manager.bindings[spec.command_id].item is second
    assert second.GetItemLabel() == 'First action\tCtrl+J'


def test_apply_and_reopen_preserve_semantic_bindings_and_menu_display(manager):
    result = manager.apply_overrides({'library.import': 'control+shift+k', 'reader.next': ''})
    assert result == {'library.import': 'Ctrl+Shift+K', 'reader.next': ''}
    assert manager.frame.items[1].GetItemLabel() == 'Import books\tCtrl+Shift+K'
    reopened = KeyboardManager(store=KeymapStore(manager.store.path), registry=manager.registry)
    assert reopened.overrides == result
    assert reopened.shortcut('library.new') == 'Ctrl+N'


@pytest.mark.parametrize('invalid', [
    {'library.import': 'Ctrl+N'}, {'library.import': 'Ctrl+C'},
    {'unknown.command': 'Ctrl+J'}, {'library.import': False},
])
def test_rejected_maps_leave_active_persisted_and_native_menu_state_unchanged(manager, invalid):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    before = snapshot(manager)
    with pytest.raises(ValueError):
        manager.apply_overrides(invalid)
    assert snapshot(manager) == before


def test_native_parser_failure_happens_before_any_change(manager, monkeypatch):
    before = snapshot(manager)

    def unavailable(*args):
        raise ValueError('Synthetic native parser rejection')
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple', unavailable)
    with pytest.raises(ValueError, match='parser'):
        manager.apply_overrides({'library.import': 'Ctrl+K'})
    assert snapshot(manager) == before


def test_storage_failure_does_not_change_live_or_saved_bindings(manager, monkeypatch):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    before = snapshot(manager)

    def unavailable(*args):
        raise KeymapError('Synthetic read-only storage')
    monkeypatch.setattr(manager.store, 'save', unavailable)
    with pytest.raises(KeymapError, match='read-only'):
        manager.apply_overrides({'library.import': 'Ctrl+J'})
    assert snapshot(manager) == before


def test_native_apply_failure_on_first_save_does_not_create_a_settings_file(manager):
    before = snapshot(manager)
    manager.frame.items[1].fail_once = True
    with pytest.raises(RuntimeError, match='native menu failure'):
        manager.apply_overrides({'library.import': 'Ctrl+K'})
    assert snapshot(manager) == before


def test_native_apply_failure_cannot_commit_then_require_a_second_disk_write(manager, monkeypatch):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    before = snapshot(manager)
    original = manager.store.save
    calls = []

    def fail_second(*args):
        calls.append(args)
        if len(calls) > 1:
            raise KeymapError('Synthetic failure writing rollback')
        return original(*args)
    monkeypatch.setattr(manager.store, 'save', fail_second)
    manager.frame.items[1].fail_once = True
    with pytest.raises((KeymapError, RuntimeError)):
        manager.apply_overrides({'library.import': 'Ctrl+J'})
    assert snapshot(manager) == before
    assert calls == []


@pytest.mark.parametrize('case', ['wrong-context', 'detached-menu', 'live-conflict'])
def test_rejected_extension_registration_does_not_leave_partial_command_or_binding(manager, case):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    item = MenuItem(91, 'Extension action')
    if case != 'detached-menu':
        manager.frame.items.append(item)
    spec = CommandSpec('extension.action', 'Extension action',
                       'reader' if case == 'wrong-context' else 'main',
                       'Ctrl+K' if case == 'live-conflict' else 'Ctrl+J')
    before = snapshot(manager)
    with pytest.raises(ValueError):
        manager.register_menu_command(spec, item)
    assert snapshot(manager) == before
    assert 'extension.action' not in manager.registry.specs


def test_explicit_late_command_registration_is_discoverable_and_rebindable(manager):
    item = MenuItem(91, 'Extension action\tCtrl+J')
    manager.frame.items.append(item)
    spec = CommandSpec('extension.action', 'Extension action', default_shortcut='Ctrl+J')
    manager.register_menu_command(spec, item)
    assert manager.available('extension.action')
    manager.apply_overrides({'extension.action': 'Ctrl+Shift+J'})
    assert item.GetItemLabel() == 'Extension action\tCtrl+Shift+J'
    manager.refresh()
    assert manager.available('extension.action')


def test_startup_saved_binding_conflicting_with_new_unmanaged_menu_uses_safe_defaults(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    KeymapStore(path).save({'library.import': 'Ctrl+K'}, registry)
    before = path.read_bytes()
    frame = Frame()
    frame.items.append(MenuItem(91, 'New module action\tCtrl+K'))
    manager = KeyboardManager(frame, store=KeymapStore(path), registry=registry)
    manager.activate_loaded()
    assert manager.overrides == {}
    assert manager.load_notice
    assert frame.items[1].GetItemLabel() == 'Import books\tCtrl+I'
    assert path.read_bytes() == before


def test_startup_default_collision_is_unassigned_with_notice_and_no_disk_write(tmp_path, registry):
    path = tmp_path / 'keyboard-shortcuts.json'
    frame = Frame()
    frame.items.append(MenuItem(91, 'New module action\tCtrl+I'))
    manager = KeyboardManager(frame, store=KeymapStore(path), registry=registry)
    manager.activate_loaded()
    assert manager.shortcut('library.import') == ''
    assert manager.load_notice
    assert frame.items[1].GetItemLabel() == 'Import books'
    assert not path.exists()


def test_unknown_menu_shortcut_is_a_real_conflict(manager):
    manager.frame.items.append(MenuItem(91, 'Unregistered action\tCtrl+J'))
    manager.refresh()
    before = snapshot(manager)
    with pytest.raises(ValueError, match='Unregistered action'):
        manager.apply_overrides({'library.import': 'Ctrl+J'})
    assert snapshot(manager) == before


def test_disabled_native_item_or_parent_is_unavailable(manager):
    assert manager.available('library.import')
    manager.frame.items[1].enabled = False
    assert not manager.available('library.import')
    manager.frame.items[1].enabled = True
    manager.frame.menu.enabled = False
    assert not manager.available('library.import')
    manager.frame.menu.enabled = True
    manager.frame.enabled = False
    assert not manager.available('library.import')
    manager.frame.enabled = True
    manager.frame._closing = True
    assert not manager.available('library.import')
    assert not manager.available('missing.command')


def test_root_change_loads_the_new_folders_map_without_overwriting_either(manager, tmp_path):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    previous_path = manager.store.path
    previous = previous_path.read_bytes()
    another = tmp_path / 'another-library'
    new_path = another / 'keyboard-shortcuts.json'
    KeymapStore(new_path).save({'library.import': 'Ctrl+J'}, manager.registry)
    manager.change_root(another)
    assert manager.shortcut('library.import') == 'Ctrl+J'
    assert manager.frame.items[1].GetItemLabel() == 'Import books\tCtrl+J'
    assert previous_path.read_bytes() == previous
    assert manager.store.path == new_path


def dialog_double(manager):
    dialog = object.__new__(KeyboardSettingsDialog)
    dialog.manager = manager
    dialog.original_store = manager.store
    dialog.draft = dict(manager.overrides)
    dialog.dialog = SimpleNamespace(EndModal=lambda result: None)
    dialog.messages = []
    dialog._message = dialog.messages.append
    dialog.rebuild = lambda **kwargs: None
    return dialog


def file_dialog_wx(path):
    class Chooser:
        def __init__(self, *args, **kwargs):
            pass

        def ShowModal(self):
            return 1

        def GetPath(self):
            return str(path)

        def Destroy(self):
            pass
    return SimpleNamespace(FileDialog=Chooser, FD_OPEN=2, FD_FILE_MUST_EXIST=4, ID_OK=1)


@pytest.mark.parametrize('raw', [
    '{broken',
    '{"version":1,"bindings":{"unknown.command":"Ctrl+J"}}',
    '{"version":1,"bindings":{"library.import":"Ctrl+N"}}',
    '{"version":1,"bindings":{"library.import":"Ctrl+C"}}',
])
def test_invalid_import_keeps_draft_live_map_and_persisted_settings(manager, tmp_path, raw):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    before = snapshot(manager)
    path = tmp_path / 'import.json'
    path.write_text(raw)
    dialog = dialog_double(manager)
    dialog.draft['reader.next'] = 'Ctrl+Shift+J'
    draft_before = dict(dialog.draft)
    dialog.wx = file_dialog_wx(path)
    dialog.import_map()
    assert dialog.draft == draft_before
    assert snapshot(manager) == before
    assert 'not imported' in dialog.messages[-1]


def test_valid_import_is_only_a_draft_until_save(manager, tmp_path):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    before = snapshot(manager)
    path = tmp_path / 'import.json'
    KeymapStore(path).save({'library.import': 'Ctrl+J'}, manager.registry)
    dialog = dialog_double(manager)
    dialog.wx = file_dialog_wx(path)
    dialog.import_map()
    assert dialog.draft == {'library.import': 'Ctrl+J'}
    assert snapshot(manager) == before


def test_settings_open_before_root_change_cannot_save_into_new_library(manager, tmp_path):
    dialog = dialog_double(manager)
    dialog.draft['library.import'] = 'Ctrl+K'
    manager.change_root(tmp_path / 'new-library')
    before = snapshot(manager)
    dialog.save()
    assert snapshot(manager) == before
    assert 'folder changed' in dialog.messages[-1]


def test_pending_saved_module_map_activates_only_after_all_command_ids_are_known(tmp_path, registry):
    specs = (CommandSpec('extension.one', 'First extension', default_shortcut='Ctrl+J'),
             CommandSpec('extension.two', 'Second extension', default_shortcut='Ctrl+H'))
    complete = CommandRegistry((*registry.commands(), *specs))
    path = tmp_path / 'keyboard-shortcuts.json'
    expected = {'library.import': 'Ctrl+K', 'extension.one': 'Ctrl+Shift+J', 'extension.two': ''}
    KeymapStore(path).save(expected, complete)
    raw = path.read_bytes()
    frame = Frame()
    first, second = MenuItem(91, 'First extension\tCtrl+J'), MenuItem(92, 'Second extension\tCtrl+H')
    frame.items.extend((first, second))
    manager = KeyboardManager(frame, store=KeymapStore(path), registry=registry)
    manager.activate_loaded()
    assert manager.overrides == {} and manager.load_notice
    manager.register_menu_command(specs[0], first)
    assert manager.overrides == {}  # Do not apply a partial imported map.
    assert path.read_bytes() == raw
    manager.register_menu_command(specs[1], second)
    assert manager.overrides == expected
    assert not manager.load_notice
    assert frame.items[1].GetItemLabel() == 'Import books\tCtrl+K'
    assert first.GetItemLabel() == 'First extension\tCtrl+Shift+J'
    assert second.GetItemLabel() == 'Second extension'
    assert path.read_bytes() == raw


def test_native_registration_failure_rolls_back_registry_bindings_and_active_profile(manager):
    manager.apply_overrides({'library.import': 'Ctrl+K'})
    item = MenuItem(91, 'Extension action\tCtrl+J')
    manager.frame.items.append(item)
    before = snapshot(manager)
    item.fail_once = True
    with pytest.raises(RuntimeError, match='native menu failure'):
        manager.register_menu_command(CommandSpec('extension.action', 'Extension action',
                                                  default_shortcut='Ctrl+J'), item)
    assert snapshot(manager) == before


def test_failed_registration_cannot_partly_activate_a_pending_saved_map(tmp_path, registry):
    spec = CommandSpec('extension.action', 'Extension action', default_shortcut='Ctrl+J')
    path = tmp_path / 'keyboard-shortcuts.json'
    expected = {'library.import': 'Ctrl+K', 'extension.action': 'Ctrl+Shift+J'}
    KeymapStore(path).save(expected, CommandRegistry((*registry.commands(), spec)))
    frame = Frame()
    item = MenuItem(91, 'Extension action\tCtrl+J')
    frame.items.append(item)
    manager = KeyboardManager(frame, store=KeymapStore(path), registry=registry)
    manager.activate_loaded()
    before = snapshot(manager)
    item.fail_once = True
    with pytest.raises(RuntimeError):
        manager.register_menu_command(spec, item)
    assert snapshot(manager) == before
    assert manager.load_notice
    manager.register_menu_command(spec, item)
    assert manager.overrides == expected
    assert not manager.load_notice
    assert manager.store.path.read_bytes() == before[1]


def test_inserted_top_level_menu_does_not_change_enablement_of_registered_command(manager):
    item = MenuItem(91, 'Extension action\tCtrl+J')
    manager.frame.items.append(item)
    manager.register_menu_command(CommandSpec('extension.action', 'Extension action',
                                              default_shortcut='Ctrl+J'), item)
    assert manager.available('extension.action')
    manager.frame.menu = DynamicMenuBar([
        ('&New top menu', Menu(), True), ('&Library', Menu(manager.frame.items), False),
    ])
    manager.refresh()
    assert manager.bindings['extension.action'].top_index == 1
    assert not manager.available('extension.action')


def test_registration_queued_before_installation_preserves_saved_module_profile(tmp_path, monkeypatch):
    from carr_library.ui.builtin_keyboard_commands import builtin_commands
    frame = Frame()
    frame.library = SimpleNamespace(root=tmp_path)
    frame.menu = DynamicMenuBar([('&Library', Menu(frame.items), True)])
    frame.Bind = lambda *args, **kwargs: None
    item = MenuItem(91, 'Extension action\tCtrl+J')
    frame.items.append(item)
    spec = CommandSpec('extension.action', 'Extension action', default_shortcut='Ctrl+J')
    path = tmp_path / 'keyboard-shortcuts.json'
    expected = {'extension.action': 'Ctrl+Shift+J', 'library.import': 'Ctrl+K'}
    KeymapStore(path).save(expected, CommandRegistry((*builtin_commands(), spec)))
    raw = path.read_bytes()
    register_menu_command(frame, spec, item)
    with pytest.raises(ValueError, match='already waiting'):
        register_menu_command(frame, spec, item)
    fake_wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object(),
                              CallAfter=lambda callback, *args: callback(*args))
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    manager = install_keyboard_settings(frame)
    assert manager.overrides == expected
    assert manager.available('extension.action')
    assert item.GetItemLabel() == 'Extension action\tCtrl+Shift+J'
    assert path.read_bytes() == raw
    assert not frame._pending_keyboard_commands
