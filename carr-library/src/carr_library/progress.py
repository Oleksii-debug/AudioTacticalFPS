"""Bounded operation progress and optional, independent local feedback.

Counts describe a producer's real units. Finishing a count is not evidence that
an output was verified or durably published: only an explicit verified finish
can report 100 percent. No audio project, microphone or rendering state is used.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
import base64
import io
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile
import threading
import time
from uuid import uuid4
import wave

from .ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer


FILENAME = 'operation-progress.json'
MAX_BYTES = 4096
STATES = frozenset(('running', 'paused', 'resumed', 'completed', 'cancelled', 'failed'))
TERMINAL = frozenset(('completed', 'cancelled', 'failed'))
MODES = ('Off', 'Beep', 'Speech', 'Beep+Speech')
_UNSET = object()


def _text(value, name, limit=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        raise ValueError(f'Invalid progress {name}.')
    value.encode('utf-8', errors='strict')
    return value


def _number(value, name):
    if value is not None and (type(value) not in (int, float)
            or not 0 <= value <= 10**18 or not math.isfinite(value)):
        raise ValueError(f'Invalid progress {name}.')
    return value


@dataclass(frozen=True)
class ProgressHandle:
    task_id: str
    generation: int
    owner: str


@dataclass(frozen=True)
class ProgressEvent:
    handle: ProgressHandle
    label: str
    phase: str
    completed: int | float | None = None
    total: int | float | None = None
    state: str = 'running'
    verified: bool = False
    sequence: int = 0

    def __post_init__(self):
        if type(self.handle) is not ProgressHandle:
            raise ValueError('A progress task handle is required.')
        _text(self.handle.task_id, 'task identity', 128)
        _text(self.label, 'label')
        _text(self.phase, 'phase')
        _number(self.completed, 'completed count')
        _number(self.total, 'total count')
        if self.completed is not None and self.total is not None and self.completed > self.total:
            raise ValueError('A completed count cannot exceed its total.')
        if self.state not in STATES or type(self.verified) is not bool:
            raise ValueError('Invalid progress state.')
        if self.verified != (self.state == 'completed'):
            raise ValueError('Completed progress requires an explicit verified finish.')
        if (self.state == 'completed' and self.total is not None
                and self.completed != self.total):
            raise ValueError('All known units must be processed before verified completion.')

    @property
    def task_id(self):
        return self.handle.task_id

    @property
    def percent(self):
        if self.state == 'completed':
            return 100
        if self.total and self.completed is not None and self.completed < self.total:
            return math.floor(100 * self.completed / self.total)
        return None

    @property
    def awaiting_verification(self):
        return (self.state not in TERMINAL and self.total is not None
                and self.completed == self.total)

    def describe(self):
        state = self.state.capitalize()
        if self.state == 'completed':
            detail = '100%; verified complete'
        elif self.awaiting_verification:
            detail = f'{self.completed:g} of {self.total:g} processed; final verification pending'
        elif self.percent is not None:
            detail = f'{self.percent}%; {self.completed:g} of {self.total:g}'
        elif self.total is None:
            detail = ('Total unknown' if self.completed is None
                      else f'{self.completed:g} processed; total unknown')
        else:
            detail = f'Total {self.total:g}; completed amount unknown'
        return f'{self.label}: {self.phase}. {state}. {detail}.'


class ProgressHub:
    """Thread-safe latest-event storage; no unbounded producer-to-UI queue.

Old terminal tasks may be evicted. Active tasks are never silently discarded;
starting above the configured concurrent limit fails before changing any state.
Generation-bound handles withdraw prior producers on restart or close.
"""
    def __init__(self, *, max_tasks=32):
        if type(max_tasks) is not int or not 1 <= max_tasks <= 256:
            raise ValueError('Choose a progress task limit between 1 and 256.')
        self.max_tasks = max_tasks
        self._lock = threading.RLock()
        self._owner = uuid4().hex
        self._generation = self._sequence = 0
        self._tasks = OrderedDict()
        self._pending = OrderedDict()
        self._closed = False

    def _publish(self, event):
        self._sequence += 1
        event = replace(event, sequence=self._sequence)
        self._tasks[event.task_id] = event
        self._tasks.move_to_end(event.task_id)
        self._pending[event.task_id] = event
        self._pending.move_to_end(event.task_id)
        return event

    def start(self, task_id, label, phase='Starting', *, total=None):
        with self._lock:
            if self._closed:
                return None
            handle = ProgressHandle(task_id, self._generation + 1, self._owner)
            event = ProgressEvent(handle, label, phase, 0 if total is not None else None, total)
            previous = self._tasks.get(task_id)
            if previous is not None and previous.state not in TERMINAL:
                raise ValueError('This progress task is already active.')
            if previous is None and len(self._tasks) >= self.max_tasks:
                retired = next((key for key, item in self._tasks.items() if item.state in TERMINAL), None)
                if retired is None:
                    raise ValueError('The concurrent progress task limit was reached.')
                self._tasks.pop(retired)
                self._pending.pop(retired, None)
            self._generation += 1
            self._publish(event)
            return handle

    def _owned(self, handle):
        if self._closed or type(handle) is not ProgressHandle or handle.owner != self._owner:
            return None
        event = self._tasks.get(handle.task_id)
        return event if event is not None and event.handle == handle else None

    def update(self, handle, *, phase=_UNSET, completed=_UNSET, total=_UNSET, state='running'):
        if state not in ('running', 'paused', 'resumed'):
            raise ValueError('Use finish for a terminal progress state.')
        with self._lock:
            previous = self._owned(handle)
            if previous is None or previous.state in TERMINAL:
                return None
            next_phase = previous.phase if phase is _UNSET else phase
            next_total = previous.total if total is _UNSET else total
            next_completed = previous.completed if completed is _UNSET else completed
            event = ProgressEvent(handle, previous.label, next_phase, next_completed, next_total, state)
            if (event.phase == previous.phase and event.total == previous.total
                    and previous.completed is not None
                    and (event.completed is None or event.completed < previous.completed)):
                raise ValueError('Progress cannot move backwards within the same phase and total.')
            return self._publish(event)

    def pause(self, handle):
        return self.update(handle, state='paused')

    def resume(self, handle):
        return self.update(handle, state='resumed')

    def finish(self, handle, *, state='completed', verified=False):
        if state not in TERMINAL:
            raise ValueError('Choose completed, cancelled or failed.')
        with self._lock:
            previous = self._owned(handle)
            if previous is None or previous.state in TERMINAL:
                return None
            event = replace(previous, state=state, verified=verified)
            return self._publish(event)

    def snapshot(self):
        with self._lock:
            return tuple(self._tasks.values())

    def drain(self):
        with self._lock:
            events = tuple(self._pending.values())
            self._pending.clear()
            return events

    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._tasks.clear()


@dataclass(frozen=True)
class FeedbackPreferences:
    mode: str = 'Off'
    interval: float = 5.0
    volume: int = 25
    muted: bool = False
    background: bool = False

    def __post_init__(self):
        if (self.mode not in MODES or type(self.interval) not in (int, float)
                or not 1 <= self.interval <= 120 or not math.isfinite(self.interval)
                or type(self.volume) is not int or not 0 <= self.volume <= 100
                or type(self.muted) is not bool or type(self.background) is not bool):
            raise ValueError('Invalid operation feedback preferences.')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate progress preference field.')
        result[key] = value
    return result


def validate_preferences_bytes(raw):
    """Strict, side-effect-free backup validation of the portable preference file."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Operation feedback preferences are empty or too large.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid operation feedback preferences JSON.') from exc
    expected = {'version', 'kind', 'mode', 'interval', 'volume', 'muted', 'background'}
    if (type(value) is not dict or set(value) != expected
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'carr-operation-progress'):
        raise ValueError('Unsupported operation feedback preference format.')
    return FeedbackPreferences(**{key: value[key] for key in expected - {'version', 'kind'}})


def encode_preferences(preferences):
    if type(preferences) is not FeedbackPreferences:
        raise ValueError('Validated feedback preferences are required.')
    raw = (json.dumps(dict(version=1, kind='carr-operation-progress', **asdict(preferences)),
                      sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    validate_preferences_bytes(raw)
    return raw


class ProgressPreferenceStore:
    def __init__(self, root):
        self.root = _private_root(root)
        self.path = self.root / FILENAME

    def load(self):
        if not os.path.lexists(self.path):
            return FeedbackPreferences()
        _regular_if_present(self.path)
        with self.path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        return validate_preferences_bytes(raw)

    def save(self, preferences):
        raw = encode_preferences(preferences)
        with exclusive_writer(self.root):
            _regular_if_present(self.path)
            temporary = None
            try:
                with NamedTemporaryFile(dir=self.root, prefix='.progress-', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name != 'nt':
                    descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return raw


TONE_FREQUENCIES = {0: 440, 25: 554, 50: 659, 75: 784, 100: 880}
PCM_RATE = 24000
PCM_DURATION = 0.16


def progress_wave(percent, *, volume=25, state='running'):
    """Return actual mono PCM16 WAV; unknown progress is a two-tone pattern.

Duration and amplitude are fixed bounded notifications, not audio project
content. Cancel/failure/pause use distinct descending or low-pitch patterns.
"""
    if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
        raise ValueError('A percentage from 0 through 100 or unknown is required.')
    if type(volume) is not int or not 0 <= volume <= 100 or state not in STATES:
        raise ValueError('Invalid progress sound settings.')
    if state == 'cancelled':
        frequencies = (440, 330)
    elif state == 'failed':
        frequencies = (330, 220)
    elif state == 'paused':
        frequencies = (330,)
    elif percent is None:
        frequencies = (330, 440)
    else:
        frequencies = (TONE_FREQUENCIES[(percent // 25) * 25],)
    samples = int(PCM_RATE * PCM_DURATION)
    section = samples // len(frequencies)
    ramp = int(PCM_RATE * 0.008)
    data = bytearray()
    for index in range(samples):
        part = min(index // section, len(frequencies) - 1)
        local = index - part * section
        envelope = max(0, min(1, local / ramp, (section - 1 - local) / ramp))
        amplitude = 0.45 * 32767 * volume / 100 * envelope
        sample = round(amplitude * math.sin(2 * math.pi * frequencies[part] * local / PCM_RATE))
        data.extend(struct.pack('<h', sample))
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(PCM_RATE)
        stream.writeframes(bytes(data))
    return output.getvalue()


class FeedbackUnavailable(RuntimeError):
    pass


_SPEECH_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $voices = @($synth.GetInstalledVoices() | Where-Object {$_.Enabled})
  if ($voices.Count -eq 0) { throw 'No installed enabled voice' }
  $synth.Volume = [int]$request.volume
  $synth.SetOutputToDefaultAudioDevice()
  $synth.Speak([string]$request.text)
} finally { $synth.Dispose() }
'''


_BEEP_SCRIPT = '''
import sys
import winsound
raw = sys.stdin.buffer.read(8193)
if not 44 <= len(raw) <= 8192:
    raise ValueError('Invalid bounded notification')
winsound.PlaySound(raw, winsound.SND_MEMORY | winsound.SND_SYNC | winsound.SND_NODEFAULT)
'''


def _run_notification(command, request, *, cancelled, timeout):
    """Own only this short process; never stop another audio owner's output."""
    if cancelled():
        return False
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    start = time.monotonic()
    first = True
    try:
        while True:
            if cancelled():
                return False
            if time.monotonic() - start > timeout:
                raise FeedbackUnavailable('Operation audio did not complete within its notification limit.')
            try:
                process.communicate(input=request if first else None, timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                first = False
        if process.returncode != 0:
            raise FeedbackUnavailable('Operation audio output is unavailable; check the installed audio support.')
        return True
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
            process.wait(timeout=2)


class WindowsFeedbackAdapter:
    """Real optional Windows output, used only by a dedicated feedback worker.

SAPI availability is established by the actual installed voice call. Neither
Windows presence nor a PowerShell executable is reported as speech success.
Speech text travels as JSON stdin, never interpolated into executable code.
"""
    def __init__(self):
        self.speech_available = None

    def play(self, raw, *, cancelled):
        if cancelled():
            return
        if os.name != 'nt':
            raise FeedbackUnavailable('Operation beeps require Windows audio output on this installation.')
        # SND_MEMORY cannot be combined with SND_ASYNC; this bounded playback
        # runs in its own process. PlaySound is process-global: calling it here
        # would interrupt Audio Studio's independently owned PlaySound stream.
        _run_notification([sys.executable, '-I', '-c', _BEEP_SCRIPT], raw,
                          cancelled=cancelled, timeout=3)

    def speak(self, text, *, volume, cancelled):
        if cancelled():
            return
        if os.name != 'nt':
            self.speech_available = False
            raise FeedbackUnavailable('Operation speech requires Windows SAPI and an installed enabled voice.')
        binary = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not binary.is_file():
            self.speech_available = False
            raise FeedbackUnavailable('Windows SAPI notification support is unavailable.')
        request = json.dumps({'text': text[:240], 'volume': volume}, ensure_ascii=True).encode('utf-8')
        command = [str(binary), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                   base64.b64encode(_SPEECH_SCRIPT.encode('utf-16-le')).decode('ascii')]
        try:
            if _run_notification(command, request, cancelled=cancelled, timeout=10):
                self.speech_available = True
        except FeedbackUnavailable:
            self.speech_available = False
            raise FeedbackUnavailable('Windows speech is unavailable; check installed enabled SAPI voices.') from None


class ProgressFeedback:
    """Single bounded coalescing audio worker, quiet unless explicitly enabled.

Queued work is withdrawn on mute, settings changes, media activity and close.
The external busy callback must only inspect thread-safe host state, never wx.
"""
    def __init__(self, preferences=None, *, adapter=None, media_busy=lambda: False,
                 max_tasks=32, clock=time.monotonic):
        self.preferences = preferences or FeedbackPreferences()
        encode_preferences(self.preferences)
        if type(max_tasks) is not int or not 1 <= max_tasks <= 256:
            raise ValueError('Invalid feedback task limit.')
        self.adapter = adapter or WindowsFeedbackAdapter()
        self.media_busy, self.clock, self.max_tasks = media_busy, clock, max_tasks
        self._condition = threading.Condition(threading.RLock())
        self._pending = OrderedDict()
        self._last = None
        self._generation = 0
        self._closed = self._background = self._playback = self._recording = False
        self._notice = ''
        self._thread = None

    @property
    def notice(self):
        with self._condition:
            return self._notice

    def configure(self, preferences):
        encode_preferences(preferences)
        with self._condition:
            self.preferences = preferences
            self._generation += 1
            self._pending.clear()
            self._last = None
            self._notice = ''
            self._condition.notify_all()

    def set_media_state(self, *, playback=False, recording=False):
        if type(playback) is not bool or type(recording) is not bool:
            raise ValueError('Playback and recording states must be booleans.')
        with self._condition:
            self._playback, self._recording = playback, recording
            if playback or recording:
                self._generation += 1
                self._pending.clear()
            self._condition.notify_all()

    def set_background(self, background):
        if type(background) is not bool:
            raise ValueError('The background state must be a boolean.')
        with self._condition:
            self._background = background
            if background and not self.preferences.background:
                self._generation += 1
                self._pending.clear()
            self._condition.notify_all()

    def _quiet(self):
        preferences = self.preferences
        try:
            media = bool(self.media_busy())
        except Exception:
            media = True
        return (self._closed or preferences.mode == 'Off' or preferences.muted
                or preferences.volume == 0 or self._playback or self._recording or media
                or (self._background and not preferences.background))

    def submit(self, event, *, manual=False):
        if type(event) is not ProgressEvent:
            raise ValueError('A validated progress event is required.')
        with self._condition:
            if self._quiet():
                return False
            self._pending[event.handle] = (event, bool(manual))
            self._pending.move_to_end(event.handle)
            while len(self._pending) > self.max_tasks:
                self._pending.popitem(last=False)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='operation-feedback', daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def _cancelled(self, generation):
        with self._condition:
            return generation != self._generation or self._quiet()

    def _run(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                if not self._pending:
                    self._condition.wait()
                    continue
                if self._quiet():
                    self._pending.clear()
                    continue
                key, (event, manual) = next(iter(self._pending.items()))
                wait = 0 if self._last is None or manual else self.preferences.interval - (self.clock() - self._last)
                if wait > 0:
                    self._condition.wait(timeout=min(wait, 0.1))
                    continue
                self._pending.pop(key)
                preferences, generation = self.preferences, self._generation
                self._last = self.clock()
            cancelled = lambda: self._cancelled(generation)
            try:
                if 'Beep' in preferences.mode and not cancelled():
                    self.adapter.play(progress_wave(event.percent, volume=preferences.volume, state=event.state),
                                      cancelled=cancelled)
                if 'Speech' in preferences.mode and not cancelled():
                    self.adapter.speak(event.describe(), volume=preferences.volume, cancelled=cancelled)
            except Exception as exc:
                with self._condition:
                    # Only our fixed capability messages are user-visible; an
                    # adapter's arbitrary diagnostic never becomes UI content.
                    self._notice = (str(exc) if type(exc) is FeedbackUnavailable else
                                    'Operation audio feedback is unavailable; visual progress remains active.')
                    self._pending.clear()

    def close(self):
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending.clear()
            self._condition.notify_all()
