"""Actual installed wx + local FFmpeg media workflow on a synthetic PCM source.

No microphone, private recording, network, physical playback or NVDA is used.
The final close test blocks only a public presentation callback before work;
actual process ownership and complete audio bytes have separate strict suites.
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback
import wave


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.media_conversion import MediaConversionService
    from carr_library.audio.runtime import resolve_ffmpeg
    from carr_library.operations.broker import ResourceBroker
    from carr_library.operations.models import ResourceLimits
    from carr_library.ui import media_conversion as ui

    checks, errors, timers = [], [], []
    settled = Event()
    def watchdog():
        if not settled.wait(120):
            print('FAIL: native media conversion exceeded the watchdog', flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False
    app = App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    check('media UI comes from the installed package',
          'site-packages' in [part.casefold() for part in Path(ui.__file__).parts])

    with TemporaryDirectory(prefix='carr-native-media-') as temporary:
        root = Path(temporary).resolve()
        original = root / 'Незмінний оригінал Ω.wav'
        with wave.open(str(original), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(struct.pack('<h', 1000) * 6000)
        original_bytes = original.read_bytes()
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic native media')
        broker = ResourceBroker.for_app(library.root, limits=ResourceLimits(cpu_slots=2,
            memory_bytes=1024**3, disk_bytes=128 * 1024**2, min_free_disk_bytes=0))
        service = MediaConversionService(library.root, ffmpeg=resolve_ffmpeg(), broker=broker)
        frame = wx.Frame(None, title='Synthetic native media workflow')
        frame.library, frame.cancel_event = library, Event()
        frame.working = frame._closing = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-media')
        release, waiting, acknowledged = Event(), Event(), Event()
        native_inspect = service.inspect
        calls = [0]
        def inspected(paths, **options):
            calls[0] += 1
            if calls[0] == 2:
                waiting.set()
                while not release.wait(.01):
                    if options['cancelled']():
                        acknowledged.set()
                        raise InterruptedError('Synthetic presentation stop acknowledged.')
                if options['cancelled']():
                    acknowledged.set()
                    raise InterruptedError('Synthetic presentation stop acknowledged.')
            return native_inspect(paths, **options)
        service.inspect = inspected
        def run_job(title, operation, *, done, failed, **options):
            if frame.working:
                return False
            frame.working = True
            frame.cancel_event.clear()
            future = executor.submit(operation)
            def finished(completed):
                def apply():
                    frame.working = False
                    try:
                        result = completed.result()
                    except Exception as error:
                        failed(error)
                    else:
                        done(result)
                wx.CallAfter(apply)
            future.add_done_callback(finished)
        frame.run_job = run_job
        app.SetTopWindow(frame)
        frame.Show()
        state = {'phase': 0, 'active': True, 'view': None, 'complete': False}
        deadline = monotonic() + 105
        def press(view, name):
            button = view.buttons[name]
            check('enabled named media button: ' + name,
                  isinstance(button, wx.Button) and button.IsEnabled() and bool(button.GetName()))
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native media command dispatch: ' + name, button.GetEventHandler().ProcessEvent(event))
        def schedule():
            timers.append(wx.CallLater(25, poll))
        def poll():
            if not state['active']:
                return
            view = state['view']
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native media flow did not finish within its bound.')
                if state['phase'] == 4:
                    if not waiting.is_set():
                        schedule()
                        return
                    press(view, 'close')
                    check('closing retains native controls until the active operation settles',
                          view.session.close_requested and view.session.busy
                          and not view.session.closed and view.dialog.IsModal())
                    release.set()
                    state['complete'] = True
                    return
                if view.session.busy:
                    schedule()
                    return
                if state['phase'] == 0:
                    check('owned real media modal',
                          view.dialog.GetParent() is frame and view.dialog.IsModal()
                          and view.dialog.GetName() == 'Prepared media conversion workspace')
                    check('all eight output settings have distinct accessible names',
                          len(view.fields) == 8 and
                          {control.GetName() for control in view.fields.values()} == set(ui._LABELS.values()))
                    check('actual typed audio choices and bitrate spin',
                          all(isinstance(view.fields[name], wx.Choice) for name in ui._CHOICES)
                          and isinstance(view.fields['bitrate_kbps'], wx.SpinCtrl)
                          and isinstance(view.timeout, wx.SpinCtrlDouble))
                    keys = [button.GetLabel().split('&', 1)[1][0].casefold()
                            for button in view.buttons.values() if '&' in button.GetLabel()]
                    check('distinct native media action mnemonics', len(keys) == len(set(keys)))
                    view.add_paths((original,))
                    view.title.ChangeValue('Конвертація Україна Ω')
                    view.disk.SetValue(32)
                    state['phase'] = 1
                    press(view, 'inspect')
                elif state['phase'] == 1:
                    check('actual FFprobe inspection retains Unicode original',
                          len(view.setup.inspections) == 1 and
                          Path(view.setup.inspections[0].path).name == original.name and
                          view.setup.inspections[0].streams[0].sample_rate == 24000)
                    check('inspection does not silently prepare or start', not service.jobs())
                    view.fields['format'].SetSelection(ui._CHOICES['format'].index('flac'))
                    view.fields['pcm_bits'].SetSelection(1)
                    view.fields['sample_rate'].SetSelection(ui._CHOICES['sample_rate'].index(24000))
                    view.fields['channels'].SetSelection(0)
                    view.fields['title'].ChangeValue('Результат Україна Ω')
                    press(view, 'apply')
                    check('native choices become the exact typed 24-bit FLAC request',
                          view.setup.frozen_choices()[0].preset.format == 'flac'
                          and view.setup.frozen_choices()[0].preset.pcm_bits == 24
                          and view.setup.frozen_choices()[0].preset.title == 'Результат Україна Ω')
                    state['phase'] = 2
                    press(view, 'prepare')
                elif state['phase'] == 2:
                    check('Prepare retains sources and freezes all output controls without conversion',
                          view.current_job is not None and service.jobs()[0]['status'] == 'prepared'
                          and all(not control.IsEnabled() for control in view.fields.values())
                          and not view.buttons['prepare'].IsEnabled() and view.buttons['start'].IsEnabled())
                    check('prepared title and source preserve exact Unicode',
                          view.title.GetValue() == 'Конвертація Україна Ω'
                          and original.name in view.source_list.GetString(0))
                    state['phase'] = 3
                    press(view, 'start')
                elif state['phase'] == 3:
                    check('actual FFmpeg conversion and complete decode accepted one exact output',
                          view.summary is not None and view.summary['status'] == 'complete'
                          and view.summary['completed'] == 1
                          and view.summary['outputs'][0]['decoded_frames'] == 6000
                          and view.summary['outputs'][0]['pcm_bits'] == 24
                          and view.buttons['export'].IsEnabled())
                    check('original bytes stay unchanged and native resources are quiescent',
                          original.read_bytes() == original_bytes and not broker.snapshot()['leases'])
                    identity = view.current_job
                    press(view, 'new')
                    check('New unlocks controls without changing the saved complete job',
                          view.current_job is None and all(control.IsEnabled() for control in view.fields.values())
                          and service.verify(identity)['completed'] == 1)
                    view.add_paths((original,))
                    state['phase'] = 4
                    press(view, 'inspect')
                schedule()
            except Exception:
                errors.append(traceback.format_exc())
                state['active'] = False
                release.set()
                view.session.cancel.set()
                if view.dialog.IsModal():
                    view.dialog.EndModal(wx.ID_CANCEL)
        def ready(view):
            state['view'] = view
            schedule()
        try:
            ui.show_media_conversion(frame, service=service, on_ready=ready)
            check('close completed only after operation cancellation was acknowledged',
                  state['complete'] and acknowledged.is_set())
            if errors:
                raise AssertionError('\n'.join(errors))
        finally:
            state['active'] = False
            release.set()
            frame.cancel_event.set()
            if state['view'] is not None:
                state['view'].session.closed = True
                state['view'].session.cancel.set()
            for timer in timers:
                timer.Stop()
            executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            app.ProcessPendingEvents()
            loop.ProcessIdle()
            settled.set()
    return dict(status='PASS', checks=checks, actual_ffmpeg='PASS', physical_audio='NOT_RUN',
                nvda='NOT_RUN', scope='Actual installed Windows media controls, synthetic PCM conversion and lifecycle.')


def main():
    parser = ArgumentParser()
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if not options.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows native mode is required.'}))
        return 77
    try:
        result = run_native()
    except Exception:
        result = {'status': 'FAIL', 'error': traceback.format_exc(),
                  'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    if options.output is not None:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
