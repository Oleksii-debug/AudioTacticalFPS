"""Installed native recording controls and real FFmpeg with synthetic input only.

Run using the installed interpreter with -I -B and an explicitly selected pinned
audio directory. Optional --ffmpeg/--ffprobe must identify those exact binaries.
This never discovers or opens a microphone.
"""
import argparse
import ctypes
from ctypes import wintypes
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



class _DriverCallbacks:
    """Own only this probe's wx timers and reject queued callbacks from old modals."""

    def __init__(self, later, on_error):
        self._later = later
        self._on_error = on_error
        self._generation = 0
        self._pending = {}

    def schedule(self, milliseconds, callback):
        generation = self._generation
        owner = {}

        def invoke():
            timer = owner.get('timer')
            if timer is not None:
                self._pending.pop(id(timer), None)
            if generation != self._generation:
                return
            try:
                callback()
            except Exception:
                self._on_error(traceback.format_exc())

        timer = self._later(milliseconds, invoke)
        owner['timer'] = timer
        self._pending[id(timer)] = timer
        return timer

    def reset(self):
        # Invalidate before stopping: an already queued native notification must
        # not gain authority over the next dialog even if Stop cannot recall it.
        self._generation += 1
        failures = []
        for identity, timer in tuple(self._pending.items()):
            try:
                timer.Stop()
            except Exception as exc:
                failures.append(exc)
            else:
                self._pending.pop(identity, None)
        if failures:
            raise RuntimeError('Native recording driver timer cleanup failed.') from failures[0]


def _live_recording_dialog(windows, parent, retired_handles, is_window):
    """Ignore same-title windows that are closing, foreign or no longer modal."""
    matches = []
    for window in windows:
        if not window or window.GetTitle() != 'Local microphone recording':
            continue
        handle = window.GetHandle()
        if (window.GetParent() is parent and window.IsModal()
                and handle not in retired_handles and is_window(handle)):
            matches.append(window)
    if len(matches) > 1:
        raise RuntimeError('More than one owned recording modal is active.')
    return matches[0] if matches else None

def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioRuntime
    from carr_library.audio.models import AudioCancelled
    import carr_library.audio.recording_monitor as monitor_module
    import carr_library.speech._wave_output as wave_output_module
    import carr_library.operations.broker as broker_module
    import carr_library.operations._locks as locks_module
    import carr_library.audio.cleanup as cleanup_module
    import carr_library.speech.playback_devices as devices_module
    import carr_library.ui.worker_lifetime as lifetime_module
    import carr_library.audio.process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.ui.recording_dialog as dialog_module
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, captures, completed, cancelled_captures = [], [], [], [], []
    audio_proof = {}
    deadline = monotonic() + 180
    def watchdog_failed():
        print('FAIL: native recording probe or owned cleanup exceeded 210 seconds', file=sys.stderr, flush=True)
        os._exit(1)
    watchdog = threading.Timer(210, watchdog_failed)
    watchdog.daemon = True
    watchdog.start()
    stage, dialog, frame, finished = 0, None, None, False
    dialog_handle = dialog_loop = None
    retired_handles = set()
    host_handle = host_executor = host_cancel = None
    abort_pending = False

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
            audio_proof[name] = {'path': str(binary), 'sha256': digest,
                                 'binding': 'explicit selected directory and pinned lock'}
            selected = selected_audio / 'bin' / (name + '.exe')
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            check('explicit selected audio executable: ' + name, binary.samefile(selected))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            if (bundle and manifest.get('bundled_audio')
                    and selected_audio == (bundle / 'audio').resolve(strict=True)):
                entry = 'audio/bin/' + name + '.exe'
                check('exact bundled executable: ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed executable bytes: ' + name, digest == manifest['files'][entry])

    checkout = Path(__file__).resolve().parents[1]
    for module in (process_module, recording_module, monitor_module, wave_output_module,
                   broker_module, locks_module, cleanup_module, devices_module,
                   lifetime_module, dialog_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and sha256(actual.read_bytes()).digest() == sha256(expected.read_bytes()).digest())

    def failed(detail):
        errors.append(detail)
        print('FAIL: native recording phase ' + str(stage) + '\n' + detail,
              file=sys.stderr, flush=True)
        try:
            abort()
        except Exception:
            cleanup_detail = traceback.format_exc()
            errors.append(cleanup_detail)
            print('FAIL: native recording failure cleanup\n' + cleanup_detail,
                  file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failed(traceback.format_exc())
            return True

    app = ProbeApp(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    driver = _DriverCallbacks(wx.CallLater, failed)
    original_factory = dialog_module.RecordingService
    original_chooser = wx.FileDialog
    original_runner = recording_module.run_process
    original_monitor = monitor_module.DryMonitor
    segments, monitors = [], []
    paused_at = None
    paused_frames = None

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        exported = root / 'native-export.wav'

        class ExportChooser:
            """The destination is a test fixture; the export button is native."""
            def __init__(self, parent, message, **options):
                check('export requests a new WAV without overwrite permission',
                      message == 'Export verified recording to a new WAV'
                      and options['style'] == wx.FD_SAVE)
            def ShowModal(self):
                return wx.ID_OK
            def GetPath(self):
                return str(exported)
            def Destroy(self):
                pass

        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        runtime = AudioRuntime(root / 'runtime',
            ffmpeg=arguments.ffmpeg or str(selected_audio / 'bin' / 'ffmpeg.exe'),
            ffprobe=arguments.ffprobe or str(selected_audio / 'bin' / 'ffprobe.exe'))
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
        def tracked_run(command, **options):
            item = {'active': True, 'path': Path(command[-1]), 'pcm': None}
            segments.append(item)
            try:
                result = original_runner(command, **options)
                check('captured segment stays within native fixture byte bound', item['path'].stat().st_size < 20 * 1024**2)
                with wave.open(str(item['path']), 'rb') as reader:
                    item['pcm'] = reader.readframes(reader.getnframes())
                return result
            finally:
                item['active'] = False
        recording_module.run_process = tracked_run

        class TrackedMonitor(original_monitor):
            def __init__(self):
                super().__init__()
                monitors.append(self)
        monitor_module.DryMonitor = TrackedMonitor
        monitoring_inventory = {'started': False, 'finished': False, 'count': None, 'error': None}
        real_monitoring_outputs = service.monitoring_outputs

        def monitoring_outputs(*, cancelled):
            monitoring_inventory['started'] = True
            try:
                values = real_monitoring_outputs(cancelled=cancelled)
                monitoring_inventory['count'] = sum(value.selectable for value in values)
                print('ACTUAL_MONITORING_OUTPUTS: ' + str(monitoring_inventory['count']), flush=True)
                return values
            except Exception as exc:
                monitoring_inventory['error'] = type(exc).__name__
                raise
            finally:
                monitoring_inventory['finished'] = True

        service.monitoring_outputs = monitoring_outputs
        real_capture = service.capture

        def capture(device_id, **kwargs):
            item = {'seconds': 0, 'cancel': threading.Event(), 'active': True}
            original_cancelled = kwargs['cancelled']
            kwargs['cancelled'] = lambda: item['cancel'].is_set() or original_cancelled()
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

        def active_dialog():
            return (dialog is not None and bool(dialog) and dialog_handle is not None
                and user32.IsWindow(dialog_handle) and dialog.GetHandle() == dialog_handle
                and dialog.IsModal() and dialog_loop is not None and dialog_loop.IsRunning())

        def control(name):
            if not active_dialog():
                raise RuntimeError('The current recording modal no longer owns native input.')
            found = wx.FindWindowByName(name, dialog)
            if found is None:
                raise AssertionError('Missing accessible native control: ' + name)
            return found

        def abort():
            nonlocal abort_pending
            for item in captures:
                item['cancel'].set()
            driver.reset()
            if abort_pending:
                return
            abort_pending = True
            def close_owned():
                nonlocal abort_pending
                abort_pending = False
                try:
                    if active_dialog():
                        # The real Close handler waits for capture/monitor cleanup.
                        dialog.Close()
                    else:
                        loop = wx.EventLoopBase.GetActive()
                        if loop is not None and loop.IsMain() and loop.IsRunning():
                            loop.ScheduleExit(1)
                        elif monotonic() < deadline:
                            abort_pending = True
                            wx.CallLater(25, close_owned)
                except Exception:
                    detail = traceback.format_exc()
                    errors.append(detail)
                    print('FAIL: native recording owned close\n' + detail,
                          file=sys.stderr, flush=True)
            wx.CallAfter(close_owned)

        def guarded(callback):
            def invoke():
                try:
                    callback()
                except Exception:
                    failed(traceback.format_exc())
            return invoke

        def press(target, key=wx.WXK_SPACE):
            current_dialog, current_handle = dialog, dialog_handle
            target.SetFocus()
            def dispatch():
                if (dialog is not current_dialog or dialog_handle != current_handle
                        or not active_dialog()):
                    raise RuntimeError('Queued native input belongs to an expired recording modal.')
                check('native focus reaches ' + target.GetName(), wx.Window.FindFocus() == target)
                check('native key dispatch: ' + target.GetName(), simulator.Char(key))
            driver.schedule(25, dispatch)

        def drive():
            nonlocal stage, dialog, dialog_handle, dialog_loop, paused_at, paused_frames
            if errors or finished:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native recording acceptance exceeded its deadline')
            if dialog is None:
                current = wx.EventLoopBase.GetActive()
                if current is not None and not current.IsMain() and current.IsRunning():
                    dialog = _live_recording_dialog(
                        (window for window in wx.GetTopLevelWindows() if isinstance(window, wx.Dialog)),
                        frame, retired_handles, user32.IsWindow)
                    if dialog is not None:
                        dialog_handle, dialog_loop = dialog.GetHandle(), current
                        print('NATIVE_RECORDING_MODAL: ' + str(stage) + ':' + str(dialog_handle), flush=True)
            if dialog is None:
                driver.schedule(25, drive)
                return
            if stage >= 100:
                drive_monitor()
                driver.schedule(25, drive)
                return
            refresh = control('Refresh microphones')
            start = control('Start recording')
            stop = control('Stop and save recording')
            cancel = control('Cancel unfinished recording')
            selection = control('Microphone device (explicit selection required)')
            saved = control('Saved recordings')
            status = control('Recording status')
            if stage == 0 and refresh.IsEnabled():
                check('opening the dialog does not discover or capture', not service._devices and not captures)
                check('standalone recording does not expose an unused selection action',
                      not control('Use selected recording').IsShown())
                gauge = dialog.progress_panel.gauge
                check('recording progress has a native accessible gauge',
                      isinstance(gauge, wx.Gauge)
                      and gauge.GetName().startswith('Recording operation progress: ')
                      and gauge.GetRange() == 100)
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
            elif stage == 3 and captures and captures[0]['seconds'] >= .1 and stop.IsEnabled():
                check('actual FFmpeg reports recorded PCM while UI remains responsive', captures[0]['active'])
                stage = 30
                press(control('Pause microphone recording'))
            elif stage == 30 and dialog.capture_state == 'paused':
                check('Pause acknowledges actual capture exit before enabling Resume',
                      segments and not any(part['active'] for part in segments)
                      and control('Resume microphone recording').IsEnabled())
                check('paused recording retains its real shared admission lease',
                      len(service._broker_instance.snapshot()['leases']) == 1
                      and recording_module._CAPTURE.locked())
                paused_at, paused_frames = monotonic(), segments[-1]['path'].read_bytes()
                stage = 31
            elif stage == 31 and monotonic() - paused_at >= .15:
                check('paused source does not acquire synthetic frames',
                      segments[-1]['path'].read_bytes() == paused_frames
                      and not any(part['active'] for part in segments))
                stage = 32
                press(control('Resume microphone recording'))
            elif stage == 32 and len(segments) >= 2 and captures[0]['seconds'] >= .7:
                stage = 4
                press(stop)
            elif stage == 4 and saved.GetCount() == 1 and start.IsEnabled():
                check('Stop finalizes and verifies an actual retained recording', len(completed) == 1
                    and completed[0]['completion'] == 'stopped' and service.get(completed[0]['id']) == completed[0])
                with wave.open(completed[0]['path'], 'rb') as joined:
                    retained_pcm = joined.readframes(joined.getnframes())
                check('Pause Resume receipt binds unchanged contiguous PCM ranges',
                      completed[0]['schema'] == 'carr-recording/2'
                      and len(completed[0]['segments']) == 2
                      and retained_pcm == b''.join(part['pcm'] for part in segments[:2]))
                check('default capture opens no monitoring output', monitors == [])
                check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
                wx.FileDialog = ExportChooser
                stage = 41
                press(control('Export verified recording to new WAV'))
            elif stage == 41 and exported.is_file() and start.IsEnabled():
                check('native export copies exact verified WAV without changing retained input',
                      exported.read_bytes() == Path(completed[0]['path']).read_bytes()
                      and sha256(exported.read_bytes()).hexdigest() == completed[0]['audio']['sha256']
                      and service.get(completed[0]['id']) == completed[0])
                check('native export reports its completed destination',
                      status.GetValue().startswith('Verified WAV exported to '))
                wx.FileDialog = original_chooser
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
            driver.schedule(25, drive)

        def drive_monitor():
            nonlocal stage, paused_at, paused_frames
            refresh, start = control('Refresh microphones'), control('Start recording')
            selection = control('Microphone device (explicit selection required)')
            output = control('Monitoring output (explicit selection required)')
            enable = control('Enable dry input monitoring')
            status = control('Recording status')
            if stage == 100 and refresh.IsEnabled():
                check('new dialog starts with dry monitoring disabled', not enable.GetValue() and not monitors)
                stage = 101
                press(refresh)
            elif stage == 101 and selection.GetCount() == 1 and refresh.IsEnabled():
                stage = 102
                press(selection, wx.WXK_DOWN)
            elif stage == 102 and selection.GetSelection() == 0:
                stage = 103
                press(control('Refresh monitoring outputs'))
            elif stage == 103 and monitoring_inventory['finished'] and refresh.IsEnabled():
                if monitoring_inventory['error'] is not None:
                    raise RuntimeError('Actual Windows monitoring enumeration failed: '
                                       + monitoring_inventory['error'] + '. ' + status.GetValue())
                if output.GetCount() == 0:
                    raise RuntimeError('Actual Windows monitoring output is required; no output was enumerated. '
                                       'Native monitoring remains unqualified. ' + status.GetValue())
                check('monitor enumeration does not open output or select a fallback',
                      not monitors and output.GetSelection() == wx.NOT_FOUND)
                stage = 104
                press(enable)
            elif stage == 104 and enable.GetValue():
                check('monitoring Start requires explicit selected output', not start.IsEnabled())
                stage = 105
                press(output, wx.WXK_DOWN)
            elif stage == 105 and output.GetSelection() >= 0 and start.IsEnabled():
                control('Maximum recording duration in minutes').SetValue(1)
                control('Recorded 16-bit PCM WAV sample rate').SetSelection(2)
                control('Recording title').SetValue('Native dry monitoring synthetic take')
                stage = 106
                press(start)
            elif stage == 106 and len(captures) >= 4 and captures[3]['seconds'] >= .2:
                check('explicit dry monitoring owns an actual selected waveOut handle',
                      monitors and monitors[-1].handle.value and monitors[-1].device is not None)
                stage = 107
                press(control('Pause microphone recording'))
            elif stage == 107 and dialog.capture_state == 'paused':
                check('monitored Pause waits for producer and output buffer quiescence',
                      not any(part['active'] for part in segments)
                      and all(not value.handle.value and not value.pending for value in monitors))
                check('monitored Pause keeps resource reservation until final save',
                      len(service._broker_instance.snapshot()['leases']) == 1)
                paused_at, paused_frames = monotonic(), segments[-1]['path'].read_bytes()
                stage = 108
            elif stage == 108 and monotonic() - paused_at >= .15:
                check('dry monitored pause omits wall-time frames', segments[-1]['path'].read_bytes() == paused_frames)
                stage = 109
                press(control('Resume microphone recording'))
            elif stage == 109 and len(monitors) >= 2 and captures[3]['seconds'] >= .8:
                stage = 110
                press(control('Stop and save recording'))
            elif stage == 110 and len(completed) == 2 and start.IsEnabled():
                result = service.get(completed[1]['id'])
                check('actual dry monitor records bounded submission evidence without changing saved PCM',
                      result == completed[1] and result['monitoring']['enabled']
                      and result['monitoring']['submitted_frames'] > 0
                      and result['monitoring']['submitted_frames'] + result['monitoring']['dropped_frames'] <= result['audio']['frames']
                      and len(result['segments']) == 2)
                check('monitored Stop releases all owned buffers and shared leases',
                      all(not value.handle.value and not value.pending for value in monitors)
                      and service._broker_instance.snapshot()['leases'] == [])
                stage = 111
                press(control('Close'))

        def open_recording():
            nonlocal frame, dialog, dialog_handle, dialog_loop, stage
            if frame.working:
                if monotonic() >= deadline:
                    raise TimeoutError('Canonical desktop did not become ready')
                driver.schedule(25, open_recording)
                return
            driver.reset()
            driver.schedule(25, drive)
            try:
                result = dialog_module.show_recording(frame)
            finally:
                driver.reset()
            if errors:
                abort()
                return
            check('Escape closes only after active capture cleanup', stage == 8 and result is None
                and len(cancelled_captures) == 2 and not any(item['active'] for item in captures))
            check('closing preserves exactly the verified original', len(service.list()) == 1
                and service.get(completed[0]['id']) == completed[0]
                and not list(service.root.glob('.pending-*')))
            previous_handle = dialog_handle
            if previous_handle is None:
                raise AssertionError('The first native recording modal was not captured.')
            retired_handles.add(previous_handle)
            dialog, dialog_handle, dialog_loop = None, None, None

            def open_monitoring():
                nonlocal finished, stage
                if monotonic() >= deadline:
                    raise TimeoutError('The first recording modal did not release its native window.')
                if user32.IsWindow(previous_handle):
                    driver.schedule(25, open_monitoring)
                    return
                check('second recording modal starts after first HWND and callbacks retire',
                      not driver._pending and not any(item['active'] for item in captures))
                # A future native window may legitimately reuse this integer;
                # retirement is established by observed destruction, not an
                # eternal handle blacklist.
                retired_handles.discard(previous_handle)
                stage = 100
                driver.reset()
                driver.schedule(25, drive)
                try:
                    dialog_module.show_recording(frame)
                finally:
                    driver.reset()
                if not errors:
                    check('second explicit monitoring dialog closes after completed cleanup',
                          stage == 111 and len(service.list()) == 2 and len(completed) == 2
                          and not any(item['active'] for item in captures)
                          and all(not value.handle.value and not value.pending for value in monitors))
                finished = True
                loop = wx.EventLoopBase.GetActive()
                if loop is not None and loop.IsMain() and loop.IsRunning():
                    loop.ScheduleExit(1 if errors else 0)
                elif errors:
                    abort()

            # Return to the actual main loop before opening another same-title
            # modal. Native Destroy and old timer notifications must drain first.
            driver.schedule(25, open_monitoring)

        try:
            frame = DesktopFrame(library)
            host_handle, host_executor, host_cancel = frame.GetHandle(), frame.executor, frame.cancel_event
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            wx.CallAfter(guarded(open_recording))
            app.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            try:
                try:
                    driver.reset()
                finally:
                    for item in captures:
                        item['cancel'].set()
                    if host_executor is not None:
                        host_cancel.set()
                        try:
                            if user32.IsWindow(host_handle):
                                frame.Close(force=True)
                            cleanup_deadline = monotonic() + 20
                            while user32.IsWindow(host_handle):
                                if monotonic() >= cleanup_deadline:
                                    raise TimeoutError('Actual recording host Close did not release its HWND.')
                                app.Yield()
                        finally:
                            # Even a native Close/wait failure cannot bypass the
                            # retained worker and its registered finalizers.
                            host_executor.shutdown(wait=True, cancel_futures=True)
                        check('recording host Close drains its registered native owners',
                              not host_executor.cleanup_failures
                              and not any(item['active'] for item in captures)
                              and all(not value.handle.value and not value.pending for value in monitors)
                              and (service._broker_instance is None
                                   or service._broker_instance.snapshot()['leases'] == []))
            finally:
                # Keep the exact synthetic input/runner/monitor fixtures owned
                # until cancellation and executor drain have been attempted.
                dialog_module.RecordingService = original_factory
                recording_module.run_process = original_runner
                monitor_module.DryMonitor = original_monitor
                wx.FileDialog = original_chooser
                if native_handle is not None:
                    native_handle.close()
    watchdog.cancel()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'actual_ffmpeg_synthetic_capture': True, 'verified_wav_export': True,
        'actual_pause_resume': True, 'actual_selected_waveout_monitor': True,
        'retained_frame_ranges': True, 'owned_cleanup_and_shared_admission': True,
        'sequential_modal_ownership': True, 'actual_monitoring_outputs': monitoring_inventory['count'],
        'file_destination': 'CONTROLLED_FIXTURE', 'audio_runtime': audio_proof, 'physical_microphone': 'NOT_RUN',
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
