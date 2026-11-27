"""Explicit native recording controls adapted from the reviewed PR149 dialog.

Uses the current Studio controller/player and shared ProgressView. Capture
owns its native cleanup and resource lease even after this modal window closes.
"""
from concurrent.futures import ThreadPoolExecutor
import threading

from ..audio.capture_types import AudioCancelled, AudioCleanupPending
from ..audio.recording import RecordingOptions, RecordingService, unverified_recording_files
from ..progress import ProgressTracker
from .progress_view import ProgressView


def show_recording(studio):
    import wx
    if studio.busy or studio.closed:
        raise ValueError('Finish or cancel the current Studio operation before recording.')
    controller = studio.controller
    if controller.output_dir is None:
        raise ValueError('Choose an existing audio output folder before opening microphone recording.')
    service = RecordingService(controller.private_root, controller.output_dir, ffmpeg=controller.ffmpeg)
    dialog = wx.Dialog(studio.dialog, title='Local microphone recording', size=(860, 820),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetMinSize((680, 600))
    area = wx.GetClientDisplayRect()
    dialog.SetSize((min(860, area.width), min(820, area.height)))
    dialog.CentreOnParent()
    outer = wx.BoxSizer(wx.VERTICAL)
    description = wx.StaticText(dialog, label='Refresh microphones and select one explicitly, then Start. '
        'Stop saves a verified recording; Cancel discards unfinished audio. '
        'Playback and progress sounds stay silent during capture. No microphone opens automatically.')
    description.Wrap(810)
    outer.Add(description, 0, wx.EXPAND | wx.ALL, 8)
    body = wx.ScrolledWindow(dialog, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
    body.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, factory, **kwargs):
        layout.Add(wx.StaticText(body, label=label), 0, wx.LEFT | wx.TOP, 6)
        control = factory(body, **kwargs)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        return control

    microphone = field('Microphone device (explicit selection required)', wx.Choice, choices=[])
    title = field('Recording title', wx.TextCtrl, value='Microphone recording')
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=180, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice,
                        choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    saved = field('Saved recordings', wx.Choice, choices=[])
    details = field('Recording result and retained input path', wx.TextCtrl, value='',
                    style=wx.TE_MULTILINE | wx.TE_READONLY)
    details.SetMinSize((-1, 95))
    body.SetSizer(layout)
    body.FitInside()
    outer.Add(body, 1, wx.EXPAND | wx.ALL, 4)
    progress = ProgressView(dialog, settings_root=controller.private_root,
                            label='Recording operation progress', wx_module=wx)
    outer.Add(progress.panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
    status = wx.TextCtrl(dialog, value='No microphone selected.',
                        style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Recording status')
    status.SetMinSize((-1, 65))
    outer.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for row in ((('refresh', 'Refresh microphones'), ('start', 'Start recording'),
                 ('stop', 'Stop and save recording'), ('cancel', 'Cancel unfinished recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('use', 'Add selected recording to Studio playlist'), ('close', 'Close'))):
        box = wx.WrapSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(dialog, label=label)
            button.SetName(label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 3)
        outer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 3)
    dialog.SetSizer(outer)
    dialog.SetEscapeId(buttons['close'].GetId())
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-recording')
    cancelled, stopped = threading.Event(), threading.Event()
    busy, alive, closing = False, True, False
    devices, records, selected_result = (), (), None
    generation, active_kind = 0, ''
    cleanup_owner = cleanup_retry = None

    def report(message):
        if alive:
            status.ChangeValue(message)

    def controls():
        for control in (microphone, title, minutes, sample_rate, channels, saved):
            control.Enable(not busy)
        buttons['refresh'].Enable(not busy)
        buttons['start'].Enable(not busy and 0 <= microphone.GetSelection() < len(devices))
        buttons['stop'].Enable(busy and (active_kind == 'capture'
            or cleanup_owner is not None and not cleanup_owner.quiescent))
        buttons['cancel'].Enable(busy)
        for key in ('preview', 'use'):
            buttons[key].Enable(not busy and 0 <= saved.GetSelection() < len(records))
        buttons['stop_preview'].Enable(not busy)
        body.FitInside()

    def display_record(event=None):
        index = saved.GetSelection()
        if 0 <= index < len(records):
            value, audio = records[index], records[index]['audio']
            details.ChangeValue(f"{value['title']}\n{audio['duration_seconds']:.2f} seconds; "
                f"{audio['sample_rate']} Hz; {audio['channels']} channel(s).\n{value['path']}\n"
                'Listen and Add verify these retained bytes. Add does not start playback or change script text.')
        else:
            details.ChangeValue('')
        controls()

    def show_records(values, select_id=None):
        nonlocal records
        records = tuple(values)
        saved.SetItems([f"{row['title']} ({row['created'][:19]})" for row in records])
        saved.SetSelection(next((index for index, row in enumerate(records)
                                 if row['id'] == select_id), wx.NOT_FOUND))
        display_record()

    def poll_cleanup():
        nonlocal busy
        if not alive or cleanup_owner is None:
            return
        if cleanup_owner.quiescent:
            # The core reservation releases resources only after native silence.
            # A failed admission-lock release still blocks a later Start.
            busy = False
            report('Microphone shutdown confirmed. Unfinished capture was not published. '
                   'Start will recheck that all recording resources were released.')
            controls()
            if closing:
                dialog.EndModal(wx.ID_CANCEL)
        else:
            wx.CallLater(100, poll_cleanup)

    def run(operation, complete, phase, *, kind='read', measured=False):
        nonlocal busy, active_kind
        if busy:
            return
        busy, active_kind = True, kind
        controls()
        tracker = ProgressTracker(phase)
        try:
            progress.track(tracker, audible=False)
        except Exception:
            pass  # Display failure cannot reclassify a verified capture.

        def measured_progress(seconds):
            tracker.update(phase=f'Recorded {seconds:.1f} seconds; Stop saves, Cancel discards')
        report(phase)
        try:
            future = executor.submit(operation, measured_progress) if measured else executor.submit(operation)
        except Exception as exc:
            busy = False
            tracker.finish('failed', phase='The recording operation could not start.')
            report(str(exc))
            controls()
            return

        def finished(future):
            def apply():
                nonlocal busy, cleanup_owner
                if not alive:
                    return
                busy = False
                try:
                    value = future.result()
                except AudioCleanupPending as exc:
                    cleanup_owner = exc.cleanup_owner
                    busy = not cleanup_owner.quiescent
                    tracker.finish('failed', phase='Microphone cleanup has not confirmed silence.')
                    report('Microphone cleanup is still pending. Playback, tones and speech remain blocked. '
                           'Stop retries cleanup; Close retains its owner in the background.')
                    if busy:
                        wx.CallLater(100, poll_cleanup)
                except (AudioCancelled, InterruptedError):
                    tracker.finish('cancelled', phase='Recording cancelled; unfinished audio discarded.')
                    report('Cancelled. Previously saved recordings remain available.')
                except Exception as exc:
                    tracker.finish('failed', phase='The recording operation failed.')
                    report(str(exc))
                else:
                    # The producer result, including late-cancel publication, is
                    # authoritative before any observer/control update.
                    tracker.finish('completed', verified=True, phase='The operation result was verified.')
                    try:
                        if not closing:
                            complete(value)
                    except Exception as exc:
                        report('The operation completed, but its display or selection could not finish: ' + str(exc))
                try:
                    progress.refresh(force=True)
                    controls()
                except Exception:
                    pass
                if closing and not busy:
                    dialog.EndModal(wx.ID_CANCEL)
            if alive:
                wx.CallAfter(apply)
        future.add_done_callback(finished)

    def refresh(event=None):
        nonlocal devices, generation
        if busy:
            return
        generation += 1
        devices = ()
        microphone.SetItems([])
        cancelled.clear()
        def complete(values):
            nonlocal devices
            devices = tuple(values)
            microphone.SetItems([item.name + ' (' + item.id[:8] + ')' for item in devices])
            microphone.SetSelection(wx.NOT_FOUND)
            report('Choose a microphone explicitly, then Start. Discovery did not record audio.')
        run(lambda: service.devices(cancelled=cancelled.is_set), complete, 'Listing local microphones')

    def start(event=None):
        nonlocal generation
        if busy:
            return
        index = microphone.GetSelection()
        if not 0 <= index < len(devices):
            report('Select a microphone before Start.')
            return
        try:
            options = RecordingOptions(max_seconds=minutes.GetValue() * 60,
                sample_rate=int(sample_rate.GetStringSelection()), channels=channels.GetSelection() + 1,
                max_bytes=2 * 1024**3)
            requested_title, device_id = title.GetValue(), devices[index].id
            if not studio._stop_for_project_change():
                report('Playback did not confirm silence. No microphone was opened.')
                return
            cancelled.clear()
            stopped.clear()
            generation += 1
            def operation(notify):
                return service.capture(device_id, title=requested_title, options=options,
                    cancelled=cancelled.is_set, stop_requested=stopped.is_set, progress=notify)
            def complete(value):
                show_records((*records, value), value['id'])
                report('Saved and verified. The recording is retained in this audio project. '
                       'Choose Listen or Add selected recording to Studio playlist.')
            run(operation, complete, 'Waiting for audio silence and shared resources, then recording',
                kind='capture', measured=True)
        except Exception as exc:
            report(str(exc))

    def preview(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        selected_generation = generation
        def complete(value):
            def play():
                if alive and not busy and selected_generation == generation:
                    studio.playback.play(value['path'])
                    studio.refresh_playback()
                    report('Playing the verified saved recording.')
            studio._with_quiet_output(play)
        run(lambda: service.get(records[index]['id'], cancelled=cancelled.is_set),
            complete, 'Verifying saved recording')

    def use(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        def complete(value):
            nonlocal selected_result
            if not studio._stop_for_project_change():
                raise RuntimeError('Stop saved-recording playback before selecting this input.')
            selected_result = value
            dialog.EndModal(wx.ID_OK)
        run(lambda: service.get(records[index]['id'], cancelled=cancelled.is_set),
            complete, 'Verifying selected recording')

    def stop_playback(event=None):
        nonlocal generation
        generation += 1
        if studio._stop_for_project_change():
            report('Saved-recording playback stopped.')
        else:
            report('Playback has not confirmed silence. Capture remains unavailable.')

    def stop_recording(event=None):
        nonlocal cleanup_retry
        stopped.set()
        if cleanup_owner is not None and not cleanup_owner.quiescent:
            if cleanup_retry is None or not cleanup_retry.is_alive():
                try:
                    cleanup_retry = threading.Thread(target=cleanup_owner.close,
                        name='recording-cleanup-retry', daemon=True)
                    cleanup_retry.start()
                except (OSError, RuntimeError) as exc:
                    report('Cleanup retry could not start. Its owner remains retained. ' + str(exc))
                    return
            report('Retrying microphone cleanup. All application output stays silent until confirmed.')
        elif busy:
            report('Stop requested. Waiting for WAV finalization and owned process cleanup.')

    def cancel_recording(event=None):
        cancelled.set()
        if busy:
            report('Cancellation requested. Waiting for the microphone and owned process family to stop.')

    def close(event=None):
        nonlocal closing, generation
        generation += 1
        if busy:
            closing = True
            cancelled.set()
            if cleanup_owner is not None and not cleanup_owner.quiescent:
                studio._message('Microphone cleanup remains pending. Its native owner, resources and audio admission stay reserved.')
                dialog.EndModal(wx.ID_CANCEL)
                return
            report('Closing the microphone and discarding unfinished audio before leaving.')
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()
        elif studio._stop_for_project_change():
            dialog.EndModal(wx.ID_CANCEL)
        elif event is not None and hasattr(event, 'Veto'):
            event.Veto()

    for key, callback in (('refresh', refresh), ('start', start), ('stop', stop_recording),
            ('cancel', cancel_recording), ('preview', preview), ('stop_preview', stop_playback),
            ('use', use), ('close', close)):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    microphone.Bind(wx.EVT_CHOICE, lambda event: controls())
    saved.Bind(wx.EVT_CHOICE, display_record)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        controls()
        cancelled.clear()
        def loaded_records(value):
            values, unverified = value
            show_records(values)
            if unverified:
                report(f'{len(unverified)} retained WAV original(s) lack a verified receipt. '
                       f'They remain unselected in {service.assets} for explicit recovery.')
            else:
                report('Saved receipts loaded. Refresh microphones and select a device explicitly before Start.')
        run(lambda: (service.list(cancelled=cancelled.is_set),
                     unverified_recording_files(service.project_root)),
            loaded_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancelled.set()
        generation += 1
        executor.shutdown(wait=False, cancel_futures=True)
        try:
            studio._stop_for_project_change()
        finally:
            try:
                progress.close()
            finally:
                dialog.Destroy()
    return selected_result
