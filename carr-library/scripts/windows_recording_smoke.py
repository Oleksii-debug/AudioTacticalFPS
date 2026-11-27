"""Installed native recording controls and real FFmpeg with synthetic input only.

Run using the installed interpreter with -I -B. Optional --ffmpeg/--ffprobe
select the staged local executables. This never discovers or opens a microphone.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_media_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    parser.add_argument('--audio-evidence', type=Path, required=True)
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        if native_handle is not None:
            native_handle.close()


def _run_native(arguments):
    import ctypes
    from ctypes import wintypes
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

    binding = _acceptance_bindings()
    def verify_audio(runtime):
        checked = binding.verify_audio_evidence(runtime, arguments.audio_evidence)
        if audio_proof and audio_proof != checked:
            raise AssertionError('External audio executable identity changed during native acceptance.')
        audio_proof.update(checked)

    source_identity = binding.verify_installed_sources(process_module, recording_module, dialog_module)
    check('exact current installed recording source', bool(source_identity))

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            abort()
            return True

    app = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    key_pending = False
    pressed_keys = {}
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
            item['late_progress'] = observe
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
            nonlocal key_pending
            key_pending = True
            name = target.GetName()
            top = wx.GetTopLevelParent(target)
            focus_deadline = min(deadline, monotonic() + 5)
            def release():
                nonlocal key_pending
                try:
                    if key in pressed_keys:
                        check('native key release: ' + name, simulator.KeyUp(key))
                finally:
                    pressed_keys.pop(key, None)
                    key_pending = False
            def dispatch():
                nonlocal key_pending
                if errors or finished:
                    key_pending = False
                    return
                top.Raise()
                user32.SetForegroundWindow(top.GetHandle())
                target.SetFocus()
                if wx.Window.FindFocus() != target or user32.GetForegroundWindow() != top.GetHandle():
                    if monotonic() >= focus_deadline:
                        raise TimeoutError('Native foreground and focus did not reach ' + target.GetName())
                    wx.CallLater(25, guarded(dispatch))
                    return
                check('native focus reaches ' + target.GetName(), wx.Window.FindFocus() == target)
                check('native recording dialog owns the foreground', user32.GetForegroundWindow() == top.GetHandle())
                try:
                    check('native key dispatch: ' + name, simulator.KeyDown(key))
                    pressed_keys[key] = name
                except BaseException:
                    simulator.KeyUp(key)
                    key_pending = False
                    raise
                wx.CallLater(40, guarded(release))
            wx.CallAfter(guarded(dispatch))

        def drive():
            nonlocal stage, dialog
            if errors or finished:
                return
            if monotonic() >= deadline:
                raise TimeoutError('Native recording acceptance exceeded its deadline')
            if key_pending:
                wx.CallLater(25, guarded(drive))
                return
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
                gauges = [child for child in dialog.GetChildren() if isinstance(child, wx.Gauge)]
                check('recording progress has a native accessible gauge', len(gauges) == 1
                      and control('Recording operation progress') is gauges[0])
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

        def lifecycle_checks():
            # Observe real executor futures; do not substitute worker results,
            # native process launching or the installed recording implementation.
            original_executor = dialog_module.ThreadPoolExecutor
            original_call_after = wx.CallAfter
            futures = []
            active_case = None
            def observed_call_after(callback, *args, **kwargs):
                case = active_case
                expected = 'show_recording.<locals>.run.<locals>.finished.<locals>.apply'
                if (case is not None and getattr(callback, '__module__', None) == dialog_module.__name__
                        and getattr(callback, '__qualname__', None) == expected):
                    case['apply_posts'] += 1
                    def delivered():
                        try:
                            callback(*args, **kwargs)
                        except Exception:
                            errors.append(traceback.format_exc())
                            raise
                        finally:
                            selected = case['dialog']
                            case['apply_deliveries'].append(selected is not None and not bool(selected))
                    return original_call_after(delivered)
                return original_call_after(callback, *args, **kwargs)
            class ObservedExecutor(original_executor):
                def submit(self, *args, **kwargs):
                    future = super().submit(*args, **kwargs)
                    futures.append(future)
                    case = active_case
                    add_callback = future.add_done_callback
                    def observed_add(callback):
                        expected = 'show_recording.<locals>.run.<locals>.finished'
                        if (case is not None and getattr(callback, '__module__', None) == dialog_module.__name__
                                and getattr(callback, '__qualname__', None) == expected):
                            def completed(value):
                                try:
                                    callback(value)
                                finally:
                                    selected = case['dialog']
                                    # Only Python lifetime state is read on the
                                    # worker; native validity is checked by the UI loop.
                                    case['finished_callbacks'].append(selected is not None
                                        and selected.recording_alive is False)
                            return add_callback(completed)
                        return add_callback(callback)
                    future.add_done_callback = observed_add
                    return future
            dialog_module.ThreadPoolExecutor = ObservedExecutor
            wx.CallAfter = observed_call_after
            try:
                for destruction in ('direct', 'native-parent'):
                    parent = wx.Frame(frame, title='Owned recording lifetime fixture')
                    parent.library, parent.working, parent.report = library, False, frame.report
                    parent.Show()
                    observed = {'dialog': None, 'phase': 0, 'stopped': False, 'events': [],
                                'apply_posts': 0, 'apply_deliveries': [],
                                'finished_callbacks': [], 'finished_before_destroy': 0}
                    active_case = observed
                    start_count, future_start = len(captures), len(futures)
                    expires = monotonic() + 25
                    def dispatch(control, event_type):
                        event = wx.CommandEvent(event_type.typeId, control.GetId())
                        event.SetEventObject(control)
                        control.GetEventHandler().ProcessEvent(event)
                    def tick(observed=observed, parent=parent, start_count=start_count,
                             future_start=future_start, expires=expires, destruction=destruction):
                        if observed['stopped']:
                            return
                        try:
                            if monotonic() >= expires:
                                raise TimeoutError('Pending recording native destruction did not complete')
                            selected = observed['dialog']
                            if selected is None:
                                selected = next((window for window in wx.GetTopLevelWindows()
                                    if isinstance(window, wx.Dialog) and window.GetParent() == parent
                                    and window.GetTitle() == 'Local microphone recording'), None)
                                if selected is not None:
                                    observed['dialog'] = selected
                                    observed['id'] = selected._native_id
                                    def native_destroy(event):
                                        observed['events'].append(event.GetId())
                                        event.Skip()
                                    selected.Bind(wx.EVT_WINDOW_DESTROY, native_destroy)
                            if selected is not None:
                                refresh = wx.FindWindowByName('Refresh microphones', selected)
                                choice = wx.FindWindowByName('Microphone device (explicit selection required)', selected)
                                start = wx.FindWindowByName('Start recording', selected)
                                if observed['phase'] == 0 and refresh.IsEnabled():
                                    observed['phase'] = 1
                                    dispatch(refresh, wx.EVT_BUTTON)
                                elif observed['phase'] == 1 and refresh.IsEnabled() and choice.GetCount() == 1:
                                    choice.SetSelection(0)
                                    dispatch(choice, wx.EVT_CHOICE)
                                    observed['phase'] = 2
                                elif observed['phase'] == 2 and start.IsEnabled():
                                    observed['phase'] = 3
                                    dispatch(start, wx.EVT_BUTTON)
                                elif (observed['phase'] == 3 and len(captures) == start_count + 1
                                      and captures[-1]['seconds'] >= .1):
                                    check('real capture is pending before ' + destruction + ' destruction',
                                          captures[-1]['active'] and any(not future.done() for future in futures[future_start:]))
                                    observed['phase'] = 4
                                    observed['finished_before_destroy'] = len(observed['finished_callbacks'])
                                    if destruction == 'direct':
                                        selected.Destroy()
                                    else:
                                        # Native parent deletion bypasses the Python Destroy override.
                                        parent.DestroyChildren()
                                    return
                            wx.CallLater(20, tick)
                        except Exception:
                            errors.append(traceback.format_exc())
                            observed['stopped'] = True
                            parent.DestroyChildren()
                    try:
                        wx.CallLater(20, tick)
                        outcome = dialog_module.show_recording(parent)
                        observed['stopped'] = True
                        selected = observed['dialog']
                        check('native destruction exits recording modal: ' + destruction,
                              outcome is None and observed['phase'] == 4 and selected is not None)
                        captures[-1]['late_progress'](99.0)
                        while (bool(selected) or any(not future.done() for future in futures[future_start:])
                               or not any(observed['finished_callbacks'][observed['finished_before_destroy']:])
                               or len(observed['apply_deliveries']) < observed['apply_posts']):
                            if monotonic() >= expires:
                                raise TimeoutError('Owned capture and actual completion delivery did not finish after native destruction')
                            app.Yield()
                            sleep(.01)
                        check('actual recording completion releases the detached view: ' + destruction,
                              any(observed['finished_callbacks'][observed['finished_before_destroy']:])
                              and len(observed['apply_deliveries']) == observed['apply_posts'])
                        captures[-1]['late_progress'](100.0)
                        app.Yield()
                        check('cached native identity receives real destruction: ' + destruction,
                              observed['events'].count(observed['id']) == 1 and not bool(selected))
                        check('recording cancellation and dead-view latch: ' + destruction,
                              selected.recording_alive is False and selected.recording_cancelled.is_set()
                              and not captures[-1]['active'])
                        check('late recording callbacks keep the original and no staged capture: ' + destruction,
                              len(service.list()) == 1 and service.get(completed[0]['id']) == completed[0]
                              and not list(service.root.glob('.pending-*')))
                        from carr_library.operations import ResourceBroker
                        check('native capture lease released: ' + destruction,
                              'microphone:' + device.id not in ResourceBroker.for_app(library.root).snapshot()['exclusive_devices'])
                        if errors:
                            raise AssertionError('\n'.join(errors))
                    finally:
                        observed['stopped'] = True
                        for item in captures[start_count:]:
                            item['cancel'].set()
                        if parent:
                            parent.DestroyChildren()
                            parent.Destroy()
                        app.Yield()
            finally:
                active_case = None
                wx.CallAfter = original_call_after
                dialog_module.ThreadPoolExecutor = original_executor

        def open_recording():
            nonlocal frame, finished
            if frame.working:
                if monotonic() >= deadline:
                    raise TimeoutError('Canonical desktop did not become ready')
                wx.CallLater(25, guarded(open_recording))
                return
            wx.CallLater(25, guarded(drive))
            result = dialog_module.show_recording(frame)
            # Escape can finish the modal before the scheduled key-up tick.
            # Release it while this real event loop is still owned by the probe.
            for key, name in tuple(pressed_keys.items()):
                try:
                    check('native key release after modal close: ' + name, simulator.KeyUp(key))
                finally:
                    pressed_keys.pop(key, None)
            finished = True
            if not errors:
                check('Escape closes only after active capture cleanup', stage == 8 and result is None
                    and len(cancelled_captures) == 2 and not any(item['active'] for item in captures))
                check('closing preserves exactly the verified original', len(service.list()) == 1
                    and service.get(completed[0]['id']) == completed[0]
                    and not list(service.root.glob('.pending-*')))
            if not errors:
                lifecycle_checks()
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
            for key in tuple(pressed_keys):
                simulator.KeyUp(key)
                pressed_keys.pop(key, None)
            for item in captures:
                item['cancel'].set()
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.Destroy()
                app.Yield()
    verify_audio(runtime)
    print(json.dumps({'status': 'PASS', 'checks': checks, 'actual_native_keys': True,
        'source_identity': source_identity,
        'actual_ffmpeg_synthetic_capture': True, 'direct_parent_destruction': 'PASS',
        'late_recording_progress_and_completion': 'PASS', 'audio_runtime': audio_proof, 'physical_microphone': 'NOT_RUN',
        'nvda': 'NOT_RUN', 'audio_listening': 'NOT_RUN'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
