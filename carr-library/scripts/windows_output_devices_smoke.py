"""Installed native output selection and lifetime evidence.

The keyboard/modal proof uses explicitly labelled synthetic endpoint choices.
WinMM enumeration is real. If this runner exposes an actual endpoint, a short
silent PCM exercises the existing MCI transport too. Physical routing/listening
and NVDA always remain separate, and an empty device inventory is reported.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.audio import output_devices as devices
    from carr_library.audio.output_devices import OutputDevice
    from carr_library.reader.live_reading import LiveStatus
    from carr_library.speech.player import WavePlayer
    from carr_library.ui.live_reading import LiveReadingControls
    from carr_library.ui import output_devices as selection

    checks, errors = [], []
    observed = {'native_inventory': 'NOT_RUN', 'native_transport': 'NOT_RUN',
                'physical_routing_and_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
    installed = Path(carr_library.__file__).resolve()
    check('application is the actual installed package', 'site-packages' in str(installed).lower())
    check('native wx Windows backend', wx.Platform == '__WXMSW__')
    actual = devices.output_devices()
    check('real WinMM inventory obeys fixed bounds', len(actual) <= 256)
    check('real endpoint identities are unique', len({item.endpoint_id for item in actual}) == len(actual))
    observed.update(native_inventory='PASS', available_output_devices=len(actual))
    if actual:
        with TemporaryDirectory(prefix='carr-output-device-native-') as folder:
            recording = Path(folder) / 'Українська 🎧.wav'
            with wave.open(str(recording), 'wb') as stream:
                stream.setparams((1, 2, 22050, 0, 'NONE', 'PCM'))
                stream.writeframes(b'\0\0' * 2205)
            original = sha256(recording.read_bytes()).hexdigest()
            player = WavePlayer(output_device=actual[0])
            try:
                player.play(recording)
                check('real MCI retains explicitly selected endpoint', player.output_device is actual[0])
            finally:
                check('real MCI confirms native closure', player.close() is True and not player._open)
            check('native output does not change source PCM', sha256(recording.read_bytes()).hexdigest() == original)
            observed['native_transport'] = 'PASS'
    else:
        observed['native_transport_reason'] = 'This runner exposes no audio endpoint; no physical playback is inferred.'

    class Application(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = Application(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    held = [None]
    owners, state = [], {}
    rows = (OutputDevice(0, 'synthetic:headphones', 'Fixture headphones'),
            OutputDevice(1, 'synthetic:speakers', 'Fixture speakers'))
    original_devices, original_choices = devices.output_devices, selection.output_devices
    devices.output_devices = selection.output_devices = lambda: rows
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL

    def release():
        if held[0] is not None:
            code, modifiers = held[0]
            held[0] = None
            simulator.KeyUp(code, modifiers)

    def fail():
        release()
        gui.ExitMainLoop()

    def later(callback, delay=30):
        def invoke():
            if errors:
                return
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc())
                fail()
        return wx.CallLater(delay, invoke)

    def until(predicate, complete, timeout=8):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('Native output selection stage exceeded its bound.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        top, process = wx.GetTopLevelParent(control), wintypes.DWORD()
        handle = user32.GetForegroundWindow()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        ready = (process.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
        return ready

    def key(control, code, complete):
        def down():
            check('native target has foreground and focus', foreground(control))
            held[0] = (code, wx.MOD_NONE)
            check('native key accepted', simulator.KeyDown(code, wx.MOD_NONE))
            later(lambda: (release(), later(complete)), 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def modal():
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.SingleChoiceDialog)
                     and window.GetTitle() == 'Playback output device'), None)

    def list_control(dialog):
        pending = list(dialog.GetChildren())
        while pending:
            control = pending.pop(0)
            if isinstance(control, wx.ListBox):
                return control
            pending.extend(control.GetChildren())
        raise AssertionError('Native output choice list was not found.')

    class Core:
        def __init__(self, source, **options):
            self.busy, self.quiescent, self.silence_uncertain = False, True, False
            self.output_device, self.closed, self.starts = None, False, 0
            self.changes = []
        def status(self):
            return LiveStatus()
        def set_output_device(self, selected):
            assert not self.closed and not self.busy
            self.output_device = selected
            self.changes.append(selected)
        def close(self):
            self.closed = True
            return None
        def stop(self, reason=None):
            pass
        def start(self, *args, **kwargs):
            self.starts += 1
            raise AssertionError('Device review must not start reading.')

    def create_owner():
        outer = wx.Frame(None, title='Output review parent', size=(1200, 700))
        class Owner(wx.Dialog):
            def Destroy(self):
                if hasattr(self, 'reading'):
                    self.reading.close()
                return super().Destroy() if self else False
        dialog = Owner(outer, title='Installed output selection proof', size=(1180, 680),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        text = wx.TextCtrl(dialog, value='Exact reading text — Українська 🎧.', style=wx.TE_MULTILINE)
        layout.Add(text, 1, wx.EXPAND | wx.ALL, 5)
        source = SimpleNamespace(supports_resume=False)
        controls = LiveReadingControls(dialog, layout, text, source,
            current=lambda: (1, text.GetValue()), show_section=lambda value: None,
            core_factory=Core, ready=lambda: bool(dialog))
        dialog.reading = controls
        native_id = dialog.GetId()
        def destroyed(event):
            if event.GetId() == native_id:
                controls.close()
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        dialog.SetSizer(layout)
        owners.append((outer, dialog, controls))
        state.update(outer=outer, dialog=dialog, controls=controls)
        outer.Show()
        dialog.Show()
        return controls

    def open_choice(complete):
        controls = state['controls']
        key(controls.output_button, wx.WXK_SPACE,
            lambda: until(lambda: modal() is not None, complete))

    def first_choice():
        dialog = modal()
        chooser = list_control(dialog)
        key(chooser, wx.WXK_DOWN, lambda: key(chooser, wx.WXK_RETURN, first_applied))

    def first_applied():
        controls = state['controls']
        check('explicit native keyboard selection reaches current owner', controls.core.output_device == rows[0])
        check('device selection never starts speech', controls.core.starts == 0)
        open_choice(cancel_choice)

    def cancel_choice():
        key(list_control(modal()), wx.WXK_ESCAPE, cancelled)

    def cancelled():
        controls = state['controls']
        check('native Escape preserves previous explicit device', controls.core.output_device == rows[0])
        check('cancel does not apply another output', controls.core.changes == [rows[0]])
        open_choice(default_choice)

    def default_choice():
        chooser = list_control(modal())
        key(chooser, wx.WXK_HOME, lambda: key(chooser, wx.WXK_RETURN, default_applied))

    def default_applied():
        controls = state['controls']
        check('default output requires explicit native confirmation', controls.core.output_device is None)
        check('only explicit selections applied', controls.core.changes == [rows[0], None])
        open_choice(direct_disposal)

    def direct_disposal():
        controls = state['controls']
        state['dialog'].Destroy()
        until(lambda: modal() is None, lambda: after_direct(controls))

    def after_direct(controls):
        check('direct native owner destruction closes reading controls', controls.closed and controls.core.closed)
        check('destroyed owner cannot apply modal selection', controls.core.changes == [rows[0], None])
        state['outer'].Destroy()
        create_owner()
        open_choice(parent_disposal)

    def parent_disposal():
        controls = state['controls']
        state['outer'].Destroy()
        until(lambda: modal() is None, lambda: after_parent(controls))

    def after_parent(controls):
        check('actual parent destruction closes borrowed controls', controls.closed and controls.core.closed)
        check('parent destruction cannot apply or start playback', controls.core.changes == [] and controls.core.starts == 0)
        observed['native_ui'] = 'PASS'
        gui.ExitMainLoop()

    def begin():
        create_owner()
        open_choice(first_choice)

    timeout = wx.CallLater(90000, lambda: (errors.append('Native output review exceeded 90 seconds.'), fail()))
    try:
        later(begin)
        gui.MainLoop()
    finally:
        timeout.Stop()
        release()
        devices.output_devices, selection.output_devices = original_devices, original_choices
        for outer, dialog, controls in owners:
            controls.close()
            if dialog:
                dialog.Destroy()
            if outer:
                outer.Destroy()
        gui.ProcessPendingEvents()
    if errors:
        print(json.dumps({'result': 'FAIL', 'checks': checks, 'errors': errors, **observed}, ensure_ascii=False))
        return 1
    check('complete native UI sequence executed', observed.get('native_ui') == 'PASS')
    print(json.dumps({'result': 'PASS', 'checks': checks, **observed}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'result': 'FAIL', 'error': traceback.format_exc(), 'nvda': 'NOT_RUN'}))
        raise
