"""One measured progress contract and native, optionally audible presentation.

The caller reports known work units; unknown work stays indeterminate. Only an
explicit ``succeed`` after durable completion produces 100 percent. Events carry
operation tokens so callbacks from a previous operation cannot change a new one.
Nothing in this module enters an audio render graph or an output audio file.

Native API references: https://docs.wxpython.org/wx.Gauge.html and
https://docs.python.org/3/library/winsound.html . Windows memory-WAV playback is
synchronous on one bounded background thread, never on the GUI thread.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
from io import BytesIO
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
import wave
import weakref

from ..durable_state import atomic_json, local_path, read_json, regular, state_lock

_ACTIVE = {'running', 'paused'}
_TERMINAL = {'succeeded', 'failed', 'cancelled', 'incomplete'}
_LABELS = {'idle': 'Ready', 'running': 'In progress', 'paused': 'Paused',
           'succeeded': 'Completed', 'failed': 'Failed', 'cancelled': 'Cancelled',
           'incomplete': 'Ended without confirmed completion'}


def operation_outcome(result):
    """Combine actual job/batch receipts without treating partial work as done.

    Plain values and rows without status fields remain ordinary successful
    return values. An explicit unknown status requires attention. Only typed
    status fields are inspected; user text in search results is never a state.
    """
    complete = {'complete', 'completed', 'succeeded', 'success', 'ok', 'passed', 'pass',
                'ready', 'verified', 'saved', 'exported', 'duplicate', 'excluded'}
    failure = {'failed', 'error', 'timed_out', 'timeout', 'corrupt', 'invalid'}
    cancelled = {'cancelled', 'canceled', 'aborted', 'stopped'}
    rank = {'succeeded': 0, 'paused': 1, 'cancelled': 2, 'failed': 3}
    outcome, pending, inspected = 'succeeded', [result], 0
    while pending:
        item = pending.pop()
        inspected += 1
        if inspected > 100_000:
            raise ValueError('Operation result exceeds the progress receipt limit.')
        if isinstance(item, (list, tuple)):
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ('status', 'state'):
            state = item.get(key)
            if state is None:
                continue
            if not isinstance(state, str):
                current = 'paused'
            else:
                state = state.casefold()
                current = ('failed' if state in failure else 'cancelled' if state in cancelled
                           else 'succeeded' if state in complete else 'paused')
            if rank[current] > rank[outcome]:
                outcome = current
        for key in ('rows', 'results'):
            if isinstance(item.get(key), (list, tuple)):
                pending.append(item[key])
    return outcome


def _phase(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError('A short, readable progress phase is required.')
    return value.strip()


def _number(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 10**12 or positive and value == 0:
        raise ValueError(f'Progress {name} must be a finite {"positive" if positive else "nonnegative"} measured value.')
    return value


@dataclass(frozen=True)
class ProgressEvent:
    token: int = 0
    sequence: int = 0
    state: str = 'idle'
    phase: str = 'Ready'
    completed: float | int | None = None
    total: float | int | None = None
    percent: int | None = None
    elapsed_seconds: float = 0.0
    eta_seconds: float | None = None
    total_changed: bool = False


class ProgressTracker:
    """Thread-safe measured state; no timer guesses and no fabricated totals."""
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.RLock()
        self._event = ProgressEvent()
        self._started = self._paused_at = None
        self._paused_seconds = 0.0
        self._samples = deque(maxlen=30)

    @property
    def event(self):
        with self._lock:
            return self._event

    def _elapsed(self, now):
        if self._started is None:
            return 0.0
        stop = self._paused_at if self._paused_at is not None else now
        return max(0.0, stop - self._started - self._paused_seconds)

    def _replace(self, **changes):
        self._event = replace(self._event, sequence=self._event.sequence + 1, **changes)
        return self._event

    def start(self, phase, total=None):
        phase = _phase(phase)
        if total is not None:
            _number(total, 'total', positive=True)
        with self._lock:
            self._started, self._paused_at, self._paused_seconds = self.clock(), None, 0.0
            self._samples.clear()
            if total is not None:
                self._samples.append((0.0, 0))
            self._event = ProgressEvent(token=self._event.token + 1, sequence=self._event.sequence + 1,
                                        state='running', phase=phase, completed=0 if total is not None else None,
                                        total=total, percent=0 if total is not None else None)
            return self._event

    def _current(self, token, *, running=False):
        return (token is None or token == self._event.token) and self._event.state in ({'running'} if running else _ACTIVE)

    def update(self, completed=None, total=None, phase=None, *, token=None):
        if completed is not None:
            _number(completed, 'completed units')
        if total is not None:
            _number(total, 'total', positive=True)
            if completed is None or completed > total:
                raise ValueError('Completed units must be between zero and the measured total.')
        if phase is not None:
            phase = _phase(phase)
        with self._lock:
            if not self._current(token, running=True):
                return None
            previous = self._event
            current_phase = phase or previous.phase
            changed = previous.total is not None and total != previous.total
            if changed and current_phase == previous.phase:
                raise ValueError('A changed progress total needs an explicit new phase description.')
            if total == previous.total and previous.completed is not None and completed is not None and completed < previous.completed:
                raise ValueError('Measured progress cannot move backwards while the total is fixed.')
            elapsed = self._elapsed(self.clock())
            if total != previous.total:
                self._samples.clear()
            if completed is not None and total is not None:
                if not self._samples or completed > self._samples[-1][1]:
                    self._samples.append((elapsed, completed))
                percent = min(99, math.floor(100 * completed / total))
            else:
                percent = None
            eta = None
            if total is not None and completed < total and len(self._samples) >= 3:
                first, last = self._samples[0], self._samples[-1]
                duration, count = last[0] - first[0], last[1] - first[1]
                if duration >= 2.0 and count > 0:
                    estimate = (total - completed) * duration / count
                    if math.isfinite(estimate) and 0 <= estimate <= 30 * 24 * 3600:
                        eta = estimate
            return self._replace(phase=current_phase, completed=completed, total=total, percent=percent,
                                 elapsed_seconds=elapsed, eta_seconds=eta,
                                 total_changed=changed or (previous.total_changed and current_phase == previous.phase))

    def transition(self, state, phase=None, *, token=None):
        if state not in {'paused', 'running', *_TERMINAL}:
            raise ValueError('Unknown progress state.')
        if phase is not None:
            phase = _phase(phase)
        with self._lock:
            if not self._current(token):
                return None
            now = self.clock()
            if state == 'paused':
                if self._event.state == 'paused':
                    return self._event
                self._paused_at = now
            elif state == 'running':
                if self._event.state != 'paused':
                    return self._event
                self._paused_seconds += max(0.0, now - self._paused_at)
                self._paused_at = None
                self._samples.clear()
                if self._event.completed is not None:
                    self._samples.append((self._elapsed(now), self._event.completed))
            percent = 100 if state == 'succeeded' else self._event.percent
            completed = self._event.total if state == 'succeeded' and self._event.total is not None else self._event.completed
            return self._replace(state=state, phase=phase or self._event.phase, completed=completed,
                                 percent=percent, eta_seconds=None, elapsed_seconds=self._elapsed(now))


def describe_progress(event):
    parts = [_LABELS[event.state] + ': ' + event.phase + '.']
    if event.percent is None and event.state in _ACTIVE:
        parts.append('Progress is indeterminate; total work is not yet known.')
        if event.completed is not None:
            parts.append(f'{event.completed:g} units completed.')
    elif event.percent is not None:
        parts.append(f'{event.percent} percent.')
        if event.total is not None:
            parts.append(f'{event.completed:g} of {event.total:g} units.')
        if event.state == 'running' and event.completed == event.total:
            parts.append('Final completion has not yet been confirmed.')
    if event.total_changed:
        parts.append('The measured total changed for this phase.')
    if event.eta_seconds is not None:
        seconds = max(1, math.ceil(event.eta_seconds))
        if seconds < 60:
            estimate = f'{seconds} seconds'
        elif seconds < 3600:
            estimate = f'{math.ceil(seconds / 60)} minutes'
        else:
            estimate = f'{seconds / 3600:.1f} hours'
        parts.append('Estimated remaining time from measured progress: ' + estimate + '.')
    return ' '.join(parts)


@dataclass(frozen=True)
class ProgressPreferences:
    mode: str = 'off'
    interval_seconds: float = 10.0
    volume: float = 0.25
    quiet: bool = False
    background: bool = False
    mute_during_audio: bool = True

    def __post_init__(self):
        if self.mode not in ('off', 'beep'):
            raise ValueError('Choose Off or Beep for standalone progress sound.')
        if type(self.interval_seconds) not in (int, float) or not math.isfinite(self.interval_seconds) or not 1 <= self.interval_seconds <= 120:
            raise ValueError('Progress sound interval must be between 1 and 120 seconds.')
        if type(self.volume) not in (int, float) or not math.isfinite(self.volume) or not 0 <= self.volume <= 1:
            raise ValueError('Progress sound volume must be between zero and one.')
        if any(type(getattr(self, key)) is not bool for key in ('quiet', 'background', 'mute_during_audio')):
            raise ValueError('Progress sound options must be boolean.')


def _settings_path(root):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    return local_path(root / 'state' / 'ui' / 'progress.json')


def load_preferences(root):
    path = _settings_path(root)
    if not os.path.lexists(path):
        return ProgressPreferences()
    if regular(path).st_size > 4096:
        raise ValueError('Progress preferences exceed their size limit.')
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != {'schema', 'preferences'} or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported progress preferences schema.')
    if not isinstance(value['preferences'], dict) or set(value['preferences']) != set(asdict(ProgressPreferences())):
        raise ValueError('Invalid progress preference fields.')
    return ProgressPreferences(**value['preferences'])


def save_preferences(root, preferences):
    if not isinstance(preferences, ProgressPreferences):
        raise ValueError('Validated progress preferences are required.')
    path = _settings_path(root)
    with state_lock(path.parent, 'progress'):
        atomic_json(path, {'schema': 1, 'preferences': asdict(preferences)}, replace=path.exists())


def progress_frequency(percent):
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError('A measured percent between zero and 100 is required.')
    return 300.0 * 2 ** (percent / 50.0)


def progress_tone(percent, *, volume=0.25, state='running', sample_rate=22050):
    """Small PCM memory image for private playback, never a render output."""
    if type(volume) not in (int, float) or not math.isfinite(volume) or not 0 <= volume <= 1:
        raise ValueError('Invalid progress tone volume.')
    if sample_rate not in (22050, 24000, 44100, 48000):
        raise ValueError('Unsupported progress tone sample rate.')
    if state in ('failed', 'incomplete'):
        notes = (240.0, 180.0)
    elif state == 'cancelled':
        notes = (400.0, 200.0)
    elif state == 'paused':
        notes = (500.0, 500.0)
    elif state == 'resumed':
        notes = (500.0, 700.0)
    elif state == 'succeeded':
        notes = (progress_frequency(100), 1600.0)
    elif state == 'running':
        notes = (360.0, 520.0) if percent is None else (progress_frequency(percent),)
    else:
        raise ValueError('Unsupported progress tone state.')
    count, gap = round(sample_rate * .08), round(sample_rate * .03)
    frames = bytearray()
    ramp = max(1, round(sample_rate * .008))
    for index, frequency in enumerate(notes):
        if index:
            frames.extend(b'\0\0' * gap)
        for sample in range(count):
            envelope = min(1.0, sample / ramp, (count - 1 - sample) / ramp)
            amplitude = int(round(32767 * .35 * volume * envelope * math.sin(2 * math.pi * frequency * sample / sample_rate)))
            frames.extend(struct.pack('<h', amplitude))
    output = BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(frames)
    return output.getvalue()


class WindowsProgressSound:
    """At most one short, noninterrupting Windows memory-WAV playback thread."""
    def __init__(self, *, backend=None):
        if backend is None and sys.platform == 'win32':
            try:
                import winsound as backend
            except ImportError:
                backend = None
        self.backend, self.available = backend, backend is not None
        self._lock, self._thread = threading.Lock(), None
        self.muted, self.closed, self.last_error = False, False, None

    def play(self, percent, *, volume, state='running'):
        with self._lock:
            if (not self.available or self.closed or self.muted or volume == 0
                    or self._thread is not None and self._thread.is_alive()):
                return False
            raw = progress_tone(percent, volume=volume, state=state)
            def run():
                try:
                    if not self.closed and not self.muted:
                        # SND_MEMORY | SND_ASYNC is unsupported. This finite
                        # playback runs off the UI thread and never interrupts
                        # another PlaySound invocation with SND_NOSTOP.
                        flags = self.backend.SND_MEMORY | self.backend.SND_NODEFAULT | self.backend.SND_NOSTOP
                        self.backend.PlaySound(raw, flags)
                except (OSError, RuntimeError) as exc:
                    self.last_error = str(exc)
            self._thread = threading.Thread(target=run, name='carr-progress-sound', daemon=True)
            self._thread.start()
            return True

    def close(self):
        self.closed = self.muted = True


class OperationProgress:
    """Bind measured events to native controls without changing keyboard focus."""
    def __init__(self, frame, gauge, label, *, wx_module=None, clock=time.monotonic, sound=None):
        if wx_module is None:
            import wx as wx_module
        self.frame, self.gauge, self.label, self.wx = frame, gauge, label, wx_module
        self.clock, self.tracker = clock, ProgressTracker(clock=clock)
        self.sound = sound or WindowsProgressSound()
        self._ui_thread, self._pending_lock = threading.get_ident(), threading.Lock()
        self._pending, self._scheduled = None, False
        self._displayed_sequence, self._last_display, self._last_sound = -1, -math.inf, -math.inf
        self._last_state, self.audio_active, self.closed = 'idle', False, False
        self._players = weakref.WeakSet()
        self.root, self.preferences = None, ProgressPreferences()
        self.timer = self.wx.Timer(frame)
        frame.Bind(self.wx.EVT_TIMER, self._tick, self.timer)
        self.reload_preferences()
        self._display(self.tracker.event, force=True)

    @property
    def event(self):
        return self.tracker.event

    def reload_preferences(self):
        self.root = local_path(self.frame.library.root, exists=True)
        try:
            self.preferences = load_preferences(self.root)
        except (ValueError, OSError) as exc:
            self.preferences = ProgressPreferences()
            # A readable notice is allowed here; no focus move or speech call.
            if getattr(self.frame, 'status', None) is not None:
                message = f'Progress sound is Off because preferences could not be read: {exc}'
                if threading.get_ident() == self._ui_thread:
                    self.frame.status.SetValue(message)
                else:
                    self.wx.CallAfter(lambda: self.frame.status.SetValue(message))
        self._mute()

    def configure(self, preferences):
        if local_path(self.frame.library.root, exists=True) != self.root:
            raise ValueError('The library folder changed. Reopen progress settings.')
        save_preferences(self.root, preferences)
        self.preferences = preferences
        self._mute()

    def _mute(self):
        playing = False
        for player in tuple(self._players):
            try:
                playing = playing or player.state in ('playing', 'recording', 'monitoring')
            except (OSError, RuntimeError):
                # An uncertain device state stays quiet until stop or close.
                playing = True
        self.sound.muted = (self.preferences.mode == 'off' or self.preferences.quiet
                            or self.preferences.mute_during_audio and (self.audio_active or playing))

    def register_player(self, player):
        """Observe actual playback state, including natural EOF/error/pause.

        Multiple players are combined; one stopped player cannot unmute another
        still playing. Weak references avoid keeping a closed dialog alive.
        """
        if threading.get_ident() != self._ui_thread:
            self.wx.CallAfter(lambda: self.register_player(player))
            return player
        if self.closed:
            return player
        try:
            self._players.add(player)
        except TypeError as exc:
            raise ValueError('A weak-referenceable playback object is required.') from exc
        self.refresh_audio_state()
        return player

    def unregister_player(self, player):
        if threading.get_ident() != self._ui_thread:
            self.wx.CallAfter(lambda: self.unregister_player(player))
            return
        self._players.discard(player)
        if not self.closed:
            self.refresh_audio_state()

    def refresh_audio_state(self):
        if threading.get_ident() != self._ui_thread:
            self.wx.CallAfter(self.refresh_audio_state)
            return
        if not self.closed:
            self._mute()
            self._timer_state()

    def _timer_state(self):
        if self.event.state == 'running' or self._players:
            if not self.timer.IsRunning():
                self.timer.Start(250)
        else:
            self.timer.Stop()

    def set_audio_active(self, active=True):
        if type(active) is not bool:
            raise ValueError('Audio activity must be boolean.')
        self.audio_active = active
        self._mute()

    def start(self, phase, total=None):
        if local_path(self.frame.library.root, exists=True) != self.root:
            self.reload_preferences()
        event = self.tracker.start(phase, total)
        self._publish(event)
        return event.token

    def update(self, completed=None, total=None, phase=None, *, token=None):
        event = self.tracker.update(completed, total, phase, token=token)
        self._publish(event)
        return event

    def pause(self, phase=None, *, token=None):
        return self._transition('paused', phase, token)

    def resume(self, phase=None, *, token=None):
        return self._transition('running', phase, token)

    def succeed(self, phase=None, *, token=None):
        """Call only after the operation's durable success has been verified."""
        return self._transition('succeeded', phase, token)

    def fail(self, phase=None, *, token=None):
        return self._transition('failed', phase, token)

    def cancel(self, phase=None, *, token=None):
        return self._transition('cancelled', phase, token)

    success = succeed
    failed = fail
    cancelled = cancel
    paused = pause

    def _transition(self, state, phase, token):
        event = self.tracker.transition(state, phase, token=token)
        self._publish(event)
        return event

    def finalize(self, *, token=None):
        event = self.event
        if token is not None and event.token != token:
            return
        if event.state == 'running':
            self._transition('incomplete', None, token)
        else:
            self._publish(event)

    def _publish(self, event):
        if event is None or self.closed:
            return
        if threading.get_ident() == self._ui_thread:
            self._display(event)
            return
        # Coalesce worker updates to one pending CallAfter, preventing an
        # unbounded GUI event queue during fast audio or indexing operations.
        with self._pending_lock:
            if self._pending is None or event.sequence >= self._pending.sequence:
                self._pending = event
            if self._scheduled:
                return
            self._scheduled = True
        self.wx.CallAfter(self._flush)

    def _flush(self):
        with self._pending_lock:
            event, self._pending, self._scheduled = self._pending, None, False
        if event is not None:
            self._display(event)

    def _display(self, event, *, force=False):
        if (self.closed or event.token != self.event.token or event.sequence < self._displayed_sequence):
            return
        now = self.clock()
        state_changed = event.state != self._last_state
        if not force and not state_changed and now - self._last_display < .1:
            return
        text = describe_progress(event)
        self.gauge.SetName('Operation progress: ' + _LABELS[event.state])
        self.gauge.SetRange(100)
        if event.percent is not None:
            self.gauge.SetValue(event.percent)
        elif event.state == 'running':
            self.gauge.Pulse()
        else:
            self.gauge.SetValue(0)
        self.label.SetLabel(text)
        self.label.SetName('Operation progress details: ' + text)
        self._displayed_sequence, self._last_display = event.sequence, now
        self._timer_state()
        self._mute()
        self._sound(event, state_changed=state_changed)
        self._last_state = event.state

    def _sound(self, event, *, state_changed=False):
        preference = self.preferences
        if (self.sound.muted or not self.sound.available or preference.volume == 0 or event.state == 'idle'
                or not preference.background and not self.frame.IsActive()):
            return
        now = self.clock()
        if not state_changed and (event.state != 'running' or now - self._last_sound < preference.interval_seconds):
            return
        state = 'resumed' if state_changed and event.state == 'running' and self._last_state == 'paused' else event.state
        if self.sound.play(event.percent, volume=preference.volume, state=state):
            self._last_sound = now

    def _tick(self, event=None):
        if self.closed:
            return
        self.refresh_audio_state()
        current = self.event
        # Flush a throttled latest value even if no further worker event arrives.
        if current.sequence > self._displayed_sequence:
            self._display(current, force=True)
        elif current.state == 'running' and current.percent is None:
            self.gauge.Pulse()
        self._sound(current)

    def query(self, event=None):
        self.frame.report(describe_progress(self.event))

    def close(self):
        self.closed = True
        self.timer.Stop()
        self.sound.close()
        self._players.clear()

    def settings_dialog(self, event=None):
        if local_path(self.frame.library.root, exists=True) != self.root:
            self.reload_preferences()
        wx = self.wx
        dialog = wx.Dialog(self.frame, title='Progress sound settings', size=(640, 480))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(dialog, label='Leave Off when your screen reader already announces progress.\n'
                'Beep uses rising pitch for known progress and two alternating tones for unknown work.'), 0, wx.EXPAND | wx.ALL, 8)
            mode = wx.Choice(dialog, choices=['Off', 'Beep'])
            mode.SetName('Standalone progress sound')
            mode.SetSelection(1 if self.preferences.mode == 'beep' else 0)
            layout.Add(mode, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Interval in seconds'), 0, wx.LEFT | wx.TOP, 8)
            interval = wx.SpinCtrl(dialog, min=1, max=120, initial=round(self.preferences.interval_seconds))
            interval.SetName('Progress sound interval in seconds')
            layout.Add(interval, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Sound volume, percent'), 0, wx.LEFT | wx.TOP, 8)
            volume = wx.Slider(dialog, value=round(self.preferences.volume * 100), minValue=0, maxValue=100)
            volume.SetName('Progress sound volume percent')
            layout.Add(volume, 0, wx.EXPAND | wx.ALL, 8)
            checks = {}
            for name, label in (('quiet', 'Quiet mode: mute progress sound'), ('background', 'Allow progress sound in the background'),
                                ('mute_during_audio', 'Mute progress sound during playback or recording')):
                control = wx.CheckBox(dialog, label=label)
                control.SetName(label)
                control.SetValue(getattr(self.preferences, name))
                checks[name] = control
                layout.Add(control, 0, wx.ALL, 8)
            if not self.sound.available:
                layout.Add(wx.StaticText(dialog, label='Standalone Beep playback is available with the Windows audio runtime.'), 0, wx.ALL, 8)
            layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            mode.SetFocus()
            if dialog.ShowModal() == wx.ID_OK:
                selected = mode.GetSelection()
                if selected not in (0, 1):
                    raise ValueError('Choose Off or Beep.')
                self.configure(ProgressPreferences(mode=('off', 'beep')[selected], interval_seconds=interval.GetValue(),
                    volume=volume.GetValue() / 100, **{name: control.GetValue() for name, control in checks.items()}))
                self.frame.report('Progress sound preferences saved.')
        except (ValueError, OSError, RuntimeError) as exc:
            self.frame.report(f'Progress preferences were not saved: {exc}')
        finally:
            dialog.Destroy()


def install_operation_progress(frame):
    """Install once after normal menus and before configurable shortcuts."""
    import wx
    if getattr(frame, 'operation_progress', None) is not None:
        frame.operation_progress.reload_preferences()
        return frame.operation_progress
    label = wx.StaticText(frame.host, label='Ready')
    gauge = wx.Gauge(frame.host, range=100, style=wx.GA_HORIZONTAL)
    frame.layout.Insert(1, label, 0, wx.EXPAND | wx.ALL, 4)
    frame.layout.Insert(2, gauge, 0, wx.EXPAND | wx.ALL, 4)
    controller = OperationProgress(frame, gauge, label)
    frame.operation_progress = controller
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'help'), None)
    if menu is None:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Help')
    for title, handler in [('Current operation progress\tCtrl+Alt+P', controller.query),
                            ('Progress sound settings...', controller.settings_dialog)]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    def destroy(event):
        if event.GetEventObject() is frame:
            controller.close()
        event.Skip()
    frame.Bind(wx.EVT_WINDOW_DESTROY, destroy)
    return controller
