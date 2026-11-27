"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from ..audio.models import AudioCancelled, AudioCleanupPending
from ..audio.recording import RecordingOptions, RecordingService, unverified_recording_files
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from .operation_progress import OperationProgress


def show_recording(frame, *, on_selected=None):
    import wx
    if getattr(frame, 'working', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    dialog = wx.Dialog(frame, title='Local microphone recording', size=(820, 740))
    dialog.library = library
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Refresh devices and select a microphone, then choose Start. '
        'Stop saves a verified recording; Cancel discards an unfinished recording. '
        'No microphone opens automatically. Recording uses the selected Windows device locally.'),
        0, wx.EXPAND | wx.ALL, 8)

    def field(label, factory, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 8)
        control = factory(dialog, **kwargs)
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        return control

    microphone = field('Microphone device (explicit selection required)', wx.Choice, choices=[])
    title = field('Recording title', wx.TextCtrl, value='Microphone recording')
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=180, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice, choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    saved = field('Saved recordings', wx.Choice, choices=[])
    details = field('Recording result and retained input path', wx.TextCtrl,
                    value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Recording status', wx.TextCtrl, value='No microphone selected.', style=wx.TE_READONLY)
    gauge = wx.Gauge(dialog, range=100)
    gauge.SetName('Recording operation progress')
    progress_label = wx.StaticText(dialog, label='Ready')
    layout.Add(gauge, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(progress_label, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for row in ((('refresh', 'Refresh microphones'), ('start', 'Start recording'),
                 ('stop', 'Stop and save recording'), ('cancel', 'Cancel unfinished recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('use', 'Use selected recording'), ('close', 'Close'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(dialog, label=label)
            button.SetName(label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    progress = OperationProgress(dialog, gauge, progress_label, wx_module=wx,
                                 accessible_name='Recording operation progress')
    parent_progress = getattr(frame, 'operation_progress', None)
    controllers = tuple(item for item in (progress, parent_progress) if item is not None)
    service = RecordingService(library.root)
    player = WavePlayer()
    for controller in controllers:
        controller.register_player(player)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-recording')
    cancelled, stopped = threading.Event(), threading.Event()
    busy, alive, closing, devices, records, holds, selected_result = False, True, False, (), (), [], None
    generation = 0
    active_future = None
    cleanup_owner = cleanup_future = cleanup_retry = None
    hold_guard = threading.RLock()

    def controls():
        for control in (microphone, title, minutes, sample_rate, channels, saved):
            control.Enable(not busy)
        buttons['refresh'].Enable(not busy)
        buttons['start'].Enable(not busy and 0 <= microphone.GetSelection() < len(devices))
        buttons['stop'].Enable(busy and bool(holds))
        buttons['cancel'].Enable(busy)
        for key in ('preview', 'use'):
            buttons[key].Enable(not busy and 0 <= saved.GetSelection() < len(records))
        buttons['stop_preview'].Enable(not busy)

    def release_holds():
        nonlocal holds
        with hold_guard:
            current, holds = holds, []
        for hold in current:
            try:
                hold.close()
            except Exception as exc:
                progress.reporting_error = 'Progress audio release needs attention: ' + str(exc)

    def release_completed(future):
        nonlocal cleanup_owner, cleanup_future
        with hold_guard:
            if cleanup_future is future:
                return
            cleanup_future = future
        try:
            future.result()
        except AudioCleanupPending as exc:
            cleanup_owner = exc.cleanup_owner
            # This plain callback keeps the existing local/parent holds alive
            # after modal destruction. Only the runner establishes silence.
            cleanup_owner.add_quiescent_callback(release_holds)
            return
        except BaseException:
            pass
        release_holds()

    def release_when_stopped():
        if active_future is not None:
            if active_future.done():
                release_completed(active_future)
            else:
                active_future.add_done_callback(release_completed)
        else:
            release_holds()

    def poll_cleanup():
        nonlocal busy
        if not alive or cleanup_owner is None:
            return
        if cleanup_owner.quiescent:
            release_holds()
            busy = False
            status.SetValue('Microphone cleanup confirmed. No unfinished recording was published.')
            controls()
            if closing:
                dialog.EndModal(wx.ID_CANCEL)
        else:
            wx.CallLater(100, poll_cleanup)

    def display_record(event=None):
        index = saved.GetSelection()
        if 0 <= index < len(records):
            value = records[index]
            audio = value['audio']
            details.SetValue(f"{value['title']}\n{audio['duration_seconds']:.2f} seconds; "
                f"{audio['sample_rate']} Hz; {audio['channels']} channel(s).\n{value['path']}\n"
                'Playback and selection verify the retained bytes before use.')
        controls()

    def show_records(values, select_id=None):
        nonlocal records
        records = tuple(values)
        saved.SetItems([f"{row['title']} ({row['created'][:19]})" for row in records])
        selected = next((index for index, row in enumerate(records) if row['id'] == select_id), wx.NOT_FOUND)
        saved.SetSelection(selected)
        display_record()

    def run(operation, complete, phase, *, prepared=False, measured=False):
        nonlocal busy, active_future
        if busy and not prepared:
            return
        busy = True
        controls()
        def observe(method, *args, **kwargs):
            try:
                return getattr(progress, method)(*args, **kwargs)
            except Exception as exc:
                progress.reporting_error = 'Recording progress feedback is unavailable: ' + str(exc)
        token = observe('start', phase)
        def measured_progress(seconds):
            if token is not None:
                observe('update', seconds, None, f'Recorded {seconds:.1f} seconds', token=token)
        status.SetValue(phase)
        try:
            future = executor.submit(lambda: operation(measured_progress)) if measured else executor.submit(operation)
            active_future = future
        except Exception:
            busy = False
            release_holds()
            controls()
            raise

        def finished(future):
            release_completed(future)
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = False
                try:
                    result = future.result()
                    complete(result)
                    if token is not None:
                        observe('succeed', phase + ': verified complete', token=token)
                except AudioCleanupPending as exc:
                    busy = not exc.cleanup_owner.quiescent
                    if token is not None:
                        observe('fail', 'Microphone cleanup needs confirmation', token=token)
                    status.SetValue('Microphone cleanup has not confirmed silence. Media remains muted. '
                                    'Stop retries cleanup; Close retains the cleanup owner in the background.')
                    if busy:
                        wx.CallLater(100, poll_cleanup)
                except (AudioCancelled, InterruptedError):
                    if token is not None:
                        observe('cancel', 'Recording operation cancelled; unfinished audio discarded.', token=token)
                    status.SetValue('Cancelled. Previous saved recordings are preserved.')
                except Exception as exc:
                    if token is not None:
                        observe('fail', 'Recording operation failed', token=token)
                    status.SetValue(str(exc))
                controls()
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
            devices = values
            microphone.SetItems([item.name + ' (' + item.id[:8] + ')' for item in devices])
            microphone.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose a microphone explicitly, then Start. Device discovery did not record audio.')
        run(lambda: service.devices(cancelled=cancelled.is_set), complete, 'Listing local microphones')

    def start(event=None):
        nonlocal busy, generation, holds
        if busy:
            return
        index = microphone.GetSelection()
        if not 0 <= index < len(devices):
            status.SetValue('Select a microphone before Start.')
            return
        try:
            options = RecordingOptions(max_seconds=minutes.GetValue() * 60,
                sample_rate=int(sample_rate.GetStringSelection()), channels=channels.GetSelection() + 1,
                max_bytes=2 * 1024**3)
            requested_title = title.GetValue()
            device_id = devices[index].id
            cancelled.clear()
            stopped.clear()
            generation += 1
            current_generation = generation
            busy = True
            controls()
            for controller in controllers:
                holds.append(controller.hold_audio(recording=True))
            controls()
            status.SetValue('Waiting for playback and progress audio to confirm silence before opening the microphone.')
            deadline = time.monotonic() + 3

            def prepare():
                nonlocal busy
                if not alive:
                    release_holds()
                    return
                if cancelled.is_set() or stopped.is_set() or current_generation != generation:
                    busy = False
                    release_holds()
                    controls()
                    status.SetValue('Stopped before opening the microphone. No recording was created.')
                    if closing:
                        dialog.EndModal(wx.ID_CANCEL)
                    return
                if not all(hold.ready for hold in holds):
                    if time.monotonic() >= deadline:
                        busy = False
                        release_holds()
                        controls()
                        status.SetValue('Progress playback has not confirmed silence; no microphone was opened.')
                    else:
                        wx.CallLater(25, prepare)
                    return

                def operation(report):
                    player.close()
                    return service.capture(device_id, title=requested_title, options=options,
                        cancelled=cancelled.is_set, stop_requested=stopped.is_set,
                        progress=report)

                def complete(value):
                    show_records((*records, value), value['id'])
                    status.SetValue('Saved and verified. The recording is retained as a new Studio input.')
                run(operation, complete, 'Recording microphone; Stop saves, Cancel discards',
                    prepared=True, measured=True)
            prepare()
        except Exception as exc:
            busy = False
            release_holds()
            controls()
            status.SetValue(str(exc))

    def preview(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        current_generation = generation
        def complete(value):
            play_when_quiet(controllers, lambda: player.play(value['path']), schedule=wx.CallLater,
                alive=lambda: alive and generation == current_generation,
                on_error=lambda exc: status.SetValue(str(exc)))
        run(lambda: service.get(records[index]['id'], cancelled=cancelled.is_set), complete, 'Verifying saved recording')

    def use(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        def complete(value):
            nonlocal selected_result
            if on_selected is not None:
                on_selected(value)
            selected_result = value
            dialog.EndModal(wx.ID_OK)
        run(lambda: service.get(records[index]['id'], cancelled=cancelled.is_set), complete, 'Verifying selected recording')

    def close(event=None):
        nonlocal closing, generation
        generation += 1
        if busy:
            closing = True
            cancelled.set()
            if cleanup_owner is not None and not cleanup_owner.quiescent:
                frame.report('Microphone cleanup is still pending. Its capture owner and media quiet reservation are retained.')
                dialog.EndModal(wx.ID_CANCEL)
                return
            status.SetValue('Closing the microphone and discarding unfinished audio before leaving.')
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()
        else:
            dialog.EndModal(wx.ID_CANCEL)

    def stop_playback(event=None):
        nonlocal generation
        generation += 1
        player.stop()
        for controller in controllers:
            controller.refresh_audio_state()

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
                    status.SetValue('Cleanup retry could not start. The native owner and quiet reservation '
                                    'remain retained. ' + str(exc))
                    return
            status.SetValue('Retrying microphone cleanup. Media stays muted until silence is confirmed.')
        elif busy:
            status.SetValue('Stop requested. Waiting for WAV finalization and owned process cleanup.')

    def cancel_recording(event=None):
        cancelled.set()
        if busy:
            status.SetValue('Cancellation requested. Waiting for the microphone and owned processes to stop.')

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
        def loaded_records(result):
            values, unverified = result
            show_records(values)
            if unverified:
                status.SetValue(f'{len(unverified)} retained WAV original(s) lack a verified receipt. '
                    f'They remain unselected and unplayed in {service.root}. Preserve them for explicit recovery.')
            else:
                status.SetValue('Saved recording receipts loaded. Refresh microphones and choose a device explicitly before Start.')
        run(lambda: (service.list(cancelled=cancelled.is_set), unverified_recording_files(library.root)),
            loaded_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancelled.set()
        generation += 1
        executor.shutdown(wait=False, cancel_futures=True)
        silence_confirmed = False
        try:
            player.close()
            silence_confirmed = True
        except (OSError, RuntimeError) as exc:
            try:
                frame.report('Recording preview device could not confirm silence: ' + str(exc))
            except Exception:
                pass
        finally:
            for controller in controllers:
                try:
                    controller.unregister_player(player, silence_confirmed=silence_confirmed)
                except Exception:
                    pass
            release_when_stopped()
            progress.close()
            dialog.Destroy()
    return selected_result


def install_recording_actions(frame):
    import wx
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'audio studio'), None)
    if menu is None:
        raise RuntimeError('Install the shared Audio Studio menu before recording actions.')
    item = menu.Append(wx.ID_ANY, 'Record microphone and manage saved inputs...')
    frame.Bind(wx.EVT_MENU, lambda event: show_recording(frame), item)
