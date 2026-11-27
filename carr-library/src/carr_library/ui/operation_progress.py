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
from ..progress import ProgressHub, ProgressState

_ACTIVE = {'running', 'paused', 'cancelling'}
_TERMINAL = {'succeeded', 'failed', 'cancelled', 'incomplete'}
_LABELS = {'idle': 'Ready', 'running': 'In progress', 'paused': 'Paused',
           'succeeded': 'Completed', 'failed': 'Failed', 'cancelled': 'Cancelled',
           'incomplete': 'Ended without confirmed completion', 'cancelling': 'Cancellation requested'}


def operation_outcome(result):
    """Combine actual job/batch receipts without treating partial work as done.

    Plain values and rows without status fields remain ordinary successful
    return values. An explicit unknown status requires attention. Only typed
    status fields are inspected; user text in search results is never a state.
    """
    complete = {'complete', 'completed', 'succeeded', 'success', 'ok', 'passed', 'pass',
                'ready', 'verified', 'saved', 'exported', 'duplicate', 'excluded'}
    failure = {'failed', 'error', 'timed_out', 'timeout', 'corrupt', 'invalid'}
    cancelled = {'cancelled', 'canceled', 'aborted'}
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
    if type(value) not in (int, float) or not 0 <= value <= 10**12 or not math.isfinite(value) or positive and value == 0:
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
    """Legacy event vocabulary projected from the shared measured progress hub."""
    def __init__(self, *, clock=time.monotonic, hub=None):
        self.clock = clock
        self.hub = hub if hub is not None else ProgressHub(clock=clock)
        self._lock = self.hub._lock
        self.task = None
        self._revision = None
        self._event = ProgressEvent()
        self._started = self._paused_at = None
        self._paused_seconds = 0.0
        self._samples = deque(maxlen=30)

    @property
    def event(self):
        with self._lock:
            if self.task is not None and self.task.snapshot().revision != self._revision:
                self._project()
            return self._event

    def _elapsed(self, now):
        if self._started is None:
            return 0.0
        stop = self._paused_at if self._paused_at is not None else now
        return max(0.0, stop - self._started - self._paused_seconds)

    def _replace(self, **changes):
        self._event = replace(self._event, sequence=self._event.sequence + 1, **changes)
        return self._event

    def adopt(self, task):
        with self._lock:
            if task is self.task:
                return self.event
            previous = self._event
            self.task = task
            self._samples.clear()
            self._paused_at = None
            self._paused_seconds = 0.0
            self._event = ProgressEvent(token=previous.token + 1, sequence=previous.sequence + 1)
            self._revision = None
            if task is None:
                self._started = None
                return self._event
            snapshot = task.snapshot()
            self._started = snapshot.started_at
            return self._project(eta_seconds=snapshot.eta_seconds,
                                 elapsed_seconds=max(0.0, snapshot.updated_at - snapshot.started_at))

    def _project(self, **changes):
        snapshot = self.task.snapshot()
        self._revision = snapshot.revision
        state = 'incomplete' if snapshot.state == ProgressState.UNAVAILABLE else snapshot.state.value
        values = dict(state=state, phase=snapshot.phase or snapshot.title,
                      completed=snapshot.completed, total=snapshot.total, percent=snapshot.percent)
        values.update(changes)
        return self._replace(**values)

    def start(self, phase, total=None):
        phase = _phase(phase)
        if total is not None:
            _number(total, 'total', positive=True)
        with self._lock:
            if self.task is not None and not self.task.snapshot().terminal:
                self.task.unavailable('This legacy observer was superseded by a new operation.')
            self.task = self.hub.start_measured(phase, total=total)
            self.task.update(phase=phase)
            self._revision = self.task.snapshot().revision
            self._started, self._paused_at, self._paused_seconds = self.clock(), None, 0.0
            self._samples.clear()
            if total is not None:
                self._samples.append((0.0, 0))
            self._event = ProgressEvent(token=self._event.token + 1, sequence=self._event.sequence + 1,
                                        state='running', phase=phase, completed=0 if total is not None else None,
                                        total=total, percent=0 if total is not None else None)
            return self._event

    def _current(self, token, *, running=False):
        event = self.event
        return (token is None or token == event.token) and event.state in ({'running'} if running else _ACTIVE)

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
            previous = self.event
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
            eta = None
            if total is not None and completed < total and len(self._samples) >= 3:
                first, last = self._samples[0], self._samples[-1]
                duration, count = last[0] - first[0], last[1] - first[1]
                if duration >= 2.0 and count > 0:
                    estimate = (total - completed) * duration / count
                    if math.isfinite(estimate) and 0 <= estimate <= 30 * 24 * 3600:
                        eta = estimate
            self.task.update(completed=completed, total=total, phase=current_phase)
            return self._project(elapsed_seconds=elapsed, eta_seconds=eta,
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
            if phase is not None and self.task.snapshot().state == ProgressState.RUNNING:
                self.task.update(phase=phase)
            message = phase or self._event.phase
            if state == 'paused':
                self.task.pause(message)
            elif state == 'running':
                self.task.resume(message)
            elif state == 'succeeded':
                snapshot = self.task.snapshot()
                if snapshot.state == ProgressState.PAUSED:
                    self.task.resume(message)
                if snapshot.total is not None and snapshot.completed != snapshot.total:
                    self.task.update(completed=snapshot.total)
                self.task.succeed(durable=True, message=message)
            elif state == 'failed':
                self.task.fail(message)
            elif state == 'cancelled':
                self.task.cancelled(message)
            else:
                self.task.unavailable(message)
            return self._project(phase=phase or self._event.phase, eta_seconds=None,
                                 elapsed_seconds=self._elapsed(now))

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
        if self.mode not in ('off', 'beep', 'speech', 'both'):
            raise ValueError('Choose Off, Beep, Speech or Beep + Speech for progress sound.')
        if type(self.interval_seconds) not in (int, float) or not 1 <= self.interval_seconds <= 120 or not math.isfinite(self.interval_seconds):
            raise ValueError('Progress sound interval must be between 1 and 120 seconds.')
        if type(self.volume) not in (int, float) or not 0 <= self.volume <= 1 or not math.isfinite(self.volume):
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
    if type(percent) not in (int, float) or not 0 <= percent <= 100 or not math.isfinite(percent):
        raise ValueError('A measured percent between zero and 100 is required.')
    return 300.0 * 2 ** (percent / 50.0)


def progress_tone(percent, *, volume=0.25, state='running', sample_rate=22050):
    """Small PCM memory image for private playback, never a render output."""
    if type(volume) not in (int, float) or not 0 <= volume <= 1 or not math.isfinite(volume):
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
    """One bounded notification worker; existing local speech is explicitly injected."""
    def __init__(self, *, backend=None):
        if backend is None and sys.platform == 'win32':
            try:
                import winsound as backend
            except ImportError:
                backend = None
        self.backend, self.available = backend, backend is not None
        from ..progress_feedback import ProgressFeedback
        from ..progress_preferences import ProgressPreferences as SharedPreferences
        self.feedback = ProgressFeedback(SharedPreferences(), idle_exit=True)
        self._lock = threading.RLock()
        self._muted, self.closed, self.last_error = False, False, None
        self._cancel = threading.Event()
        self._speech_provider, self._active_provider = None, None
        self._uncertain_providers = []
        self._kind = None

    @property
    def _thread(self):
        return self.feedback._worker or self.feedback._last_worker

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, value):
        self._muted = bool(value)
        if self._muted:
            self.cancel()

    @property
    def speech_available(self):
        return self._speech_provider is not None

    @property
    def speech_label(self):
        return getattr(self._speech_provider, 'label', '')

    @property
    def speech_provider(self):
        return self._speech_provider

    @property
    def quiescent(self):
        with self._lock:
            if any(not provider.quiescent for provider in self._uncertain_providers):
                return False
            if self._thread is None or not self._thread.is_alive():
                return True
            # Cancellation makes it safe to wait for synthesis in the background
            # if it has not entered playback. A finite memory tone must drain.
            return (self._kind == 'speech' and self._cancel.is_set()
                    and self._active_provider is not None and self._active_provider.quiescent)

    def cancel(self):
        with self._lock:
            self._cancel.set()
            self.feedback.suspend()

    def set_speech_provider(self, provider):
        if provider is not None and (not callable(getattr(provider, 'speak_progress', None))
                                     or not isinstance(getattr(provider, 'quiescent', None), bool)):
            raise ValueError('Use a cancellable, explicitly selected local progress voice.')
        with self._lock:
            self._cancel.set()
            old, self._speech_provider = self._speech_provider, provider
            if old is not None and old is not provider and callable(getattr(old, 'close', None)):
                old.close()
            self.last_error = ('The progress audio device has not confirmed silence.'
                               if any(not item.quiescent for item in self._uncertain_providers) else None)

    def _start(self, operation, *, kind, provider=None):
        with self._lock:
            if (self.closed or self.muted or not self.quiescent
                    or self._thread is not None and self._thread.is_alive()):
                return False
            self._cancel = cancelled = threading.Event()
            self._kind, self._active_provider = kind, provider

            def run(shared_cancelled):
                try:
                    if not self.closed and not self.muted and not cancelled.is_set() and not shared_cancelled():
                        operation(lambda: cancelled.is_set() or shared_cancelled())
                except Exception as exc:
                    if not cancelled.is_set():
                        self.last_error = str(exc)
                finally:
                    if provider is not None and not provider.quiescent:
                        with self._lock:
                            if provider not in self._uncertain_providers:
                                self._uncertain_providers.append(provider)
                            self.last_error = 'The progress audio device has not confirmed silence.'

            return self.feedback.try_play(run, provider=provider)

    def play(self, percent, *, volume, state='running'):
        if not self.available or volume == 0:
            return False
        raw = progress_tone(percent, volume=volume, state=state)

        def play(cancelled):
            if not cancelled():
                # Never use global PlaySound(None), which could stop NVDA or
                # another app. A recording/playback hold waits for this finite
                # owned synchronous cue to drain on its background thread.
                flags = self.backend.SND_MEMORY | self.backend.SND_NODEFAULT | self.backend.SND_NOSTOP
                self.backend.PlaySound(raw, flags)

        return self._start(play, kind='beep')

    def speak(self, text, *, volume, beep=False, percent=None, state='running'):
        with self._lock:
            provider = self._speech_provider
            if provider is None or volume == 0 or beep and not self.available:
                return False
            raw = progress_tone(percent, volume=volume, state=state) if beep else None

            def announce(cancelled):
                if raw is not None and not cancelled():
                    flags = self.backend.SND_MEMORY | self.backend.SND_NODEFAULT | self.backend.SND_NOSTOP
                    self.backend.PlaySound(raw, flags)
                if cancelled():
                    return
                with self._lock:
                    self._kind = 'speech'
                provider.speak_progress(text, volume=volume, cancelled=cancelled)

            return self._start(announce, kind='beep' if beep else 'speech', provider=provider)

    def close(self):
        self.closed = self.muted = True
        self.set_speech_provider(None)
        self.feedback.close()


class _AudioQuietHold:
    """Admit transport startup only after owned notifications are quiet."""
    def __init__(self, controller, recording):
        self.controller, self.recording, self.closed = controller, recording, False

    @property
    def ready(self):
        controller = self.controller
        if self.closed or controller.closed:
            return False
        if controller._uncertain_players:
            return False
        if not self.recording and any(hold.recording and not hold.closed
                                      for hold in tuple(controller._audio_holds)):
            return False
        for player in tuple(controller._players):
            try:
                state = player.state
            except Exception:
                return False
            if state not in ('playing', 'stopped', 'paused', 'error'):
                return False
            if self.recording and state == 'playing':
                return False
        if self.recording:
            if controller.audio_active or controller.live_media_feedback.busy:
                return False
            media = getattr(controller.frame, 'progress_media_busy', False)
            try:
                if bool(media() if callable(media) else media):
                    return False
            except Exception:
                return False
        if not self.recording and not controller.preferences.mute_during_audio:
            return True
        return bool(getattr(controller.sound, 'quiescent', True))

    def close(self):
        if self.closed:
            return
        self.closed = True
        controller = self.controller
        if threading.get_ident() != controller._ui_thread:
            controller.wx.CallAfter(lambda: controller._release_audio_hold(self))
        else:
            controller._release_audio_hold(self)


class LiveMediaFeedback:
    """Nonblocking handoff from the one progress sound to reader/device audio.

    Leases contain plain activity callables, never native controls. Their release
    may run after a reader window has closed while its worker finishes cleanup.
    No release callback schedules GUI work or waits for the audio thread.
    """
    def __init__(self, progress):
        self.progress = progress
        self._lock = threading.Lock()
        self._activities = {}

    @property
    def quiet_during_media(self):
        return self.progress.preferences.mute_during_audio

    @property
    def quiescent(self):
        if (self.progress.closed or self.progress._uncertain_players
                or any(hold.recording and not hold.closed
                       for hold in tuple(self.progress._audio_holds))):
            return False
        with self._lock:
            activities = tuple(self._activities.values())
        for activity in activities:
            try:
                if getattr(activity, 'silence_uncertain', False):
                    return False
            except Exception:
                return False
        if not self.quiet_during_media:
            return True
        return bool(getattr(self.progress.sound, 'quiescent', False))

    @property
    def busy(self):
        with self._lock:
            activities = tuple(self._activities.values())
        for activity in activities:
            try:
                if activity():
                    return True
            except Exception:
                # Unknown cleanup/device state must not start another cue.
                return True
        return False

    def suspend(self):
        if self.quiet_during_media:
            self.progress.sound.muted = True

    def hold(self, activity):
        if not callable(activity):
            raise ValueError('A plain media activity callback is required.')
        token = object()
        with self._lock:
            self._activities[token] = activity
        self.suspend()

        def release():
            with self._lock:
                self._activities.pop(token, None)
        return release


class OperationProgress:
    """Bind measured events to native controls without changing keyboard focus."""
    def __init__(self, frame, gauge, label, *, wx_module=None, clock=time.monotonic, sound=None, accessible_name=None):
        if wx_module is None:
            import wx as wx_module
        self.frame, self.gauge, self.label, self.wx = frame, gauge, label, wx_module
        if accessible_name is not None and (not isinstance(accessible_name, str)
                or not accessible_name.strip() or len(accessible_name) > 256
                or any(ord(character) < 32 for character in accessible_name)):
            raise ValueError('Use a short readable native progress control name.')
        self.accessible_name = accessible_name
        self.clock, self.tracker = clock, ProgressTracker(clock=clock)
        self.sound = sound or WindowsProgressSound()
        self._ui_thread, self._pending_lock = threading.get_ident(), threading.Lock()
        self._pending, self._scheduled = None, False
        self._displayed_sequence, self._last_display, self._last_sound = -1, -math.inf, -math.inf
        self._last_state, self.audio_active, self.closed = 'idle', False, False
        self._last_token, self._pending_sound = 0, None
        self._players = weakref.WeakSet()
        self._uncertain_players, self._audio_holds = set(), set()
        self.root, self.preferences = None, ProgressPreferences()
        self.live_media_feedback = LiveMediaFeedback(self)
        frame.live_media_feedback = self.live_media_feedback
        self.timer = self.wx.Timer(frame)
        frame.Bind(self.wx.EVT_TIMER, self._tick, self.timer)
        self.reload_preferences()
        self._display(self.tracker.event, force=True)

    @property
    def event(self):
        return self.tracker.event

    @property
    def speech_provider(self):
        return getattr(self.sound, 'speech_provider', None)

    def reload_preferences(self):
        root = local_path(self.frame.library.root, exists=True)
        if self.root is not None and root != self.root:
            # A persisted speech preference never authorizes another library's
            # helper or voice. The user selects a local voice again explicitly.
            self._pending_sound = None
            self.set_speech_provider(None)
            self._timer_state()
        self.root = root
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
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Validated progress preferences are required.')
        if local_path(self.frame.library.root, exists=True) != self.root:
            raise ValueError('The library folder changed. Reopen progress settings.')
        if preferences.mode in ('speech', 'both') and not getattr(self.sound, 'speech_available', False):
            raise ValueError('Select an installed progress voice in Reader or Audio Studio first.')
        if preferences.mode == 'both' and not self.sound.available:
            raise ValueError('Beep + Speech requires the Windows audio runtime. Choose Speech here.')
        save_preferences(self.root, preferences)
        cancel = getattr(self.sound, 'cancel', None)
        if callable(cancel):
            cancel()
        self.preferences = preferences
        self._mute()

    def _mute(self):
        playing, recording = False, False
        for player in tuple(self._uncertain_players):
            try:
                if player.state in ('stopped', 'paused', 'error'):
                    self._uncertain_players.discard(player)
            except (OSError, RuntimeError):
                pass
        playing = uncertain = bool(self._uncertain_players)
        for player in tuple(self._players):
            try:
                state = player.state
                recording = recording or state in ('recording', 'monitoring')
                playing = playing or state == 'playing'
                uncertain = uncertain or state not in ('playing', 'recording', 'monitoring', 'stopped', 'paused', 'error')
            except (OSError, RuntimeError):
                # An uncertain device state stays quiet until stop or close.
                uncertain = True
        media = getattr(self.frame, 'progress_media_busy', False)
        try:
            media = bool(media() if callable(media) else media)
        except Exception:
            media = True
        recording = recording or any(hold.recording for hold in self._audio_holds)
        playback_wait = any(not hold.recording for hold in self._audio_holds)
        unavailable_voice = (self.preferences.mode in ('speech', 'both')
                             and not getattr(self.sound, 'speech_available', False)
                             or self.preferences.mode == 'both' and not self.sound.available)
        self.sound.muted = (self.preferences.mode == 'off' or unavailable_voice or self.preferences.quiet
                            or self.preferences.volume == 0
                            or not self.preferences.background and not self.frame.IsActive()
                            or recording or uncertain or self.preferences.mute_during_audio
                            and (self.audio_active or playing or playback_wait
                                  or media or self.live_media_feedback.busy))
        if self.sound.muted:
            # A mute boundary discards deferred audio immediately, even if the
            # policy is lifted before the next GUI timer event.
            self._pending_sound = None
            self._timer_state()

    def set_speech_provider(self, provider, *, root=None):
        """Select an approved local voice for this session; never enable sound."""
        if provider is not None:
            current = local_path(self.frame.library.root, exists=True)
            selected_root = root if root is not None else getattr(provider, 'library_root', None)
            if current != self.root or selected_root is not None and local_path(selected_root, exists=True) != current:
                raise ValueError('The library folder changed. Select its progress voice again.')
            provider_root = getattr(provider, 'library_root', None)
            if provider_root is not None and local_path(provider_root, exists=True) != current:
                raise ValueError('This progress voice belongs to a different library. Select it again.')
        setter = getattr(self.sound, 'set_speech_provider', None)
        if setter is None:
            if provider is not None:
                raise ValueError('This progress sound backend does not support local speech.')
            return
        setter(provider)
        self._mute()

    def hold_audio(self, *, recording=False):
        """Cancel notifications before transport startup and expose quiet readiness.

        A recorder must keep the returned recording hold until capture has
        actually stopped, and must not open capture until ``hold.ready``. There
        is no microphone capture implementation in this progress controller.
        """
        if type(recording) is not bool:
            raise ValueError('Recording activity must be boolean.')
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError('Prepare audio transport on the native UI thread.')
        if self.closed:
            raise RuntimeError('The progress controller is closed.')
        hold = _AudioQuietHold(self, recording)
        self._audio_holds.add(hold)
        self._mute()
        self._timer_state()
        return hold

    def _release_audio_hold(self, hold):
        self._audio_holds.discard(hold)
        if not self.closed:
            self.refresh_audio_state()

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

    def unregister_player(self, player, *, silence_confirmed=True):
        if type(silence_confirmed) is not bool:
            raise ValueError('Playback silence confirmation must be boolean.')
        if threading.get_ident() != self._ui_thread:
            self.wx.CallAfter(lambda: self.unregister_player(player, silence_confirmed=silence_confirmed))
            return
        if not silence_confirmed:
            # Strong retention is intentional: closing a dialog must not forget
            # native output whose stop/close both failed.
            self._uncertain_players.add(player)
        else:
            self._uncertain_players.discard(player)
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
        if (self.event.state == 'running' or self._pending_sound is not None
                or self._players or self._uncertain_players or self._audio_holds):
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
        state_changed = event.state != self._last_state or event.token != self._last_token
        if not force and not state_changed and now - self._last_display < .1:
            return
        text = describe_progress(event)
        self.gauge.SetName(self.accessible_name or ('Operation progress: ' + _LABELS[event.state]))
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
        self._last_state, self._last_token = event.state, event.token

    def _sound(self, event, *, state_changed=False):
        preference = self.preferences
        if self._pending_sound is not None and self._pending_sound[:2] != (event.token, event.state):
            self._pending_sound = None
        available = (getattr(self.sound, 'speech_available', False)
                     if preference.mode in ('speech', 'both') else self.sound.available)
        if (self.sound.muted or not available or preference.volume == 0 or event.state == 'idle'
                or not preference.background and not self.frame.IsActive()):
            # Quiet/background/capture policy also discards a deferred cue; it
            # must not be replayed later after its operation is no longer current.
            self._pending_sound = None
            self._timer_state()
            return
        now = self.clock()
        state = ('resumed' if state_changed and event.token == self._last_token
                 and event.state == 'running' and self._last_state == 'paused' else event.state)
        if state_changed:
            # Keep only the latest state cue. The existing bounded worker may
            # still be speaking the running state, so cancel that stale speech
            # and retry this cue on the GUI timer after its owned output drains.
            self._pending_sound = (event.token, event.state, state)
            cancel = getattr(self.sound, 'cancel', None)
            if callable(cancel):
                cancel()
        if (self._pending_sound is None and not state_changed
                and (event.state != 'running' or now - self._last_sound < preference.interval_seconds)):
            return
        if self._pending_sound is not None:
            state = self._pending_sound[2]
        if preference.mode in ('speech', 'both'):
            measured = (f'{event.percent} percent. ' if event.percent is not None else
                        'Total work is not yet known. ' if event.state == 'running' else '')
            text = _LABELS[event.state] + '. ' + measured + event.phase[:160] + '.'
            formatter = getattr(self.speech_provider, 'notification_text', None)
            if formatter is not None:
                text = formatter(event, state=state)
            played = self.sound.speak(text, volume=preference.volume, beep=preference.mode == 'both',
                                      percent=event.percent, state=state)
        else:
            played = self.sound.play(event.percent, volume=preference.volume, state=state)
        if played:
            self._last_sound = now
            self._pending_sound = None
        self._timer_state()

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
        text = describe_progress(self.event)
        if getattr(self.sound, 'last_error', None):
            text += ' Progress sound needs attention: ' + self.sound.last_error
        elif self.preferences.mode in ('speech', 'both') and not getattr(self.sound, 'speech_available', False):
            text += ' Select a local progress voice in Reader or Audio Studio for this session.'
        self.frame.report(text)

    def close(self):
        self.closed = True
        self._pending_sound = None
        self.timer.Stop()
        self.sound.close()
        self._players.clear()
        self._audio_holds.clear()

    def settings_dialog(self, event=None):
        if local_path(self.frame.library.root, exists=True) != self.root:
            self.reload_preferences()
        wx = self.wx
        dialog = wx.Dialog(self.frame, title='Progress sound settings', size=(680, 570))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(dialog, label='Leave Off when your screen reader already announces progress.\n'
                'Beep uses rising pitch for known work. Speech uses only the local voice you explicitly select.'), 0, wx.EXPAND | wx.ALL, 8)
            mode = wx.Choice(dialog, choices=['Off', 'Beep', 'Speech: selected local voice', 'Beep + Speech'])
            mode.SetName('Standalone progress sound')
            mode.SetSelection(('off', 'beep', 'speech', 'both').index(self.preferences.mode))
            layout.Add(mode, 0, wx.EXPAND | wx.ALL, 8)
            selected_voice = getattr(self.sound, 'speech_label', '')
            voice_notice = wx.StaticText(dialog, label=('Selected for this session: ' + selected_voice
                if selected_voice else 'No progress voice selected. Use "Use voice for progress" in Reader or Audio Studio.'))
            voice_notice.SetName('Selected local progress voice')
            layout.Add(voice_notice, 0, wx.EXPAND | wx.ALL, 8)
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
                                ('mute_during_audio', 'Mute progress sound during playback (recording is always muted)')):
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
                if selected not in (0, 1, 2, 3):
                    raise ValueError('Choose Off, Beep, Speech or Beep + Speech.')
                self.configure(ProgressPreferences(mode=('off', 'beep', 'speech', 'both')[selected], interval_seconds=interval.GetValue(),
                    volume=volume.GetValue() / 100, **{name: control.GetValue() for name, control in checks.items()}))
                self.frame.report('Progress sound preferences saved.')
        except (ValueError, OSError, RuntimeError) as exc:
            self.frame.report(f'Progress preferences were not saved: {exc}')
        finally:
            dialog.Destroy()


class ProgressCompatibility:
    """Old call vocabulary over the one native panel, task hub and audio owner."""
    def __init__(self, panel):
        self.panel, self.frame, self.wx = panel, panel.frame, panel.wx
        self.tracker = ProgressTracker(hub=panel.hub, clock=panel.hub._clock)
        self._pending_lock = threading.Lock()
        self._scheduled = False

    def _sync(self):
        if self.tracker.hub is not self.panel.hub:
            self.tracker.hub = self.panel.hub
            self.tracker.clock = self.panel.hub._clock
            self.tracker._lock = self.panel.hub._lock
            self.tracker.adopt(None)
        self.tracker.adopt(self.panel.active_task)

    @property
    def event(self):
        self._sync()
        return self.tracker.event

    @property
    def closed(self):
        return self.panel.closed

    @property
    def root(self):
        return self.panel.store.library_root

    @property
    def speech_provider(self):
        return self.panel.speech_provider

    @property
    def live_media_feedback(self):
        return self.panel.media.live_feedback

    @property
    def preferences(self):
        value = self.panel.preferences
        return ProgressPreferences(mode='both' if value.mode == 'beep_speech' else value.mode,
            interval_seconds=value.interval_seconds, volume=value.volume / 100,
            quiet=value.muted, background=value.allow_background,
            mute_during_audio=value.quiet_during_media)

    @property
    def reporting_error(self):
        value = self.panel._reporting_failure
        return value[2] if value is not None else None

    @reporting_error.setter
    def reporting_error(self, value):
        if value is None:
            self.panel._reporting_failure = None
        else:
            self.panel.notice = str(value)

    def configure(self, preferences):
        from decimal import Decimal
        from ..progress_preferences import ProgressPreferences as SharedPreferences, LegacyProgressPreferences
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Validated progress preferences are required.')
        measured = Decimal(str(preferences.volume)) * 100
        volume = int(measured) if measured == measured.to_integral_value() else float(measured)
        policy_type = LegacyProgressPreferences if self.panel.store.legacy else SharedPreferences
        self.panel.save_preferences(policy_type(
            mode='beep_speech' if preferences.mode == 'both' else preferences.mode,
            interval_seconds=preferences.interval_seconds, volume=volume,
            muted=preferences.quiet, allow_background=preferences.background,
            quiet_during_media=preferences.mute_during_audio))

    def _refresh(self):
        if self.closed:
            return
        if threading.get_ident() == self.panel.media.ui_thread:
            self.panel.refresh()
            return
        with self._pending_lock:
            if self._scheduled:
                return
            self._scheduled = True

        def flush():
            with self._pending_lock:
                self._scheduled = False
            if not self.closed:
                self.panel.refresh()
        self.wx.CallAfter(flush)

    def start(self, phase, total=None):
        if self.closed:
            return None
        self._sync()
        value = self.tracker.start(phase, total)
        self.panel.active_task = self.tracker.task
        self.panel._last_revision = None
        self.panel._reporting_failure = None
        self._refresh()
        return value.token

    def update(self, completed=None, total=None, phase=None, *, token=None):
        if self.closed:
            return None
        self._sync()
        if completed is None and total is None and phase is not None:
            with self.tracker._lock:
                if not self.tracker._current(token, running=True):
                    return None
                self.tracker.task.update(phase=_phase(phase))
                value = self.tracker.event
        else:
            value = self.tracker.update(completed, total, phase, token=token)
        if value is not None:
            self._refresh()
        return value

    def _transition(self, state, phase=None, *, token=None):
        if self.closed:
            return None
        self._sync()
        value = self.tracker.transition(state, phase, token=token)
        if value is not None:
            self._refresh()
        return value

    def pause(self, phase=None, *, token=None):
        return self._transition('paused', phase, token=token)

    def resume(self, phase=None, *, token=None):
        return self._transition('running', phase, token=token)

    def succeed(self, phase=None, *, token=None):
        return self._transition('succeeded', phase, token=token)

    def fail(self, phase=None, *, token=None):
        return self._transition('failed', phase, token=token)

    def cancel(self, phase=None, *, token=None):
        return self._transition('cancelled', phase, token=token)

    success, failed, cancelled, paused = succeed, fail, cancel, pause

    def finalize(self, *, token=None):
        if self.closed:
            return None
        self._sync()
        value = self.tracker.event
        if token is not None and token != value.token:
            return None
        if value.state == 'running':
            return self._transition('incomplete', token=token)
        self._refresh()
        return value

    def set_audio_active(self, active=True):
        if type(active) is not bool:
            raise ValueError('Audio activity must be boolean.')
        self.panel.media.audio_active = active
        self.panel.refresh_audio_state()

    def reload_preferences(self):
        from ..durable_state import local_path
        root = local_path(self.frame.library.root, exists=True)
        if root != local_path(self.root, exists=True):
            return self.panel.change_root(root)
        self.panel.feedback.suspend()
        self.panel.preferences = self.panel._read_preferences()
        self.panel.feedback.set_preferences(self.panel._effective_preferences(self.panel.preferences))
        if not self.closed:
            self.panel.mute.SetValue(self.panel.preferences.muted)
            self._refresh()

    def hold_audio(self, *, recording=False):
        return self.panel.hold_audio(recording=recording)

    def register_player(self, player):
        return self.panel.register_player(player)

    def unregister_player(self, player, *, silence_confirmed=True):
        return self.panel.unregister_player(player, silence_confirmed=silence_confirmed)

    def refresh_audio_state(self):
        return self.panel.refresh_audio_state()

    def set_speech_provider(self, provider, *, root=None):
        return self.panel.set_speech_provider(provider, root=root)

    def settings_dialog(self, event=None):
        return self.panel.settings(event)

    def query(self, event=None):
        return self.panel.query(event)

    def close(self):
        return self.panel.close()


def install_operation_progress(frame):
    """Install the shared observer once and expose the legacy call vocabulary."""
    from .progress_panel import install_progress_panel
    return install_progress_panel(frame).legacy
