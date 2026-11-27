"""Bounded navigation over one borrowed WavePlayer and reader generation.

The native host supplies its existing quiet-start and Stop lifecycle. Explicit
output selection delegates to that same player; live speed conversion is absent.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import time

from ..durable_state import regular
from ..speech._io import local_file


MAX_SKIP_SECONDS = 3600
MAX_SLEEP_SECONDS = 24 * 3600
MIN_REPEAT_SECONDS = .25
TICK_MILLISECONDS = 200


def _number(value, label, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be a finite number from {minimum:g} to {maximum:g}.')
    return float(value)


def _stamp(path):
    value = regular(path)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass(frozen=True)
class _Audio:
    generation: object
    path: Path
    duration: float
    stamp: tuple


class PlaybackNavigation:
    """Reader-owned controls; all methods run on its existing UI thread.

    ``dispatch(action, alive=..., on_error=...)`` uses play_when_quiet in the
    native host. ``stop_current`` must also invalidate the reader's own pending
    playback generation. The controller never owns or closes the borrowed player.
    """
    def __init__(self, player, *, generation, alive=lambda: True, dispatch=None,
                 stop_current=None, report=lambda message: None, clock=time.monotonic):
        self.player, self.generation, self.alive = player, generation, alive
        self.dispatch = dispatch or self._immediate
        self.stop_current = stop_current or player.stop
        self.report, self.clock = report, clock
        self.closed, self.audio, self._serial, self._pending = False, None, 0, False
        self.skip_seconds = 10.0
        self.a = self.b = self.sleep_deadline = None
        self.repeat, self._last_state = False, 'stopped'
        self.listeners = set()

    @staticmethod
    def _immediate(action, *, alive, on_error):
        try:
            if alive():
                action()
        except Exception as exc:
            on_error(exc)

    def _notice(self, message):
        try:
            self.report(message)
        except Exception:
            pass  # Feedback cannot undo a native transport change.

    def _changed(self):
        for callback in tuple(self.listeners):
            try:
                callback()
            except Exception:
                pass

    def bind(self):
        """Call only after the reader explicitly starts its new PCM recording."""
        if self.closed or not self.alive() or self.player.state not in ('playing', 'paused'):
            raise ValueError('Start the current recording before enabling playback navigation.')
        path = local_file(self.player.path)
        duration = _number(self.player.duration_seconds, 'Recording duration', .000001, float('1e15'))
        self.invalidate(notify=False)
        self.audio = _Audio(self.generation(), path, duration, _stamp(path))
        self._last_state = self.player.state
        self._changed()

    def invalidate(self, *, notify=True):
        self._serial += 1
        self.audio, self._pending = None, False
        self.a = self.b = self.sleep_deadline = None
        self.repeat, self._last_state = False, 'stopped'
        if notify:
            self._changed()

    def _current(self):
        audio = self.audio
        try:
            if self.closed or audio is None or not self.alive() or self.generation() != audio.generation:
                return False
            return (self.player.path == audio.path and self.player.duration_seconds == audio.duration
                    and _stamp(audio.path) == audio.stamp)
        except (OSError, ValueError, RuntimeError):
            return False

    def _sample(self, *, active=False):
        if not self._current():
            self.invalidate(notify=False)
            raise ValueError('This playback belongs to an earlier or changed recording. Start the current recording again.')
        state = self.player.state
        position = _number(self.player.position_seconds, 'Player position', 0, float('1e15'))
        position = min(self.audio.duration, position)
        if active and state not in ('playing', 'paused'):
            raise ValueError('Start or resume this recording before seeking; navigation never starts stopped audio.')
        return state, position

    def set_skip(self, seconds):
        self.skip_seconds = _number(seconds, 'Skip interval in seconds', 1, MAX_SKIP_SECONDS)
        self._changed()

    def select_output(self, device):
        """The named action explicitly stops audio and revokes queued navigation."""
        if self.closed or not self.alive():
            raise ValueError('The reader closed. Reopen playback controls.')
        select = getattr(self.player, 'select_output', None)
        if not callable(select):
            raise ValueError('This playback transport does not support selecting an output.')
        self.invalidate()
        self.stop_current()
        select(device)
        self._notice('Playback stopped. The selected output applies to the next explicit Play or Read; audio was not restarted.')
        self._changed()

    def skip(self, direction):
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Choose backward or forward navigation.')
        _, position = self._sample(active=True)
        self.seek_seconds(max(0, min(self.audio.duration, position + direction * self.skip_seconds)))

    def seek_percent(self, percentage):
        percentage = _number(percentage, 'Playback percentage', 0, 100)
        self._sample(active=True)
        self.seek_seconds(self.audio.duration * (percentage / 100))

    def seek_seconds(self, seconds):
        self._sample(active=True)
        seconds = _number(seconds, 'Playback position in seconds', 0, self.audio.duration)
        if self.repeat and not self.a <= seconds < self.b:
            self.repeat = False
        self._seek(seconds)

    def mark_a(self):
        _, self.a = self._sample(active=True)
        self.b, self.repeat = None, False
        self._serial += 1; self._pending = False
        self._changed()

    def mark_b(self):
        _, position = self._sample(active=True)
        if self.a is None:
            raise ValueError('Mark A before marking B.')
        self.set_range(self.a, position)

    def set_range(self, start, end):
        self._sample(active=True)
        start = _number(start, 'Repeat start A', 0, self.audio.duration)
        end = _number(end, 'Repeat end B', 0, self.audio.duration)
        if end - start < MIN_REPEAT_SECONDS:
            raise ValueError(f'B must follow A by at least {MIN_REPEAT_SECONDS:g} seconds.')
        self.a, self.b, self.repeat = start, end, False
        self._serial += 1; self._pending = False
        self._changed()

    def enable_repeat(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Repeat must be explicitly enabled or disabled.')
        if enabled:
            self._sample(active=True)
            if self.a is None or self.b is None:
                raise ValueError('Mark both A and B before enabling repeat.')
        self.repeat = enabled
        self._serial += 1; self._pending = False
        self._changed()

    def clear_repeat(self):
        self.a = self.b = None
        self.enable_repeat(False)

    def set_sleep(self, seconds):
        seconds = _number(seconds, 'Sleep timer in seconds', 1, MAX_SLEEP_SECONDS)
        self._sample(active=True)
        self.sleep_deadline = self.clock() + seconds
        self._changed()

    def cancel_sleep(self):
        self.sleep_deadline = None
        self._changed()

    def _sleep_due(self):
        return self.sleep_deadline is not None and self.clock() >= self.sleep_deadline

    def _sleep_stop(self):
        self._serial += 1; self._pending = False
        self.repeat, self.sleep_deadline = False, None
        try:
            self.stop_current()
            if self.player.state != 'stopped':
                raise RuntimeError('The playback device did not confirm Stop.')
        finally:
            self.invalidate()
        self._notice('Sleep timer stopped the current recording.')

    def _seek(self, seconds, *, looping=False):
        self._serial += 1
        serial, self._pending = self._serial, True
        def current():
            try:
                valid = serial == self._serial and self._current()
                if valid:
                    state, position = self._sample()
                    natural_end = state == 'stopped' and position >= self.audio.duration and self._last_state == 'playing'
                    valid = state in ('playing', 'paused') if not looping else self.repeat and (state == 'playing' or natural_end)
            except Exception:
                valid = False
            if not valid and serial == self._serial:
                self._pending = False
            return valid
        def failed(error):
            if serial == self._serial:
                self._pending, self.repeat = False, False
                self._notice(f'Playback navigation did not complete: {error}')
                self._changed()
        def apply():
            try:
                if not current():
                    return
                if self._sleep_due():
                    self._sleep_stop(); return
                self.player.seek(seconds)
                self._last_state = self.player.state
                if not looping:
                    suffix = ' Position is estimated on this backend.' if self.player.position_is_approximate else ''
                    self._notice(f'Playback moved to {seconds:.1f} seconds.{suffix}')
            finally:
                if serial == self._serial:
                    self._pending = False
                self._changed()
        try:
            self.dispatch(apply, alive=current, on_error=failed)
        except Exception as exc:
            failed(exc)

    def tick(self):
        if self.closed or self.audio is None:
            return
        try:
            state, position = self._sample()
            if self._sleep_due():
                self._sleep_stop(); return
            natural_end = state == 'stopped' and position >= self.audio.duration and self._last_state == 'playing'
            if state == 'error':
                raise RuntimeError('The playback backend stopped with an error.')
            if self.repeat and not self._pending and (state == 'playing' or natural_end) and position >= self.b:
                self._seek(self.a, looping=True)
            elif state == 'stopped' and not (self.repeat and natural_end):
                self.invalidate(); return
            if state in ('playing', 'paused'):
                self._last_state = state
            self._changed()
        except Exception as exc:
            self.invalidate()
            self._notice(f'Playback navigation is unavailable; repeat and sleep controls were cleared. {exc}')

    def snapshot(self):
        active, state, position, duration = False, 'stopped', 0.0, 0.0
        if self._current():
            try:
                state, position = self._sample()
                duration = self.audio.duration
                active = state in ('playing', 'paused')
            except (OSError, ValueError, RuntimeError) as exc:
                self.invalidate(notify=False)
                self._notice(f'Playback navigation is unavailable; repeat and sleep controls were cleared. {exc}')
        return dict(active=active, state=state, position=position, duration=duration,
            approximate=bool(self.player.position_is_approximate), skip_seconds=self.skip_seconds,
            a=self.a, b=self.b, repeat=self.repeat,
            sleep_remaining=max(0, self.sleep_deadline - self.clock()) if self.sleep_deadline is not None else None)

    def close(self):
        self.closed = True
        self.invalidate()
        self.listeners.clear()


class PlaybackNavigationHost:
    """A timer owned by the reader; its optional controls window only borrows it."""
    def __init__(self, owner, player, *, generation, alive, stop_current, progress=(),
                 report=lambda message: None, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        from ..speech.progress_speech import play_when_quiet
        self.owner, self.wx, self.closed, self.view = owner, wx_module, False, None
        # MSW may detach the original Python wrapper before delivering Destroy.
        # Capture the native identity while alive; never query a dying wrapper.
        self.owner_id = owner.GetId()
        def dispatch(action, *, alive, on_error):
            play_when_quiet(progress, action, schedule=self.wx.CallLater, alive=alive, on_error=on_error)
        self.controller = PlaybackNavigation(player, generation=generation, alive=alive,
            dispatch=dispatch, stop_current=stop_current, report=report)
        self.timer = self.wx.Timer(owner)
        owner.Bind(self.wx.EVT_TIMER, self._tick, self.timer)
        owner.Bind(self.wx.EVT_WINDOW_DESTROY, self._destroyed)
        self.timer.Start(TICK_MILLISECONDS)

    def _tick(self, event=None):
        if not self.closed:
            self.controller.tick()

    def _destroyed(self, event):
        if event.GetId() == self.owner_id:
            self.close()
        event.Skip()

    def bind(self):
        self.controller.bind()

    def invalidate(self):
        self.controller.invalidate()

    def show(self, event=None):
        if self.closed:
            return
        if self.view is not None:
            self.view.Raise(); return
        view = self.view = create_navigation_dialog(self.owner, self.controller, wx_module=self.wx)
        try:
            view.ShowModal()
        finally:
            self.controller.listeners.discard(view.refresh)
            self.view = None
            view.Destroy()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.timer.Stop()
        finally:
            self.controller.close()
            if self.view is not None and self.view.IsModal():
                self.view.EndModal(self.wx.ID_CANCEL)


def create_navigation_dialog(owner, controller, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class NavigationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(owner, title='Playback navigation', size=(720, 700),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            outer = wx.BoxSizer(wx.VERTICAL)
            panel = wx.ScrolledWindow(self, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            layout = wx.BoxSizer(wx.VERTICAL)
            self.position = wx.StaticText(panel, label='No active recording')
            self.position.SetName('Current playback position and backend accuracy')
            layout.Add(self.position, 0, wx.ALL | wx.EXPAND, 8)
            self.output = None
            self.outputs = (None,)
            if callable(getattr(controller.player, 'output_devices', None)):
                layout.Add(wx.StaticText(panel, label='Playback output for this reader session'), 0, wx.ALL, 4)
                self.output = wx.Choice(panel, choices=['Automatic output chosen by the playback backend'])
                self.output.SetName('Requested playback output; apply explicitly after selection')
                self.output.SetSelection(0 if controller.player.output_device is None else -1)
                layout.Add(self.output, 0, wx.ALL | wx.EXPAND, 4)
                box = wx.BoxSizer(wx.HORIZONTAL)
                self.reload_outputs = wx.Button(panel, label='Reload available outputs')
                self.reload_outputs.SetName('Reload available playback outputs without playing audio')
                self.reload_outputs.Bind(wx.EVT_BUTTON, lambda event: self.perform(self.load_outputs))
                box.Add(self.reload_outputs, 0, wx.ALL, 4)
                self.apply_output = wx.Button(panel, label='Stop audio and use selected output')
                self.apply_output.SetName('Stop audio and use selected playback output')
                self.apply_output.Bind(wx.EVT_BUTTON, lambda event: self.perform(self.use_output))
                box.Add(self.apply_output, 0, wx.ALL, 4)
                layout.Add(box, 0, wx.EXPAND)
            self.buttons = {}
            def field(label, value):
                layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
                result = wx.TextCtrl(panel, value=str(value)); result.SetName(label.replace('&', ''))
                layout.Add(result, 0, wx.ALL | wx.EXPAND, 4)
                return result
            def row(items):
                box = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, callback in items:
                    button = wx.Button(panel, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, lambda event, action=callback: self.perform(action))
                    self.buttons[key] = button; box.Add(button, 0, wx.ALL, 4)
                layout.Add(box, 0, wx.EXPAND)
            self.skip = field('&Skip interval in seconds, 1 to 3600', f'{controller.skip_seconds:g}')
            def move(direction):
                controller.set_skip(float(self.skip.GetValue())); controller.skip(direction)
            row((('back', 'Back&ward', lambda: move(-1)), ('forward', '&Forward', lambda: move(1))))
            self.percent = field('Seek &percentage, 0 to 100', '0')
            row((('seek', 'Seek to percentage', lambda: controller.seek_percent(float(self.percent.GetValue()))),))
            self.range = wx.StaticText(panel, label='A and B are not set')
            self.range.SetName('Repeat range A and B'); layout.Add(self.range, 0, wx.ALL, 4)
            row((('a', 'Mark &A here', controller.mark_a), ('b', 'Mark &B here', controller.mark_b),
                 ('clear', 'Clear &repeat', controller.clear_repeat)))
            self.repeat = wx.CheckBox(panel, label='&Enable A–B repeat')
            self.repeat.SetName('Enable A–B repeat')
            self.repeat.Bind(wx.EVT_CHECKBOX, lambda event: self.perform(lambda: controller.enable_repeat(self.repeat.GetValue())))
            layout.Add(self.repeat, 0, wx.ALL, 4)
            self.minutes = field('S&leep after minutes; up to 1440; includes paused time', '30')
            row((('sleep', 'Set sleep timer', lambda: controller.set_sleep(float(self.minutes.GetValue()) * 60)),
                 ('cancel_sleep', 'Cancel sleep &timer', controller.cancel_sleep)))
            self.status = wx.TextCtrl(panel, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 70))
            self.status.SetName('Playback navigation status')
            self.status.ChangeValue('Start audio in the reader first. Repeat uses a 200 ms control timer and the backend seek precision. '
                'Closing this window keeps enabled repeat/sleep active; Stop or closing the reader clears them.')
            layout.Add(self.status, 1, wx.ALL | wx.EXPAND, 6)
            panel.SetSizer(layout); panel.FitInside()
            outer.Add(panel, 1, wx.EXPAND)
            close = wx.Button(self, wx.ID_CANCEL, '&Close'); close.SetName('Close playback navigation controls')
            outer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
            self.SetSizer(outer); self.SetEscapeId(wx.ID_CANCEL)
            controller.listeners.add(self.refresh)
            self.refresh()

        def load_outputs(self):
            if controller.closed or not controller.alive():
                raise ValueError('The reader closed. Reopen playback controls.')
            values = controller.player.output_devices()
            selected = controller.player.output_device
            self.outputs = (None, *values)
            self.output.Set(['Automatic output chosen by the playback backend',
                *(f'{row.name}; device {row.index + 1}' + ('' if row.selectable else '; no stable identity, unavailable')
                  for row in values)])
            index = 0 if selected is None else next((index for index, row in enumerate(self.outputs)
                if row is not None and row.identity == selected.identity), -1)
            self.output.SetSelection(index)
            self.status.ChangeValue(f'{len(values)} Windows playback output(s) listed. '
                'Selection is for this reader session only; no sound was played.'
                + (' The previously selected output is unavailable; choose explicitly.' if index < 0 else ''))

        def use_output(self):
            index = self.output.GetSelection()
            if not 0 <= index < len(self.outputs):
                raise ValueError('Choose an enumerated output or automatic output explicitly.')
            selected = self.outputs[index]
            if selected is not None and not selected.selectable:
                raise ValueError('This output has no stable interface identity. Choose a different output or automatic output.')
            controller.select_output(selected)
            self.status.ChangeValue('Playback stopped. Start audio explicitly to use '
                + (selected.name if selected is not None else 'automatic output') + '.')

        def perform(self, action):
            try:
                action()
            except Exception as exc:
                self.status.ChangeValue(str(exc))
            self.refresh()

        def refresh(self):
            value = controller.snapshot()
            accuracy = 'estimated position' if value['approximate'] else 'native measured position'
            label = f'{value["state"]}; {value["position"]:.0f} of {value["duration"]:.1f} seconds; {accuracy}'
            if value['sleep_remaining'] is not None:
                label += f'; sleep in {math.ceil(value["sleep_remaining"])} seconds'
            if self.position.GetLabel() != label:
                self.position.SetLabel(label)
            self.range.SetLabel('A: ' + ('not set' if value['a'] is None else f'{value["a"]:.2f} s')
                + '; B: ' + ('not set' if value['b'] is None else f'{value["b"]:.2f} s'))
            for key, button in self.buttons.items():
                button.Enable(value['active'] if key not in ('clear', 'cancel_sleep') else
                    value['a'] is not None if key == 'clear' else value['sleep_remaining'] is not None)
            self.repeat.SetValue(value['repeat'])
            self.repeat.Enable(value['active'] and value['a'] is not None and value['b'] is not None)

    return NavigationDialog()
