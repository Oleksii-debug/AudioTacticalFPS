"""Explicit session controls for one existing live reader.

No key is active by default. Nothing is persisted, synthesized or played here.
The existing LiveReadingControls retains all source, worker and device ownership.
"""
from dataclasses import dataclass
from itertools import count
import re
import sys

from .command_registry import normalize_shortcut


_ACTIONS = (('show', 'Show this reader'),
            ('pause', 'Pause or resume voice reading'),
            ('stop', 'Stop voice reading'))
# RegisterHotKey uses a positive application ID, not wx's negative window IDs.
# IDs are never reused in this process, so queued old notifications cannot become
# commands for a later configuration.
_HOTKEY_IDS = count(0x5000)


def _shortcut(value):
    result = normalize_shortcut(value)
    if not result:
        return result
    parts = result.split('+')
    modifiers, key = set(parts[:-1]), parts[-1]
    if ('Ctrl' not in modifiers or not modifiers & {'Alt', 'Shift'}
            or not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[01])', key)):
        raise ValueError('Use Ctrl with Alt or Shift and a letter, digit or F1-F11. '
                         'Native editing, Windows keys and F12 remain unavailable.')
    return result


@dataclass(frozen=True)
class SessionOptions:
    tray: bool = False
    show: str = ''
    pause: str = ''
    stop: str = ''

    def __post_init__(self):
        if type(self.tray) is not bool:
            raise ValueError('Choose explicitly whether to show the tray icon.')
        occupied = set()
        for name, _label in _ACTIONS:
            value = _shortcut(getattr(self, name))
            if value and value in occupied:
                raise ValueError('Choose a different session hotkey for each action.')
            occupied.add(value)
            object.__setattr__(self, name, value)

    @property
    def enabled(self):
        return self.tray or any(getattr(self, name) for name, _ in _ACTIONS)

    @property
    def bindings(self):
        return tuple((name, getattr(self, name)) for name, _ in _ACTIONS
                     if getattr(self, name))


def _local_shortcuts(controls):
    """Use the active canonical keymap, without opening or rewriting settings."""
    manager = getattr(controls._host, 'keyboard_commands', None)
    if manager is None:
        from .builtin_keyboard_commands import builtin_commands
        values = [spec.default_shortcut for spec in builtin_commands()]
    else:
        values = list(manager.registry.effective(manager.overrides).values())
        values.extend(getattr(manager, '_unmanaged', ()))
    # The editable-copy Save As accelerator is owned by its existing dialog.
    # Ctrl+S/F/H need no duplicate rule here: global keys require two modifiers.
    values.append('Ctrl+Shift+S')
    return frozenset(value.casefold() for value in values if value)


class SessionSlot:
    """One explicitly selected reader in one running wx application."""
    def __init__(self):
        self.owner = None


class ReaderSessionControls:
    """Revoke dispatch first; retain uncertain native cleanup for explicit retry."""
    def __init__(self, controls, slot, native_factory):
        self.controls, self.slot, self.native_factory = controls, slot, native_factory
        self.core, self.dialog = controls.core, controls.dialog
        self.options = SessionOptions()
        self.epoch = 0
        self.enabled = False
        self.closed = False
        self.native = None

    def available(self):
        return (not self.closed and not self.controls.closed
                and self.controls.core is self.core and self.controls.dialog is self.dialog
                and bool(self.dialog))

    def require_current(self):
        if not self.available():
            raise ValueError('This reading window has closed or changed. Select its controls again.')

    def _check_keys(self, options):
        if not options.bindings:
            return
        occupied = _local_shortcuts(self.controls)
        if any(value.casefold() in occupied for _name, value in options.bindings):
            raise ValueError('A selected global hotkey belongs to the active application keymap. '
                             'Choose an unassigned combination; your keymap was not changed.')

    def _revoke(self):
        self.enabled = False
        self.epoch += 1

    def configure(self, options):
        if not isinstance(options, SessionOptions):
            raise ValueError('Review a complete session-controls selection.')
        self.require_current()
        self._check_keys(options)
        if self.slot.owner is not None and self.slot.owner is not self:
            raise ValueError('Another reader owns the session controls. Disable its controls '
                             'or retry their pending cleanup before choosing this reader.')
        if not options.enabled:
            return self.disable()
        if self.enabled and self.options == options:
            return True
        if not self.disable():
            raise RuntimeError('Previous session controls could not confirm cleanup. Retry cleanup first.')
        self.require_current()
        self.slot.owner = self
        self.epoch += 1
        epoch = self.epoch
        try:
            self.native = self.native_factory(self, epoch)
            self.require_current()
            self.native.start(options)
            self.require_current()
            self._check_keys(options)
            if self.slot.owner is not self or epoch != self.epoch:
                raise ValueError('The selected reader changed while enabling session controls.')
            self.options, self.enabled = options, True
            return True
        except (Exception, KeyboardInterrupt):
            self.disable()
            raise

    def disable(self):
        self._revoke()
        self.options = SessionOptions()
        if self.native is not None:
            try:
                confirmed = self.native.release() is True
            except (Exception, KeyboardInterrupt):
                confirmed = False
            if not confirmed:
                # These exact native IDs and their receiver remain owned. A new
                # reader must not silently acquire a replacement session.
                self.slot.owner = self
                return False
            self.native = None
        if self.slot.owner is self:
            self.slot.owner = None
        return True

    def detach(self):
        """Pure lifetime latch; safe inside a native destruction notification."""
        self.closed = True
        self._revoke()

    def close(self):
        self.detach()
        return self.disable()

    def status(self):
        if self.native is not None and not self.enabled:
            return ('Controls are inactive, but native cleanup could not be confirmed. '
                    'Use Disable / retry cleanup. If the native receiver is gone, close '
                    'the application before enabling a new session.')
        if self.enabled:
            rows = [f'{label}: {getattr(self.options, name)}'
                    for name, label in _ACTIONS if getattr(self.options, name)]
            return ('Enabled for this reader until disabled or this reader closes. '
                    + ('The tray icon is visible. ' if self.options.tray else '')
                    + '; '.join(rows))
        return 'Tray and global hotkeys are off. No selection is saved after application exit.'

    def invoke(self, action, epoch, *, generation=None):
        if (not self.enabled or self.slot.owner is not self or epoch != self.epoch
                or not self.available()):
            return False
        if generation is not None and generation != self.controls._start_generation:
            return False
        try:
            self._check_keys(self.options)
        except Exception as exc:
            self.disable()
            if self.available():
                self.controls._report(str(exc) + ' Session controls were disabled.')
            return False
        if action == 'show':
            self.dialog.Show()
            if not self.available() or epoch != self.epoch:
                return False
            if self.dialog.IsIconized():
                self.dialog.Iconize(False)
            if self.available() and epoch == self.epoch:
                self.dialog.Raise()
            return True
        if action == 'pause':
            if self.core.busy and self.core.status().state in ('loading', 'rendering', 'playing', 'paused'):
                self.controls.pause()
                return True
            return False
        if action == 'stop':
            self.controls.stop()
            return True
        return False


def _event_id(event):
    try:
        return event.GetId()
    except (AttributeError, RuntimeError):
        return None


class _WindowsHotKeys:
    """Use MOD_NOREPEAT, which wxWidgets 3.2's modifier translation drops.

    Native notifications still reach the existing wx child through EVT_HOTKEY.
    This is RegisterHotKey, not a system keyboard hook or a polling thread.
    """
    def __init__(self, handle):
        import ctypes
        from ctypes import wintypes
        if sys.platform != 'win32':
            raise RuntimeError('Global hotkeys require Windows.')
        if type(handle) is not int or handle <= 0:
            raise ValueError('A live native receiver is required; thread-wide registration is unavailable.')
        self.handle = handle
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, identity, modifiers, key):
        return bool(self.api.RegisterHotKey(self.handle, identity, modifiers | 0x4000, key))

    def unregister(self, identity):
        return bool(self.api.UnregisterHotKey(self.handle, identity))


class _WxSession:
    """A child event receiver cannot keep the application alive after exit."""
    def __init__(self, owner, epoch):
        import wx
        if sys.platform != 'win32':
            raise RuntimeError('Session tray controls and global hotkeys require Windows.')
        self.wx, self.owner, self.epoch = wx, owner, epoch
        self.panel = None
        self.panel_alive = False
        self.keys = {}
        self.tray = None
        host = owner.controls._host
        if not host:
            raise ValueError('The desktop owner has closed.')
        panel = wx.Panel(host)
        self.panel = panel
        self.panel_id = panel.GetId()
        self.panel_alive = True
        try:
            panel.Hide()
            panel.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.hotkeys = _WindowsHotKeys(panel.GetHandle())
        except BaseException:
            panel.Destroy()
            raise

    def destroyed(self, event):
        if _event_id(event) == self.panel_id:
            self.panel_alive = False
            if self.keys:
                self.owner.detach()
        event.Skip()

    def _current(self):
        self.owner.require_current()
        if (self.owner.slot.owner is not self.owner or self.owner.epoch != self.epoch
                or not self.panel_alive or not self.panel):
            raise ValueError('The native session receiver changed.')

    def _invoke(self, action, *, generation=None):
        try:
            self.owner.invoke(action, self.epoch, generation=generation)
        except Exception as exc:
            if self.owner.available():
                self.owner.controls._report(exc)

    def start(self, options):
        wx = self.wx
        for action, shortcut in options.bindings:
            self._current()
            identity = next(_HOTKEY_IDS)
            if identity > 0xBFFF:
                raise RuntimeError('This session exhausted its native hotkey IDs. Restart the application.')
            pieces = shortcut.split('+')
            modifiers = 0
            for piece in pieces[:-1]:
                modifiers |= {'Ctrl': 0x0002, 'Alt': 0x0001, 'Shift': 0x0004}[piece]
            key = pieces[-1]
            keycode = 0x70 + int(key[1:]) - 1 if key.startswith('F') and len(key) > 1 else ord(key)
            handler = lambda event, selected=action: self._invoke(selected)
            self.panel.Bind(wx.EVT_HOTKEY, handler, id=identity)
            # Retain the candidate on an exception: the native call may already
            # have succeeded. A documented False return means collision/no grant.
            self.keys[identity] = handler
            if self.hotkeys.register(identity, modifiers, keycode) is not True:
                del self.keys[identity]
                self.panel.Unbind(wx.EVT_HOTKEY, id=identity)
                raise ValueError(f'{shortcut} is unavailable. Choose another session hotkey.')
            self._current()
        if options.tray:
            self._current()
            import wx.adv
            if not wx.adv.TaskBarIcon.IsAvailable():
                raise RuntimeError('The Windows tray is unavailable.')
            session = self

            class TrayIcon(wx.adv.TaskBarIcon):
                def CreatePopupMenu(self):
                    owner = session.owner
                    if not owner.available() or not owner.enabled or owner.epoch != session.epoch:
                        return None
                    generation = owner.controls._start_generation
                    menu = wx.Menu()
                    for action, label in _ACTIONS:
                        item = menu.Append(wx.ID_ANY, label)
                        if action == 'pause':
                            item.Enable(owner.core.busy and owner.core.status().state in
                                        ('loading', 'rendering', 'playing', 'paused'))
                        menu.Bind(wx.EVT_MENU,
                            lambda event, chosen=action: session._invoke(chosen, generation=generation),
                            id=item.GetId())
                    menu.AppendSeparator()
                    item = menu.Append(wx.ID_ANY, 'Disable these session controls')
                    def disable(event):
                        if owner.epoch == session.epoch and owner.slot.owner is owner:
                            confirmed = owner.disable()
                            if owner.available():
                                owner.controls._report(owner.status())
                    menu.Bind(wx.EVT_MENU, disable, id=item.GetId())
                    return menu

            self.tray = TrayIcon()
            self.tray.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda event: self._invoke('show'))
            icon = wx.ArtProvider.GetIcon(wx.ART_INFORMATION, wx.ART_OTHER, (16, 16))
            if not icon.IsOk() or self.tray.SetIcon(icon, 'Resonance: current voice reader') is not True:
                raise RuntimeError('The session tray icon could not be shown.')
            self._current()

    def release(self):
        confirmed = True
        for identity in tuple(self.keys):
            try:
                if (not self.panel_alive or not self.panel
                        or self.hotkeys.unregister(identity) is not True):
                    confirmed = False
                    continue
                del self.keys[identity]
                self.panel.Unbind(self.wx.EVT_HOTKEY, id=identity)
            except (Exception, KeyboardInterrupt):
                confirmed = False
        if self.tray is not None:
            try:
                if self.tray.IsIconInstalled() and self.tray.RemoveIcon() is not True:
                    confirmed = False
                else:
                    # wx documents delayed Destroy with no return value.
                    self.tray.Destroy()
                    self.tray = None
            except (Exception, KeyboardInterrupt):
                confirmed = False
        if confirmed and not self.keys and self.tray is None:
            if self.panel_alive and self.panel:
                try:
                    if self.panel.Destroy() is False:
                        return False
                except (Exception, KeyboardInterrupt):
                    return False
            self.panel_alive = False
            return True
        return False


def _slot(wx):
    app = wx.GetApp()
    if app is None:
        raise RuntimeError('Open the desktop application before choosing session controls.')
    value = getattr(app, '_carr_reader_session_controls', None)
    if value is None:
        value = SessionSlot()
        app._carr_reader_session_controls = value
    if not isinstance(value, SessionSlot):
        raise RuntimeError('The application session-controls owner is unavailable.')
    return value


def create_session_dialog(controls):
    import wx
    if sys.platform != 'win32':
        raise RuntimeError('Tray controls and session global hotkeys are available in Windows builds.')
    owner = getattr(controls, '_background_controls', None)
    if owner is None:
        owner = ReaderSessionControls(controls, _slot(wx), _WxSession)
        controls._background_controls = owner
        native_id = controls.dialog.GetId()
        def destroyed(event):
            if _event_id(event) == native_id:
                owner.detach()
                # Never touch the dying reader's controls inside this event.
                # The separate child receiver belongs to the desktop. If the
                # loop is closing, the exact owner remains retained in its slot.
                try:
                    wx.CallAfter(owner.close)
                except (Exception, KeyboardInterrupt):
                    pass
            event.Skip()
        controls.dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    owner.require_current()

    class SessionDialog(wx.Dialog):
        def __init__(self):
            super().__init__(controls.dialog, title='Tray and session hotkeys', size=(640, 430))
            self.alive = True
            self.native_id = self.GetId()
            layout = wx.BoxSizer(wx.VERTICAL)
            text = ('Controls apply only to this reader. Show brings it forward without starting speech. '
                    'Nothing is enabled automatically or saved for the next application launch.')
            layout.Add(wx.StaticText(self, label=text), 0, wx.ALL | wx.EXPAND, 8)
            self.tray = wx.CheckBox(self, label='Show a tray icon for this reader')
            self.tray.SetName('Enable tray icon for this reader')
            self.tray.SetValue(owner.options.tray)
            layout.Add(self.tray, 0, wx.ALL, 8)
            self.hotkeys = wx.CheckBox(self, label='Enable the following global hotkeys for this session')
            self.hotkeys.SetName('Explicitly enable session global hotkeys')
            self.hotkeys.SetValue(bool(owner.options.bindings))
            layout.Add(self.hotkeys, 0, wx.ALL, 8)
            self.fields = {}
            for name, label in _ACTIONS:
                layout.Add(wx.StaticText(self, label=label + ' (blank means unassigned)'), 0, wx.LEFT | wx.RIGHT, 8)
                field = wx.TextCtrl(self, value=getattr(owner.options, name))
                field.SetName(label + ' session hotkey')
                layout.Add(field, 0, wx.ALL | wx.EXPAND, 5)
                self.fields[name] = field
            self.notice = wx.TextCtrl(self, value=owner.status(), style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.notice.SetName('Session controls status')
            layout.Add(self.notice, 1, wx.ALL | wx.EXPAND, 8)
            row = wx.BoxSizer(wx.HORIZONTAL)
            for label, action in (('Apply for this reader', self.apply),
                                  ('Disable / retry cleanup', self.disable),
                                  ('Close', self.closing)):
                button = wx.Button(self, label=label)
                button.SetName(label)
                button.Bind(wx.EVT_BUTTON, action)
                row.Add(button, 0, wx.ALL, 5)
                if label == 'Close':
                    self.SetEscapeId(button.GetId())
            layout.Add(row, 0, wx.ALL, 3)
            self.SetSizer(layout)
            self.Bind(wx.EVT_CLOSE, self.closing)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)

        def current(self):
            return self.alive and bool(self) and owner.available()

        def report(self, message):
            if self.current():
                self.notice.ChangeValue(str(message))

        def apply(self, event=None):
            if not self.current():
                return
            try:
                use_keys = self.hotkeys.GetValue()
                if not self.current():
                    return
                show_tray = self.tray.GetValue()
                if not self.current():
                    return
                values = {}
                for name, _label in _ACTIONS:
                    if not self.current():
                        return
                    values[name] = self.fields[name].GetValue() if use_keys else ''
                if use_keys and not any(value.strip() for value in values.values()):
                    raise ValueError('Enter at least one explicit session hotkey, or turn global hotkeys off.')
                selected = SessionOptions(show_tray, **values)
                if not self.current():
                    return
                owner.configure(selected)
                self.report(owner.status())
            except Exception as exc:
                self.report(str(exc) + '\n' + owner.status())

        def disable(self, event=None):
            if not self.current():
                return
            selected = owner.slot.owner or owner
            selected.disable()
            self.report(selected.status())

        def closing(self, event=None):
            if not self.alive:
                return
            self.alive = False
            if self:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                elif self:
                    self.Destroy()

        def destroyed(self, event):
            if _event_id(event) == self.native_id:
                self.alive = False
            event.Skip()

        def Destroy(self):
            self.alive = False
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    return SessionDialog()


def show_session_controls(controls, event=None):
    if controls.closed:
        return
    dialog = None
    try:
        dialog = create_session_dialog(controls)
        dialog.ShowModal()
    except Exception as exc:
        owner = getattr(controls, '_background_controls', None)
        if (not controls.closed and controls.dialog
                and (owner is None or owner.available())):
            controls._report(exc)
    finally:
        if dialog:
            dialog.Destroy()


def close_session_controls(controls):
    owner = getattr(controls, '_background_controls', None)
    return owner.close() if owner is not None else True
