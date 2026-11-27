"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from ..audio.capture_types import AudioCancelled, AudioCleanupPending
from ..audio.recording import RecordingOptions, RecordingService, RecordingState, unverified_recording_files
from ..audio.player import WavePlayer
from .operation_progress import operation_progress_for


def show_recording(frame, *, on_selected=None):
    import wx
    if getattr(frame, 'working', False) or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    progress = operation_progress_for(frame)
    if progress is None or progress.closed:
        frame.report('Open recording from the active desktop progress host.')
        return None
    dialog = wx.Dialog(frame, title='Local microphone recording', size=(820, 740))
    dialog.library = library
    body = wx.ScrolledWindow(dialog, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
    body.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    introduction = wx.StaticText(body, label='Refresh devices and select a microphone, then choose Start. '
        'Stop saves a verified recording; Cancel discards an unfinished recording. '
        'Pause closes the microphone; Resume reopens the same selection. Paused wall time is omitted. '
        'Dry monitoring is optional and off by default; use headphones to reduce feedback. '
        'No microphone or output opens automatically.')
    introduction.Wrap(760)
    layout.Add(introduction, 0, wx.EXPAND | wx.ALL, 8)

    def field(name, factory, **kwargs):
        layout.Add(wx.StaticText(body, label=name), 0, wx.LEFT | wx.TOP, 8)
        control = factory(body, **kwargs)
        control.SetName(name)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 6)
        return control

    microphone = field('Microphone device (explicit selection required)', wx.Choice, choices=[])
    title = field('Recording title', wx.TextCtrl, value='Microphone recording')
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=180, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice, choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    monitor_enabled = field('Enable dry microphone monitoring', wx.CheckBox,
                            label='Monitor the same capture through the explicitly selected output')
    monitor_enabled.SetValue(False)
    monitor_output = field('Monitoring output (explicit selection required)', wx.Choice, choices=[])
    notice = wx.StaticText(body, label='Monitoring queues at most 200 ms; actual device latency is not measured. '
        'Dropped monitoring frames never remove recorded samples. Paused takes retain up to 128 segments '
        'and one hour of total paused time; their joined copy needs additional staging space.')
    notice.Wrap(760)
    layout.Add(notice, 0, wx.EXPAND | wx.ALL, 8)
    saved = field('Saved recordings', wx.Choice, choices=[])
    details = field('Recording result and retained input path', wx.TextCtrl,
                    value='', style=wx.TE_MULTILINE | wx.TE_READONLY)
    status = field('Recording status', wx.TextCtrl, value='No microphone selected.', style=wx.TE_READONLY)
    gauge = wx.Gauge(body, range=100)
    gauge.SetName('Recording operation progress')
    progress_label = wx.StaticText(body, label='Ready')
    layout.Add(gauge, 0, wx.EXPAND | wx.ALL, 6)
    layout.Add(progress_label, 0, wx.EXPAND | wx.ALL, 6)
    buttons = {}
    for row in ((('runtime', 'Choose local FFmpeg...'), ('refresh', 'Refresh microphones'),
                 ('refresh_outputs', 'Refresh monitoring outputs')),
                (('start', 'Start recording'), ('pause', 'Pause recording'), ('resume', 'Resume recording')),
                (('stop', 'Stop and save recording'),
                 ('cancel', 'Cancel unfinished recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('use', 'Use selected recording'), ('close', 'Close'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(body, label=label)
            button.SetName(label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    body.SetSizer(layout)
    body.FitInside()
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.Add(body, 1, wx.EXPAND)
    dialog.SetSizer(outer)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    controllers = (progress,)
    service = RecordingService(library.root)
    player = WavePlayer()
    unregister_players = tuple(controller.register_player(player) for controller in controllers)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-recording')
    cancelled, stopped, paused = threading.Event(), threading.Event(), threading.Event()
    busy, alive, closing, devices, records, holds, selected_result = False, True, False, (), (), [], None
    generation = 0
    outputs, recording_state = (), 'idle'
    active_future = None
    cleanup_owner = cleanup_future = cleanup_retry = None
    hold_guard = threading.RLock()
    timers = set()
    content_request = None

    def current():
        return alive and not getattr(frame, '_closing', False) and frame.library is library

    def operation_cancelled():
        return cancelled.is_set() or not current()

    def later(milliseconds, callback):
        retained = []
        def fired():
            if retained:
                timers.discard(retained[0])
            if alive:
                callback()
        timer = wx.CallLater(milliseconds, fired)
        retained.append(timer)
        timers.add(timer)
        return timer

    def cancel_content_request():
        nonlocal content_request
        if content_request is not None:
            content_request.close()
            content_request = None

    def controls():
        for control in (microphone, title, minutes, sample_rate, channels, saved, monitor_enabled):
            control.Enable(not busy)
        buttons['runtime'].Enable(not busy)
        buttons['refresh'].Enable(not busy)
        buttons['refresh_outputs'].Enable(not busy)
        monitor_output.Enable(not busy and monitor_enabled.GetValue())
        valid_monitor = not monitor_enabled.GetValue() or 0 <= monitor_output.GetSelection() < len(outputs)
        buttons['start'].Enable(not busy and valid_monitor and 0 <= microphone.GetSelection() < len(devices))
        controllable = (busy and bool(holds) and not stopped.is_set() and not cancelled.is_set()
                        and (cleanup_owner is None or cleanup_owner.quiescent))
        buttons['pause'].Enable(controllable and recording_state in ('opening', 'recording'))
        buttons['resume'].Enable(controllable and recording_state == 'paused')
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
            later(100, poll_cleanup)

    def display_record(event=None):
        index = saved.GetSelection()
        if 0 <= index < len(records):
            value = records[index]
            audio = value['audio']
            details.SetValue(f"{value['title']}\n{audio['duration_seconds']:.2f} seconds; "
                f"{audio['sample_rate']} Hz; {audio['channels']} channel(s).\n{value['path']}\n"
                'Playback and selection verify the retained bytes before use.')
            if value.get('schema') == 'carr-recording/2':
                monitoring = value['monitoring']
                details.AppendText('\n' + value['timeline'] + '\n'
                    + str(len(value['segments'])) + ' retained segment(s). Monitoring '
                    + ('enabled' if monitoring['enabled'] else 'disabled') + '; '
                    + str(monitoring['submitted_frames']) + ' frames submitted, '
                    + str(monitoring['dropped_frames']) + ' frames dropped from monitoring only. '
                    'Submitted frames do not prove audible delivery.')
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
        if (busy and not prepared) or not current():
            return
        busy = True
        controls()
        def observe(method, *args, **kwargs):
            try:
                return getattr(progress, method)(*args, **kwargs)
            except Exception as exc:
                progress.reporting_error = 'Recording progress feedback is unavailable: ' + str(exc)
        event = observe('start', phase, total=None, unit='milliseconds' if measured else 'items')
        token = getattr(event, 'operation_id', event)
        def measured_progress(seconds):
            if token is not None:
                observe('update', int(seconds * 1000), None,
                        f'Recorded {seconds:.1f} seconds; final duration is not known yet', token=token)
            def display():
                if current() and busy:
                    gauge.Pulse()
                    progress_label.SetLabel(f'Recorded {seconds:.1f} seconds; waiting for verified finalization')
            wx.CallAfter(display)
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
                nonlocal busy, closing, recording_state
                if not alive:
                    return
                busy = False
                recording_state = 'idle'
                paused.clear()
                if not current():
                    closing = True
                    cancelled.set()
                    dialog.EndModal(wx.ID_CANCEL)
                    return
                try:
                    result = future.result()
                    complete(result)
                    if token is not None:
                        observe('succeed', token=token)
                    gauge.SetValue(100)
                    progress_label.SetLabel(phase + ': complete')
                except AudioCleanupPending as exc:
                    busy = not exc.cleanup_owner.quiescent
                    if token is not None:
                        observe('fail', 'Microphone cleanup needs confirmation', token=token)
                    status.SetValue('Microphone cleanup has not confirmed silence. Media remains muted. '
                                    'Stop retries cleanup; Close retains the cleanup owner in the background.')
                    if busy:
                        later(100, poll_cleanup)
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
                try:
                    wx.CallAfter(apply)
                except RuntimeError:
                    pass  # The plain cleanup callback already owns every release.
        future.add_done_callback(finished)

    def choose_runtime(event=None):
        nonlocal service, devices, generation
        if busy or not current():
            return
        chooser = wx.FileDialog(dialog, 'Select the installed local FFmpeg executable',
            wildcard='Windows executable (*.exe)|*.exe|All files|*',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if chooser.ShowModal() != wx.ID_OK or not current():
                return
            selected = RecordingService(library.root, ffmpeg=chooser.GetPath())
            selected._runtime()  # Local path validation only, no process launch.
            service = selected
            devices = ()
            generation += 1
            microphone.SetItems([])
            microphone.SetSelection(wx.NOT_FOUND)
            status.SetValue('Local FFmpeg selected. Refresh microphones, then explicitly choose a device.')
            controls()
        except Exception as exc:
            status.SetValue(str(exc))
        finally:
            chooser.Destroy()

    def refresh(event=None):
        nonlocal devices, generation
        if busy or not current():
            return
        generation += 1
        cancel_content_request()
        devices = ()
        microphone.SetItems([])
        cancelled.clear()
        def complete(values):
            nonlocal devices
            devices = values
            microphone.SetItems([item.name + ' (' + item.id[:8] + ')' for item in devices])
            microphone.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose a microphone explicitly, then Start. Device discovery did not record audio.')
        run(lambda: service.devices(cancelled=operation_cancelled), complete, 'Listing local microphones')

    def refresh_outputs(event=None):
        nonlocal outputs, generation
        if busy or not current():
            return
        generation += 1
        cancel_content_request()
        outputs = ()
        monitor_output.SetItems([])
        monitor_output.SetSelection(wx.NOT_FOUND)
        cancelled.clear()
        def complete(values):
            nonlocal outputs
            outputs = tuple(item for item in values if item.selectable)
            monitor_output.SetItems([item.name + ' (' + str(item.index) + ')' for item in outputs])
            monitor_output.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose a monitoring output explicitly if monitoring is enabled. '
                'Output discovery did not open audio.' if outputs else
                'No output with a stable identity is available. Monitoring cannot be enabled; no default was selected.')
        run(lambda: service.monitoring_outputs(cancelled=operation_cancelled), complete,
            'Listing local monitoring outputs')

    def capture_notice(value, expected_generation):
        if not isinstance(value, RecordingState):
            return
        def apply():
            nonlocal recording_state
            if (not current() or not busy or not holds or expected_generation != generation
                    or cancelled.is_set() or stopped.is_set()):
                return
            recording_state = value.state
            descriptions = {
                'opening': 'Opening the explicitly selected input and optional monitoring output.',
                'recording': 'Recording microphone. Pause closes the current input; Stop saves the take.',
                'pausing': 'Pause requested. Waiting for the input process and monitoring buffers to release.',
                'paused': 'Paused. The microphone and monitoring output are closed. Resume reopens the same selections.',
                'finalizing': 'Finalizing unchanged PCM segments and their retained source receipts.',
            }
            status.SetValue(descriptions.get(value.state, 'Recording state needs review.'))
            if value.state == 'paused':
                progress_label.SetLabel(f'Paused at {value.frames / value.sample_rate:.2f} retained seconds; '
                                        f'{value.segments} completed segment(s).')
            controls()
        try:
            wx.CallAfter(apply)
        except RuntimeError:
            pass  # The plain capture future retains actual resource ownership.

    def pause_recording(event=None):
        nonlocal recording_state
        if not current() or not busy or not holds or recording_state not in ('opening', 'recording'):
            return
        paused.set()
        recording_state = 'pausing'
        status.SetValue('Pause requested. Waiting for microphone and monitoring cleanup before acknowledgement.')
        controls()

    def resume_recording(event=None):
        nonlocal recording_state
        if (not current() or not busy or not holds or recording_state != 'paused'
                or stopped.is_set() or cancelled.is_set()):
            return
        paused.clear()
        recording_state = 'opening'
        status.SetValue('Resume requested. Rechecking the same runtime, microphone and optional monitoring output.')
        controls()

    def start(event=None):
        nonlocal busy, generation, holds, active_future, recording_state
        if busy or not current():
            return
        index = microphone.GetSelection()
        if not 0 <= index < len(devices):
            status.SetValue('Select a microphone before Start.')
            return
        try:
            seconds = minutes.GetValue() * 60
            rate, channel_count = int(sample_rate.GetStringSelection()), channels.GetSelection() + 1
            options = RecordingOptions(max_seconds=seconds, sample_rate=rate, channels=channel_count,
                max_bytes=min(2 * 1024**3, seconds * rate * channel_count * 2 + 1024**2))
            requested_title = title.GetValue()
            device_id = devices[index].id
            selected_monitor = None
            if monitor_enabled.GetValue():
                output_index = monitor_output.GetSelection()
                if not 0 <= output_index < len(outputs):
                    raise ValueError('Explicitly select a currently enumerated monitoring output before Start.')
                selected_monitor = outputs[output_index]
            cancelled.clear()
            stopped.clear()
            paused.clear()
            recording_state = 'waiting'
            generation += 1
            current_generation = generation
            active_future = None  # A waiting quiet hold belongs to no old completed future.
            busy = True
            controls()
            cancel_content_request()
            player.stop()
            for controller in controllers:
                holds.append(controller.hold_audio(recording=True))
            controls()
            status.SetValue('Waiting for playback and progress audio to confirm silence before opening the microphone.')
            deadline = time.monotonic() + 3

            def prepare():
                nonlocal busy, recording_state
                if not current():
                    busy = False
                    cancelled.set()
                    release_holds()
                    if alive:
                        dialog.EndModal(wx.ID_CANCEL)
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
                        later(25, prepare)
                    return

                def operation(report):
                    player.close()
                    return service.capture(device_id, title=requested_title, options=options,
                        cancelled=operation_cancelled, stop_requested=stopped.is_set,
                        progress=report, pause_requested=paused.is_set,
                        state_changed=lambda value: capture_notice(value, current_generation),
                        monitor_output=selected_monitor)

                def complete(value):
                    show_records((*records, value), value['id'])
                    status.SetValue('Saved and verified. The recording is retained as a new Studio input.')
                recording_state = 'opening'
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
        if busy or not current() or not 0 <= index < len(records):
            return
        cancelled.clear()
        current_generation = generation
        cancel_content_request()
        def complete(value):
            nonlocal content_request
            content_request = progress.media_action(lambda: player.play(value['path']),
                alive=lambda: current() and generation == current_generation,
                failed=lambda exc: status.SetValue(str(exc)) if current() else None)
        run(lambda: service.get(records[index]['id'], cancelled=operation_cancelled), complete, 'Verifying saved recording')

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
        run(lambda: service.get(records[index]['id'], cancelled=operation_cancelled), complete, 'Verifying selected recording')

    def close(event=None):
        nonlocal closing, generation
        generation += 1
        cancel_content_request()
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
        cancel_content_request()
        player.stop()
        progress.tick()

    def stop_recording(event=None):
        nonlocal cleanup_retry
        stopped.set()
        paused.clear()
        if not current():
            return
        controls()
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
        paused.clear()
        if not current():
            return
        controls()
        if busy:
            status.SetValue('Cancellation requested. Waiting for the microphone and owned processes to stop.')

    for key, callback in (('runtime', choose_runtime), ('refresh', refresh), ('refresh_outputs', refresh_outputs),
            ('start', start), ('pause', pause_recording), ('resume', resume_recording), ('stop', stop_recording),
            ('cancel', cancel_recording), ('preview', preview), ('stop_preview', stop_playback),
            ('use', use), ('close', close)):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    microphone.Bind(wx.EVT_CHOICE, lambda event: controls())
    monitor_enabled.Bind(wx.EVT_CHECKBOX, lambda event: controls())
    monitor_output.Bind(wx.EVT_CHOICE, lambda event: controls())
    saved.Bind(wx.EVT_CHOICE, display_record)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        controls()
        cancelled.clear()
        def loaded_records(result):
            values, unverified = result
            show_records(values)
            if unverified:
                status.SetValue(f'{len(unverified)} retained original(s) lack a verified receipt or a finalized capture. '
                    f'They remain unselected and unplayed in {service.root}. Complete backups preserve them for explicit recovery.')
            else:
                status.SetValue('Saved recording receipts loaded. Refresh microphones and choose a device explicitly before Start.')
        run(lambda: (service.list(cancelled=operation_cancelled), unverified_recording_files(library.root, cancelled=operation_cancelled)),
            loaded_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancelled.set()
        generation += 1
        executor.shutdown(wait=False, cancel_futures=True)
        cancel_content_request()
        for timer in tuple(timers):
            timer.Stop()
        timers.clear()
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
            if silence_confirmed:
                for unregister in unregister_players:
                    unregister()
            else:
                from ..audio.cleanup import pending_cleanup
                def remove_player():
                    for unregister in unregister_players:
                        unregister()
                # The shared cleanup owner strongly retains the existing
                # player until it confirms output silence.
                pending_cleanup(player.close,
                    message='Recording preview cleanup is pending.',
                    callbacks=(lambda: wx.CallAfter(remove_player),))
            release_when_stopped()
            dialog.Destroy()
    return selected_result




def create_recording_project(library_root, identity, *, title, cancelled=lambda: False):
    """Copy one verified recording into the existing managed Studio atomically.

    The capture and receipt remain immutable in their own typed namespace. This
    independent asset is an editable-production input, never a device authority.
    """
    import os
    from pathlib import Path
    import shutil
    from tempfile import mkdtemp

    from ..audio.jobs import (checked_directory, file_identity, regular_file,
                              sha256_file, publish_new, publish_noreplace)
    from ..audio.managed import ManagedProjects
    from ..audio.recording import recording_reference
    from ..ingest.writer_guard import exclusive_writer
    from ..operations._state import atomic_json
    from .audio_studio import StudioController

    root = checked_directory(library_root)
    service = RecordingService(root)
    selected = service.get(identity, cancelled=cancelled)
    reference = recording_reference(selected)
    asset = reference['asset']
    script = '[sound ' + asset + ']\n'
    controller = StudioController(private_root=root)
    controller.new(script, title=title, input_kind='script', source_reference=reference,
                   source_id='recording:' + identity + ':' + selected['audio']['sha256'])
    managed = ManagedProjects(root)
    destination, document, output = managed.paths(controller.project_id)
    controller.output_dir = output
    source = Path(selected['path'])
    expected = file_identity(regular_file(source))
    if sha256_file(source, cancelled=cancelled, expected_identity=expected) != selected['audio']['sha256']:
        raise ValueError('The selected recording changed before creating its Studio input.')
    stage = None
    with exclusive_writer(root, timeout=0):
        base = checked_directory(root / 'audio-projects', create=True)
        if os.path.lexists(destination):
            raise FileExistsError('The new managed project identity already exists.')
        try:
            stage = Path(mkdtemp(prefix='.recording-project-', dir=base))
            stage_info = stage.stat()
            assets = checked_directory(stage / 'audio' / 'assets', create=True)
            checked_directory(stage / 'studio', create=True)
            publish_new(source, assets / asset, cancelled=cancelled, expected_identity=expected)
            if sha256_file(assets / asset, cancelled=cancelled) != selected['audio']['sha256']:
                raise ValueError('The copied Studio input differs from the selected recording.')
            atomic_json(stage / 'studio' / 'project.json', controller.to_dict())
            if cancelled():
                raise InterruptedError('Creating the recording project was cancelled before publication.')
            publish_noreplace(stage, destination)
            stage = None
        finally:
            if stage is not None and os.path.lexists(stage):
                current = checked_directory(stage).stat()
                if (current.st_dev, current.st_ino) != (stage_info.st_dev, stage_info.st_ino):
                    raise ValueError('The private recording project stage changed identity.')
                shutil.rmtree(stage)
    return controller.open(document)


class RecordingActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        try:
            return self._open(event)
        except Exception as exc:
            self.frame.report('Recording could not open: ' + str(exc))

    def _open(self, event=None):
        import wx
        frame, library = self.frame, self.frame.library
        selected = show_recording(frame)
        if (selected is None or getattr(frame, '_closing', False)
                or frame.library is not library):
            return
        # A legacy receipt may have a longer title than the current Studio.
        # Confirm the new project title without altering the original receipt.
        prompt = wx.TextEntryDialog(frame, 'Title of the new editable Audio Studio project',
                                    'Use the verified microphone recording',
                                    value=selected['title'][:256])
        try:
            if prompt.ShowModal() != wx.ID_OK or frame.library is not library:
                return
            title = prompt.GetValue()
        finally:
            prompt.Destroy()

        def current():
            return frame.library is library and not getattr(frame, '_closing', False)

        def cancelled():
            return frame.cancel_event.is_set() or not current()

        def created(controller):
            if current() and not frame.cancel_event.is_set():
                from .audio_studio import AudioStudioDialog
                AudioStudioDialog(frame, controller).show()

        frame.run_job('Preparing a separate Studio input from the verified recording',
            lambda: create_recording_project(library.root, selected['id'], title=title,
                                             cancelled=cancelled),
            created)


def install_recording_actions(frame):
    """Use one semantic command and the current Audio menu, with no hotkey clash."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'recording_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() in ('audio', 'audio studio')), None)
    if menu is None:
        raise ValueError('Install the shared Audio menu before recording actions.')
    actions = RecordingActions(frame)
    item = menu.Append(wx.ID_ANY, 'Record &microphone and manage saved inputs...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('audio.recording', 'Record microphone and manage saved inputs', 'main'), item)
    frame.recording_actions = actions
    return actions
