"""Installed native ordered-effects controls and an actual FFmpeg conversion.

Only source/output file-picker results are supplied by the fixture. Parameter
and preset dialogs, keyboard events, background work and rendering are real.
This probe makes no physical-device, listening-quality or NVDA claim.
"""
from array import array
import argparse
import importlib.util
import sys
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback
import wave


def _native(audio_evidence):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import Effect
    from carr_library.audio import effect_presets, dsp
    from carr_library import runtime_settings
    from carr_library.studio import render
    from carr_library.runtime_settings import audio_runtime
    from carr_library.ui import effects_dialog, studio_dialog
    from carr_library.ui.desktop_app import DesktopFrame

    helper_path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_carr_effect_acceptance', helper_path)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    installed_sources = acceptance.verify_installed_sources(
        effect_presets, dsp, effects_dialog, studio_dialog, runtime_settings, render)
    checks, errors, proof = [], [], {}
    lifetime_proofs = []
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
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
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
        proof.update(acceptance.verify_audio_evidence(runtime, audio_evidence))

    check('actual wx Windows controls', wx.Platform == '__WXMSW__')
    check('exact current installed effects modules', len(installed_sources) == 6)
    original_picker = wx.FileDialog
    frame = None
    frames = []
    watchdog = None
    try:
        with TemporaryDirectory(prefix='resonance-native-effects-') as temporary:
            root = Path(temporary)
            source = root / 'Synthetic original — Україна.wav'
            output = root / 'Reviewed output.wav'
            data = array('h', (round(7000 * math.sin(2 * math.pi * 440 * index / 24000))
                               for index in range(48000)))
            with wave.open(str(source), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(data.tobytes())
            original = sha256(source.read_bytes()).hexdigest()
            frame = DesktopFrame(LibraryApp(root / 'library'))
            frames.append(frame)
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
                find(popup, 'Output sample rate in Hz, 8000 to 192000; Opus requires 48000').ChangeValue('48000')
                find(popup, 'Output channels: 1 for mono, 2 for stereo').ChangeValue('1')
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


            def schedule_until(predicate, complete, *, label, timeout=20):
                deadline = monotonic() + timeout
                def poll():
                    try:
                        if errors:
                            return
                        if predicate():
                            complete()
                        elif monotonic() >= deadline:
                            raise TimeoutError(label)
                        else:
                            wx.CallLater(20, poll)
                    except Exception:
                        errors.append(traceback.format_exc())
                        for window in wx.GetTopLevelWindows():
                            if window and isinstance(window, wx.Dialog) and window.IsModal():
                                window.EndModal(wx.ID_CANCEL)
                        gui.ExitMainLoop()
                wx.CallLater(20, poll)

            def lifetime(label, complete):
                """Hold an actual completed inventory, then delete its native owner."""
                owner = DesktopFrame(LibraryApp(root / ('lifetime-' + label)))
                frames.append(owner)
                owner.Show()
                original_run = owner.run_job
                state = dict(label=label, delivered=False, events=[], forbidden=[],
                             completed=False, native_dead=False, returned=False)
                lifetime_proofs.append(state)

                def observe_job(title, operation, done=None, failed=None, **options):
                    check('lifetime uses actual effect capability operation',
                          title == 'Checking local audio effects')
                    def finished(value):
                        # The original Desktop worker executed the real operation,
                        # including its shared admission and actual FFmpeg child.
                        state.update(value=value, done=done, completed=True)
                    def rejected(error):
                        state.update(error=str(error), completed=True)
                    return original_run(title, operation, finished, rejected, **options)
                owner.run_job = observe_job

                def observe_control(control, method):
                    original = getattr(control, method)
                    def call(*args, **kwargs):
                        if state['native_dead']:
                            state['forbidden'].append(method)
                            raise AssertionError('A late callback touched a deleted effects control.')
                        return original(*args, **kwargs)
                    setattr(control, method, call)

                def opened(popup):
                    state['native_id'] = popup.GetId()
                    def destroyed(event):
                        if event.GetId() == state['native_id']:
                            state['events'].append(dict(
                                id=event.GetId(), original_wrapper=event.GetEventObject() is popup))
                        event.Skip()
                    popup.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                    for name in ('Audio conversion effect settings',
                                 'Edit ordered effect chain and presets',
                                 'Use basic effect controls',
                                 'Gain in decibels, -60 to 12'):
                        control = find(popup, name)
                        for method in ('SetFocus', 'Enable'):
                            observe_control(control, method)
                        if isinstance(control, wx.TextCtrl):
                            observe_control(control, 'ChangeValue')
                    press(find(popup, 'Edit ordered effect chain and presets'))
                    schedule_until(lambda: state['completed'] and not owner.working,
                                   lambda: dispose(popup), label='Actual capability completion')

                def dispose(popup):
                    if 'error' in state:
                        raise AssertionError(state['error'])
                    check('completed actual inventory contains the reviewed effects',
                          isinstance(state['value'], tuple) and 'gain' in state['value']
                          and 'compressor' in state['value'])
                    if label == 'direct':
                        popup.Destroy()
                        check('direct Destroy immediately detaches effect owner',
                              not popup._effect_alive)
                    else:
                        # C++ parent-driven deletion must reach the cached-ID
                        # fallback without calling the child's Python Destroy.
                        owner.Destroy()
                    schedule_until(
                        lambda: not popup and len(state['events']) == 1
                            and (label == 'direct' or not owner),
                        lambda: after_native_deletion(popup),
                        label='Actual native effects owner deletion', timeout=8)

                def after_native_deletion(popup):
                    check('one actual native effect destruction event', len(state['events']) == 1)
                    check('deleted wrapper has a detached plain-state owner', not popup._effect_alive)
                    state['native_dead'] = True
                    def deliver():
                        # Replay the actual completed result only after the real
                        # native peer is gone. No operation result is substituted.
                        state['done'](state['value'])
                        state['delivered'] = True
                        check('late inventory callback touches no deleted control',
                              not state['forbidden'])
                        check('closed inventory review opens no new effect editor',
                              not any(window and isinstance(window, wx.Dialog)
                                  and window.GetTitle() == 'Ordered audio effects'
                                  for window in wx.GetTopLevelWindows()))
                        check('lifetime cases leave input and published output untouched',
                              sha256(source.read_bytes()).hexdigest() == original
                              and sha256(output.read_bytes()).hexdigest() == state['output_hash'])
                        wx.CallLater(20, complete)
                    wx.CallAfter(deliver)

                state['output_hash'] = sha256(output.read_bytes()).hexdigest()
                later('Convert audio and apply effects', opened)
                studio_dialog.show_audio_conversion(owner)
                state['returned'] = True

            def finish_lifetimes():
                check('both native deletion paths delivered the actual late callback',
                      len(lifetime_proofs) == 2 and all(row['returned'] and row['delivered']
                        and len(row['events']) == 1 and not row['forbidden']
                        for row in lifetime_proofs))
                gui.ExitMainLoop()

            def timeout():
                errors.append('Native effects acceptance exceeded 180 seconds.')
                for window in wx.GetTopLevelWindows():
                    if window and isinstance(window, wx.Dialog) and window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                gui.ExitMainLoop()

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
                except Exception:
                    errors.append(traceback.format_exc())
                    gui.ExitMainLoop()
                else:
                    wx.CallAfter(lambda: lifetime('direct',
                        lambda: lifetime('parent', finish_lifetimes)))

            watchdog = wx.CallLater(180000, timeout)
            wx.CallAfter(scenario)
            gui.MainLoop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            # Bind the exact same selected executable paths after all work too.
            verify_audio(runtime)
            check('installed sources remain exact after conversion and native lifetime work',
                  acceptance.verify_installed_sources(effect_presets, dsp, effects_dialog,
                      studio_dialog, runtime_settings, render) == installed_sources)
            watchdog.Stop()
            watchdog = None
            for owner in frames:
                owner.cancel_event.set()
                owner._closing = True
                owner.executor.shutdown(wait=True, cancel_futures=True)
                if owner:
                    owner.Destroy()
            gui.Yield()
            frames.clear()
            frame = None
    finally:
        wx.FileDialog = original_picker
        if watchdog is not None:
            watchdog.Stop()
        for owner in frames:
            owner.cancel_event.set()
            owner._closing = True
            owner.executor.shutdown(wait=True, cancel_futures=True)
            if owner:
                owner.Destroy()
        gui.Yield()
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': proof,
                      'installed_sources': installed_sources,
                      'lifetime': [{'label': row['label'], 'events': row['events'],
                                    'delivered': row['delivered']} for row in lifetime_proofs],
                      'native_keyboard': 'PASS', 'actual_ffmpeg': 'PASS',
                      'native_file_pickers': 'NOT_RUN', 'nvda': 'NOT_RUN',
                      'audible_listening': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run installed native effects acceptance on Windows with Python -I -B.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _native(args.audio_evidence)
    finally:
        if native_handle is not None:
            native_handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
