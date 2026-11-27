"""Execute the selected DesktopFrame menu builder and its real installers.

These headless native-menu records model a wrapper being discarded and later
rewrapped, including loss of Python-only attributes. They do not claim wx QA.
"""
import ast
import importlib
import importlib.util
from itertools import count
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from weakref import WeakValueDictionary

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.keyboard_settings import (KeyboardSettings, capture_commands,
    effective_shortcuts, register_command_identity, save_shortcuts)


_ids = count(1000)


class Item:
    def __init__(self, record):
        self.record = record
    def GetId(self): return self.record.identity
    def IsSeparator(self): return self.record.label is None
    def GetSubMenu(self): return None
    def GetItemLabel(self): return self.record.label
    def SetItemLabel(self, label): self.record.label = label
    def Enable(self, value=True): self.record.enabled = value
    def IsEnabled(self): return self.record.enabled


class Menu:
    def __init__(self):
        self.records, self.wrappers = [], WeakValueDictionary()
    def wrap(self, record):
        item = self.wrappers.get(record.identity)
        if item is None:
            item = Item(record)
            self.wrappers[record.identity] = item
        return item
    def Append(self, identity, label):
        record = SimpleNamespace(identity=next(_ids) if identity == -1 else identity,
                                 label=label, enabled=True)
        self.records.append(record)
        return self.wrap(record)
    def AppendSeparator(self):
        self.Append(-1, None)
    def GetMenuItems(self): return [self.wrap(record) for record in self.records]
    def GetMenuItemCount(self): return len(self.records)
    def Delete(self, identity):
        self.records[:] = [record for record in self.records if record.identity != identity]


class MenuBar:
    def __init__(self): self.menus = []
    def Append(self, menu, label): self.menus.append((label, menu))
    def Insert(self, index, menu, label): self.menus.insert(index, (label, menu))
    def GetMenuCount(self): return len(self.menus)
    def GetMenu(self, index): return self.menus[index][1]
    def GetMenuLabel(self, index): return self.menus[index][0]
    def IsEnabledTop(self, index): return True
    def EnableTop(self, index, value): pass
    def FindMenu(self, label):
        return next((i for i, (title, _) in enumerate(self.menus)
                     if title.replace('&', '') == label), -1)


class Control:
    def __init__(self, *args, **kwargs):
        self.value, self.enabled, self.name = '', True, ''
        self.identity = next(_ids)
    def Bind(self, *args, **kwargs): pass
    def Disable(self): self.enabled = False
    def Enable(self, value=True): self.enabled = value
    def SetName(self, value): self.name = value
    def SetLabel(self, value): self.value = value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def GetValue(self): return self.value
    def GetId(self): return self.identity
    def SetMinSize(self, value): self.minimum_size = value
    def SetSizer(self, value): self.sizer = value
    def Layout(self): pass
    def Add(self, *args, **kwargs): pass
    def Pulse(self): pass
    def SetRange(self, value): self.range = value
    def SetFocus(self): pass
    def Insert(self, *args, **kwargs): pass
    def Start(self, interval): self.interval = interval
    def Stop(self): pass


@pytest.fixture(params=['current-import', 'cached-native-import'])
def native_menus(tmp_path, monkeypatch, request):
    wx = ModuleType('wx')
    wx.Menu, wx.MenuBar = Menu, MenuBar
    wx.Dialog = wx.Frame = wx.Panel = wx.Window = Control
    wx.Button = wx.StaticText = wx.Gauge = wx.Timer = Control
    wx.TextCtrl = wx.CheckBox = wx.WrapSizer = wx.BoxSizer = Control
    wx.CallAfter = lambda callback, *args: callback(*args)
    wx.AcceleratorTable = tuple
    wx.NewIdRef = lambda: next(_ids)
    for name in ('ALL', 'EXPAND', 'GA_HORIZONTAL', 'ACCEL_CTRL', 'ACCEL_ALT', 'ACCEL_SHIFT',
                 'VERTICAL', 'HORIZONTAL', 'LEFT', 'RIGHT', 'TOP', 'TE_READONLY', 'TE_MULTILINE',
                 'ALIGN_CENTER_VERTICAL'):
        setattr(wx, name, 1)
    wx.ID_ANY, wx.ID_EXIT = -1, 500
    for name in ('EVT_MENU', 'EVT_BUTTON', 'EVT_WINDOW_DESTROY', 'EVT_TIMER', 'EVT_CHECKBOX'):
        setattr(wx, name, name)
    for number in range(1, 25): setattr(wx, 'WXK_F' + str(number), 2000 + number)
    if request.param == 'cached-native-import':
        # Windows installed suites may have imported an installer using real
        # native event binders before this headless fixture replaces wx.
        previous = ModuleType('wx')
        previous.__dict__.update(wx.__dict__)
        previous.EVT_MENU = object()
        monkeypatch.setitem(sys.modules, 'wx', previous)
        cached = importlib.import_module('carr_library.ui.trust_status')
        monkeypatch.setattr(cached, 'wx', previous)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda key, identity: (0, key, int(identity)))
    source = Path(importlib.util.find_spec('carr_library.ui.desktop_app').origin)
    tree = ast.parse(source.read_text(encoding='utf-8'))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DesktopFrame')
    builder = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == '_menus')
    bindings, notices = {}, []
    frame = SimpleNamespace(library=LibraryApp(tmp_path / 'library'), cancel_event=Event(),
        _closing=False, working=False, busy=False, host=Control(), layout=Control(),
        IsActive=lambda: True, IsEnabled=lambda: True, IsBeingDeleted=lambda: False,
        choice=Control(), status=Control(), GetId=lambda: 71, SetAcceleratorTable=lambda value: None,
        report=notices.append, Unbind=lambda *args, **kwargs: True,
        evidence=None, _dirty=lambda: False, _restore_note_focus=lambda: None)
    def bind(event, callback, item=None, **kwargs):
        if event == wx.EVT_MENU:
            identity = item.GetId() if item is not None else kwargs['id']
            bindings[identity] = callback
    frame.Bind = bind
    frame.SetMenuBar = lambda bar: setattr(frame, 'menu_bar', bar)
    frame.GetMenuBar = lambda: frame.menu_bar
    # Ordinary action handlers are captured, never invoked. Every installer
    # below is the actual selected production function; none is replaced.
    for node in ast.walk(builder):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self':
            if not hasattr(frame, node.attr):
                setattr(frame, node.attr, lambda event=None: None)
    env = dict(wx=wx, __name__='carr_library.ui._native_menu_contract', __package__='carr_library.ui')
    # Installers imported inside _menus can already be cached by preceding
    # installed/native suites. Bind their actual globals to this fixture's wx
    # just like top-level installers; do not replace any production installer.
    for node in (*tree.body, *ast.walk(builder)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            for alias in node.names:
                if alias.name.startswith('install_'):
                    module = importlib.import_module('carr_library.ui.' + node.module)
                    if hasattr(module, 'wx'):
                        monkeypatch.setattr(module, 'wx', wx)
                    env[alias.asname or alias.name] = getattr(module, alias.name)
    exec(compile(ast.Module(body=[builder], type_ignores=[]), str(source), 'exec'), env)
    yield SimpleNamespace(frame=frame, build=env['_menus'], wx=wx, bindings=bindings, notices=notices)
    progress = getattr(frame, 'operation_progress', None)
    if progress is not None:
        progress.close()


def test_actual_desktop_installers_retain_every_native_command_across_wrapper_recreation(native_menus):
    frame = native_menus.frame
    native_menus.build(frame)
    commands = frame.keyboard_settings.commands
    actions = {row.action: row for row in commands}
    assert len(commands) >= 60 and len(actions) == len(commands)
    assert len({row.menu_id for row in commands}) == len(commands)
    for name in ('library/import center: files, folders and queues',
                 'academic/doi and isbn lookup and open-access sources',
                 'audio studio/publish synchronized epub3', 'help/keyboard shortcuts',
                 'help/find a command', 'voice/voice reader and reading copy'):
        assert name in actions
    # A composition that ships the public-client views must make their real
    # installer actions reachable before KeyboardSettings captures the menus.
    for module, action in (('portable_sync', 'library/portable snapshots and transfer'),
                           ('trust_status', 'help/license and signed updates')):
        if importlib.util.find_spec('carr_library.ui.' + module) is not None:
            assert action in actions
    published = actions['audio studio/publish synchronized epub3']
    assert 'EPUB3 / DAISY' in published.title
    assert published.menu_id in native_menus.bindings
    missing = [(row.action, row.menu_id) for row in commands if row.menu_id not in native_menus.bindings]
    assert not missing, f'Actual menu commands without a selected native event binding: {missing}'
    effective_shortcuts(commands, {})  # All actual unchanged defaults coexist.
    assert not native_menus.notices
    controller = frame.keyboard_settings
    # wx can expose new Python wrappers while the retained native IDs survive.
    for _, menu in frame.menu_bar.menus:
        menu.wrappers.clear()
    change = controller.apply_with_undo({published.action: 'Ctrl+Shift+9'})
    controller._guard()
    assert effective_shortcuts(controller.commands, controller.overrides)[published.action] == 'Ctrl+Shift+9'
    controller.undo(change)
    controller._guard()
    assert not controller.overrides


def test_publication_alias_and_saved_key_survive_discarded_item_wrapper(native_menus):
    from carr_library.ui.studio_dialog import install_studio_actions
    frame = native_menus.frame
    bar = frame.menu_bar = MenuBar()
    menu = Menu()
    menu.Append(-1, 'Publish synchronized EPUB3...')
    bar.Append(menu, 'Audio St&udio')
    action = 'audio studio/publish synchronized epub3'
    save_shortcuts(frame.library.root, capture_commands(bar), {action: 'Ctrl+Alt+P'}, expected_token=None)
    bar.menus.clear()
    install_studio_actions(frame)
    menu = bar.GetMenu(0)
    # Canonical pending CommandSpec entries retain their original wrappers;
    # force a later wx wrapper view while preserving the actual native records.
    menu.wrappers.clear()
    assert not menu.wrappers
    controller = KeyboardSettings(frame, wx_module=native_menus.wx)
    row = next(row for row in controller.commands if row.action == action)
    assert row.default == '' and 'EPUB3 / DAISY' in row.title
    assert controller.overrides == {action: 'Ctrl+Alt+P'}
    assert effective_shortcuts(controller.commands, controller.overrides)[action] == 'Ctrl+Alt+P'
    assert row.menu_id in native_menus.bindings and not native_menus.notices


def test_retained_alias_rejects_rebinding_reused_ids_and_unknown_slash_paths():
    bar, menu = MenuBar(), Menu()
    bar.Append(menu, 'Audio Studio')
    item = menu.Append(-1, 'Publish synchronized EPUB3 / DAISY...')
    register_command_identity(bar, item, 'publish synchronized epub3')
    register_command_identity(bar, item, 'publish synchronized epub3')
    with pytest.raises(ValueError, match='different command'):
        register_command_identity(bar, item, 'a different saved command')
    assert capture_commands(bar)[0].action == 'audio studio/publish synchronized epub3'
    item.SetItemLabel('Another native command')
    with pytest.raises(ValueError, match='identity changed'):
        capture_commands(bar)
    bar._carr_command_names.clear()
    item.SetItemLabel('Unregistered / ambiguous command')
    with pytest.raises(ValueError, match='stable, unambiguous'):
        capture_commands(bar)



def test_progress_installer_adds_two_commands_to_the_existing_shared_manager(native_menus):
    from carr_library.ui.keyboard_settings import install_keyboard_settings
    from carr_library.ui.progress_panel import install_progress_panel

    frame = native_menus.frame
    frame.menu_bar = MenuBar()
    options = Menu()
    frame.menu_bar.Append(options, '&Options')
    retained = options.Append(-1, 'Retained action\tCtrl+Shift+7')
    frame.Bind(native_menus.wx.EVT_MENU, lambda event: None, retained)
    manager = install_keyboard_settings(frame)
    original = {row.menu_id for row in frame.keyboard_settings.commands}

    # This is the real installer after the real manager is active, matching
    # DesktopFrame._menus. Both callbacks must become registered native leaves.
    panel = install_progress_panel(frame)
    assert frame.keyboard_commands is manager
    assert frame.keyboard_settings.manager is manager
    query = manager.bindings['progress.query'].item
    settings = manager.bindings['options.progress'].item
    assert query.GetId() != settings.GetId()
    assert manager.registry.get('progress.query').default_shortcut == 'Ctrl+Shift+Q'
    assert native_menus.bindings[query.GetId()].__self__ is panel
    assert native_menus.bindings[settings.GetId()].__self__ is panel
    observed = {row.menu_id for row in frame.keyboard_settings.commands}
    assert observed == original | {query.GetId(), settings.GetId()}
    assert retained.GetId() in observed
    manager.guard_native()
    frame.keyboard_settings._guard()

    # Reinstalling the observer must keep one owner and the same semantic IDs.
    assert install_progress_panel(frame) is panel
    assert frame.operation_progress is panel.legacy
    assert {row.menu_id for row in frame.keyboard_settings.commands} == observed
    assert len(options.GetMenuItems()) == len(observed)


def test_active_manager_still_rejects_an_unregistered_second_native_leaf(native_menus):
    from carr_library.ui.command_registry import CommandSpec
    from carr_library.ui.keyboard_settings import install_keyboard_settings, register_menu_command

    frame = native_menus.frame
    frame.menu_bar = MenuBar()
    options = Menu()
    frame.menu_bar.Append(options, '&Options')
    manager = install_keyboard_settings(frame)
    first = options.Append(-1, 'Explicit dynamic command')
    second = options.Append(-1, 'Unregistered dynamic command')
    frame.Bind(native_menus.wx.EVT_MENU, lambda event: None, first)

    # This intentionally repeats the faulty two-append sequence. The fix is in
    # the progress installer; relaxing the manager's identity guard is unsafe.
    with pytest.raises(ValueError, match='A new native command must register its semantic identity'):
        register_menu_command(frame, CommandSpec('test.dynamic', 'Explicit dynamic command'), first)
    assert second.GetId() not in {binding.item.GetId() for binding in manager.bindings.values()}
    with pytest.raises(ValueError, match='menu commands changed'):
        manager.guard_native()
