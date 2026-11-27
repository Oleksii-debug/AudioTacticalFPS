"""Installed native recording controls and real FFmpeg with synthetic input only.

Run using the installed interpreter with -I -B. Optional --ffmpeg/--ffprobe
select the staged local executables. This never discovers or opens a microphone.
"""
import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import wave


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    parser.add_argument('--require-monitor-output', action='store_true',
        help='Require an actual identifiable waveOut output and exercise explicit dry monitoring.')
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioRuntime
    from carr_library.audio_runtime import bundled_audio_tools, verify_audio_runtime
    from carr_library.progress import ProgressState
    from carr_library.ui.progress_panel import NativeProgressPanel
    from carr_library.audio.models import AudioCancelled
    import carr_library.audio.process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.audio.recording_monitor as monitor_module
    import carr_library.ui.recording_dialog as dialog_module
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, captures, completed, cancelled_captures = [], [], [], [], []
    capture_processes, monitors, paused_snapshot = [], [], {}
    audio_proof = {}
    deadline = monotonic() + 80
    stage, dialog, frame, finished = 0, None, None, False

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

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

    checkout = Path(__file__).resolve().parents[1]
    for module in (process_module, recording_module, monitor_module, dialog_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            abort()
            return True

    app = ProbeApp(False)
    app.SetExitOnFrameDelete(False)
    frame_destroyed = threading.Event()
    simulator = wx.UIActionSimulator()
    original_factory = dialog_module.RecordingService
    original_process, original_monitor = recording_module.run_process, monitor_module.DryMonitor

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        tools = bundled_audio_tools()
        if tools is None and os.environ.get('CARR_V3_AUDIO'):
            tools = verify_audio_runtime(Path(os.environ['CARR_V3_AUDIO']))['tools']
        runtime = AudioRuntime(root / 'runtime',
            ffmpeg=arguments.ffmpeg or (tools or {}).get('ffmpeg'),
            ffprobe=arguments.ffprobe or (tools or {}).get('ffprobe'))
        verify_audio(runtime)
        service = recording_module.RecordingService(library.root, runtime=runtime)
        alternative = '@device_test_explicit_synthetic'
        device = recording_module.RecordingDevice(sha256(alternative.encode()).hexdigest(),
            'Synthetic FFmpeg input, no microphone', alternative)

        # Test-only source adapter. Production exposes only explicit DirectShow
        # identities and contains no lavfi selector or arbitrary capture argv.
        def discover(*, cancelled):
            if cancelled():
                raise AudioCancelled('Synthetic inventory cancelled')
            service._devices = {device.id: device}
            service._engine_hash = recording_module._digest(runtime.ffmpeg)
            return (device,)
        service.devices = discover
        service._input_arguments = lambda selected: [
            '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000']
        real_capture = service.capture

        def capture(device_id, **kwargs):
            item = {'seconds': 0, 'cancel': kwargs['cancelled'].__self__, 'active': True}
            captures.append(item)
            observe = kwargs['progress']
            def report(seconds):
                item['seconds'] = seconds
                observe(seconds)
            kwargs['progress'] = report
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
        dialog_module.RecordingService = lambda selected_root: service

        def owned_process(command, **kwargs):
            item = {'active': True, 'path': Path(command[-1])}
            check('pause segments never overlap actual capture owners',
                  not any(previous['active'] for previous in capture_processes))
            capture_processes.append(item)
            try:
                result = original_process(command, **kwargs)
                with wave.open(str(item['path']), 'rb') as reader:
                    data = reader.readframes(reader.getnframes())
                    item['frames'], item['pcm_sha256'] = reader.getnframes(), sha256(data).hexdigest()
                return result
            finally:
                item['active'] = False
        recording_module.run_process = owned_process

        def actual_monitor():
            owner = original_monitor()
            monitors.append(owner)
            return owner
        monitor_module.DryMonitor = actual_monitor

        def control(name):
            found = wx.FindWindowByName(name, dialog)
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

        def drive():
            nonlocal stage, dialog
            if errors or finished:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native recording acceptance exceeded its deadline')
            if dialog is None:
                dialog = next((window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.GetTitle() == 'Local microphone recording'), None)
            if dialog is None:
                wx.CallLater(25, guarded(drive))
                return
            refresh = control('Refresh microphones')
            start = control('Start recording')
            stop = control('Stop and save recording')
            cancel = control('Cancel unfinished recording')
            selection = control('Microphone device (explicit selection required)')
            saved = control('Saved recordings')
            status = control('Recording status')
            pause = control('Pause microphone recording')
            resume = control('Resume microphone recording')
            monitoring = control('Enable dry input monitoring')
            output = control('Monitoring output (explicit selection required)')
            if stage == 0 and refresh.IsEnabled():
                check('opening the dialog does not discover or capture', not service._devices and not captures)
                check('monitoring is explicitly off and no output has been opened',
                      not monitoring.GetValue() and not monitors and output.GetSelection() == wx.NOT_FOUND)
                panel = dialog.progress_panel
                check('recording uses the canonical private native progress panel',
                      isinstance(panel, NativeProgressPanel)
                      and dialog.operation_progress is panel
                      and panel.hub is frame.progress_panel.hub)
                check('recording progress has a native accessible gauge',
                      isinstance(panel.gauge, wx.Gauge) and panel.gauge.GetParent() is panel.panel
                      and panel.gauge.GetName().startswith('Current operation progress: '))
                check('recording progress query and phase remain keyboard accessible',
                      panel.text.AcceptsFocusFromKeyboard() and panel.query_button.AcceptsFocusFromKeyboard()
                      and control('Query recording progress') is panel.query_button)
                stage = 1
                press(refresh)
            elif stage == 1 and selection.GetCount() == 1 and refresh.IsEnabled():
                check('single discovered input is not automatically selected', selection.GetSelection() == wx.NOT_FOUND)
                check('Start remains disabled until explicit selection', not start.IsEnabled() and not captures)
                stage = 2
                press(selection, wx.WXK_DOWN)
            elif stage == 2 and selection.GetSelection() == 0 and start.IsEnabled():
                if arguments.require_monitor_output:
                    stage = 20
                    press(control('Refresh monitoring outputs'))
                else:
                    stage = 22
            elif stage == 20 and control('Refresh monitoring outputs').IsEnabled():
                check('actual output inventory requires explicit selection and opens nothing',
                      output.GetCount() > 0 and output.GetSelection() == wx.NOT_FOUND and not monitors)
                stage = 21
                press(monitoring)
            elif stage == 21 and monitoring.GetValue():
                stage = 23
                press(output, wx.WXK_DOWN)
            elif stage == 23 and output.GetSelection() == 0 and start.IsEnabled():
                stage = 22
            elif stage == 22 and start.IsEnabled():
                control('Maximum recording duration in minutes').SetValue(1)
                control('Recorded 16-bit PCM WAV sample rate').SetStringSelection('24000')
                control('Recording title').SetValue('Native synthetic recording')
                stage = 3
                press(start)
            elif (stage == 3 and captures and captures[0]['seconds'] >= .1 and stop.IsEnabled()
                    and pause.IsEnabled() and dialog.progress_panel.active_task.snapshot().completed > 0):
                check('actual FFmpeg reports recorded PCM while UI remains responsive', captures[0]['active'])
                snapshot = dialog.progress_panel.active_task.snapshot()
                check('native progress reports measured recording duration without an invented total',
                      snapshot.state == ProgressState.RUNNING and snapshot.unit == 'milliseconds'
                      and snapshot.completed > 0 and snapshot.total is None and snapshot.percent is None)
                check('native Pause is available only for an active segment', pause.IsEnabled() and not resume.IsEnabled())
                stage = 30
                press(pause)
            elif stage == 30 and dialog.capture_state == 'paused' and resume.IsEnabled():
                check('Pause acknowledges exact capture-owner exit before enabling Resume',
                      len(capture_processes) == 1 and not capture_processes[0]['active']
                      and dialog.progress_panel.active_task.snapshot().state == ProgressState.PAUSED)
                check('Pause confirms every monitoring handle and buffer released',
                      all(not owner.handle.value and not owner.pending for owner in monitors))
                paused_snapshot.update(at=monotonic(), bytes=capture_processes[0]['path'].read_bytes(),
                                       frames=capture_processes[0]['frames'])
                stage = 31
            elif stage == 31 and monotonic() - paused_snapshot['at'] >= .2:
                check('paused wall time adds no frames and opens no second input',
                      len(capture_processes) == 1 and not capture_processes[0]['active']
                      and capture_processes[0]['path'].read_bytes() == paused_snapshot['bytes'])
                stage = 32
                press(resume)
            elif (stage == 32 and dialog.capture_state == 'recording' and len(capture_processes) == 2
                  and captures[0]['seconds'] * 24000 > paused_snapshot['frames'] + 2400):
                check('Resume starts a new owned segment in the same measured operation',
                      capture_processes[1]['active'] and not capture_processes[0]['active']
                      and dialog.progress_panel.active_task.snapshot().state == ProgressState.RUNNING)
                stage = 4
                press(stop)
            elif stage == 4 and saved.GetCount() == 1 and start.IsEnabled():
                check('Stop finalizes and verifies an actual retained recording', len(completed) == 1
                    and completed[0]['completion'] == 'stopped' and service.get(completed[0]['id']) == completed[0])
                check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
                retained = completed[0]
                check('verified v2 timeline retains exact segment counts and PCM hashes',
                      retained['schema'] == 'carr-recording/2' and len(retained['segments']) == 2
                      and retained['audio']['frames'] == sum(item['frames'] for item in capture_processes)
                      and all(segment['frames'] == item['frames'] and segment['pcm_sha256'] == item['pcm_sha256']
                              for segment, item in zip(retained['segments'], capture_processes)))
                if arguments.require_monitor_output:
                    check('explicit native monitoring submitted captured PCM and closed owned buffers',
                          retained['monitoring']['enabled'] and retained['monitoring']['submitted_frames'] > 0
                          and len(monitors) == 2 and all(not owner.handle.value and not owner.pending for owner in monitors))
                else:
                    check('default recording never opens a monitoring output',
                          not monitors and not retained['monitoring']['enabled'])
                snapshot = dialog.progress_panel.active_task.snapshot()
                check('native progress completes only after retained recording verification',
                      snapshot.state == ProgressState.SUCCEEDED and snapshot.percent == 100
                      and dialog.progress_panel.gauge.GetValue() == 100)
                stage = 5
                press(start)
            elif stage == 5 and len(captures) == 2 and captures[1]['seconds'] >= .1 and pause.IsEnabled():
                stage = 50
                press(pause)
            elif stage == 50 and dialog.capture_state == 'paused' and resume.IsEnabled():
                check('second take is actually paused before cancellation',
                      not any(item['active'] for item in capture_processes))
                stage = 6
                press(cancel)
            elif stage == 6 and start.IsEnabled() and len(cancelled_captures) == 1:
                check('Cancel removes unfinished audio and preserves prior original', len(service.list()) == 1
                    and service.get(completed[0]['id']) == completed[0]
                    and len(list(service.root.glob('*.wav'))) == 1 and not list(service.root.glob('.pending-*')))
                check('Cancel is announced distinctly', status.GetValue().startswith('Cancelled.'))
                check('native progress keeps Cancel distinct from retained completion',
                      dialog.progress_panel.active_task.snapshot().state == ProgressState.CANCELLED)
                stage = 7
                press(start)
            elif stage == 7 and len(captures) == 3 and captures[2]['seconds'] >= .1:
                stage = 8
                press(stop, wx.WXK_ESCAPE)
            wx.CallLater(25, guarded(drive))

        def owner_destroyed(event):
            if event.GetEventObject() is frame:
                frame_destroyed.set()
            event.Skip()

        def finish_owner_close():
            if (not frame_destroyed.is_set()
                    or any(thread.is_alive() for thread in frame.executor._threads)
                    or any(thread.is_alive() for thread in threading.enumerate()
                           if thread.name.startswith('carr-recording'))):
                if monotonic() >= deadline:
                    raise TimeoutError('Native recording owners did not finish closing.')
                wx.CallLater(25, guarded(finish_owner_close))
                return
            check('actual native frame destruction finishes owned workers', frame_destroyed.is_set())
            app.ExitMainLoop()

        def open_recording():
            nonlocal frame, finished
            if frame.working:
                if monotonic() >= deadline:
                    raise TimeoutError('Canonical desktop did not become ready')
                wx.CallLater(25, guarded(open_recording))
                return
            wx.CallLater(25, guarded(drive))
            result = dialog_module.show_recording(frame)
            finished = True
            if not errors:
                check('Escape closes only after active capture cleanup', stage == 8 and result is None
                    and len(cancelled_captures) == 2 and not any(item['active'] for item in captures))
                check('closing preserves exactly the verified original', len(service.list()) == 1
                    and service.get(completed[0]['id']) == completed[0]
                    and not list(service.root.glob('.pending-*')))
            frame.Close()
            wx.CallLater(25, guarded(finish_owner_close))

        try:
            frame = DesktopFrame(library)
            frame.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            wx.CallAfter(guarded(open_recording))
            app.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            dialog_module.RecordingService = original_factory
            for item in captures:
                item['cancel'].set()
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                if not frame_destroyed.is_set():
                    frame.Destroy()
            if native_handle is not None:
                native_handle.close()
            recording_module.run_process, monitor_module.DryMonitor = original_process, original_monitor
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'actual_ffmpeg_synthetic_capture': True, 'audio_runtime': audio_proof, 'physical_microphone': 'NOT_RUN',
        'native_dry_monitoring': 'PASS' if arguments.require_monitor_output else 'NOT_RUN_OFF_BY_DEFAULT',
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
