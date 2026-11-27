"""Real controller boundaries with explicit native stand-ins; not Windows evidence."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui.command_registry import CommandRegistry, CommandSpec
from carr_library.ui.desktop_integration import ClipboardWatch, DesktopIntegration, GLOBAL_COMMANDS, GlobalHotkeys


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
    registry = CommandRegistry([CommandSpec(name, name) for name in GLOBAL_COMMANDS]
                               + [CommandSpec('another.action', 'Another action', default_shortcut='Ctrl+Shift+Q')])
    manager = SimpleNamespace(registry=registry, bindings={name: object() for name in GLOBAL_COMMANDS}, overrides={})
    frame = SimpleNamespace(Bind=Mock(), Unbind=Mock(), report=Mock(), _closing=False)
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
    e.manager.overrides = {'another.action': 'Ctrl+Shift+K'}
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
    manager = hotkeys().manager
    manager.available = lambda identity: identity in manager.bindings
    manager.invoke = lambda identity: invoked.append(identity) or True
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), active=('workspace', None),
        _closing=False, working=False, report=reports.append, keyboard_commands=manager,
        Bind=Mock(), Unbind=Mock(), IsEnabled=lambda: True,
        GetId=lambda: -31994, GetHandle=lambda: 123)
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', EVT_HOTKEY='hotkey', EVT_WINDOW_DESTROY='destroy',
                         EVT_BUTTON=SimpleNamespace(typeId='button'), CommandEvent=Event)
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
    dialog = SimpleNamespace(IsEnabled=lambda: True)
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
    e.owner.attach_reader(SimpleNamespace(IsEnabled=lambda: True), button, button, lambda: True)
    e.frame.active = ('another-workspace', None)
    assert not e.owner.available('voice.stop')
    e.owner.close()


def test_win32_adapter_requires_exact_window_and_retains_native_no_repeat(monkeypatch):
    import ctypes
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI
    native = SimpleNamespace(RegisterHotKey=Mock(return_value=1), UnregisterHotKey=Mock(return_value=1))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: native, raising=False)
    handle = [123]
    owner = WindowsHotkeyAPI(SimpleNamespace(GetHandle=lambda: handle[0]))
    assert owner.register(0x4200, 'Ctrl+Alt+Shift+F11')
    native.RegisterHotKey.assert_called_once_with(123, 0x4200, 0x4007, 0x7a)
    assert owner.unregister(0x4200)
    native.UnregisterHotKey.assert_called_once_with(123, 0x4200)
    handle[0] = 0
    with pytest.raises(ValueError, match='owner changed'):
        owner.register(0x4200, 'Ctrl+Shift+K')
    with pytest.raises(ValueError, match='owner changed'):
        owner.unregister(0x4200)
    assert native.RegisterHotKey.call_count == native.UnregisterHotKey.call_count == 1


def test_direct_native_destroy_cleans_only_the_actual_owner_and_drops_late_timer(tmp_path):
    e = desktop(tmp_path)
    e.owner.watch_clipboard(True)
    child = SimpleNamespace(GetId=lambda: -31993, GetEventObject=lambda: object(), Skip=Mock())
    e.owner._destroyed(child)
    assert not e.owner.closed and e.owner.timer.running
    actual = SimpleNamespace(GetId=lambda: -31994, GetEventObject=lambda: e.frame, Skip=Mock())
    e.owner._destroyed(actual)
    assert e.owner.closed and not e.owner.timer.running
    e.owner.timer.Stop = lambda: pytest.fail('Late callback touched a destroyed native timer.')
    e.owner._tick()
    actual.Skip.assert_called_once()


def test_tray_uses_semantic_actions_and_uncertain_removal_is_visible_and_retryable(tmp_path, monkeypatch):
    import sys
    from types import ModuleType
    e = desktop(tmp_path)
    e.frame.keyboard_commands.registry.register(CommandSpec('desktop.settings', 'Desktop integration'))
    e.frame.keyboard_commands.bindings['desktop.settings'] = object()
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


def test_native_probe_observes_frame_id_before_deleted_python_wrapper():
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_desktop_integration_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    observer, = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == 'frame_destroyed']
    module = ast.fix_missing_locations(ast.Module(body=[observer], type_ignores=[]))
    destroyed, skipped = [], []
    environment = dict(native_frame_id=-31994, destroyed=destroyed)
    exec(compile(module, str(script), 'exec'), environment)

    class Event:
        def __init__(self, identity): self.identity = identity
        def GetId(self): return self.identity
        def GetEventObject(self):
            pytest.fail('Deleted native owner identity must not depend on its Python wrapper.')
        def Skip(self): skipped.append(self.identity)

    environment['frame_destroyed'](Event(-31993))
    assert not destroyed
    environment['frame_destroyed'](Event(-31994))
    assert destroyed == [True] and skipped == [-31993, -31994]
    assignment, = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'native_frame_id' for target in node.targets)]
    bind, = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'Bind'
        and any(isinstance(arg, ast.Name) and arg.id == 'frame_destroyed' for arg in node.args)]
    assert assignment.lineno < bind.lineno and ast.unparse(assignment.value) == 'frame.GetId()'


@pytest.mark.parametrize('event_seen, peer_alive', [(False, False), (False, True), (True, True), (True, False)])
def test_native_probe_close_wait_requires_both_event_and_dead_original_peer(event_seen, peer_alive):
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_desktop_integration_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in ('frame_is_destroyed', 'pump')]
    assert {node.name for node in functions} == {'frame_is_destroyed', 'pump'}
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    clock, yields = [0.0], []

    class Peer:
        def __bool__(self): return peer_alive
        def Close(self): pytest.fail('Observation cannot force Close.')
        def Destroy(self): pytest.fail('Observation cannot force Destroy.')

    def sleep(seconds):
        assert seconds == .01
        clock[0] += seconds

    environment = dict(frame=Peer(), destroyed=[True] if event_seen else [], errors=[],
        app=SimpleNamespace(Yield=lambda: yields.append(True)),
        time=SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    exec(compile(module, str(script), 'exec'), environment)
    predicate = environment['frame_is_destroyed']
    assert predicate() is (event_seen and not peer_alive)
    if event_seen and not peer_alive:
        environment['pump'](predicate, 'owned native close')
        assert not yields and clock[0] == 0
    else:
        with pytest.raises(TimeoutError, match='owned native close'):
            environment['pump'](predicate, 'owned native close')
        assert 12 <= clock[0] < 12.02 and yields
    labels = {'Native DesktopFrame did not acknowledge destruction.', 'Failure-path desktop cleanup did not destroy the frame.'}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'pump'
             and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
             and node.args[1].value in labels]
    assert len(calls) == 2
    assert all(isinstance(call.args[0], ast.Name) and call.args[0].id == 'frame_is_destroyed' for call in calls)


class CleanupTray:
    def __init__(self):
        self.active, self.installed, self.alive = True, True, True
        self.refuse_remove, self.refuse_destroy = False, False
        self.removals = self.destroys = 0

    def __bool__(self):
        return self.alive

    def IsIconInstalled(self):
        assert self.alive
        return self.installed

    def RemoveIcon(self):
        assert self.alive
        self.removals += 1
        if self.refuse_remove:
            return False
        self.installed = False
        return True

    def Destroy(self):
        assert self.alive and not self.installed
        self.destroys += 1
        if self.refuse_destroy:
            raise RuntimeError('tray destruction unconfirmed')
        self.alive = False


class DeletedDesktopPeer:
    def __init__(self):
        self.native_calls = []

    def __bool__(self):
        return False

    def GetId(self):
        self.native_calls.append('id')
        raise RuntimeError('original native frame already deleted')

    def GetHandle(self):
        self.native_calls.append('handle')
        raise RuntimeError('original native frame already deleted')

    def Unbind(self, *args, **kwargs):
        self.native_calls.append('unbind')
        raise RuntimeError('original native frame already deleted')


def cleanup_window(tmp_path, monkeypatch):
    import ctypes
    from carr_library.ui.desktop_integration import WindowsHotkeyAPI

    value = desktop(tmp_path)
    api = SimpleNamespace(RegisterHotKey=Mock(return_value=1),
                          UnregisterHotKey=Mock(return_value=1))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: api, raising=False)
    value.owner.hotkeys.platform = 'win32'
    value.owner.hotkeys.native = WindowsHotkeyAPI(value.frame)
    value.owner.hotkeys.enable({'desktop.show': 'Ctrl+Alt+Shift+K'})
    value.owner.watch_clipboard(True)
    value.owner._tick()
    value.owner.tray = CleanupTray()
    value.api = api
    return value


def replace_deleted_peer(value, *, handle=123):
    dead = DeletedDesktopPeer()
    proxy = SimpleNamespace(GetId=lambda: -31994, GetHandle=lambda: handle, Unbind=Mock())
    value.owner.frame = value.owner.hotkeys.frame = value.owner.hotkeys.native.frame = dead
    native_event = SimpleNamespace(GetId=lambda: -31994, GetEventObject=lambda: proxy, Skip=Mock())
    return dead, proxy, native_event


def test_native_destroy_uses_current_base_proxy_after_original_frame_wrapper_is_dead(tmp_path, monkeypatch):
    value = cleanup_window(tmp_path, monkeypatch)
    tray = value.owner.tray
    value.owner.reader, value.owner._assistant_reader = object(), object()
    dead, proxy, native_event = replace_deleted_peer(value)

    value.owner._destroyed(native_event)

    assert value.owner.closed and value.owner._cleanup_complete
    assert value.owner.reader is value.owner._assistant_reader is None
    assert value.owner.watch.closed and not value.owner.watch.enabled
    assert value.owner.watch.pending is None and not value.owner.timer.running
    assert not value.owner.hotkeys._registered and value.owner.hotkeys._cleanup_complete
    value.api.UnregisterHotKey.assert_called_once_with(123, 0x4200)
    assert not tray.active and not tray.alive and value.owner.tray is None
    assert tray.removals == tray.destroys == 1
    assert [call.args[0] for call in proxy.Unbind.call_args_list] == ['timer', 'hotkey', 'destroy']
    assert dead.native_calls == [] and not getattr(value.owner, 'cleanup_error', None)
    native_event.Skip.assert_called_once()
    calls = value.owner._read.call_count
    value.owner._tick()
    value.owner.hotkeys._event(event())
    assert value.owner._read.call_count == calls and not value.invoked
    value.owner.close()
    assert value.api.UnregisterHotKey.call_count == 1 and tray.destroys == 1


def test_failed_close_attempts_every_owner_and_real_destroy_can_retry_retained_resources(tmp_path, monkeypatch):
    value = cleanup_window(tmp_path, monkeypatch)
    tray = value.owner.tray
    tray.refuse_remove = True
    value.api.UnregisterHotKey.return_value = 0
    stop = value.owner.timer.Stop
    stop_error = RuntimeError('timer stop unconfirmed')
    value.owner.timer.Stop = Mock(side_effect=stop_error)
    value.frame.Unbind.side_effect = RuntimeError('native unbind unconfirmed')

    with pytest.raises(RuntimeError) as caught:
        value.owner.close()

    message = str(caught.value)
    assert caught.value.__cause__ is stop_error
    assert 'timer stop unconfirmed' in message and 'native unbind unconfirmed' in message
    assert 'confirm shortcut release' in message and 'confirm tray-icon removal' in message
    assert value.owner.cleanup_error == message
    assert value.owner.closed and not value.owner._cleanup_complete
    assert value.owner.watch.closed and value.owner.watch.pending is None
    value.owner.timer.Stop.assert_called_once()
    value.api.UnregisterHotKey.assert_called_once_with(123, 0x4200)
    assert [call.args[0] for call in value.frame.Unbind.call_args_list] == ['timer', 'hotkey']
    assert value.owner._destroy_bound and value.owner.hotkeys._registered == {0x4200: 'desktop.show'}
    assert value.owner.tray is tray and not tray.active and tray.alive
    assert tray.removals == 1 and tray.destroys == 0
    value.owner.hotkeys._event(event())
    assert not value.invoked

    value.owner.timer.Stop = stop
    value.api.UnregisterHotKey.return_value = 1
    tray.refuse_remove = False
    dead, proxy, native_event = replace_deleted_peer(value)
    value.owner._destroyed(native_event)
    assert value.owner._cleanup_complete and not value.owner._destroy_bound
    assert not value.owner.timer.running and value.owner.hotkeys._registered == {}
    assert value.owner.tray is None and not tray.alive
    assert tray.removals == 2 and tray.destroys == 1
    assert value.api.UnregisterHotKey.call_count == 2 and dead.native_calls == []
    assert [call.args[0] for call in proxy.Unbind.call_args_list] == ['timer', 'hotkey', 'destroy']
    assert value.owner.cleanup_error == message  # The observed first failure is not erased.


def test_global_key_close_preserves_release_and_unbind_errors_before_retry():
    value = hotkeys()
    value.owner.enable({'desktop.show': 'Ctrl+Alt+Shift+K'})
    value.native.refuse_release = True
    value.frame.Unbind.side_effect = RuntimeError('hotkey binding still owned')

    with pytest.raises(RuntimeError) as caught:
        value.owner.close()

    assert 'confirm shortcut release' in str(caught.value)
    assert 'hotkey binding still owned' in str(caught.value)
    assert value.owner.closed and not value.owner.enabled and not value.owner._cleanup_complete
    assert value.owner._registered == {0x4200: 'desktop.show'} and value.owner._bound
    value.owner._event(event())
    assert not value.dispatched
    value.native.refuse_release = False
    value.frame.Unbind.side_effect = None
    value.owner.close()
    assert not value.native.keys and not value.owner._registered and not value.owner._bound
    assert value.owner._cleanup_complete and value.frame.Unbind.call_count == 2
    value.owner.close()
    assert value.frame.Unbind.call_count == 2


def test_destroy_proxy_with_wrong_hwnd_cannot_release_a_different_window(tmp_path, monkeypatch):
    value = cleanup_window(tmp_path, monkeypatch)
    tray = value.owner.tray
    dead, proxy, native_event = replace_deleted_peer(value, handle=456)

    value.owner._destroyed(native_event)

    assert value.owner.closed and not value.owner._cleanup_complete
    assert 'destruction owner changed' in value.owner.cleanup_error
    value.api.UnregisterHotKey.assert_not_called()
    proxy.Unbind.assert_not_called()
    assert value.owner.hotkeys._registered == {0x4200: 'desktop.show'}
    assert value.owner.watch.closed and value.owner.watch.pending is None
    assert not value.owner.timer.running and not tray.alive and value.owner.tray is None
    assert dead.native_calls and all(call in ('handle', 'unbind') for call in dead.native_calls)
    native_event.Skip.assert_called_once()


def test_tray_destroy_failure_does_not_prevent_key_release_or_discard_retry_owner(tmp_path, monkeypatch):
    value = cleanup_window(tmp_path, monkeypatch)
    tray = value.owner.tray
    tray.refuse_destroy = True

    with pytest.raises(RuntimeError, match='tray destruction unconfirmed'):
        value.owner.close()

    assert not value.owner.hotkeys._registered and value.owner.hotkeys._cleanup_complete
    assert value.owner.tray is tray and tray.alive and not tray.installed and not tray.active
    assert value.owner._destroy_bound and not value.owner._cleanup_complete
    assert tray.removals == 1 and tray.destroys == 1
    tray.refuse_destroy = False
    value.owner.close()
    assert value.owner._cleanup_complete and value.owner.tray is None and not tray.alive
    assert tray.removals == 1 and tray.destroys == 2
    value.api.UnregisterHotKey.assert_called_once_with(123, 0x4200)


def test_missing_destroy_proxy_still_attempts_owned_cleanup_and_preserves_error(tmp_path, monkeypatch):
    value = cleanup_window(tmp_path, monkeypatch)
    native_event = SimpleNamespace(GetId=lambda: -31994,
        GetEventObject=Mock(side_effect=RuntimeError('native event owner unavailable')), Skip=Mock())

    value.owner._destroyed(native_event)

    assert value.owner.closed and value.owner._cleanup_complete
    assert value.owner.watch.pending is None and not value.owner.timer.running
    assert value.owner.hotkeys._registered == {} and value.owner.tray is None
    assert value.owner.cleanup_error == 'Native destruction owner: native event owner unavailable'
    native_event.Skip.assert_called_once()
