"""Installed keyboard batch review, real FFmpeg outputs and receipt-only resume.

Only the explicit input/folder picker results are controlled. Review/settings
dialogs, default-No, native key events, worker, codec and publication are real.
No audio device, microphone, model, acoustic-quality or NVDA claim is made.
"""
from array import array
import argparse
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows required'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--audio-lock', type=Path,
        default=Path(__file__).resolve().parents[1] / 'packaging/windows-audio.lock.json')
    args = parser.parse_args()
    selected_audio = args.audio_directory.resolve(strict=True)
    pin = json.loads(args.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import conversion, conversion_batch, conversion_batch_cli, dsp, process
    from carr_library import durable_state, runtime_settings
    from carr_library.operations import broker
    from carr_library.research import export_file
    from carr_library.ui import audio_conversion, audio_conversion_batch, studio_dialog
    from carr_library.ui.desktop_app import DesktopFrame

    checks, failures, evidence = [], [], {}
    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = (wintypes.HWND,), wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = (), wintypes.HWND

    def pump(predicate, label, timeout=45):
        deadline = monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield(); sleep(.01)

    def named(parent, name):
        item = wx.Window.FindWindowByName(name, parent)
        if item is None:
            raise AssertionError('Missing native control: ' + name)
        return item

    def press(item):
        if item is None:
            raise AssertionError('Missing native keyboard target.')
        top = wx.GetTopLevelParent(item)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); item.SetFocus()
        pump(lambda: wx.Window.FindFocus() is item and user32.GetForegroundWindow() == top.GetHandle(),
             'Native batch focus', 10)
        if not simulator.KeyDown(wx.WXK_SPACE):
            raise AssertionError('Native Space key down failed.')
        try:
            gui.Yield(); sleep(.03)
        finally:
            if not simulator.KeyUp(wx.WXK_SPACE):
                raise AssertionError('Native Space key up failed.')
        gui.Yield()

    def later(title, action):
        deadline = monotonic() + 45
        def attempt():
            try:
                matches = [window for window in wx.GetTopLevelWindows()
                           if window.IsShown() and window.GetTitle() == title]
                if not matches:
                    if monotonic() >= deadline:
                        raise TimeoutError('Missing native dialog: ' + title)
                    wx.CallLater(20, attempt)
                    return
                popup, = matches
                action(popup)
            except Exception:
                failures.append(traceback.format_exc())
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                gui.ExitMainLoop()
        wx.CallLater(20, attempt)

    checkout = Path(__file__).resolve().parents[1]
    for module in (conversion, conversion_batch, conversion_batch_cli, audio_conversion,
                   audio_conversion_batch, studio_dialog, dsp, process, broker,
                   durable_state, runtime_settings, export_file):
        installed = Path(module.__file__).resolve()
        source = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact installed source: ' + module.__name__,
            installed.is_relative_to(Path(sys.prefix).resolve()) and not installed.is_relative_to(checkout)
            and sha256(installed.read_bytes()).digest() == sha256(source.read_bytes()).digest())
    check('actual Windows native batch controls', wx.Platform == '__WXMSW__')
    old_file, old_dir, frame = wx.FileDialog, wx.DirDialog, None
    try:
        with TemporaryDirectory(prefix='resonance-batch-native-') as temporary:
            root = Path(temporary)
            output = root / 'outputs'; output.mkdir()
            sources = []
            for index in range(2):
                path = root / f'Оригінал Ω {index}.wav'
                values = array('h', (round(4000 * math.sin(2*math.pi*(440+index*220)*n/24000))
                                    for n in range(6000)))
                if sys.byteorder != 'little': values.byteswap()
                with wave.open(str(path), 'wb') as stream:
                    stream.setparams((1, 2, 24000, 0, 'NONE', 'PCM')); stream.writeframes(values.tobytes())
                sources.append(path)
            originals = [sha256(path.read_bytes()).hexdigest() for path in sources]
            app = LibraryApp(root / 'library')
            settings = runtime_settings.load_settings(app.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin/ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin/ffprobe.exe'))
            runtime_settings.save_settings(app.root / 'state', settings)
            runtime = runtime_settings.audio_runtime(root / 'probe', state_root=app.root / 'state')
            for name in ('ffmpeg', 'ffprobe'):
                path = Path(getattr(runtime, name)).resolve(strict=True)
                with path.open('rb') as stream: digest = file_digest(stream, 'sha256').hexdigest()
                wanted = pin['output_files']['bin/' + name + '.exe']
                check('explicit pinned batch runtime: ' + name,
                    path.samefile(selected_audio / 'bin' / (name + '.exe'))
                    and path.stat().st_size == wanted['size'] and digest == wanted['sha256'])
                evidence[name] = {'sha256': digest, 'size': path.stat().st_size}
            frame = DesktopFrame(app); frame.Show(); gui.SetTopWindow(frame)
            command = frame.keyboard_commands.registry.get('audio.convert_batch')
            check('batch command uses the current registry API',
                  command.command_id == 'audio.convert_batch' and command.context == 'main')
            class FilePicker:
                def __init__(self, *args, **kwargs):
                    check('batch picker requests explicit multiple files', bool(kwargs['style'] & wx.FD_MULTIPLE))
                def ShowModal(self): return wx.ID_OK
                def GetPaths(self): return list(map(str, sources))
                def Destroy(self): pass
            class FolderPicker:
                def __init__(self, *args, **kwargs): pass
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(output)
                def Destroy(self): pass
            wx.FileDialog, wx.DirDialog = FilePicker, FolderPicker

            def settings_dialog(dialog):
                named(dialog, 'Output channels, 1 or 2').ChangeValue('1')
                named(dialog, 'Target loudness in LUFS; empty leaves levels unchanged').ChangeValue('')
                press(wx.Window.FindWindowById(wx.ID_OK, dialog))

            def batch_dialog(dialog, resume):
                control = named(dialog, 'Explicitly resume saved audio batch')
                if resume:
                    press(control)
                check('batch resume is an explicit native choice', bool(control.GetValue()) == resume)
                press(wx.Window.FindWindowById(wx.ID_OK, dialog))

            def approval(dialog, approved):
                detail = named(dialog, 'Reviewed audio batch files and settings')
                check('native batch review is read-only and names both sources',
                    isinstance(detail, wx.TextCtrl) and bool(detail.GetWindowStyleFlag() & wx.TE_READONLY)
                    and all(str(path) in detail.GetValue() for path in sources))
                no = named(dialog, 'Keep audio review without running')
                check('native batch approval defaults to No', dialog.GetDefaultItem() is no)
                press(named(dialog, 'Run exactly reviewed audio batch') if approved else no)

            def invoke(resume=False, approved=True):
                later('Batch audio conversion settings', settings_dialog)
                later('Review audio batch destination', lambda dialog: batch_dialog(dialog, resume))
                later('Run reviewed audio batch?', lambda dialog: approval(dialog, approved))
                check('native registered batch action dispatches', frame.keyboard_commands.invoke('audio.convert_batch'))

            def scenario():
                try:
                    invoke(approved=False)
                    pump(lambda: not frame.working and 'not started' in frame.status.GetValue(), 'Default-No batch review')
                    check('No leaves every output and manifest absent',
                          not list(output.glob('*.wav')) and not (output / conversion_batch.MANIFEST).exists())
                    invoke()
                    pump(lambda: not frame.working and '2/2 committed outputs' in frame.status.GetValue(), 'Real batch conversion')
                    manifest = durable_state.read_json(output / conversion_batch.MANIFEST)
                    check('native batch records only actual returned successes',
                          [row['status'] for row in manifest['items']] == ['completed', 'completed']
                          and all(row['receipt'] for row in manifest['items']))
                    paths = [output / row['output'] for row in manifest['plan']['sources']]
                    previous = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
                    for path, row in zip(paths, manifest['items']):
                        with wave.open(str(path), 'rb') as stream:
                            check('actual batch PCM format and duration',
                                  (stream.getframerate(), stream.getnchannels(), stream.getnframes()) == (24000, 1, 6000))
                        check('actual batch output matches its completion receipt',
                              sha256(path.read_bytes()).hexdigest() == row['receipt']['audio']['sha256'])
                    invoke(resume=True)
                    pump(lambda: not frame.working and '2/2 committed outputs' in frame.status.GetValue(), 'Explicit native resume')
                    check('native resume preserves all completed bytes and timestamps',
                          [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == previous)
                    check('native batch preserves both original sources',
                          [sha256(path.read_bytes()).hexdigest() for path in sources] == originals)
                    check('native batch releases shared resource admission',
                          broker.ResourceBroker.for_app(app.root).snapshot()['leases'] == [])
                    check('native batch leaves no owned conversion staging',
                          not list(output.glob('.audio-convert-*')))
                except Exception:
                    failures.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()
            wx.CallAfter(scenario); gui.MainLoop()
            if failures: raise RuntimeError('\n'.join(failures))
            frame.cancel_event.set(); frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            check('native batch host finalizers complete', not frame.executor.cleanup_failures)
            frame.progress_panel.close(); frame.Destroy(); frame = None; gui.Yield()
    finally:
        wx.FileDialog, wx.DirDialog = old_file, old_dir
        if frame is not None:
            frame.cancel_event.set(); frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.progress_panel.close(); frame.Destroy(); gui.Yield()
        if native_handle is not None: native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': evidence,
                      'actual_ffmpeg': 'PASS', 'native_keyboard': 'PASS',
                      'file_and_folder_pickers': 'CONTROLLED', 'physical_audio': 'NOT_RUN',
                      'acoustic_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    def exceeded():
        print(json.dumps({'result': 'FAIL', 'reason': 'Native batch deadline exceeded',
                          'cleanup': 'NOT_CONFIRMED', 'timeout_seconds': 180}), flush=True)
        os._exit(1)
    watchdog = threading.Timer(180, exceeded)
    watchdog.daemon = True; watchdog.start()
    result = main()
    watchdog.cancel()
    raise SystemExit(result)
