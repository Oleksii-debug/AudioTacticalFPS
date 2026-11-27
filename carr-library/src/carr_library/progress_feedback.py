"""Optional local progress notifications, isolated from render/output pipelines.

The default is silent. Tone synthesis creates a short in-memory notification,
never an output file or a renderer input. Only an explicitly injected approved
local provider can speak; this module does not implement or acquire TTS.
"""
from dataclasses import dataclass, replace
from io import BytesIO
import math
import struct
import threading
import time
import wave

from .progress import ProgressSnapshot, ProgressState
from .progress_preferences import ProgressPreferences, LegacyProgressPreferences


def progress_frequency(percent):
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError('A measured integer percentage from 0 to 100 is required.')
    return 220 + round(percent * 8.8)


@dataclass(frozen=True)
class Tone:
    frequency: int
    duration_ms: int = 70
    volume: int = 30
    gap_after_ms: int = 40

    def __post_init__(self):
        if (type(self.frequency) is not int or not 37 <= self.frequency <= 4000
                or type(self.duration_ms) is not int or not 20 <= self.duration_ms <= 250
                or type(self.volume) is not int or not 0 <= self.volume <= 100
                or type(self.gap_after_ms) is not int or not 0 <= self.gap_after_ms <= 200):
            raise ValueError('Invalid bounded progress tone.')


class _LegacyVolumeTone(Tone):
    def __post_init__(self):
        Tone(self.frequency, self.duration_ms, 0, self.gap_after_ms)
        if (type(self.volume) not in (int, float) or not math.isfinite(self.volume)
                or not 0 <= self.volume <= 100):
            raise ValueError('Invalid measured legacy progress volume.')


@dataclass(frozen=True)
class FeedbackCue:
    operation_id: str
    revision: int
    kind: str
    tones: tuple[Tone, ...]
    speech: str | None


def tone_wave(tone):
    """Return a small mono PCM WAV with measured frequency/duration/amplitude."""
    if not isinstance(tone, Tone):
        raise ValueError('A validated Tone is required.')
    rate = 22050
    frames = round(rate * tone.duration_ms / 1000)
    fade = min(round(rate * 0.005), frames // 4)
    amplitude = 12000 * tone.volume / 100
    samples = []
    for index in range(frames):
        envelope = min(1.0, index / max(1, fade), (frames - 1 - index) / max(1, fade))
        samples.append(round(amplitude * envelope * math.sin(2 * math.pi * tone.frequency * index / rate)))
    target = BytesIO()
    with wave.open(target, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    return target.getvalue()


class WindowsToneSink:
    """Worker-only Windows playback; per-notification volume is in the PCM."""
    supports_volume = True

    def __init__(self, backend=None):
        if backend is None:
            try:
                import winsound as backend
            except ImportError as exc:
                raise ValueError('Native progress tones are available on Windows.') from exc
        self._backend = backend

    def play(self, tone, *, cancelled=lambda: False):
        if cancelled():
            return
        payload = tone_wave(tone)
        if not cancelled():
            # SND_MEMORY cannot safely use SND_ASYNC. The bounded synchronous
            # call runs only in ProgressFeedback's daemon worker, never wx.
            self._backend.PlaySound(payload, self._backend.SND_MEMORY
                                    | self._backend.SND_NODEFAULT | self._backend.SND_SYNC
                                    | getattr(self._backend, 'SND_NOSTOP', 0))


class FeedbackPlanner:
    """Pure, throttled cue selection from truthful immutable snapshots."""
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._seen = {}
        self._last_emit = float('-inf')

    def discard(self, snapshot):
        """A quiet boundary consumes the current notice without replay later."""
        if snapshot is not None:
            with self._lock:
                self._seen[snapshot.operation_id] = snapshot.revision, snapshot.state

    def plan(self, snapshot, preferences, *, active=True, media_busy=False):
        if not isinstance(snapshot, ProgressSnapshot) or not isinstance(preferences, ProgressPreferences):
            raise ValueError('A progress snapshot and validated preferences are required.')
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError('A finite monotonic feedback clock is required.')
        with self._lock:
            identifier = snapshot.operation_id
            previous = self._seen.get(identifier)
            if len(self._seen) >= 256 and identifier not in self._seen:
                self._seen.pop(next(iter(self._seen)))
            marker = snapshot.revision, snapshot.state
            if (preferences.mode == 'off' or preferences.muted or preferences.volume == 0
                    or (not active and not preferences.allow_background)
                    or (media_busy and preferences.quiet_during_media)):
                self._seen[identifier] = marker
                return None
            state_change = previous is not None and previous[1] != snapshot.state
            repeat_busy = snapshot.state == ProgressState.RUNNING and snapshot.percent is None
            if previous == marker and not repeat_busy:
                return None
            urgent = state_change or snapshot.terminal
            interval = 0.25 if urgent else preferences.interval_seconds
            if now - self._last_emit < interval:
                return None
            kind = {
                ProgressState.PAUSED: 'paused', ProgressState.CANCELLING: 'cancelling',
                ProgressState.CANCELLED: 'cancelled', ProgressState.FAILED: 'failed',
                ProgressState.SUCCEEDED: 'completed',
                ProgressState.UNAVAILABLE: 'unavailable',
            }.get(snapshot.state, 'busy' if snapshot.percent is None else 'progress')
            if snapshot.state == ProgressState.RUNNING and previous and previous[1] == ProgressState.PAUSED:
                kind = 'resumed'
            volume = preferences.volume
            tone = _LegacyVolumeTone if isinstance(preferences, LegacyProgressPreferences) else Tone
            if kind == 'failed':
                tones = (tone(220, 140, volume), tone(160, 180, volume))
            elif kind == 'unavailable':
                tones = (tone(220, 100, volume),)
            elif kind == 'cancelled':
                tones = (tone(440, 70, volume), tone(330, 110, volume))
            elif kind == 'cancelling':
                tones = (tone(330, 70, volume),)
            elif kind == 'paused':
                tones = (tone(330, 70, volume), tone(330, 70, volume))
            elif kind == 'resumed':
                tones = (tone(330, 70, volume), tone(440, 70, volume))
            elif kind == 'busy':
                tones = (tone(350, 30, volume, 80), tone(350, 30, volume))
            elif kind == 'completed':
                tones = (tone(progress_frequency(100), 180, volume),)
            else:
                tones = (tone(progress_frequency(snapshot.percent), 70, volume),)
            if preferences.mode not in ('beep', 'beep_speech'):
                tones = ()
            speech = snapshot.describe() if preferences.mode in ('speech', 'beep_speech') else None
            self._seen[identifier] = marker
            self._last_emit = float(now)
            return FeedbackCue(identifier, snapshot.revision, kind, tones, speech)


class ProgressFeedback:
    """A single latest-cue queue; playback/speech never blocks the GUI thread.

    A speech provider must implement ``speak_progress(text, volume=0..100,
    cancelled=callback)`` and honor cancellation. It is supplied by the owning
    approved local voice adapter. No provider means no speech capability.
    """
    def __init__(self, preferences, *, tone_sink=None, speech_provider=None, clock=time.monotonic,
                 idle_exit=False):
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Validated progress preferences are required.')
        self.preferences = preferences
        if type(idle_exit) is not bool:
            raise ValueError('Notification worker idle policy must be boolean.')
        self._idle_exit = idle_exit
        self.tone_sink, self.speech_provider = tone_sink, speech_provider
        self.planner = FeedbackPlanner(clock=clock)
        self._condition = threading.Condition()
        self._pending = None
        self._worker = None
        self._last_worker = None
        self._closed = False
        self._playing = False
        self._generation = 0
        self._operation_id = None
        self._announced_state = None
        self._retired_providers = []
        self._last_error = ''
        if preferences.mode not in self.available_modes:
            self._last_error = 'This progress sound mode requires an available local tone or approved speech provider.'

    @property
    def available_modes(self):
        return tuple(mode for mode, available in (
            ('off', True), ('beep', self.tone_sink is not None),
            ('speech', self.speech_provider is not None),
            ('beep_speech', self.tone_sink is not None and self.speech_provider is not None)) if available)

    @property
    def last_error(self):
        with self._condition:
            return self._last_error

    @property
    def quiescent(self):
        """Recorder adapters can check that no notification is still playing."""
        with self._condition:
            return not self._playing and self._pending is None and self._silence_confirmed()

    def _silence_confirmed(self):
        retained = []
        for provider in self._retired_providers:
            try:
                quiet = bool(getattr(provider, 'quiescent', True))
            except Exception:
                quiet = False
            if not quiet:
                retained.append(provider)
        self._retired_providers = retained
        try:
            quiet = bool(getattr(self.speech_provider, 'quiescent', True))
        except Exception:
            quiet = False
        return quiet and not retained

    def suspend(self, snapshot=None):
        """Drop queued feedback and request cancellation of approved speech.

        A tone already inside the OS playback call is bounded to 250 ms. A
        recording owner must suppress feedback before opening its microphone
        and observe quiescent if acoustic separation is required.
        """
        with self._condition:
            self._generation += 1
            self._pending = None
            self.planner.discard(snapshot)
            self._condition.notify_all()

    def set_speech_provider(self, provider):
        if provider is not None and not callable(getattr(provider, 'speak_progress', None)):
            raise ValueError('Use an explicitly selected cancellable local progress voice.')
        with self._condition:
            previous = self.speech_provider
            self._generation += 1
            self._pending = None
            self.speech_provider = provider
            if previous is not None and previous is not provider:
                self._retired_providers.append(previous)
            self._last_error = ''
            self._condition.notify_all()
            return previous

    def set_preferences(self, preferences):
        if not isinstance(preferences, ProgressPreferences) or preferences.mode not in self.available_modes:
            raise ValueError('Choose an available local progress-feedback mode.')
        with self._condition:
            self.preferences = preferences
            self._generation += 1
            self._pending = None
            self._last_error = ''
            self._condition.notify_all()

    def update(self, snapshot, *, active=True, media_busy=False):
        with self._condition:
            if self._closed or self.preferences.mode not in self.available_modes:
                return None
            if snapshot.operation_id != self._operation_id:
                # A newer operation supersedes queued and active old notices.
                self._operation_id = snapshot.operation_id
                self._announced_state = None
                self._generation += 1
                self._pending = None
            cue = self.planner.plan(snapshot, self.preferences, active=active, media_busy=media_busy)
            if cue is None:
                # Quiet/mute/background changes also invalidate a queued cue.
                if (self.preferences.mode == 'off' or self.preferences.muted or self.preferences.volume == 0
                        or (not active and not self.preferences.allow_background)
                        or (media_busy and self.preferences.quiet_during_media)):
                    self._generation += 1
                    self._pending = None
                return None
            formatter = getattr(self.speech_provider, 'format_progress', None)
            if cue.speech is not None and callable(formatter):
                cue = replace(cue, speech=formatter(snapshot, kind=cue.kind))
            if self._announced_state is not None and snapshot.state != self._announced_state:
                # A real state change supersedes running speech immediately;
                # the one latest-cue slot retains its terminal notice until
                # that cancelled owned call has drained.
                self._generation += 1
            self._announced_state = snapshot.state
            self._pending = cue, self._generation, self.preferences.volume, self.speech_provider
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name='progress-notifications', daemon=True)
                self._last_worker = self._worker
                self._worker.start()
            self._condition.notify_all()
            return cue

    def _cancelled(self, generation):
        with self._condition:
            return self._closed or generation != self._generation

    def try_play(self, operation, *, provider=None):
        """Run one already validated legacy cue on this same owned worker.

        Compatibility callers retain their existing PCM/volume vocabulary and
        backpressure instead of rounding it into a different notification.
        This entry never starts a parallel thread or replaces a pending cue.
        """
        if not callable(operation):
            raise ValueError('A local cancellable notification callback is required.')
        with self._condition:
            if self._closed or self._playing or self._pending is not None:
                return False
            self._pending = operation, self._generation, None, provider
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name='progress-notifications', daemon=True)
                self._last_worker = self._worker
                self._worker.start()
            self._condition.notify_all()
            return True

    def _run(self):
        while True:
            with self._condition:
                if self._idle_exit and self._pending is None:
                    # Relinquish ownership while holding the condition. A new
                    # update may then start one successor; this consumer exits
                    # without taking another cue or touching its replacement.
                    self._worker = None
                    return
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                while not self._closed and self._pending is not None and not self._silence_confirmed():
                    # Device state may settle without a callback. Retain only
                    # the latest cue and poll plain owned state, never wx.
                    self._condition.wait(timeout=.1)
                if self._closed:
                    return
                if self._pending is None:
                    continue
                cue, generation, volume, speech_provider = self._pending
                self._pending = None
                self._playing = True
            cancelled = lambda version=generation: self._cancelled(version)
            try:
                if callable(cue):
                    if not cancelled():
                        cue(cancelled)
                    continue
                for number, tone in enumerate(cue.tones):
                    if cancelled():
                        break
                    self.tone_sink.play(tone, cancelled=cancelled)
                    if number + 1 < len(cue.tones):
                        with self._condition:
                            self._condition.wait_for(lambda: self._closed or generation != self._generation,
                                                     timeout=tone.gap_after_ms / 1000)
                if cue.speech is not None and not cancelled():
                    speaker = speech_provider.speak_progress
                    if type(volume) is float:
                        speaker = getattr(speech_provider, '_speak_progress_measured', None)
                        if not callable(speaker):
                            raise ValueError('This progress voice needs an explicitly selected whole-percent volume.')
                    speaker(cue.speech, volume=volume, cancelled=cancelled)
            except Exception as exc:
                with self._condition:
                    self._last_error = 'Progress notification unavailable: ' + str(exc)
            finally:
                with self._condition:
                    try:
                        quiet = bool(getattr(speech_provider, 'quiescent', True))
                    except Exception:
                        quiet = False
                    if not quiet and speech_provider not in self._retired_providers:
                        self._retired_providers.append(speech_provider)
                        self._last_error = 'The progress audio device has not confirmed silence.'
                    self._playing = False
                    self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._condition.notify_all()
