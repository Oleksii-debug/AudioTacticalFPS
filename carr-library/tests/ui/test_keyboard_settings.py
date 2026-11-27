"""Preserved keyboard contracts through the canonical command manager.

Native key dispatch is qualified by the separate Windows probes. These tests
use explicit control doubles and real files; historical menu-name preferences
remain passive bytes rather than becoming a second command registry.
"""
from itertools import count
import json
import os
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.command_registry import CommandRegistry, CommandSpec, normalize_shortcut
from carr_library.ui.keyboard_settings import (
    KeyboardManager, KeyboardSettingsDialog, install_keyboard_settings,
)


_ids = count(200)


class Item:
    def __init__(self, label, identity=None, submenu=None):
        self.label, self.identity = label, next(_ids) if identity is None else identity
        self.submenu = submenu

    def IsSeparator(self): return self.label is None
    def GetItemLabel(self): return self.label
    def SetItemLabel(self, label): self.label = label
    def GetId(self): return self.identity
    def GetSubMenu(self): return self.submenu
    def IsEnabled(self): return True


class Menu:
    def __init__(self, *labels):
        self.items = [Item(label) for label in labels]

    def GetMenuItems(self): return self.items

    def Append(self, identity, label):
        item = Item(label, None if identity == -1 else identity)
        self.items.append(item)
        return item

    def AppendSeparator(self): self.items.append(Item(None))


class MenuBar:
    def __init__(self):
        self.menus = [('&Library', Menu('&New library\tCtrl+N', 'Import books\tCtrl+I'))]

    def GetMenuCount(self): return len(self.menus)
    def GetMenuLabel(self, index): return self.menus[index][0]
    def GetMenu(self, index): return self.menus[index][1]
    def IsEnabledTop(self, index): return True
    def Append(self, menu, label): self.menus.append((label, menu))


class Frame:
    def __init__(self, root):
        root.mkdir(exist_ok=True)
        self.library = SimpleNamespace(root=root)
        self.menu_bar, self.bindings, self.reports = MenuBar(), {}, []
        self._closing = False

    def GetMenuBar(self): return self.menu_bar
    def IsEnabled(self): return True
    def Bind(self, event, callback, item): self.bindings[item.GetId()] = callback
    def report(self, message): self.reports.append(message)


@pytest.fixture
def wx(monkeypatch):
    native = SimpleNamespace(
        ID_ANY=-1, ID_OK=1, ID_CANCEL=2, EVT_MENU='menu', Menu=Menu,
        CallAfter=lambda callback, *args: callback(*args),
    )
    monkeypatch.setitem(sys.modules, 'wx', native)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda shortcut, identity: (0, normalize_shortcut(shortcut), identity))
    return native


@pytest.fixture
def frame(tmp_path):
    return Frame(tmp_path / 'library')


def manager_for(frame):
    registry = CommandRegistry((
        CommandSpec('library.new', 'New library', default_shortcut='Ctrl+N'),
        CommandSpec('library.import', 'Import books', default_shortcut='Ctrl+I'),
        CommandSpec('reader.next', 'Next source location', 'reader', 'Ctrl+PageDown'),
    ))
    manager = KeyboardManager(frame, store=KeymapStore(frame.library.root / 'keyboard-shortcuts.json'),
                              registry=registry)
    manager.activate_loaded()
    return manager


@pytest.mark.parametrize(('raw', 'expected'), [
    ('ctrl+shift+k', 'Ctrl+Shift+K'), ('Shift + Control + alt + 9', 'Ctrl+Alt+Shift+9'),
    ('F9', 'F9'), ('ctrl+f12', 'Ctrl+F12'), ('', ''), ('   ', ''),
])
def test_shortcut_normalization(raw, expected):
    assert normalize_shortcut(raw) == expected


@pytest.mark.parametrize('raw', [
    'Insert', 'Ctrl+Insert', 'Shift+Insert', 'Caps Lock', 'NVDA+F9',
    'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+A', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+V',
    'Alt+F4', 'F10', 'Shift+F10', 'Tab', 'Ctrl+Tab', 'Shift+Tab',
    'Return', 'Escape', 'Ctrl+Left', 'Ctrl+Home', 'Ctrl+Delete', 'Ctrl+Backspace',
    'Space', 'A', 'Shift+A', 'Alt+F', 'Alt+Shift+9', 'Ctrl+Ctrl+Q', 'Win+Q',
    'F25', 'Ctrl++', 'Ctrl+\nK',
])
def test_global_navigation_editing_and_screen_reader_conflicts_are_denied(raw):
    with pytest.raises(ValueError):
        normalize_shortcut(raw)


def test_nested_command_identity_is_explicit_and_independent_of_caption(frame, wx):
    controller = manager_for(frame)
    submenu = Menu('Research && evidence...\tCtrl+Shift+K')
    frame.menu_bar.GetMenu(0).items += [Item(None), Item('Advanced...', submenu=submenu)]
    command = CommandSpec('research.evidence', 'Research evidence', default_shortcut='Ctrl+Shift+K')
    controller.register_menu_command(command, submenu.items[0])
    controller.apply_overrides({'research.evidence': 'F9'})
    assert submenu.items[0].label == 'Research && evidence...\tF9'
    assert controller.bindings['research.evidence'].item is submenu.items[0]
    assert 'research/advanced/research & evidence' not in controller.registry.specs


def test_effective_conflicts_include_unchanged_commands(frame, wx):
    controller = manager_for(frame)
    with pytest.raises(ValueError, match='assigned'):
        controller.apply_overrides({'library.new': 'Ctrl+I'})
    with pytest.raises(ValueError, match='reserved'):
        controller.apply_overrides({'library.new': 'Alt+F4'})
    with pytest.raises(ValueError, match='Unknown'):
        controller.apply_overrides({'library.missing': 'F9'})
    controller.apply_overrides({'library.import': '', 'library.new': 'Ctrl+I'})
    assert controller.shortcut('library.import') == ''
    assert controller.shortcut('library.new') == 'Ctrl+I'


def test_duplicate_semantic_ids_are_not_retargeted(frame, wx):
    controller = manager_for(frame)
    original = controller.registry.get('library.new')
    with pytest.raises(ValueError, match='already registered'):
        controller.registry.register(CommandSpec('library.new', 'Different command', default_shortcut='F9'))
    assert controller.registry.get('library.new') is original
    assert controller.bindings['library.new'].item is frame.menu_bar.GetMenu(0).items[0]


def test_persist_restart_actual_menu_ids_and_default_reset(frame, wx):
    controller = manager_for(frame)
    original = controller.bindings['library.new'].item.GetId()
    controller.apply_overrides({'library.new': 'Ctrl+Shift+K', 'library.import': ''})
    assert frame.menu_bar.GetMenu(0).items[0].label == '&New library\tCtrl+Shift+K'
    assert frame.menu_bar.GetMenu(0).items[1].label == 'Import books'
    path = frame.library.root / 'keyboard-shortcuts.json'
    assert json.loads(path.read_bytes()) == {
        'version': 1, 'bindings': {'library.new': 'Ctrl+Shift+K', 'library.import': ''}}
    reopened = Frame(frame.library.root)
    restart = manager_for(reopened)
    assert reopened.menu_bar.GetMenu(0).items[0].label.endswith('Ctrl+Shift+K')
    assert restart.bindings['library.new'].item.GetId() != original
    assert restart.bindings['library.new'].item is reopened.menu_bar.GetMenu(0).items[0]
    restart.apply_overrides({})
    assert reopened.menu_bar.GetMenu(0).items[0].label.endswith('Ctrl+N')
    assert json.loads(path.read_bytes()) == {'version': 1, 'bindings': {}}


def test_failed_save_rolls_back_live_menu_and_map(frame, wx, monkeypatch):
    controller = manager_for(frame)
    controller.apply_overrides({'library.new': 'F9'})
    before = controller.store.path.read_bytes()

    def unavailable(*args, **kwargs):
        raise OSError('Disk full')

    monkeypatch.setattr(controller.store, 'save', unavailable)
    with pytest.raises(OSError, match='Disk full'):
        controller.apply_overrides({'library.new': 'F12'})
    assert frame.menu_bar.GetMenu(0).items[0].label.endswith('F9')
    assert controller.overrides == {'library.new': 'F9'}
    assert controller.store.path.read_bytes() == before


def test_concurrent_settings_change_does_not_overwrite_another_window(frame, wx):
    first = manager_for(frame)
    other_frame = Frame(frame.library.root)
    second = manager_for(other_frame)
    first.apply_overrides({'library.new': 'F9'})
    with pytest.raises(ValueError, match='another window'):
        second.apply_overrides({'library.new': 'F12'})
    assert other_frame.menu_bar.GetMenu(0).items[0].label.endswith('Ctrl+N')
    assert KeymapStore(second.store.path).load() == {'library.new': 'F9'}


def test_opening_another_library_keeps_original_preferences_and_rejects_stale_dialog(
        frame, wx, tmp_path):
    controller = manager_for(frame)
    controller.apply_overrides({'library.new': 'F9'})
    path, saved = controller.store.path, controller.store.path.read_bytes()
    dialog = object.__new__(KeyboardSettingsDialog)
    dialog.manager, dialog.original_store = controller, controller.store
    messages = []
    dialog._message = messages.append
    other = tmp_path / 'other-library'
    other.mkdir()
    frame.library = SimpleNamespace(root=other)
    controller.change_root(other)
    dialog.save()
    assert 'folder changed' in messages[-1]
    assert not controller.store.path.exists()
    assert frame.menu_bar.GetMenu(0).items[0].label.endswith('Ctrl+N')
    controller.apply_overrides({'library.new': 'F12'})
    assert KeymapStore(other / 'keyboard-shortcuts.json').load() == {'library.new': 'F12'}
    assert path.read_bytes() == saved


@pytest.mark.parametrize('kind', ['duplicate', 'unknown', 'invalid_schema', 'legacy_envelope'])
def test_invalid_saved_preferences_are_preserved_until_explicit_repair(frame, wx, kind):
    path = frame.library.root / 'keyboard-shortcuts.json'
    value = {'version': 1, 'bindings': {'library.new': 'F9'}}
    if kind == 'duplicate':
        value['bindings']['library.new'] = 'Ctrl+I'
    elif kind == 'unknown':
        value['bindings'] = {'library.deleted': 'F9'}
    elif kind == 'invalid_schema':
        value['version'] = True
    else:
        value = {'schema': 1, 'overrides': {'library/new library': 'F9'},
                 'originals': {'library/new library': 'Ctrl+N'}}
    raw = json.dumps(value).encode('utf-8')
    path.write_bytes(raw)
    controller = manager_for(frame)
    assert controller.load_notice
    assert frame.menu_bar.GetMenu(0).items[0].label.endswith('Ctrl+N')
    assert path.read_bytes() == raw
    controller.apply_overrides({})
    assert KeymapStore(path).load() == {}


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'ancestor'])
def test_settings_reject_linked_paths(frame, wx, tmp_path, kind):
    controller = manager_for(frame)
    controller.apply_overrides({'library.new': 'F9'})
    path = controller.store.path
    if kind == 'ancestor':
        source = path.parent
        destination = tmp_path / 'outside-root'
        source.rename(destination)
        source.symlink_to(destination, target_is_directory=True)
    else:
        outside = tmp_path / 'outside.json'
        path.rename(outside)
        if kind == 'symlink':
            path.symlink_to(outside)
        else:
            os.link(outside, path)
    with pytest.raises(ValueError, match='links'):
        KeymapStore(path).load()


@pytest.mark.parametrize('raw', [
    b'{"schema":1,"overrides":{"library/new library":"F9"},"originals":{"library/new library":"Ctrl+N"}}',
    b'{"schema":1,"overrides":{"library/deleted command":"F12"},"originals":{"library/deleted command":"Ctrl+K"}}',
    b'{malformed historical file',
])
def test_legacy_only_keymap_is_not_activated_or_overwritten(frame, wx, raw):
    legacy = frame.library.root / 'state' / 'ui' / 'shortcuts.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(raw)
    controller = manager_for(frame)
    assert controller.overrides == {}
    assert controller.shortcut('library.new') == 'Ctrl+N'
    assert 'kept for review' in controller.load_notice
    assert 'not applied automatically' in controller.load_notice
    assert not controller.store.path.exists()
    assert legacy.read_bytes() == raw
    controller.apply_overrides({'library.new': 'F12'})
    assert KeymapStore(controller.store.path).load() == {'library.new': 'F12'}
    assert legacy.read_bytes() == raw
    reopened = manager_for(Frame(frame.library.root))
    assert reopened.shortcut('library.new') == 'F12'
    assert not reopened.load_notice
    assert legacy.read_bytes() == raw


def test_legacy_review_notice_is_visible_after_install_and_root_change(frame, wx, tmp_path):
    legacy = frame.library.root / 'state' / 'ui' / 'shortcuts.json'
    legacy.parent.mkdir(parents=True)
    raw = b'{"schema":1,"overrides":{},"originals":{}}'
    legacy.write_bytes(raw)
    controller = install_keyboard_settings(frame)
    assert frame.reports and 'kept for review' in frame.reports[-1]
    assert install_keyboard_settings(frame) is controller
    assert legacy.read_bytes() == raw
    other = tmp_path / 'another-library'
    old = other / 'state' / 'ui' / 'shortcuts.json'
    old.parent.mkdir(parents=True)
    old.write_bytes(raw)
    controller.change_root(other)
    assert 'kept for review' in frame.reports[-1]
    assert not (other / 'keyboard-shortcuts.json').exists()
    assert old.read_bytes() == raw


class Control:
    def __init__(self, parent, *args, value='', choices=(), label='', **kwargs):
        self.parent, self.value, self.choices, self.label = parent, value, list(choices), label
        self.identity = args[0] if args else None
        self.handlers, self.selection, self.name = {}, -1, ''
        self.focused, self.enabled = False, True
        parent.controls.append(self)

    def SetName(self, value): self.name = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def Set(self, value): self.choices = list(value)
    def ChangeValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetFocus(self): self.focused = True
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def SetMinSize(self, value): pass
    def Wrap(self, value): pass
    def Bind(self, event, callback): self.handlers[event] = callback


class Dialog:
    def __init__(self, parent, wx, **kwargs):
        self.controls, self.wx, self.destroyed = [], wx, False
        self.result = wx.ID_CANCEL

    def SetSizer(self, value): self.sizer = value
    def SetEscapeId(self, value): self.escape = value
    def EndModal(self, value): self.result = value
    def ShowModal(self):
        self.wx.on_modal(self)
        return self.result
    def Destroy(self): self.destroyed = True


def _dialog_wx(wx, on_modal):
    for name in ('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT', 'TOP',
                 'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT'):
        setattr(wx, name, 1)
    wx.EVT_CHOICE, wx.EVT_BUTTON, wx.EVT_TEXT, wx.EVT_LISTBOX = 'choice', 'button', 'text', 'list'
    wx.Choice = wx.ListBox = wx.TextCtrl = wx.StaticText = wx.Button = Control
    wx.BoxSizer = wx.WrapSizer = lambda value: SimpleNamespace(Add=lambda *args, **kwargs: None)
    wx.on_modal = on_modal
    wx.Dialog = lambda parent, **kwargs: Dialog(parent, wx, **kwargs)


@pytest.mark.parametrize('mode', ['apply', 'conflict', 'reset', 'cancel'])
def test_native_dialog_accessible_fields_apply_reset_and_cancel(frame, wx, mode):
    controller = manager_for(frame)
    observed = []

    def interact(dialog):
        observed.append(dialog)
        fields = {control.name: control for control in dialog.controls if control.name}
        commands = fields['Commands and their current shortcuts']
        key = fields['Shortcut for selected command; leave empty to unassign']
        status = fields['Keyboard settings status']
        assert dialog.escape == wx.ID_CANCEL
        assert fields['Filter keyboard commands'].focused
        commands.SetSelection(0)
        commands.handlers[wx.EVT_LISTBOX](None)
        assert key.value == 'Ctrl+N'
        key.ChangeValue('Ctrl+C' if mode == 'conflict' else 'F9')
        assign = next(control for control in dialog.controls if control.label == '&Assign shortcut')
        if mode != 'cancel':
            assign.handlers[wx.EVT_BUTTON](None)
        if mode == 'conflict':
            assert status.focused and 'reserved' in status.value
            assert controller.overrides == {}
        elif mode == 'reset':
            reset = next(control for control in dialog.controls if control.label == 'Reset &all defaults')
            reset.handlers[wx.EVT_BUTTON](None)
            assert 'Choose Save' in status.value
        if mode in {'apply', 'reset'}:
            save = next(control for control in dialog.controls if control.identity == wx.ID_OK)
            save.handlers[wx.EVT_BUTTON](None)

    _dialog_wx(wx, interact)
    accepted = KeyboardSettingsDialog(frame, controller).show()
    assert observed[0].destroyed
    assert accepted == (mode in {'apply', 'reset'})
    expected = 'F9' if mode == 'apply' else 'Ctrl+N'
    assert frame.menu_bar.GetMenu(0).items[0].label.endswith(expected)
    if mode in {'cancel', 'conflict'}:
        assert controller.overrides == {}
        assert not controller.store.path.exists()
