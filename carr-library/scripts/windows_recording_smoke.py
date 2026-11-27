"""Installed native recording controls and real FFmpeg with synthetic input only.

Run using the installed interpreter with -I -B and an explicitly selected test
FFmpeg executable. This never discovers or opens a microphone. Hardware output
monitoring and listening remain separate acceptance tasks.
"""
import argparse
import faulthandler
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from time import monotonic
import traceback
import wave


def run(arguments, report):
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.recording import AudioCancelled
    from carr_library.audio.render import tool_path
    import carr_library.audio.process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.ui.recording_dialog as dialog_module
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, captures, completed, cancelled_captures = report['checks'], [], [], [], []
    audio_proof = report['audio_runtime']
    deadline = monotonic() + 80
    stage, dialog, frame, finished = 0, None, None, False
    dialog_id, dialog_destroyed = None, False
    pause_until, paused_seconds, paused_bytes = None, None, None
    native_segments = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def verify_audio(runtime):
        binary = Path(runtime.ffmpeg).resolve(strict=True)
        with binary.open('rb') as stream:
            digest = file_digest(stream, 'sha256').hexdigest()
        audio_proof['ffmpeg'] = {'path': str(binary), 'sha256': digest}
        check('explicit local FFmpeg executable', binary == Path(arguments.ffmpeg).resolve(strict=True))
        check('selected executable hash retained', len(digest) == 64)

    checkout = Path(__file__).resolve().parents[1]
    for module in (process_module, recording_module, dialog_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('exact module source: ' + module.__name__,
              (not actual.is_relative_to(checkout) if arguments.expect_installed else actual == expected)
              and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            abort()
            return True

    app = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    original_factory = dialog_module.RecordingService
    original_process = recording_module.run_process

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        runtime = SimpleNamespace(ffmpeg=tool_path('ffmpeg', arguments.ffmpeg))
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
        production_input = service._input_arguments
        def synthetic_input(selected):
            # Keep production options and ordering. Only the actual hardware
            # source is substituted; newer FFmpeg input/output option changes
            # must fail this synthetic native gate as well as the device gate.
            arguments = production_input(selected)
            arguments[arguments.index('-f') + 1] = 'lavfi'
            arguments[arguments.index('-i') + 1] = 'sine=frequency=440:sample_rate=24000'
            return ['-re', *arguments]
        service._input_arguments = synthetic_input
        real_capture = service.capture

        def real_segment(command, **kwargs):
            item = {'path': Path(command[-1]), 'active': True}
            assert not any(part['active'] for part in native_segments)
            native_segments.append(item)
            try:
                result = original_process(command, **kwargs)
                with wave.open(str(item['path']), 'rb') as reader:
                    count = reader.getnframes()
                    assert 0 <= count <= 60 * reader.getframerate()
                    digest, remaining = sha256(), count
                    while remaining:
                        frames = min(remaining, 32768)
                        raw = reader.readframes(frames)
                        assert len(raw) == frames * reader.getnchannels() * reader.getsampwidth()
                        digest.update(raw)
                        remaining -= frames
                    item.update(frames=count, pcm_sha256=digest.hexdigest())
                return result
            finally:
                item['active'] = False
        recording_module.run_process = real_segment

        def capture(device_id, **kwargs):
            item = {'seconds': 0, 'active': True}
            captures.append(item)
            observe = kwargs['progress']
            state_observer = kwargs['state_changed']
            def state(value):
                item['state'] = value.state
                item['frames'] = value.frames
                state_observer(value)
            kwargs['state_changed'] = state
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
        def selected_service(selected_root, **kwargs):
            assert Path(selected_root) == library.root
            return service
        dialog_module.RecordingService = selected_service

        def control(name):
            found = wx.FindWindowByName(name, dialog)
            if found is None:
                raise AssertionError('Missing accessible native control: ' + name)
            return found

        def abort():
            if frame is not None:
                frame.cancel_event.set()
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
            nonlocal stage, dialog, dialog_id, dialog_destroyed, pause_until, paused_seconds, paused_bytes
            if errors or finished:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native recording acceptance exceeded its deadline')
            if dialog is None:
                dialog = next((window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.GetTitle() == 'Local microphone recording'), None)
                if dialog is not None:
                    dialog_id = dialog.GetId()
                    def destroyed(event):
                        nonlocal dialog_destroyed
                        if event.GetId() == dialog_id:
                            dialog_destroyed = True
                        event.Skip()
                    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
            if dialog is None:
                wx.CallLater(25, guarded(drive))
                return
            refresh = control('Refresh microphones')
            start = control('Start recording')
            retake = control('Record retake of selected recording')
            stop = control('Stop and save recording')
            pause = control('Pause recording')
            resume = control('Resume recording')
            cancel = control('Cancel unfinished recording')
            selection = control('Microphone device (explicit selection required)')
            saved = control('Saved recordings')
            status = control('Recording status')
            monitoring = control('Dry monitoring during capture (off by default)')
            monitor_output = control('Monitoring output (explicit selection required)')
            if stage == 0 and refresh.IsEnabled():
                check('opening the dialog does not discover or capture', not service._devices and not captures)
                check('recording progress has a native accessible gauge',
                      isinstance(control('Recording operation progress'), wx.Gauge))
                check('native retake waits for a selected original', not retake.IsEnabled())
                check('Pause and Resume never start capture from idle',
                      not pause.IsEnabled() and not resume.IsEnabled())
                check('native monitoring is unchecked by default', isinstance(monitoring, wx.CheckBox)
                      and not monitoring.GetValue())
                check('native monitoring requires an explicit output selection',
                      monitor_output.GetSelection() == wx.NOT_FOUND and not monitor_output.IsEnabled())
                check('native output discovery stays an explicit separate action',
                      control('Refresh monitoring outputs').IsEnabled())
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
                stage = 20
                press(monitoring)
            elif stage == 20 and monitoring.GetValue():
                check('native enabling monitoring blocks Start without a selected output',
                      monitor_output.IsEnabled() and not start.IsEnabled() and not captures)
                stage = 21
                press(monitoring)
            elif stage == 21 and not monitoring.GetValue() and start.IsEnabled():
                check('native disabling monitoring restores ordinary capture without fallback',
                      not monitor_output.IsEnabled() and monitor_output.GetSelection() == wx.NOT_FOUND)
                stage = 3
                press(start)
            elif stage == 3 and captures and captures[0]['seconds'] >= .1 and stop.IsEnabled():
                check('actual FFmpeg reports recorded PCM while UI remains responsive', captures[0]['active'])
                check('recording holds shared quiet even during progress queries',
                      any(hold.recording and not hold.closed
                          for hold in frame.operation_progress._audio_holds))
                check('progress query stays keyboard accessible during capture',
                      control('Current operation progress').IsEnabled())
                stage = 31
                press(control('Current operation progress'))
            elif stage == 31 and wx.Window.FindFocus() == status:
                check('explicit query is readable inside the actual modal dialog',
                      bool(status.GetValue()) and status.GetValue() == frame.status.GetValue())
                stage = 32
                press(pause)
            elif stage == 32 and resume.IsEnabled() and captures[0].get('state') == 'paused':
                check('Pause acknowledges the real FFmpeg tree exit before Resume is enabled',
                      len(native_segments) == 1 and not native_segments[0]['active']
                      and native_segments[0]['frames'] > 0)
                check('Pause retains the same worker, quiet hold and shared device lease',
                      frame.working and captures[0]['active']
                      and any(hold.recording and not hold.closed for hold in frame.operation_progress._audio_holds))
                from carr_library.operations import ResourceBroker
                leases = ResourceBroker.for_app(library.root).snapshot()['leases']
                check('paused input remains reserved by the shared resource broker',
                      len(leases) == 1 and leases[0]['request']['devices'] == ['microphone:' + device.id])
                paused_seconds = captures[0]['seconds']
                paused_bytes = sha256(native_segments[0]['path'].read_bytes()).hexdigest()
                pause_until = monotonic() + .2
                stage = 33
            elif stage == 33 and monotonic() >= pause_until:
                check('paused wall time never appends PCM or opens another input',
                      len(native_segments) == 1 and not native_segments[0]['active']
                      and captures[0]['state'] == 'paused'
                      and sha256(native_segments[0]['path'].read_bytes()).hexdigest() == paused_bytes)
                stage = 34
                press(resume)
            elif stage == 34 and len(native_segments) == 2 and native_segments[1]['active'] and captures[0]['seconds'] >= paused_seconds + .1:
                check('explicit Resume uses the same take and the next real owned segment',
                      len(captures) == 1 and captures[0]['state'] == 'recording'
                      and frame.working and not resume.IsEnabled())
                stage = 4
                press(stop)
            elif stage == 4 and saved.GetCount() == 1 and start.IsEnabled():
                check('Stop finalizes and verifies an actual retained recording', len(completed) == 1
                    and completed[0]['completion'] == 'stopped' and service.get(completed[0]['id']) == completed[0])
                check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
                ranges = completed[0]['segments']
                check('saved pause joins match both actual PCM segments exactly',
                      completed[0]['schema'] == 'carr-recording/4' and len(ranges) == 2
                      and [row['ended_by'] for row in ranges] == ['paused', 'stopped']
                      and all(row['frames'] == part['frames']
                              and row['pcm_sha256'] == part['pcm_sha256']
                              for row, part in zip(ranges, native_segments))
                      and completed[0]['audio']['frames'] == sum(row['frames'] for row in ranges))
                check('native retake targets the displayed saved original', retake.IsEnabled()
                      and saved.GetSelection() == 0)
                stage = 5
                press(retake)
            elif stage == 5 and len(captures) == 2 and captures[1]['seconds'] >= .1 and stop.IsEnabled():
                stage = 6
                press(stop)
            elif stage == 6 and saved.GetCount() == 2 and start.IsEnabled():
                check('retake preserves the previous verified original', len(completed) == 2
                    and completed[1]['retake']['previous_id'] == completed[0]['id']
                    and service.get(completed[0]['id']) == completed[0]
                    and service.get(completed[1]['id']) == completed[1])
                stage = 7
                press(start)
            elif stage == 7 and len(captures) == 3 and captures[2]['seconds'] >= .1:
                stage = 8
                press(cancel)
            elif stage == 8 and start.IsEnabled() and len(cancelled_captures) == 1:
                check('Cancel removes unfinished audio and preserves both originals', len(service.list()) == 2
                    and all(service.get(item['id']) == item for item in completed)
                    and len(list(service.root.glob('*.wav'))) == 2 and not list(service.root.glob('.pending-*')))
                check('Cancel is announced distinctly', status.GetValue().startswith('Cancelled.'))
                stage = 9
                press(start)
            elif stage == 9 and len(captures) == 4 and captures[3]['seconds'] >= .1:
                stage = 10
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
            app.Yield()
            finished = True
            if not errors:
                check('native recording owner window actually destroyed', dialog_destroyed
                      and dialog_id is not None and wx.FindWindowById(dialog_id) is None)
                check('Escape closes only after active capture cleanup', stage == 10 and result is None
                    and len(cancelled_captures) == 2 and not any(item['active'] for item in captures))
                check('closing preserves exactly the verified originals', len(service.list()) == 2
                    and all(service.get(item['id']) == item for item in completed)
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
            recording_module.run_process = original_process
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                app.Yield()
            if native_handle is not None:
                native_handle.close()
    report.update(status='PASS', actual_native_keys=True, actual_ffmpeg_synthetic_capture=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    report = {'status': 'FAILED', 'checks': [], 'audio_runtime': {},
              'actual_native_keys': False, 'actual_ffmpeg_synthetic_capture': False,
              'physical_microphone': 'NOT_RUN', 'human_listening': 'NOT_RUN',
              'nvda': 'NOT_RUN', 'live_monitoring': 'NOT_RUN',
              'independent_deadline_seconds': 110}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    faulthandler.dump_traceback_later(110, exit=True)
    try:
        run(args, report)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    main()
