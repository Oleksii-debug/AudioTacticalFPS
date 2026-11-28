"""Installed native ordered-effects controls and an actual FFmpeg conversion.

Only source/output file-picker results are supplied by the fixture. Parameter
and preset dialogs, keyboard events, background work and rendering are real.
This probe makes no physical-device, listening-quality or NVDA claim.
"""
from array import array
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows required'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import Effect
    from carr_library.audio import effect_presets, conversion_batch
    from carr_library.runtime_settings import audio_runtime
    from carr_library.ui import effects_dialog, studio_dialog, audio_conversion_dialog
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
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            proof[name] = {'path': str(binary), 'sha256': digest}
            if bundle:
                entry = 'audio-runtime/bin/' + name + '.exe'
                check('exact bundled ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed ' + name, digest == manifest['files'][entry])

    check('actual wx Windows controls', wx.Platform == '__WXMSW__')
    for module in (effects_dialog, effect_presets, studio_dialog, conversion_batch, audio_conversion_dialog):
        check('installed module: ' + module.__name__,
              'site-packages' in [part.casefold() for part in Path(module.__file__).resolve().parts])
    original_picker, original_directory_picker = wx.FileDialog, wx.DirDialog
    frame = None
    try:
        with TemporaryDirectory(prefix='resonance-native-effects-') as temporary:
            # Windows TEMP may use an 8.3 alias; production reviews resolved paths.
            root = Path(temporary).resolve(strict=True)
            source = root / 'Synthetic original — Україна.wav'
            output = root / 'Reviewed output.wav'
            data = array('h', (round(7000 * math.sin(2 * math.pi * 440 * index / 24000))
                               for index in range(48000)))
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(data.tobytes())
            original = sha256(source.read_bytes()).hexdigest()
            second_source = root / 'Another selected original.wav'
            second_source.write_bytes(source.read_bytes())
            second_original = sha256(second_source.read_bytes()).hexdigest()
            frame = DesktopFrame(LibraryApp(root / 'library'))
            frame.Show()
            gui.SetTopWindow(frame)
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

            class SelectedBatchPaths:
                def __init__(self, *args, **kwargs):
                    check('batch picker explicitly allows several existing local files',
                          kwargs.get('style') == wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
                def ShowModal(self): return wx.ID_OK
                def GetPaths(self): return [str(source), str(second_source)]
                def Destroy(self): pass

            class SelectedBatchParent:
                def __init__(self, *args, **kwargs):
                    check('batch parent picker requires an existing directory',
                          kwargs.get('style') == wx.DD_DIR_MUST_EXIST)
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(root)
                def Destroy(self): pass

            def drive_batch(popup, *, resume):
                find(popup, 'Output channels, 1 or 2').ChangeValue('1')
                find(popup, 'Target loudness in LUFS; empty leaves levels unchanged').ChangeValue('')
                format_choice = find(popup, 'Batch output format')
                check('native batch format selector', isinstance(format_choice, wx.Choice))
                format_choice.SetStringSelection('wav')
                resume_choice = find(popup, 'Resume matching audio batch')
                check('matching batch resume is opt-in', resume_choice.GetValue() is False)
                if resume:
                    press(resume_choice)
                    check('native explicit resume checked', resume_choice.GetValue() is True)
                press(wx.Window.FindWindowById(wx.ID_OK, popup))

            def name_batch(popup):
                check('native batch output naming', isinstance(popup, wx.TextEntryDialog))
                popup.SetValue('Native reviewed batch')
                press(wx.Window.FindWindowById(wx.ID_OK, popup))

            def review_batch(popup, *, resume):
                review = find(popup, 'Selected audio batch review')
                text = review.GetValue()
                print(json.dumps({'event': 'native-audio-batch-review',
                    'temporary_root': temporary, 'selected_sources': [str(source), str(second_source)],
                    'review': text}, ensure_ascii=True), flush=True)
                check('native batch review is read-only', isinstance(review, wx.TextCtrl) and not review.IsEditable())
                check('review names both originals and exact output settings',
                      json.dumps(str(source), ensure_ascii=False) in text
                      and json.dumps(str(second_source), ensure_ascii=False) in text
                      and '24000 Hz, 1 channel(s)' in text and 'Selected files: 2.' in text)
                check('review exposes no-clobber and retained completed files',
                      'Existing output files are never overwritten.' in text
                      and 'Cancellation keeps completed files.' in text)
                check('conversion requires explicit native confirmation',
                      popup.GetDefaultItem() is wx.Window.FindWindowById(wx.ID_CANCEL, popup))
                if resume:
                    check('native resume review is distinct', 'Resume only matching saved outputs.' in text)
                press(find(popup, 'Convert reviewed files'))

            def batch_scenario(*, resume):
                wx.FileDialog, wx.DirDialog = SelectedBatchPaths, SelectedBatchParent
                later('Convert audio batch and apply effects', lambda popup: drive_batch(popup, resume=resume))
                later('Batch output folder', name_batch)
                later('Review audio conversion batch', lambda popup: review_batch(popup, resume=resume))
                studio_dialog.show_audio_conversion(frame, batch=True)
                pump(lambda: not frame.working and 'Audio batch completed: 2 of 2 files saved' in frame.status.GetValue(),
                     'Actual reviewed batch completion', 90)
                directory = root / 'Native reviewed batch'
                receipt = json.loads((directory / 'conversion-manifest.json').read_text(encoding='utf-8'))
                check('actual batch has two durable completed receipts',
                      receipt['done'] == 2 and receipt['failed'] == 0
                      and all(row['status'] == 'completed' for row in receipt['items']))
                for row in receipt['items']:
                    path = directory / row['output']
                    with wave.open(str(path), 'rb') as stream:
                        check('batch output uses actual measured sample counts',
                              (stream.getframerate(), stream.getnchannels(), stream.getnframes()) == (24000, 1, 48000))
                    check('batch receipt matches actual output bytes',
                          sha256(path.read_bytes()).hexdigest() == row['candidate']['sha256'])
                check('batch keeps both selected originals unchanged',
                      sha256(source.read_bytes()).hexdigest() == original
                      and sha256(second_source.read_bytes()).hexdigest() == second_original)
                return {row['output']: ((directory / row['output']).read_bytes(),
                                        (directory / row['output']).stat().st_mtime_ns)
                        for row in receipt['items']}

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
                    batch_before = batch_scenario(resume=False)
                    # Clear only the presentation text so the second wait cannot
                    # accidentally observe the first batch's completion message.
                    frame.status.ChangeValue('Reviewing the same completed batch again.')
                    batch_after = batch_scenario(resume=True)
                    check('explicit native resume preserves completed output bytes and timestamps', batch_after == batch_before)
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
            frame.Destroy()
            frame = None
            gui.Yield()
    finally:
        wx.FileDialog, wx.DirDialog = original_picker, original_directory_picker
        if frame is not None:
            frame.cancel_event.set()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            gui.Yield()
        if native_handle is not None:
            native_handle.close()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': proof,
                      'native_keyboard': 'PASS', 'actual_ffmpeg': 'PASS',
                      'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
                      'audible_listening': 'NOT_RUN'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
