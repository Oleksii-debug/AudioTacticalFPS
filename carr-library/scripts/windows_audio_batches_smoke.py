"""Actual installed Windows queue controls and pinned local FFmpeg conversion.

File pickers, physical playback, speech quality and NVDA are separate checks.
This synthetic probe retains no user media or executable payload in its report.
"""
from argparse import ArgumentParser
from dataclasses import asdict
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback
import wave


MODULES = (
    'carr_library.audio.batch_models', 'carr_library.audio.media_conversion',
    'carr_library.audio.batches', 'carr_library.audio_batch_backup',
    'carr_library.ui.audio_batches', 'carr_library.ui.audio_studio',
    'carr_library.audio.recording_process', 'carr_library.security.admission',
)


def installed_identity():
    if not sys.flags.isolated:
        raise ValueError('Use the isolated installed Python with -I.')
    prefix = Path(sys.prefix).resolve(strict=True)
    result = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('A native audio queue module was loaded outside the installed application.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    native = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.batch_models import AudioPreset, Selection
    from carr_library.audio.jobs import sha256_file
    from carr_library.security.controller import TrustController
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    from carr_library.ui.audio_batches import AudioBatchDialog

    origins = installed_identity()
    binary = Path(os.environ['CARR_FFMPEG']).resolve(strict=True)
    checks, errors = [], []
    finished = {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)
    class Application(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = Application(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows native queue controls', wx.Platform == '__WXMSW__')
    frame = wx.Frame(None, title='Synthetic local audio queue acceptance')
    frame.trust_controller = TrustController()
    gui.SetTopWindow(frame)
    frame.Show()
    with TemporaryDirectory(prefix='carr-native-audio-batch-') as folder:
        folder = Path(folder)
        app = LibraryApp(folder / 'library', trust_controller=frame.trust_controller)
        controller = StudioController(private_root=app.root, ffmpeg=binary,
                                      trust_controller=frame.trust_controller)
        controller.new('Saved synthetic Studio text.', title='Native queue host')
        controller.save_in_library()
        host = AudioStudioDialog(frame, controller)
        host.dialog.Show()
        queue = AudioBatchDialog(host)
        source = folder / 'synthetic-original.wav'
        with wave.open(str(source), 'wb') as output:
            output.setparams((1, 2, 8000, 2000, 'NONE', 'not compressed'))
            output.writeframes(b''.join(struct.pack('<h', 3000 if index % 20 < 10 else -3000)
                                       for index in range(2000)))
        original = source.read_bytes()
        queue.draft.add(Selection('audio', str(source), 'Original title',
                                 AudioPreset(output_format='wav', sample_rate=8000, channels=1,
                                             max_seconds=5, timeout_seconds=30)))
        queue.refresh(0)
        state = {'active': True, 'phase': 'shown'}
        timers = []
        deadline = monotonic() + 150

        def schedule():
            if state['active']:
                timers.append(wx.CallLater(25, poll))

        def press(key):
            button = queue.buttons[key]
            if not isinstance(button, wx.Button) or not button.IsEnabled():
                raise AssertionError('Expected an enabled native queue action: ' + key)
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            if not button.GetEventHandler().ProcessEvent(event):
                raise AssertionError('The native queue action was not handled: ' + key)

        def poll():
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('The native queue workflow exceeded its explicit deadline.')
                if not queue.dialog.IsShown() or not queue.dialog.IsModal():
                    schedule()
                    return
                phase = state['phase']
                if phase == 'shown':
                    check('owned named native queue modal', queue.dialog.GetParent() is host.dialog
                          and queue.dialog.GetName() == 'Local audio batches and audiobooks')
                    check('native source actions and durable queue controls are named',
                          set(queue.buttons) == {'runtime', 'audio', 'video', 'books', 'open', 'new',
                              'apply', 'up', 'down', 'remove', 'create', 'run', 'resume', 'retry', 'pause', 'cancel', 'export',
                              'duration-review', 'duration-open', 'duration-publish', 'duration-export'}
                          and all(button.GetName() == button.GetLabel() for button in queue.buttons.values()))
                    check('native per-item format rate channel and split selectors',
                          all(isinstance(queue.fields[name], wx.Choice)
                              for name in ('output_format', 'sample_rate', 'channels', 'split'))
                          and queue.fields['audio_stream'].GetName() == 'Explicit audio stream index')
                    check('native exact preview is read only',
                          not queue.preview.IsEditable() and 'Original title' in queue.preview.GetValue())
                    check('queue retains the exact configured host authority',
                          queue.service.trust_controller is controller.trust_controller is frame.trust_controller)
                    queue.fields['title'].SetValue('Доставка 🙂')
                    queue.fields['title'].SetFocus()
                    state['phase'] = 'focus'
                elif phase == 'focus':
                    if wx.Window.FindFocus() is not queue.fields['title']:
                        schedule()
                        return
                    check('native filename field receives actual keyboard focus', True)
                    queue.fields['max_seconds'].SetValue('nan')
                    press('apply')
                    check('invalid numeric preset cannot replace the selected item',
                          queue.draft.items[0].title == 'Original title' and 'bounded' in queue.status.GetValue())
                    queue.fields['max_seconds'].SetValue('5')
                    queue.fields['output_format'].SetStringSelection('flac')
                    press('apply')
                    check('native apply retains explicit numeric preset and Unicode title',
                          queue.draft.items[0].title == 'Доставка 🙂'
                          and queue.draft.items[0].preset.output_format == 'flac'
                          and queue.draft.items[0].preset.channels == 1)
                    state['phase'] = 'created'
                    press('create')
                elif phase == 'created':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    if queue.batch_id is None:
                        raise AssertionError('Queue creation failed: ' + queue.status.GetValue())
                    batch = queue.service.load(queue.batch_id)
                    check('actual retained plan is queued before any codec work',
                          batch.status == 'queued' and batch.items[0].attempt == 0)
                    captured = queue.service.path(batch.id) / 'sources' / batch.items[0].source.file
                    check('actual retained original is an independent exact copy',
                          captured.read_bytes() == original and captured.stat().st_nlink == 1)
                    check('saved native preview contains exact collision-safe output names',
                          batch.items[0].outputs[0] in queue.preview.GetValue()
                          and batch.items[0].id in batch.items[0].outputs[0])
                    check('saved per-item delivery settings cannot be silently edited',
                          not queue.buttons['apply'].IsEnabled() and not queue.fields['title'].IsEnabled())
                    press('pause')
                    request = queue.service.describe(batch.id)
                    check('native pause is recorded as an unacknowledged durable request',
                          request['control']['action'] == 'pause' and request['control_acknowledged'] is False)
                    state['phase'] = 'paused'
                    press('run')
                elif phase == 'paused':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    value = queue.service.describe(queue.batch_id)
                    check('owned worker acknowledges the paused checkpoint',
                          value['status'] == 'paused' and value['control_acknowledged'] is True)
                    check('paused first item has no unverified completed output',
                          value['items'][0]['attempt'] == 0
                          and not Path(value['items'][0]['outputs'][0]).exists())
                    state['phase'] = 'complete'
                    press('resume')
                elif phase == 'complete':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    batch = queue.service.load(queue.batch_id)
                    if batch.status != 'complete':
                        raise AssertionError('Actual native queue conversion failed: ' + queue.preview.GetValue()[:4000])
                    receipt = queue.service.verified_receipt(batch, batch.items[0], durable=True)
                    row = receipt['files'][0]
                    path = Path(queue.service.describe(batch.id)['items'][0]['outputs'][0])
                    check('actual pinned FFmpeg output was decoded before completion',
                          row['sample_rate'] == 8000 and row['channels'] == 1 and row['frames'] == 2000)
                    check('actual encoded output matches its source-bound receipt',
                          row['sha256'] == sha256_file(path)
                          and receipt['source_sha256'] == sha256(original).hexdigest()
                          and receipt['runtime_sha256'] == batch.runtime.sha256)
                    check('immutable selected original remains byte identical', source.read_bytes() == original)
                    check('native completed queue is visible in accessible status',
                          'complete' in queue.status.GetValue().lower() and 'complete' in queue.preview.GetValue())
                    check('shared resource lease is released after codec settlement',
                          queue.service.broker.snapshot()['leases'] == [])
                    finished.update(batch_id=batch.id, item_id=batch.items[0].id, output=row,
                                    runtime=asdict(batch.runtime), stamp=path.stat().st_mtime_ns)
                    state['phase'] = 'replayed'
                    press('run')
                elif phase == 'replayed':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    batch = queue.service.load(queue.batch_id)
                    path = Path(queue.service.describe(batch.id)['items'][0]['outputs'][0])
                    check('completed replay preserves actual output bytes and timestamp',
                          batch.status == 'complete' and batch.items[0].attempt == 1
                          and sha256_file(path) == finished['output']['sha256']
                          and path.stat().st_mtime_ns == finished['stamp'])
                    check('queue and existing Studio worker settle before native close', not queue.busy and not host.busy)
                    state['active'] = False
                    queue.close()
                    return
                schedule()
            except BaseException:
                errors.append(traceback.format_exc())
                state['active'] = False
                host.cancelled.set()
                queue.close()

        schedule()
        try:
            queue.dialog.ShowModal()
            if errors:
                raise AssertionError(errors[-1])
            check('native queue modal closes without a late callback owner', queue.closed and queue.token is None)
            check('installed implementation remains unchanged through native execution',
                  installed_identity() == origins)
            check('no native queue callback failed', not errors)
            receipt = {'status': 'PASS', 'checks': checks, 'installed_mode': True, 'isolated': True,
                       'module_origins': origins, 'native_controls': 'PASS',
                       'actual_ffmpeg': finished['runtime'], 'verified_output': finished['output'],
                       'file_picker': 'NOT_RUN', 'physical_audio': 'NOT_RUN',
                       'speech_quality': 'NOT_CLAIMED', 'nvda': 'NOT_RUN'}
        finally:
            state['active'] = False
            for timer in timers:
                timer.Stop()
            host.cancelled.set()
            # Join the existing owner before a failed probe can remove its
            # synthetic inputs/stages. The watchdog records failure if native
            # quiescence cannot settle within this standalone probe's bound.
            host.executor.shutdown(wait=True, cancel_futures=True)
            host._job_token = object()
            host.busy = queue.busy = False
            if not queue.closed:
                queue.close()
            queue.dialog.Destroy()
            host.close()
            host.dialog.Destroy()
            frame.Destroy()
            if native is not None:
                native.close()
    # Completion includes owner/window cleanup and removal of synthetic inputs.
    # A valid source-style installed environment has no optional DLL handle.
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or not args.native:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit installed native Windows execution is required.'}))
        return 77
    settled = Event()
    def watchdog():
        if not settled.wait(180):
            print(json.dumps({'status': 'FAIL', 'reason': 'Native queue probe exceeded 180 seconds.'}), flush=True)
            os._exit(1)
    Thread(target=watchdog, daemon=True).start()
    try:
        return run_native()
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-16000:], flush=True)
        print(json.dumps({'status': 'FAIL', 'error': type(error).__name__ + ': ' + str(error)[:4000],
                          'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        settled.set()


if __name__ == '__main__':
    raise SystemExit(main())
