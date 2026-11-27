"""Real controller boundaries with explicit native stand-ins; not Windows evidence."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.keybindings import Command
from carr_library.ui.desktop_integration import (ClipboardWatch, DesktopCommands, DesktopIntegration,
                                                 GLOBAL_COMMANDS, GlobalHotkeys, MENU_PATHS)


def test_watch_is_inert_then_coalesces_exact_text_and_never_opens_or_persists_it():
    text, scope, messages, reads = ['  😀 Exact\r\ncopy.\t '], ['library-a'], [], []
    watch = ClipboardWatch(lambda: reads.append(True) or text[0], lambda: scope[0], messages.append)
    assert not watch.poll() and reads == [] and watch.pending is None
    watch.enable()
    assert reads == [] and watch.poll() and watch.pending == text[0]
    assert not watch.poll() and len(messages) == 1
    text[0] = 'Replacement remains only in memory.'
    assert watch.poll() and watch.take() == text[0] and watch.pending is None
    assert not watch.poll()  # An unchanged clipboard cannot reopen itself.
    scope[0] = 'library-b'
    count = len(reads)
    assert not watch.poll() and not watch.enabled and watch.pending is None
    assert len(reads) == count
    assert all('Exact' not in message and 'Replacement' not in message for message in messages)


@pytest.mark.parametrize('invalid', ['', '\0secret', '\ud800', 'a' * 2_000_001, None],
                         ids=['empty', 'nul', 'surrogate', 'over-budget', 'none'])
def test_invalid_watch_input_clears_previous_pending_without_changing_source(invalid):
    text, messages = ['Valid exact text'], []
    watch = ClipboardWatch(lambda: text[0], lambda: 'a', messages.append)
    watch.enable(); assert watch.poll()
    text[0] = invalid
    assert not watch.poll() and watch.pending is None and watch.enabled
    assert not watch.poll() and len(messages) == 2
    text[0] = 'Valid exact text'
    assert watch.poll() and watch.take() == text[0]
    watch.disable()
    assert watch.pending is None and not watch.enabled
    with pytest.raises(ValueError):
        watch.take()


def test_closed_or_changed_scope_never_releases_a_pending_copy():
    scope, read = ['a'], Mock(return_value='Independent snapshot')
    watch = ClipboardWatch(read, lambda: scope[0], Mock())
    watch.enable(); watch.poll()
    scope[0] = 'b'
    with pytest.raises(ValueError, match='changed'):
        watch.take()
    watch.enable(); watch.poll(); watch.close()
    count = read.call_count
    assert not watch.poll() and read.call_count == count and watch.pending is None
    with pytest.raises(ValueError, match='closed'):
        watch.enable()


class Native:
    def __init__(self):
        self.keys, self.calls = {}, []
        self.fail_at, self.refuse_release = None, False
    def register(self, identity, shortcut):
        self.calls.append(('register', identity, shortcut))
        if len(self.keys) == self.fail_at:
            return False
        self.keys[identity] = shortcut
        return True
    def unregister(self, identity):
        self.calls.append(('release', identity))
        if self.refuse_release:
            return False
        self.keys.pop(identity)
        return True


def hotkeys(*, platform='win32'):
    aliases = (*GLOBAL_COMMANDS, 'desktop.settings', 'desktop.hide')
    commands = [Command(MENU_PATHS[name], name) for name in aliases]
    commands.append(Command('Other/Another action', 'Another action', 'Ctrl+Shift+Q'))
    bindings = [SimpleNamespace(command=command, item=SimpleNamespace(
        IsEnabled=lambda: True, GetId=lambda index=index: index + 100))
        for index, command in enumerate(commands)]
    native_manager = SimpleNamespace(commands=commands, bindings=bindings, overrides={})
    frame = SimpleNamespace(Bind=Mock(), Unbind=Mock(), report=Mock(), _closing=False,
        keyboard_actions=native_manager, library=SimpleNamespace(root='/native-fixture'),
        active=('workspace', None), IsEnabled=lambda: True)
    manager = DesktopCommands(frame, SimpleNamespace())
    native, dispatched = Native(), []
    owner = GlobalHotkeys(frame, manager, dispatched.append,
                         wx_module=SimpleNamespace(EVT_HOTKEY='hotkey'), platform=platform, native=native)
    return SimpleNamespace(owner=owner, native=native, dispatched=dispatched, frame=frame, manager=manager)

def event(identity=0x4200):
    return SimpleNamespace(GetId=lambda: identity)


def test_hotkeys_require_explicit_enable_and_dispatch_semantic_identity_only():
    e = hotkeys()
    assert e.native.calls == [] and not e.owner.enabled
    e.owner._event(event())
    assert e.dispatched == []
    e.owner.enable({'voice.clipboard': 'Ctrl+Alt+Shift+K'})
    e.owner._event(event())
    assert e.dispatched == ['voice.clipboard']
    e.owner.disable()
    e.owner._event(event())
    assert not e.native.keys and e.dispatched == ['voice.clipboard']
    e.owner.close()
    e.frame.Unbind.assert_called_once()


@pytest.mark.parametrize('bindings', [
    {'voice.reader': 'Ctrl+Alt+V'}, {'voice.reader': 'F7'}, {'voice.reader': 'Ctrl+Shift+F12'},
    {'voice.reader': 'Ctrl+Shift+Q'}, {'unknown.action': 'Ctrl+Shift+K'},
    {'voice.reader': 'Ctrl+Shift+K', 'voice.stop': 'Ctrl+Shift+K'},
])
def test_invalid_or_conflicting_global_assignments_do_not_register_any_native_key(bindings):
    e = hotkeys()
    with pytest.raises(ValueError):
        e.owner.enable(bindings)
    assert e.native.calls == [] and not e.owner.enabled


def test_unsupported_platform_is_visible_and_performs_no_registration():
    e = hotkeys(platform='linux')
    with pytest.raises(ValueError, match='Windows'):
        e.owner.enable({'voice.reader': 'Ctrl+Shift+K'})
    assert not e.native.calls


def test_partial_native_registration_conflict_releases_only_owned_new_keys():
    e = hotkeys()
    e.native.fail_at = 1
    with pytest.raises(ValueError, match='another application'):
        e.owner.enable({'voice.reader': 'Ctrl+Shift+K', 'voice.stop': 'Ctrl+Shift+J'})
    assert not e.native.keys and not e.owner.enabled
    assert e.native.calls[-1] == ('release', 0x4200)
    e.owner._event(event())
    assert not e.dispatched


def test_unconfirmed_release_retains_exact_retry_ownership_without_dispatch():
    e = hotkeys()
    e.owner.enable({'voice.reader': 'Ctrl+Shift+K'})
    e.native.refuse_release = True
    with pytest.raises(RuntimeError, match='confirm'):
        e.owner.disable()
    assert e.owner._registered and e.native.keys and not e.owner.enabled
    with pytest.raises(ValueError, match='Disable'):
        e.owner.enable({'voice.reader': 'Ctrl+Shift+J'})
    e.owner._event(event())
    assert not e.dispatched
    e.native.refuse_release = False
    e.owner.disable()
    assert not e.owner._registered and not e.native.keys


def test_changed_local_assignment_and_reentrant_modal_event_cannot_dispatch_stale_action():
    e = hotkeys()
    e.owner.enable({'voice.reader': 'Ctrl+Shift+K'})
    def modal(identity):
        e.dispatched.append(identity)
        e.owner._event(event())
    e.owner.dispatch = modal
    e.owner._event(event())
    assert e.dispatched == ['voice.reader']
    e.manager.manager.overrides = {'Other/Another action': 'Ctrl+Shift+K'}
    e.owner._event(event())
    assert e.dispatched == ['voice.reader'] and not e.native.keys and not e.owner.enabled
    e.frame.report.assert_called_once()


class Timer:
    def __init__(self, parent): self.running = False
    def Start(self, milliseconds): self.running = True; return True
    def Stop(self): self.running = False


class Event:
    def __init__(self, kind, identity): self.kind, self.identity = kind, identity
    def SetEventObject(self, owner): self.owner = owner


def desktop(tmp_path):
    invoked, reports = [], []
    manager = hotkeys().frame.keyboard_actions
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), active=('workspace', None),
        _closing=False, working=False, report=reports.append, keyboard_actions=manager,
        Bind=Mock(), Unbind=Mock(), IsEnabled=lambda: True, GetId=lambda: 73)
    def received(event):
        binding, = [binding for binding in manager.bindings
                    if binding.item.GetId() == event.identity]
        alias, = [name for name, path in MENU_PATHS.items() if path == binding.command.identity]
        invoked.append(alias)
        return True
    frame.GetEventHandler = lambda: SimpleNamespace(ProcessEvent=received)
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', EVT_HOTKEY='hotkey', EVT_WINDOW_DESTROY='destroy',
                         EVT_BUTTON=SimpleNamespace(typeId='button'), EVT_MENU=SimpleNamespace(typeId='menu'),
                         CommandEvent=Event)
    owner = DesktopIntegration(frame, wx_module=wx)
    owner._read = Mock(return_value='Never used unless enabled')
    owner.watch.read = owner._read
    return SimpleNamespace(owner=owner, frame=frame, wx=wx, invoked=invoked, reports=reports)

def test_controller_never_reads_on_open_or_after_close_and_clears_pending_before_root_change(tmp_path):
    e = desktop(tmp_path)
    assert not e.owner.timer.running and not e.owner.watch.enabled and not e.owner.hotkeys.enabled
    e.owner._tick()
    e.owner._read.assert_not_called()
    e.owner.watch_clipboard(True); e.owner._tick()
    assert e.owner.watch.pending
    e.frame.library = SimpleNamespace(root=tmp_path / 'other')
    e.owner._tick()
    assert e.owner.watch.pending is None and not e.owner.timer.running and not e.owner.watch.enabled
    e.owner.close()
    count = e.owner._read.call_count
    e.owner._tick()
    assert e.owner._read.call_count == count and not e.owner.available('voice.clipboard')
    e.owner.close()
    assert e.frame.Unbind.call_count == 3


def test_watch_suspends_in_modals_and_jobs_then_requires_explicit_review(tmp_path, monkeypatch):
    from carr_library.ui import speech_dialog
    e = desktop(tmp_path)
    opened = []
    monkeypatch.setattr(speech_dialog, 'show_speech_reader', lambda parent, **options: opened.append(options))
    e.owner.watch_clipboard(True)
    e.frame.IsEnabled = lambda: False
    e.owner._tick(); e.owner._read.assert_not_called()
    e.frame.IsEnabled = lambda: True
    e.frame.working = True
    e.owner._tick(); e.owner._read.assert_not_called()
    e.frame.working = False
    e.owner._tick()
    assert not opened
    e.owner.open_watched()
    assert opened == [{'text': 'Never used unless enabled', 'title': 'Watched clipboard reading copy',
                       'recovery_slot': 'clipboard-watch'}]
    assert not list(tmp_path.rglob('*'))  # No automatic durable clipboard history.


def test_reader_global_controls_dispatch_exact_buttons_then_detach_before_native_destroy(tmp_path):
    e = desktop(tmp_path)
    native = []
    def button(identity):
        value = SimpleNamespace(IsEnabled=lambda: True, GetId=lambda: identity)
        def received(event):
            assert event.kind == 'button' and event.identity == identity and event.owner is value
            native.append(identity); return True
        value.GetEventHandler = lambda: SimpleNamespace(ProcessEvent=received)
        return value
    alive = [True]
    dialog = SimpleNamespace(IsEnabled=lambda: True, GetId=lambda: 91, Bind=Mock())
    pause, stop = button(10), button(11)
    detach = e.owner.attach_reader(dialog, pause, stop, lambda: alive[0])
    assert e.owner.invoke('voice.pause') and e.owner.invoke('voice.stop')
    assert native == [10, 11] and not e.invoked
    alive[0] = False
    assert not e.owner.invoke('voice.stop')
    detach(); detach()
    assert e.owner.reader is None and not e.owner.invoke('voice.pause')
    e.owner.close()


def test_new_scope_cannot_use_previous_readers_buttons(tmp_path):
    e = desktop(tmp_path)
    button = SimpleNamespace(IsEnabled=lambda: pytest.fail('Stale native control was touched.'))
    e.owner.attach_reader(SimpleNamespace(IsEnabled=lambda: True, GetId=lambda: 91, Bind=Mock()), button, button, lambda: True)
    e.frame.active = ('another-workspace', None)
    assert not e.owner.available('voice.stop')
    e.owner.close()


def native_hotkey_api(monkeypatch):
    import ctypes
    properties = {}
    def put(hwnd, name, value):
        properties[hwnd, name] = value
        return 1
    native = SimpleNamespace(RegisterHotKey=Mock(return_value=1),
        UnregisterHotKey=Mock(return_value=1), SetPropW=Mock(side_effect=put),
        GetPropW=Mock(side_effect=lambda hwnd, name: properties.get((hwnd, name))),
        RemovePropW=Mock(side_effect=lambda hwnd, name: properties.pop((hwnd, name), None)))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: native, raising=False)
    return native, properties


def test_win32_adapter_requires_exact_window_and_retains_native_no_repeat(monkeypatch):
    import ctypes
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, properties = native_hotkey_api(monkeypatch)
    handle = [123]
    owner = WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: handle[0]))
    assert owner.register(0x4200, 'Ctrl+Alt+Shift+F11')
    native.RegisterHotKey.assert_called_once_with(123, 0x4200, 0x4007, 0x7a)
    assert owner.unregister(0x4200)
    native.UnregisterHotKey.assert_called_once_with(123, 0x4200)
    handle[0] = 0
    properties.clear()  # The native HWND is gone/reused, not merely a Python wrapper.
    with pytest.raises(ValueError, match='owner changed'):
        owner.register(0x4200, 'Ctrl+Shift+K')
    with pytest.raises(ValueError, match='owner changed'):
        owner.unregister(0x4200)
    assert native.RegisterHotKey.call_count == native.UnregisterHotKey.call_count == 1


def test_direct_native_destroy_cleans_only_the_actual_owner_and_drops_late_timer(tmp_path):
    e = desktop(tmp_path)
    e.owner.watch_clipboard(True)
    child = SimpleNamespace(GetId=lambda: 74, Skip=Mock())
    e.owner._destroyed(child)
    assert not e.owner.closed and e.owner.timer.running
    actual = SimpleNamespace(GetId=e.frame.GetId, Skip=Mock())
    e.owner._destroyed(actual)
    assert e.owner.closed and not e.owner.timer.running
    e.owner.timer.Stop = lambda: pytest.fail('Late callback touched a destroyed native timer.')
    e.owner._tick()
    actual.Skip.assert_called_once()


def test_tray_uses_semantic_actions_and_uncertain_removal_is_visible_and_retryable(tmp_path, monkeypatch):
    import sys
    from types import ModuleType
    e = desktop(tmp_path)
    shown = [True]
    e.frame.IsShown = lambda: shown[0]
    e.frame.Show = lambda value: shown.__setitem__(0, value)
    e.frame.Hide = lambda: shown.__setitem__(0, False)
    e.frame.Iconize, e.frame.Raise = Mock(), Mock()
    e.frame.GetIcon = lambda: SimpleNamespace(IsOk=lambda: True)
    class Menu:
        def __init__(self): self.items, self.handlers = [], []
        def Append(self, identity, label):
            item = SimpleNamespace(label=label, Enable=Mock())
            self.items.append(item); return item
        def Bind(self, event, handler, item): self.handlers.append(handler)
    class Tray:
        IsAvailable = staticmethod(lambda: True)
        def __init__(self): self.installed, self.destroyed, self.refuse = False, False, False
        def SetIcon(self, icon, tooltip): self.installed = True; return True
        def IsIconInstalled(self): return self.installed
        def RemoveIcon(self):
            if self.refuse: return False
            self.installed = False; return True
        def Bind(self, *args, **kwargs): pass
        def Destroy(self):
            assert not self.destroyed
            self.destroyed = True
    wx, adv = ModuleType('wx'), ModuleType('wx.adv')
    wx.adv, wx.Menu, wx.ID_ANY, wx.EVT_MENU = adv, Menu, -1, 'menu'
    adv.TaskBarIcon, adv.EVT_TASKBAR_LEFT_DCLICK = Tray, 'double-click'
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setitem(sys.modules, 'wx.adv', adv)
    e.owner.wx = wx
    e.owner.set_tray(True)
    icon = e.owner.tray
    menu = icon.CreatePopupMenu()
    menu.handlers[1](None)
    assert e.invoked == ['voice.clipboard']
    e.owner.hide_window()
    assert not shown[0]
    icon.refuse = True
    with pytest.raises(RuntimeError, match='confirm removal'):
        e.owner.set_tray(False)
    assert shown[0] and e.owner.tray is icon and not icon.active and not icon.destroyed
    menu.handlers[1](None)
    assert e.invoked == ['voice.clipboard']
    icon.refuse = False
    e.owner.set_tray(False)
    assert e.owner.tray is None and icon.destroyed
    e.owner.wx = e.wx
    e.owner.close()


def test_real_command_inventory_replacement_is_observed_without_cached_bindings(tmp_path):
    e = desktop(tmp_path)
    assert e.owner.invoke('voice.clipboard') and e.invoked == ['voice.clipboard']
    old = e.frame.keyboard_actions
    remaining = [row for row in old.bindings if row.command.identity != MENU_PATHS['voice.clipboard']]
    e.frame.keyboard_actions = SimpleNamespace(bindings=remaining,
        commands=[row.command for row in remaining], overrides={})
    assert not e.owner.available('voice.clipboard')
    assert not e.owner.invoke('voice.clipboard')
    assert e.invoked == ['voice.clipboard']


def test_changed_scope_revokes_global_delivery_before_another_window_can_run_it():
    e = hotkeys()
    e.owner.enable({'voice.reader': 'Ctrl+Shift+K'})
    e.frame.active = ('another-workspace', None)
    e.owner._event(event())
    assert not e.dispatched and not e.owner.enabled and not e.native.keys


def test_failed_global_close_keeps_exact_keys_retryable_and_detaches_events_once():
    e = hotkeys()
    e.owner.enable({'voice.reader': 'Ctrl+Shift+K'})
    e.native.refuse_release = True
    with pytest.raises(RuntimeError, match='confirm'):
        e.owner.close()
    assert e.owner.closed and e.owner._registered and e.native.keys
    e.owner._event(event())
    assert not e.dispatched
    e.native.refuse_release = False
    e.owner.close()
    assert not e.owner._registered and not e.native.keys
    e.frame.Unbind.assert_called_once()


def test_failed_tray_close_retains_native_owner_until_acknowledged_retry(tmp_path):
    e = desktop(tmp_path)
    permitted = [False]
    tray = SimpleNamespace(active=True, IsIconInstalled=lambda: True,
        RemoveIcon=lambda: permitted[0], Destroy=Mock())
    e.owner.tray = tray
    with pytest.raises(RuntimeError, match='tray-icon removal'):
        e.owner.close()
    assert e.owner.closed and e.owner.tray is tray and not tray.active
    tray.Destroy.assert_not_called()
    permitted[0] = True
    e.owner.close()
    assert e.owner.tray is None
    tray.Destroy.assert_called_once()
    assert e.frame.Unbind.call_count == 3


def test_reader_native_destroy_revokes_buttons_before_late_global_dispatch(tmp_path):
    e = desktop(tmp_path)
    hooks = {}
    dialog = SimpleNamespace(IsEnabled=lambda: True, GetId=lambda: 91,
        Bind=lambda event, handler: hooks.__setitem__(event, handler))
    control = SimpleNamespace(IsEnabled=lambda: pytest.fail('Destroyed button was touched.'))
    e.owner.attach_reader(dialog, control, control, lambda: True)
    hooks['destroy'](SimpleNamespace(GetId=lambda: 91, Skip=Mock()))
    assert e.owner.reader is None
    assert not e.owner.invoke('voice.stop')
    e.owner.close()


def test_native_destroy_never_calls_through_an_invalidated_frame_wrapper(tmp_path):
    e = desktop(tmp_path)
    native = Native()
    e.owner.hotkeys.platform, e.owner.hotkeys.native = 'win32', native
    e.owner.hotkeys.enable({'voice.reader': 'Ctrl+Shift+K'})
    e.owner.watch_clipboard(True)
    e.owner._tick()
    e.frame.GetId = Mock(side_effect=RuntimeError('Deleted wx wrapper'))
    e.frame.Unbind = Mock(side_effect=RuntimeError('Deleted wx wrapper'))
    destroyed = SimpleNamespace(GetId=lambda: 73, Skip=Mock())
    e.owner._destroyed(destroyed)
    assert e.owner.closed and e.owner.hotkeys.closed
    assert not native.keys and not e.owner.hotkeys._registered
    assert not e.owner.timer.running and e.owner.watch.pending is None
    assert not hasattr(e.owner, 'cleanup_error')
    e.frame.GetId.assert_not_called()
    e.frame.Unbind.assert_not_called()
    e.owner.close()
    e.frame.Unbind.assert_not_called()
    e.owner._tick()
    e.owner.hotkeys._event(event())
    assert not e.invoked
    destroyed.Skip.assert_called_once()


def test_deleted_wrapper_release_uses_exact_native_marker_then_removes_only_it(monkeypatch):
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, properties = native_hotkey_api(monkeypatch)
    frame = SimpleNamespace(GetHandle=Mock(return_value=123))
    owner = WindowsHotkeyAPI(frame)
    marker = owner._owner_key
    properties[123, 'another-owner'] = 9
    assert owner.register(0x4200, 'Ctrl+Alt+Shift+K')
    frame.GetHandle = Mock(side_effect=RuntimeError('Deleted wx wrapper'))
    assert owner.unregister(0x4200)
    owner.close()
    frame.GetHandle.assert_not_called()
    native.UnregisterHotKey.assert_called_once_with(123, 0x4200)
    native.RemovePropW.assert_called_once_with(123, marker)
    assert properties == {(123, 'another-owner'): 9}
    assert not owner._keys and not owner._owner_attached
    owner.close()
    native.RemovePropW.assert_called_once()


@pytest.mark.parametrize('replacement', [None, 2], ids=['missing-marker', 'different-marker'])
def test_reused_hwnd_never_receives_old_owners_unregister_or_property_removal(monkeypatch, replacement):
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, properties = native_hotkey_api(monkeypatch)
    owner = WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: 123))
    assert owner.register(0x4200, 'Ctrl+Shift+K')
    if replacement is None:
        properties.clear()
    else:
        properties[123, owner._owner_key] = replacement
    before = dict(properties)
    with pytest.raises(ValueError, match='owner changed'):
        owner.unregister(0x4200)
    with pytest.raises(ValueError, match='owner changed'):
        owner.close()
    native.UnregisterHotKey.assert_not_called()
    native.RemovePropW.assert_not_called()
    assert owner._keys == {0x4200} and owner._owner_attached
    assert properties == before


def test_native_marker_admission_failure_never_registers_any_key(monkeypatch):
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, properties = native_hotkey_api(monkeypatch)
    native.SetPropW.side_effect = lambda *_args: 0
    with pytest.raises(RuntimeError, match='owner marker'):
        WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: 123))
    assert properties == {}
    native.RegisterHotKey.assert_not_called()
    native.UnregisterHotKey.assert_not_called()


def test_failed_marker_release_remains_retryable_after_keys_are_confirmed_free(monkeypatch):
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, properties = native_hotkey_api(monkeypatch)
    owner = WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: 123))
    assert owner.register(0x4200, 'Ctrl+Shift+K')
    assert owner.unregister(0x4200)
    remove = native.RemovePropW.side_effect
    native.RemovePropW.side_effect = lambda *_args: None
    with pytest.raises(RuntimeError, match='owner marker'):
        owner.close()
    assert owner._owner_attached and not owner._keys and properties
    native.RemovePropW.side_effect = remove
    owner.close()
    assert not owner._owner_attached and not properties
    native.UnregisterHotKey.assert_called_once()


def test_native_owner_rejects_cross_thread_release_before_any_mutating_call(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native, _properties = native_hotkey_api(monkeypatch)
    owner = WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: 123))
    assert owner.register(0x4200, 'Ctrl+Shift+K')
    with ThreadPoolExecutor(max_workers=1) as worker:
        with pytest.raises(RuntimeError, match='original UI thread'):
            worker.submit(owner.unregister, 0x4200).result(5)
    native.UnregisterHotKey.assert_not_called()
    assert owner.unregister(0x4200)
    owner.close()


def test_direct_destroy_retains_unconfirmed_keys_without_dying_wrapper_calls(tmp_path):
    e = desktop(tmp_path)
    native = Native()
    e.owner.hotkeys.platform, e.owner.hotkeys.native = 'win32', native
    e.owner.hotkeys.enable({'voice.reader': 'Ctrl+Shift+K'})
    native.refuse_release = True
    e.frame.Unbind = Mock(side_effect=RuntimeError('Deleted wx wrapper'))
    e.owner._destroyed(SimpleNamespace(GetId=lambda: 73, Skip=Mock()))
    assert e.owner.closed and e.owner.hotkeys.closed and e.owner.hotkeys._registered
    assert 'confirm shortcut release' in e.owner.cleanup_error
    e.owner.hotkeys._event(event())
    assert not e.invoked
    native.refuse_release = False
    e.owner.close()
    assert not e.owner.hotkeys._registered and not native.keys
    e.frame.Unbind.assert_not_called()
