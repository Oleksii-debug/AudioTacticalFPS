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
    'carr_library.audio.book_duration', 'carr_library.audio.jobs',
    'carr_library.audio.publishing', 'carr_library.audio.ffmpeg',
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
        class Tone:
            def __init__(self):
                self.calls = []
            def cache_identity(self, voice, **options):
                return {'fixture': 'native-duration-accepted-tone-v1'}
            def synthesize(self, text, output, **options):
                self.calls.append(text)
                with wave.open(str(output), 'wb') as target:
                    target.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
                    target.writeframes(b''.join(struct.pack('<h', 2000 if index % 20 < 10 else -2000)
                                                for index in range(1600)))
                return output
        voice = Tone()
        book = StudioController(private_root=app.root, ffmpeg=binary, registry=voice,
                                trust_controller=frame.trust_controller)
        book.new('[output sample_rate=48000]\n# One\nNarrator: First measured source.\n'
                 '# Two\nNarrator: Second measured source.', title='Native measured book',
                 source_id='native-duration-synthetic', input_kind='script')
        book.configure(voice='fixture:duration')
        book.save_in_library()
        book_original = book.project_path.read_bytes()
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
        deadline = monotonic() + 240

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
                    press('new')
                    queue.service.registry = voice
                    queue.draft.add(Selection('book', book.project_id, 'Native measured delivery',
                        AudioPreset(output_format='wav', sample_rate=48000, channels=2,
                                    max_seconds=5, timeout_seconds=60, split='chapters')))
                    queue.refresh(0)
                    state['phase'] = 'book-created'
                    press('create')
                elif phase == 'book-created':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    if queue.batch_id is None:
                        raise AssertionError('Native book plan capture failed: ' + queue.status.GetValue())
                    batch = queue.service.load(queue.batch_id)
                    check('native saved audiobook keeps its reviewed chapter outputs',
                          batch.items[0].kind == 'book' and len(batch.items[0].outputs) == 2
                          and batch.items[0].preset.split == 'chapters')
                    state['phase'] = 'book-rendered'
                    press('run')
                elif phase == 'book-rendered':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    batch = queue.service.load(queue.batch_id)
                    check('native audiobook completes accepted PCM before duration planning',
                          batch.status == 'complete'
                          and queue.service.verified_receipt(batch, batch.items[0]) is not None)
                    finished['voice_calls'] = list(voice.calls)
                    queue.duration_seconds.SetValue('0.137')
                    queue.duration_format.SetStringSelection('wav')
                    check('native measured duration controls are named and enabled',
                          queue.duration_seconds.GetName() == 'Reviewed duration per audiobook part in seconds'
                          and queue.duration_format.GetName() == 'Measured audiobook part output format'
                          and queue.buttons['duration-review'].IsEnabled()
                          and not queue.buttons['duration-publish'].IsEnabled())
                    queue.duration_seconds.SetFocus()
                    state['phase'] = 'duration-focus'
                elif phase == 'duration-focus':
                    if wx.Window.FindFocus() is not queue.duration_seconds:
                        schedule()
                        return
                    check('native measured duration field receives actual keyboard focus', True)
                    state['phase'] = 'duration-reviewed'
                    press('duration-review')
                elif phase == 'duration-reviewed':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    review = queue.duration_reviewed
                    if review is None:
                        raise AssertionError('Actual duration review failed: ' + queue.status.GetValue())
                    finished['duration_review'] = review
                    root = queue.service.path(queue.batch_id) / 'duration' / review['id']
                    check('native duration preview contains every exact reviewed output name',
                          not queue.preview.IsEditable()
                          and all(row['name'] in queue.preview.GetValue() for row in review['parts']))
                    check('native duration review retains independent exact accepted PCM',
                          sha256_file(root / 'source.wav') == review['source']['sha256']
                          and (root / 'source.wav').stat().st_nlink == 1
                          and not (root / 'outputs').exists())
                    check('native measured part intervals cover the source without gaps or overlap',
                          review['parts'][0]['first_frame'] == 0
                          and review['parts'][-1]['end_frame'] == review['source']['frames']
                          and all(first['end_frame'] == second['first_frame']
                                  for first, second in zip(review['parts'], review['parts'][1:])))
                    queue.duration_seconds.SetValue('0.2')
                    check('native duration edit revokes prior publication approval',
                          queue.duration_reviewed is None and not queue.buttons['duration-publish'].IsEnabled())
                    queue.duration_seconds.SetValue('0.137')
                    state['phase'] = 'duration-approved'
                    press('duration-review')
                elif phase == 'duration-approved':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    check('native repeated review reopens the same immutable accepted source',
                          queue.duration_reviewed == finished['duration_review']
                          and voice.calls == finished['voice_calls'])
                    state['phase'] = 'duration-published'
                    press('duration-publish')
                elif phase == 'duration-published':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    review = finished['duration_review']
                    batch = queue.service.load(queue.batch_id)
                    value = queue.service.describe_duration(batch.id, batch.items[0].id, review['id'])
                    if value['status'] != 'complete':
                        raise AssertionError('Actual measured duration publication failed: ' + queue.status.GetValue())
                    root = queue.service.path(batch.id) / 'duration' / review['id']
                    from carr_library.audio.ffmpeg import wav_info
                    def pcm(path):
                        info = wav_info(path)
                        with path.open('rb') as stream:
                            stream.seek(info.data_offset)
                            return stream.read(info.data_size)
                    joined = b''.join(pcm(root / 'outputs' / row['name']) for row in value['receipt']['files'])
                    source_pcm = pcm(root / 'source.wav')
                    check('native actual lossless parts rejoin to the exact accepted PCM',
                          joined == source_pcm
                          and sum(row['pcm_frames'] for row in value['receipt']['files']) == review['source']['frames'])
                    check('native measured duration publication never resynthesises speech',
                          voice.calls == finished['voice_calls'])
                    check('native measured parts preserve the original saved audiobook',
                          book.project_path.read_bytes() == book_original and source.read_bytes() == original)
                    from carr_library.audio_batch_backup import state_paths
                    paths = {path.relative_to(app.root).as_posix() for path in state_paths(app.root)}
                    prefix = root.relative_to(app.root).as_posix()
                    check('native measured PCM and receipts enter the existing typed backup inventory',
                          {prefix + '/source.wav', prefix + '/review.json', prefix + '/outputs/receipt.json'} <= paths)
                    check('native duration worker and shared resource lease settle before reuse',
                          not queue.busy and not host.busy and queue.service.broker.snapshot()['leases'] == [])
                    finished['duration_stamps'] = {
                        path.name: (path.stat().st_mtime_ns, sha256_file(path))
                        for path in (root / 'outputs').iterdir()}
                    finished['duration_proof'] = {'status': 'complete', 'review_sha256': review['sha256'],
                        'source_sha256': review['source']['sha256'], 'source_frames': review['source']['frames'],
                        'parts': len(review['parts']), 'pcm_join_sha256': sha256(joined).hexdigest(),
                        'source_pcm_sha256': sha256(source_pcm).hexdigest(), 'resynthesised': False}
                    state['phase'] = 'duration-replayed'
                    press('duration-publish')
                elif phase == 'duration-replayed':
                    if queue.busy or host.busy:
                        schedule()
                        return
                    review = finished['duration_review']
                    root = queue.service.path(queue.batch_id) / 'duration' / review['id']
                    check('native duration replay reuses exact completed bytes and timestamps',
                          {path.name: (path.stat().st_mtime_ns, sha256_file(path))
                           for path in (root / 'outputs').iterdir()} == finished['duration_stamps']
                          and voice.calls == finished['voice_calls'])
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
                       'measured_duration': finished['duration_proof'],
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
    receipt['native_cleanup'] = 'PASS'
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
        if not settled.wait(270):
            print(json.dumps({'status': 'FAIL', 'reason': 'Native measured-duration probe exceeded 270 seconds.'}), flush=True)
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
