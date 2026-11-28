"""Installed native clip controls plus real FFmpeg rendering of synthetic PCM.

The file-picker result is supplied; numeric and choice dialogs stay native.
Real Windows keyboard
events activate buttons; a real wx event loop handles background completion.
Physical recording devices, native file pickers and NVDA are separate gates.
"""
import argparse
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loop-output-device', type=int)
    parser.add_argument('--loop-output-identity')
    options = parser.parse_args(argv)
    if (options.loop_output_device is None) != (options.loop_output_identity is None):
        parser.error('Loop playback requires both the exact output index and its reviewed identity.')
    if options.loop_output_identity is not None and (
            options.loop_output_device < 0 or len(options.loop_output_identity) != 64
            or any(value not in '0123456789abcdef' for value in options.loop_output_identity)):
        parser.error('Choose a nonnegative output index and its lower-case SHA256 identity.')
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
    loop_proof = {'status': 'NOT_RUN',
        'reason': 'No exact reviewed output endpoint was supplied for loop playback.',
        'acoustic_listening': 'NOT_RUN', 'physical_output_device': 'NOT_RUN'}
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
                entry = 'audio-runtime/bin/' + name + '.exe'
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
        name = control.GetName()
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'Clip control did not receive foreground keyboard focus', timeout=10)
        check('keyboard focus: ' + name, wx.Window.FindFocus() is control)
        check('Space key down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            sleep(.04)
        finally:
            check('Space key up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
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


    def answer_effect_chain(gain):
        deadline = monotonic() + 20
        def failed():
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            gui.ExitMainLoop()

        def edit_numeric():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == 'Edit gain effect']
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native gain editor did not open.')
                    wx.CallLater(20, edit_numeric)
                    return
                popup, = matching
                control = wx.Window.FindWindowByName('gain db (dB); -120 to 24', popup)
                check('actual numeric clip effect control', isinstance(control, wx.TextCtrl))
                control.ChangeValue(str(gain))
                press(wx.Window.FindWindowByName('Apply effect', popup))
            except Exception:
                failed()

        def answer():
            try:
                matching = [window for window in wx.GetTopLevelWindows()
                            if window.IsShown() and window.GetTitle() == 'Ordered audio effects']
                if not matching:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native ordered effects did not open.')
                    wx.CallLater(20, answer)
                    return
                popup, = matching
                kind = wx.Window.FindWindowByName('Effect type to add', popup)
                check('actual supported effect choice', isinstance(kind, wx.Choice)
                      and kind.SetStringSelection('gain'))
                press(wx.Window.FindWindowByName('Add effect', popup))
                wx.CallLater(20, edit_numeric)
                press(wx.Window.FindWindowByName('Edit selected effect', popup))
                press(wx.Window.FindWindowByName('Apply effect chain', popup))
            except Exception:
                failed()
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
                    check('native loop controls are named and require an explicit preview', all(
                        isinstance(dialog.buttons[name], wx.Button)
                        and dialog.buttons[name].GetName() and not dialog.buttons[name].IsEnabled()
                        for name in ('loop', 'loop_off')))
                    check('opening the clip editor does not start a preview loop',
                          dialog.player is None and not dialog._preview_timer.IsRunning())
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
                    answer_effect_chain(-6)
                    press(dialog.buttons['clip_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native clip effects')
                    clip = dialog.project.scenes[0].cues[0]
                    check('native clip effects persist in schema three',
                          dialog.project.schema == 3 and len(clip.effects) == 1
                          and clip.effects[0].parameters['gain_db'] == -6)
                    answer_effect_chain(-6)
                    press(dialog.buttons['master_effects'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native master effects')
                    check('native master effects persist independently',
                          len(dialog.project.effects) == 1
                          and dialog.project.effects[0].parameters['gain_db'] == -6)
                    affected = dialog.project
                    effect_job = editor.service.submit(affected, outputs)
                    effect_results = []
                    frame.run_job('Native saved effect render', lambda: editor.service.run(effect_job),
                                  effect_results.append, lambda error: errors.append(str(error)))
                    pump(lambda: not frame.working, 'Rendering saved native effect chains')
                    check('native effect project completes actual rendering',
                          bool(effect_results) and effect_results[0]['status'] == 'complete')
                    def pcm(path):
                        with wave.open(str(path), 'rb') as stream:
                            data = stream.readframes(stream.getnframes())
                        return data, struct.unpack('<' + 'h' * (len(data) // 2), data)
                    baseline = pcm(Path(results[0]['destination']) / 'master.wav')[1]
                    master = Path(effect_results[0]['destination']) / 'master.wav'
                    actual = pcm(master)[1]
                    rms_before = math.sqrt(sum(value * value for value in baseline) / len(baseline))
                    rms_after = math.sqrt(sum(value * value for value in actual) / len(actual))
                    check('actual native clip and master effects both apply',
                          len(actual) == len(baseline) and .245 < rms_after / rms_before < .257)
                    chapter, = (Path(effect_results[0]['destination']) / 'chapters').glob('*.wav')
                    check('delivered chapter matches the mastered audio', pcm(chapter)[0] == pcm(master)[0])
                    dialog.master_rate.ChangeValue('48000')
                    press(dialog.buttons['sample_rate'])
                    pump(lambda: not frame.working and not dialog.pending, 'Saving native master sample rate')
                    check('native master sample rate preserves effect chains',
                          dialog.project.sample_rate == 48000 and dialog.project.effects == affected.effects)
                    if options.loop_output_identity is not None:
                        import carr_library
                        from carr_library.speech import (player as player_module,
                            output_devices, progress_speech, _wave_output)
                        package_root = Path(carr_library.__file__).resolve(strict=True).parent
                        source_root = Path(__file__).resolve(strict=True).parents[1] / 'src'
                        module_hashes = {}
                        for module in (ui, clips, player_module, output_devices,
                                       progress_speech, _wave_output):
                            loaded = Path(module.__file__).resolve(strict=True)
                            expected = source_root.joinpath(*module.__name__.split('.')).with_suffix('.py')
                            check('loop module belongs to the actual installed package: ' + module.__name__,
                                  loaded.is_relative_to(package_root))
                            check('loop module bytes are bounded: ' + module.__name__,
                                  loaded.stat().st_size <= 2 * 1024**2
                                  and expected.stat().st_size <= 2 * 1024**2)
                            actual_bytes, expected_bytes = loaded.read_bytes(), expected.read_bytes()
                            check('loop module matches the exact checked-out source: ' + module.__name__,
                                  actual_bytes == expected_bytes)
                            module_hashes[module.__name__] = sha256(actual_bytes).hexdigest()

                        player = player_module.WavePlayer()
                        selected, = [value for value in player.output_devices()
                                     if value.index == options.loop_output_device]
                        check('native loop output matches the separately reviewed exact identity',
                              selected.identity == options.loop_output_identity)
                        player.select_output(selected)
                        dialog.player = player
                        check('selecting the exact loop output does not start audio',
                              player.path is None and player.state == 'stopped')
                        before_project = dialog.project
                        press(dialog.buttons['preview'])
                        pump(lambda: not frame.working and not dialog.pending
                             and dialog._preview_binding is not None, 'Rendering explicit native loop preview')
                        preview_path = player.path
                        preview_digest = sha256(preview_path.read_bytes()).hexdigest()
                        check('first native clip preview does not enable loop automatically',
                              not player.repeat_enabled and player.repeat_count == 0)
                        pump(lambda: player.state == 'stopped', 'Waiting for the short preview to end naturally',
                             timeout=8)
                        check('loop remains keyboard available after natural completion',
                              dialog.buttons['loop'].IsEnabled())
                        press(dialog.buttons['loop'])
                        pump(lambda: player.repeat_count >= 2, 'Waiting for two actual native clip repeats',
                             timeout=8)
                        repeats = player.repeat_count
                        check('actual native loop keeps the exact selected output',
                              player.output_device.identity == options.loop_output_identity
                              and player.active_output_index == options.loop_output_device
                              and player.repeat_enabled and player.ab_loop == (0, player.duration_seconds))
                        check('native loop retains exact preview and project bytes',
                              player.path == preview_path
                              and sha256(preview_path.read_bytes()).hexdigest() == preview_digest
                              and dialog.project is before_project
                              and sha256(retained.read_bytes()).hexdigest() == original
                              and sha256(source.read_bytes()).hexdigest() == original)
                        press(dialog.buttons['loop_off'])
                        pump(lambda: not player.repeat_enabled and not dialog._repeat_pending,
                             'Disabling native clip loop', timeout=5)
                        after_disable = player.repeat_count
                        pump(lambda: player.state == 'stopped', 'Finishing the explicitly disabled loop',
                             timeout=8)
                        gui.Yield()
                        check('native loop disable leaves natural completion stopped',
                              not dialog._preview_timer.IsRunning() and player.repeat_count == after_disable)
                        press(dialog.buttons['stop'])
                        check('native Stop revokes loop and closes the exact playback owner',
                              dialog._preview_binding is None and not player.repeat_enabled
                              and not player._open and player._wave is None
                              and not dialog._preview_timer.IsRunning()
                              and not dialog.buttons['loop'].IsEnabled())
                        loop_proof.update(status='PASS', reason=None, completed_repeats=repeats,
                            output_index=options.loop_output_device,
                            output_identity=options.loop_output_identity,
                            preview_sha256=preview_digest, module_sha256=module_hashes,
                            native_keyboard='PASS', fixture='synthetic retained PCM with saved clip effects')

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
        'native_file_pickers': 'NOT_RUN', 'physical_microphone': 'NOT_RUN',
        'loop_preview': loop_proof}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
