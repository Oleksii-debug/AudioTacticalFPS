"""Session ownership and native-adapter failure boundaries; no real OS-key claims."""
from dataclasses import replace
from types import SimpleNamespace
import sys

import pytest

from carr_library.ui import reader_background as module
from carr_library.ui.reader_background import ReaderSessionControls, SessionOptions, SessionSlot


class Dialog:
    def __init__(self):
        self.alive = True
        self.calls = []
        self.iconized = False
    def __bool__(self):
        return self.alive
    def Show(self):
        assert self.alive
        self.calls.append('show')
    def IsIconized(self):
        assert self.alive
        return self.iconized
    def Iconize(self, value):
        assert self.alive
        self.iconized = value
        self.calls.append('restore')
    def Raise(self):
        assert self.alive
        self.calls.append('raise')


class Controls:
    def __init__(self):
        self.dialog = Dialog()
        self.core = SimpleNamespace(busy=False, status=lambda: SimpleNamespace(state='idle'))
        self.closed = False
        self._start_generation = 0
        self._host = SimpleNamespace()
        self.calls = []
    def pause(self):
        self.calls.append('pause')
    def stop(self):
        self._start_generation += 1
        self.calls.append('stop')
    def _report(self, value):
        self.calls.append(('report', str(value)))


class Native:
    def __init__(self, owner, epoch):
        self.owner, self.epoch = owner, epoch
        self.started = []
        self.released = 0
        self.confirmed = True
        self.on_start = lambda: None
    def start(self, options):
        self.started.append(options)
        self.on_start()
    def release(self):
        self.released += 1
        return self.confirmed


def session(controls=None, slot=None):
    controls, slot = controls or Controls(), slot or SessionSlot()
    created = []
    def factory(owner, epoch):
        native = Native(owner, epoch)
        created.append(native)
        return native
    owner = ReaderSessionControls(controls, slot, factory)
    return controls, slot, owner, created


@pytest.mark.parametrize('shortcut', [
    'F7', 'Ctrl+K', 'Shift+F7', 'Alt+K', 'Win+K', 'Ctrl+Alt+F12',
    'Ctrl+Alt+Delete', 'Ctrl+Shift+C', 'Ctrl+Alt+Insert', 'Ctrl+Alt+CapsLock',
    'Ctrl+Alt+Tab', 'Ctrl+Alt+Escape', 'Ctrl+Alt+PageDown', 'Ctrl++Q',
    'Ctrl+Ctrl+Alt+Q', 'Ctrl+Alt+Q\n', 'Ctrl+Alt+F24',
])
def test_global_key_selection_keeps_native_and_os_keys_unavailable(shortcut):
    with pytest.raises(ValueError):
        SessionOptions(show=shortcut)


@pytest.mark.parametrize('shortcut, normalized', [
    ('control+shift+q', 'Ctrl+Shift+Q'),
    ('alt+ctrl+7', 'Ctrl+Alt+7'),
    ('Ctrl+Alt+Shift+f11', 'Ctrl+Alt+Shift+F11'),
])
def test_explicit_supported_hotkeys_use_the_shared_canonical_parser(shortcut, normalized):
    assert SessionOptions(show=shortcut).show == normalized


@pytest.mark.parametrize('value', [1, 'yes', None])
def test_tray_choice_requires_an_explicit_boolean(value):
    with pytest.raises(ValueError):
        SessionOptions(tray=value)


def test_duplicate_hotkeys_are_rejected_before_any_native_owner_exists():
    with pytest.raises(ValueError, match='different'):
        SessionOptions(show='Ctrl+Alt+Q', stop='Control+Alt+q')


def test_default_off_constructs_no_native_receiver_and_cannot_start_reading():
    controls, slot, owner, created = session()
    assert owner.options == SessionOptions() and not owner.enabled
    assert slot.owner is None and not created
    assert owner.invoke('show', owner.epoch) is False
    assert owner.configure(SessionOptions()) is True
    assert controls.calls == controls.dialog.calls == [] and not created


def test_explicit_current_owner_uses_existing_actions_and_show_never_reads():
    controls, slot, owner, created = session()
    owner.configure(SessionOptions(tray=True))
    controls.dialog.iconized = True
    epoch = owner.epoch
    assert owner.invoke('show', epoch) is True
    assert controls.dialog.calls == ['show', 'restore', 'raise']
    assert controls.calls == [] and not controls.core.busy
    assert owner.invoke('pause', epoch) is False
    controls.core.busy = True
    controls.core.status = lambda: SimpleNamespace(state='playing')
    assert owner.invoke('pause', epoch) is True
    assert owner.invoke('stop', epoch) is True
    assert controls.calls == ['pause', 'stop']
    assert created[0].started == [SessionOptions(tray=True)]
    assert slot.owner is owner


def test_active_canonical_keymap_collision_never_rewrites_or_registers():
    controls, slot, owner, created = session()
    overrides = {'command': 'Ctrl+Alt+Q'}
    registry = SimpleNamespace(effective=lambda saved: dict(saved))
    controls._host.keyboard_commands = SimpleNamespace(registry=registry, overrides=overrides)
    with pytest.raises(ValueError, match='keymap'):
        owner.configure(SessionOptions(show='Ctrl+Alt+Q'))
    assert controls._host.keyboard_commands.overrides == overrides
    assert not created and slot.owner is None


def test_changed_local_keymap_revokes_existing_global_callbacks():
    controls, slot, owner, created = session()
    overrides = {}
    controls._host.keyboard_commands = SimpleNamespace(
        registry=SimpleNamespace(effective=lambda saved: dict(saved)), overrides=overrides)
    owner.configure(SessionOptions(show='Ctrl+Alt+Q'))
    epoch = owner.epoch
    overrides['other'] = 'Ctrl+Alt+Q'
    assert owner.invoke('show', epoch) is False
    assert not owner.enabled and slot.owner is None
    assert not controls.dialog.calls and created[0].released == 1
    assert 'keymap' in controls.calls[-1][1]


def test_another_reader_is_never_silently_substituted():
    first, slot, owner, _ = session()
    second, _, replacement, created = session(slot=slot)
    owner.configure(SessionOptions(tray=True))
    with pytest.raises(ValueError, match='Another reader'):
        replacement.configure(SessionOptions(tray=True))
    assert slot.owner is owner and not created
    assert owner.invoke('stop', owner.epoch) is True and second.calls == []
    old_epoch = owner.epoch
    assert owner.close() is True
    replacement.configure(SessionOptions(tray=True))
    assert owner.invoke('stop', old_epoch) is False
    assert slot.owner is replacement and second.calls == []


@pytest.mark.parametrize('change', ['closed-controls', 'dead-native', 'different-core', 'different-dialog'])
def test_exact_reader_lifetime_and_core_are_rechecked(change):
    controls, slot, owner, _ = session()
    owner.configure(SessionOptions(tray=True))
    if change == 'closed-controls':
        controls.closed = True
    elif change == 'dead-native':
        controls.dialog.alive = False
    elif change == 'different-core':
        controls.core = SimpleNamespace(busy=True)
    else:
        controls.dialog = Dialog()
    assert all(owner.invoke(action, owner.epoch) is False for action in ('show', 'pause', 'stop'))
    assert controls.calls == [] and controls.dialog.calls == []
    assert owner.close() is True and slot.owner is None


def test_old_config_and_old_popup_generation_cannot_control_new_work():
    controls, slot, owner, _ = session()
    owner.configure(SessionOptions(tray=True))
    old_epoch, generation = owner.epoch, controls._start_generation
    owner.configure(SessionOptions(show='Ctrl+Alt+Shift+Q'))
    assert owner.invoke('stop', old_epoch) is False
    controls._start_generation += 1
    assert owner.invoke('stop', owner.epoch, generation=generation) is False
    assert controls.calls == []


@pytest.mark.parametrize('returned', [False, None])
def test_unconfirmed_release_keeps_exact_owner_and_blocks_replacement(returned):
    controls, slot, owner, created = session()
    owner.configure(SessionOptions(tray=True))
    native = created[0]
    native.confirmed = returned
    epoch = owner.epoch
    assert owner.close() is False
    assert owner.native is native and slot.owner is owner and owner.closed
    assert not owner.enabled and owner.invoke('stop', epoch) is False
    other, _, next_owner, next_created = session(slot=slot)
    with pytest.raises(ValueError, match='Another reader'):
        next_owner.configure(SessionOptions(tray=True))
    assert not next_created and other.calls == []
    native.confirmed = True
    assert slot.owner.disable() is True
    next_owner.configure(SessionOptions(tray=True))
    assert slot.owner is next_owner


@pytest.mark.parametrize('error', [OSError, KeyboardInterrupt])
def test_partial_native_start_rolls_back_and_retains_failed_cleanup(error):
    controls, slot, owner, _ = session()
    native = Native(owner, 0)
    native.confirmed = False
    def start(options):
        native.started.append(options)
        raise error('Native setup stopped after acquiring one binding')
    native.start = start
    owner.native_factory = lambda _owner, _epoch: native
    with pytest.raises(error):
        owner.configure(SessionOptions(tray=True, show='Ctrl+Alt+Shift+Q'))
    assert slot.owner is owner and owner.native is native and native.released == 1
    assert not owner.enabled
    assert owner.invoke('stop', owner.epoch) is False
    native.confirmed = True
    assert owner.disable() and slot.owner is None


def test_reentrant_target_close_during_registration_cannot_commit_enablement():
    controls, slot, owner, _ = session()
    native = Native(owner, 0)
    native.on_start = owner.detach
    owner.native_factory = lambda _owner, _epoch: native
    with pytest.raises(ValueError, match='closed'):
        owner.configure(SessionOptions(tray=True))
    assert not owner.enabled and slot.owner is None and native.released == 1


def test_detach_is_a_pure_latch_and_does_not_touch_native_cleanup():
    controls, slot, owner, created = session()
    owner.configure(SessionOptions(tray=True))
    epoch = owner.epoch
    owner.detach()
    assert created[0].released == 0 and owner.native is created[0]
    assert owner.closed and not owner.enabled and slot.owner is owner
    assert owner.invoke('stop', epoch) is False
    assert owner.close() is True and created[0].released == 1


class Panel:
    """Explicit test double for native acknowledgements; not a Windows proof."""
    def __init__(self, parent):
        self.alive = True
        self.events = {}
        self.destroyed = 0
    def __bool__(self):
        return self.alive
    def GetId(self):
        return 93
    def GetHandle(self):
        return 12345
    def Hide(self):
        pass
    def Bind(self, event, handler, id=None):
        self.events[event, id] = handler
    def Unbind(self, event, id=None):
        return self.events.pop((event, id), None) is not None
    def Destroy(self):
        self.destroyed += 1
        self.alive = False
        return True


class NativeKeys:
    def __init__(self, handle):
        assert handle == 12345
        self.owned = {}
        self.registration = []
        self.released = []
        self.reject_key = None
        self.fail_release = False
    def register(self, identity, modifiers, key):
        self.registration.append((identity, modifiers, key))
        if key == self.reject_key:
            return False
        assert identity not in self.owned and 0 <= identity <= 0xBFFF
        self.owned[identity] = modifiers, key
        return True
    def unregister(self, identity):
        self.released.append(identity)
        if self.fail_release:
            return False
        return self.owned.pop(identity, None) is not None


def wx_session(monkeypatch):
    wx = SimpleNamespace(Panel=Panel, EVT_WINDOW_DESTROY='destroy', EVT_HOTKEY='hotkey')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    keys = []
    def make_keys(handle):
        value = NativeKeys(handle)
        keys.append(value)
        return value
    monkeypatch.setattr(module, '_WindowsHotKeys', make_keys)
    controls, slot, owner, _ = session()
    owner.native_factory = module._WxSession
    return controls, slot, owner, keys


def test_actual_wx_adapter_uses_unique_positive_ids_exact_modifiers_and_existing_routes(monkeypatch):
    controls, slot, owner, keys = wx_session(monkeypatch)
    owner.configure(SessionOptions(show='Ctrl+Alt+Shift+Q', stop='Ctrl+Shift+F11'))
    native = owner.native
    assert [row[1:] for row in keys[0].registration] == [(7, ord('Q')), (6, 0x7A)]
    identities = [row[0] for row in keys[0].registration]
    callbacks = [native.panel.events['hotkey', identity] for identity in identities]
    callbacks[0](None)
    callbacks[1](None)
    assert controls.dialog.calls == ['show', 'raise'] and controls.calls == ['stop']
    assert owner.disable()
    assert set(keys[0].released) == set(identities) and not keys[0].owned
    assert native.panel.destroyed == 1
    callbacks[0](None)
    callbacks[1](None)
    assert controls.dialog.calls == ['show', 'raise'] and controls.calls == ['stop']


def test_native_collision_rolls_back_only_our_prior_registration(monkeypatch):
    controls, slot, owner, keys = wx_session(monkeypatch)
    factory = owner.native_factory
    def collision(selected, epoch):
        native = factory(selected, epoch)
        native.hotkeys.reject_key = ord('Z')
        return native
    owner.native_factory = collision
    with pytest.raises(ValueError, match='unavailable'):
        owner.configure(SessionOptions(show='Ctrl+Alt+Shift+Q', stop='Ctrl+Alt+Shift+Z'))
    assert len(keys[0].registration) == 2 and len(keys[0].released) == 1
    assert keys[0].released == [keys[0].registration[0][0]]
    assert not keys[0].owned and slot.owner is None


def test_failed_native_unregister_retains_exact_receiver_for_retry(monkeypatch):
    controls, slot, owner, keys = wx_session(monkeypatch)
    owner.configure(SessionOptions(stop='Ctrl+Alt+Shift+Q'))
    native, identity = owner.native, next(iter(keys[0].owned))
    callback = native.panel.events['hotkey', identity]
    keys[0].fail_release = True
    assert owner.close() is False
    callback(None)
    assert not controls.calls and native.panel.destroyed == 0
    assert native.keys and slot.owner is owner
    keys[0].fail_release = False
    assert owner.disable() is True
    assert keys[0].released == [identity, identity] and native.panel.destroyed == 1


def test_native_receiver_destruction_is_pure_and_cannot_claim_unregistration(monkeypatch):
    controls, slot, owner, keys = wx_session(monkeypatch)
    owner.configure(SessionOptions(stop='Ctrl+Alt+Shift+Q'))
    native, identity = owner.native, next(iter(keys[0].owned))
    callback = native.panel.events['hotkey', identity]
    child = SimpleNamespace(GetId=lambda: native.panel_id + 1, Skip=lambda: None)
    native.destroyed(child)
    assert owner.enabled and native.panel_alive
    native.destroyed(SimpleNamespace(GetId=lambda: native.panel_id, Skip=lambda: None))
    native.panel.alive = False
    assert not native.panel_alive and owner.closed
    assert keys[0].released == []
    callback(None)
    assert controls.calls == []
    assert owner.close() is False and slot.owner is owner and native.keys


class UIWindow:
    """Poison native peers after a real bound destruction callback."""
    next_id = 100
    forbidden = []
    def __init__(self, parent=None, *args, **kwargs):
        UIWindow.next_id += 1
        self.identity = UIWindow.next_id
        self.parent = parent
        self.children = []
        if isinstance(parent, UIWindow):
            parent.children.append(self)
        self.alive = True
        self.events = {}
        self.value = kwargs.get('value', '')
        self.modal = True
        self.destroy_requests = 0
    def __bool__(self):
        return self.alive
    def checked(self, name):
        if not self.alive:
            UIWindow.forbidden.append(name)
            raise AssertionError('Access to a deleted native peer: ' + name)
    def GetId(self):
        self.checked('GetId')
        return self.identity
    def Bind(self, event, action, id=None):
        self.checked('Bind')
        self.events[event, id] = action
    def SetName(self, name):
        self.checked('SetName')
    def SetValue(self, value):
        self.checked('SetValue')
        self.value = value
    def GetValue(self):
        self.checked('GetValue')
        return self.value
    def ChangeValue(self, value):
        self.checked('ChangeValue')
        self.value = value
    def SetSizer(self, layout):
        self.checked('SetSizer')
    def SetEscapeId(self, identity):
        self.checked('SetEscapeId')
    def IsModal(self):
        self.checked('IsModal')
        return self.modal
    def EndModal(self, code):
        self.checked('EndModal')
        self.modal = False
    def ShowModal(self):
        self.checked('ShowModal')
        return 0
    def native_delete(self):
        if not self.alive:
            return
        for child in tuple(self.children):
            child.native_delete()
        self.alive = False
        action = self.events.get(('destroy', None))
        if action is not None:
            action(SimpleNamespace(GetId=lambda: self.identity, Skip=lambda: None))
    def Destroy(self):
        self.checked('Destroy')
        self.destroy_requests += 1
        self.native_delete()
        return True


def native_dialog_fixture(monkeypatch):
    UIWindow.forbidden = []
    application, queued = SimpleNamespace(), []
    wx = SimpleNamespace(
        Dialog=UIWindow, StaticText=UIWindow, CheckBox=UIWindow, TextCtrl=UIWindow,
        Button=UIWindow, BoxSizer=lambda *args: SimpleNamespace(Add=lambda *args: None),
        VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8, LEFT=16, RIGHT=32,
        TE_READONLY=64, TE_MULTILINE=128, ID_CANCEL=0,
        EVT_BUTTON='button', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy',
        GetApp=lambda: application, CallAfter=lambda callback, *args: queued.append((callback, args)))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    controls = Controls()
    controls._host = UIWindow()
    controls.dialog = UIWindow(controls._host)
    dialog = module.create_session_dialog(controls)
    owner = controls._background_controls
    created = []
    def factory(selected, epoch):
        value = Native(selected, epoch)
        created.append(value)
        return value
    owner.native_factory = factory
    return controls, dialog, owner, created, queued


@pytest.mark.parametrize('destroy', ['settings', 'reader-parent'])
def test_actual_apply_handler_stops_between_native_reads_after_owner_deletion(monkeypatch, destroy):
    controls, dialog, owner, created, queued = native_dialog_fixture(monkeypatch)
    original = dialog.hotkeys.GetValue
    def value_then_delete():
        original()
        if destroy == 'settings':
            dialog.native_delete()  # native deletion bypasses Python Destroy
        else:
            controls._host.native_delete()
        return True
    dialog.hotkeys.GetValue = value_then_delete
    dialog.apply(None)
    assert not created and not owner.enabled and owner.slot.owner is None
    assert UIWindow.forbidden == []
    assert not dialog.alive
    for callback, args in queued:
        callback(*args)
    assert UIWindow.forbidden == []


def test_reader_native_deletion_latches_before_deferred_cleanup_and_late_actions(monkeypatch):
    controls, dialog, owner, created, queued = native_dialog_fixture(monkeypatch)
    dialog.tray.SetValue(True)
    dialog.apply(None)
    assert owner.enabled and len(created) == 1
    epoch, held_apply, held_disable = owner.epoch, dialog.apply, dialog.disable
    controls._host.native_delete()
    assert owner.closed and not owner.enabled and created[0].released == 0
    assert owner.invoke('show', epoch) is False
    held_apply(None)
    held_disable(None)
    assert not controls.calls and not UIWindow.forbidden
    assert len(queued) == 1
    callback, args = queued.pop()
    callback(*args)
    assert created[0].released == 1 and owner.slot.owner is None
    assert not UIWindow.forbidden


def test_win32_adapter_keeps_native_no_repeat_flag_and_exact_receiver(monkeypatch):
    import ctypes
    calls = []
    class Function:
        def __init__(self, name):
            self.name = name
        def __call__(self, *args):
            calls.append((self.name, args))
            return 1
    api = SimpleNamespace(RegisterHotKey=Function('register'), UnregisterHotKey=Function('unregister'))
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kwargs: api, raising=False)
    native = module._WindowsHotKeys(12345)
    assert native.register(0x5000, 7, ord('Q')) is True
    assert native.unregister(0x5000) is True
    assert calls == [('register', (12345, 0x5000, 7 | 0x4000, ord('Q'))),
                     ('unregister', (12345, 0x5000))]


@pytest.mark.parametrize('handle', [0, -1, None, True])
def test_hotkeys_cannot_silently_become_thread_wide_without_a_native_receiver(monkeypatch, handle):
    import ctypes
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    def forbidden(*args, **kwargs):
        pytest.fail('No native API is opened for an invalid receiver.')
    monkeypatch.setattr(ctypes, 'WinDLL', forbidden, raising=False)
    with pytest.raises(ValueError, match='receiver'):
        module._WindowsHotKeys(handle)
