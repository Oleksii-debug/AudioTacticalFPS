"""Installed native ordered-effects controls and an actual FFmpeg conversion.

Only source/output file-picker results are supplied by the fixture. Parameter
and preset dialogs, keyboard events, background work and rendering are real.
This probe makes no physical-device, listening-quality or NVDA claim.
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
    from carr_library.audio import conversion, dsp, effect_presets, process
    from carr_library import durable_state, runtime_settings
    from carr_library.operations import broker
    from carr_library.research import export_file
    from carr_library.runtime_settings import audio_runtime
    from carr_library.ui import audio_conversion, effects_dialog, studio_dialog
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, proof = [], [], {}
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
    for module in (audio_conversion, effects_dialog, studio_dialog, conversion, effect_presets,
                   dsp, process, broker, export_file, durable_state, runtime_settings):
        actual = Path(module.__file__).resolve()
        original = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact installed source: ' + module.__name__,
              actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(original.read_bytes()).digest())

    original_picker = wx.FileDialog
    frame = None
    try:
        with TemporaryDirectory(prefix='resonance-native-effects-') as temporary:
            root = Path(temporary)
            source = root / 'Synthetic original — Україна.wav'
            output = root / 'Reviewed output.wav'
            data = array('h', (round(7000 * math.sin(2 * math.pi * 440 * index / 24000))
                               for index in range(48000)))
            if sys.byteorder != 'little':
                data.byteswap()
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(data.tobytes())
            original = sha256(source.read_bytes()).hexdigest()
            library = LibraryApp(root / 'library')
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
            frame = DesktopFrame(library)
            frame.Show()
            gui.SetTopWindow(frame)
            commands = {command.command_id for command in frame.keyboard_commands.registry.commands('main')}
            check('existing Convert audio command is registered', 'audio.convert' in commands)
            runtime = audio_runtime(root, state_root=frame.library.root / 'state')
            verify_audio(runtime)
            capabilities = runtime.capabilities()['effects']

            def drive_chain(popup):
                ordered = find(popup, 'Ordered audio effects')
                ordered.SetSelection(0)
                later('Edit gain effect', lambda edit: set_gain(edit, -9))
                press(find(popup, 'Edit selected effect'))
                check('native edit applies numeric value', 'gain_db=-9' in ordered.GetString(0))
                press(find(popup, 'Toggle effect bypass'))
                press(find(popup, 'Move effect down'))
                check('native reorder and bypass', 'compressor' in ordered.GetString(0)
                      and 'bypassed' in ordered.GetString(1))
                press(find(popup, 'Undo chain change'))
                check('native undo restores position', 'gain' in ordered.GetString(0))
                press(find(popup, 'Redo chain change'))
                check('native redo restores position', 'gain' in ordered.GetString(1))
                def name_preset(picker):
                    check('native preset name dialog', isinstance(picker, wx.TextEntryDialog))
                    picker.SetValue('Тест — тихіше')
                    press(wx.Window.FindWindowById(wx.ID_OK, picker))
                later('Save effect preset', name_preset)
                press(find(popup, 'Save chain as a new preset'))
                check('saved preset is visible in native status',
                      'Тест — тихіше' in find(popup, 'Effect chain status').GetValue())
                later('Load effect preset', lambda picker: choose(picker, 0))
                press(find(popup, 'Load effect preset'))
                check('dry built-in empties only the editable chain', ordered.GetCount() == 0)
                later('Load effect preset', lambda picker: choose(picker, 3))
                press(find(popup, 'Load effect preset'))
                check('native saved preset reload keeps exact order and bypass',
                      ordered.GetCount() == 2 and 'compressor' in ordered.GetString(0)
                      and 'bypassed' in ordered.GetString(1))
                press(find(popup, 'Apply effect chain'))

            def drive_conversion(popup):
                find(popup, 'Output sample rate in Hz, 8000 to 192000').ChangeValue('48000')
                find(popup, 'Output channels, 1 or 2').ChangeValue('1')
                find(popup, 'Target loudness in LUFS; empty leaves levels unchanged').ChangeValue('')
                def adjust_chain(chain_dialog):
                    ordered = find(chain_dialog, 'Ordered audio effects')
                    ordered.SetSelection(0)
                    later('Edit gain effect', lambda edit: set_gain(edit, -12))
                    press(find(chain_dialog, 'Edit selected effect'))
                    press(find(chain_dialog, 'Apply effect chain'))
                later('Ordered audio effects', adjust_chain)
                press(find(popup, 'Edit ordered effect chain and presets'))
                conversion_status = find(popup, 'Audio conversion effect settings')
                pump(lambda: 'Ordered chain active' in conversion_status.GetValue(), 'Advanced chain review')
                check('basic controls are inactive when ordered chain applies',
                      not find(popup, 'Gain in decibels, -60 to 12').IsEnabled())
                press(wx.Window.FindWindowById(wx.ID_OK, popup))

            class SelectedPath:
                def __init__(self, *args, **kwargs):
                    self.output = kwargs.get('style') == wx.FD_SAVE
                    check('file picker is explicit local source or new output',
                          self.output or kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(output if self.output else source)
                def Destroy(self): pass

            def scenario():
                try:
                    check('actual main event loop', gui.GetMainLoop() is not None)
                    later('Ordered audio effects', drive_chain)
                    reviewed = effects_dialog.show_effect_chain(frame, frame.library.root,
                        (Effect('gain', gain_db=-3), Effect('compressor')), available=capabilities)
                    check('reviewed native result returns exact typed chain', reviewed is not None
                          and reviewed[0].kind == 'compressor' and reviewed[1].kind == 'gain'
                          and reviewed[1].parameters['gain_db'] == -9 and reviewed[1].enabled is False)
                    saved, = effect_presets.EffectPresetStore(frame.library.root).list()
                    check('native preset save is durable', saved.effects == reviewed)
                    wx.FileDialog = SelectedPath
                    later('Convert audio and apply effects', drive_conversion)
                    studio_dialog.show_audio_conversion(frame)
                    pump(lambda: not frame.working, 'Actual conversion completion')
                    check('conversion reports the reviewed new output', output.is_file()
                          and 'Saved ' in frame.status.GetValue())
                    with wave.open(str(output), 'rb') as stream:
                        channels = stream.getnchannels()
                        rendered = array('h')
                        rendered.frombytes(stream.readframes(stream.getnframes()))
                        check('actual conversion retains output format', stream.getframerate() == 48000
                              and channels == 1 and stream.getnframes() == 96000)
                    original_rms = math.sqrt(sum(value * value for value in data) / len(data))
                    result_rms = math.sqrt(sum(value * value for value in rendered) / len(rendered))
                    # Require the actual -12 dB attenuation after the reviewed
                    # 48 kHz mono conversion, allowing only PCM quantization.
                    check('actual typed chain changes rendered audio', .245 < result_rms / original_rms < .257)
                    check('conversion preserves original bytes', sha256(source.read_bytes()).hexdigest() == original)
                    check('conversion releases shared runtime admission',
                          broker.ResourceBroker.for_app(frame.library.root).snapshot()['leases'] == [])
                    check('conversion removes owned temporary staging',
                          not list(root.glob('.audio-convert-*')))
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
            check('native conversion leaves no retained host cleanup failure', not frame.executor.cleanup_failures)
            frame.progress_panel.close()
            frame.Destroy()
            frame = None
            gui.Yield()
    finally:
        wx.FileDialog = original_picker
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.progress_panel.close()
            frame.Destroy()
            gui.Yield()
        if native_handle is not None:
            native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': proof,
                      'native_keyboard': 'PASS', 'actual_ffmpeg': 'PASS',
                      'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
                      'audible_listening': 'NOT_RUN', 'live_playback_speed_pitch': 'NOT_IMPLEMENTED'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    def watchdog_failure():
        print(json.dumps({'result': 'FAIL', 'reason': 'Native effects deadline exceeded',
                          'cleanup': 'NOT_CONFIRMED', 'timeout_seconds': 150}), flush=True)
        os._exit(1)
    watchdog = threading.Timer(150, watchdog_failure)
    watchdog.daemon = True
    watchdog.start()
    result = main()
    # Only a returned main has completed its normal/error-path cleanup. If it
    # raises and an owned worker keeps the process alive, retain the watchdog.
    watchdog.cancel()
    raise SystemExit(result)
