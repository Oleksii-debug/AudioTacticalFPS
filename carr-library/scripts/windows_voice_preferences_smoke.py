"""Actual native preference controls with synthetic declarations, never inference.

Run from source or an installed package; --installed requires its sealed origin.
The real registry's public Voice values, durable settings and serial worker are
used. This checks native controls/lifetime, not NVDA or acoustic quality.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    arguments = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This acceptance probe requires actual native Windows controls.')
    import wx
    from carr_library import runtime_settings
    from carr_library.speech import Voice
    from carr_library.ui import voice_preferences

    checks, errors, applied, futures = [], [], [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
    if arguments.installed:
        for module in (runtime_settings, voice_preferences):
            check('installed origin: ' + module.__name__,
                  'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
    app.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title='Local voice preference acceptance')
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    authorized = [True]
    callback_count = [0]
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voice-preference-acceptance')

    def guard():
        if not authorized[0]:
            raise ValueError('The active library changed.')

    def execute(work, done, *, on_failed):
        future = executor.submit(work)
        futures.append(future)
        callback_count[0] += 1
        def deliver():
            try:
                try:
                    result = future.result()
                except Exception as exc:
                    on_failed(exc)
                else:
                    done(result)
            finally:
                callback_count[0] -= 1
        future.add_done_callback(lambda _future: wx.CallAfter(deliver))
        return True

    def pump(predicate, message, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)

    def find(window, name):
        result = wx.Window.FindWindowByName(name, window)
        check('native control: ' + name, result is not None)
        return result

    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Native preference focus')
        check('native Space down', simulator.KeyDown(wx.WXK_SPACE))
        try:
            app.Yield()
        finally:
            check('native Space up', simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    with TemporaryDirectory(prefix='native-voice-preferences-') as temporary:
        state = Path(temporary) / 'state'
        voices = (Voice('native:uk', 'Українська декларація', 'Synthetic metadata only', 'uk'),
                  Voice('native:sk', 'Slovenské údaje', 'Synthetic metadata only', 'sk'))
        selected = {'voice_id': voices[0].id, 'language': 'uk', 'rate': 0, 'volume': 80}
        def drive():
            try:
                window, = [item for item in wx.GetTopLevelWindows()
                           if item.IsShown() and item.GetTitle() == 'Local voice preferences']
                check('opening preferences is silent and does not write',
                      not applied and not runtime_settings.settings_path(state).exists())
                press(find(window, 'Add or remove favorite'))
                pump(lambda: callback_count[0] == 0, 'Favorite save did not settle')
                check('native favorite persists', runtime_settings.load_voice_preferences(state)['favorites'] == ['native:uk'])
                press(find(window, 'Save default for language'))
                pump(lambda: callback_count[0] == 0, 'Default save did not settle')
                check('native exact-language default persists',
                      runtime_settings.load_voice_preferences(state)['defaults'] == {'uk': 'native:uk'})
                find(window, 'Voice profile name').ChangeValue('Нативний профіль')
                find(window, 'Profile synthesis rate, minus ten to ten').SetValue(-2)
                find(window, 'Profile volume, zero to one hundred').SetValue(55)
                press(find(window, 'Save voice profile'))
                pump(lambda: callback_count[0] == 0, 'Profile save did not settle')
                profile = find(window, 'Saved voice profiles')
                profile.SetSelection(0)
                event = wx.CommandEvent(wx.EVT_CHOICE.typeId, profile.GetId())
                event.SetEventObject(profile)
                profile.GetEventHandler().ProcessEvent(event)
                press(find(window, 'Use voice profile'))
                check('native profile applies exact declared controls', applied == [{
                    'voice_id': 'native:uk', 'language': 'uk', 'rate': -2, 'volume': 55}])
                original = runtime_settings.settings_path(state).read_bytes()
                authorized[0] = False
                press(find(window, 'Save default for language'))
                check('changed library refuses native save',
                      'library changed' in find(window, 'Voice preference status').GetValue()
                      and runtime_settings.settings_path(state).read_bytes() == original)
                authorized[0] = True
                press(find(window, 'Close voice preferences'))
            except Exception:
                errors.append(traceback.format_exc())
                for item in wx.GetTopLevelWindows():
                    if isinstance(item, wx.Dialog) and item.IsModal():
                        item.EndModal(wx.ID_CANCEL)

        def scenario():
            try:
                wx.CallLater(100, drive)
                voice_preferences.show_voice_preferences(frame, state_root=state, voices=voices,
                    preferences=runtime_settings.load_voice_preferences(state), selected=selected,
                    execute=execute, guard=guard, apply_selection=applied.append)
                pump(lambda: callback_count[0] == 0 and all(item.done() for item in futures),
                     'Native preference cleanup did not settle')
                check('closed modal has no pending worker callback', callback_count[0] == 0)
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                frame.Destroy()
                wx.CallAfter(app.ExitMainLoop)

        try:
            frame.Show()
            app.SetTopWindow(frame)
            wx.CallAfter(scenario)
            app.MainLoop()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    if errors:
        raise RuntimeError('\n'.join(errors))
    print(json.dumps({'status': 'passed', 'checks': checks, 'native': True,
                      'acoustic_quality': 'NOT_TESTED', 'NVDA': 'NOT_TESTED'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
