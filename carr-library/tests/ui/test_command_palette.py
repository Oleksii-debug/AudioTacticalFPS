"""Command search and native-menu dispatch boundaries, without an NVDA claim."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.keybindings import Command
from carr_library.ui.command_palette import (CommandChoice, CommandPalette,
    CommandPaletteActions, MAX_QUERY, PALETTE_IDENTITY, show_command_palette)
from carr_library.ui.productivity import KeyboardActions, MenuBinding, collect_commands, install_productivity_actions


class Item:
    next_id = 100

    def __init__(self, label, child=None):
        self.label, self.child, self.enabled = label, child, True
        self.identity = Item.next_id
        Item.next_id += 1

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
def keyboard(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        EVT_MENU=SimpleNamespace(typeId=7), CommandEvent=NativeEvent, ID_OK=1, ID_CANCEL=2,
        WXK_RETURN=13, WXK_NUMPAD_ENTER=313, ID_ANY=-1, Menu=Menu))
    save, archive = Item('&Save\tCtrl+S'), Item('&Export bookmarks\tCtrl+Shift+B')
    nested = Item('Book tools', Menu([archive]))
    bar = MenuBar([('&File', Menu([save, Item(None), nested])),
                   ('&Options', Menu([Item('&Find a command...\tCtrl+Shift+P')])),
                   ('&Help', Menu([Item('Українська довідка')]))])
    frame = SimpleNamespace(menu_bar=bar, library=SimpleNamespace(root=tmp_path), active=('library', 'topic'),
                            working=False, busy=False, _closing=False, report=Mock(),
                            IsBeingDeleted=lambda: False, Bind=Mock())
    frame.handler = SimpleNamespace(ProcessEvent=Mock(return_value=True))
    frame.GetEventHandler = lambda: frame.handler
    frame.GetMenuBar = lambda: bar
    action = KeyboardActions(frame, collect_commands(bar))
    frame.keyboard_actions = action
    return action


def test_filter_uses_menu_path_unicode_words_and_current_native_shortcut(keyboard):
    palette = CommandPalette(keyboard)
    export, = palette.matching('book tools export')
    assert export.binding.command.identity == 'File/Book tools/Export bookmarks'
    assert export.shortcut == 'Ctrl+Shift+B'
    assert all(row.binding.command.identity != PALETTE_IDENTITY for row in palette.matching(''))
    assert palette.matching('УКРАЇНСЬКА')[0].binding.command.label == 'Help — Українська довідка'
    keyboard._apply({'File/Save': 'Ctrl+Alt+S'})
    save, = palette.matching('ctrl+alt+s')
    assert save.label == 'File — Save; Ctrl+Alt+S'
    assert palette.matching('File/Save ctrl+s') == ()
    assert palette.matching('__import__("os")') == ()
    with pytest.raises(ValueError, match='characters'):
        palette.matching('a' * (MAX_QUERY + 1))


def test_dispatch_reuses_exact_menu_id_and_original_frame_handler(keyboard):
    palette = CommandPalette(keyboard)
    choice, = palette.matching('File/Save')
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


@pytest.mark.parametrize('change', ['library', 'topic', 'registry', 'menu_bar', 'detached_bar', 'working', 'busy', 'closing', 'destroyed'])
def test_changed_scope_or_inactive_frame_rejects_previously_selected_command(keyboard, change):
    palette = CommandPalette(keyboard)
    selected, = palette.matching('File/Save')
    frame = keyboard.frame
    if change == 'library': frame.library = object()
    elif change == 'topic': frame.active = ('library', 'another topic')
    elif change == 'registry': frame.keyboard_actions = object()
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
    selected, = palette.matching('File/Save')
    menu = keyboard.frame.menu_bar.GetMenu(0)
    if change == 'detached':
        menu.items.remove(selected.binding.item)
    elif change == 'replaced':
        replacement = Item(selected.binding.item.label)
        replacement.identity = selected.menu_id
        menu.items[0] = replacement
    elif change == 'id_changed':
        selected.binding.item.identity += 1
    else:
        keyboard.bindings = tuple(MenuBinding(row.command, row.item, row.label) for row in keyboard.bindings)
    with pytest.raises(ValueError):
        palette.dispatch(selected)
    keyboard.frame.handler.ProcessEvent.assert_not_called()


def test_forged_selection_and_palette_recursion_cannot_dispatch(keyboard):
    palette = CommandPalette(keyboard)
    fake = MenuBinding(Command('File/Save', 'File — Save'), Item('Save'), 'Save')
    recursive = next(binding for binding in keyboard.bindings if binding.command.identity == PALETTE_IDENTITY)
    for selected in (None, 'File/Save', CommandChoice(fake, fake.item.GetId(), '', True),
                     CommandChoice(recursive, recursive.item.GetId(), '', True)):
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
    selected = dialog.rows[1].binding
    dialog.filter.SetValue('file')
    dialog.refresh()
    assert dialog.rows[dialog.commands.GetSelection()].binding is selected
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
    dialog.filter.SetValue('File/Save')
    dialog.refresh()
    enter = SimpleNamespace(GetKeyCode=lambda: 13, Skip=Mock())
    dialog.list_key(enter)
    dialog.EndModal.assert_called_once_with(1)
    assert dialog.result.binding.command.identity == 'File/Save'
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


def test_installer_registers_palette_in_the_same_shortcut_inventory(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object()))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), Bind=Mock(),
                            menu_bar=MenuBar([('&Library', Menu([Item('Import books\tCtrl+I')]))]))
    keyboard = install_productivity_actions(frame)
    palette, = [binding for binding in keyboard.bindings if binding.command.identity == PALETTE_IDENTITY]
    assert palette.command.shortcut == 'Ctrl+Shift+P'
    assert palette.item.GetItemLabel().endswith('\tCtrl+Shift+P')
    assert keyboard is frame.keyboard_actions and install_productivity_actions(frame) is keyboard
    assert sum(binding.command.shortcut == 'Ctrl+Shift+P' for binding in keyboard.bindings) == 1


def test_palette_default_cannot_silently_reassign_an_existing_shortcut(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object()))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), Bind=Mock(),
                            menu_bar=MenuBar([('&Library', Menu([Item('Existing command\tCtrl+Shift+P')]))]))
    with pytest.raises(ValueError, match='already assigned'):
        install_productivity_actions(frame)
