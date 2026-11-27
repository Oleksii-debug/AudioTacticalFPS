"""Explicit recording controls adapted from PR123, using the current host worker.

No recorder or model starts when the dialog opens. Native controls remain
available during capture, including measured progress, Stop, Cancel and Close.
"""
from dataclasses import dataclass, field
import threading
import time

from ..audio.recording import (AudioCancelled, AudioCleanupPending, RecordingOptions,
                               RecordingService, unverified_recording_files)
from ..speech.player import WavePlayer
from ..speech.output_devices import output_devices
from ..speech.progress_speech import play_when_quiet
from .audio_runtime_setup import _bind_modal_progress_query


@dataclass(frozen=True)
class _VerifiedRecording:
    """An actual service result, delivered only by the host's completed Future."""
    value: object
    status: str = field(default='completed', init=False)


def show_recording(frame, *, on_selected=None, project_id=None, window_parent=None,
                   selection_label='&Add to saved Studio project', ffmpeg=None):
    import wx
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    progress = getattr(frame, 'operation_progress', None)
    service = RecordingService(library.root, ffmpeg=ffmpeg)
    player = WavePlayer()
    dialog = wx.Dialog(window_parent or frame, title='Local microphone recording', size=(860, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.library = library
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Refresh devices and select a microphone, then choose Start. '
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
    minutes = field('Maximum recording duration in minutes', wx.SpinCtrl, min=1, max=360, initial=10)
    sample_rate = field('Recorded 16-bit PCM WAV sample rate', wx.Choice, choices=['48000', '44100', '24000', '16000'])
    sample_rate.SetSelection(0)
    channels = field('Recorded WAV channels', wx.Choice, choices=['Mono', 'Stereo'])
    channels.SetSelection(0)
    monitoring = field('Dry monitoring during capture (off by default)', wx.CheckBox,
                       label='Enable dry monitoring (&D)')
    monitoring.SetValue(False)
    monitor_output = field('Monitoring output (explicit selection required)', wx.Choice, choices=[])
    fields.Add(wx.StaticText(body, label='Optional monitoring plays the same captured PCM through '
        'the selected output. Use headphones to avoid feedback. The saved recording is unchanged.'),
        0, wx.EXPAND | wx.ALL, 8)
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
    for row in ((('refresh', '&Refresh microphones'), ('query', 'Current operation &progress')),
                (('refresh_outputs', 'Refresh monitoring outputs (&V)'),),
                (('start', '&Start recording'), ('retake', 'Record retake of selected recording (&U)'),
                 ('stop', 'Stop and save recording (&T)'), ('cancel', '&Cancel unfinished recording')),
                (('preview', '&Listen to saved recording'), ('stop_preview', 'Stop playback (&X)'),
                 ('use', selection_label), ('close', 'Cl&ose'))):
        box = wx.WrapSizer(wx.HORIZONTAL)
        for key, label in row:
            button = wx.Button(dialog, label=label)
            button.SetName({'use': 'Use selected recording', 'retake': 'Record retake of selected recording',
                            'stop': 'Stop and save recording', 'stop_preview': 'Stop playback',
                            'refresh_outputs': 'Refresh monitoring outputs'}.get(
                            key, label.replace('&', '')))
            buttons[key] = button
            box.Add(button, 0, wx.ALL, 4)
        layout.Add(box, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.status = status
    dialog.report = lambda message: status.SetValue(message)
    try:
        if progress is not None:
            progress.register_player(player)
    except BaseException:
        player.close()
        dialog.Destroy()
        raise
    cancelled, stopped = threading.Event(), threading.Event()
    active_started, active_finished = threading.Event(), threading.Event()
    busy, alive, closing, devices, records, holds, selected_result = (
        False, True, False, (), (), [], None)
    generation = 0
    outputs = ()
    recording = False
    cleanup_owner = cleanup_retry = None
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
        has_output = not monitoring.GetValue() or 0 <= monitor_output.GetSelection() < len(outputs)
        for control in (microphone, title, minutes, sample_rate, channels, saved, monitoring):
            control.Enable(not busy)
        monitor_output.Enable(not busy and bool(monitoring.GetValue()))
        buttons['refresh'].Enable(not busy)
        buttons['refresh_outputs'].Enable(not busy)
        buttons['start'].Enable(not busy and has_device and has_output)
        buttons['retake'].Enable(not busy and has_device and has_record and has_output)
        buttons['stop'].Enable(busy and recording)
        buttons['cancel'].Enable(busy)
        for key in ('preview', 'use'):
            buttons[key].Enable(not busy and has_record)
        buttons['stop_preview'].Enable(not busy)
        buttons['query'].Enable(progress is not None)

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
        shared_report = [lambda *args: None]

        def measured_progress(seconds):
            shared_report[0](seconds, None, f'Recorded {seconds:.1f} seconds')
            def display():
                if current() and busy and expected_generation == generation:
                    gauge.Pulse()
                    progress_label.SetLabel(f'Recorded {seconds:.1f} seconds; total unknown')
            try:
                wx.CallAfter(display)
            except RuntimeError:
                pass

        def perform(report):
            nonlocal cleanup_owner, cleanup_saved
            shared_report[0] = report
            active_started.set()
            try:
                if cancellation():
                    raise AudioCancelled('Recording operation cancelled before it started.')
                value = operation(measured_progress) if measured else operation()
                return _VerifiedRecording(value)
            except AudioCancelled as exc:
                raise InterruptedError(str(exc)) from exc
            except AudioCleanupPending as exc:
                cleanup_owner = exc.cleanup_owner
                cleanup_saved = getattr(exc, 'saved_recording', None)
                cleanup_owner.add_quiescent_callback(lambda: wx.CallAfter(settled, expected_generation))
                raise
            finally:
                active_finished.set()
                try:
                    wx.CallAfter(settled, expected_generation)
                except RuntimeError:
                    pass

        def arrived(outcome=None, error=None):
            nonlocal busy, delivered, recording
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
                with_progress=True, discard=lambda value: release_when_stopped())
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
        nonlocal outputs, generation
        if busy:
            return
        generation += 1
        outputs = ()
        monitor_output.SetItems([])
        cancelled.clear()
        def complete(values):
            nonlocal outputs
            outputs = tuple(values)
            monitor_output.SetItems([item.name for item in outputs])
            monitor_output.SetSelection(wx.NOT_FOUND)
            status.SetValue('Choose a monitoring output explicitly. Discovery did not open '
                            'an output or start a microphone.')
        run(lambda: output_devices(cancelled=cancellation), complete,
            'Listing local monitoring outputs')

    def start(event=None, *, retake_of=None):
        nonlocal busy, generation, holds, cleanup_owner, cleanup_saved, recording
        if busy or not current():
            return
        if getattr(frame, 'working', False):
            status.SetValue('Finish the current operation before starting a recording.')
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
            requested_output = None
            if monitoring.GetValue():
                output_index = monitor_output.GetSelection()
                if not 0 <= output_index < len(outputs):
                    status.SetValue('Refresh monitoring outputs and explicitly select one before Start.')
                    return
                requested_output = outputs[output_index]
            cancelled.clear()
            frame.cancel_event.clear()
            stopped.clear()
            active_started.clear()
            active_finished.clear()
            cleanup_owner = cleanup_saved = None
            generation += 1
            current_generation = generation
            busy = recording = True
            controls()
            player.stop()
            if progress is not None:
                holds.append(progress.hold_audio(recording=True))
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
                if cancellation() or stopped.is_set() or current_generation != generation:
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

                def operation(report):
                    return service.capture(device_id, title=requested_title, options=options,
                        cancelled=cancellation, stop_requested=stopped.is_set, progress=report,
                        retake_of=retake_of, monitor_output=requested_output)

                def complete(value):
                    show_records((*records, value), value['id'])
                    status.SetValue('Saved and verified. The original recording is retained.')
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
            play_when_quiet((progress,), lambda: player.play(value['path']),
                schedule=wx.CallLater,
                alive=lambda: current() and generation == current_generation,
                on_error=lambda exc: status.SetValue(str(exc)) if current() else None)
        run(lambda: service.get(identity, cancelled=cancellation), complete,
            'Verifying saved recording')

    def use(event=None):
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        identity = records[index]['id']
        if on_selected is not None:
            def selected():
                value = service.get(identity, cancelled=cancellation)
                if project_id is not None:
                    value = dict(value, retained=service.retain(identity, project_id, cancelled=cancellation))
                return value
            def complete(value):
                nonlocal selected_result
                if cancellation():
                    return
                on_selected(value)
                selected_result = value
                dialog.EndModal(wx.ID_OK)
            run(selected, complete, 'Verifying selected recording')
            return
        from ..audio.projects import ProjectStore
        store = ProjectStore(library.root)
        def projects():
            values = store.list_projects()
            return tuple(store.load(row['id']) for row in values
                         if project_id is None or row['id'] == project_id)
        def choose(projects):
            if cancellation():
                return
            if not projects:
                status.SetValue('Save a Studio project before adding a recording. The original remains retained.')
                return
            picker = wx.SingleChoiceDialog(dialog, 'Choose a saved Studio project', 'Add recording',
                                           [project.title for project in projects])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                index = picker.GetSelection()
                if not 0 <= index < len(projects):
                    raise ValueError('Choose an existing project.')
                project = projects[index]
            finally:
                picker.Destroy()
            if cancellation():
                return
            picker = wx.SingleChoiceDialog(dialog, 'Append the recording after which chapter?', 'Add recording',
                                           [scene.title for scene in project.scenes])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                index = picker.GetSelection()
                if not 0 <= index < len(project.scenes):
                    raise ValueError('Choose an existing chapter.')
                scene = project.scenes[index]
            finally:
                picker.Destroy()
            # Both modal decisions can outlive cancellation or the current scope.
            if cancellation():
                return
            run(lambda: service.attach(identity, project.id, scene.id,
                    expected_fingerprint=project.fingerprint, cancelled=cancellation),
                lambda result: status.SetValue('Recording added to ' + result.title
                    + '. Source text is unchanged; reopen Studio to inspect the new project revision.'),
                'Retaining verified recording in Studio')
        run(projects, choose, 'Loading saved Studio projects')

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
        nonlocal generation
        generation += 1
        player.stop()
        if progress is not None:
            progress.refresh_audio_state()

    def stop_recording(event=None):
        nonlocal cleanup_retry
        if not busy or not current():
            return
        stopped.set()
        if cleanup_owner is not None and not cleanup_owner.quiescent:
            if cleanup_retry is None or not cleanup_retry.is_alive():
                cleanup_retry = threading.Thread(target=cleanup_owner.close,
                    name='recording-cleanup-retry', daemon=True)
                cleanup_retry.start()
            status.SetValue('Retrying microphone cleanup. Media stays muted until silence is confirmed.')
        elif busy:
            status.SetValue('Stop requested. Waiting for WAV finalization and owned process cleanup.')

    def cancel_recording(event=None):
        if not busy or not current():
            return
        cancelled.set()
        frame.cancel_event.set()
        if busy:
            status.SetValue('Cancellation requested. Waiting for the microphone and owned processes to stop.')

    def query_progress(event=None):
        if progress is not None and current():
            progress.query(event)
            # Keep an explicit progress query reachable inside this modal view.
            status.SetValue(frame.status.GetValue())
            status.SetFocus()

    _bind_modal_progress_query(frame, dialog, query_progress)
    for key, callback in (('refresh', refresh), ('refresh_outputs', refresh_outputs),
            ('query', query_progress), ('start', start),
            ('retake', retake), ('stop', stop_recording),
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
            if progress is not None:
                progress.unregister_player(player, silence_confirmed=silence_confirmed)
            release_when_stopped()
            dialog.Destroy()
    return selected_result

