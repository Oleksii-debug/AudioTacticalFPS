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
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import AudioRuntime
    from carr_library.audio.models import AudioCancelled
    import carr_library.audio.process as process_module
    import carr_library.audio.recording as recording_module
    import carr_library.ui.recording_dialog as dialog_module
    from carr_library.ui.desktop_app import DesktopFrame

    checks, errors, captures, completed, cancelled_captures = [], [], [], [], []
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
                entry = 'audio/bin/' + name + '.exe'
                check('exact bundled executable: ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed executable bytes: ' + name, digest == manifest['files'][entry])

    checkout = Path(__file__).resolve().parents[1]
    for module in (process_module, recording_module, dialog_module):
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

    with TemporaryDirectory(prefix='resonance-native-recording-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic recording acceptance')
        runtime = AudioRuntime(root / 'runtime', ffmpeg=arguments.ffmpeg, ffprobe=arguments.ffprobe)
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
            if stage == 0 and refresh.IsEnabled():
                check('opening the dialog does not discover or capture', not service._devices and not captures)
                check('recording progress has a native accessible gauge',
                      any(isinstance(child, wx.Gauge) and child.GetName().startswith('Operation progress: ')
                          for child in dialog.GetChildren()))
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
                stage = 4
                press(stop)
            elif stage == 4 and saved.GetCount() == 1 and start.IsEnabled():
                check('Stop finalizes and verifies an actual retained recording', len(completed) == 1
                    and completed[0]['completion'] == 'stopped' and service.get(completed[0]['id']) == completed[0])
                check('Stop reports retained completion', status.GetValue().startswith('Saved and verified.'))
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

        def destruction_case(parent):
            nonlocal dialog
            owner = DesktopFrame(library)
            owner.Show()
            state = {'phase': 0, 'destroyed': False, 'finished': False, 'destroy_calls': 0}
            before = len(captures)
            cancelled_before = len(cancelled_captures)
            limit = monotonic() + 30
            dialog = None

            def drive_destruction():
                nonlocal dialog
                if state['finished'] or state['destroyed'] or errors:
                    return
                if monotonic() >= limit:
                    raise TimeoutError('Native recording destruction did not reach actual capture')
                if dialog is None:
                    dialog = next((window for window in wx.GetTopLevelWindows()
                        if isinstance(window, wx.Dialog) and window.GetParent() == owner
                        and window.GetTitle() == 'Local microphone recording'), None)
                if dialog is not None:
                    refresh = control('Refresh microphones')
                    selection = control('Microphone device (explicit selection required)')
                    start = control('Start recording')
                    if state['phase'] == 0 and refresh.IsEnabled():
                        state['phase'] = 1
                        press(refresh)
                    elif state['phase'] == 1 and selection.GetCount() == 1 and refresh.IsEnabled():
                        state['phase'] = 2
                        press(selection, wx.WXK_DOWN)
                    elif state['phase'] == 2 and selection.GetSelection() == 0 and start.IsEnabled():
                        state['phase'] = 3
                        press(start)
                    elif state['phase'] == 3 and len(captures) == before + 1 and captures[-1]['seconds'] >= .1:
                        check('native destruction occurs during actual FFmpeg capture', captures[-1]['active'])
                        owner_id = dialog.GetId()
                        def destroyed(event):
                            if event.GetId() == owner_id:
                                state['destroyed'] = True
                            event.Skip()
                        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                        actual_destroy = dialog.Destroy
                        def tracked_destroy():
                            state['destroy_calls'] += 1
                            return actual_destroy()
                        dialog.Destroy = tracked_destroy
                        state['phase'] = 4
                        (owner if parent else dialog).Destroy()
                        return
                wx.CallLater(25, guarded(drive_destruction))

            try:
                wx.CallLater(25, guarded(drive_destruction))
                result = dialog_module.show_recording(owner)
                state['finished'] = True
                # Record before any fixture cleanup can call Destroy.
                check('recording modal return never destroys a pending native peer twice',
                      state['destroy_calls'] == (0 if parent else 1))
                while any(item['active'] for item in captures) and monotonic() < limit:
                    app.Yield(); sleep(.01)
                # Drain the real worker's queued UI completion after native
                # destruction. The callback must observe the plain lifetime flag.
                drained = []
                wx.CallAfter(lambda: drained.append(True))
                while not drained and monotonic() < limit:
                    app.Yield(); sleep(.01)
                check('direct parent destruction returns from modal capture' if parent else
                      'direct dialog destruction returns from modal capture',
                      state['phase'] == 4 and state['destroyed'] and result is None)
                check('native destruction cancels capture and drains its late completion',
                      drained and len(cancelled_captures) == cancelled_before + 1
                      and not any(item['active'] for item in captures) and not errors)
                check('native destruction preserves the verified original and removes partial audio',
                      len(service.list()) == 1 and service.get(completed[0]['id']) == completed[0]
                      and len(list(service.root.glob('*.wav'))) == 1
                      and not list(service.root.glob('.pending-*')))
                if parent:
                    check('recording parent destruction detaches its executor and progress timer',
                          owner._native_destroyed and owner._closing and owner.cancel_event.is_set()
                          and owner.operation_progress.closed)
            finally:
                state['finished'] = True
                for item in captures[before:]:
                    item['cancel'].set()
                owner.executor.shutdown(wait=True, cancel_futures=True)
                if not owner._native_destroyed and owner and not owner.IsBeingDeleted():
                    owner.Destroy()
                app.Yield()
                print('MODAL-RECORDING-LIFETIME:', json.dumps(state), flush=True)
                dialog = None

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
                destruction_case(False)
                destruction_case(True)
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
            for item in captures:
                item['cancel'].set()
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                app.Yield()
            if native_handle is not None:
                native_handle.close()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'actual_ffmpeg_synthetic_capture': True, 'audio_runtime': audio_proof, 'physical_microphone': 'NOT_RUN',
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
