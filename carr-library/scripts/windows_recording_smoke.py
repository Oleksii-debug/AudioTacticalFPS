"""Actual installed Studio buttons and real FFmpeg synthetic capture.

Adapted from the existing PR152/PR149 native recording probe. Production never
exposes the fixed test source. Physical microphone, listening and NVDA remain
NOT_RUN; successful source/keyboard evidence does not replace those gates.
"""
import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--output', type=Path)
    arguments = parser.parse_args()
    if not arguments.native or sys.platform != 'win32':
        parser.error('This acceptance probe requires --native on actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.capture_types import AudioCancelled
    from carr_library.audio.runtime import find_ffmpeg
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    import carr_library.audio.capture_process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.audio.windows_job as windows_module
    import carr_library.audio.player as player_module
    import carr_library.progress as progress_module
    import carr_library.ui.audio_recording as dialog_module
    import carr_library.ui.audio_studio as studio_module

    checks, errors, captures, completed, cancelled_captures = [], [], [], [], []
    report = {'status': 'FAILED', 'actual_native_keys': False,
              'actual_ffmpeg_synthetic_capture': False, 'checks': checks,
              'physical_microphone': 'NOT_RUN', 'nvda': 'NOT_RUN',
              'audio_listening': 'NOT_RUN', 'simultaneous_monitoring': 'NOT_SUPPORTED'}
    deadline = monotonic() + 80
    stage, dialog, studio, finished = 0, None, None, False
    watchdog_done = threading.Event()

    def watchdog():
        if not watchdog_done.wait(100):
            print('FAIL: owned recording-probe watchdog expired', flush=True)
            os._exit(2)  # This probe only; owned child Job Objects die on handle close.

    threading.Thread(target=watchdog, name='native-recording-deadline', daemon=True).start()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    checkout = Path(__file__).resolve().parents[1]
    report['installed_source_sha256'] = {}
    for module in (process_module, recording_module, windows_module, player_module,
                   progress_module, dialog_module, studio_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('installed exact source: ' + module.__name__,
              not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())
        report['installed_source_sha256'][module.__name__] = sha256(actual.read_bytes()).hexdigest()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            abort()
            return True

    app = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    original_factory = dialog_module.RecordingService
    frame = None

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary).resolve()
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        project = root / 'Аудіопроєкт — запис'
        project.mkdir()
        binary = find_ffmpeg(arguments.ffmpeg)
        check('actual existing local FFmpeg selected', binary is not None)
        binary = Path(binary).resolve(strict=True)
        with binary.open('rb') as stream:
            report['ffmpeg'] = {'path': str(binary), 'sha256': file_digest(stream, 'sha256').hexdigest()}
        service = recording_module.RecordingService(library.root, project, ffmpeg=binary)
        service._runtime()
        alternative = '@device_test_explicit_synthetic'
        device = recording_module.RecordingDevice(sha256(alternative.encode()).hexdigest(),
            'Synthetic FFmpeg input, no microphone', alternative)
        literal = 'Незмінний оригінал [pause 2]\nThis remains literal narration.\n'
        controller = StudioController(private_root=library.root)
        controller.new(title='Native recording acceptance', script=literal)
        controller.configure(output_dir=project, ffmpeg=binary)
        original_text = controller.script

        def discover(*, cancelled):
            if cancelled():
                raise AudioCancelled('Synthetic inventory cancelled')
            service._devices = {device.id: device}
            service._engine_hash = recording_module._digest(service.runtime.ffmpeg)
            return (device,)

        # Test-only source injection, while the actual FFmpeg and lifecycle run.
        service.devices = discover
        service._input_arguments = lambda selected: [
            '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000']
        real_capture = service.capture

        def capture(device_id, **kwargs):
            item = {'seconds': 0, 'cancel': kwargs['cancelled'].__self__, 'active': True}
            captures.append(item)
            notify = kwargs['progress']
            def measured(seconds):
                item['seconds'] = seconds
                notify(seconds)
            kwargs['progress'] = measured
            try:
                result = real_capture(device_id, **kwargs)
                completed.append(result)
                return result
            except AudioCancelled:
                cancelled_captures.append(item)
                raise
            finally:
                item['active'] = False

        service.capture = capture

        def factory(selected_root, selected_project, **kwargs):
            check('actual Studio project and library passed to recorder',
                  Path(selected_root).resolve() == library.root.resolve()
                  and Path(selected_project).resolve() == project)
            return service

        dialog_module.RecordingService = factory

        def control(name, owner=None):
            found = wx.FindWindowByName(name, owner or dialog)
            if found is None:
                raise AssertionError('Missing accessible native control: ' + name)
            return found

        def abort():
            for item in captures:
                item['cancel'].set()
            if dialog is not None and bool(dialog):
                dialog.Close()
            else:
                app.ExitMainLoop()

        def guarded(callback):
            def invoke():
                try:
                    callback()
                except Exception:
                    errors.append(traceback.format_exc())
                    abort()
            return invoke

        def press(target, key=wx.WXK_SPACE):
            target.SetFocus()
            def dispatch():
                check('native focus reaches ' + target.GetName(), wx.Window.FindFocus() == target)
                check('native key dispatch: ' + target.GetName(), simulator.Char(key))
            wx.CallLater(25, guarded(dispatch))

        def open_entry():
            studio.notebook.SetSelection(2)
            press(control('Record microphone and saved inputs', studio.dialog))

        def drive():
            nonlocal stage, dialog, finished
            if errors or finished:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native recording acceptance exceeded its deadline.')
            if stage == 0:
                stage = 1
                open_entry()
            elif stage in (6, 13) and (dialog is None or not bool(dialog)):
                if stage == 6:
                    check('explicit Add queues the exact verified original without playback',
                          tuple(studio.playback.playlist) == (Path(completed[0]['path']),)
                          and studio.playback.state == 'stopped')
                    check('recording selection preserves literal input and original text',
                          controller.input_kind == 'narration' and controller.script == original_text)
                    dialog, stage = None, 7
                    open_entry()
                else:
                    check('Escape waits for actual capture cancellation and cleanup',
                          len(cancelled_captures) == 2 and not any(item['active'] for item in captures))
                    check('closing preserves exactly the verified original',
                          len(service.list()) == 1 and service.get(completed[0]['id']) == completed[0]
                          and not list(project.glob('.pending-recording-*')))
                    check('recording resources and audio admission released',
                          service.broker.snapshot()['leases'] == []
                          and not progress_module.recording_audio_notice())
                    check('three native capture commands preserve the narration source',
                          len(captures) == 3 and controller.script == original_text)
                    finished = True
                    app.ExitMainLoop()
                    return
            else:
                if dialog is None or not bool(dialog):
                    dialog = next((window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.GetTitle() == 'Local microphone recording'), None)
                if dialog is not None:
                    refresh = control('Refresh microphones')
                    start = control('Start recording')
                    stop = control('Stop and save recording')
                    cancel = control('Cancel unfinished recording')
                    selection = control('Microphone device (explicit selection required)')
                    saved = control('Saved recordings')
                    status = control('Recording status')
                    if stage == 1 and refresh.IsEnabled():
                        check('opening the actual Studio action does not discover or capture',
                              not service._devices and not captures)
                        check('recording has the existing accessible progress gauge',
                              isinstance(control('Recording operation progress percentage'), wx.Gauge))
                        stage = 2
                        press(refresh)
                    elif stage in (2, 8) and selection.GetCount() == 1 and refresh.IsEnabled():
                        check('single discovered input remains explicitly unselected',
                              selection.GetSelection() == wx.NOT_FOUND and not start.IsEnabled())
                        stage += 1
                        press(selection, wx.WXK_DOWN)
                    elif stage in (3, 9) and selection.GetSelection() == 0 and start.IsEnabled():
                        control('Maximum recording duration in minutes').SetValue(1)
                        control('Recording title').SetValue('Синтетичний запис — Unicode')
                        stage += 1
                        press(start)
                    elif stage == 4 and captures and captures[0]['seconds'] >= .1 and stop.IsEnabled():
                        check('actual FFmpeg produces PCM while native controls remain responsive', captures[0]['active'])
                        stage = 5
                        press(stop)
                    elif stage == 5 and saved.GetCount() == 1 and start.IsEnabled():
                        check('Stop finalizes and verifies an actual retained original',
                              len(completed) == 1 and completed[0]['completion'] == 'stopped'
                              and service.get(completed[0]['id']) == completed[0])
                        check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
                        check('only the verified completed capture reaches one hundred percent',
                              control('Recording operation progress percentage').GetValue() == 100)
                        stage = 6
                        press(control('Add selected recording to Studio playlist'))
                    elif stage == 7 and refresh.IsEnabled() and saved.GetCount() == 1:
                        check('reopened recording dialog does not select a device automatically',
                              selection.GetSelection() == wx.NOT_FOUND and not start.IsEnabled())
                        stage = 8
                        press(refresh)
                    elif stage == 10 and len(captures) == 2 and captures[1]['seconds'] >= .1:
                        stage = 11
                        press(cancel)
                    elif stage == 11 and start.IsEnabled() and len(cancelled_captures) == 1:
                        check('Cancel preserves the prior original and removes unfinished capture',
                              len(service.list()) == 1 and service.get(completed[0]['id']) == completed[0]
                              and len(list(service.assets.glob('*.wav'))) == 1
                              and not list(project.glob('.pending-recording-*')))
                        check('Cancel is announced distinctly', status.GetValue().startswith('Cancelled.'))
                        stage = 12
                        press(start)
                    elif stage == 12 and len(captures) == 3 and captures[2]['seconds'] >= .1:
                        stage = 13
                        press(stop, wx.WXK_ESCAPE)
            wx.CallLater(25, guarded(drive))

        try:
            frame = wx.Frame(None, title='Installed Audio Studio recording acceptance')
            frame.library = library
            frame.report = lambda message: print('UI: ' + str(message), flush=True)
            studio = AudioStudioDialog(frame, controller)
            app.SetTopWindow(studio.dialog)
            studio.dialog.Show()
            studio.dialog.Raise()
            wx.CallLater(50, guarded(drive))
            app.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
            check('native recording acceptance reached its terminal stage', finished and stage == 13)
            report.update(status='PASS', actual_native_keys=True, actual_ffmpeg_synthetic_capture=True,
                          retained_pcm=completed[0]['audio'])
        finally:
            dialog_module.RecordingService = original_factory
            for item in captures:
                item['cancel'].set()
            if studio is not None:
                studio.closed = True
                studio.cancelled.set()
                studio.timer.Stop()
                studio.playback_timer.Stop()
                studio.playback.close()
                studio.progress_view.close()
                studio.executor.shutdown(wait=True, cancel_futures=True)
                studio.dialog.Destroy()
            if frame is not None:
                frame.Destroy()
                app.Yield()
            if native_handle is not None:
                native_handle.close()
            watchdog_done.set()
            if arguments.output:
                arguments.output.parent.mkdir(parents=True, exist_ok=True)
                arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
