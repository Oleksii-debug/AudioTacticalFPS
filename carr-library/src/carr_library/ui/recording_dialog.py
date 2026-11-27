"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
from dataclasses import dataclass, field
import threading
import time

from ..audio.recording import (AudioCancelled, AudioCleanupPending, RecordingOptions,
                               RecordingService, unverified_recording_files)
from ..audio.player import WavePlayer
from ..progress import current_progress
from .operation_progress import operation_progress_for


@dataclass(frozen=True)
class _VerifiedRecording:
    """An actual service result, delivered only by the host's completed Future."""
    value: object
    status: str = field(default='completed', init=False)


def show_recording(frame, *, on_selected=None, project_id=None, window_parent=None,
                   selection_label='Use selected recording', ffmpeg=None):
    import wx
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    progress = operation_progress_for(frame)
    service = RecordingService(library.root, ffmpeg=ffmpeg,
                               progress_hub=getattr(frame, 'progress_hub', None))
    player = WavePlayer(progress_hub=getattr(frame, 'progress_hub', None))
    dialog = wx.Dialog(window_parent or frame, title='Local microphone recording', size=(860, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.library = library
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Refresh devices and select a microphone, then choose Start. '
        'Pause closes the microphone; Resume continues without added silence. '
        'Stop saves a verified recording; Cancel discards an unfinished recording. '
        'No microphone opens automatically. Recording uses the selected Windows device locally.'),
        0, wx.EXPAND | wx.ALL, 8)

    body = wx.ScrolledWindow(dialog, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
    body.SetScrollRate(0, 12)
    fields = wx.BoxSizer(wx.VERTICAL)
    body.SetSizer(fields)
    layout.Add(body, 1, wx.EXPAND)

    def field(caption, factory, **kwargs):
        fields.Add(wx.StaticText(body, label=caption), 0, wx.LEFT | wx.TOP, 8)
        control = factory(body, **kwargs)
        control.SetName(caption)
        fields.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        return control

    microphone = field('Microphone device (explicit selection required)', wx.Choice, choices=[])
    title = field('Recording title', wx.TextCtrl, value='Microphone recording')
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=180, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice, choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    monitor = field('Live monitoring through selected output', wx.CheckBox,
                    label='Monitor captured audio (headphones recommended)')
    monitor.SetValue(False)
    output = field('Monitoring output device (explicit selection required)', wx.Choice, choices=[])
    selection_action = field('Selected recording action', wx.TextCtrl,
                             value=selection_label, style=wx.TE_READONLY)
    saved = field('Saved recordings', wx.Choice, choices=[])
    details = field('Recording result and retained input path', wx.TextCtrl,
                    value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Recording status', wx.TextCtrl, value='No microphone selected.', style=wx.TE_READONLY)
    gauge = wx.Gauge(dialog, range=100)
    gauge.SetName('Recording operation progress')
    progress_label = wx.StaticText(dialog, label='Ready')
    progress_label.SetName('Recording operation phase and count')
    layout.Add(gauge, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(progress_label, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for row in ((('refresh', 'Refresh microphones'), ('outputs', 'Refresh monitoring outputs')),
                (('start', 'Start recording'), ('retake', 'Record retake of selected recording'),
                 ('pause', 'Pause recording'), ('resume', 'Resume recording'),
                 ('stop', 'Stop and save recording'), ('cancel', 'Cancel unfinished recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('use', selection_label), ('close', 'Close'))):
        box = wx.WrapSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(dialog, label=label)
            button.SetName('Use selected recording' if key == 'use' else label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    try:
        unregister_player = progress.register_player(player) if progress is not None else (lambda: None)
    except BaseException:
        player.close()
        dialog.Destroy()
        raise
    cancelled, stopped, pause_requested = threading.Event(), threading.Event(), threading.Event()
    active_started, active_finished = threading.Event(), threading.Event()
    busy, alive, closing, devices, output_devices, records, holds, selected_result = (
        False, True, False, (), (), (), [], None)
    generation = 0
    recording = False
    capture_state = 'idle'
    cleanup_owner = cleanup_retry = preview_request = None
    cleanup_saved = None
    hold_guard = threading.RLock()

    def current():
        return (alive and not getattr(frame, '_closing', False)
                and getattr(frame, 'library', None) is library)

    def cancellation():
        return cancelled.is_set() or frame.cancel_event.is_set() or not current()

    def controls():
        if not alive:
            return
        has_device = 0 <= microphone.GetSelection() < len(devices)
        has_record = 0 <= saved.GetSelection() < len(records)
        has_output = not monitor.GetValue() or 0 <= output.GetSelection() < len(output_devices)
        for control in (microphone, title, minutes, sample_rate, channels, saved, monitor):
            control.Enable(not busy)
        output.Enable(not busy and monitor.GetValue())
        buttons['outputs'].Enable(not busy and monitor.GetValue())
        buttons['refresh'].Enable(not busy)
        buttons['start'].Enable(not busy and has_device and has_output)
        buttons['retake'].Enable(not busy and has_device and has_output and has_record)
        buttons['pause'].Enable(busy and recording and capture_state == 'recording'
                                and not stopped.is_set() and not cancelled.is_set())
        buttons['resume'].Enable(busy and recording and capture_state == 'paused'
                                 and not stopped.is_set() and not cancelled.is_set())
        buttons['stop'].Enable(busy and recording)
        buttons['cancel'].Enable(busy)
        for key in ('preview', 'use'):
            buttons[key].Enable(not busy and has_record)
        buttons['stop_preview'].Enable(not busy)

    def release_holds():
        nonlocal holds
        with hold_guard:
            current_holds, holds = holds, []
        for hold in current_holds:
            try:
                hold.close()
            except Exception as exc:
                if progress is not None:
                    progress.reporting_error = 'Progress audio release needs attention: ' + str(exc)

    def release_when_stopped():
        if cleanup_owner is not None and not cleanup_owner.quiescent:
            return
        if active_finished.is_set() or not active_started.is_set():
            release_holds()

    def settled(expected_generation):
        if expected_generation != generation:
            return
        release_when_stopped()
        if alive and closing:
            if cleanup_owner is None or cleanup_owner.quiescent:
                dialog.EndModal(wx.ID_CANCEL)

    def poll_cleanup():
        nonlocal busy, recording
        if not alive or cleanup_owner is None:
            return
        if cleanup_owner.quiescent:
            release_holds()
            busy = recording = False
            status.SetValue('Microphone cleanup confirmed. ' + (
                'The saved recording remains available.' if cleanup_saved is not None
                else 'No unfinished recording was published.'))
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
        nonlocal busy, cleanup_owner, cleanup_saved
        if (busy and not prepared) or not current():
            return False
        if getattr(frame, 'working', False):
            busy = False
            release_holds()
            controls()
            status.SetValue('Finish or cancel the current operation before recording work.')
            return False
        busy = True
        active_started.clear()
        active_finished.clear()
        expected_generation = generation
        controls()
        status.SetValue(phase)
        gauge.SetValue(0)
        progress_label.SetLabel(phase)
        delivered = False

        def measured_progress(seconds):
            task = current_progress()
            if task is not None:
                try:
                    task.update(completed=int(seconds), total=None, phase='recording',
                                message=f'Recorded {seconds:.1f} seconds')
                except Exception:
                    pass
            def display():
                if current() and busy and expected_generation == generation:
                    gauge.Pulse()
                    progress_label.SetLabel(f'Recorded {seconds:.1f} seconds; total unknown')
            try:
                wx.CallAfter(display)
            except RuntimeError:
                pass

        def perform():
            nonlocal cleanup_owner, cleanup_saved
            active_started.set()
            try:
                if cancellation():
                    raise AudioCancelled('Recording operation cancelled before it started.')
                value = operation(measured_progress) if measured else operation()
                return _VerifiedRecording(value)
            except AudioCleanupPending as exc:
                cleanup_owner = exc.cleanup_owner
                cleanup_saved = getattr(exc, 'saved_recording', None)
                cleanup_owner.add_quiescent_callback(lambda: wx.CallAfter(release_holds))
                raise
            finally:
                active_finished.set()
                try:
                    wx.CallAfter(settled, expected_generation)
                except RuntimeError:
                    pass

        def arrived(outcome=None, error=None):
            nonlocal busy, delivered, recording, capture_state
            if delivered:
                return
            delivered = True
            release_when_stopped()
            if not alive:
                return
            if closing or not current():
                if cleanup_owner is None or cleanup_owner.quiescent:
                    dialog.EndModal(wx.ID_CANCEL)
                return
            busy = recording = False
            capture_state = 'idle'
            if isinstance(error, AudioCleanupPending):
                busy = recording = not error.cleanup_owner.quiescent
                if cleanup_saved is not None:
                    show_records((*records, cleanup_saved), cleanup_saved['id'])
                status.SetValue('Microphone cleanup has not confirmed silence. Media remains muted. '
                    + ('The verified saved recording is retained. ' if cleanup_saved is not None else '')
                    + 'Stop retries cleanup; Close retains the cleanup owner in the background.')
                if busy:
                    wx.CallLater(100, poll_cleanup)
            elif isinstance(error, (AudioCancelled, InterruptedError)):
                status.SetValue('Cancelled. Previous saved recordings are preserved.')
                progress_label.SetLabel('Cancelled')
            elif error is not None:
                status.SetValue(str(error))
                progress_label.SetLabel('Failed; retained recordings are unchanged')
            else:
                try:
                    if not isinstance(outcome, _VerifiedRecording):
                        raise TypeError('The recording operation returned an unsupported result.')
                    complete(outcome.value)
                    gauge.SetValue(100)
                    progress_label.SetLabel('Verified complete')
                except Exception as exc:
                    # Presentation never retries or reverses the service's result.
                    status.SetValue('The operation returned; its result needs review. ' + str(exc))
            controls()

        try:
            accepted = frame.run_job(phase, perform,
                done=lambda value: arrived(value), failed=lambda error: arrived(error=error),
                progress_total=None, progress_unit='seconds' if measured else 'recordings')
        except Exception as exc:
            arrived(error=exc)
            return False
        if accepted is False or (busy and not getattr(frame, 'working', False)
                                 and not active_started.is_set()):
            arrived(error=RuntimeError('The recording operation did not start.'))
            return False
        return True

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
        run(lambda: service.devices(cancelled=cancellation), complete, 'Listing local microphones')

    def refresh_outputs(event=None):
        nonlocal output_devices, generation
        if busy:
            return
        generation += 1
        output_devices = ()
        output.SetItems([])
        output.SetSelection(wx.NOT_FOUND)
        cancelled.clear()
        def complete(values):
            nonlocal output_devices
            output_devices = tuple(values)
            output.SetItems([f'{item.name} (output {item.id}; {item.channels} channels)'
                             for item in output_devices])
            output.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose an output explicitly to monitor. No microphone was opened.')
        run(lambda: service.monitor_devices(cancelled=cancellation), complete,
            'Listing local monitoring outputs')

    def start(event=None, *, retake_of=None):
        nonlocal busy, generation, holds, cleanup_owner, cleanup_saved, recording, capture_state
        if busy or not current():
            return
        index = microphone.GetSelection()
        if not 0 <= index < len(devices):
            status.SetValue('Select a microphone before Start.')
            return
        monitor_enabled = bool(monitor.GetValue())
        output_index = output.GetSelection()
        if monitor_enabled and not 0 <= output_index < len(output_devices):
            status.SetValue('Choose a monitoring output explicitly before Start.')
            return
        try:
            options = RecordingOptions(max_seconds=minutes.GetValue() * 60,
                sample_rate=int(sample_rate.GetStringSelection()), channels=channels.GetSelection() + 1,
                max_bytes=2 * 1024**3)
            requested_title = title.GetValue()
            device_id = devices[index].id
            monitor_device_id = output_devices[output_index].id if monitor_enabled else None
            cancelled.clear()
            stopped.clear()
            pause_requested.clear()
            capture_state = 'starting'
            active_started.clear()
            active_finished.clear()
            cleanup_owner = cleanup_saved = None
            generation += 1
            current_generation = generation
            busy = recording = True
            controls()
            player.stop()
            if progress is not None:
                holds.append(progress.hold_audio())
            controls()
            status.SetValue('Waiting for playback and progress audio to confirm silence before opening the microphone.')
            deadline = time.monotonic() + 3

            def prepare():
                nonlocal busy, recording
                if not current():
                    busy = recording = False
                    release_holds()
                    if alive and not getattr(frame, '_closing', False):
                        dialog.EndModal(wx.ID_CANCEL)
                    return
                if cancelled.is_set() or stopped.is_set() or current_generation != generation:
                    busy = recording = False
                    release_holds()
                    controls()
                    status.SetValue('Stopped before opening the microphone. No recording was created.')
                    if closing:
                        dialog.EndModal(wx.ID_CANCEL)
                    return
                if not all(hold.ready for hold in holds):
                    if time.monotonic() >= deadline:
                        busy = recording = False
                        release_holds()
                        controls()
                        status.SetValue('Progress playback has not confirmed silence; no microphone was opened.')
                    else:
                        wx.CallLater(25, prepare)
                    return

                def observed_state(value, seconds):
                    task = current_progress()
                    if task is not None:
                        try:
                            task.update(completed=int(seconds), total=None, phase=value,
                                        message=f'{value}: {seconds:.1f} recorded seconds')
                        except Exception:
                            pass
                    def display():
                        nonlocal capture_state
                        if not current() or not busy or closing or current_generation != generation:
                            return
                        capture_state = value
                        messages = {
                            'paused': 'Paused. The microphone is closed. Resume continues; Stop saves; Cancel discards.',
                            'recording': 'Recording microphone. Pause closes the device; Stop saves; Cancel discards.',
                            'finalizing': 'Finalizing and verifying the recorded PCM before saving.'}
                        status.SetValue(messages.get(value, value))
                        progress_label.SetLabel(f'{value}: {seconds:.1f} recorded seconds')
                        controls()
                    try:
                        wx.CallAfter(display)
                    except RuntimeError:
                        pass

                def operation(report):
                    player.stop()
                    return service.capture(device_id, title=requested_title, options=options,
                        cancelled=cancellation, stop_requested=stopped.is_set, progress=report,
                        retake_of=retake_of, monitor=monitor_enabled, monitor_device_id=monitor_device_id,
                        pause_requested=pause_requested.is_set, state_changed=observed_state)

                def complete(value):
                    show_records((*records, value), value['id'])
                    monitor_result = value.get('monitor')
                    monitor_error = monitor_result.get('error') if isinstance(monitor_result, dict) else None
                    pause_info = value.get('capture')
                    reason = {'segment-limit': ' The 128-part limit was reached; this recording was saved.',
                              'pause-timeout': ' The 24-hour pause limit was reached; this recording was saved.'}
                    status.SetValue('Saved and verified. The original recording is retained.'
                        + (f" Pauses: {pause_info['pause_count']}; retained parts: {len(pause_info['parts'])}."
                           if isinstance(pause_info, dict) else '')
                        + reason.get(value.get('completion'), '')
                        + (' Monitoring needs attention: ' + str(monitor_error) if monitor_error else ''))
                run(operation, complete, 'Recording microphone; Stop saves, Cancel discards',
                    prepared=True, measured=True)
            prepare()
        except Exception as exc:
            busy = recording = False
            release_holds()
            controls()
            status.SetValue(str(exc))

    def retake(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        start(retake_of=records[index]['id'])

    def preview(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        current_generation = generation
        identity = records[index]['id']
        def complete(value):
            nonlocal preview_request
            action = lambda: player.play(value['path'])
            if progress is not None:
                if preview_request is not None:
                    preview_request.close()
                preview_request = progress.media_action(action,
                    alive=lambda: current() and generation == current_generation,
                    failed=lambda exc: status.SetValue(str(exc)) if current() else None)
            elif current():
                action()
        run(lambda: service.get(identity, cancelled=cancellation), complete,
            'Verifying saved recording')

    def use(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        identity = records[index]['id']
        def selected():
            value = service.get(identity, cancelled=cancellation)
            if project_id is not None:
                value = dict(value, retained=service.retain(identity, project_id, cancelled=cancellation))
            return value
        def complete(value):
            nonlocal selected_result
            if on_selected is not None:
                on_selected(value)
            selected_result = value
            dialog.EndModal(wx.ID_OK)
        run(selected, complete, 'Verifying selected recording')

    def close(event=None):
        nonlocal closing
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
        nonlocal generation, preview_request
        generation += 1
        if preview_request is not None:
            preview_request.close()
            preview_request = None
        player.stop()
        if progress is not None:
            progress.tick()

    def pause_recording(event=None):
        nonlocal capture_state
        if not current() or not busy or not recording or capture_state != 'recording':
            return
        pause_requested.set()
        capture_state = 'pausing'
        controls()
        status.SetValue('Pause requested. Waiting for the microphone process to close and its PCM to verify.')

    def resume_recording(event=None):
        nonlocal capture_state
        if (not current() or not busy or not recording or capture_state != 'paused'
                or stopped.is_set() or cancelled.is_set()):
            return
        pause_requested.clear()
        capture_state = 'resuming'
        controls()
        status.SetValue('Resume requested. Verifying the selected runtime before reopening the same microphone.')

    def stop_recording(event=None):
        nonlocal cleanup_retry
        stopped.set()
        controls()
        if cleanup_owner is not None and not cleanup_owner.quiescent:
            if cleanup_retry is None or not cleanup_retry.is_alive():
                cleanup_retry = threading.Thread(target=cleanup_owner.close,
                    name='recording-cleanup-retry', daemon=True)
                cleanup_retry.start()
            status.SetValue('Retrying microphone cleanup. Media stays muted until silence is confirmed.')
        elif busy:
            status.SetValue('Stop requested. Waiting for WAV finalization and owned process cleanup.')

    def cancel_recording(event=None):
        cancelled.set()
        controls()
        if busy:
            status.SetValue('Cancellation requested. Waiting for the microphone and owned processes to stop.')

    for key, callback in (('refresh', refresh), ('outputs', refresh_outputs), ('start', start),
            ('retake', retake), ('pause', pause_recording), ('resume', resume_recording), ('stop', stop_recording),
            ('cancel', cancel_recording), ('preview', preview), ('stop_preview', stop_playback),
            ('use', use), ('close', close)):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    microphone.Bind(wx.EVT_CHOICE, lambda event: controls())
    output.Bind(wx.EVT_CHOICE, lambda event: controls())
    monitor.Bind(wx.EVT_CHECKBOX, lambda event: controls())
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
        run(lambda: (service.list(cancelled=cancellation), unverified_recording_files(library.root)),
            loaded_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancelled.set()
        if preview_request is not None:
            preview_request.close()
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
            # A failed device close retains the same player's explicit hub
            # ownership; its cleanup token establishes actual quiescence.
            if silence_confirmed:
                unregister_player()
            release_when_stopped()
            dialog.Destroy()
    return selected_result


def install_recording_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'recording_menu_item', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'audio'), None)
    if menu is None:
        raise RuntimeError('Install the shared Audio menu before recording actions.')
    label = 'Record microphone and manage saved inputs...'
    item = menu.Append(wx.ID_ANY, label)
    def open_recording(event=None):
        try:
            show_recording(frame)
        except Exception as exc:
            frame.report(str(exc))
    frame.Bind(wx.EVT_MENU, open_recording, item)
    register_menu_command(frame, CommandSpec('audio.recording', label.rstrip('.'), 'main'), item)
    frame.recording_menu_item = item
    return item
