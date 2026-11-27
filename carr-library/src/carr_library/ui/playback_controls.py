"""Shared native Reader/Studio transport; commands never block the event loop."""
from concurrent.futures import ThreadPoolExecutor
import math
import threading

from ..audio.playback import output_devices
from ..audio.playback_resume import prepare_audio
from ..audio.transport import PlaybackTransport


class _MediaOnUI:
    """Keep the existing progress panel's media ownership on its UI thread."""
    def __init__(self, wx, progress, valid):
        self.wx, self.progress, self.valid = wx, progress, valid
        self._generation = 0
        self._lock = threading.Lock()

    def _current(self, generation):
        with self._lock:
            return generation == self._generation

    def set_media_state(self, **values):
        with self._lock:
            generation = self._generation
        def release():
            if self._current(generation):
                self.progress.set_media_state(**values)
        if self.wx.IsMainThread():
            release()
        else:
            self.wx.CallAfter(release)

    def prepare_media(self, **values):
        with self._lock:
            self._generation += 1
            generation = self._generation
        finished, abandoned = threading.Event(), threading.Event()
        result = [False]

        def prepare():
            try:
                if not abandoned.is_set() and self._current(generation) and self.valid():
                    result[0] = self.progress.prepare_media(**values)
                if abandoned.is_set() and self._current(generation):
                    self.progress.set_media_state(owner=values.get('owner'))
            finally:
                finished.set()
        if self.wx.IsMainThread():
            prepare()
        else:
            self.wx.CallAfter(prepare)
            if not finished.wait(3):
                abandoned.set()
                return False
        return result[0] and not abandoned.is_set() and self._current(generation) and self.valid()


def _seconds(text):
    try:
        value = float(text)
    except (ValueError, TypeError) as exc:
        raise ValueError('Enter a finite number of seconds.') from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError('Enter a nonnegative finite number of seconds.')
    return value


class PlaybackControls:
    """Parent-owned native panel, one bounded command worker, no focus polling."""
    def __init__(self, parent, root, *, guard=lambda: None, progress=None, owner=None, player=None):
        import wx
        self.wx = wx
        self.window = wx.Panel(parent)
        self.window.SetName('Audio playback controls')
        self.alive, self.window_alive, self.busy = True, True, False
        self.epoch = 0
        self.cancelled = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='audio-transport')
        self._guard = guard
        self._stop_pending = False
        self._stop_done = None
        self._last_text = None
        self.buttons, self.native_controls = [], []
        bridge = _MediaOnUI(wx, progress, self.current) if progress is not None else None
        self.controller = PlaybackTransport(root, player=player, guard=self.check,
                                             progress=bridge, owner=owner)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.status = wx.TextCtrl(self.window, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('Playback state, position, duration and timing accuracy')
        self.status.SetMinSize((-1, 90))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.play_button = self._button(row, 'Play / resume saved position', self.play)
        self.pause_button = self._button(row, 'Pause / continue audio', lambda _: self.submit('Pause / continue', self.controller.pause_resume), native=True)
        self.stop_button = self._button(row, 'Stop audio', self.stop)
        query = wx.Button(self.window, label='Read current playback position')
        query.SetName('Read current playback position')
        query.Bind(wx.EVT_BUTTON, self.query)
        row.Add(query, 0, wx.ALL, 3)
        layout.Add(row, 0, wx.EXPAND)
        self.resume = wx.CheckBox(self.window, label='Use saved position only for this exact source and recording')
        self.resume.SetName('Use saved position only for this exact source and recording')
        self.resume.SetValue(True)
        layout.Add(self.resume, 0, wx.ALL, 5)
        self.device_rows = (None,)
        layout.Add(wx.StaticText(self.window, label='Audio output device'), 0, wx.ALL, 4)
        self.device = wx.Choice(self.window, choices=['System default output'])
        self.device.SetName('Audio output device; used when starting playback')
        self.device.SetSelection(0)
        layout.Add(self.device, 0, wx.EXPAND | wx.ALL, 4)
        self.native_controls.append(self.device)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self._button(row, 'Reload audio output devices', self.reload_devices, native=True)
        self.seek = self._field(row, 'Seek to seconds', '0')
        self._button(row, 'Seek to entered position', self.seek_to, native=True)
        self._button(row, 'Back 10 seconds', lambda _: self.submit('Seek back', lambda: self.controller.seek_relative(-10)), native=True)
        self._button(row, 'Forward 10 seconds', lambda _: self.submit('Seek forward', lambda: self.controller.seek_relative(10)), native=True)
        layout.Add(row, 0, wx.EXPAND)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.loop_a = self._field(row, 'Loop A seconds', '0')
        self.loop_b = self._field(row, 'Loop B seconds', '1')
        self._button(row, 'Repeat A to B', self.apply_loop, native=True)
        self._button(row, 'Clear A–B repeat', lambda _: self.submit('Clear loop', self.controller.clear_loop), native=True)
        layout.Add(row, 0, wx.EXPAND)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self.sleep_minutes = self._field(row, 'Sleep timer minutes; zero disables', '0')
        self._button(row, 'Apply sleep timer', self.apply_sleep)
        layout.Add(row, 0, wx.EXPAND)
        layout.Add(wx.StaticText(self.window, label='Rendered chapters or segments'), 0, wx.ALL, 4)
        self.segments = wx.Choice(self.window)
        self.segments.SetName('Rendered chapters or segments; audio boundaries, not word timing')
        layout.Add(self.segments, 0, wx.EXPAND | wx.ALL, 4)
        self.native_controls.append(self.segments)
        row = wx.WrapSizer(wx.HORIZONTAL)
        self._button(row, 'Go to selected chapter', self.seek_segment, native=True)
        layout.Add(row, 0, wx.EXPAND)
        self.window.SetSizer(layout)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer = wx.Timer(self.window)
        self.window.Bind(wx.EVT_TIMER, self.poll, self.timer)
        self.timer.Start(500)
        self._enable()
        self.status.ChangeValue('Prepare a recording. Windows WinMM provides measured pause/seek controls. '
                                'The optional ffplay fallback exposes play/stop and completion without an advancing playhead.')

    def _button(self, row, label, action, *, native=False):
        button = self.wx.Button(self.window, label=label)
        button.SetName(label)
        button.Bind(self.wx.EVT_BUTTON, action)
        row.Add(button, 0, self.wx.ALL, 3)
        self.buttons.append(button)
        if native:
            self.native_controls.append(button)
        return button

    def _field(self, row, label, value):
        row.Add(self.wx.StaticText(self.window, label=label), 0, self.wx.ALL | self.wx.ALIGN_CENTER_VERTICAL, 3)
        field = self.wx.TextCtrl(self.window, value=value, size=(85, -1))
        field.SetName(label)
        row.Add(field, 0, self.wx.ALL, 3)
        return field

    def current(self):
        if not self.alive or not self.window_alive:
            return False
        try:
            self._guard()
            return True
        except Exception:
            return False

    def check(self):
        if not self.current():
            raise ValueError('The playback window, source or data folder changed. Prepare the recording again.')

    def _enable(self):
        if not self.alive or not self.window_alive:
            return
        for button in self.buttons:
            button.Enable(not self.busy)
        for control in self.native_controls:
            control.Enable(not self.busy and self.controller.player.native is not None)
        self.stop_button.Enable()

    def _text(self, message, *, focus=False):
        if not self.alive or not self.window_alive:
            return
        if message != self._last_text:
            self.status.ChangeValue(str(message))
            self._last_text = message
        if focus:
            self.status.SetInsertionPoint(0)
            self.status.SetFocus()

    def submit(self, label, action, done=None, *, quiet=False):
        if not self.alive or not self.window_alive:
            return False
        if self.busy:
            if not quiet:
                self._text('Wait for the current playback command, or choose Stop audio.')
            return False
        self.busy = True
        self.cancelled.clear()
        epoch = self.epoch
        if not quiet:
            self._enable()
            self._text(label + '…')

        def work():
            self.check()
            result = action()
            self.check()
            return result

        def finished(future):
            def apply():
                if not self.alive or not self.window_alive or epoch != self.epoch:
                    return
                self.busy = False
                self._enable()
                try:
                    result = future.result()
                    self.check()
                    if self.cancelled.is_set():
                        raise InterruptedError('Playback command cancelled.')
                    if done is not None:
                        done(result)
                    elif isinstance(result, str):
                        self._text(result)
                except Exception as exc:
                    self._text(str(exc))
                    # A withdrawn root/source is never left playing merely
                    # because its result can no longer update this window.
                    if not self.current():
                        self._stop_pending = True
                if self._stop_pending:
                    self._stop_pending = False
                    self._stop_without_guard()
            if self.alive:
                try:
                    self.wx.CallAfter(apply)
                except RuntimeError:
                    self.cancelled.set()
        try:
            self.executor.submit(work).add_done_callback(finished)
        except Exception as exc:
            self.busy = False
            self._enable()
            self._text(str(exc))
            return False
        return True

    def set_source(self, path, *, owner=None, segments=(), autoplay=False, authorize=lambda: None):
        if not self.current():
            return False
        resume, device = self.resume.GetValue(), self._device()
        def prepare():
            authorize()
            value = prepare_audio(path, owner=owner, guard=self.check,
                                  cancelled=lambda: self.cancelled.is_set() or not self.current())
            self.controller.load(value, segments=segments, authorize=authorize)
            if autoplay:
                return self.controller.play(resume=resume, device_id=device)
            return 'Recording prepared. ' + self.controller.status_text()
        def accepted(message):
            self.segments.SetItems([f'{start:.2f} seconds — {title}' for start, title in segments])
            if segments:
                self.segments.SetSelection(0)
            self._text(message)
        return self.submit('Verifying recording', prepare, accepted)

    def _device(self):
        index = self.device.GetSelection()
        if not 0 <= index < len(self.device_rows):
            raise ValueError('Choose an available audio output device.')
        return self.device_rows[index]

    def play(self, event=None):
        if not self.current():
            return
        try:
            resume, device = self.resume.GetValue(), self._device()
            self.submit('Starting playback', lambda: self.controller.play(resume=resume, device_id=device))
        except Exception as exc:
            self._text(str(exc))

    def _stop_without_guard(self):
        # Stopping owns no source capability and remains valid after root drift.
        self.busy = True
        self._enable()
        def finished(future):
            def apply():
                if not self.alive or not self.window_alive:
                    return
                self.busy = False
                self._stop_pending = False
                self._enable()
                try:
                    self._text(future.result())
                    done, self._stop_done = getattr(self, '_stop_done', None), None
                    if done is not None:
                        done()
                except Exception as exc:
                    self._stop_done = None
                    self._text(str(exc))
            if self.alive:
                self.wx.CallAfter(apply)
        def stopped():
            if not self.current():
                return self.controller.withdraw()
            result = self.controller.stop()
            # Authorization may change while the bounded device stop runs.
            return result if self.current() else self.controller.withdraw()
        try:
            self.executor.submit(stopped).add_done_callback(finished)
        except RuntimeError as exc:
            self.busy = False
            self._enable()
            self._text(str(exc))

    def stop(self, event=None, *, on_stopped=None):
        if not self.alive:
            return
        if on_stopped is not None:
            self._stop_done = on_stopped
        self.cancelled.set()
        if self.busy:
            self._stop_pending = True
            self._text('Stop requested. Waiting for the current bounded command to stop.')
        else:
            self._stop_without_guard()
            self._text('Stop requested; the next status update confirms the device state.')

    def quiesce(self):
        """A producer must not start while this panel is preparing a recording."""
        if self.busy:
            raise ValueError('Wait for the playback command or choose Stop audio before starting another operation.')
        return self.controller.stop()

    def poll(self, event=None):
        if self.alive and self.controller.prepared is not None and not self.busy:
            self.submit('', self.controller.poll, quiet=True)

    def query(self, event=None):
        if self.alive and self.window_alive:
            self._text(self.controller.status_text(), focus=True)

    def reload_devices(self, event=None):
        def loaded(rows):
            self.device_rows = (None,) + tuple(row.id for row in rows)
            self.device.SetItems(['System default output'] + [row.name for row in rows])
            self.device.SetSelection(0)
            self._text('Audio output devices refreshed. Choose the device before starting playback.')
        self.submit('Discovering audio output devices', output_devices, loaded)

    def seek_to(self, event=None):
        if not self.current():
            return
        try:
            seconds = _seconds(self.seek.GetValue())
            self.submit('Seeking', lambda: self.controller.seek(seconds))
        except Exception as exc:
            self._text(str(exc))

    def apply_loop(self, event=None):
        if not self.current():
            return
        try:
            start, end = _seconds(self.loop_a.GetValue()), _seconds(self.loop_b.GetValue())
            self.submit('Setting A–B repeat', lambda: self.controller.loop(start, end))
        except Exception as exc:
            self._text(str(exc))

    def apply_sleep(self, event=None):
        if not self.current():
            return
        try:
            minutes = _seconds(self.sleep_minutes.GetValue())
            self.submit('Setting sleep timer', lambda: self.controller.sleep(minutes * 60 if minutes else None))
        except Exception as exc:
            self._text(str(exc))

    def seek_segment(self, event=None):
        if not self.current():
            return
        index = self.segments.GetSelection()
        self.submit('Opening audio chapter', lambda: self.controller.seek_segment(index))

    def dispose(self, *, on_stopped=None):
        if not self.alive:
            return
        self.alive = False
        self._stop_done = None
        self.epoch += 1
        self.cancelled.set()
        try:
            self.timer.Stop()
        except RuntimeError:
            pass
        def close():
            self.controller.close()
            if on_stopped is not None:
                on_stopped()
        try:
            self.executor.submit(close)
        except RuntimeError:
            # A rejected executor must not orphan a playing device. This is
            # bounded by the player's stop contract and never touches controls.
            close()
        finally:
            self.executor.shutdown(wait=False, cancel_futures=False)

    def _destroyed(self, event):
        if event.GetEventObject() is self.window:
            self.window_alive = False
            self.dispose()
        event.Skip()
