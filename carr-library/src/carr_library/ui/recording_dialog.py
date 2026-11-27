"""Keyboard-addressable explicit microphone capture; no capture on dialog open."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from ..audio.models import AudioCancelled
from ..audio.recording import RecordingOptions, RecordingService
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet
from .operation_progress import OperationProgress


def show_recording(frame, *, on_selected=None):
    import wx
    if getattr(frame, 'working', False):
        frame.report('Finish the current operation before opening microphone recording.')
        return None
    library = frame.library
    class RecordingDialog(wx.Dialog):
        def Destroy(self):
            if getattr(self, '_destroy_requested', False):
                return False
            self._destroy_requested = True
            dispose = getattr(self, 'dispose', None)
            if dispose is not None:
                dispose()
            return super().Destroy() if self else False

    dialog = RecordingDialog(frame, title='Local microphone recording', size=(820, 740))
    native_id = dialog.GetId()
    dialog._native_id = native_id
    dialog._destroy_requested = False
    dialog.recording_alive = True
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
    progress = OperationProgress(dialog, gauge, progress_label, wx_module=wx)
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
    dialog.recording_cancelled = cancelled

    def dispose():
        # Destruction may arrive through another Python wrapper or from a
        # native parent. This latch never calls controls or native window APIs.
        nonlocal alive, closing, generation
        if not alive:
            return
        alive, closing = False, True
        dialog.recording_alive = False
        cancelled.set()
        stopped.set()
        generation += 1

    def destroyed(event):
        if event.GetId() == native_id:
            dispose()
        event.Skip()

    def report(message):
        if alive:
            status.SetValue(str(message))

    dialog.dispose = dispose
    dialog.report = report
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)

    def controls():
        if not alive:
            return
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
        current, holds = holds, []
        for hold in current:
            try:
                hold.close()
            except Exception as exc:
                progress.reporting_error = 'Progress audio release needs attention: ' + str(exc)

    def release_when_stopped():
        # An exceptional modal exit still cancels the worker. Keep the parent
        # muted until its capture has actually terminated; hold.close marshals
        # back to the owning UI thread when called by the completion callback.
        if active_future is not None and not active_future.done():
            active_future.add_done_callback(lambda future: release_holds())
        else:
            release_holds()

    def display_record(event=None):
        if not alive:
            return
        index = saved.GetSelection()
        if 0 <= index < len(records):
            value = records[index]
            audio = value['audio']
            details.SetValue(f"{value['title']}\n{audio['duration_seconds']:.2f} seconds; "
                f"{audio['sample_rate']} Hz; {audio['channels']} channel(s).\n{value['path']}\n"
                'Receipt: ' + value.get('receipt_status', value.get('status', 'complete'))
                + '. Playback and selection verify the retained bytes before use.'
                + ('\n' + '\n'.join(value.get('warnings', ())) if value.get('warnings') else ''))
        controls()

    def show_records(values, select_id=None):
        nonlocal records
        if not alive:
            return
        records = tuple(values)
        saved.SetItems([f"{row['title']} ({row['created'][:19]})" for row in records])
        selected = next((index for index, row in enumerate(records) if row['id'] == select_id), wx.NOT_FOUND)
        saved.SetSelection(selected)
        display_record()

    def run(operation, complete, phase, *, prepared=False, measured=False):
        nonlocal busy, active_future
        if not alive:
            return
        if busy and not prepared:
            return
        busy = True
        controls()
        def observe(method, *args, **kwargs):
            if not alive:
                return None
            try:
                return getattr(progress, method)(*args, **kwargs)
            except Exception as exc:
                progress.reporting_error = 'Recording progress feedback is unavailable: ' + str(exc)
        token = observe('start', phase)
        def measured_progress(seconds):
            if token is not None:
                observe('update', seconds, None, f'Recorded {seconds:.1f} seconds', token=token)
        report(phase)
        try:
            future = executor.submit(lambda: operation(measured_progress)) if measured else executor.submit(operation)
            active_future = future
        except Exception:
            busy = False
            release_holds()
            controls()
            raise

        def finished(future):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = False
                release_holds()
                try:
                    result = future.result()
                    complete(result)
                    if token is not None:
                        observe('succeed', phase + ': verified complete', token=token)
                except (AudioCancelled, InterruptedError):
                    if token is not None:
                        observe('cancel', 'Recording operation cancelled; unfinished audio discarded.', token=token)
                    report('Cancelled. Previous saved recordings are preserved.')
                except Exception as exc:
                    if token is not None:
                        observe('fail', 'Recording operation failed', token=token)
                    report(str(exc))
                controls()
                if alive and closing and dialog:
                    dialog.EndModal(wx.ID_CANCEL)
            wx.CallAfter(apply)
        future.add_done_callback(finished)

    def refresh(event=None):
        nonlocal devices, generation
        if not alive:
            return
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
            report('Choose a microphone explicitly, then Start. Device discovery did not record audio.')
        run(lambda: service.devices(cancelled=cancelled.is_set), complete, 'Listing local microphones')

    def start(event=None):
        nonlocal busy, generation, holds
        if not alive:
            return
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
            deadline = time.monotonic() + 3

            def prepare():
                nonlocal busy
                if not alive:
                    return
                if cancelled.is_set() or stopped.is_set() or current_generation != generation:
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
                        report('Progress playback has not confirmed silence; no microphone was opened.')
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
                    message = 'Saved and verified. The recording is retained as a new Studio input.'
                    if value.get('warnings'):
                        message += '\n' + '\n'.join(value['warnings'])
                    report(message)
                run(operation, complete, 'Recording microphone; Stop saves, Cancel discards',
                    prepared=True, measured=True)
            prepare()
        except Exception as exc:
            busy = False
            release_holds()
            controls()
            report(str(exc))

    def preview(event=None):
        if not alive:
            return
        index = saved.GetSelection()
        if busy or not 0 <= index < len(records):
            return
        cancelled.clear()
        current_generation = generation
        def complete(value):
            play_when_quiet(controllers, lambda: player.play(value['path']), schedule=wx.CallLater,
                alive=lambda: alive and not closing and generation == current_generation,
                on_error=lambda exc: report(str(exc)))
        run(lambda: service.get(records[index]['id'], cancelled=cancelled.is_set), complete, 'Verifying saved recording')

    def use(event=None):
        if not alive:
            return
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
        if not alive:
            return
        generation += 1
        if busy:
            closing = True
            cancelled.set()
            report('Closing the microphone and discarding unfinished audio before leaving.')
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()
        else:
            dialog.EndModal(wx.ID_CANCEL)

    def stop_playback(event=None):
        nonlocal generation
        if not alive:
            return
        generation += 1
        player.stop()
        for controller in controllers:
            controller.refresh_audio_state()

    def stop_recording(event=None):
        if not alive:
            return
        stopped.set()

    def cancel_recording(event=None):
        if not alive:
            return
        cancelled.set()

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
        run(lambda: service.list(cancelled=cancelled.is_set), show_records, 'Loading saved recording receipts')
        buttons['refresh'].SetFocus()
        dialog.ShowModal()
    finally:
        dispose()
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
            if dialog:
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
