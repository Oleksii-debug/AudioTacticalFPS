"""Explicit local voice and real media state for the shared progress observer.

This adapter uses PR99's existing VoiceRegistry and WavePlayer. It owns no
speech engine, recording device, render graph, persisted voice choice or model.
Temporary notification audio never enters a production project or export.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from typing import Callable

from .engines import VoiceRegistry, _wave_info
from .player import WavePlayer


MAX_SPOKEN_CHARACTERS = 240
MAX_NOTIFICATION_BYTES = 8 * 1024 * 1024
MAX_NOTIFICATION_SECONDS = 20
COOPERATIVE_DEADLINE_SECONDS = 30


def _flag(value):
    try:
        return bool(value() if callable(value) else value)
    except Exception:
        return True


class RecordingLease:
    """Keep this lease until the real recorder has closed its microphone.

    Request first, poll ``ready`` without blocking wx, then open the microphone.
    False means a notification is still active or its stop was not confirmed.
    This object does not itself implement or start a recorder.
    """
    def __init__(self, bridge, token):
        self._bridge, self._token = bridge, token

    @property
    def ready(self):
        return self._bridge._recording_ready(self._token)

    def close(self):
        with self._bridge._lock:
            self._bridge._recordings.discard(self._token)


class ProgressMediaBridge:
    """Poll actual registered transports on their owning desktop thread."""
    def __init__(self, frame):
        self.frame = frame
        self._lock = threading.RLock()
        self._players = {}
        self._recordings = set()
        self._notifications = set()
        self._uncertain_playback = False
        self._previous_media = getattr(frame, 'progress_media_busy', False)
        self._previous_recording = getattr(frame, 'progress_recording_active', False)

    @property
    def feedback(self):
        return getattr(getattr(self.frame, 'progress_panel', None), 'feedback', None)

    def register_playback(self, getter: Callable[[], WavePlayer | None]):
        if not callable(getter):
            raise ValueError('A live playback getter is required.')
        token = object()
        with self._lock:
            if len(self._players) >= 32:
                raise ValueError('Too many open playback transports.')
            self._players[token] = getter

        def remove():
            uncertain = False
            try:
                player = getter()
                uncertain = player is not None and not player.silence_confirmed
            except Exception:
                uncertain = True
            with self._lock:
                self._players.pop(token, None)
                self._uncertain_playback |= uncertain
        return remove

    def media_busy(self):
        if _flag(self._previous_media):
            return True
        with self._lock:
            if self._uncertain_playback:
                return True
            getters = tuple(self._players.values())
        for getter in getters:
            try:
                player = getter()
                if player is not None and (player.state not in ('stopped', 'paused', 'closed')
                                           or (player.state in ('stopped', 'closed')
                                               and not player.silence_confirmed)):
                    return True
            except Exception:
                return True
        return False

    @property
    def recording_requested(self):
        """Thread-safe preflight state; never calls a native UI callback."""
        with self._lock:
            return bool(self._recordings)

    def recording_active(self):
        return self.recording_requested or _flag(self._previous_recording)

    def before_playback(self):
        feedback = self.feedback
        if feedback is not None and feedback.preferences.quiet_during_media:
            feedback.suspend()

    def begin_recording(self):
        """Suppress first; the owner may open its microphone only when ready."""
        token = object()
        with self._lock:
            if len(self._recordings) >= 32:
                raise ValueError('Too many pending recording requests.')
            self._recordings.add(token)
        feedback = self.feedback
        if feedback is not None:
            feedback.suspend()
        return RecordingLease(self, token)

    def _recording_ready(self, token):
        with self._lock:
            if token not in self._recordings or self._notifications or self._uncertain_playback:
                return False
        feedback = self.feedback
        return feedback is None or feedback.quiescent

    def _notification_started(self):
        token = object()
        with self._lock:
            if self._recordings:
                raise InterruptedError('Progress audio is suppressed for recording.')
            if len(self._notifications) >= 32:
                raise RuntimeError('A previous notification could not be confirmed stopped.')
            self._notifications.add(token)
        return token

    def _notification_stopped(self, token, *, confirmed):
        if confirmed:
            with self._lock:
                self._notifications.discard(token)
        # Uncertain native cleanup deliberately leaves recording blocked.


def install_media_progress(frame):
    existing = getattr(frame, 'progress_media_bridge', None)
    if existing is not None:
        if not isinstance(existing, ProgressMediaBridge):
            raise ValueError('The existing media-progress adapter is incompatible.')
        return existing
    bridge = ProgressMediaBridge(frame)
    frame.progress_media_bridge = bridge
    frame.progress_media_busy = bridge.media_busy
    frame.progress_recording_active = bridge.recording_active
    return bridge


class LocalVoiceProgressSpeech:
    """Speak with the user's explicitly selected existing PR99 local voice.

    Called only by ProgressFeedback's worker. The monotonic deadline is a
    cooperative cancellation request: existing provider preflight may finish
    before observing it. Expired/cancelled synthesis can never start playback.
    Each call owns its player instance and never changes another app's volume.
    """
    def __init__(self, registry: VoiceRegistry, *, voice_id: str, language: str,
                 media: ProgressMediaBridge | None = None,
                 _player_factory=WavePlayer, _clock=time.monotonic):
        if not isinstance(registry, VoiceRegistry):
            raise ValueError('Select a voice from the existing local audio registry.')
        voices = {voice.id: voice for voice in registry.voices() if voice.available}
        if not isinstance(voice_id, str) or voice_id not in voices:
            raise ValueError('Select an available installed local voice for progress.')
        voice = voices[voice_id]
        if (not isinstance(language, str) or not language
                or not any(language.casefold().replace('_', '-') == item.casefold().replace('_', '-')
                           or ('-' not in language and '_' not in language
                               and language.casefold() == item.replace('_', '-').split('-')[0].casefold())
                           for item in voice.languages)):
            raise ValueError('The selected voice does not declare this progress language.')
        probe = _player_factory()
        try:
            if not probe.available:
                raise ValueError('No supported local notification playback transport is available.')
        finally:
            probe.close()
        self.registry, self.voice_id, self.language = registry, voice_id, language
        self.media, self._player_factory, self._clock = media, _player_factory, _clock
        self._run_lock = threading.Lock()

    def speak_progress(self, text: str, *, volume: int, cancelled: Callable[[], bool]):
        if (not isinstance(text, str) or not text.strip() or '\x00' in text
                or type(volume) is not int or not 0 <= volume <= 100
                or not callable(cancelled)):
            raise ValueError('A bounded progress announcement and volume are required.')
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError('A progress announcement is already active.')
        deadline = self._clock() + COOPERATIVE_DEADLINE_SECONDS

        def interrupted():
            return cancelled() or bool(self.media and self.media.recording_requested)

        def expired():
            return self._clock() >= deadline

        def cancel_request():
            return interrupted() or expired()

        try:
            if interrupted() or volume == 0:
                return
            with TemporaryDirectory(prefix='carr-progress-voice-') as directory:
                output = Path(directory) / 'notification.wav'
                try:
                    self.registry.synthesize(text[:MAX_SPOKEN_CHARACTERS], output,
                        voice=self.voice_id, language=self.language, rate=0, pitch=0,
                        volume=volume / 100, cancelled=cancel_request)
                except InterruptedError:
                    if interrupted():
                        return
                    if expired():
                        raise TimeoutError('The progress voice exceeded its cooperative time allowance.') from None
                    raise
                if interrupted():
                    return
                if expired():
                    raise TimeoutError('The progress voice exceeded its cooperative time allowance.')
                if output.stat().st_size > MAX_NOTIFICATION_BYTES:
                    raise ValueError('The local progress voice exceeded its waveform budget.')
                info = _wave_info(output)
                if info['frames'] / info['sample_rate'] > MAX_NOTIFICATION_SECONDS:
                    raise ValueError('The local progress announcement is too long.')
                player = self._player_factory()
                token = None
                try:
                    if cancel_request():
                        return
                    if self.media:
                        try:
                            token = self.media._notification_started()
                        except InterruptedError:
                            if interrupted():
                                return
                            raise
                    if cancel_request():
                        return
                    player.play(output)
                    while not cancel_request():
                        state = player.state
                        if state == 'stopped':
                            break
                        if state != 'playing':
                            raise RuntimeError('The progress playback transport did not complete normally.')
                        threading.Event().wait(0.025)
                    if expired() and not interrupted():
                        raise TimeoutError('The progress announcement exceeded its time allowance.')
                finally:
                    try:
                        player.close()
                    finally:
                        if self.media and token is not None:
                            self.media._notification_stopped(token,
                                confirmed=bool(player.silence_confirmed))
                    if not player.silence_confirmed:
                        raise RuntimeError('Progress playback could not be confirmed stopped; recording remains blocked.')
        finally:
            self._run_lock.release()
