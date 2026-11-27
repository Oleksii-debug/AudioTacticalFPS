"""Bounded navigation over one borrowed WavePlayer and playback generation.

The native host supplies its existing worker, quiet-start and Stop lifecycle.
Independent listening controls borrow that same player and worker.
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
    """Owner-local controls; all methods run on its existing UI thread.

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
    """An owner-local timer; its optional controls window only borrows it."""
    def __init__(self, owner, player, *, generation, alive, stop_current, progress=(),
                 report=lambda message: None, wx_module=None, prepare=None, on_play=lambda: None):
        if wx_module is None:
            import wx as wx_module
        from ..speech.progress_speech import play_when_quiet
        self.owner, self.wx, self.closed, self.view = owner, wx_module, False, None
        self._native_id = owner.GetId()
        self._close_error = None
        self.prepare, self.on_play = prepare, on_play
        self._preparation_cancel = None
        self._retained_listening = set()
        self._listening_error = None
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
        if event.GetId() == self._native_id:
            # wx may supply a different wrapper for the same deleted peer.
            # Listener callbacks must detach before controller invalidation.
            self.closed = True
            if self._preparation_cancel is not None:
                self._preparation_cancel.set()
            self.controller.listeners.clear()
            self.controller.close()
            try:
                self.timer.Stop()
            except (OSError, RuntimeError) as exc:
                self._close_error = exc
        event.Skip()

    def bind(self):
        self.controller.bind()

    def invalidate(self):
        self.controller.invalidate()

    def show(self, event=None):
        if self.closed or not self.owner:
            return
        if self.view is not None and self.view and self.view.alive:
            self.view.Raise(); return
        view = self.view = create_navigation_dialog(self.owner, self.controller, wx_module=self.wx,
            settings=self.show_settings if self.prepare is not None else None)
        try:
            view.ShowModal()
        finally:
            self.controller.listeners.discard(view.refresh)
            self.view = None
            if view:
                view.Destroy()

    def _discard_listening(self, prepared):
        self._retained_listening.add(prepared)
        if prepared.close() is False:
            raise RuntimeError('The unused listening copy has not confirmed cleanup.')
        self._retained_listening.discard(prepared)

    def _prepare_settings(self, selected, completed, failed, *, active):
        import threading
        if self.closed or not self.controller.alive() or not active() or self.prepare is None:
            raise ValueError('This playback owner is no longer available.')
        player = self.controller.player
        source, identity, position = player.path, player._identity, player.position_seconds
        if source is None or identity is None or player._stamp(source) != identity:
            raise ValueError('Select the current checked recording before preparing playback.')
        self.controller.stop_current()
        if self.closed or not self.controller.alive() or not active():
            raise ValueError('Playback changed while stopping the previous preview.')
        if player._stamp(source) != identity:
            raise ValueError('The original recording changed while stopping playback.')
        if player.path is None:
            player.open(source)  # Clip Stop closes transport; selection stays silent.
        if player.path != source or player._identity != identity:
            raise ValueError('The stopped recording no longer matches the selected preview.')
        request = player.listening_request(selected.speed, selected.pitch_semitones,
                                           position_seconds=position)
        generation = self.controller.generation()
        cancellation = self._preparation_cancel = threading.Event()
        def stopped():
            return (cancellation.is_set() or self.closed or not self.controller.alive()
                    or not active() or self.controller.generation() != generation)
        def finish(prepared):
            try:
                if stopped():
                    self._discard_listening(prepared)
                    if active() and not self.closed:
                        failed(InterruptedError('Listening preparation belongs to a stopped preview.'))
                    return
                self._retained_listening.add(prepared)
                player.apply_listening(prepared)
                self._retained_listening.discard(prepared)
                self._preparation_cancel = None
                completed('Listening copy verified and selected. Start playback explicitly. '
                          'Position uses the original recording; transformed alignment is approximate.')
            except (Exception, KeyboardInterrupt) as exc:
                if getattr(prepared, '_owner', None) is None and not prepared.closed:
                    try:
                        self._discard_listening(prepared)
                    except (Exception, KeyboardInterrupt) as cleanup:
                        self._listening_error = cleanup
                if active() and not self.closed:
                    failed(exc)
        def error(exc):
            self._preparation_cancel = None
            if active() and not self.closed:
                failed(exc)
        accepted = self.prepare(request, finish, error, cancelled=stopped)
        if accepted is False:
            cancellation.set()
            raise RuntimeError('The existing audio worker did not admit listening preparation.')
        return cancellation.set

    def _play_current(self):
        if self.closed or not self.controller.alive():
            return
        generation = self.controller.generation()
        def current():
            return (not self.closed and self.controller.alive()
                    and self.controller.generation() == generation)
        def begin():
            if current():
                self.on_play()
                self.controller.player.play_current()
                self.bind()
        self.controller.dispatch(begin, alive=current,
                                 on_error=lambda error: self.controller._notice(str(error)))

    def show_settings(self):
        if self.closed or not self.controller.alive() or not self.owner:
            return
        from .playback_settings import show_playback_settings
        player = self.controller.player
        parent = self.view if self.view is not None and self.view and self.view.alive else self.owner
        show_playback_settings(parent, settings=player.listening_settings, levels=player.levels,
            prepare=self._prepare_settings, apply_levels=lambda value: player.set_levels(
                value.volume, muted=value.muted, balance=value.balance),
            mix_available=player.live_mix_available,
            alive=lambda: not self.closed and self.controller.alive(),
            play=self._play_current, stop=self.controller.stop_current, wx_module=self.wx)

    def close(self):
        if self._preparation_cancel is not None:
            self._preparation_cancel.set()
        for prepared in tuple(self._retained_listening):
            self._discard_listening(prepared)
        self._listening_error = None
        if self.closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self.closed = True
        self.controller.listeners.clear()
        try:
            self.timer.Stop()
        except (OSError, RuntimeError) as exc:
            self._close_error = exc
            raise
        finally:
            self.controller.close()
            if self.view is not None and self.view and self.view.alive and self.view.IsModal():
                self.view.EndModal(self.wx.ID_CANCEL)


def create_navigation_dialog(owner, controller, *, wx_module=None, settings=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class NavigationDialog(wx.Dialog):
        def __init__(self):
            super().__init__(owner, title='Playback navigation', size=(660, 700),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self.alive, self._destroy_requested = True, False
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            layout = wx.BoxSizer(wx.VERTICAL)
            self.position = wx.StaticText(self, label='No active recording')
            self.position.SetName('Current playback position and backend accuracy')
            layout.Add(self.position, 0, wx.ALL | wx.EXPAND, 8)
            self.buttons = {}
            def field(label, value):
                layout.Add(wx.StaticText(self, label=label), 0, wx.ALL, 4)
                result = wx.TextCtrl(self, value=str(value)); result.SetName(label.replace('&', ''))
                layout.Add(result, 0, wx.ALL | wx.EXPAND, 4)
                return result
            def row(items):
                box = wx.BoxSizer(wx.HORIZONTAL)
                for key, label, callback in items:
                    button = wx.Button(self, label=label); button.SetName(label.replace('&', ''))
                    button.Bind(wx.EVT_BUTTON, lambda event, action=callback: self.perform(action))
                    self.buttons[key] = button; box.Add(button, 0, wx.ALL, 4)
                layout.Add(box, 0, wx.EXPAND)
            if settings is not None:
                row((('settings', 'Independent playback settings...', settings),))
            self.skip = field('&Skip interval in seconds, 1 to 3600', f'{controller.skip_seconds:g}')
            def move(direction):
                controller.set_skip(float(self.skip.GetValue())); controller.skip(direction)
            row((('back', 'Back&ward', lambda: move(-1)), ('forward', '&Forward', lambda: move(1))))
            self.percent = field('Seek &percentage, 0 to 100', '0')
            row((('seek', 'Seek to percentage', lambda: controller.seek_percent(float(self.percent.GetValue()))),))
            self.range = wx.StaticText(self, label='A and B are not set')
            self.range.SetName('Repeat range A and B'); layout.Add(self.range, 0, wx.ALL, 4)
            row((('a', 'Mark &A here', controller.mark_a), ('b', 'Mark &B here', controller.mark_b),
                 ('clear', 'Clear &repeat', controller.clear_repeat)))
            self.repeat = wx.CheckBox(self, label='&Enable A–B repeat')
            self.repeat.SetName('Enable A–B repeat')
            self.repeat.Bind(wx.EVT_CHECKBOX, lambda event: self.perform(lambda: controller.enable_repeat(self.repeat.GetValue())))
            layout.Add(self.repeat, 0, wx.ALL, 4)
            self.minutes = field('S&leep after minutes; up to 1440; includes paused time', '30')
            row((('sleep', 'Set sleep timer', lambda: controller.set_sleep(float(self.minutes.GetValue()) * 60)),
                 ('cancel_sleep', 'Cancel sleep &timer', controller.cancel_sleep)))
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 70))
            self.status.SetName('Playback navigation status')
            self.status.ChangeValue('Listen to a rendered master in Audio Studio first. Repeat uses a 200 ms control timer and the backend seek precision. '
                'Closing this window keeps enabled repeat/sleep active; Stop or closing Audio Studio clears them.')
            layout.Add(self.status, 1, wx.ALL | wx.EXPAND, 6)
            close = wx.Button(self, wx.ID_CANCEL, '&Close'); close.SetName('Close playback navigation controls')
            layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
            self.SetSizer(layout); self.SetEscapeId(wx.ID_CANCEL)
            controller.listeners.add(self.refresh)
            self.refresh()

        def perform(self, action):
            if not self.alive:
                return
            try:
                action()
            except Exception as exc:
                if self.alive:
                    self.status.ChangeValue(str(exc))
            self.refresh()

        def dispose(self):
            self.alive = False
            controller.listeners.discard(self.refresh)

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def refresh(self):
            if not self.alive:
                return
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
                if key == 'settings':
                    button.Enable(not controller.closed and controller.alive() and controller.player.path is not None)
                    continue
                button.Enable(value['active'] if key not in ('clear', 'cancel_sleep') else
                    value['a'] is not None if key == 'clear' else value['sleep_remaining'] is not None)
            self.repeat.SetValue(value['repeat'])
            self.repeat.Enable(value['active'] and value['a'] is not None and value['b'] is not None)

    return NavigationDialog()

