"""Opt-in native tray, clipboard review and semantic global command dispatch.

All native operations run on the existing UI thread. There is no key hook,
clipboard history, background speech, command registry or playback backend here.
"""
from hashlib import sha256
import sys
import threading
import warnings

from ..desktop_preferences import DesktopPreferences, DesktopPreferenceStore, global_shortcut
from .command_registry import CommandSpec

# These are existing menu identities, not a second command registry. Runtime
# labels, availability and dispatch still come from the shared native manager.
_RETAINED_NATIVE_OWNERS = set()


def retained_desktop_cleanup():
    """Unreleased native owners remain reachable for an explicit UI-thread retry."""
    return tuple(_RETAINED_NATIVE_OWNERS)


GLOBAL_COMMANDS = ('desktop.show', 'voice.clipboard', 'voice.watched_clipboard',
                   'voice.reader', 'voice.pause', 'voice.stop', 'progress.query')


class ClipboardWatch:
    """Retain only the newest bounded snapshot in memory until explicit Open."""
    def __init__(self, read, scope, notice):
        self.read, self.scope, self.notice = read, scope, notice
        self.enabled, self.closed = False, False
        self.pending = self._scope = self._digest = self._error = None

    def enable(self):
        if self.closed:
            raise ValueError('This desktop window is closed.')
        self.disable()
        self._scope = self.scope()
        self.pending = self._digest = self._error = None
        self.enabled = True

    def disable(self):
        self.enabled = False
        self.pending = self._scope = self._digest = self._error = None

    def _same_scope(self):
        try:
            return self.scope() == self._scope
        except Exception:
            return False

    def poll(self):
        if self.closed or not self.enabled:
            return False
        if not self._same_scope():
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
        # A clipboard owner can service native events while text is read.
        # Re-attest the captured library before retaining even an inert copy.
        if self.closed or not self.enabled or not self._same_scope():
            self.disable()
            self.notice('Clipboard watching stopped because the library selection changed.')
            return False
        self._error = None
        fingerprint = sha256(text.encode('utf-8')).digest()
        if fingerprint == self._digest:
            return False
        self._digest, self.pending = fingerprint, text
        self.notice('New clipboard text is ready. Choose Open watched clipboard to review it; no speech has started.')
        return True

    def take(self):
        if self.closed or not self.enabled or not self._same_scope():
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
        self.thread = threading.get_ident()
        self.handle = frame.GetHandle()
        if not self.handle:
            raise ValueError('Global shortcuts need a current native window handle.')
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
        self.api.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, identifier, shortcut):
        if threading.get_ident() != self.thread or self.frame.GetHandle() != self.handle:
            raise ValueError('The native global-shortcut owner changed.')
        parts = global_shortcut(shortcut).split('+')
        modifiers = 0x4000 | sum({'Alt': 1, 'Ctrl': 2, 'Shift': 4}[name] for name in parts[:-1])
        key = parts[-1]
        virtual = 0x70 + int(key[1:]) - 1 if key.startswith('F') else ord(key)
        return bool(self.api.RegisterHotKey(self.handle, identifier, modifiers, virtual))

    def unregister(self, identifier):
        if threading.get_ident() != self.thread or self.frame.GetHandle() != self.handle:
            raise ValueError('The native global-shortcut owner changed before release.')
        return bool(self.api.UnregisterHotKey(self.handle, identifier))


class GlobalHotkeys:
    """Own a bounded set of native registrations, never an OS keyboard hook."""
    def __init__(self, frame, manager, dispatch, *, wx_module=None, platform=None, native=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.frame, self.manager, self.dispatch = wx_module, frame, manager, dispatch
        self.platform = sys.platform if platform is None else platform
        self.enabled, self.closed = False, False
        self.native, self._dispatching = native, False
        self._registered, self._bindings = {}, {}
        self._bound = False
        self._native_event = getattr(self.wx, 'EVT_HOTKEY', None)
        if self._native_event is not None:
            self.frame.Bind(self._native_event, self._event)
            self._bound = True

    def validate(self, bindings):
        values = DesktopPreferences(global_shortcuts=bindings).global_shortcuts
        effective = self.manager.registry.effective(self.manager.overrides)
        for identity, shortcut in values.items():
            if identity not in GLOBAL_COMMANDS or identity not in self.manager.bindings:
                raise ValueError('This global action is unavailable in the current desktop: ' + identity)
            if self.manager.registry.get(identity).context != 'main':
                raise ValueError('Global actions must use an installed desktop command.')
            for other, current in effective.items():
                if current == shortcut and other != identity:
                    raise ValueError(shortcut + ' already belongs to ' + self.manager.registry.get(other).label + '.')
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
        if failures:
            raise RuntimeError('; '.join(failures))

    def _event(self, event):
        if self.closed or not self.enabled or self._dispatching or getattr(self.frame, '_closing', False):
            return
        identity = self._registered.get(event.GetId())
        if identity is None:
            return
        try:
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

    def close(self):
        self.closed = True
        try:
            self.disable()
        finally:
            if self._bound:
                self.frame.Unbind(self._native_event, handler=self._event)
                self._bound = False


class DesktopIntegration:
    def __init__(self, frame, *, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        self.wx, self.frame = wx_module, frame
        self.closed, self.tray, self.reader = False, None, None
        self._ui_thread = threading.get_ident()
        self._cleanup_complete = False
        self._timer_stopped = self._timer_unbound = self._destroy_unbound = False
        self._library = frame.library
        self.items = {}
        self.store = DesktopPreferenceStore(frame.library.root)
        self.preferences = DesktopPreferences()
        try:
            self.preferences = self.store.load()
        except Exception as exc:
            frame.report('Desktop preferences are unavailable; optional features are off. ' + str(exc))
        self.watch = ClipboardWatch(self._read, self.scope, self._notice)
        self.timer = self.wx.Timer(frame)
        self.frame.Bind(self.wx.EVT_TIMER, self._tick, self.timer)
        self.frame.Bind(self.wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.hotkeys = GlobalHotkeys(frame, frame.keyboard_commands, self.invoke, wx_module=self.wx)

    def _ui(self):
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError('Desktop native actions must run on their owning UI thread.')

    def scope(self):
        self._ui()
        if not self._live() or self.frame.library is not self._library:
            raise ValueError('The desktop library selection changed. Reopen this action.')
        self.store.guard()
        return (id(self._library), str(self._library.root), self.store._root_identity,
                self.frame.active)

    def _require_scope(self, expected):
        if self.scope() != expected:
            raise ValueError('The desktop library selection changed. Reopen this action.')

    def _notice(self, text):
        if not self._live():
            return
        self._ui()
        status = getattr(self.frame, 'status', None)
        if status is not None:
            status.ChangeValue(text)  # Passive observation must not move keyboard focus.
        else:
            self.frame.report(text)

    def _read(self):
        from .speech_dialog import _read_clipboard_text
        return _read_clipboard_text(self.wx)

    def _live(self):
        return not self.closed and not getattr(self.frame, '_closing', False)

    def available(self, identity):
        if not self._live():
            return False
        if identity == 'desktop.show':
            return True
        try:
            self.scope()
        except Exception:
            return False
        if identity in ('voice.pause', 'voice.stop'):
            current = self.reader
            if current is None:
                return False
            _token, dialog, controls, alive, scope = current
            try:
                return (alive() and scope == self.scope() and dialog.IsEnabled()
                        and controls[identity].IsEnabled())
            except Exception:
                return False
        if identity == 'voice.watched_clipboard' and self.watch.pending is None:
            return False
        return self.frame.keyboard_commands.available(identity)

    def invoke(self, identity):
        if self._live() and identity != 'desktop.show':
            try:
                self.scope()
            except Exception:
                self.watch.disable()
                self.timer.Stop()
                self.hotkeys.disable()
                self._refresh()
                return False
        if not self.available(identity):
            return False
        manager = self.frame.keyboard_commands
        manager.registry.get(identity)
        if identity in ('voice.pause', 'voice.stop'):
            # Send the same native button event as Space; source/cancel/quiet
            # checks remain in the existing reader handler, not this adapter.
            button = self.reader[2][identity]
            event = self.wx.CommandEvent(self.wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            return bool(button.GetEventHandler().ProcessEvent(event))
        if identity == 'desktop.show':
            item = manager.bindings[identity].item
            event = self.wx.CommandEvent(self.wx.EVT_MENU.typeId, item.GetId())
            event.SetEventObject(self.frame)
            return bool(self.frame.GetEventHandler().ProcessEvent(event))
        return manager.invoke(identity)

    def attach_reader(self, dialog, pause, stop, alive):
        if not self._live() or self.reader is not None:
            raise ValueError('Another reading window already owns the desktop playback controls.')
        scope = self.scope()
        token = object()
        self.reader = token, dialog, {'voice.pause': pause, 'voice.stop': stop}, alive, scope
        def detach():
            if self.reader is not None and self.reader[0] is token:
                self.reader = None
        return detach

    def show_window(self, event=None):
        self._ui()
        if not self._live():
            return
        self.frame.Show(True)
        self.frame.Iconize(False)
        if self.reader is not None and self.reader[3]():
            self.reader[1].Raise()
        else:
            self.frame.Raise()

    def hide_window(self, event=None):
        self._ui()
        if not self._live() or self.tray is None or not self.tray.IsIconInstalled():
            raise ValueError('Enable the available tray icon before hiding this window.')
        if not self.frame.IsEnabled() or getattr(self.frame, 'working', False):
            raise ValueError('Finish the active dialog or operation before hiding this window.')
        self.frame.Hide()

    def watch_clipboard(self, enabled):
        self._ui()
        if type(enabled) is not bool or not self._live():
            raise ValueError('Clipboard watching needs an open desktop window.')
        if enabled:
            self.watch.enable()
            if not self.timer.Start(1000):
                self.watch.disable()
                raise RuntimeError('The clipboard observation timer could not be started.')
            self.frame.report('Clipboard watching is on for this library selection. New text stays in memory until you explicitly open it.')
        else:
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
        # A scope switch clears data even while a modal/operation is active.
        if self.watch.enabled and not self.watch._same_scope():
            self.watch.disable()
            self.timer.Stop()
            self._notice('Clipboard watching stopped because the library selection changed.')
        elif self.frame.IsEnabled() and not getattr(self.frame, 'working', False):
            self.watch.poll()
        self._refresh()

    def _open_copy(self, text, scope, *, watched):
        self._require_scope(scope)
        from .speech_dialog import show_speech_reader
        show_speech_reader(
            self.frame, text=text,
            title='Watched clipboard reading copy' if watched else 'Clipboard reading copy',
            source_guard=lambda: self._require_scope(scope),
            recovery_slot='clipboard-watch' if watched else 'clipboard')

    def open_clipboard(self, event=None):
        if not self._live() or not self.frame.IsEnabled() or getattr(self.frame, 'working', False):
            raise ValueError('Finish the active operation before opening clipboard text.')
        scope = self.scope()
        text = self._read()
        self._require_scope(scope)
        self._open_copy(text, scope, watched=False)

    def open_watched(self, event=None):
        if not self._live() or not self.frame.IsEnabled() or getattr(self.frame, 'working', False):
            raise ValueError('Finish the active operation before opening watched text.')
        scope = self.scope()
        text = self.watch.take()
        self._refresh()
        self._open_copy(text, scope, watched=True)

    def _remove_tray(self):
        if self.tray is None:
            return
        old = self.tray
        old.active = False
        if old.IsIconInstalled() and not old.RemoveIcon():
            raise RuntimeError('The desktop did not confirm removal of its tray icon. Retry Disable.')
        # Keep the exact owner if either native operation fails.
        old.Destroy()
        self.tray = None

    def set_tray(self, enabled):
        self._ui()
        if type(enabled) is not bool or not self._live():
            raise ValueError('The tray icon needs an open desktop window.')
        if not enabled:
            if self.tray is not None:
                if not self.frame.IsShown():
                    self.show_window()
                self._remove_tray()
            return
        if self.tray is not None:
            if not self.tray.active:
                raise RuntimeError('Finish the previous tray-icon cleanup before enabling it again.')
            return
        self.scope()
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
                    command = controller.frame.keyboard_commands.registry.get(identity)
                    item = menu.Append(wx.ID_ANY, command.label)
                    item.Enable(self.active and controller.available(identity))
                    menu.Bind(wx.EVT_MENU,
                        lambda event, identity=identity: controller.invoke(identity)
                        if self.active else None, item)
                return menu
        tray = Tray()
        try:
            icon = self.frame.GetIcon()
            if not icon.IsOk():
                icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
            if not tray.SetIcon(icon, 'Resonance — open the menu to show the window'):
                raise RuntimeError('The system tray did not accept the icon.')
            tray.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK,
                      lambda event: self.invoke('desktop.show') if tray.active else None)
        except Exception as error:
            self.tray = tray
            try:
                self._remove_tray()
            except Exception as cleanup:
                _RETAINED_NATIVE_OWNERS.add(self)
                raise RuntimeError(str(error) + ' Tray cleanup needs attention: ' + str(cleanup)) from error
            raise
        self.tray = tray

    def save_preferences(self, preferences):
        if not self._live() or self.hotkeys._registered:
            raise ValueError('Disable global shortcuts before changing saved preferences.')
        self.scope()
        self.hotkeys.validate(preferences.global_shortcuts)
        previous = self.tray is not None
        self.set_tray(preferences.tray_icon)
        try:
            self.store.save(preferences)
        except Exception as error:
            try:
                self.set_tray(previous)
            except Exception as cleanup:
                raise RuntimeError(str(error) + ' Tray cleanup needs attention: ' + str(cleanup)) from error
            raise
        self.preferences = preferences

    def change_root(self):
        self._ui()
        if not self._live():
            raise ValueError('This desktop window is closed.')
        # Revoke every old selection's authority even if one native release fails.
        self.watch.disable()
        self.reader = None
        failures = []
        for release in (self.timer.Stop, self.hotkeys.disable, self._remove_tray):
            try:
                release()
            except Exception as error:
                failures.append(str(error))
        self._refresh()
        if failures:
            raise RuntimeError('Previous desktop cleanup needs attention: ' + '; '.join(failures))
        self._library = self.frame.library
        self.store = DesktopPreferenceStore(self._library.root)
        self.preferences = DesktopPreferences()
        self.preferences = self.store.load()
        if self.preferences.tray_icon:
            self.set_tray(True)

    def close(self):
        self._ui()
        if self._cleanup_complete:
            return
        # Authority ends before cleanup; failed handles stay strongly owned and
        # retryable even when a forced native Destroy releases the frame.
        _RETAINED_NATIVE_OWNERS.add(self)
        self.closed = True
        self.reader = None
        self.watch.close()
        errors = []
        if not self._timer_stopped:
            try:
                self.timer.Stop()
                self._timer_stopped = True
            except Exception as error:
                errors.append(str(error))
        if not self._timer_unbound:
            try:
                self.frame.Unbind(self.wx.EVT_TIMER, source=self.timer, handler=self._tick)
                self._timer_unbound = True
            except Exception as error:
                errors.append(str(error))
        for release in (self.hotkeys.close, self._remove_tray):
            try:
                release()
            except Exception as error:
                errors.append(str(error))
        if not errors and not self._destroy_unbound:
            try:
                self.frame.Unbind(self.wx.EVT_WINDOW_DESTROY, handler=self._destroyed)
                self._destroy_unbound = True
            except Exception as error:
                errors.append(str(error))
        if errors:
            self.cleanup_error = 'Desktop cleanup needs attention: ' + '; '.join(errors)
            raise RuntimeError(self.cleanup_error)
        self._cleanup_complete = True
        self.cleanup_error = None
        _RETAINED_NATIVE_OWNERS.discard(self)

    def _destroyed(self, event):
        if event.GetEventObject() is self.frame:
            try:
                self.close()
            except Exception:
                # No UI access after destruction. The module retains the owner
                # and diagnostic for an explicit retry on its original UI thread.
                try:
                    warnings.warn('Desktop native cleanup is unconfirmed; its owner remains retained.',
                                  RuntimeWarning, stacklevel=1)
                except Exception:
                    pass
        event.Skip()


def install_desktop_integration(frame):
    import wx
    from .builtin_keyboard_commands import plain_caption
    from .keyboard_settings import register_menu_command
    from .desktop_settings import show_desktop_settings
    controller = DesktopIntegration(frame)
    frame.desktop_integration = controller
    bar = frame.GetMenuBar()
    menu = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                if plain_caption(bar.GetMenuLabel(index)) == 'options')
    voice = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if plain_caption(bar.GetMenuLabel(index)) == 'voice')
    clipboard = voice.Append(wx.ID_ANY, 'Read clipboard text...')
    def open_clipboard(event):
        if controller._live():
            try:
                controller.open_clipboard(event)
            except Exception as error:
                frame.report(str(error))
    frame.Bind(wx.EVT_MENU, open_clipboard, clipboard)
    register_menu_command(frame, CommandSpec('voice.clipboard', 'Read clipboard text...'), clipboard)
    menu.AppendSeparator()
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
        register_menu_command(frame, CommandSpec(identity, label), item)
        controller.items[identity] = item
    controller._refresh()
    if controller.preferences.tray_icon:
        try:
            controller.set_tray(True)
        except Exception as exc:
            frame.report('Saved tray preference is unavailable; the main window remains open. ' + str(exc))
    return controller
