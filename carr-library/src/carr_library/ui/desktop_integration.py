"""Opt-in native tray, clipboard review and semantic global command dispatch.

All native operations run on the existing UI thread. There is no key hook,
clipboard history, background speech, command registry or playback backend here.
"""
from hashlib import sha256
import math
import re
import sys
from threading import get_ident
from uuid import uuid4

from ..desktop_preferences import DesktopPreferences, DesktopPreferenceStore, global_shortcut
from ..keybindings import validate_bindings

# These are existing menu identities, not a second command registry. Runtime
# labels, availability and dispatch still come from the shared native manager.
GLOBAL_COMMANDS = ('desktop.show', 'voice.clipboard', 'voice.watched_clipboard',
                   'voice.reader', 'voice.pause', 'voice.stop', 'progress.query')

# A finite mapping into the existing menu-path registry. These are aliases for
# opt-in OS delivery only; they never store or register a second command table.
MENU_PATHS = {
    'desktop.settings': 'Desktop/Desktop integration',
    'desktop.show': 'Desktop/Show Resonance window',
    'desktop.hide': 'Desktop/Hide window to tray',
    'desktop.clipboard_watch': 'Desktop/Watch clipboard for this library selection',
    'voice.watched_clipboard': 'Desktop/Open watched clipboard',
    'voice.pause': 'Desktop/Pause or resume current voice reader',
    'voice.stop': 'Desktop/Stop current voice reader',
    'voice.clipboard': 'Voice/Read clipboard',
    'voice.reader': 'Voice/Voice reader and reading copy',
    'progress.query': 'Help/Current operation progress',
}


class DesktopCommands:
    """Read and invoke the current KeyboardActions inventory; retain no bindings."""
    def __init__(self, frame, wx):
        self.frame, self.wx = frame, wx

    @property
    def manager(self):
        manager = getattr(self.frame, 'keyboard_actions', None)
        if manager is None:
            raise ValueError('The existing desktop command inventory is not ready.')
        return manager

    def scope(self):
        return id(self.frame.library), str(self.frame.library.root), self.frame.active

    def binding(self, identity):
        path = MENU_PATHS.get(identity)
        found = [binding for binding in self.manager.bindings
                 if binding.command.identity == path and binding.command.context == 'main']
        if len(found) != 1:
            raise ValueError('This action is unavailable in the current desktop: ' + identity)
        return found[0]

    def command(self, identity):
        return self.binding(identity).command

    def effective(self):
        return validate_bindings(self.manager.commands, self.manager.overrides)

    def available(self, identity):
        try:
            return (not getattr(self.frame, '_closing', False)
                    and self.frame.IsEnabled() and not getattr(self.frame, 'working', False)
                    and not getattr(self.frame, 'busy', False)
                    and self.binding(identity).item.IsEnabled())
        except (ValueError, RuntimeError):
            return False

    def invoke(self, identity):
        if not self.available(identity):
            return False
        item = self.binding(identity).item
        event = self.wx.CommandEvent(self.wx.EVT_MENU.typeId, item.GetId())
        event.SetEventObject(self.frame)
        return bool(self.frame.GetEventHandler().ProcessEvent(event))


class ClipboardWatch:
    """Retain only the newest bounded snapshot in memory until explicit Open."""
    def __init__(self, read, scope, notice):
        self.read, self.scope, self.notice = read, scope, notice
        self.enabled, self.closed = False, False
        self.pending = self._scope = self._digest = self._error = None

    def enable(self):
        if self.closed:
            raise ValueError('This desktop window is closed.')
        self._scope = self.scope()
        self.pending = self._digest = self._error = None
        self.enabled = True

    def disable(self):
        self.enabled = False
        self.pending = self._scope = self._digest = self._error = None

    def poll(self):
        if self.closed or not self.enabled:
            return False
        if self.scope() != self._scope:
            self.disable()
            self.notice('Clipboard watching stopped because the library selection changed.')
            return False
        try:
            from ..speech._io import text_value
            text = text_value(self.read(), 'Clipboard reading text')
        except Exception:
            self.pending = self._digest = None
            if self._error is None:
                self.notice('Clipboard text is unavailable. Watching remains on; no reading copy was opened.')
                self._error = True
            return False
        self._error = None
        fingerprint = sha256(text.encode('utf-8')).digest()
        if fingerprint == self._digest:
            return False
        self._digest, self.pending = fingerprint, text
        self.notice('New clipboard text is ready. Choose Open watched clipboard to review it; no speech has started.')
        return True

    def take(self):
        if self.closed or not self.enabled or self.scope() != self._scope:
            self.disable()
            raise ValueError('Clipboard watching stopped or its library selection changed.')
        if self.pending is None:
            raise ValueError('No new watched clipboard text is ready.')
        text, self.pending = self.pending, None
        return text

    def close(self):
        self.disable()
        self.closed = True


class WindowsHotkeyAPI:
    """Use WM_HOTKEY through wx with native no-repeat registration.

    wxWidgets 3.2 discards MOD_NOREPEAT in its RegisterHotKey wrapper. The fixed
    Win32 call below preserves it; ordinary wx EVT_HOTKEY remains the receiver.
    """
    def __init__(self, frame):
        import ctypes
        from ctypes import wintypes
        self.frame = frame
        self.handle = frame.GetHandle()
        if not self.handle:
            raise ValueError('Global shortcuts need a current native window handle.')
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self.api.UnregisterHotKey.restype = wintypes.BOOL
        self.api.SetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE)
        self.api.SetPropW.restype = wintypes.BOOL
        self.api.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.api.GetPropW.restype = wintypes.HANDLE
        self.api.RemovePropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
        self.api.RemovePropW.restype = wintypes.HANDLE
        self._thread = get_ident()
        self._owner_key = 'Carr.GlobalHotkeys.' + uuid4().hex
        self._owner_attached, self._keys = False, set()
        # A per-window marker distinguishes this actual HWND from a later
        # numerical handle reuse. Value 1 is an application tag, not an
        # allocated data handle; only this unique property is ever removed.
        if not self.api.SetPropW(self.handle, self._owner_key, 1):
            raise RuntimeError('Windows did not accept the native shortcut owner marker.')
        self._owner_attached = True

    def _owner(self):
        if get_ident() != self._thread:
            raise RuntimeError('Native shortcut cleanup must use its original UI thread.')
        if (not self._owner_attached
                or self.api.GetPropW(self.handle, self._owner_key) != 1):
            raise ValueError('The native global-shortcut owner changed; cleanup remains unconfirmed.')

    def register(self, identifier, shortcut):
        if self.frame.GetHandle() != self.handle:
            raise ValueError('The native global-shortcut owner changed.')
        self._owner()
        if identifier in self._keys:
            raise ValueError('Release the existing native shortcut identity before registering it again.')
        parts = global_shortcut(shortcut).split('+')
        modifiers = 0x4000 | sum({'Alt': 1, 'Ctrl': 2, 'Shift': 4}[name] for name in parts[:-1])
        key = parts[-1]
        virtual = 0x70 + int(key[1:]) - 1 if key.startswith('F') else ord(key)
        accepted = bool(self.api.RegisterHotKey(self.handle, identifier, modifiers, virtual))
        if accepted:
            self._keys.add(identifier)
        return accepted

    def unregister(self, identifier):
        # Destruction callbacks may arrive after SIP invalidates the Python
        # wrapper. Never call through it, and never act on an unmarked/reused
        # HWND. A missing marker is not evidence of successful key release.
        self._owner()
        if identifier not in self._keys:
            raise ValueError('Only an exactly retained native shortcut may be released.')
        released = bool(self.api.UnregisterHotKey(self.handle, identifier))
        if released:
            self._keys.remove(identifier)
        return released

    def close(self):
        if not self._owner_attached:
            return
        self._owner()
        if self._keys:
            raise RuntimeError('Native shortcut release remains unconfirmed.')
        if self.api.RemovePropW(self.handle, self._owner_key) != 1:
            raise RuntimeError('Windows did not confirm removal of the shortcut owner marker.')
        self._owner_attached = False


class GlobalHotkeys:
    """Own a bounded set of native registrations, never an OS keyboard hook."""
    def __init__(self, frame, manager, dispatch, *, wx_module=None, platform=None, native=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.frame, self.manager, self.dispatch = wx_module, frame, manager, dispatch
        self.platform = sys.platform if platform is None else platform
        self.enabled, self.closed = False, False
        self.native, self._dispatching = native, False
        self._detached, self._owner_destroyed = False, False
        self._registered, self._bindings = {}, {}
        self._scope = None
        self._native_event = getattr(self.wx, 'EVT_HOTKEY', None)
        if self._native_event is not None:
            self.frame.Bind(self._native_event, self._event)

    def validate(self, bindings):
        values = DesktopPreferences(global_shortcuts=bindings).global_shortcuts
        effective = self.manager.effective()
        commands = {command.identity: command for command in self.manager.manager.commands}
        for identity, shortcut in values.items():
            if identity not in GLOBAL_COMMANDS:
                raise ValueError('This global action is unavailable in the current desktop: ' + identity)
            command = self.manager.command(identity)
            for other, current in effective.items():
                if current == shortcut and other != command.identity:
                    raise ValueError(shortcut + ' already belongs to ' + commands[other].label + '.')
        return dict(values)

    def enable(self, bindings):
        if self.closed or self._registered:
            raise ValueError('Disable the previous global shortcuts before changing assignments.')
        if self.platform != 'win32':
            raise ValueError('This global-shortcut adapter is available only on Windows.')
        if self._native_event is None:
            raise ValueError('This wx build has no native global-key event support.')
        values = self.validate(bindings)
        if not values:
            raise ValueError('Save at least one explicit global shortcut first.')
        if self.native is None:
            self.native = WindowsHotkeyAPI(self.frame)
        try:
            for index, (identity, shortcut) in enumerate(values.items(), 0x4200):
                if not self.native.register(index, shortcut):
                    raise ValueError(shortcut + ' could not be registered; another application or Windows may own it.')
                self._registered[index] = identity
            self._bindings, self.enabled = values, True
            self._scope = self.manager.scope()
        except Exception as exc:
            try:
                self.disable()
            except Exception as cleanup:
                raise RuntimeError(str(exc) + ' Global-key cleanup needs attention: ' + str(cleanup)) from exc
            raise

    def disable(self):
        self.enabled = False  # Queued events lose authority before cleanup begins.
        failures = []
        for identifier in tuple(self._registered):
            try:
                if not self.native.unregister(identifier):
                    raise RuntimeError('Windows did not confirm shortcut release.')
                del self._registered[identifier]
            except Exception as exc:
                failures.append(str(exc))
        self._bindings = {}
        self._scope = None
        if failures:
            raise RuntimeError('; '.join(failures))

    def _event(self, event):
        if self.closed or not self.enabled or self._dispatching or getattr(self.frame, '_closing', False):
            return
        identity = self._registered.get(event.GetId())
        if identity is None:
            return
        try:
            if self.manager.scope() != self._scope:
                raise ValueError('The selected library changed; explicitly enable shortcuts again.')
            self.validate(self._bindings)  # Local assignments may have changed.
        except Exception as exc:
            try:
                self.disable()
            except Exception as cleanup:
                self.frame.report('Global shortcuts stopped; key release needs attention: ' + str(cleanup))
            else:
                self.frame.report('Global shortcuts stopped: ' + str(exc))
            return
        self._dispatching = True
        try:
            self.dispatch(identity)
        except Exception as exc:
            if not self.closed and not getattr(self.frame, '_closing', False):
                self.frame.report('The global action could not complete: ' + str(exc))
        finally:
            self._dispatching = False

    def close(self, *, owner_destroyed=False):
        self.closed = True
        self._owner_destroyed = self._owner_destroyed or owner_destroyed
        try:
            self.disable()
            release_marker = getattr(self.native, 'close', None)
            if callable(release_marker):
                release_marker()
        finally:
            if self._native_event is not None and not self._detached:
                if not self._owner_destroyed:
                    self.frame.Unbind(self._native_event, handler=self._event)
                self._detached = True


class DesktopIntegration:
    def __init__(self, frame, *, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.frame = wx_module, frame
        self.closed, self.tray, self.reader = False, None, None
        self._detached, self._owner_id = False, frame.GetId()
        self._owner_destroyed = False
        self.commands = DesktopCommands(frame, self.wx)
        self._assistant_reader = None
        self.items = {}
        self.store = DesktopPreferenceStore(frame.library.root)
        self.preferences = DesktopPreferences()
        try:
            self.preferences = self.store.load()
        except Exception as exc:
            frame.report('Desktop preferences are unavailable; optional features are off. ' + str(exc))
        self.watch = ClipboardWatch(self._read, self.scope, frame.report)
        self.timer = self.wx.Timer(frame)
        self.frame.Bind(self.wx.EVT_TIMER, self._tick, self.timer)
        self.frame.Bind(self.wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.hotkeys = GlobalHotkeys(frame, self.commands, self.invoke, wx_module=self.wx)

    def scope(self):
        return (id(self.frame.library), str(self.frame.library.root), self.frame.active)

    def _read(self):
        from .clipboard_reader import _read_clipboard_text
        return _read_clipboard_text(self.wx)

    def _live(self):
        return not self.closed and not getattr(self.frame, '_closing', False)

    def available(self, identity):
        if not self._live():
            return False
        if identity == 'desktop.show':
            return True
        if identity in ('voice.pause', 'voice.stop'):
            current = self.reader
            if current is None:
                return False
            _token, dialog, controls, alive, scope = current
            try:
                return (alive() and scope == self.scope() and dialog.IsEnabled()
                        and controls[identity].IsEnabled())
            except RuntimeError:
                return False
        if identity == 'voice.watched_clipboard' and self.watch.pending is None:
            return False
        return self.commands.available(identity)

    def invoke(self, identity):
        if not self.available(identity):
            return False
        manager = self.commands
        manager.command(identity)
        if identity in ('voice.pause', 'voice.stop'):
            # Send the same native button event as Space; source/cancel/quiet
            # checks remain in the existing reader handler, not this adapter.
            button = self.reader[2][identity]
            event = self.wx.CommandEvent(self.wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            return bool(button.GetEventHandler().ProcessEvent(event))
        if identity == 'desktop.show':
            item = manager.binding(identity).item
            event = self.wx.CommandEvent(self.wx.EVT_MENU.typeId, item.GetId())
            event.SetEventObject(self.frame)
            return bool(self.frame.GetEventHandler().ProcessEvent(event))
        return manager.invoke(identity)

    def attach_reader(self, dialog, pause, stop, alive, *, player=None,
                      source_snapshot=None, player_guard=None, pause_action=None, stop_action=None):
        if not self._live() or self.reader is not None:
            raise ValueError('Another reading window already owns the desktop playback controls.')
        values = (player, source_snapshot, player_guard, pause_action, stop_action)
        qualified = any(value is not None for value in values)
        if qualified:
            from ..speech.player import WavePlayer
            if not isinstance(player, WavePlayer) or not all(callable(value) for value in values[1:]):
                raise TypeError('Current-player commands require the exact existing WavePlayer and reader guards.')
        token = object()
        dialog_id = dialog.GetId()
        self.reader = token, dialog, {'voice.pause': pause, 'voice.stop': stop}, alive, self.scope()
        self._assistant_reader = ({'token': token, 'session': uuid4().hex, 'player': player,
            'snapshot': source_snapshot, 'guard': player_guard,
            'pause': pause_action, 'stop': stop_action} if qualified else None)
        def detach():
            if self.reader is not None and self.reader[0] is token:
                self.reader = None
                self._assistant_reader = None
        def destroyed(event):
            if event.GetId() == dialog_id:
                detach()
            event.Skip()
        dialog.Bind(self.wx.EVT_WINDOW_DESTROY, destroyed)
        return detach

    def assistant_player(self, reader_window, token, workspace, action, *, expected=None,
                         cancelled=lambda: False):
        """UI-only status or idempotent safe control of this exact reader owner."""
        if not self.wx.IsMainThread():
            raise RuntimeError('Current-player controls must run on the native UI thread.')
        if action not in ('status', 'pause', 'stop'):
            raise ValueError('This bridge permits Status, Pause and Stop only.')
        current, owned = self.reader, self._assistant_reader
        if (not self._live() or current is None or owned is None or current[0] is not token
                or owned['token'] is not token or current[1] is not reader_window
                or not current[3]() or current[4] != self.scope()
                or not self.frame.active or self.frame.active[0] != workspace):
            raise PermissionError('The exact reader session or selected library changed.')
        owned['guard']()
        signature = owned['snapshot']()
        if type(signature) is not str or not re.fullmatch('[0-9a-f]{64}', signature):
            raise ValueError('The reader did not return an exact source signature.')
        if expected is not None and (
                expected.get('session') != owned['session'] or expected.get('source_sha256') != signature
                or expected.get('action') != action):
            raise PermissionError('The reviewed player session or source changed before the action.')
        if cancelled():
            raise InterruptedError('The player action was cancelled before native dispatch.')
        player = owned['player']
        before = player.state
        if action == 'pause' and before == 'playing':
            owned['pause']()  # The existing handler; never toggle an already-paused player.
        elif action == 'stop':
            owned['stop']()
        after = player.state
        if ((action == 'pause' and after not in ('paused', 'stopped'))
                or (action == 'stop' and after != 'stopped')):
            raise RuntimeError('The native player did not confirm the requested safe state.')
        owned['guard']()
        if owned['snapshot']() != signature:
            raise PermissionError('The reading source changed during the player action.')
        position, duration = player.position_seconds, player.duration_seconds
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (position, duration)):
            raise ValueError('The native player returned invalid measured status.')
        return {'schema': 'carr-current-player-receipt/1', 'session': owned['session'],
            'source_sha256': signature, 'workspace': workspace, 'action': action,
            'state': after, 'position_seconds': position, 'duration_seconds': duration,
            'position_approximate': bool(player.position_is_approximate),
            'notice': 'Captured current-reader status. No player was created and no audio was started.'}

    def show_window(self, event=None):
        if not self._live():
            return
        self.frame.Show(True)
        self.frame.Iconize(False)
        if self.reader is not None and self.reader[3]():
            self.reader[1].Raise()
        else:
            self.frame.Raise()

    def hide_window(self, event=None):
        if not self._live() or self.tray is None or not self.tray.IsIconInstalled():
            raise ValueError('Enable the available tray icon before hiding this window.')
        if not self.frame.IsEnabled() or getattr(self.frame, 'working', False):
            raise ValueError('Finish the active dialog or operation before hiding this window.')
        self.frame.Hide()

    def watch_clipboard(self, enabled):
        if type(enabled) is not bool or not self._live():
            raise ValueError('Clipboard watching needs an open desktop window.')
        if enabled:
            self.watch.enable()
            if not self.timer.Start(1000):
                self.watch.disable()
                raise RuntimeError('The clipboard observation timer could not be started.')
            self.frame.report('Clipboard watching is on for this library selection. New text stays in memory until you explicitly open it.')
        else:
            if not self.hotkeys.enabled:
                self.timer.Stop()
            self.watch.disable()
            self.frame.report('Clipboard watching is off; its temporary snapshot was cleared.')
        self._refresh()

    def _refresh(self):
        if self.closed:
            return
        item = self.items.get('desktop.clipboard_watch')
        if item is not None:
            item.Check(self.watch.enabled)
        item = self.items.get('voice.watched_clipboard')
        if item is not None:
            item.Enable(self.watch.pending is not None)

    def _tick(self, event=None):
        if self.closed:
            return
        if not self._live():
            self.timer.Stop()
            self.watch.close()
            return
        if self.hotkeys.enabled and self.hotkeys._scope != self.scope():
            try:
                self.hotkeys.disable()
            except Exception as exc:
                self.frame.report('Global shortcut release needs attention: ' + str(exc))
            else:
                self.frame.report('Global shortcuts stopped because the library selection changed.')
        # A scope switch clears data even while a modal/operation is active.
        if self.watch.enabled and self.watch._scope != self.scope():
            self.watch.disable()
            self.timer.Stop()
            self.frame.report('Clipboard watching stopped because the library selection changed.')
        elif self.frame.IsEnabled() and not getattr(self.frame, 'working', False):
            self.watch.poll()
        if not self.watch.enabled and not self.hotkeys.enabled:
            self.timer.Stop()
        self._refresh()

    def disable_hotkeys(self):
        try:
            self.hotkeys.disable()
        finally:
            if not self.watch.enabled:
                self.timer.Stop()

    def open_watched(self, event=None):
        if not self._live() or not self.frame.IsEnabled() or getattr(self.frame, 'working', False):
            raise ValueError('Finish the active operation before opening watched text.')
        text = self.watch.take()
        self._refresh()
        from .speech_dialog import show_speech_reader
        show_speech_reader(self.frame, text=text, title='Watched clipboard reading copy',
                           recovery_slot='clipboard-watch')

    def set_tray(self, enabled):
        if type(enabled) is not bool or not self._live():
            raise ValueError('The tray icon needs an open desktop window.')
        if not enabled:
            if self.tray is not None:
                if not self.frame.IsShown():
                    self.show_window()
                old = self.tray
                old.active = False
                if old.IsIconInstalled() and not old.RemoveIcon():
                    raise RuntimeError('The desktop did not confirm removal of its tray icon. Retry Disable.')
                old.Destroy()
                self.tray = None
            return
        if self.tray is not None:
            if not self.tray.active:
                raise RuntimeError('Finish the previous tray-icon cleanup before enabling it again.')
            return
        import wx.adv
        if not wx.adv.TaskBarIcon.IsAvailable():
            raise ValueError('This desktop session does not provide a system tray.')
        controller, wx = self, self.wx
        class Tray(wx.adv.TaskBarIcon):
            active = True
            def CreatePopupMenu(self):
                menu = wx.Menu()
                for identity in ('desktop.show', 'voice.clipboard', 'voice.watched_clipboard',
                                 'voice.pause', 'voice.stop', 'progress.query', 'desktop.settings'):
                    command = controller.commands.command(identity)
                    item = menu.Append(wx.ID_ANY, command.label)
                    item.Enable(self.active and controller.available(identity))
                    menu.Bind(wx.EVT_MENU,
                        lambda event, identity=identity: controller.invoke(identity)
                        if self.active else None, item)
                return menu
        tray = Tray()
        self.tray = tray  # Preserve the exact native owner if release is uncertain.
        try:
            icon = self.frame.GetIcon()
            if not icon.IsOk():
                icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
            if not tray.SetIcon(icon, 'Resonance — open the menu to show the window'):
                raise RuntimeError('The system tray did not accept the icon.')
            tray.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK,
                      lambda event: self.invoke('desktop.show') if tray.active else None)
        except Exception:
            tray.active = False
            if tray.IsIconInstalled() and not tray.RemoveIcon():
                raise RuntimeError('The failed tray installation still needs cleanup. Retry Disable.')
            tray.Destroy()
            self.tray = None
            raise

    def save_preferences(self, preferences):
        if not self._live() or self.hotkeys._registered:
            raise ValueError('Disable global shortcuts before changing saved preferences.')
        self.hotkeys.validate(preferences.global_shortcuts)
        previous = self.tray is not None
        self.set_tray(preferences.tray_icon)
        try:
            self.store.save(preferences)
        except Exception:
            self.set_tray(previous)
            raise
        self.preferences = preferences

    def change_root(self):
        # Every old scope loses authority before fallible native release/load.
        self.watch.disable()
        self.reader = self._assistant_reader = None
        self.timer.Stop()
        errors = []
        for release in (self.hotkeys.disable, lambda: self.set_tray(False)):
            try:
                release()
            except Exception as exc:
                errors.append(str(exc))
        self.preferences = DesktopPreferences()
        self.store = DesktopPreferenceStore(self.frame.library.root)
        try:
            self.preferences = self.store.load()
        except Exception as exc:
            errors.append('New desktop preferences are unavailable: ' + str(exc))
        self._refresh()
        self.activate_saved_tray()
        if errors:
            self.frame.report('Desktop choices need attention: ' + '; '.join(errors))

    def activate_saved_tray(self):
        """Called after the existing command inventory captures every menu."""
        if self.preferences.tray_icon:
            try:
                self.set_tray(True)
            except Exception as exc:
                self.frame.report('Saved tray preference is unavailable; the main window remains open. ' + str(exc))

    def close(self):
        # Revocation precedes fallible native release. A failed release keeps
        # this same owner available for retry, without enabling late events.
        self.closed = True
        self.reader = self._assistant_reader = None
        self.watch.close()
        errors = []
        if not self._detached:
            try:
                self.timer.Stop()
                if not self._owner_destroyed:
                    self.frame.Unbind(self.wx.EVT_TIMER, source=self.timer, handler=self._tick)
                    self.frame.Unbind(self.wx.EVT_WINDOW_DESTROY, handler=self._destroyed)
                self._detached = True
            except Exception as exc:
                errors.append(str(exc))
        try:
            self.hotkeys.close(owner_destroyed=self._owner_destroyed)
        except Exception as exc:
            errors.append(str(exc))
        if self.tray is not None:
            old = self.tray
            old.active = False
            try:
                if old.IsIconInstalled() and not old.RemoveIcon():
                    raise RuntimeError('The desktop did not confirm tray-icon removal; retry Close.')
                old.Destroy()
                self.tray = None
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('Desktop cleanup needs attention: ' + '; '.join(errors))

    def _destroyed(self, event):
        if event.GetId() == self._owner_id:
            self._owner_destroyed = True
            try:
                self.close()
            except Exception as exc:
                self.cleanup_error = str(exc)
        event.Skip()


def install_desktop_integration(frame):
    """Add accessible native menus before the existing KeyboardActions capture."""
    import wx
    from .desktop_settings import show_desktop_settings
    controller = DesktopIntegration(frame)
    frame.desktop_integration = controller
    menu = wx.Menu()
    frame.GetMenuBar().Append(menu, '&Desktop')
    actions = (
        ('desktop.settings', 'Desktop integration...', lambda e: show_desktop_settings(frame, controller)),
        ('desktop.show', 'Show Resonance window', controller.show_window),
        ('desktop.hide', 'Hide window to tray', controller.hide_window),
        ('desktop.clipboard_watch', 'Watch clipboard for this library selection',
         lambda e: controller.watch_clipboard(not controller.watch.enabled)),
        ('voice.watched_clipboard', 'Open watched clipboard...', controller.open_watched),
        ('voice.pause', 'Pause or resume current voice reader', lambda e: controller.invoke('voice.pause')),
        ('voice.stop', 'Stop current voice reader', lambda e: controller.invoke('voice.stop')),
    )
    for identity, label, handler in actions:
        item = (menu.AppendCheckItem(wx.ID_ANY, label) if identity == 'desktop.clipboard_watch'
                else menu.Append(wx.ID_ANY, label))
        def invoke(event, handler=handler):
            if controller._live():
                try:
                    handler(event)
                except Exception as exc:
                    frame.report(str(exc))
        frame.Bind(wx.EVT_MENU, invoke, item)
        controller.items[identity] = item
    controller._refresh()
    return controller
