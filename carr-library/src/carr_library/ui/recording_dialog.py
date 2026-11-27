"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
import threading
import time

from ..audio.models import AudioCancelled, AudioCleanupPending
from ..audio.recording import RecordingOptions, RecordingService, unverified_recording_files
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from ..progress import ProgressState, bind_progress, current_progress
from ..durable_state import local_path, regular
from .progress_panel import NativeProgressPanel


def show_recording(frame, *, on_selected=None):
    import wx
    if getattr(frame, 'working', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    library_root = local_path(library.root, exists=True)
    info = regular(library_root, directory=True)
    library_identity = info.st_dev, info.st_ino
    executor = frame.executor
    frame.cancel_event.clear()
    if not callable(getattr(frame, 'register_worker_cleanup', None)):
        raise RuntimeError('Recording needs the current desktop worker lifetime owner.')
    dialog = wx.Dialog(frame, title='Local microphone recording', size=(820, 740))
    dialog.library = library
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Refresh devices and select a microphone, then choose Start. '
        'Stop saves a verified recording; Cancel discards an unfinished recording. '
        'No microphone opens automatically. Recording uses the selected Windows device locally.'),
        0, wx.EXPAND | wx.ALL, 8)

    def field(caption, factory, **kwargs):
        layout.Add(wx.StaticText(dialog, label=caption), 0, wx.LEFT | wx.TOP, 8)
        control = factory(dialog, **kwargs)
        control.SetName(caption)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        return control

    microphone = field('Microphone device (explicit selection required)', wx.Choice, choices=[])
    title = field('Recording title', wx.TextCtrl, value='Microphone recording')
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=180, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice, choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    monitoring = field('Monitor microphone in headphones (off by default)', wx.CheckBox,
                       label='Enable dry input monitoring')
    monitoring.SetName('Enable dry input monitoring')
    monitoring.SetValue(False)
    monitor_output = field('Monitoring output (explicit selection required)', wx.Choice, choices=[])
    saved = field('Saved recordings', wx.Choice, choices=[])
    details = field('Recording result and retained input path', wx.TextCtrl,
                    value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Recording status', wx.TextCtrl, value='No microphone selected.', style=wx.TE_READONLY)
    buttons = {}
    for row in ((('refresh', 'Refresh microphones'), ('refresh_monitor', 'Refresh monitoring outputs'),
                 ('start', 'Start recording'),
                 ('stop', 'Stop and save recording'), ('cancel', 'Cancel unfinished recording')),
                (('pause', 'Pause microphone recording'), ('resume', 'Resume microphone recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('export', 'Export verified recording to new WAV'),
                 ('use', 'Use selected recording'), ('close', 'Close'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(dialog, label=label)
            button.SetName(label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    if on_selected is None:
        buttons['use'].Hide()
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    dialog.host, dialog.layout, dialog.working = dialog, layout, False
    parent_progress = getattr(frame, 'progress_panel', None)
    progress = NativeProgressPanel(dialog)
    if parent_progress is not None:
        progress.hub = parent_progress.hub
    dialog.progress_panel = progress
    progress.set_context_name('Recording operation progress')
    progress.text.SetName('Recording current progress and phase')
    progress.query_button.SetName('Query recording progress')
    controllers = tuple(item for item in (progress, parent_progress) if item is not None)
    service = RecordingService(library.root)
    player = WavePlayer()
    for controller in controllers:
        controller.register_player(player)
    cancelled, stopped, paused = threading.Event(), threading.Event(), threading.Event()
    monitor_outputs, capture_phase = (), 'idle'
    dialog.capture_state = 'idle'
    busy, alive, closing, devices, records, holds, selected_result = False, True, False, (), (), [], None
    generation = 0
    active_future = None
    cleanup_owner = cleanup_future = cleanup_retry = None
    hold_guard = threading.RLock()
    player_guard = threading.RLock()
    player_cleaned = cleanup_queued = False
    unregister_cleanup = lambda: None

    def close_player():
        with player_guard:
            player.close()

    def cleanup_player():
        nonlocal player_cleaned
        with player_guard:
            if player_cleaned:
                return
            player.close()
            for controller in controllers:
                controller.unregister_player(player, silence_confirmed=True)
            player_cleaned = True
            unregister_cleanup()

    unregister_cleanup = frame.register_worker_cleanup(cleanup_player)

    def request_cleanup():
        nonlocal cleanup_queued
        if cleanup_queued:
            return
        cleanup_queued = True
        try:
            future = executor.submit(cleanup_player)
        except Exception:
            # The already registered host finalizer remains strongly owned and
            # retryable; a rejected submission must not discard native handles.
            cleanup_queued = False
            return
        def checked(completed):
            try:
                completed.result()
            except BaseException:
                # The registered host finalizer owns retries, including after
                # modal destruction. Do not update a stale native window.
                pass
        future.add_done_callback(checked)

    def scope_current():
        if (not alive or getattr(frame, 'library', None) is not library
                or library.root != library_root or getattr(frame, '_closing', False)):
            return False
        try:
            current = regular(local_path(library_root, exists=True), directory=True)
            return (current.st_dev, current.st_ino) == library_identity
        except (OSError, ValueError):
            return False

    def stopping():
        return cancelled.is_set() or frame.cancel_event.is_set() or not scope_current()

    def controls():
        dialog.working = busy
        available = not busy and scope_current()
        for control in (microphone, title, minutes, sample_rate, channels, saved, monitoring):
            control.Enable(available)
        buttons['refresh'].Enable(available)
        buttons['refresh_monitor'].Enable(available)
        monitor_output.Enable(available and monitoring.GetValue())
        monitor_ready = not monitoring.GetValue() or 0 <= monitor_output.GetSelection() < len(monitor_outputs)
        buttons['start'].Enable(available and monitor_ready and 0 <= microphone.GetSelection() < len(devices))
        buttons['pause'].Enable(busy and capture_phase == 'recording' and not paused.is_set())
        buttons['resume'].Enable(busy and capture_phase == 'paused' and paused.is_set())
        buttons['stop'].Enable(busy and bool(holds))
        buttons['cancel'].Enable(busy)
        for key in ('preview', 'use', 'export'):
            buttons[key].Enable(available and 0 <= saved.GetSelection() < len(records))
        buttons['stop_preview'].Enable(available)

    def release_holds():
        nonlocal holds
        with hold_guard:
            current, holds = holds, []
        for hold in current:
            try:
                hold.close()
            except Exception as exc:
                progress.notice = 'Progress audio release needs attention: ' + str(exc)

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
        if not scope_current() or cleanup_owner is None:
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
        if not prepared and scope_current():
            frame.cancel_event.clear()
        if stopping():
            if prepared:
                busy = False
                release_holds()
            controls()
            return
        busy = True
        controls()
        def observe(method, *args, **kwargs):
            try:
                return getattr(progress, method)(*args, **kwargs)
            except Exception as exc:
                progress.notice = 'Recording progress feedback is unavailable: ' + str(exc)
        task = observe('begin', phase)
        def measured_progress(seconds):
            if task is not None:
                try:
                    task.update(phase=f'Recorded {seconds:.1f} seconds')
                except Exception as exc:
                    progress.notice = 'Recording progress feedback is unavailable: ' + str(exc)
        status.SetValue(phase)
        def work():
            if stopping():
                raise AudioCancelled('Recording operation stopped before execution.')
            if task is None:
                return operation(measured_progress) if measured else operation()
            with bind_progress(task):
                return operation(measured_progress) if measured else operation()
        try:
            future = executor.submit(work)
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
                if not scope_current():
                    if task is not None:
                        observe('finish', task, 'cancelled', 'The recording dialog no longer owns the active library.')
                    busy = cleanup_owner is not None and not cleanup_owner.quiescent
                    status.SetValue('The active library changed or is closing. Any completed recording '
                                    'remains in its original library. Reopen this dialog before continuing.'
                                    + (' Native cleanup is still pending; media remains muted.' if busy else ''))
                    controls()
                    if closing or getattr(frame, '_closing', False):
                        dialog.EndModal(wx.ID_CANCEL)
                    return
                try:
                    result = future.result()
                    complete(result)
                    if task is not None:
                        observe('finish', task, 'succeeded', phase + ': verified complete')
                except AudioCleanupPending as exc:
                    busy = not exc.cleanup_owner.quiescent
                    if task is not None:
                        observe('finish', task, 'failed', 'Microphone cleanup needs confirmation')
                    status.SetValue('Microphone cleanup has not confirmed silence. Media remains muted. '
                                    'Stop retries cleanup; Close retains the cleanup owner in the background.' if busy else
                                    'Native cleanup is confirmed. No unfinished recording was published; previous originals are preserved.')
                    if busy:
                        wx.CallLater(100, poll_cleanup)
                except (AudioCancelled, InterruptedError):
                    if task is not None:
                        observe('finish', task, 'cancelled', 'Recording operation cancelled; unfinished audio discarded.')
                    status.SetValue('Cancelled. Previous saved recordings are preserved.')
                except Exception as exc:
                    if task is not None:
                        observe('finish', task, 'failed', 'Recording operation failed')
                    status.SetValue(str(exc))
                controls()
                if closing and not busy:
                    dialog.EndModal(wx.ID_CANCEL)
            if alive:
                wx.CallAfter(apply)
        future.add_done_callback(finished)

    def refresh(event=None):
        nonlocal devices, generation
        if busy or not scope_current():
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
        run(lambda: service.devices(cancelled=stopping), complete, 'Listing local microphones')

    def refresh_monitor(event=None):
        nonlocal monitor_outputs
        if busy or not scope_current():
            return
        monitor_outputs = ()
        monitor_output.SetItems([])
        monitor_output.SetSelection(wx.NOT_FOUND)
        cancelled.clear()
        def complete(values):
            nonlocal monitor_outputs
            monitor_outputs = tuple(value for value in values if value.selectable)
            monitor_output.SetItems([f'{value.name}; device {value.index + 1}' for value in monitor_outputs])
            monitor_output.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose a monitoring output explicitly. Use headphones to avoid microphone feedback; no output was opened.')
        run(lambda: service.monitoring_outputs(cancelled=stopping), complete,
            'Listing monitoring outputs without playing audio')

    def start(event=None):
        nonlocal busy, generation, holds, capture_phase
        if busy or not scope_current():
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
            output = None
            if monitoring.GetValue():
                output_index = monitor_output.GetSelection()
                if not 0 <= output_index < len(monitor_outputs):
                    raise ValueError('Select a monitoring output explicitly before Start.')
                output = monitor_outputs[output_index]
            paused.clear()
            capture_phase = dialog.capture_state = 'opening'
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
                if stopping() or stopped.is_set() or current_generation != generation:
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
                    close_player()
                    def state_changed(snapshot):
                        task = current_progress()
                        if task is not None:
                            try:
                                if snapshot.state == 'paused' and task.snapshot().state == ProgressState.RUNNING:
                                    task.pause('Microphone and monitoring are closed; paused time is not recorded.')
                                elif snapshot.state != 'paused' and task.snapshot().state == ProgressState.PAUSED:
                                    task.resume('Preparing the next recording operation.')
                            except Exception as exc:
                                progress.notice = 'Recording progress feedback is unavailable: ' + str(exc)
                        def apply_state():
                            nonlocal capture_phase
                            if (not scope_current() or not busy or current_generation != generation
                                    or cancelled.is_set() or stopped.is_set()):
                                return
                            capture_phase = dialog.capture_state = snapshot.state
                            messages = {
                                'opening': 'Opening the selected microphone for a new segment.',
                                'recording': 'Recording. Pause closes this microphone segment; Stop saves.',
                                'pausing': 'Finishing the current segment and closing microphone and monitoring.',
                                'paused': 'Paused; microphone and monitoring are closed. Resume starts a new segment. Stop saves retained frames.',
                                'finalizing': 'Verifying and joining retained PCM frames; review joins before use.',
                            }
                            status.SetValue(messages.get(snapshot.state, snapshot.state))
                            controls()
                        wx.CallAfter(apply_state)
                    return service.capture(device_id, title=requested_title, options=options,
                        cancelled=stopping, stop_requested=stopped.is_set,
                        progress=report, pause_requested=paused.is_set,
                        state_changed=state_changed, monitor_output=output)

                def complete(value):
                    show_records((*records, value), value['id'])
                    status.SetValue('Saved and verified. The recording is retained as a new Studio input. Review pause joins before use.')
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
        if busy or not scope_current() or not 0 <= index < len(records):
            return
        cancelled.clear()
        current_generation = generation
        def complete(value):
            play_when_quiet(controllers, lambda: player.play(value['path']), schedule=wx.CallLater,
                alive=lambda: scope_current() and generation == current_generation,
                on_error=lambda exc: status.SetValue(str(exc)))
        run(lambda: service.get(records[index]['id'], cancelled=stopping), complete, 'Verifying saved recording')

    def use(event=None):
        index = saved.GetSelection()
        if busy or not scope_current() or not 0 <= index < len(records):
            return
        cancelled.clear()
        def complete(value):
            nonlocal selected_result
            if on_selected is not None:
                on_selected(value)
            selected_result = value
            dialog.EndModal(wx.ID_OK)
        run(lambda: service.get(records[index]['id'], cancelled=stopping), complete, 'Verifying selected recording')

    def export(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        if not scope_current():
            status.SetValue('The active library changed. Reopen recording from that library before exporting.')
            return
        identity = records[index]['id']
        chooser = wx.FileDialog(dialog, 'Export verified recording to a new WAV',
            defaultFile='recording-' + identity[:8] + '.wav',
            wildcard='WAV audio (*.wav)|*.wav', style=wx.FD_SAVE)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            destination = chooser.GetPath()
        finally:
            chooser.Destroy()
        cancelled.clear()

        def complete(value):
            status.SetValue('Verified WAV exported to ' + value['path'] + '. The retained original is unchanged.')

        run(lambda: service.export(identity, destination, cancelled=stopping), complete,
            'Copying and verifying the recording export')

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

    def pause_recording(event=None):
        nonlocal capture_phase
        if busy and scope_current() and capture_phase == 'recording' and not stopped.is_set() and not cancelled.is_set():
            paused.set()
            capture_phase = dialog.capture_state = 'pausing'
            status.SetValue('Pause requested. Waiting for the microphone and monitoring to close.')
            controls()

    def resume_recording(event=None):
        nonlocal capture_phase
        if busy and scope_current() and capture_phase == 'paused' and not stopped.is_set() and not cancelled.is_set():
            paused.clear()
            capture_phase = dialog.capture_state = 'opening'
            status.SetValue('Resume requested. Reopening the explicitly selected microphone.')
            controls()

    def cancel_recording(event=None):
        cancelled.set()
        if busy:
            status.SetValue('Cancellation requested. Waiting for the microphone and owned processes to stop.')

    for key, callback in (('refresh', refresh), ('refresh_monitor', refresh_monitor),
            ('start', start), ('stop', stop_recording), ('pause', pause_recording), ('resume', resume_recording),
            ('cancel', cancel_recording), ('preview', preview), ('stop_preview', stop_playback),
            ('use', use), ('export', export), ('close', close)):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    monitoring.Bind(wx.EVT_CHECKBOX, lambda event: controls())
    monitor_output.Bind(wx.EVT_CHOICE, lambda event: controls())
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
        run(lambda: (service.list(cancelled=stopping), unverified_recording_files(library.root)),
            loaded_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancelled.set()
        generation += 1
        # Reuse the host executor and its already registered finalizer.
        # Capture/monitor cleanup keeps its media holds independently of wx.
        try:
            for controller in controllers:
                controller.unregister_player(player, silence_confirmed=False)
            release_when_stopped()
            if active_future is not None and not active_future.done():
                active_future.add_done_callback(lambda _: request_cleanup())
            else:
                request_cleanup()
        finally:
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
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('audio.recording', 'Record microphone and manage saved inputs'), item)
