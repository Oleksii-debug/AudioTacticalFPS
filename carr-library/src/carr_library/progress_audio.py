"""Optional notification earcons and local speech, isolated from rendered audio.

Only short synthetic UI cues are generated here. Production renderers never
import this module, receive its samples, or include notification files in a
project. The single daemon worker coalesces progress and never blocks wx.
"""
from array import array
from dataclasses import replace
import hashlib
import json
import io
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import wave

from .progress import ProgressEvent, ProgressPreferences

SAMPLE_RATE = 22050


def progress_frequency(percent):
    if type(percent) not in (int, float) or not 0 <= percent <= 100 or not math.isfinite(percent):
        raise ValueError('A progress pitch needs an actual percentage between 0 and 100.')
    return 220.0 * 4 ** (percent / 100)


def cue_notes(event):
    """(frequency Hz, duration seconds), with zero denoting a silent gap."""
    if event.state == 'complete' and event.verified:
        return ((660, .08), (0, .025), (880, .12))
    if event.state == 'failed':
        return ((440, .1), (0, .035), (220, .15))
    if event.state == 'cancelled':
        return ((330, .08), (0, .025), (220, .08))
    if event.state == 'paused':
        return ((330, .07), (0, .07), (330, .07))
    if event.transition == 'resume':
        return ((330, .07), (0, .025), (440, .07))
    if event.percent is None:
        return ((165, .06), (0, .09), (165, .06))
    return ((progress_frequency(event.percent), .09),)


def cue_wav(event, *, volume=.12):
    if not isinstance(event, ProgressEvent):
        raise ValueError('Notification audio requires a shared progress event.')
    if type(volume) not in (int, float) or not 0 <= volume <= 1 or not math.isfinite(volume):
        raise ValueError('Notification volume must be between 0 and 1.')
    samples = array('h')
    ramp = round(SAMPLE_RATE * .005)
    # A fixed -6dB peak at volume 1 leaves headroom; no production DSP.
    for frequency, duration in cue_notes(event):
        length = round(SAMPLE_RATE * duration)
        for index in range(length):
            envelope = min(1, index / ramp, (length - 1 - index) / ramp)
            samples.append(round(16000 * volume * envelope * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE))
                           if frequency else 0)
    if sys.byteorder != 'little':
        samples.byteswap()
    target = io.BytesIO()
    with wave.open(target, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.tobytes())
    return target.getvalue()


_NOTICES = {
    'en': {'running': 'In progress', 'complete': 'Complete', 'failed': 'Failed',
           'cancelled': 'Cancelled', 'paused': 'Paused', 'resume': 'Resumed',
           'unknown': 'Total unknown', 'percent': 'percent'},
    'uk': {'running': 'Робота триває', 'complete': 'Завершено', 'failed': 'Помилка',
           'cancelled': 'Скасовано', 'paused': 'Призупинено', 'resume': 'Відновлено',
           'unknown': 'Загальний обсяг невідомий', 'percent': 'відсотків'},
    'sk': {'running': 'Prebieha', 'complete': 'Dokončené', 'failed': 'Chyba',
           'cancelled': 'Zrušené', 'paused': 'Pozastavené', 'resume': 'Pokračuje sa',
           'unknown': 'Celkový rozsah nie je známy', 'percent': 'percent'},
}


def speech_text(event, language='en'):
    # No source paragraphs, paths or exception details enter notification speech.
    if language not in _NOTICES:
        raise ValueError('Select English, Ukrainian or Slovak for progress speech.')
    words = _NOTICES[language]
    value = event.title[:90] + '. '
    if event.transition == 'resume':
        value += words['resume'] + '. '
    value += words[event.state]
    return value + ('. ' + words['unknown'] + '.' if event.percent is None else
                    f'. {event.percent} ' + words['percent'] + '.')


def configured_registry():
    """Discover only existing local adapters; never install or download a voice."""
    from .audio.engines import default_registry
    from .audio.runtime import resolve_ffmpeg
    try:
        ffmpeg = resolve_ffmpeg()
    except (ValueError, OSError, RuntimeError):
        ffmpeg = None
    return default_registry(ffmpeg=ffmpeg)


def voice_fingerprint(registry, voice_id, language, *, cancelled=lambda: False):
    """Bind the explicit selection to current adapter/model/runtime identities."""
    if language not in _NOTICES:
        raise ValueError('Progress speech supports English, Ukrainian and Slovak notices.')
    voices = [voice for voice in registry.voices(cancelled=cancelled)
              if voice.id == voice_id and voice.available
              and language in {item.lower().replace('_', '-').split('-')[0]
                               for item in voice.languages}
              and 'volume' in voice.controls]
    if len(voices) != 1:
        raise ValueError('The selected local voice and notification language are unavailable.')
    voice = voices[0]
    identity = registry.cache_identity(voice_id, cancelled=cancelled)
    raw = json.dumps({'voice': voice_id, 'language': language, 'engine': voice.engine,
                      'revision': voice.revision, 'runtime': identity},
                     ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


class NotificationSchedule:
    """Deterministic throttle, shared across concurrent jobs; no sound I/O."""
    def __init__(self):
        self._sent = {}
        self._last = -math.inf

    @staticmethod
    def allowed(preferences, *, foreground, media_active):
        return (preferences.mode != 'off' and not preferences.muted and not preferences.quiet
                and preferences.volume > 0 and (foreground or preferences.background)
                and not (media_active and preferences.suppress_during_media))

    @staticmethod
    def signature(event):
        return event.state, event.percent, event.transition if event.transition in ('pause', 'resume') else '', event.phase

    def suppress(self, events, now):
        """Never replay an old terminal cue after quiet/background playback."""
        present = {e.operation_id for e in events}
        self._sent = {key: value for key, value in self._sent.items() if key in present}
        for event in events:
            if event.terminal or event.state == 'paused':
                self._sent[event.operation_id] = (now, self.signature(event))

    def choose(self, events, preferences, *, now, foreground=True, media_active=False):
        if not self.allowed(preferences, foreground=foreground, media_active=media_active):
            self.suppress(events, now)
            return None
        present = {e.operation_id for e in events}
        self._sent = {key: value for key, value in self._sent.items() if key in present}
        candidates = []
        for event in events:
            # The parent announces overall work. Its separately visible subtask
            # does not cause duplicate beeps/speech for the same nested job.
            if event.parent_id in present:
                continue
            signature = self.signature(event)
            last_time, last_signature = self._sent.get(event.operation_id, (-math.inf, None))
            repeating = event.state == 'running' and event.percent is None
            if signature == last_signature and not repeating:
                continue
            boundary = event.terminal or event.transition in ('pause', 'resume')
            interval = .25 if boundary else preferences.interval_seconds
            if now - self._last < interval or now - last_time < interval:
                continue
            candidates.append((not boundary, last_time, event.sequence, event))
        if not candidates:
            return None
        event = min(candidates, key=lambda row: row[:3])[-1]
        self._sent[event.operation_id] = (now, self.signature(event))
        self._last = now
        return event


class LocalNotificationOutput:
    """Private short cues through the current player and explicitly chosen voice.

    A persisted identity may reconnect only to that exact local voice/runtime.
    No first-English-voice fallback or provider request is permitted.
    """
    def __init__(self, *, registry_factory=configured_registry, player_factory=None):
        self._registry_factory, self._player_factory = registry_factory, player_factory
        self._registry = None
        self._possibly_audible = False
        self._retained = []

    @property
    def quiescent(self):
        return not self._possibly_audible and not self._retained

    def set_registry(self, registry):
        self._registry = registry

    def _play(self, path, cancelled):
        from .audio.player import WavePlayer
        player = (self._player_factory or WavePlayer)()
        if cancelled():
            player.close()
            return
        self._possibly_audible = True
        try:
            player.play(path)
            while not cancelled() and player.state in ('starting', 'playing'):
                time.sleep(.02)
            if player.state in ('error', 'unavailable') and not cancelled():
                raise RuntimeError('The local audio device could not play a progress notification.')
        finally:
            try:
                player.close()
            except Exception:
                # Retain the handle and do not claim silence after a failed
                # native close. Content transport must remain outside this cue.
                self._retained.append(player)
                raise
            else:
                self._possibly_audible = False

    def _temporary(self, name, write, cancelled):
        directory = TemporaryDirectory(prefix='carr-notification-')
        path = Path(directory.name) / name
        try:
            write(path)
            if not cancelled():
                self._play(path, cancelled)
        finally:
            if self.quiescent:
                directory.cleanup()
            else:
                self._retained.append(directory)

    def beep(self, raw, *, cancelled):
        if not cancelled():
            self._temporary('cue.wav', lambda path: path.write_bytes(raw), cancelled)

    def speak(self, text, *, volume, cancelled, selection=None):
        if cancelled():
            return
        if (not isinstance(selection, tuple) or len(selection) != 3
                or not all(isinstance(value, str) and value for value in selection)):
            raise RuntimeError('Choose a local progress voice in Progress settings. No substitute was used.')
        voice_id, language, expected = selection
        deadline = time.monotonic() + 30
        stopped = lambda: cancelled() or time.monotonic() >= deadline
        registry = self._registry
        if registry is None:
            registry = self._registry_factory()
            self._registry = registry
        if voice_fingerprint(registry, voice_id, language, cancelled=stopped) != expected:
            raise RuntimeError('The selected progress voice or runtime changed. Review and select it again.')
        def synthesize(path):
            if stopped():
                raise InterruptedError('Progress speech stopped.')
            registry.synthesize(text, path, voice=voice_id, language=language, volume=volume,
                                cancelled=stopped)
            if path.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeError('The local voice exceeded the short-notification audio budget.')
        self._temporary('progress.wav', synthesize, stopped)


class NotificationWorker:
    """Bounded latest-state worker. offer and settings changes never wait."""
    def __init__(self, preferences=None, *, output=None, clock=time.monotonic):
        self._preferences = preferences or ProgressPreferences()
        self._output = output or LocalNotificationOutput()
        self._clock = clock
        self._condition = threading.Condition()
        self._events = {}
        self._closed = False
        self._foreground, self._media_active = True, False
        self._revision = 0
        self._schedule = NotificationSchedule()
        self._error = ''
        self._active_output = False
        self._thread = None  # Off means no audio thread, device or subprocess.

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def quiescent(self):
        with self._condition:
            return (not self._active_output and
                    bool(getattr(self._output, 'quiescent', True)))

    def clear(self):
        """Discard a prior library/operation and cancel only our own output."""
        with self._condition:
            self._events.clear()
            self._schedule = NotificationSchedule()
            self._revision += 1
            self._condition.notify_all()

    def set_registry(self, registry):
        with self._condition:
            setter = getattr(self._output, 'set_registry', None)
            if callable(setter):
                setter(registry)
            self._revision += 1
            self._condition.notify_all()

    def configure(self, preferences):
        if not isinstance(preferences, ProgressPreferences):
            raise ValueError('Use validated progress preferences.')
        with self._condition:
            self._preferences = preferences
            self._revision += 1
            self._error = ''
            self._condition.notify_all()

    def environment(self, *, foreground, media_active):
        with self._condition:
            self._foreground, self._media_active = bool(foreground), bool(media_active)
            if not self._schedule.allowed(self._preferences,
                    foreground=self._foreground, media_active=self._media_active):
                self._schedule.suppress(tuple(self._events.values()), self._clock())
            self._condition.notify_all()

    def offer(self, events):
        values = tuple(events)
        if len(values) > 4096 or any(not isinstance(value, ProgressEvent) for value in values):
            raise ValueError('Use a bounded shared progress snapshot.')
        with self._condition:
            if self._closed:
                return
            self._events = {event.operation_id: event for event in values}
            if not self._schedule.allowed(self._preferences,
                    foreground=self._foreground, media_active=self._media_active):
                self._schedule.suppress(values, self._clock())
            if self._preferences.mode != 'off' and self._thread is None:
                self._thread = threading.Thread(target=self._run, name='progress-notifications', daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _cancelled(self, revision, event):
        with self._condition:
            current = self._events.get(event.operation_id)
            return (self._closed or self._revision != revision or current is None
                    or current.state != event.state
                    or not self._schedule.allowed(self._preferences,
                        foreground=self._foreground, media_active=self._media_active))

    def _run(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                preferences = self._preferences
                event = self._schedule.choose(tuple(self._events.values()), preferences,
                    now=self._clock(), foreground=self._foreground, media_active=self._media_active)
                if event is None:
                    self._condition.wait(timeout=.25 if self._events else None)
                    continue
                revision = self._revision
                self._active_output = True
            cancelled = lambda: self._cancelled(revision, event)
            try:
                if preferences.mode in ('beep', 'beep_speech') and not cancelled():
                    self._output.beep(cue_wav(event, volume=preferences.volume), cancelled=cancelled)
                if preferences.mode in ('speech', 'beep_speech') and not cancelled():
                    self._output.speak(speech_text(event, preferences.voice_language or 'en'),
                        volume=preferences.volume, cancelled=cancelled,
                        selection=(preferences.voice_id, preferences.voice_language, preferences.voice_fingerprint))
            except Exception as exc:
                # Expected preemption (quiet, new operation, content playback)
                # is not a failed voice and must not overwrite newer settings.
                if not cancelled() or not bool(getattr(self._output, 'quiescent', True)):
                    with self._condition:
                        self._error = 'Progress audio unavailable: ' + ' '.join(str(exc).split())[:300]
                        self._preferences = replace(self._preferences, mode='off')
                        self._revision += 1
            finally:
                with self._condition:
                    self._active_output = False
                    self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._events.clear()
            self._revision += 1
            self._condition.notify_all()
        # Never join or wait for an OS audio/speech device from the UI thread.
