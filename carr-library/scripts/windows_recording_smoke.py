"""Installed native recording controls and real FFmpeg with synthetic input only.

Run using the installed interpreter with -I -B. Optional --ffmpeg selects the installed local executable. This never discovers or opens a microphone.
"""
import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg')
    parser.add_argument('--expected-ffmpeg-sha256')
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    import threading
    from carr_library.application import LibraryApp
    from carr_library.audio.capture_types import AudioCancelled
    from carr_library.audio.runtime import resolve_ffmpeg, verify_runtime
    import carr_library.audio.capture_process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.audio.recording_monitor as monitor_module
    import carr_library.audio.playback_devices as outputs_module
    import carr_library.ui.recording_dialog as dialog_module
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, captures, completed, cancelled_captures, segments = [], [], [], [], [], []
    paused_snapshot, paused_at = None, None
    audio_proof = {}
    deadline = monotonic() + 80
    stage, dialog, frame, finished = 0, None, None, False

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def verify_audio(binary):
        binary = Path(binary).resolve(strict=True)
        digest = recording_module._digest(binary)
        audio_proof['ffmpeg'] = {'path': str(binary), 'sha256': digest}
        if arguments.expected_ffmpeg_sha256 is not None:
            check('selected FFmpeg matches the independently supplied pinned digest',
                  digest == arguments.expected_ffmpeg_sha256)
        bundle_runtime = Path(sys.prefix).parent / 'audio-runtime'
        if binary.parent == bundle_runtime.resolve():
            verify_runtime(bundle_runtime)
            check('selected bundled FFmpeg passed the existing runtime verifier',
                  binary.name == 'ffmpeg.exe')

    checkout = Path(__file__).resolve().parents[1]
    for module in (process_module, recording_module, monitor_module, outputs_module, dialog_module):
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
    simulator = wx.UIActionSimulator()
    original_factory = dialog_module.RecordingService
    original_run = recording_module.run_process
    original_monitor_open = monitor_module.DryMonitor.open

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        ffmpeg = resolve_ffmpeg(arguments.ffmpeg)
        if ffmpeg is None:
            raise RuntimeError('Select the actual prepared FFmpeg runtime for native capture acceptance.')
        verify_audio(ffmpeg)
        service = recording_module.RecordingService(library.root, ffmpeg=ffmpeg)
        service._runtime()
        alternative = '@device_test_explicit_synthetic'
        device = recording_module.RecordingDevice(sha256(alternative.encode()).hexdigest(),
            'Synthetic FFmpeg input, no microphone', alternative)

        # Test-only source adapter. Production exposes only explicit DirectShow
        # identities and contains no lavfi selector or arbitrary capture argv.
        def discover(*, cancelled):
            if cancelled():
                raise AudioCancelled('Synthetic inventory cancelled')
            service._devices = {device.id: device}
            service._engine_hash = recording_module._digest(service.ffmpeg)
            return (device,)
        service.devices = discover
        service._input_arguments = lambda selected: [
            '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000']
        real_capture = service.capture

        def tracked_run(command, **kwargs):
            check('native capture segment has no overlapping input family',
                  not any(part['active'] for part in segments))
            part = {'path': Path(command[-1]), 'active': True}
            segments.append(part)
            try:
                return original_run(command, **kwargs)
            finally:
                part['active'] = False

        def no_unsolicited_output(*args, **kwargs):
            raise AssertionError('Default recording unexpectedly opened a monitoring output.')
        recording_module.run_process = tracked_run
        monitor_module.DryMonitor.open = no_unsolicited_output

        def capture(device_id, **kwargs):
            abort_signal = threading.Event()
            observed_cancel = kwargs['cancelled']
            kwargs['cancelled'] = lambda: abort_signal.is_set() or observed_cancel()
            item = {'seconds': 0, 'cancel': abort_signal, 'active': True}
            captures.append(item)
            observe = kwargs['progress']
            observed_state = kwargs.get('state_changed')
            def notice(value):
                item['state'] = value
                if observed_state is not None:
                    observed_state(value)
            kwargs['state_changed'] = notice
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
            nonlocal stage, dialog, paused_snapshot, paused_at
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
            pause = control('Pause recording')
            resume = control('Resume recording')
            cancel = control('Cancel unfinished recording')
            selection = control('Microphone device (explicit selection required)')
            saved = control('Saved recordings')
            status = control('Recording status')
            if stage == 0 and refresh.IsEnabled():
                check('opening the dialog does not discover or capture', not service._devices and not captures)
                check('recording progress has a native accessible gauge',
                      isinstance(control('Recording operation progress'), wx.Gauge))
                check('pause and resume are named native controls without initial authority',
                      isinstance(pause, wx.Button) and isinstance(resume, wx.Button)
                      and not pause.IsEnabled() and not resume.IsEnabled())
                check('native dry monitoring is off and has no selected output',
                      isinstance(control('Enable dry microphone monitoring'), wx.CheckBox)
                      and not control('Enable dry microphone monitoring').GetValue()
                      and control('Monitoring output (explicit selection required)').GetSelection() == wx.NOT_FOUND)
                check('recording controls have a native scrolling keyboard container',
                      isinstance(pause.GetParent(), wx.ScrolledWindow))
                stage = 1
                press(refresh)
            elif stage == 1 and selection.GetCount() == 1 and refresh.IsEnabled():
                check('single discovered input is not automatically selected', selection.GetSelection() == wx.NOT_FOUND)
                check('Start remains disabled until explicit selection', not start.IsEnabled() and not captures)
                stage = 2
                press(selection, wx.WXK_DOWN)
            elif stage == 2 and selection.GetSelection() == 0 and start.IsEnabled():
                control('Maximum recording duration in minutes').SetValue(1)
                control('Recording title').SetValue('Native synthetic recording')
                stage = 3
                press(start)
            elif stage == 3 and captures and captures[0]['seconds'] >= .1 and pause.IsEnabled():
                check('actual FFmpeg reports recorded PCM while UI remains responsive', captures[0]['active'])
                stage = 31
                press(pause)
            elif stage == 31 and resume.IsEnabled():
                state = captures[0].get('state')
                check('Pause is acknowledged only after the actual owned input family closes',
                      state is not None and state.state == 'paused' and state.frames > 0
                      and len(segments) == 1 and not segments[0]['active'] and captures[0]['active'])
                paused_snapshot = (state.frames, sha256(segments[0]['path'].read_bytes()).hexdigest())
                paused_at = monotonic()
                stage = 32
            elif stage == 32 and monotonic() - paused_at >= .15:
                check('paused time adds no PCM and opens no replacement input',
                      len(segments) == 1 and not segments[0]['active']
                      and captures[0]['state'].frames == paused_snapshot[0]
                      and sha256(segments[0]['path'].read_bytes()).hexdigest() == paused_snapshot[1])
                check('prepared recording device and monitoring choices stay frozen',
                      not selection.IsEnabled() and not control('Enable dry microphone monitoring').IsEnabled()
                      and not control('Refresh monitoring outputs').IsEnabled())
                stage = 33
                press(resume)
            elif (stage == 33 and len(segments) == 2 and segments[1]['active']
                  and captures[0]['seconds'] >= paused_snapshot[0] / captures[0]['state'].sample_rate + .1):
                check('Resume reopens one actual input segment while the first remains closed',
                      not segments[0]['active'] and captures[0]['active'])
                stage = 4
                press(stop)
            elif stage == 4 and saved.GetCount() == 1 and start.IsEnabled():
                check('Stop finalizes and verifies an actual retained recording', len(completed) == 1
                    and completed[0]['completion'] == 'stopped' and service.get(completed[0]['id']) == completed[0])
                check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
                check('native pause/resume retains two exact contiguous frame ranges',
                      completed[0]['schema'] == 'carr-recording/2' and len(completed[0]['segments']) == 2
                      and completed[0]['segments'][0]['ended_by'] == 'paused'
                      and completed[0]['segments'][1]['start_frame'] == completed[0]['segments'][0]['frames']
                      and not completed[0]['monitoring']['enabled'])
                stage = 5
                press(start)
            elif stage == 5 and len(captures) == 2 and captures[1]['seconds'] >= .1:
                stage = 6
                press(cancel)
            elif stage == 6 and start.IsEnabled() and len(cancelled_captures) == 1:
                check('Cancel removes unfinished audio and preserves prior original', len(service.list()) == 1
                    and service.get(completed[0]['id']) == completed[0]
                    and len(list(service.root.glob('*.wav'))) == 1 and not list(service.root.glob('.pending-*')))
                check('Cancel is announced distinctly', status.GetValue().startswith('Cancelled.'))
                stage = 7
                press(start)
            elif stage == 7 and len(captures) == 3 and captures[2]['seconds'] >= .1:
                stage = 8
                press(stop, wx.WXK_ESCAPE)
            wx.CallLater(25, guarded(drive))

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
                from carr_library.operations import ResourceBroker
                check('native family quiescence releases the recording device reservation',
                      ResourceBroker.for_app(library.root).snapshot()['leases'] == [])
                check('closing preserves exactly the verified original', len(service.list()) == 1
                    and service.get(completed[0]['id']) == completed[0]
                    and not list(service.root.glob('.pending-*')))
            app.ExitMainLoop()

        try:
            frame = DesktopFrame(library)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            wx.CallAfter(guarded(open_recording))
            app.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            dialog_module.RecordingService = original_factory
            recording_module.run_process = original_run
            monitor_module.DryMonitor.open = original_monitor_open
            for item in captures:
                item['cancel'].set()
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.operation_progress.close()
                frame.Destroy()
                app.Yield()
            if native_handle is not None:
                native_handle.close()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'actual_ffmpeg_synthetic_capture': True, 'actual_pause_resume': True,
        'monitoring_controls': 'EXECUTED_DEFAULT_OFF', 'physical_monitoring': 'NOT_RUN',
        'audio_runtime': audio_proof, 'physical_microphone': 'NOT_RUN',
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
