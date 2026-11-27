"""Controller transactions with real keymap files and explicit wx/menu doubles.

Actual OS key dispatch is covered by the separate native keyboard probe. These
tests exercise persistence failure, rejected imports and late module wiring.
"""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

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
    dialog.original_registry = manager.registry
    dialog.original_root = manager.store.path.parent
    dialog.closed = dialog._destroy_requested = False
    dialog._native_id = 700
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


def export_form(manager, tmp_path):
    private = tmp_path / 'private-keymaps'
    private.mkdir()
    manager.change_root(private)
    destination = tmp_path / 'exported-keymap.json'
    dialog = dialog_double(manager)
    dialog.draft['library.import'] = 'Ctrl+K'
    native = file_dialog_wx(destination)
    native.FD_SAVE, native.FD_OVERWRITE_PROMPT = 8, 16
    native.YES_NO, native.NO_DEFAULT, native.ICON_QUESTION = 32, 64, 128
    native.YES, native.NO = 256, 512
    native.MessageBox = Mock(return_value=native.YES)
    dialog.wx = native
    return dialog, destination


@pytest.mark.parametrize('existing', [False, True])
def test_keymap_export_preserves_other_work_written_during_validation(
        manager, tmp_path, monkeypatch, existing):
    dialog, destination = export_form(manager, tmp_path)
    if existing:
        destination.write_bytes(b'Previously approved export')
    before = snapshot(manager)
    validate = manager.validate
    independent = b'Independent work saved while the draft was validated'

    def validate_then_external_write(draft):
        normalized = validate(draft)
        destination.write_bytes(independent)
        return normalized

    monkeypatch.setattr(manager, 'validate', validate_then_external_write)
    dialog.export_map()
    assert destination.read_bytes() == independent
    assert 'destination changed' in dialog.messages[-1]
    assert dialog.wx.MessageBox.call_count == int(existing)
    assert snapshot(manager) == before
    assert not list(tmp_path.glob('.research-export-*'))


def test_declined_keymap_replacement_never_validates_or_writes(
        manager, tmp_path, monkeypatch):
    dialog, destination = export_form(manager, tmp_path)
    destination.write_bytes(b'Keep existing keymap')
    dialog.wx.MessageBox.return_value = dialog.wx.NO
    validate = Mock(side_effect=AssertionError('Declined export must not validate or write'))
    monkeypatch.setattr(manager, 'validate', validate)
    dialog.export_map()
    validate.assert_not_called()
    assert destination.read_bytes() == b'Keep existing keymap'
    assert 'cancelled' in dialog.messages[-1].lower()


@pytest.mark.parametrize('existing', [False, True])
def test_confirmed_keymap_export_keeps_the_live_shortcuts_unchanged(manager, tmp_path, existing):
    import json

    dialog, destination = export_form(manager, tmp_path)
    if existing:
        destination.write_bytes(b'Previously approved export')
    before = snapshot(manager)
    dialog.export_map()
    assert json.loads(destination.read_text()) == {'version': 1, 'bindings': dialog.draft}
    assert snapshot(manager) == before
    assert dialog.wx.MessageBox.call_count == int(existing)
    assert 'was exported' in dialog.messages[-1]


def earlier_keymap(manager, *, overrides=None, originals=None, encoding='utf-8'):
    import json
    from carr_library.durable_state import atomic_json
    manager.frame.library = SimpleNamespace(root=manager.store.path.parent)
    path = manager.store.path.parent / 'state' / 'ui' / 'shortcuts.json'
    value = dict(schema=1,
        overrides={'library/new library': 'F9'} if overrides is None else overrides,
        originals={'library/new library': 'Ctrl+N'} if originals is None else originals)
    atomic_json(path, value)
    if encoding != 'utf-8':
        path.write_bytes(json.dumps(value, ensure_ascii=False).encode(encoding))
    return path


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-32'])
def test_earlier_keymap_is_reviewed_then_saved_only_through_canonical_writer(manager, encoding):
    import json
    path = earlier_keymap(manager, encoding=encoding)
    original = path.read_bytes()
    before = snapshot(manager)
    manager._load()
    assert manager.overrides == {} and 'earlier keyboard map' in manager.load_notice
    assert not manager.store.path.exists() and path.read_bytes() == original
    dialog = dialog_double(manager)
    dialog.rows = []
    dialog.wx = SimpleNamespace(ID_OK=1)
    dialog.dialog.EndModal = Mock()
    dialog.import_earlier_map()
    assert dialog.draft == {'library.new': 'F9'}
    assert snapshot(manager) == before
    assert path.read_bytes() == original and 'review draft only' in dialog.messages[-1]
    dialog.save()
    dialog.dialog.EndModal.assert_called_once_with(1)
    assert manager.overrides == {'library.new': 'F9'}
    assert json.loads(manager.store.path.read_bytes()) == {'version': 1, 'bindings': {'library.new': 'F9'}}
    assert path.read_bytes() == original


def test_earlier_explicit_choice_keeps_its_key_and_new_default_is_only_unassigned_in_draft(manager):
    path = earlier_keymap(manager, overrides={'library/new library': 'Ctrl+I'})
    original = path.read_bytes()
    before = snapshot(manager)
    dialog = dialog_double(manager)
    dialog.import_earlier_map()
    assert dialog.draft == {'library.new': 'Ctrl+I', 'library.import': ''}
    assert 'Import books' in dialog.messages[-1] and 'unassigned in this draft' in dialog.messages[-1]
    assert snapshot(manager) == before and path.read_bytes() == original


@pytest.mark.parametrize('case', [
    'unknown-action', 'ambiguous-action', 'changed-default', 'protected-key',
    'explicit-conflict', 'duplicate-json', 'hardlink', 'oversized', 'unsupported-old-key',
    'reused-native-id',
])
def test_earlier_map_rejection_preserves_every_draft_live_binding_and_file(manager, case):
    import json
    import os
    path = earlier_keymap(manager)
    value = json.loads(path.read_bytes())
    if case == 'unknown-action':
        value['overrides'], value['originals'] = {'missing/action': 'F9'}, {'missing/action': ''}
    elif case == 'ambiguous-action':
        manager.frame.items.append(MenuItem(13, 'New library'))
    elif case == 'reused-native-id':
        replacement = MenuItem(manager.frame.items[0].GetId(), 'New library')
        manager.frame.items[0] = replacement
    elif case == 'changed-default':
        value['originals']['library/new library'] = 'Ctrl+P'
    elif case == 'protected-key':
        value['overrides']['library/new library'] = 'Ctrl+C'
    elif case == 'explicit-conflict':
        value['overrides']['library/import books'] = 'F9'
        value['originals']['library/import books'] = 'Ctrl+I'
    elif case == 'unsupported-old-key':
        value['overrides']['library/new library'] = 'F24'
    raw = json.dumps(value).encode('utf-8')
    if case == 'duplicate-json':
        raw = b'{"schema":1,"schema":1,"overrides":{},"originals":{}}'
    elif case == 'oversized':
        raw = b' ' * (128 * 1024 + 1)
    path.write_bytes(raw)
    if case == 'hardlink':
        os.link(path, path.with_name('linked-copy.json'))
    before = snapshot(manager)
    dialog = dialog_double(manager)
    dialog.draft = {'reader.next': 'Ctrl+Shift+J'}
    previous_draft = dict(dialog.draft)
    dialog.import_earlier_map()
    assert dialog.draft == previous_draft
    assert snapshot(manager) == before and path.read_bytes() == raw
    assert 'not imported' in dialog.messages[-1]


def test_earlier_map_scope_change_does_not_read_or_write_another_library(manager, tmp_path):
    path = earlier_keymap(manager)
    raw = path.read_bytes()
    dialog = dialog_double(manager)
    dialog.draft = {'library.import': 'Ctrl+K'}
    manager.change_root(tmp_path / 'another-library')
    other = snapshot(manager)
    dialog.import_earlier_map()
    assert dialog.draft == {'library.import': 'Ctrl+K'}
    assert 'folder changed' in dialog.messages[-1]
    assert snapshot(manager) == other and path.read_bytes() == raw


def test_earlier_named_action_alias_remains_explicit_under_a_longer_visible_caption(manager):
    item = MenuItem(71, 'Publish synchronized EPUB3 / DAISY...')
    item.carr_command_name = 'publish synchronized epub3'
    manager.frame.items.append(item)
    manager.register_menu_command(
        CommandSpec('audio.publish', 'Publish synchronized EPUB3 and DAISY'), item)
    path = earlier_keymap(manager,
        overrides={'library/publish synchronized epub3': 'Ctrl+Shift+P'},
        originals={'library/publish synchronized epub3': ''})
    raw = path.read_bytes()
    before = snapshot(manager)
    dialog = dialog_double(manager)
    dialog.import_earlier_map()
    assert dialog.draft == {'audio.publish': 'Ctrl+Shift+P'}
    assert snapshot(manager) == before and path.read_bytes() == raw


def test_two_earlier_actions_cannot_silently_collapse_into_one_installed_command(manager):
    from carr_library.keyboard_preferences import import_legacy_keymap
    path = earlier_keymap(manager,
        overrides={'library/new library': 'F9', 'legacy/alias': 'F12'},
        originals={'library/new library': 'Ctrl+N', 'legacy/alias': 'Ctrl+N'})
    raw = path.read_bytes()
    before = snapshot(manager)
    with pytest.raises(KeymapError, match='same installed command'):
        import_legacy_keymap(path, manager.registry,
            {'library/new library': ['library.new'], 'legacy/alias': ['library.new']})
    assert snapshot(manager) == before and path.read_bytes() == raw


def test_earlier_help_palette_mapping_follows_its_explicit_canonical_identity(manager):
    item = MenuItem(81, 'Find a command...')
    manager.frame.items.append(item)
    manager.register_menu_command(
        CommandSpec('commands.find', 'Find a command', default_shortcut='Ctrl+Shift+P'), item)
    path = earlier_keymap(manager, overrides={'help/find a command': 'F9'},
                          originals={'help/find a command': 'Ctrl+Shift+P'})
    raw = path.read_bytes()
    before = snapshot(manager)
    dialog = dialog_double(manager)
    dialog.import_earlier_map()
    assert dialog.draft == {'commands.find': 'F9'}
    assert snapshot(manager) == before and path.read_bytes() == raw


def test_settings_rewrapped_self_destroy_drops_every_queued_action(manager, monkeypatch):
    dialog = dialog_double(manager)
    before, draft = snapshot(manager), dict(dialog.draft)
    skipped = []
    dialog.on_destroy(SimpleNamespace(GetId=lambda: 701, GetEventObject=lambda: dialog,
                                      Skip=lambda: skipped.append('child')))
    assert not dialog.closed
    dialog.on_destroy(SimpleNamespace(GetId=lambda: 700, GetEventObject=lambda: object(),
                                      Skip=lambda: skipped.append('owner')))
    assert dialog.closed and skipped == ['child', 'owner']
    forbidden = Mock(side_effect=AssertionError('A disposed settings owner touched native controls or storage.'))
    dialog.status = SimpleNamespace(ChangeValue=forbidden, SetFocus=forbidden)
    dialog._message = KeyboardSettingsDialog._message.__get__(dialog)
    dialog.rebuild = KeyboardSettingsDialog.rebuild.__get__(dialog)
    dialog.wx = SimpleNamespace(FileDialog=forbidden)
    monkeypatch.setattr(manager, 'validate', forbidden)
    monkeypatch.setattr(manager, 'apply_overrides', forbidden)
    monkeypatch.setattr(manager, 'earlier_keymap_draft', forbidden)
    for action in (dialog.rebuild, dialog.selected, dialog.assign, dialog.reset_selected,
                   dialog.reset_all, dialog.import_map, dialog.import_earlier_map,
                   dialog.export_map, dialog.save):
        action()
    dialog._message('Late result')
    assert not dialog.show()
    forbidden.assert_not_called()
    assert dialog.draft == draft and snapshot(manager) == before


@pytest.mark.parametrize('operation', ['import_map', 'export_map'])
@pytest.mark.parametrize('boundary', ['modal-return', 'chooser-destroy'])
@pytest.mark.parametrize('accepted', [False, True], ids=['cancel', 'accept'])
def test_settings_native_owner_loss_after_picker_cannot_read_write_or_apply(
        manager, tmp_path, monkeypatch, operation, boundary, accepted):
    dialog = dialog_double(manager)
    before, draft = snapshot(manager), dict(dialog.draft)
    path = tmp_path / 'untouched-keymap.json'
    path.write_bytes(b'Independent existing file')
    failures = []

    class Peer:
        alive = True
        def __bool__(self):
            return self.alive

    dialog.dialog = Peer()
    class Chooser(Peer):
        def detach(self):
            dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog._native_id,
                GetEventObject=lambda: object(), Skip=lambda: None))
            dialog.dialog.alive = self.alive = False

        def ShowModal(self):
            if boundary == 'modal-return':
                self.detach()
            return 1 if accepted else 0

        def GetPath(self):
            if not self:
                failures.append('GetPath')
            assert self
            return str(path)

        def Destroy(self):
            if not self:
                failures.append('Destroy')
            assert self
            if boundary == 'chooser-destroy':
                self.detach()
            else:
                self.alive = False

    chooser = Chooser()
    dialog.wx = SimpleNamespace(FileDialog=lambda *args, **kwargs: chooser,
        FD_OPEN=2, FD_FILE_MUST_EXIST=4, FD_SAVE=8, ID_OK=1)
    forbidden = Mock(side_effect=AssertionError('The picker outlived its owner.'))
    monkeypatch.setattr(manager, 'validate', forbidden)
    monkeypatch.setattr(KeymapStore, 'load', forbidden)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.write_export', forbidden)
    getattr(dialog, operation)()
    assert dialog.closed and not dialog.dialog and not chooser and not failures
    assert dialog.draft == draft and snapshot(manager) == before
    assert path.read_bytes() == b'Independent existing file'
    forbidden.assert_not_called()


def test_settings_export_confirmation_cannot_outlive_its_native_owner(manager, tmp_path, monkeypatch):
    dialog, destination = export_form(manager, tmp_path)
    destination.write_bytes(b'Preserve the approved destination')
    before, draft = snapshot(manager), dict(dialog.draft)
    def confirm(*args, **kwargs):
        dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog._native_id,
            GetEventObject=lambda: object(), Skip=lambda: None))
        return dialog.wx.YES
    dialog.wx.MessageBox.side_effect = confirm
    forbidden = Mock(side_effect=AssertionError('A destroyed owner cannot publish a keymap.'))
    monkeypatch.setattr(manager, 'validate', forbidden)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.write_export', forbidden)
    dialog.export_map()
    assert dialog.closed and dialog.draft == draft
    assert snapshot(manager) == before
    assert destination.read_bytes() == b'Preserve the approved destination'
    forbidden.assert_not_called()


def test_settings_show_does_not_destroy_an_already_deleted_native_wrapper(manager):
    dialog = dialog_double(manager)
    destroyed = []
    class Peer:
        alive = True
        def __bool__(self):
            return self.alive
        def ShowModal(self):
            dialog.on_destroy(SimpleNamespace(GetId=lambda: dialog._native_id,
                GetEventObject=lambda: object(), Skip=lambda: None))
            self.alive = False
            return 1
        def Destroy(self):
            destroyed.append(True)
            raise AssertionError('The native wrapper was already deleted.')
    dialog.dialog = Peer()
    dialog.wx = SimpleNamespace(ID_OK=1)
    assert not dialog.show() and dialog.closed
    assert not destroyed


def test_reader_command_chooser_discards_late_native_events_after_owner_destruction(manager, monkeypatch):
    from carr_library.ui.keyboard_settings import show_command_launcher

    controls, calls = [], []
    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent, self.handlers, self.value, self.selection = parent, {}, '', -1
            controls.append(self)
        def native(self):
            assert self.parent is None or not isinstance(self.parent, Window) or self.parent.alive, (
                'A queued callback touched a deleted chooser control.')
        def SetName(self, value): self.native()
        def Add(self, *args): self.native()
        def Set(self, values): self.native(); self.values = values
        def GetValue(self): self.native(); return self.value
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def Enable(self, value=True): self.native()
        def ChangeValue(self, value): self.native(); self.value = value
        def SetFocus(self): self.native()
        def Bind(self, event, handler): self.native(); self.handlers[event] = handler

    class Window(Control):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.alive = True
        def __bool__(self): return self.alive
        def native(self): assert self.alive, 'The native chooser was already destroyed.'
        def GetId(self): self.native(); return 902
        def SetSizer(self, layout): self.native()
        def SetEscapeId(self, identity): self.native()
        def EndModal(self, result): self.native(); calls.append('accepted')
        def ShowModal(self):
            self.handlers['destroy'](SimpleNamespace(GetId=lambda: 902,
                GetEventObject=lambda: object(), Skip=lambda: calls.append('destroy-event')))
            self.alive = False
            for control in controls:
                if control is not self:
                    for handler in control.handlers.values():
                        handler(None)
            return 1
        def Destroy(self):
            raise AssertionError('Native parent destruction already deleted the chooser.')

    wx = SimpleNamespace(Dialog=Window, BoxSizer=Control, StaticText=Control, TextCtrl=Control,
        ListBox=Control, Button=Control, Window=SimpleNamespace(FindFocus=lambda: None),
        EVT_TEXT='text', EVT_LISTBOX='list', EVT_LISTBOX_DCLICK='double',
        EVT_BUTTON='button', EVT_WINDOW_DESTROY='destroy', ID_OK=1, ID_CANCEL=2,
        VERTICAL=0, HORIZONTAL=0, ALL=0, EXPAND=0, LEFT=0, ALIGN_RIGHT=0, TE_READONLY=0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    invoked = Mock(side_effect=AssertionError('A deleted chooser cannot dispatch a selection.'))
    assert not show_command_launcher(SimpleNamespace(), manager, context='reader',
                                     available=lambda identity: True, invoke=invoked)
    invoked.assert_not_called()
    assert calls == ['destroy-event']


def test_settings_changed_scope_before_show_still_disposes_its_live_native_window(manager, tmp_path):
    dialog = dialog_double(manager)
    destroyed = []
    dialog.dialog = SimpleNamespace(
        ShowModal=lambda: pytest.fail('A stale keymap dialog cannot be shown.'),
        Destroy=lambda: destroyed.append(True))
    manager.change_root(tmp_path / 'changed-before-show')
    before = snapshot(manager)
    assert not dialog.show() and dialog.closed
    assert destroyed == [True] and snapshot(manager) == before
    assert 'folder changed' in dialog.messages[-1]
