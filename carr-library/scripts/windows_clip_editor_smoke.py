"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

The file-picker result is supplied; numeric and choice dialogs stay native.
Real Windows keyboard
events activate buttons; a real wx event loop handles background completion.
Physical recording devices, native file pickers and NVDA are separate gates.
"""
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.studio import clips
    from carr_library.ui import studio_clips as ui
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, audio_proof = [], [], {}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def verify_audio(runtime):
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            if bundle:
                entry = 'audio/bin/' + name + '.exe'
                check('exact bundled executable: ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed executable bytes: ' + name, digest == manifest['files'][entry])

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


    def answer_native_dialog(title, value):
        """Enter test data into the real modal dialog, then use its native OK."""
        deadline = monotonic() + 8
        def answer():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == title]
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('The expected native clip dialog did not open: ' + title)
                    wx.CallLater(20, answer)
                    return
                popup, = matching
                if isinstance(popup, wx.TextEntryDialog):
                    popup.SetValue(str(value))
                elif isinstance(popup, wx.SingleChoiceDialog):
                    popup.SetSelection(value)
                else:
                    raise AssertionError('Unexpected native clip dialog type')
                button = wx.Window.FindWindowById(wx.ID_OK, popup)
                check('actual native clip review button: ' + title, isinstance(button, wx.Button))
                press(button)
            except Exception:
                errors.append(traceback.format_exc())
                gui.ExitMainLoop()
        wx.CallLater(20, answer)

    check('real wx Windows controls', wx.Platform == '__WXMSW__')
    for module in (ui, clips):
        check('installed module: ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    actual_picker = wx.FileDialog
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
        class SelectedPath:
            def __init__(self, *args, **kwargs):
                check('original selection requires an existing file',
                      kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(source)
            def Destroy(self): pass
        wx.FileDialog = SelectedPath
        try:
            frame = DesktopFrame(LibraryApp(root / 'private library'))
            frame.Show()
            gui.SetTopWindow(frame)
            editor = clips.ClipEditor(frame.library.root)
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
                    retained = Path(dialog.project.scenes[0].cues[0].path)
                    for name, value in {'trim_start': '.5', 'duration': '.75', 'start': '.25',
                                        'pan': '-1', 'fade_in': '.1', 'fade_out': '.1'}.items():
                        dialog.fields[name].ChangeValue(value)
                    press(dialog.buttons['save'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving clip settings')
                    edited = editor.service.projects.load(project.id)
                    cue = edited.scenes[0].cues[0]
                    check('native Save persists reviewed numeric controls',
                          (edited.revision, cue.trim_start, cue.duration, cue.start, cue.pan) == (3, .5, .75, .25, -1))
                    outputs = root / 'rendered'; outputs.mkdir()
                    results = []
                    identity = editor.service.submit(edited, outputs)
                    frame.run_job('Native edited clip render', lambda: editor.service.run(identity), results.append,
                                  lambda error: errors.append(str(error)))
                    pump(lambda: not frame.working, 'Rendering native edited clip')
                    check('real edited project rendered', results and results[0]['status'] == 'complete')
                    with wave.open(str(Path(results[0]['destination']) / 'master.wav'), 'rb') as stream:
                        count, channels = stream.getnframes(), stream.getnchannels()
                        samples = struct.unpack('<' + 'h' * count * channels, stream.readframes(count))
                        check('actual left-only timeline and one-second duration', count == 24000
                              and channels == 2 and max(map(abs, samples[1::2])) == 0
                              and max(map(abs, samples[::2])) > 1000)

                    press(dialog.buttons['copy'])
                    pump(lambda: not frame.working and not dialog.pending, 'Copying verified clip')
                    check('native copy preserves the saved project', dialog.project == edited)
                    answer_native_dialog('Split at an output sample', '.375')
                    press(dialog.buttons['split'])
                    pump(lambda: not frame.working and not dialog.pending, 'Splitting native clip')
                    check('native split preserves retained source and measured cuts',
                          len(dialog.project.scenes[0].cues) == 2
                          and {item.path for item in dialog.project.scenes[0].cues} == {cue.path}
                          and [item.duration for item in dialog.project.scenes[0].cues] == [.375, .375])
                    answer_native_dialog('Join adjacent clips', 0)
                    press(dialog.buttons['join'])
                    pump(lambda: not frame.working and not dialog.pending, 'Joining native cuts')
                    check('native join restores the original instructions', dialog.project.scenes[0].cues == (cue,))
                    press(dialog.buttons['undo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Undoing native join')
                    check('native undo restores both cuts', len(dialog.project.scenes[0].cues) == 2)
                    press(dialog.buttons['redo'])
                    pump(lambda: not frame.working and not dialog.pending, 'Redoing native join')
                    check('native redo restores the joined clip', dialog.project.scenes[0].cues == (cue,))
                    answer_native_dialog('Paste copied clip', '1.25')
                    press(dialog.buttons['paste'])
                    pump(lambda: not frame.working and not dialog.pending, 'Pasting native clip')
                    check('native paste creates a distinct clip at the reviewed position',
                          len(dialog.project.scenes[0].cues) == 2
                          and dialog.project.scenes[0].cues[1].start == 1.25
                          and dialog.project.scenes[0].cues[1].id != cue.id)
                    press(dialog.buttons['cut'])
                    pump(lambda: not frame.working and not dialog.pending, 'Cutting pasted clip')
                    check('native cut keeps original bytes and the remaining clip',
                          dialog.project.scenes[0].cues == (cue,)
                          and sha256(retained.read_bytes()).hexdigest() == original)
                    edited = dialog.project
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
        'fixture': 'synthetic PCM rendered by actual FFmpeg', 'audio_runtime': audio_proof, 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
