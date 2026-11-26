"""Truthful shared progress and strict local notification preferences.

An operation is complete only after its caller accepts the result. Reaching a
work counter's total alone cannot report 100 percent. No wx dependency here.
"""
from dataclasses import asdict, dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import queue
import struct
from tempfile import NamedTemporaryFile, TemporaryDirectory
import threading
import time
from uuid import uuid4
import wave

from .ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer

FILENAME = 'progress-settings.json'
MAX_BYTES = 8192
TERMINAL_STATES = frozenset(('completed', 'failed', 'cancelled'))


def _message(value):
    if not isinstance(value, str):
        raise ValueError('Progress messages must be text.')
    return ' '.join(value.split())[:500]


@dataclass(frozen=True)
class ProgressSnapshot:
    operation_id: str
    label: str
    phase: str
    state: str
    completed: int | None
    total: int | None
    unit: str
    elapsed_seconds: float
    eta_seconds: float | None
    sequence: int
    verified: bool = False
    total_notice: str = ''

    @property
    def percent(self):
        if self.state == 'completed' and self.verified:
            return 100
        if self.total is None:
            return None
        return min(99, (self.completed or 0) * 100 // self.total) if self.total else 0

    @property
    def description(self):
        state = {'running': 'In progress', 'paused': 'Paused', 'completed': 'Complete',
                 'failed': 'Needs attention', 'cancelled': 'Cancelled'}[self.state]
        parts = [self.label, self.phase, state]
        parts.append('Total work is not yet known; progress is indeterminate'
                     if self.percent is None else f'{self.percent} percent')
        if self.total is not None:
            parts.append(f'{self.completed} of {self.total} {self.unit}')
        if self.total_notice:
            parts.append(self.total_notice)
        if self.eta_seconds is not None:
            parts.append(f'Estimated remaining time: {math.ceil(self.eta_seconds)} seconds, based on measured progress')
        return '. '.join(part for part in parts if part) + '.'


class ProgressTracker:
    """One thread-safe operation; views poll its latest immutable snapshot."""
    def __init__(self, label, *, total=None, completed=0, unit='items', clock=time.monotonic):
        self._lock, self._clock = threading.RLock(), clock
        self.operation_id, self.label = uuid4().hex, _message(label)
        if not self.label:
            raise ValueError('Name the progress operation.')
        self._phase, self._state, self._sequence = 'Starting', 'running', 0
        self._started = clock()
        self._finished = None
        self._completed = self._total = None
        self._baseline, self._baseline_time = 0, self._started
        self._unit = _message(unit)[:80] or 'items'
        self._verified, self._total_notice = False, ''
        if total is not None:
            self.update(completed=completed, total=total)

    def update(self, *, phase=None, completed=None, total=None, unit=None):
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
            values = {}
            if phase is not None:
                values['_phase'] = _message(phase)
            if completed is not None or total is not None:
                if (type(completed) is not int or type(total) is not int
                        or not 0 <= completed <= total <= 10**12):
                    raise ValueError('Progress needs actual nonnegative completed and total units.')
                if self._total == total and self._completed is not None and completed < self._completed:
                    raise ValueError('Completed work cannot decrease while the total stays fixed.')
                if self._total != total:
                    values.update(_baseline=completed, _baseline_time=self._clock(),
                                  _total_notice=(f'Total work changed from {self._total} to {total}'
                                                 if self._total is not None else ''))
                values.update(_completed=completed, _total=total)
            if unit is not None:
                values['_unit'] = _message(unit)[:80] or 'items'
            changed = any(getattr(self, key) != value for key, value in values.items())
            for key, value in values.items():
                setattr(self, key, value)
            if changed:
                self._sequence += 1
            return changed

    def finish(self, state='completed', *, verified=False, phase=None):
        if not isinstance(state, str) or state not in TERMINAL_STATES or type(verified) is not bool:
            raise ValueError('Choose a completed, failed or cancelled operation state.')
        if state == 'completed' and not verified:
            raise ValueError('Successful completion must be verified before reporting 100 percent.')
        description = _message(phase) if phase is not None else {
            'completed': 'Operation finished and its result was accepted',
            'failed': 'The operation did not finish successfully',
            'cancelled': 'The operation stopped; accepted work is retained where supported'}[state]
        with self._lock:
            if self._state in TERMINAL_STATES:
                return False
            self._state, self._verified = state, state == 'completed'
            self._phase = description
            if self._verified and self._total is not None:
                self._completed = self._total
            self._finished = self._clock()
            self._sequence += 1
            return True

    def pause(self):
        with self._lock:
            if self._state != 'running':
                return False
            self._state = 'paused'
            self._sequence += 1
            return True

    def resume(self):
        with self._lock:
            if self._state != 'paused':
                return False
            self._state = 'running'
            self._baseline = self._completed or 0
            self._baseline_time = self._clock()
            self._sequence += 1
            return True

    def snapshot(self):
        with self._lock:
            now = self._finished if self._finished is not None else self._clock()
            elapsed, eta = max(0.0, now - self._started), None
            if self._state == 'running' and self._total is not None:
                measured = (self._completed or 0) - self._baseline
                seconds = max(0.0, now - self._baseline_time)
                if measured > 0 and seconds >= 1 and self._completed < self._total:
                    eta = seconds * (self._total - self._completed) / measured
            return ProgressSnapshot(self.operation_id, self.label, self._phase, self._state,
                                    self._completed, self._total, self._unit, elapsed, eta,
                                    self._sequence, self._verified, self._total_notice)


@dataclass(frozen=True)
class ProgressSettings:
    mode: str = 'off'
    volume: int = 20
    interval_seconds: int = 5
    muted: bool = False
    background: bool = False

    def __post_init__(self):
        if self.mode not in ('off', 'beep'):
            raise ValueError('Application progress output supports Off or Beep; speech is provided by your screen reader.')
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise ValueError('Progress tone volume must be from 0 to 100.')
        if type(self.interval_seconds) is not int or not 1 <= self.interval_seconds <= 60:
            raise ValueError('Progress tone interval must be from 1 to 60 seconds.')
        if type(self.muted) is not bool or type(self.background) is not bool:
            raise ValueError('Progress mute and background settings must be true or false.')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate progress preference field.')
        result[key] = value
    return result


def validate_progress_settings_payload(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Progress preferences are empty or exceed their byte budget.')
    try:
        payload = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite setting')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid progress preferences JSON.') from exc
    if (type(payload) is not dict or set(payload) != {'kind', 'version', *ProgressSettings.__dataclass_fields__}
            or payload['kind'] != 'carr-progress-settings'
            or type(payload['version']) is not int or payload['version'] != 1):
        raise ValueError('Unsupported progress preferences format.')
    return ProgressSettings(**{key: payload[key] for key in ProgressSettings.__dataclass_fields__})


def encode_progress_settings(settings):
    if not isinstance(settings, ProgressSettings):
        raise ValueError('Expected validated progress preferences.')
    raw = (json.dumps({'kind': 'carr-progress-settings', 'version': 1, **asdict(settings)},
                      sort_keys=True, indent=2) + '\n').encode('utf-8')
    validate_progress_settings_payload(raw)
    return raw


def snapshot_progress_settings(root):
    """Allowlisted settings bytes; the backup caller already holds its lock."""
    root = _private_root(root)
    path = root / FILENAME
    if not os.path.lexists(path):
        return {}
    _regular_if_present(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Progress preferences exceed their byte budget.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != size:
        raise ValueError('Progress preferences changed while reading.')
    validate_progress_settings_payload(raw)
    return {FILENAME: raw}


class ProgressSettingsStore:
    def __init__(self, root):
        self.root = _private_root(root)

    def load(self):
        raw = snapshot_progress_settings(self.root).get(FILENAME)
        return validate_progress_settings_payload(raw) if raw is not None else ProgressSettings()

    def save(self, settings):
        raw = encode_progress_settings(settings)
        path = self.root / FILENAME
        with exclusive_writer(self.root):
            _regular_if_present(path)
            temporary = None
            try:
                with NamedTemporaryFile(dir=self.root, prefix='.progress-', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                if os.name != 'nt':
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return settings


def tone_segments(snapshot, *, resumed=False):
    if snapshot.state == 'completed':
        return ((880, 90), (1100, 90))
    if snapshot.state == 'failed':
        return ((330, 130), (220, 130))
    if snapshot.state == 'cancelled':
        return ((440, 80), (330, 80))
    if snapshot.state == 'paused':
        return ((350, 80),)
    if resumed:
        return ((350, 60), (440, 60))
    frequency = 330 if snapshot.percent is None else round(220 * 2 ** (snapshot.percent / 50))
    return ((frequency, 70),)


def tone_wave(segments, volume, *, sample_rate=16000):
    """Small original PCM cues, separate from every rendered media track."""
    if type(volume) is not int or not 0 <= volume <= 100 or type(sample_rate) is not int or sample_rate != 16000:
        raise ValueError('Invalid progress tone volume or sample rate.')
    if (not isinstance(segments, tuple) or not 1 <= len(segments) <= 4
            or any(type(segment) is not tuple or len(segment) != 2
                   or type(segment[0]) is not int or not 100 <= segment[0] <= 2000
                   or type(segment[1]) is not int or not 20 <= segment[1] <= 200
                   for segment in segments)):
        raise ValueError('Invalid bounded progress tone.')
    samples = bytearray()
    amplitude = round(12000 * volume / 100)
    for index, (frequency, milliseconds) in enumerate(segments):
        if index:
            samples.extend(b'\0\0' * (sample_rate // 50))
        frames, ramp = sample_rate * milliseconds // 1000, sample_rate // 200
        for frame in range(frames):
            envelope = min(1.0, frame / ramp, (frames - 1 - frame) / ramp)
            value = round(amplitude * envelope * math.sin(2 * math.pi * frequency * frame / sample_rate))
            samples.extend(struct.pack('<h', value))
    output = BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples)
    return output.getvalue()


class ProgressTonePolicy:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.operation = self.last_state = self.last_sequence = None
        self.last_time = float('-inf')

    def choose(self, snapshot, settings, *, active=True, suppressed=False):
        if snapshot.operation_id != self.operation:
            self.operation, self.last_state, self.last_sequence = snapshot.operation_id, None, None
            self.last_time = float('-inf')
        if settings.mode == 'off' or settings.muted or not settings.volume or suppressed or (not active and not settings.background):
            return None
        now = self.clock()
        transition = snapshot.state != self.last_state
        if snapshot.state in TERMINAL_STATES and self.last_sequence == snapshot.sequence:
            return None
        if not transition and now - self.last_time < settings.interval_seconds:
            return None
        if snapshot.state == 'paused' and not transition:
            return None
        cue = tone_segments(snapshot, resumed=self.last_state == 'paused' and snapshot.state == 'running')
        self.last_time, self.last_state, self.last_sequence = now, snapshot.state, snapshot.sequence
        return cue


_audio_lock = threading.RLock()
_audio_owners = set()
_dispatcher = None


class WindowsToneBackend:
    """At most two tiny temporary WAVs; native playback is asynchronous."""
    def __init__(self, sound=None):
        if sound is None:
            if os.name != 'nt':
                raise OSError('Application progress tones require Windows in this build.')
            import winsound
            sound = winsound
        self.sound = sound
        self.directory = TemporaryDirectory(prefix='research-progress-tones-')
        self.index, self.playing = 0, False

    def prepare(self, raw):
        self.index = 1 - self.index
        path = Path(self.directory.name) / f'tone-{self.index}.wav'
        path.write_bytes(raw)
        return path

    def play(self, path):
        self.sound.PlaySound(str(path), self.sound.SND_FILENAME | self.sound.SND_ASYNC | self.sound.SND_NODEFAULT)
        self.playing = True

    def stop(self):
        if self.playing:
            self.sound.PlaySound(None, 0)
            self.playing = False

    def close(self):
        self.stop()
        self.directory.cleanup()


class ToneDispatcher:
    """Latest-request mailbox; waveform preparation stays off the UI thread."""
    def __init__(self, *, backend_factory=WindowsToneBackend):
        self.backend_factory = backend_factory
        self.mailbox = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.backend = None
        self.thread = threading.Thread(target=self._work, daemon=True, name='progress-tones')
        self.thread.start()

    def request(self, segments, volume, valid=lambda: True):
        if self.closed.is_set():
            return False
        request = (segments, volume, valid)
        try:
            self.mailbox.put_nowait(request)
        except queue.Full:
            try:
                self.mailbox.get_nowait()
            except queue.Empty:
                pass
            try:
                self.mailbox.put_nowait(request)
            except queue.Full:
                return False
        return True

    def _work(self):
        try:
            while not self.closed.is_set():
                try:
                    segments, volume, valid = self.mailbox.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    if not valid():
                        continue
                    raw = tone_wave(segments, volume)
                    if self.backend is None:
                        self.backend = self.backend_factory()
                    path = self.backend.prepare(raw)
                    # The same lock serializes preview admission with the tiny
                    # asynchronous start call. A queued tone cannot replace a
                    # preview after the preview's admission flag has been set.
                    with _audio_lock:
                        if not _audio_owners and not self.closed.is_set() and valid():
                            self.backend.play(path)
                except (OSError, RuntimeError, ValueError):
                    continue  # Notifications never fail the actual operation.
        finally:
            with _audio_lock:
                if self.backend is not None:
                    try:
                        self.backend.close()
                    except (OSError, RuntimeError):
                        pass

    def silence(self):
        with _audio_lock:
            if self.backend is not None:
                try:
                    self.backend.stop()
                except (OSError, RuntimeError):
                    pass

    def close(self):
        self.closed.set()
        self.silence()


def request_progress_tone(segments, volume, valid=lambda: True):
    global _dispatcher
    if os.name != 'nt':
        return False
    with _audio_lock:
        if _audio_owners:
            return False
        if _dispatcher is None:
            _dispatcher = ToneDispatcher()
        return _dispatcher.request(segments, volume, valid)


def set_media_active(owner, active):
    """A local playback/recording owner suppresses app progress sounds."""
    with _audio_lock:
        if active:
            _audio_owners.add(owner)
            if _dispatcher is not None:
                _dispatcher.silence()
        else:
            _audio_owners.discard(owner)


def progress_audio_suppressed():
    with _audio_lock:
        return bool(_audio_owners)
