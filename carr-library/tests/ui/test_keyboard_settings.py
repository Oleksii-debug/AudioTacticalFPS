"""Headless native-control contracts; no Windows or NVDA validation is claimed."""
from itertools import count
import json
import os
from types import SimpleNamespace

import pytest

from carr_library.ui.keyboard_settings import (KeyboardSettings, accelerator_entries, capture_commands,
    effective_shortcuts, install_keyboard_settings, load_shortcuts, normalize_shortcut, save_shortcuts)


_ids = count(200)


class Item:
    def __init__(self, label, identity=None, submenu=None):
        self.label, self.identity, self.submenu = label, next(_ids) if identity is None else identity, submenu
    def IsSeparator(self):
        return self.label is None
    def GetItemLabel(self):
        return self.label
    def SetItemLabel(self, label):
        self.label = label
    def GetId(self):
        return self.identity
    def GetSubMenu(self):
        return self.submenu


class Menu:
    def __init__(self, *labels):
        self.items = [Item(label) for label in labels]
    def GetMenuItems(self):
        return self.items
    def Append(self, identity, label):
        item = Item(label, None if identity == -1 else identity)
        self.items.append(item)
        return item
    def AppendSeparator(self):
        self.items.append(Item(None))


class MenuBar:
    def __init__(self):
        self.menus = [('&File', Menu('&Save\tCtrl+S', 'E&xit\tAlt+F4')),
                      ('&Library', Menu('&New library\tCtrl+N', 'Import books\tCtrl+I')),
                      ('&Help', Menu('&Keyboard guide\tF1'))]
    def GetMenuCount(self):
        return len(self.menus)
    def GetMenuLabel(self, index):
        return self.menus[index][0]
    def GetMenu(self, index):
        return self.menus[index][1]
    def Append(self, menu, label):
        self.menus.append((label, menu))


class Focus:
    def __init__(self):
        self.focused = False
    def SetFocus(self):
        self.focused = True


class Frame:
    def __init__(self, root):
        root.mkdir(exist_ok=True)
        self.library = SimpleNamespace(root=root)
        self.menu_bar, self.bindings, self.reports = MenuBar(), {}, []
        self.choice, self.status = Focus(), Focus()
    def Bind(self, event, callback, item):
        self.bindings[item.GetId()] = callback
    def SetAcceleratorTable(self, table):
        self.table = table
    def GetMenuBar(self):
        return self.menu_bar
    def report(self, message):
        self.reports.append(message)


@pytest.fixture
def wx():
    return SimpleNamespace(ACCEL_CTRL=1, ACCEL_ALT=2, ACCEL_SHIFT=4, ID_ANY=-1,
                           EVT_MENU='menu', Menu=Menu, AcceleratorTable=lambda entries: tuple(entries),
                           **{'WXK_F' + str(index): 1000 + index for index in range(1, 25)})


@pytest.fixture
def frame(tmp_path):
    return Frame(tmp_path / 'library')


@pytest.mark.parametrize(('raw', 'expected'), [('ctrl+shift+k', 'Ctrl+Shift+K'),
    ('Shift + Control + alt + 9', 'Ctrl+Alt+Shift+9'), ('F9', 'F9'),
    ('ctrl+f24', 'Ctrl+F24'), ('', ''), ('   ', '')])
def test_shortcut_normalization(raw, expected):
    assert normalize_shortcut(raw) == expected


@pytest.mark.parametrize('raw', ['Insert', 'Ctrl+Insert', 'Shift+Insert', 'Caps Lock', 'NVDA+F9',
    'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+A', 'Ctrl+Z', 'Ctrl+Y', 'Ctrl+Shift+V', 'Ctrl+F',
    'Ctrl+W', 'Alt+F4', 'Ctrl+Alt+N', 'F6', 'F10', 'Shift+F10', 'Tab', 'Ctrl+Tab', 'Shift+Tab',
    'Return', 'Escape', 'Ctrl+Left', 'Ctrl+Home', 'Ctrl+Delete', 'Ctrl+Backspace', 'Space',
    'A', 'Shift+A', 'Alt+F', 'Alt+Shift+9', 'Ctrl+Ctrl+Q', 'Win+Q', 'F25', 'Ctrl++', 'Ctrl+\nK'])
def test_global_navigation_editing_and_screen_reader_conflicts_are_denied(raw):
    with pytest.raises(ValueError):
        normalize_shortcut(raw)


def test_stable_paths_nested_menus_mnemonics_and_original_keys(frame):
    submenu = Menu('Research && evidence...\tCtrl+Shift+K')
    frame.menu_bar.GetMenu(1).items += [Item(None), Item('Advanced...', submenu=submenu)]
    commands = capture_commands(frame.menu_bar)
    assert commands[0].action == 'file/save' and commands[0].default == 'Ctrl+S'
    assert commands[1].default == 'Alt+F4'
    nested = next(item for item in commands if item.action.endswith('research & evidence'))
    assert nested.action == 'library/advanced/research & evidence'
    assert nested.title == 'Library / Advanced... / Research & evidence...'


def test_effective_conflicts_include_unchanged_commands(frame):
    commands = capture_commands(frame.menu_bar)
    with pytest.raises(ValueError, match='already assigned.*Import books'):
        effective_shortcuts(commands, {'library/new library': 'Ctrl+I'})
    with pytest.raises(ValueError, match='reserved'):
        effective_shortcuts(commands, {'library/new library': 'Alt+F4', 'file/exit': ''})
    with pytest.raises(ValueError, match='unavailable'):
        effective_shortcuts(commands, {'library/missing action': 'F9'})
    keys = effective_shortcuts(commands, {'library/import books': '', 'library/new library': 'Ctrl+I'})
    assert keys['library/import books'] == '' and keys['library/new library'] == 'Ctrl+I'


def test_duplicate_action_paths_and_numeric_ids_are_not_retargeted(frame):
    original = frame.menu_bar.GetMenu(1).items[0]
    frame.menu_bar.GetMenu(1).items.append(Item('New library...'))
    with pytest.raises(ValueError, match='Duplicate'):
        capture_commands(frame.menu_bar)
    frame.menu_bar.GetMenu(1).items[-1] = Item('Different command', identity=original.GetId())
    with pytest.raises(ValueError, match='IDs'):
        capture_commands(frame.menu_bar)


def test_persist_restart_actual_menu_ids_and_default_reset(frame, wx):
    controller = KeyboardSettings(frame, wx_module=wx)
    original = capture_commands(frame.menu_bar)
    controller.apply({'library/new library': 'Ctrl+Shift+K', 'library/import books': ''})
    assert frame.menu_bar.GetMenu(1).items[0].label == '&New library\tCtrl+Shift+K'
    assert frame.menu_bar.GetMenu(1).items[1].label == 'Import books'
    entry = (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('K'), original[2].menu_id)
    assert entry in frame.table
    path = frame.library.root / 'state' / 'ui' / 'shortcuts.json'
    saved = json.loads(path.read_bytes())
    assert saved['originals'] == {'library/new library': 'Ctrl+N', 'library/import books': 'Ctrl+I'}
    assert all(isinstance(key, str) and '/' in key for key in saved['overrides'])
    reopened = Frame(frame.library.root)
    restart = KeyboardSettings(reopened, wx_module=wx)
    assert reopened.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+Shift+K')
    assert reopened.menu_bar.GetMenu(1).items[0].GetId() != original[2].menu_id
    assert (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('K'), reopened.menu_bar.GetMenu(1).items[0].GetId()) in reopened.table
    restart.apply({})
    assert reopened.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+N')
    assert json.loads(path.read_bytes()) == {'schema': 1, 'overrides': {}, 'originals': {}}


def test_upgrade_preserves_saved_keys_and_disables_only_conflicting_default(frame, wx):
    previous = KeyboardSettings(frame, wx_module=wx)
    previous.apply({'library/new library': 'Ctrl+Alt+Q', 'library/import books': 'F9'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    saved = path.read_bytes()
    upgraded = Frame(frame.library.root)
    research = Menu('Build advanced query\tCtrl+Alt+Q', 'Other research action\tCtrl+Alt+J')
    upgraded.menu_bar.Append(research, '&Research')
    controller = KeyboardSettings(upgraded, wx_module=wx)
    assert controller.reload() is True
    keys = effective_shortcuts(controller.commands, controller.overrides)
    assert keys['library/new library'] == 'Ctrl+Alt+Q'
    assert keys['library/import books'] == 'F9'
    assert keys['research/build advanced query'] == ''
    assert keys['research/other research action'] == 'Ctrl+Alt+J'
    owner_id = upgraded.menu_bar.GetMenu(1).items[0].GetId()
    assert [entry for entry in upgraded.table if entry[:2] ==
            (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('Q'))] == [
                (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('Q'), owner_id)]
    assert not any(entry[2] == research.items[0].GetId() for entry in upgraded.table)
    assert research.items[0].label == 'Build advanced query'
    assert path.read_bytes() == saved
    loaded, token = load_shortcuts(upgraded.library.root, controller.commands)
    assert loaded == controller.overrides and token == controller.token
    explanation = upgraded.reports[-1]
    assert 'Ctrl+Alt+Q' in explanation and 'Build advanced query' in explanation
    assert 'New library' in explanation and 'unassigned' in explanation
    observed = []
    def inspect_notice(dialog):
        controls = {control.name: control for control in dialog.controls if control.name}
        observed.append(controls['Shortcut settings status'].value)
        choice = controls['Command to change']
        choice.SetSelection(next(index for index, command in enumerate(controller.commands)
                                 if command.action == 'research/build advanced query'))
        choice.handlers[wx.EVT_CHOICE](None)
        assert controls['Current and original shortcut'].value == 'Current: Unassigned. Original: Ctrl+Alt+Q.'
    _dialog_wx(wx, inspect_notice)
    controller.show()
    assert observed == [explanation]

    # The compatibility result is persisted only by an explicit Apply action.
    controller.apply(dict(controller.overrides))
    persisted = json.loads(path.read_bytes())
    assert persisted['overrides']['research/build advanced query'] == ''
    assert persisted['originals']['research/build advanced query'] == 'Ctrl+Alt+Q'
    restarted = Frame(frame.library.root)
    restarted.menu_bar.Append(Menu('Build advanced query\tCtrl+Alt+Q',
                                  'Other research action\tCtrl+Alt+J'), '&Research')
    restored = KeyboardSettings(restarted, wx_module=wx)
    assert restored.overrides == controller.overrides
    assert not restarted.reports


def test_duplicate_explicit_saved_bindings_are_not_repaired_by_upgrade(frame, wx):
    previous = KeyboardSettings(frame, wx_module=wx)
    previous.apply({'library/new library': 'Ctrl+Alt+Q', 'library/import books': 'F9'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    data = json.loads(path.read_bytes())
    data['overrides']['library/import books'] = 'Ctrl+Alt+Q'
    path.write_text(json.dumps(data), encoding='utf-8')
    damaged = path.read_bytes()
    upgraded = Frame(frame.library.root)
    upgraded.menu_bar.Append(Menu('Build advanced query\tCtrl+Alt+Q'), '&Research')
    controller = KeyboardSettings(upgraded, wx_module=wx)
    assert controller.reload() is False
    assert controller.overrides == {}
    with pytest.raises(ValueError, match='already assigned'):
        load_shortcuts(upgraded.library.root, controller.commands)
    assert 'could not be applied' in upgraded.reports[-1]
    assert path.read_bytes() == damaged


def test_upgrade_does_not_allow_saving_two_explicit_owners_for_one_key(frame, wx):
    previous = KeyboardSettings(frame, wx_module=wx)
    previous.apply({'library/new library': 'Ctrl+Alt+Q'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    upgraded = Frame(frame.library.root)
    upgraded.menu_bar.Append(Menu('Build advanced query\tCtrl+Alt+Q'), '&Research')
    controller = KeyboardSettings(upgraded, wx_module=wx)
    assert controller.overrides['library/new library'] == 'Ctrl+Alt+Q'
    saved, table = path.read_bytes(), upgraded.table
    collision = {**controller.overrides, 'research/build advanced query': 'Ctrl+Alt+Q'}
    with pytest.raises(ValueError, match='already assigned'):
        controller.apply(collision)
    with pytest.raises(ValueError, match='already assigned'):
        save_shortcuts(upgraded.library.root, controller.commands, collision,
                       expected_token=controller.token)
    assert path.read_bytes() == saved and upgraded.table == table


@pytest.mark.parametrize('collision', ['path', 'menu-id'])
def test_upgrade_does_not_mask_duplicate_command_registration(frame, wx, collision):
    previous = KeyboardSettings(frame, wx_module=wx)
    previous.apply({'library/new library': 'Ctrl+Alt+Q'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    saved = path.read_bytes()
    upgraded = Frame(frame.library.root)
    if collision == 'path':
        upgraded.menu_bar.GetMenu(1).items.append(Item('New library'))
    else:
        duplicate = Menu()
        duplicate.items.append(Item('Build advanced query\tCtrl+Alt+Q',
                                    identity=upgraded.menu_bar.GetMenu(1).items[0].GetId()))
        upgraded.menu_bar.Append(duplicate, '&Research')
    with pytest.raises(ValueError, match='Duplicate|IDs'):
        KeyboardSettings(upgraded, wx_module=wx)
    assert path.read_bytes() == saved


def test_upgrade_does_not_retarget_saved_unknown_command(frame, wx):
    previous = KeyboardSettings(frame, wx_module=wx)
    previous.apply({'library/new library': 'Ctrl+Alt+Q'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    saved = path.read_bytes()
    upgraded = Frame(frame.library.root)
    upgraded.menu_bar.GetMenu(1).items.pop(0)
    upgraded.menu_bar.Append(Menu('Build advanced query\tCtrl+Alt+Q'), '&Research')
    controller = KeyboardSettings(upgraded, wx_module=wx)
    assert controller.reload() is False
    with pytest.raises(ValueError, match='unavailable'):
        load_shortcuts(upgraded.library.root, controller.commands)
    assert path.read_bytes() == saved
    assert not controller.overrides


def test_failed_save_rolls_back_live_menu_and_accelerator_table(frame, wx, monkeypatch):
    import carr_library.ui.keyboard_settings as module
    controller = KeyboardSettings(frame, wx_module=wx)
    table = frame.table
    def unavailable(*args, **kwargs):
        raise OSError('Disk full')
    monkeypatch.setattr(module, 'atomic_json', unavailable)
    with pytest.raises(OSError, match='Disk full'):
        controller.apply({'library/new library': 'F9'})
    assert frame.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+N')
    assert frame.table == table and controller.overrides == {}


def test_concurrent_settings_change_does_not_overwrite_another_window(frame, wx):
    first = KeyboardSettings(frame, wx_module=wx)
    other_frame = Frame(frame.library.root)
    second = KeyboardSettings(other_frame, wx_module=wx)
    first.apply({'library/new library': 'F9'})
    with pytest.raises(ValueError, match='Another window'):
        second.apply({'library/new library': 'F12'})
    assert other_frame.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+N')
    assert load_shortcuts(frame.library.root, second.commands)[0] == {'library/new library': 'F9'}


def test_opening_another_library_loads_only_that_roots_preferences(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9'})
    other = tmp_path / 'other-library'
    other.mkdir()
    frame.library = SimpleNamespace(root=other)
    with pytest.raises(ValueError, match='folder changed'):
        controller.apply({'library/new library': 'F12'})
    assert controller.reload() is True
    assert frame.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+N')
    controller.apply({'library/new library': 'F12'})
    assert json.loads((other / 'state/ui/shortcuts.json').read_bytes())['overrides'] == {'library/new library': 'F12'}


@pytest.mark.parametrize('kind', ['duplicate', 'unknown', 'changed_default', 'invalid_schema'])
def test_invalid_saved_preferences_keep_originals_and_can_be_reset(frame, wx, kind):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    value = json.loads(path.read_bytes())
    if kind == 'duplicate':
        value['overrides']['library/new library'] = 'Ctrl+I'
        # Both owners are explicit: unlike a new implicit default, neither may
        # be silently disabled to make a damaged saved configuration load.
        value['overrides']['library/import books'] = 'Ctrl+I'
        value['originals']['library/import books'] = 'Ctrl+I'
    elif kind == 'unknown':
        value['overrides'] = {'library/deleted command': 'F9'}
        value['originals'] = {'library/deleted command': 'Ctrl+N'}
    elif kind == 'changed_default':
        value['originals']['library/new library'] = 'Ctrl+Shift+N'
    else:
        value['schema'] = True
    path.write_text(json.dumps(value), encoding='utf-8')
    assert controller.reload() is False
    assert frame.menu_bar.GetMenu(1).items[0].label.endswith('Ctrl+N')
    assert frame.reports and 'Original keyboard shortcuts are active' in frame.reports[-1]
    controller.apply({})
    assert load_shortcuts(frame.library.root, controller.commands)[0] == {}


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'ancestor'])
def test_settings_reject_linked_paths(frame, wx, tmp_path, kind):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9'})
    path = frame.library.root / 'state/ui/shortcuts.json'
    if kind == 'ancestor':
        source = path.parent
        destination = tmp_path / 'outside-ui'
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
        load_shortcuts(frame.library.root, controller.commands)


def test_installer_adds_focus_actions_and_binds_real_menu_ids(frame, wx, monkeypatch):
    monkeypatch.setitem(__import__('sys').modules, 'wx', wx)
    controller = install_keyboard_settings(frame)
    keys = effective_shortcuts(controller.commands, controller.overrides)
    for action, target in [('help/focus library and research topic', frame.choice), ('help/focus operation status', frame.status)]:
        command = next(item for item in controller.commands if item.action == action)
        assert any(entry[2] == command.menu_id for entry in frame.table)
        frame.bindings[command.menu_id](None)
        assert target.focused
    count_before = len(controller.commands)
    assert install_keyboard_settings(frame) is controller
    assert len(capture_commands(frame.menu_bar)) == count_before
    assert keys['help/keyboard shortcuts'] == 'Ctrl+Alt+K'


def test_expanded_publication_menu_preserves_saved_epub_shortcut(frame, wx, monkeypatch):
    from carr_library.ui.studio_dialog import install_studio_actions
    frame.menu_bar.Append(Menu('Publish synchronized EPUB3...'), 'Audio St&udio')
    action = 'audio studio/publish synchronized epub3'
    save_shortcuts(frame.library.root, capture_commands(frame.menu_bar),
                   {action: 'Ctrl+Alt+P'}, expected_token=None)
    frame.menu_bar.menus.pop()
    monkeypatch.setitem(__import__('sys').modules, 'wx', wx)
    monkeypatch.setitem(__import__('sys').modules, 'carr_library.ui.studio_clips',
                       SimpleNamespace(show_clip_editor=lambda frame: None))
    install_studio_actions(frame)
    controller = KeyboardSettings(frame, wx_module=wx)
    command = next(row for row in controller.commands if row.action == action)
    assert 'EPUB3 / DAISY' in command.title
    assert controller.overrides == {action: 'Ctrl+Alt+P'}
    assert effective_shortcuts(controller.commands, controller.overrides)[action] == 'Ctrl+Alt+P'
    assert command.menu_id in frame.bindings
    assert not frame.reports


class Control(Focus):
    def __init__(self, parent, *args, value='', choices=(), label='', style=0, **kwargs):
        super().__init__()
        if len(args) > 1:
            label = args[1]
        self.parent, self.value, self.choices, self.label = parent, value, list(choices), label
        self.identity = args[0] if args else None
        self.handlers, self.selection, self.name, self.enabled = {}, -1, '', True
        parent.controls.append(self)
    def SetName(self, value): self.name = value
    def GetLabel(self): return self.label
    def Enable(self, value=True): self.enabled = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def ChangeValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SelectAll(self): self.selected_all = True
    def Bind(self, event, callback): self.handlers[event] = callback


class Dialog:
    def __init__(self, parent, wx, **kwargs):
        self.controls, self.wx, self.destroyed, self.handlers = [], wx, False, {}
    def __bool__(self): return not self.destroyed
    def GetId(self):
        assert not self.destroyed
        return id(self)
    def Bind(self, event, callback): self.handlers[event] = callback
    def SetSizer(self, value): self.sizer = value
    def SetEscapeId(self, value): self.escape = value
    def ShowModal(self): self.wx.on_modal(self)
    def Destroy(self): self.destroyed = True


def _dialog_wx(wx, on_modal):
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL',
                 'LEFT', 'TOP', 'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT'):
        setattr(wx, name, 1)
    wx.ID_APPLY, wx.ID_CANCEL, wx.EVT_CHOICE, wx.EVT_BUTTON = 10, 11, 'choice', 'button'
    wx.EVT_TEXT = 'text'
    wx.EVT_WINDOW_DESTROY = 'destroy'
    wx.Choice = wx.TextCtrl = wx.StaticText = wx.Button = Control
    wx.BoxSizer = lambda value: SimpleNamespace(Add=lambda *args, **kwargs: None)
    wx.on_modal = on_modal
    class NativeDialog(Dialog):
        def __init__(self, parent, **kwargs):
            super().__init__(parent, wx, **kwargs)
    wx.Dialog = NativeDialog


@pytest.mark.parametrize('mode', ['apply', 'conflict', 'reset', 'cancel'])
def test_native_dialog_accessible_fields_apply_reset_and_cancel(frame, wx, mode):
    controller = KeyboardSettings(frame, wx_module=wx)
    observed = []
    def interact(dialog):
        observed.append(dialog)
        controls = {control.name: control for control in dialog.controls if control.name}
        choice = controls['Command to change']
        key = controls['Keyboard shortcut; empty means unassigned']
        status = controls['Shortcut settings status']
        assert dialog.escape == wx.ID_CANCEL and choice.focused
        choice.SetSelection(next(i for i, item in enumerate(controller.commands) if item.action == 'library/new library'))
        choice.handlers[wx.EVT_CHOICE](None)
        assert 'Original: Ctrl+N' in controls['Current and original shortcut'].value
        key.ChangeValue('Ctrl+C' if mode == 'conflict' else 'F9')
        apply = next(control for control in dialog.controls if control.identity == wx.ID_APPLY)
        if mode != 'cancel':
            apply.handlers[wx.EVT_BUTTON](None)
        if mode == 'conflict':
            assert status.focused and 'not applied' in status.value
        elif mode == 'reset':
            reset = next(control for control in dialog.controls if control.label.startswith('&Reset'))
            reset.handlers[wx.EVT_BUTTON](None)
            assert 'restored and saved' in status.value
        elif mode == 'apply':
            assert key.focused and key.selected_all
    _dialog_wx(wx, interact)
    controller.show()
    assert observed[0].destroyed
    expected = 'F9' if mode == 'apply' else 'Ctrl+N'
    assert frame.menu_bar.GetMenu(1).items[0].label.endswith(expected)
    assert controller.overrides == ({'library/new library': 'F9'} if mode == 'apply' else {})
