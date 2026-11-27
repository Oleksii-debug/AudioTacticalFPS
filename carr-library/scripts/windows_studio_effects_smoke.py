"""Installed native persisted Studio effect scopes and measured synthetic PCM.

Only local source/output chooser results and a competing saved revision are
supplied by the fixture. Effect dialogs, keyboard actions, shared background
work, ProjectStore persistence and FFmpeg rendering are real. No playback,
physical-device, speech-quality or NVDA acceptance is implied.
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
from time import monotonic, sleep
import threading
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows required'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
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
    from carr_library.audio import Effect
    from carr_library.audio import dsp, effect_presets, process
    from carr_library import durable_state, runtime_settings
    from carr_library.operations import broker
    from carr_library.speech import VoiceRegistry
    from carr_library.studio import project as project_module, clips, managed_inputs, render
    from carr_library.studio.project import Role
    from dataclasses import replace
    from carr_library.runtime_settings import audio_runtime
    from carr_library.ui import effects_dialog, studio_clips
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, proof, module_origins = [], [], {}, {}
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def pump(predicate, label, timeout=45):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() >= deadline:
                raise TimeoutError(label)
            gui.Yield()
            sleep(.01)

    def find(parent, name):
        control = wx.Window.FindWindowByName(name, parent)
        check('named native control: ' + name, control is not None)
        return control

    def press(control):
        name = control.GetName()
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Keyboard focus: ' + name, 10)
        check('native Space down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.03)
        finally:
            check('native Space up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def later(title, callback):
        deadline = monotonic() + 45
        def invoke():
            try:
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if window.IsShown() and window.GetTitle() == title]
                if not dialogs:
                    if monotonic() >= deadline:
                        raise TimeoutError('Missing native dialog: ' + title)
                    wx.CallLater(20, invoke)
                    return
                popup, = dialogs
                callback(popup)
            except Exception:
                errors.append(traceback.format_exc())
                # Failure cleanup does not stand in for any successful action.
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                gui.ExitMainLoop()
        wx.CallLater(20, invoke)

    def set_gain(popup, value):
        control = find(popup, 'gain db (dB); -120 to 24')
        check('native numeric effect input', isinstance(control, wx.TextCtrl))
        control.ChangeValue(str(value))
        press(find(popup, 'Apply effect'))

    def choose(popup, selection):
        check('native preset choice dialog', isinstance(popup, wx.SingleChoiceDialog))
        popup.SetSelection(selection)
        press(wx.Window.FindWindowById(wx.ID_OK, popup))

    def verify_audio(runtime):
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected audio executable: ' + name,
                  binary.samefile(selected_audio / 'bin' / (name + '.exe')))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            proof[name] = {'path': str(binary), 'sha256': digest}

    check('actual wx Windows controls', wx.Platform == '__WXMSW__')
    checkout = Path(__file__).resolve().parents[1]
    for module in (project_module, clips, managed_inputs, render, studio_clips, effects_dialog,
                   effect_presets, dsp, process, broker, durable_state, runtime_settings):
        actual = Path(module.__file__).resolve()
        module_origins[module.__name__] = str(actual)
        original = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact installed source: ' + module.__name__,
              actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(original.read_bytes()).digest())

    original_picker, original_directory_picker = wx.FileDialog, wx.DirDialog
    frame = dialog = service = voices = None

    def pcm(path):
        with wave.open(str(path), 'rb') as stream:
            shape = stream.getframerate(), stream.getnchannels(), stream.getnframes()
            raw = stream.readframes(stream.getnframes())
        values = array('h')
        values.frombytes(raw)
        if sys.byteorder != 'little':
            values.byteswap()
        return raw, shape, math.sqrt(math.fsum(value * value for value in values) / len(values))

    try:
        with TemporaryDirectory(prefix='resonance-native-studio-effects-') as temporary:
            root = Path(temporary)
            source = root / 'Synthetic original — Україна.wav'
            data = array('h', (round(7000 * math.sin(2 * math.pi * 440 * index / 24000))
                               for index in range(24000)))
            if sys.byteorder != 'little':
                data.byteswap()
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(data.tobytes())
            original = sha256(source.read_bytes()).hexdigest()
            library = LibraryApp(root / 'private library')
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
            frame = DesktopFrame(library)
            frame.Show()
            gui.SetTopWindow(frame)
            voices = VoiceRegistry([])
            service = render.ProductionService(library.root / 'state/studio', voices=voices)
            editor = clips.ClipEditor(library.root, service=service)
            verify_audio(service._runtime())
            project = editor.create('Native persisted effects')
            # A deliberately unused speaking role exercises its real editor
            # without misrepresenting a synthetic tone as speech synthesis.
            project = service.projects.save(replace(project, revision=2,
                roles=(Role('unused-role', 'Reviewed speaking role'),)),
                expected_revision=project.revision, expected_fingerprint=project.fingerprint)
            outputs = root / 'reviewed output parent'
            outputs.mkdir()

            class SelectedPath:
                def __init__(self, *args, **kwargs):
                    check('explicit original picker requires existing file',
                          kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(source)
                def Destroy(self): pass
            class SelectedOutput:
                def __init__(self, *args, **kwargs): pass
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(outputs)
                def Destroy(self): pass
            wx.FileDialog, wx.DirDialog = SelectedPath, SelectedOutput
            dialog = studio_clips.ClipDialog(frame, editor=editor)
            dialog.Show()

            def add_gain(popup, gain, *, bypass=False):
                kinds = find(popup, 'Effect type to add')
                check('Studio exposes only duration-preserving effect choices',
                      kinds.FindString('tempo') == wx.NOT_FOUND)
                index = kinds.FindString('gain')
                check('installed runtime offers gain', index != wx.NOT_FOUND)
                kinds.SetSelection(index)
                press(find(popup, 'Add effect'))
                later('Edit gain effect', lambda edit: set_gain(edit, gain))
                press(find(popup, 'Edit selected effect'))
                if bypass:
                    press(find(popup, 'Toggle effect bypass'))

            def review_scope(scope):
                def chain(popup):
                    ordered = find(popup, 'Ordered audio effects')
                    check('new scope starts without processing', ordered.GetCount() == 0)
                    add_gain(popup, -9 if scope == 'role' else -6)
                    if scope == 'clip':
                        add_gain(popup, -3, bypass=True)
                        press(find(popup, 'Move effect up'))
                        check('clip order changes in native editor', 'bypassed' in ordered.GetString(0))
                        press(find(popup, 'Move effect down'))
                        check('clip bypass remains after reorder', 'bypassed' in ordered.GetString(1))
                    press(find(popup, 'Apply effect chain'))
                later('Ordered audio effects', chain)
                if scope == 'role':
                    later('Speaking role effects', lambda picker: choose(picker, 0))
                press(dialog.buttons[scope + '_effects'])
                pump(lambda: not frame.working and not dialog.pending, 'Saving ' + scope + ' processing')
                value = service.projects.load(project.id)
                target = {'master': value.effects, 'role': value.roles[0].effects,
                          'scene': value.scenes[0].effects,
                          'clip': value.scenes[0].cues[0].effects}[scope]
                check('native saved exact ' + scope + ' processing',
                      value.schema == 3 and target[0] == Effect('gain', gain_db=-9 if scope == 'role' else -6))
                if scope == 'clip':
                    check('native persisted ordered bypass chain',
                          len(target) == 2 and target[1] == Effect('gain', gain_db=-3, enabled=False))
                return value

            def scenario():
                try:
                    check('actual Studio effect main loop', gui.GetMainLoop() is not None)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Load current project')
                    check('native project selection matches saved identity', dialog.project.id == project.id)
                    press(dialog.buttons['add'])
                    pump(lambda: not frame.working and not dialog.pending, 'Retain exact original')
                    check('native original is managed by content identity',
                          dialog.project.scenes[0].cues[0].path == 'inputs/' + original + '.audio')
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Baseline render')
                    baseline_rows = [row for row in service.jobs() if row['status'] == 'complete']
                    check('baseline render is an actual completed job', len(baseline_rows) == 1)
                    baseline = Path(baseline_rows[0]['destination']) / 'master.wav'
                    for scope in ('clip', 'role', 'scene', 'master'):
                        edited = review_scope(scope)
                    before_cancel = edited.fingerprint
                    later('Ordered audio effects',
                          lambda popup: press(wx.Window.FindWindowById(wx.ID_CANCEL, popup)))
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Cancel effect review')
                    check('native Cancel does not create a saved revision',
                          service.projects.load(project.id).fingerprint == before_cancel)
                    press(dialog.buttons['reload'])
                    pump(lambda: not frame.working and not dialog.pending, 'Reload saved processing')
                    check('native reload preserves full typed chains', dialog.project == edited)
                    press(dialog.buttons['render'])
                    pump(lambda: not frame.working and not dialog.pending, 'Processed render')
                    processed, = [row for row in service.jobs()
                                  if row['fingerprint'] == edited.fingerprint and row['status'] == 'complete']
                    check('native render completion has readable output and focus',
                          processed['destination'] in dialog.status.GetValue()
                          and wx.Window.FindFocus() is dialog.status)
                    rendered = Path(processed['destination'])
                    master = rendered / 'master.wav'
                    chapter, = (rendered / 'chapters').glob('*.wav')
                    stem = rendered / 'stems/clips/sfx.wav'
                    check('actual clip scene master stages attenuate PCM',
                          .122 < pcm(master)[2] / pcm(baseline)[2] < .130)
                    check('track stem includes clip effects but excludes bus effects',
                          .497 < pcm(stem)[2] / pcm(baseline)[2] < .505)
                    check('mastered chapter equals final master PCM', pcm(chapter)[0] == pcm(master)[0])
                    check('processing retains measured sample grid', pcm(master)[1] == pcm(baseline)[1])
                    check('saved timeline binds ordered project effects',
                          processed['result']['effects'] == project_module.project_effects(edited))
                    newer = None
                    def concurrent_review(popup):
                        nonlocal newer
                        newer = editor.scene_duration(edited, edited.scenes[0].id, 2)
                        press(find(popup, 'Apply effect chain'))
                    later('Ordered audio effects', concurrent_review)
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Refuse stale reviewed effect save')
                    check('concurrent saved revision cannot be overwritten by native review',
                          newer is not None and service.projects.load(project.id) == newer
                          and 'changed since' in dialog.status.GetValue())
                    check('Studio effects do not start playback', dialog.player is None and not dialog.previews)
                    check('original and retained media remain unchanged',
                          sha256(source.read_bytes()).hexdigest() == original
                          and sha256((service.inputs / (original + '.audio')).read_bytes()).hexdigest() == original)
                    check('processing releases current shared admission', service.broker.snapshot()['leases'] == [])
                    press(dialog.buttons['close'])
                    check('native effect editor closes cleanly', dialog.closed)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    gui.ExitMainLoop()

            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            check('Studio effect worker cleanup is complete', not frame.executor.cleanup_failures)
            dialog.Destroy()
            dialog = None
            service.close()
            service = None
            voices.close()
            voices = None
            frame.progress_panel.close()
            frame.Destroy()
            frame = None
            gui.Yield()
    finally:
        wx.FileDialog, wx.DirDialog = original_picker, original_directory_picker
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
        if dialog is not None:
            dialog.stop()
            dialog.Destroy()
        if service is not None:
            service.close()
        if voices is not None:
            voices.close()
        if frame is not None:
            frame.progress_panel.close()
            frame.Destroy()
            gui.Yield()
        if native_handle is not None:
            native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'module_origins': module_origins,
        'audio_runtime': proof, 'native_keyboard': 'PASS', 'actual_ffmpeg': 'PASS',
        'fixture': 'actual synthetic PCM and a dormant role; no speech inference',
        'project_schema': 3, 'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'audible_listening': 'NOT_RUN', 'physical_device': 'NOT_RUN'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    def watchdog_failure():
        print(json.dumps({'result': 'FAIL', 'reason': 'Native Studio effects deadline exceeded',
                          'cleanup': 'NOT_CONFIRMED', 'timeout_seconds': 180}), flush=True)
        os._exit(1)
    watchdog = threading.Timer(180, watchdog_failure)
    watchdog.daemon = True
    watchdog.start()
    result = main()
    watchdog.cancel()
    raise SystemExit(result)
