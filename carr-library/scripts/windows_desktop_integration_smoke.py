"""Actual Windows clipboard, tray, global keys and canonical reader controls.

Uses synthetic clipboard/library text in the acceptance desktop. No TTS audio,
microphone, listening-quality or human NVDA claim. Missing native prerequisites
are failures here, never simulated passes.
"""
import argparse
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    options = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows and its interactive native desktop.')
    import wx
    import wx.adv
    from carr_library.application import LibraryApp
    from carr_library import desktop_preferences
    from carr_library.speech import ReadingDocument
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.ui import desktop_app, desktop_integration, desktop_settings, speech_dialog, worker_cleanup

    checks, errors, futures, destroyed = [], [], [], []
    def check(label, value):
        if not value:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    if options.installed:
        for module in (desktop_preferences, desktop_app, desktop_integration, desktop_settings, speech_dialog):
            check('sealed installed origin: ' + module.__name__,
                  'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            self.ExitMainLoop()
            return False
    app = App(False)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.DestroyWindow.restype = wintypes.BOOL

    class OutsideWindow:
        """A real owned Win32 window outside wx's modal-window set."""
        def __init__(self):
            self.handle = user32.CreateWindowExW(0, 'STATIC', 'Global shortcut foreground control',
                0x10CF0000, 40, 40, 480, 180, None, None, None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        def GetHandle(self): return self.handle
        def Show(self, shown=True): user32.ShowWindow(self.handle, 5 if shown else 0)
        def Raise(self): user32.SetForegroundWindow(self.handle)
        def Destroy(self):
            if self.handle:
                if not user32.DestroyWindow(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handle = None

    def pump(predicate, message, timeout=12, *, allow_errors=False):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors and not allow_errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() > deadline:
                raise TimeoutError(message)
            app.Yield()
            time.sleep(.01)

    def settle(seconds=.15):
        end = time.monotonic() + seconds
        pump(lambda: time.monotonic() >= end, 'Native events did not settle.')

    def foreground(window):
        window.Show(True)
        window.Raise()
        user32.SetForegroundWindow(window.GetHandle())
        pump(lambda: user32.GetForegroundWindow() == window.GetHandle(), 'Native foreground window')

    def named(parent, name):
        result = wx.Window.FindWindowByName(name, parent)
        check('named desktop control: ' + name, result is not None)
        return result

    def press(control):
        foreground(wx.GetTopLevelParent(control))
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control, 'Native desktop control focus')
        check('native Space', simulator.Char(wx.WXK_SPACE))
        settle()

    def put_clipboard(text):
        check('open actual Windows clipboard', wx.TheClipboard.Open())
        try:
            check('write synthetic native clipboard text', wx.TheClipboard.SetData(wx.TextDataObject(text)))
        finally:
            wx.TheClipboard.Close()

    original_cleanup = worker_cleanup.close_after_worker
    def owned_cleanup(executor, service):
        result = original_cleanup(executor, service)
        futures.append(result)
        return result
    worker_cleanup.close_after_worker = owned_cleanup
    with TemporaryDirectory(prefix='native-desktop-integration-') as temporary:
        library = LibraryApp(Path(temporary) / 'library')
        workspace = library.create_workspace('Synthetic desktop acceptance')
        imported = library.add_text(workspace, 'Unchanged original', 'Retained original stays separate.')
        asset = library.assets.resolve(imported['asset'])
        original_hash = sha256(asset.read_bytes()).hexdigest()
        manual = ReaderDraftStore(library.root)
        manual.save(ReaderResume(ReadingDocument('Unrelated manual recovery draft.')))
        original_manual = manual.path.read_bytes()
        frame = desktop_app.DesktopFrame(library)
        outside = OutsideWindow()
        controller = frame.desktop_integration
        frame.Bind(wx.EVT_WINDOW_DESTROY,
                   lambda event: (destroyed.append(True) if event.GetEventObject() is frame else None, event.Skip()))

        def settings_driver():
            try:
                dialog, = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsModal()
                           and window.GetTitle() == 'Desktop integration']
                check('native tray available', wx.adv.TaskBarIcon.IsAvailable())
                press(named(dialog, 'Show the optional system tray icon'))
                # First semantic action is Show; configure it without opening any source.
                named(dialog, 'Global shortcut').ChangeValue('Ctrl+Alt+Shift+K')
                press(named(dialog, 'Save choices'))
                check('native saved choices remain inactive', not controller.hotkeys.enabled)
                check('actual system tray icon installed', controller.tray is not None and controller.tray.IsIconInstalled())
                press(named(dialog, 'Enable saved global shortcuts'))
                check('native global key explicitly active', controller.hotkeys.enabled)
                press(named(dialog, 'Close'))
            except Exception:
                errors.append(traceback.format_exc())
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)

        def reader_driver(expected):
            try:
                pump(lambda: controller.reader is not None, 'Canonical reader controls were not attached.')
                _token, dialog, buttons, _alive, _scope = controller.reader
                editor = named(dialog, 'Reading copy (editable; Ctrl+S saves)')
                check('native reader contains the exact admitted copy', editor.GetValue() == expected)
                check('global controls bind actual canonical reader buttons',
                      buttons['voice.pause'].GetParent() is dialog and buttons['voice.stop'].GetParent() is dialog)
                stop_events = []
                def observe_stop(event):
                    stop_events.append(True)
                    event.Skip()  # The original canonical handler still executes.
                buttons['voice.stop'].Bind(wx.EVT_BUTTON, observe_stop)
                foreground(outside)
                check('native global Stop key down',
                      simulator.KeyDown(ord('J'), wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT))
                try:
                    pump(lambda: bool(stop_events), 'Actual global Stop did not reach the current reader.')
                    settle(.8)
                    check('held global key dispatches once without autorepeat', len(stop_events) == 1)
                finally:
                    check('native global Stop key up',
                          simulator.KeyUp(ord('J'), wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT))
                foreground(dialog)
                close = next(child for child in dialog.GetChildren()
                             if isinstance(child, wx.Button) and child.GetId() == wx.ID_CANCEL)
                press(close)
            except Exception:
                errors.append(traceback.format_exc())
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)

        def scenario():
            try:
                check('opening native desktop starts no clipboard observation or OS hotkeys',
                      not controller.watch.enabled and not controller.timer.IsRunning()
                      and not controller.hotkeys.enabled and controller.tray is None)
                wx.CallLater(100, settings_driver)
                controller.invoke('desktop.settings')
                if errors:
                    raise RuntimeError('\n'.join(errors))
                controller.invoke('desktop.hide')
                check('explicit Hide retains an actual tray icon', not frame.IsShown() and controller.tray.IsIconInstalled())
                foreground(outside)
                check('actual global Show key from another window',
                      simulator.Char(ord('K'), wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT))
                pump(frame.IsShown, 'Global Show did not restore the hidden window.')
                controller.hotkeys.disable()
                blocker = desktop_integration.WindowsHotkeyAPI(outside)
                check('reserve real conflict key', blocker.register(0x4301, 'Ctrl+Alt+Shift+K'))
                try:
                    try:
                        controller.hotkeys.enable({'desktop.show': 'Ctrl+Alt+Shift+K'})
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('Actual global key conflict was silently accepted.')
                    check('conflict keeps dispatch off and no partial owned registrations',
                          not controller.hotkeys.enabled and not controller.hotkeys._registered)
                finally:
                    check('release real conflict key', blocker.unregister(0x4301))
                controller.hotkeys.enable({'voice.stop': 'Ctrl+Alt+Shift+J'})
                snapshot = '😀 Український clipboard snapshot.\nNo quotation authority.'
                put_clipboard(snapshot)
                actual_snapshot = speech_dialog._read_clipboard_text(wx)
                controller.watch_clipboard(True)
                pump(lambda: controller.watch.pending == actual_snapshot, 'Native clipboard watcher did not retain the explicit snapshot.')
                check('watching never opened a reader or persisted its pending text',
                      controller.reader is None and not ReaderDraftStore(library.root, slot='clipboard-watch').path.exists())
                wx.CallLater(100, lambda: reader_driver(actual_snapshot))
                controller.invoke('voice.watched_clipboard')
                check('closed native reader detached its global controls', controller.reader is None)
                recovered = ReaderDraftStore(library.root, slot='clipboard-watch').load()
                check('clipboard recovery is an independent exact reading derivative', recovered is not None
                      and recovered.document.source_text == actual_snapshot
                      and recovered.document.source_anchor is None)
                check('manual draft and retained original survived clipboard reading',
                      manual.path.read_bytes() == original_manual and sha256(asset.read_bytes()).hexdigest() == original_hash)
                check('production reading never rewrote native clipboard', speech_dialog._read_clipboard_text(wx) == actual_snapshot)
                controller.watch_clipboard(False)
                check('Stop watching discards in-memory source text', controller.watch.pending is None)
                controller.set_tray(False)
                check('tray disable leaves the window reachable', frame.IsShown() and controller.tray is None)
                frame.Close()
                pump(lambda: bool(destroyed), 'Native DesktopFrame did not acknowledge destruction.')
                check('close owns all timer and native-key cleanup',
                      controller.closed and not controller.hotkeys._registered and controller.watch.pending is None)
                pump(lambda: all(future.done() for future in futures), 'Reader owned cleanup did not finish.', timeout=30)
                for future in futures:
                    future.result()
                receiver = desktop_integration.WindowsHotkeyAPI(outside)
                check('closed owner actually released the OS key', receiver.register(0x4302, 'Ctrl+Alt+Shift+J'))
                check('probe releases the recovered key', receiver.unregister(0x4302))
                settle()
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                app.ExitMainLoop()

        try:
            frame.Show()
            app.SetTopWindow(frame)
            wx.CallAfter(scenario)
            app.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
        finally:
            worker_cleanup.close_after_worker = original_cleanup
            if not destroyed:
                controller.close()
                frame.Close()
                pump(lambda: bool(destroyed), 'Failure-path desktop cleanup did not destroy the frame.', allow_errors=True)
            outside.Destroy()
            pump(lambda: all(future.done() for future in futures), 'Failure-path reader cleanup did not finish.',
                 timeout=30, allow_errors=True)
    print(json.dumps({'status': 'PASS', 'checks': checks, 'nvda': 'NOT_RUN',
                      'speech_audio': 'NOT_RUN', 'native_windows': 'EXECUTED'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
