"""Real native input support for standalone acceptance probes, not the product.

Reuse the HWND foreground checks and MainLoop-driven key-down/key-up boundary
from scripts/windows_citation_styles_smoke.py. Thread-local wx focus alone does
not prove that Windows input reaches a top-level window. An accepted SendInput
call alone also does not prove that an accelerator or button was dispatched.
No synthetic command, button or key event is delivered to a widget here.
"""
import json
import sys


class NativeInput:
    def __init__(self, wx, application, *, simulator=None, healthy=lambda: None):
        self.wx, self.application, self.healthy = wx, application, healthy
        self.simulator = simulator if simulator is not None else wx.UIActionSimulator()
        self.user32 = None
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            self.user32 = ctypes.WinDLL('user32', use_last_error=True)
            self.user32.GetForegroundWindow.argtypes = []
            self.user32.GetForegroundWindow.restype = wintypes.HWND
            self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            self.user32.SetForegroundWindow.restype = wintypes.BOOL
            self.user32.GetFocus.argtypes = []
            self.user32.GetFocus.restype = wintypes.HWND

    def _focused(self, control):
        current = self.wx.Window.FindFocus()
        while current is not None:
            if current is control:
                return True
            current = current.GetParent()
        return False

    def _foreground(self, top):
        return (self.user32 is None or
                top.IsActive() and self.user32.GetForegroundWindow() == top.GetHandle())

    def diagnostic(self, reason, control=None):
        current = self.wx.Window.FindFocus()
        target = control if control is not None else current
        top = self.wx.GetTopLevelParent(target) if target is not None else None
        print(json.dumps({
            'native_input_diagnostic': reason,
            'main_loop_exists': self.application.GetMainLoop() is not None,
            'wx_focus': current.GetName() if current is not None else None,
            'target': target.GetName() if target is not None else None,
            'top_handle': int(top.GetHandle()) if top is not None else None,
            'top_active': top.IsActive() if top is not None else None,
            'foreground_handle': int(self.user32.GetForegroundWindow() or 0) if self.user32 else None,
            'thread_focus_handle': int(self.user32.GetFocus() or 0) if self.user32 else None,
        }, sort_keys=True), flush=True)

    def focus(self, control, pump):
        """Use the probe's existing bounded wait; require actual Windows ownership."""
        top = self.wx.GetTopLevelParent(control)
        top.Raise()
        if self.user32 is not None:
            self.user32.SetForegroundWindow(top.GetHandle())
        if not pump(lambda: self._foreground(top)):
            self.diagnostic('Windows foreground activation did not arrive', control)
            raise AssertionError('The native probe window did not acquire foreground activation.')
        control.SetFocus()
        if not pump(lambda: self._focused(control) and self._foreground(top)):
            self.diagnostic('native focus or foreground ownership did not arrive', control)
            raise AssertionError('Native keyboard focus or foreground ownership was not acquired.')
        self.diagnostic('native focus and foreground ready', control)

    def _key(self, keycode, modifiers):
        # Use the simulator's documented paired-modifier contract. Leave time
        # for the real MSW loop to translate key-down before its paired release.
        if not self.simulator.KeyDown(keycode, modifiers):
            raise AssertionError('The native input backend rejected key-down.')
        try:
            yield 40
        finally:
            if not self.simulator.KeyUp(keycode, modifiers):
                raise AssertionError('The native input backend rejected key-up.')
        yield 80

    def press(self, keycode, modifiers=0):
        """Inject real keys with a live native loop, including after modal close.

        Existing modal/MainLoop callers already own a native dispatch loop.
        Standalone nonmodal probe code needs an actual MainLoop for MSW
        accelerator translation; repeated wx.Yield calls are not a substitute.
        """
        self.healthy()
        current = self.wx.Window.FindFocus()
        if current is None or not self._foreground(self.wx.GetTopLevelParent(current)):
            self.diagnostic('refusing native key without foreground focus', current)
            raise AssertionError('Native input requires the focused foreground window.')
        loop = self.application.GetMainLoop()
        modal = any(isinstance(window, self.wx.Dialog) and window.IsModal()
                    for window in self.wx.GetTopLevelWindows())
        if modal or loop is not None and loop.IsRunning():
            return bool(self.simulator.Char(keycode, modifiers))

        self.diagnostic('starting native MainLoop for key dispatch', current)
        completed, errors, timers = [], [], []
        keys = self._key(keycode, modifiers)

        def advance():
            try:
                self.healthy()
                delay = next(keys)
            except StopIteration:
                completed.append(True)
                self.application.ExitMainLoop()
            except BaseException as error:
                errors.append(error)
                self.application.ExitMainLoop()
            else:
                timers.append(self.wx.CallLater(delay, advance))

        def expired():
            errors.append(TimeoutError('Native key dispatch exceeded its three-second boundary.'))
            self.application.ExitMainLoop()

        watchdog = self.wx.CallLater(3000, expired)
        timers.append(self.wx.CallLater(0, advance))
        try:
            self.application.MainLoop()
        finally:
            watchdog.Stop()
            for timer in timers:
                timer.Stop()
            keys.close()  # Always release a held key if the loop ends early.
        if errors:
            raise errors[0]
        if not completed:
            raise AssertionError('The native loop stopped before delivering the key sequence.')
        return True
