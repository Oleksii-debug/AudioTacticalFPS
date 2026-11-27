"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

Only the file-picker result is supplied by the probe. Real Windows keyboard
events activate buttons; a real wx event loop handles background completion.
Physical recording devices, native file pickers and NVDA are separate gates.
"""
import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import struct
import threading
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_media_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows session required', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        if native_handle is not None:
            native_handle.close()


def _run_native(arguments):
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

    binding = _acceptance_bindings()
    def verify_audio(runtime):
        checked = binding.verify_audio_evidence(runtime, arguments.audio_evidence)
        if audio_proof and audio_proof != checked:
            raise AssertionError('External audio executable identity changed during native acceptance.')
        audio_proof.update(checked)

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
    source_identity = binding.verify_installed_sources(ui, clips)
    check('exact current installed clip source', bool(source_identity))
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
            proof_runtime = editor.service._runtime()
            verify_audio(proof_runtime)
            project = editor.create('Native scene clips')
            dialog = ui.ClipDialog(frame, editor=editor)
            dialog.Show()
            def lifecycle_checks():
                class NativeParent(wx.Frame):
                    @property
                    def working(self):
                        return frame.working
                for destruction in ('direct', 'native-parent'):
                    parent = NativeParent(frame, title='Owned clip lifetime fixture')
                    parent.library = frame.library
                    parent.cancel_event = frame.cancel_event
                    parent.operation_progress = frame.operation_progress
                    parent.run_job, parent.report = frame.run_job, frame.report
                    parent.Show()
                    selected_editor = clips.ClipEditor(frame.library.root)
                    project = selected_editor.create('Owned preview lifetime')
                    project = selected_editor.add(project, project.scenes[0].id, source,
                                                   trim_start=.5, duration=.25)
                    selected = ui.ClipDialog(parent, editor=selected_editor)
                    selected.Show()
                    selected._accept(project)
                    native_id, events = selected._native_id, []
                    def native_destroy(event, events=events):
                        events.append(event.GetId())
                        event.Skip()
                    selected.Bind(wx.EVT_WINDOW_DESTROY, native_destroy)
                    ready, release, completed_paths = threading.Event(), threading.Event(), []
                    original_preview = selected_editor.preview
                    def held_preview(*args, **kwargs):
                        # Actual installed ClipEditor/FFmpeg creates and verifies
                        # its preview before this test-only completion barrier.
                        path = original_preview(*args, **kwargs)
                        completed_paths.append(path)
                        ready.set()
                        if not release.wait(20):
                            raise TimeoutError('Owned preview completion barrier was not released')
                        return path
                    selected_editor.preview = held_preview
                    try:
                        press(selected.buttons['preview'])
                        pump(ready.is_set, 'Actual preview did not reach native lifetime barrier')
                        check('actual verified preview is pending before ' + destruction + ' destruction',
                              selected.pending and frame.working and completed_paths[0].is_file())
                        if destruction == 'direct':
                            selected.Destroy()
                        else:
                            parent.DestroyChildren()
                        pump(lambda: not bool(selected), 'Native clip wrapper was not deleted', timeout=10)
                        check('real cached-ID clip destruction latches cancellation: ' + destruction,
                              events.count(native_id) == 1 and selected.closed and parent.cancel_event.is_set())
                        selected.message('Late progress after native deletion')
                        release.set()
                        pump(lambda: not frame.working and not selected.pending,
                             'Actual preview worker completion did not release')
                        gui.Yield()
                        selected.message('Late report after actual completion')
                        check('owned late preview is cleaned without native access: ' + destruction,
                              not completed_paths[0].exists() and not selected.previews)
                        check('preview teardown preserves exact input: ' + destruction,
                              sha256(source.read_bytes()).hexdigest() == original
                              and Path(project.scenes[0].cues[0].path).read_bytes() == source.read_bytes())
                    finally:
                        release.set()
                        parent.cancel_event.set()
                        if frame.working:
                            pump(lambda: not frame.working, 'Owned fixture worker cleanup', timeout=25)
                        if selected:
                            selected.Destroy()
                        if parent:
                            parent.DestroyChildren()
                            parent.Destroy()
                        gui.Yield()

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
                    lifecycle_checks()
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
            if dialog is not None and dialog:
                dialog.stop()
                dialog.Destroy()
            if frame is not None:
                frame.Destroy()
            gui.Yield()
    verify_audio(proof_runtime)
    print(json.dumps({'result': 'PASS', 'checks': checks, 'native_keyboard': 'PASS',
        'source_identity': source_identity,
        'fixture': 'synthetic PCM rendered by actual FFmpeg', 'audio_runtime': audio_proof, 'nvda': 'NOT_RUN',
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN',
        'direct_parent_destruction': 'PASS', 'late_preview_completion': 'PASS'}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
