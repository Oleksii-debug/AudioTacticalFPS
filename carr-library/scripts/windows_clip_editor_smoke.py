"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

Only file and directory selection results are supplied by the probe. Real
Windows keyboard events activate buttons; a real wx event loop handles background completion.
Physical recording devices, native file pickers and NVDA are separate gates.
"""
import argparse
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-directory', required=True, type=Path,
                        help='Explicit pinned external FFmpeg test directory')
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioRuntime
    from carr_library.studio import clips
    from carr_library.studio.managed_inputs import resolve_managed_input
    from carr_library.studio.render import ProductionService
    from carr_library.ui import studio_clips as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, audio_proof, module_origins = [], [], {}, {}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    for name in ('carr_library.studio.clips', 'carr_library.studio.managed_inputs', 'carr_library.studio.render',
                 'carr_library.studio.project', 'carr_library.audio.process',
                 'carr_library.audio.windows_job', 'carr_library.ui.studio_clips'):
        module = __import__(name, fromlist=['__file__'])
        location = Path(module.__file__).resolve(strict=True)
        module_origins[name] = str(location)
        check('installed module origin: ' + name,
              location.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in location.parts)
        expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
        check('exact installed source bytes: ' + name,
              not location.is_relative_to(checkout)
              and sha256(location.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    def verify_audio(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            selected = selected_audio / 'bin' / (name + '.exe')
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected external executable: ' + name, binary.samefile(selected))
            check('pinned external executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    def pump(predicate, description, timeout=45):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError(description)
            gui.Yield()
            sleep(.01)
    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'Clip control did not receive foreground keyboard focus', timeout=10)
        check('keyboard focus: ' + control.GetName(), wx.Window.FindFocus() is control)
        check('Space key down: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.04)
        finally:
            check('Space key up: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    check('real wx Windows controls', wx.Platform == '__WXMSW__')
    for module in (ui, clips):
        check('installed module: ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    actual_picker = wx.FileDialog
    actual_directory_picker = wx.DirDialog
    frame = dialog = None
    with TemporaryDirectory(prefix='carr-native-clips-') as temporary:
        root = Path(temporary)
        source = root / 'Synthetic original — Україна.wav'
        values = [round(10000 * math.sin(2 * math.pi * 400 * index / 24000))
                  if index >= 12000 else 0 for index in range(48000)]
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(struct.pack('<' + 'h' * len(values), *values))
        original = sha256(source.read_bytes()).hexdigest()
        outputs = root / 'rendered'
        outputs.mkdir()
        class SelectedOutput:
            def __init__(self, *args, **kwargs):
                pass
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(outputs)
            def Destroy(self): pass
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                check('original selection requires an existing file',
                      kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(source)
            def Destroy(self): pass
        wx.FileDialog = SelectedPath
        wx.DirDialog = SelectedOutput
        try:
            frame = DesktopFrame(LibraryApp(root / 'private library'))
            frame.Show()
            gui.SetTopWindow(frame)
            def external_runtime(directory, *, input_roots=()):
                return AudioRuntime(directory, input_roots=input_roots,
                    ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                    ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            service = ProductionService(frame.library.root / 'state' / 'studio',
                                        runtime_factory=external_runtime)
            editor = clips.ClipEditor(frame.library.root, service=service)
            verify_audio(editor.service._runtime())
            project = editor.create('Native scene clips')
            dialog = ui.ClipDialog(frame, editor=editor)
            dialog.Show()
            def scenario():
                try:
                    check('real native main loop is active', gui.GetMainLoop() is not None)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Loading project')
                    check('native project choice binds saved identity', dialog.project.id == project.id)
                    check('all timing controls have readable names', all(
                        isinstance(control, wx.TextCtrl) and control.GetName() for control in dialog.fields.values()))
                    press(dialog.buttons['add'])
                    pump(lambda: not frame.working and not dialog.pending, 'Retaining original')
                    check('native Add retains an actual clip', len(dialog.project.scenes[0].cues) == 1)
                    declaration = dialog.project.scenes[0].cues[0].path
                    check('native Add retains a portable content identity',
                          dialog.project.schema == 2 and declaration == 'inputs/' + original + '.audio')
                    retained = resolve_managed_input(editor.service.root, declaration)
                    for name, value in {'trim_start': '.5', 'duration': '.75', 'start': '.25',
                                        'pan': '-1', 'fade_in': '.1', 'fade_out': '.1'}.items():
                        dialog.fields[name].ChangeValue(value)
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving clip settings')
                    edited = editor.service.projects.load(project.id)
                    cue = edited.scenes[0].cues[0]
                    check('native Save persists reviewed numeric controls',
                          (edited.revision, cue.trim_start, cue.duration, cue.start, cue.pan) == (3, .5, .75, .25, -1))
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Rendering from the native clip editor')
                    results = editor.service.jobs()
                    check('native Render creates exactly one completed saved project job',
                          len(results) == 1 and results[0]['status'] == 'complete'
                          and results[0]['fingerprint'] == edited.fingerprint)
                    check('native render completion exposes its destination in readable status',
                          results[0]['destination'] in dialog.status.GetValue()
                          and wx.Window.FindFocus() is dialog.status)
                    with wave.open(str(Path(results[0]['destination']) / 'master.wav'), 'rb') as stream:
                        count, channels = stream.getnframes(), stream.getnchannels()
                        samples = struct.unpack('<' + 'h' * count * channels, stream.readframes(count))
                        check('actual left-only timeline and one-second duration', count == 24000
                              and channels == 2 and max(map(abs, samples[1::2])) == 0
                              and max(map(abs, samples[::2])) > 1000)
                    newer = editor.scene_duration(edited, edited.scenes[0].id, 2)
                    dialog.fields['pan'].ChangeValue('1')
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Rejecting stale clip edit')
                    check('stale native edit cannot replace a newer revision',
                          'changed since' in dialog.status.GetValue() and editor.service.projects.load(project.id) == newer)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reloading current clip')
                    press(dialog.buttons['remove'])
                    pump(lambda: not frame.working and not dialog.pending, 'Removing clip')
                    check('remove changes project but retains original bytes', not dialog.project.scenes[0].cues
                          and sha256(retained.read_bytes()).hexdigest() == original
                          and sha256(source.read_bytes()).hexdigest() == original)
                    press(dialog.buttons['close'])
                    check('native keyboard close finishes cleanly', dialog.closed)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()
            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
        finally:
            wx.FileDialog = actual_picker
            wx.DirDialog = actual_directory_picker
            if frame is not None:
                frame.cancel_event.set()
                frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
            if dialog is not None:
                dialog.stop()
                dialog.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
            if native_handle is not None:
                native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'native_keyboard': 'PASS',
        'fixture': 'synthetic PCM rendered by actual FFmpeg', 'audio_runtime': audio_proof,
        'module_origins': module_origins, 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
