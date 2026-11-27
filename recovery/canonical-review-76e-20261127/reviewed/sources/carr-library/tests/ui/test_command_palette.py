"""Command search and native-menu dispatch boundaries, without an NVDA claim."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.ui.command_palette import (CommandChoice, CommandPalette,
    CommandPaletteActions, MAX_QUERY, PALETTE_IDENTITY, PALETTE_SHORTCUT,
    show_command_palette, install_command_palette)
from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.command_registry import CommandRegistry, CommandSpec
from carr_library.ui.keyboard_settings import KeyboardManager, install_keyboard_settings


class Item:
    next_id = 100

    def __init__(self, label, child=None):
        self.label, self.child, self.enabled = label, child, True
        self.identity = Item.next_id
        Item.next_id += 1

    def __bool__(self): return not getattr(self, 'deleted', False)
    def IsSeparator(self): return self.label is None
    def GetItemLabel(self): return self.label
    def SetItemLabel(self, label): self.label = label
    def GetSubMenu(self): return self.child
    def GetId(self): return self.identity
    def IsEnabled(self): return self.enabled


class Menu:
    def __init__(self, items=()): self.items = list(items)
    def GetMenuItems(self): return self.items
    def Append(self, identity, label):
        item = Item(label)
        self.items.append(item)
        return item
    def AppendSeparator(self): self.items.append(Item(None))


class MenuBar:
    def __init__(self, menus):
        self.menus = list(menus)
        self.disabled = set()

    def GetMenuCount(self): return len(self.menus)
    def GetMenu(self, index): return self.menus[index][1]
    def GetMenuLabel(self, index): return self.menus[index][0]
    def IsEnabledTop(self, index): return index not in self.disabled
    def FindMenu(self, label):
        return next((i for i, (name, _) in enumerate(self.menus) if name.replace('&', '') == label), -1)
    def Insert(self, index, menu, label): self.menus.insert(index, (label, menu))
    def Append(self, menu, label): self.menus.append((label, menu))


class NativeEvent:
    def __init__(self, kind, identity): self.kind, self.identity = kind, identity
    def SetEventObject(self, value): self.source = value


@pytest.fixture
def wx(monkeypatch):
    module = SimpleNamespace(
        EVT_MENU=SimpleNamespace(typeId=7), CommandEvent=NativeEvent, ID_OK=1, ID_CANCEL=2,
        WXK_RETURN=13, WXK_NUMPAD_ENTER=313, ID_ANY=-1, Menu=Menu,
        ACCEL_CTRL=1, ACCEL_ALT=2, ACCEL_SHIFT=4, AcceleratorTable=lambda rows: tuple(rows),
        **{'WXK_F' + str(index): 1000 + index for index in range(1, 25)})
    module.CallAfter = lambda callback, *args: callback(*args)
    monkeypatch.setitem(sys.modules, 'wx', module)
    # Key parsing is an explicit headless double; native dispatch has its own probe.
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda key, identity: (0, key, int(identity)))
    return module


def make_frame(root, bar):
    root.mkdir(exist_ok=True, parents=True)
    frame = SimpleNamespace(menu_bar=bar, library=SimpleNamespace(root=root), active=('library', 'topic'),
                            working=False, busy=False, _closing=False, report=Mock(),
                            IsBeingDeleted=lambda: False, IsEnabled=lambda: True,
                            Bind=Mock(), choice=Control(), status=Control())
    frame.handler = SimpleNamespace(ProcessEvent=Mock(return_value=True))
    frame.GetEventHandler = lambda: frame.handler
    frame.GetMenuBar = lambda: bar
    frame.SetAcceleratorTable = lambda rows: setattr(frame, 'accelerators', rows)
    return frame


@pytest.fixture
def keyboard(tmp_path, wx):
    save, archive = Item('&Save\tCtrl+S'), Item('&Export bookmarks\tCtrl+Shift+B')
    nested = Item('Book tools', Menu([archive]))
    bar = MenuBar([('&File', Menu([save, Item(None), nested])),
                   ('&Help', Menu([Item('&Find a command...\tCtrl+Shift+P'), Item('Українська довідка')]))])
    frame = make_frame(tmp_path, bar)
    action = KeyboardManager(frame, store=KeymapStore(tmp_path / 'keyboard-shortcuts.json'),
                             registry=CommandRegistry())
    frame.keyboard_commands = action
    for spec, item in (
            (CommandSpec('file.save', 'Save', default_shortcut='Ctrl+S'), save),
            (CommandSpec('file.export_bookmarks', 'Export bookmarks', default_shortcut='Ctrl+Shift+B'), archive),
            (CommandSpec(PALETTE_IDENTITY, 'Find a command', default_shortcut=PALETTE_SHORTCUT), bar.GetMenu(1).items[0]),
            (CommandSpec('help.ukrainian', 'Українська довідка'), bar.GetMenu(1).items[1])):
        action.register_menu_command(spec, item)
    return action


def test_shortcut_filter_distinguishes_save_from_save_all(keyboard):
    item = Item('Save &all\tCtrl+Shift+S')
    keyboard.frame.menu_bar.GetMenu(0).items.append(item)
    keyboard.register_menu_command(
        CommandSpec('file.save_all', 'Save all', default_shortcut='Ctrl+Shift+S'), item)
    palette = CommandPalette(keyboard)
    save, = palette.matching('file/save Ctrl+S')
    assert save.command.command_id == 'file.save'
    save_all, = palette.matching('file/save Ctrl+Shift+S')
    assert save_all.command.command_id == 'file.save_all'
    keyboard.apply_overrides({'file.save': 'Ctrl+Alt+S'})
    assert palette.matching('file/save Ctrl+S') == ()
    assert palette.matching('file/save Ctrl+Shift+S')[0].command.command_id == 'file.save_all'


def test_filter_uses_menu_path_unicode_words_and_current_native_shortcut(keyboard):
    palette = CommandPalette(keyboard)
    export, = palette.matching('book tools export')
    assert export.command.command_id == 'file.export_bookmarks'
    assert export.shortcut == 'Ctrl+Shift+B'
    assert all(row.command.command_id != PALETTE_IDENTITY for row in palette.matching(''))
    assert palette.matching('УКРАЇНСЬКА')[0].title == 'Help / Українська довідка'
    keyboard.apply_overrides({'file.save': 'Ctrl+Alt+S'})
    save, = palette.matching('ctrl+alt+s')
    assert save.label == 'File / Save; Ctrl+Alt+S; available now'
    assert palette.matching('File/Save ctrl+s') == ()
    assert palette.matching('__import__("os")') == ()
    with pytest.raises(ValueError, match='characters'):
        palette.matching('a' * (MAX_QUERY + 1))


def test_dispatch_reuses_exact_menu_id_and_original_frame_handler(keyboard):
    palette = CommandPalette(keyboard)
    choice, = palette.matching('file/save')
    assert palette.dispatch(choice)
    event, = keyboard.frame.handler.ProcessEvent.call_args.args
    assert (event.kind, event.identity, event.source) == (7, choice.binding.item.GetId(), keyboard.frame)


@pytest.mark.parametrize('disabled', ['leaf', 'parent', 'top'])
def test_leaf_ancestor_and_top_menu_disabling_prevent_dispatch(keyboard, disabled):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('Export bookmarks')
    if disabled == 'leaf':
        selected.binding.item.enabled = False
    elif disabled == 'parent':
        keyboard.frame.menu_bar.GetMenu(0).items[-1].enabled = False
    else:
        keyboard.frame.menu_bar.disabled.add(0)
    current, = palette.matching('Export bookmarks')
    assert not current.enabled and 'unavailable now' in current.label
    with pytest.raises(ValueError, match='unavailable now'):
        palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_not_called()


@pytest.mark.parametrize('change', ['library', 'root', 'keyboard_root', 'topic', 'registry',
                                  'menu_bar', 'detached_bar', 'working', 'busy', 'closing', 'destroyed'])
def test_changed_scope_or_inactive_frame_rejects_previously_selected_command(keyboard, change):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('file/save')
    frame = keyboard.frame
    if change == 'library': frame.library = object()
    elif change == 'root': frame.library.root = frame.library.root / 'changed'
    elif change == 'keyboard_root': keyboard.store.path = keyboard.store.path.parent / 'changed' / keyboard.store.path.name
    elif change == 'topic': frame.active = ('library', 'another topic')
    elif change == 'registry': frame.keyboard_commands = object()
    elif change == 'menu_bar': frame.menu_bar = object()
    elif change == 'detached_bar': frame.GetMenuBar = lambda: None
    elif change == 'closing': frame._closing = True
    elif change == 'destroyed': frame.IsBeingDeleted = lambda: True
    else: setattr(frame, change, True)
    with pytest.raises(ValueError):
        palette.dispatch(selected)
    frame.handler.ProcessEvent.assert_not_called()


@pytest.mark.parametrize('change', ['detached', 'replaced', 'id_changed', 'unregistered'])
def test_stale_item_or_registry_binding_is_not_resolved_by_reused_label_or_id(keyboard, change):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('file/save')
    menu = keyboard.frame.menu_bar.GetMenu(0)
    if change == 'detached':
        menu.items.remove(selected.binding.item)
    elif change == 'replaced':
        replacement = Item(selected.binding.item.label)
        # A replacement native menu item deletes the old retained native peer;
        # merely creating a second wx wrapper does not change this flag.
        selected.binding.item.deleted = True
        replacement.identity = selected.menu_id
        menu.items[0] = replacement
    elif change == 'id_changed':
        selected.binding.item.identity += 1
    else:
        keyboard.registry = CommandRegistry(replace(row) for row in keyboard.registry.commands())
    with pytest.raises(ValueError):
        palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_forged_selection_and_palette_recursion_cannot_dispatch(keyboard):
    palette = CommandPalette(keyboard)
    item = Item('Save')
    fake = CommandSpec('file.save', 'Save', default_shortcut='Ctrl+S')
    recursive = keyboard.registry.get(PALETTE_IDENTITY)
    for selected in (None, 'file/save', CommandChoice(fake, item.GetId(), '', True),
                     CommandChoice(recursive, keyboard.bindings[PALETTE_IDENTITY].item.GetId(), '', True)):
        with pytest.raises(ValueError):
            palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_not_called()


class Control:
    def __init__(self, value=''):
        self.value, self.selection, self.items = value, -1, []
        self.enabled, self.focused = True, False
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, index): self.selection = index
    def SetItems(self, items): self.items, self.selection = list(items), -1
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def SetFocus(self): self.focused = True


@pytest.fixture
def dialog(keyboard):
    view = CommandPaletteActions()
    view.controller = CommandPalette(keyboard)
    view.filter, view.commands, view.status, view.run = Control(), Control(), Control(), Control()
    view.rows, view.result = (), None
    view.EndModal = Mock()
    view.refresh()
    return view


def test_filter_updates_native_list_and_retains_selection_without_stealing_focus(dialog):
    dialog.commands.SetSelection(1)
    selected = dialog.rows[1].command
    dialog.filter.SetValue('file')
    dialog.refresh()
    assert dialog.rows[dialog.commands.GetSelection()].command is selected
    assert 'matching commands' in dialog.status.value and not dialog.status.focused
    dialog.filter.SetValue('missing command')
    dialog.refresh()
    assert not dialog.run.enabled and dialog.commands.items == []
    dialog.accept()
    assert dialog.status.focused and 'Select' in dialog.status.value
    dialog.EndModal.assert_not_called()


def test_disabled_or_newly_stale_choice_keeps_palette_open(dialog, keyboard):
    dialog.filter.SetValue('save')
    dialog.refresh()
    selected = dialog.rows[0]
    selected.binding.item.enabled = False
    dialog.accept()
    assert not dialog.run.enabled and 'unavailable now' in dialog.status.value
    assert dialog.result is None
    dialog.EndModal.assert_not_called()
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_enter_selects_without_dispatch_until_modal_adapter_closes(dialog, keyboard):
    dialog.filter.SetValue('file/save')
    dialog.refresh()
    enter = SimpleNamespace(GetKeyCode=lambda: 13, Skip=Mock())
    dialog.list_key(enter)
    dialog.EndModal.assert_called_once_with(1)
    assert dialog.result.command.command_id == 'file.save'
    keyboard.frame.handler.ProcessEvent.assert_not_called()
    enter.Skip.assert_not_called()
    arrow = SimpleNamespace(GetKeyCode=lambda: 38, Skip=Mock())
    dialog.list_key(arrow)
    arrow.Skip.assert_called_once()


@pytest.mark.parametrize('result', [1, 2])
def test_modal_adapter_destroys_palette_before_original_command_dispatch(keyboard, monkeypatch, result):
    controller = CommandPalette(keyboard)
    selected, = controller.matching('save')
    order = []
    dialog = SimpleNamespace(controller=controller, result=selected,
        ShowModal=lambda: result, Destroy=lambda: order.append('destroy'))
    monkeypatch.setattr('carr_library.ui.command_palette.create_command_palette', lambda _: dialog)
    keyboard.frame.handler.ProcessEvent.side_effect = lambda event: order.append('dispatch') or True
    assert show_command_palette(keyboard) is (result == 1)
    assert order == (['destroy', 'dispatch'] if result == 1 else ['destroy'])


def test_modal_adapter_checks_enablement_again_after_close(keyboard, monkeypatch):
    controller = CommandPalette(keyboard)
    selected, = controller.matching('save')
    dialog = SimpleNamespace(controller=controller, result=selected, ShowModal=lambda: 1,
                             Destroy=lambda: setattr(selected.binding.item, 'enabled', False))
    monkeypatch.setattr('carr_library.ui.command_palette.create_command_palette', lambda _: dialog)
    assert not show_command_palette(keyboard)
    keyboard.frame.handler.ProcessEvent.assert_not_called()
    assert 'unavailable now' in keyboard.frame.report.call_args.args[0]


@pytest.mark.parametrize('result', [1, 2])
def test_originating_control_regains_focus_before_dispatch_or_on_cancel(keyboard, monkeypatch, wx, result):
    controller = CommandPalette(keyboard)
    selected, = controller.matching('save')
    order = []
    previous = SimpleNamespace(IsBeingDeleted=lambda: False, IsEnabled=lambda: True,
        IsShown=lambda: True, SetFocus=lambda: order.append('focus'))
    wx.Window = SimpleNamespace(FindFocus=lambda: previous)
    dialog = SimpleNamespace(controller=controller, result=selected, ShowModal=lambda: result,
                             Destroy=lambda: order.append('destroy'))
    monkeypatch.setattr('carr_library.ui.command_palette.create_command_palette', lambda _: dialog)
    keyboard.frame.handler.ProcessEvent.side_effect = lambda event: order.append('handler focus') or True
    assert show_command_palette(keyboard) is (result == 1)
    assert order == (['destroy', 'focus', 'handler focus'] if result == 1 else ['destroy', 'focus'])


@pytest.mark.parametrize('changed', ['deleted', 'disabled', 'hidden', 'scope', 'dead_control'])
def test_cancel_does_not_refocus_a_stale_or_unavailable_control(keyboard, monkeypatch, wx, changed):
    controller = CommandPalette(keyboard)
    previous = SimpleNamespace(IsBeingDeleted=lambda: changed == 'deleted',
        IsEnabled=lambda: changed != 'disabled', IsShown=lambda: changed != 'hidden', SetFocus=Mock())
    if changed == 'dead_control':
        previous.IsBeingDeleted = Mock(side_effect=RuntimeError('native control was deleted'))
    wx.Window = SimpleNamespace(FindFocus=lambda: previous)
    def destroy():
        if changed == 'scope':
            keyboard.frame.active = ('different', 'topic')
    dialog = SimpleNamespace(controller=controller, result=None, ShowModal=lambda: 2, Destroy=destroy)
    monkeypatch.setattr('carr_library.ui.command_palette.create_command_palette', lambda _: dialog)
    assert not show_command_palette(keyboard)
    previous.SetFocus.assert_not_called()
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_installer_registers_palette_in_the_same_shortcut_inventory(tmp_path, wx):
    frame = make_frame(tmp_path, MenuBar([('&Library', Menu([Item('Import books\tCtrl+I')]))]))
    installed = install_command_palette(frame)
    keyboard = install_keyboard_settings(frame)
    palette = keyboard.registry.get(PALETTE_IDENTITY)
    item = keyboard.bindings[PALETTE_IDENTITY].item
    assert palette.default_shortcut == 'Ctrl+Shift+P'
    assert item.GetItemLabel().endswith('\tCtrl+Shift+P')
    assert keyboard is frame.keyboard_commands and install_keyboard_settings(frame) is keyboard
    assert install_command_palette(frame) is installed is item
    assert sum(command.default_shortcut == 'Ctrl+Shift+P'
               for command in keyboard.registry.commands('main')) == 1


def test_palette_default_cannot_silently_reassign_an_existing_shortcut(tmp_path, wx):
    existing = Item('Existing command\tCtrl+Shift+P')
    frame = make_frame(tmp_path, MenuBar([('&Library', Menu([existing]))]))
    item = install_command_palette(frame)
    keyboard = install_keyboard_settings(frame)
    assert existing.GetItemLabel() == 'Existing command\tCtrl+Shift+P'
    assert keyboard.shortcut(PALETTE_IDENTITY) == ''
    assert '\t' not in item.GetItemLabel() and 'unassigned' in keyboard.load_notice
    assert not (tmp_path / 'keyboard-shortcuts.json').exists()


def test_existing_saved_shortcut_keeps_ownership_when_palette_default_is_added(tmp_path, wx):
    from carr_library.durable_state import atomic_json
    path = tmp_path / 'state' / 'ui' / 'shortcuts.json'
    atomic_json(path, {'schema': 1,
        'overrides': {'library/import books': 'Ctrl+Shift+P'},
        'originals': {'library/import books': 'Ctrl+I'}})
    before = path.read_bytes()
    upgraded = make_frame(tmp_path, MenuBar([('&Library', Menu([Item('Import books\tCtrl+I')]))]))
    item = install_command_palette(upgraded)
    current = install_keyboard_settings(upgraded)
    assert current.overrides['library.import'] == 'Ctrl+Shift+P'
    assert current.overrides[PALETTE_IDENTITY] == ''
    assert item.GetItemLabel().replace('&', '') == 'Find a command...'
    assert 'preserve your saved choices' in current.load_notice
    assert current.store.legacy and current.store.path == path
    assert path.read_bytes() == before
    assert not (tmp_path / 'keyboard-shortcuts.json').exists()
    result, = CommandPalette(current).matching('ctrl+shift+p')
    assert result.command.command_id == 'library.import'


def test_late_palette_install_cannot_leave_an_uncaptured_native_action(tmp_path, wx):
    frame = make_frame(tmp_path, MenuBar([('&Library', Menu([Item('Import books\tCtrl+I')]))]))
    keyboard = install_keyboard_settings(frame)
    before = keyboard.registry.commands(), frame.menu_bar.GetMenuCount()
    item = install_command_palette(frame)
    assert keyboard.registry.commands() == before[0] and frame.menu_bar.GetMenuCount() == before[1]
    assert frame.command_palette_item is item is keyboard.bindings[PALETTE_IDENTITY].item
    assert sum(command.command_id == PALETTE_IDENTITY
               for command in keyboard.registry.commands('main')) == 1


@pytest.mark.parametrize('change', ['duplicate_id', 'renamed_item', 'moved_item', 'renamed_parent'])
def test_current_menu_identity_is_verified_again_before_existing_handler_runs(keyboard, change):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('file/save')
    bar = keyboard.frame.menu_bar
    if change == 'duplicate_id':
        extra = Item('Unrelated action')
        extra.identity = selected.menu_id
        bar.GetMenu(1).items.append(extra)
    elif change == 'renamed_item':
        selected.binding.item.label = 'Different action'
    elif change == 'moved_item':
        bar.GetMenu(0).items.remove(selected.binding.item)
        bar.GetMenu(1).items.append(selected.binding.item)
    else:
        bar.menus[0] = ('&Different menu', bar.GetMenu(0))
    with pytest.raises(ValueError, match='IDs|changed'):
        palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_display_label_can_change_when_registry_explicitly_keeps_the_same_action(keyboard):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('file/save')
    selected.binding.item.carr_command_name = 'save'
    selected.binding.item.label = 'Save a verified copy\tCtrl+S'
    assert palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_called_once()


def test_destroyed_native_wrapper_is_not_touched_to_report_palette_failure(keyboard):
    class DestroyedFrame(SimpleNamespace):
        def __bool__(self):
            return False
    # wx wrappers are false after native destruction. Their remaining Python
    # attributes can exist while every native window method already raises.
    frame = DestroyedFrame(**vars(keyboard.frame))
    frame.IsBeingDeleted = Mock(side_effect=RuntimeError('native window was deleted'))
    keyboard.frame = frame
    assert not show_command_palette(keyboard)
    frame.IsBeingDeleted.assert_not_called()
    frame.report.assert_not_called()
    frame.handler.ProcessEvent.assert_not_called()


def test_palette_ignores_child_destroy_and_detaches_on_cached_native_id(dialog, keyboard):
    dialog._native_id = 700
    skipped = []
    child = SimpleNamespace(GetId=lambda: 701, GetEventObject=lambda: dialog,
                            Skip=lambda: skipped.append('child'))
    dialog.on_destroy(child)
    assert dialog.alive
    owner = SimpleNamespace(GetId=lambda: 700, GetEventObject=lambda: object(),
                            Skip=lambda: skipped.append('owner'))
    dialog.on_destroy(owner)
    assert not dialog.alive and dialog.result is None and skipped == ['child', 'owner']
    broken = Mock(side_effect=AssertionError('Deleted native control was accessed'))
    for control in (dialog.filter, dialog.commands, dialog.status, dialog.run):
        for name in ('GetValue', 'GetSelection', 'SetItems', 'SetSelection',
                     'SetValue', 'SetFocus', 'Enable', 'Disable'):
            setattr(control, name, broken)
    dialog.refresh()
    dialog.selection_changed()
    dialog.accept()
    dialog.report('Late native result', focus=True)
    dialog.list_key(SimpleNamespace(GetKeyCode=broken, Skip=broken))
    keyboard.frame.handler.ProcessEvent.assert_not_called()
    dialog.EndModal.assert_not_called()


def test_queued_palette_ready_callback_is_discarded_after_native_detachment(keyboard, monkeypatch, wx):
    controller = CommandPalette(keyboard)
    queued, called = [], []
    class Window:
        alive = True
        result = None
        def __init__(self):
            self.controller = controller
        def __bool__(self):
            return self.alive
        def IsBeingDeleted(self):
            assert self.alive, 'A deleted native wrapper was queried.'
            return False
        def ShowModal(self):
            self.alive = False
            return wx.ID_CANCEL
        def Destroy(self):
            raise AssertionError('A deleted wrapper was destroyed twice.')
    view = Window()
    wx.CallAfter = lambda callback: queued.append(callback)
    monkeypatch.setattr('carr_library.ui.command_palette.create_command_palette', lambda _: view)
    assert not show_command_palette(keyboard, on_ready=lambda value: called.append(value))
    assert len(queued) == 1
    queued.pop()()
    assert not called
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_current_frame_facade_and_palette_share_the_installed_manager(tmp_path, wx):
    from carr_library.ui.keyboard_settings import KeyboardSettings
    frame = make_frame(tmp_path, MenuBar([('&File', Menu([Item('Save\tCtrl+S')]))]))
    manager = install_keyboard_settings(frame)
    facade = frame.keyboard_settings
    assert isinstance(facade, KeyboardSettings) and facade.manager is manager
    manager_from_palette = CommandPalette(facade).keyboard
    assert manager_from_palette is manager
    facade.apply({'file/save': 'Ctrl+Shift+S'})
    choice, = CommandPalette(facade).matching('file/save Ctrl+Shift+S')
    assert choice.binding.item.GetId() == facade.commands[0].menu_id
    assert manager.shortcut(choice.command.command_id) == 'Ctrl+Shift+S'
    assert facade.manager.registry is manager.registry
