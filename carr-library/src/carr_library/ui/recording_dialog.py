"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from ..audio.models import AudioCancelled
from ..audio.recording import RecordingOptions, RecordingService
from ..audio.process import CaptureCleanupError
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from ..progress import ProgressState, bind_progress, current_progress
from .progress_panel import NativeProgressPanel

_UNCONFIRMED_RECORDING_HOLDS = []


def show_recording(frame, *, on_selected=None):
    import wx
    if getattr(frame, 'working', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    dialog = wx.Dialog(frame, title='Local microphone recording', size=(820, 740))
    dialog.library = library
    panel = wx.ScrolledWindow(dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(panel, label='Refresh devices and select a microphone, then choose Start. '
        'Stop saves a verified recording; Cancel discards an unfinished recording. '
        'No microphone opens automatically. Recording uses the selected Windows device locally.'),
        0, wx.EXPAND | wx.ALL, 8)

    def field(label, factory, **kwargs):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.TOP, 8)
        control = factory(panel, **kwargs)
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
                 ('start', 'Start recording')),
                (('pause', 'Pause microphone recording'), ('resume', 'Resume microphone recording')),
                (('stop', 'Stop and save recording'), ('cancel', 'Cancel unfinished recording')),
                (('preview', 'Listen to saved recording'), ('stop_preview', 'Stop playback'),
                 ('use', 'Use selected recording'), ('close', 'Close'))):
        box = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(panel, label=label)
            button.SetName(label)
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    panel.SetSizer(layout)
    outer = wx.BoxSizer(wx.VERTICAL)
    outer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizer(outer)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    dialog.host, dialog.layout, dialog.working = panel, layout, False
    parent_progress = getattr(frame, 'progress_panel', None)
    dialog.progress_speech_provider = None
    selected_provider = getattr(parent_progress, 'speech_provider', None)
    if callable(getattr(selected_provider, 'copy_selection', None)):
        try:
            dialog.progress_speech_provider = selected_provider.copy_selection()
        except (ValueError, OSError, RuntimeError) as exc:
            status.SetValue('Select a progress voice again: ' + str(exc))
    progress = NativeProgressPanel(dialog)
    dialog.progress_panel = dialog.operation_progress = progress
    if parent_progress is not None:
        progress.hub = parent_progress.hub
    layout.Detach(progress.panel)
    layout.Insert(layout.GetItemCount() - 2, progress.panel, 0, wx.EXPAND | wx.ALL, 6)
    progress.gauge.SetName('Recording operation progress')
    progress.text.SetName('Recording current progress and phase')
    progress.query_button.SetName('Query recording progress')
    panel.FitInside()
    controllers = tuple(item for item in (progress, parent_progress) if item is not None)
    service = RecordingService(library.root)
    player = WavePlayer()
    for controller in controllers:
        controller.register_player(player)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='carr-recording')
    cancelled, stopped, paused = threading.Event(), threading.Event(), threading.Event()
    busy, alive, closing, devices, records, holds, selected_result = False, True, False, (), (), [], None
    monitor_outputs, capture_phase = (), 'idle'
    dialog.capture_state = capture_phase
    generation = 0
    active_future = None

    def controls():
        dialog.working = busy
        for control in (microphone, title, minutes, sample_rate, channels, saved, monitoring):
            control.Enable(not busy)
        monitor_output.Enable(not busy and monitoring.GetValue())
        buttons['refresh'].Enable(not busy)
        buttons['refresh_monitor'].Enable(not busy)
        monitor_ready = not monitoring.GetValue() or 0 <= monitor_output.GetSelection() < len(monitor_outputs)
        buttons['start'].Enable(not busy and monitor_ready and 0 <= microphone.GetSelection() < len(devices))
        buttons['stop'].Enable(busy and bool(holds))
        buttons['cancel'].Enable(busy)
        buttons['pause'].Enable(busy and capture_phase == 'recording' and not paused.is_set())
        buttons['resume'].Enable(busy and capture_phase == 'paused' and paused.is_set())
        for key in ('preview', 'use'):
            buttons[key].Enable(not busy and 0 <= saved.GetSelection() < len(records))
        buttons['stop_preview'].Enable(not busy)

    def release_holds():
        nonlocal holds
        if (active_future is not None and active_future.done() and not active_future.cancelled()
                and isinstance(active_future.exception(), CaptureCleanupError)):
            # A returned Future does not prove native cleanup. Retain quiet
            # ownership with the service's conservative microphone latch.
            _UNCONFIRMED_RECORDING_HOLDS.extend(holds)
            holds = []
            return
        current, holds = holds, []
        for hold in current:
            try:
                hold.close()
            except Exception as exc:
                progress.reporting_error = 'Progress audio release needs attention: ' + str(exc)
                progress.notice = progress.reporting_error

    def release_when_stopped():
        # An exceptional modal exit still cancels the worker. Keep the parent
        # muted until its capture has actually terminated; hold.close marshals
        # back to the owning UI thread when called by the completion callback.
        if active_future is not None and not active_future.done():
            active_future.add_done_callback(lambda future: release_holds())
        else:
            release_holds()

    def display_record(event=None):
        index = saved.GetSelection()
        if 0 <= index < len(records):
            value = records[index]
            audio = value['audio']
            details.SetValue(f"{value['title']}\n{audio['duration_seconds']:.2f} seconds; "
                f"{audio['sample_rate']} Hz; {audio['channels']} channel(s).\n{value['path']}\n"
                'Playback and selection verify the retained bytes before use.'
                + ('\n' + value['timeline'] if value.get('schema') == 'carr-recording/2' else ''))
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
        if not alive or busy and not prepared:
            return
        busy = True
        controls()
        observer_error = None

        def observe(callback):
            nonlocal observer_error
            try:
                return callback()
            except Exception as exc:
                observer_error = exc
                progress.reporting_error = 'Recording progress feedback is unavailable: ' + str(exc)
                progress.notice = progress.reporting_error

        task = observe(lambda: progress.begin(phase, unit='milliseconds' if measured else 'units'))

        def measured_progress(seconds):
            if (task is not None and not cancelled.is_set()
                    and task.snapshot().state == ProgressState.RUNNING):
                # The producer reports actual captured frames as seconds. The
                # common contract uses integral units and keeps unknown totals.
                observe(lambda: task.update(completed=int(seconds * 1000),
                    phase=f'Recorded {seconds:.1f} seconds'))

        def execute():
            if task is None:
                return operation(measured_progress) if measured else operation()
            with bind_progress(task):
                return operation(measured_progress) if measured else operation()

        def finish_progress(outcome, message):
            if observer_error is None:
                observe(lambda: progress.finish(task, outcome, message))
            if observer_error is not None and task is not None:
                observe(lambda: progress.reporting_unavailable(task, outcome, progress.reporting_error))

        status.SetValue(phase)
        try:
            future = executor.submit(execute)
            active_future = future
        except Exception:
            busy = False
            release_holds()
            controls()
            raise

        def finished(future):
            def apply():
                nonlocal busy, capture_phase
                if not alive:
                    return
                busy = False
                capture_phase = dialog.capture_state = 'idle'
                release_holds()
                try:
                    result = future.result()
                    complete(result)
                    finish_progress('succeeded', phase + ': verified complete')
                except (AudioCancelled, InterruptedError):
                    finish_progress('cancelled', 'Recording operation cancelled; unfinished audio discarded.')
                    status.SetValue('Cancelled. Previous saved recordings are preserved.')
                except Exception as exc:
                    finish_progress('failed', 'Recording operation failed')
                    status.SetValue(str(exc))
                controls()
                if closing:
                    dialog.EndModal(wx.ID_CANCEL)
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

    def refresh_monitor(event=None):
        nonlocal monitor_outputs
        if busy:
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
        run(lambda: service.monitoring_outputs(cancelled=cancelled.is_set), complete,
            'Listing monitoring outputs without playing audio')

    def start(event=None):
        nonlocal busy, generation, holds, capture_phase
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
            output = None
            if monitoring.GetValue():
                output_index = monitor_output.GetSelection()
                if not 0 <= output_index < len(monitor_outputs):
                    raise ValueError('Select a monitoring output explicitly before Start.')
                output = monitor_outputs[output_index]
            cancelled.clear()
            stopped.clear()
            paused.clear()
            capture_phase = dialog.capture_state = 'opening'
            generation += 1
            current_generation = generation
            busy = True
            controls()
            for controller in controllers:
                holds.append(controller.hold_audio(recording=True))
            controls()
            deadline = time.monotonic() + 3

            def prepare():
                nonlocal busy
                if cancelled.is_set() or stopped.is_set() or not alive or current_generation != generation:
                    busy = False
                    release_holds()
                    controls()
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
                    def state_changed(snapshot):
                        task = current_progress()
                        if task is not None:
                            try:
                                if snapshot.state == 'paused' and task.snapshot().state == ProgressState.RUNNING:
                                    task.pause('Microphone and monitoring are closed; paused time is not recorded.')
                                elif snapshot.state != 'paused' and task.snapshot().state == ProgressState.PAUSED:
                                    task.resume('Preparing the next recording operation.')
                            except Exception as exc:
                                progress.reporting_error = 'Recording progress feedback is unavailable: ' + str(exc)
                        def apply_state():
                            nonlocal capture_phase
                            if (not alive or not busy or current_generation != generation
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
                        cancelled=cancelled.is_set, stop_requested=stopped.is_set,
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
        nonlocal capture_phase
        stopped.set()
        capture_phase = dialog.capture_state = 'stopping'
        controls()

    def cancel_recording(event=None):
        nonlocal capture_phase
        cancelled.set()
        capture_phase = dialog.capture_state = 'cancelling'
        controls()

    def pause_recording(event=None):
        nonlocal capture_phase
        if busy and capture_phase == 'recording' and not stopped.is_set() and not cancelled.is_set():
            paused.set()
            capture_phase = dialog.capture_state = 'pausing'
            status.SetValue('Pause requested. Waiting for the microphone and monitoring to close.')
            controls()

    def resume_recording(event=None):
        nonlocal capture_phase
        if busy and capture_phase == 'paused' and not stopped.is_set() and not cancelled.is_set():
            paused.clear()
            capture_phase = dialog.capture_state = 'opening'
            status.SetValue('Resume requested. Reopening the explicitly selected microphone.')
            controls()

    for key, callback in (('refresh', refresh), ('refresh_monitor', refresh_monitor),
            ('start', start), ('stop', stop_recording), ('pause', pause_recording), ('resume', resume_recording),
            ('cancel', cancel_recording), ('preview', preview), ('stop_preview', stop_playback),
            ('use', use), ('close', close)):
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    microphone.Bind(wx.EVT_CHOICE, lambda event: controls())
    monitoring.Bind(wx.EVT_CHECKBOX, lambda event: controls())
    monitor_output.Bind(wx.EVT_CHOICE, lambda event: controls())
    saved.Bind(wx.EVT_CHOICE, display_record)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        controls()
        cancelled.clear()
        run(lambda: service.list(cancelled=cancelled.is_set), show_records, 'Loading saved recording receipts')
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
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'audio studio'), None)
    if menu is None:
        raise RuntimeError('Install the shared Audio Studio menu before recording actions.')
    item = menu.Append(wx.ID_ANY, 'Record microphone and manage saved inputs...')
    frame.Bind(wx.EVT_MENU, lambda event: show_recording(frame), item)
    register_menu_command(frame, CommandSpec('audio.record_microphone', 'Record microphone and manage saved inputs'), item)
