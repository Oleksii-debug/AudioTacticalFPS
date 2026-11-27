"""Private progress speech through an explicitly selected, existing local voice.

This adapter does not discover or install an engine on a progress tick. Explicit
activation uses the same approved runtime configuration as Reader and Studio.
The small temporary WAV belongs only to the notification and never enters a
reading copy, production graph, cache or exported recording.
"""
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

from ..durable_state import local_path, regular
from .player import WavePlayer
from .runtime import SpeechSettings, VoiceRegistry

_NOTICES = {
    'en': {'running': 'In progress', 'paused': 'Paused', 'resumed': 'Resumed',
           'succeeded': 'Completed', 'failed': 'Failed', 'cancelled': 'Cancelled',
           'incomplete': 'Completion is not confirmed', 'percent': 'percent',
           'unknown': 'Total work is not yet known.'},
    'uk': {'running': 'Робота триває', 'paused': 'Призупинено', 'resumed': 'Роботу відновлено',
           'succeeded': 'Завершено', 'failed': 'Помилка', 'cancelled': 'Скасовано',
           'incomplete': 'Завершення не підтверджено', 'percent': 'відсотків',
           'unknown': 'Загальний обсяг роботи поки невідомий.'},
    'sk': {'running': 'Prebieha', 'paused': 'Pozastavené', 'resumed': 'Pokračuje sa',
           'succeeded': 'Dokončené', 'failed': 'Chyba', 'cancelled': 'Zrušené',
           'incomplete': 'Dokončenie nie je potvrdené', 'percent': 'percent',
           'unknown': 'Celkový rozsah práce zatiaľ nie je známy.'},
}


# The shared ProgressHub distinguishes a requested stop from acknowledgement,
# and a missing observation from a failed operation. Neither means completion.
for _language, _cancelling, _unavailable in (
        ('en', 'Cancellation requested', 'Progress reporting is unavailable'),
        ('uk', 'Надіслано запит на скасування', 'Повідомлення про прогрес недоступні'),
        ('sk', 'Požiadavka na zrušenie', 'Hlásenie priebehu nie je dostupné')):
    _NOTICES[_language].update(cancelling=_cancelling, unavailable=_unavailable)


class SnapshotProgressSpeech:
    """Adapt the selected local voice to ProgressFeedback's 0..100 contract."""
    def __init__(self, provider):
        self.provider = provider
        self.library_root, self.label = provider.library_root, provider.label

    @property
    def quiescent(self):
        return self.provider.quiescent

    def format_progress(self, snapshot, *, kind):
        from types import SimpleNamespace
        event = SimpleNamespace(state=snapshot.state, percent=snapshot.percent,
                                phase=' '.join(snapshot.phase.split())[:120])
        return self.provider.notification_text(event, state='resumed' if kind == 'resumed' else None)

    def speak_progress(self, text, *, volume, cancelled):
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError('Progress volume must be an integer from zero to one hundred.')
        return self.provider.speak_progress(text, volume=volume / 100, cancelled=cancelled)

    def copy_selection(self):
        return SnapshotProgressSpeech(self.provider.copy_selection())

    def close(self):
        self.provider.close()


def snapshot_speech_provider(provider):
    return SnapshotProgressSpeech(provider) if isinstance(provider, LocalProgressSpeech) else provider


class _RegistryOwnership:
    """Share one explicitly activated registry without borrowing another view's."""
    def __init__(self, registry):
        self.registry, self.references = registry, 0
        self._lock = threading.Lock()
        self._closing = False
        self.finished = threading.Event()
        self.error = None

    def acquire(self):
        with self._lock:
            if self._closing:
                raise ValueError('This progress voice selection is closed. Select it again.')
            self.references += 1

    def release(self):
        with self._lock:
            self.references -= 1
            if self.references or self._closing:
                return
            self._closing = True
        def close():
            try:
                if self.registry.close() is False:
                    self.error = 'Local voice worker cleanup needs attention; its owner is retained for shutdown retry.'
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.finished.set()
        # Closing a warm native worker may wait for bounded process cleanup.
        # Notification preferences and window Close never wait for that here.
        threading.Thread(target=close, name='progress-voice-cleanup', daemon=False).start()


class LocalProgressSpeech:
    """One selected voice, bounded private PCM, and an independently owned player."""

    def __init__(self, registry, voice_id, *, library_root=None,
                 _player_factory=WavePlayer, _clock=time.monotonic, _guard=lambda: None,
                 _ownership=None):
        if not isinstance(registry, VoiceRegistry):
            raise ValueError('Use the configured local voice registry for progress speech.')
        voice = registry.get(voice_id)
        language = voice.language.replace('_', '-').split('-')[0].lower()
        if language not in _NOTICES:
            raise ValueError('Progress notices support English, Ukrainian and Slovak. Select a voice in one of these languages.')
        if 'volume' not in voice.capabilities:
            raise ValueError('This voice does not expose volume control. Choose another progress voice.')
        self.registry, self.voice, self.library_root = registry, voice, library_root
        self.language = language
        self.label = f'{voice.name}; {voice.language}; {voice.engine}'
        self._player_factory, self._clock, self._guard = _player_factory, _clock, _guard
        self._run_lock, self._closed = threading.Lock(), threading.Event()
        self._selection_lock = threading.Lock()
        self._possibly_audible = False
        self._retained = None
        self._ownership, self._ownership_released = _ownership, False
        if _ownership is not None:
            _ownership.acquire()

    @property
    def quiescent(self):
        # Set before the final cancellation check and play call. A caller that
        # cancels first can safely admit content playback while synthesis exits.
        return not self._possibly_audible

    def close(self):
        """Cancel this selection without touching any other audio device/session."""
        with self._selection_lock:
            self._closed.set()
            if self._run_lock.acquire(blocking=False):
                try:
                    self._release_ownership()
                finally:
                    self._run_lock.release()

    def _release_ownership(self):
        if self._ownership is not None and not self._ownership_released:
            self._ownership_released = True
            self._ownership.release()

    def copy_selection(self):
        """An independent notification player for another native progress view."""
        self._guard()
        with self._selection_lock:
            if self._closed.is_set():
                raise ValueError('This progress voice selection is closed. Select it again.')
            # Construction retains the same registry while close is excluded.
            # It does not rediscover or activate another voice engine.
            return LocalProgressSpeech(self.registry, self.voice.id, library_root=self.library_root,
                _player_factory=self._player_factory, _clock=self._clock, _guard=self._guard,
                _ownership=self._ownership)

    def notification_text(self, event, *, state=None):
        words = _NOTICES[self.language]
        text = words[state or event.state] + '. '
        if event.percent is not None:
            text += f'{event.percent} {words["percent"]}. '
        elif event.state == 'running':
            text += words['unknown'] + ' '
        # Phase descriptions originate in the English application UI. Keep them
        # fully accessible there instead of silently using the wrong voice
        # language or claiming a translation of an arbitrary producer message.
        if self.language == 'en':
            text += event.phase[:160] + '.'
        return text.strip()

    def speak_progress(self, text, *, volume, cancelled):
        if (not isinstance(text, str) or not text.strip() or len(text) > 240
                or any(ord(character) < 32 for character in text)):
            raise ValueError('Progress speech needs at most 240 readable characters.')
        if type(volume) not in (int, float) or not 0 <= volume <= 1 or not math.isfinite(volume):
            raise ValueError('Progress speech volume must be between zero and one.')
        if not callable(cancelled):
            raise ValueError('Progress speech needs a cancellation callback.')
        if volume == 0 or self._closed.is_set() or cancelled():
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        temporary = player = None
        try:
            if self._closed.is_set():
                return False
            if not self.quiescent:
                raise RuntimeError('The previous progress audio device has not confirmed silence.')
            self._guard()
            # No substitution if a configured voice was removed or disabled.
            if self.registry.get(self.voice.id) != self.voice:
                raise ValueError('The selected progress voice changed. Select it again.')
            deadline = self._clock() + 30.0

            def stopped():
                return self._closed.is_set() or cancelled() or self._clock() >= deadline

            if stopped():
                return False
            player = self._player_factory()
            if not player.available:
                raise RuntimeError('Local progress playback is unavailable on this computer.')
            temporary = TemporaryDirectory(prefix='carr-progress-voice-')
            output = Path(temporary.name) / 'notification.wav'
            result = self.registry.synthesize(text, self.voice.id, output,
                settings=SpeechSettings(volume=round(volume * 100), timeout_seconds=20,
                                        max_output_bytes=2 * 1024 * 1024),
                cancelled=stopped)
            if stopped():
                return False
            if (Path(result.path) != output or result.voice_id != self.voice.id
                    or not 0 < result.duration_seconds <= 20
                    or output.stat().st_size > 2 * 1024 * 1024):
                raise ValueError('The progress voice exceeded its private notification budget.')
            self._guard()
            self._possibly_audible = True
            if stopped():
                self._possibly_audible = False
                return False
            player.play(output)
            while True:
                if stopped():
                    return False
                state = player.state
                if state == 'stopped':
                    return True
                if state != 'playing':
                    raise RuntimeError('The progress playback device did not complete normally.')
                time.sleep(.02)
        except InterruptedError:
            if self._closed.is_set() or cancelled():
                return False
            raise
        finally:
            try:
                if player is not None:
                    try:
                        player.close()
                        self._possibly_audible = False
                    except (OSError, RuntimeError):
                        try:
                            quiet = player.state in ('stopped', 'paused', 'error')
                        except (OSError, RuntimeError):
                            quiet = False
                        self._possibly_audible = not quiet
                        if not quiet:
                            # Retain the owned device and its file rather than
                            # losing uncertain native playback on a failed close.
                            self._retained = (player, temporary)
                            temporary = None
                        raise
            finally:
                try:
                    if temporary is not None:
                        temporary.cleanup()
                finally:
                    if self._closed.is_set():
                        self._release_ownership()
                    self._run_lock.release()


def activate_progress_voice(library_root, voice_id, *, cancelled=lambda: False, _registry_factory=None):
    """Explicitly reauthorize the selected ID against this library's settings."""
    from ..runtime_settings import load_settings, voice_registry

    root = local_path(library_root, exists=True)
    regular(root, directory=True)
    state_root = root / 'state'
    settings = load_settings(state_root)
    from ._io import check_cancelled
    check_cancelled(cancelled)
    registry = (_registry_factory or voice_registry)(state_root, cancelled=cancelled)

    def guard():
        if load_settings(state_root) != settings:
            raise ValueError('Local engine settings changed. Select the progress voice again.')

    try:
        check_cancelled(cancelled)
        guard()
        return LocalProgressSpeech(registry, voice_id, library_root=root, _guard=guard,
                                   _ownership=_RegistryOwnership(registry))
    except BaseException:
        registry.close()
        raise


def play_when_quiet(controllers, start, *, schedule, alive, on_error, _clock=time.monotonic):
    """Poll owned notification quiescence on the GUI loop, never block the UI.

    ``schedule(milliseconds, callback)`` is normally wx.CallLater. Content
    players remain registered with OperationProgress for actual EOF/pause state.
    A cancelled/failed pending start releases every temporary mute hold.
    """
    unique = tuple(dict.fromkeys(controller for controller in controllers if controller is not None))
    holds = []

    def warning(controller, error):
        # Progress observers can be detached while a native playback start has
        # already succeeded. Preserve that outcome and record feedback failure.
        try:
            controller.reporting_error = f'Playback progress feedback is unavailable: {error}'
        except Exception:
            pass

    def release():
        # One detached view must not leave another controller permanently muted.
        for controller, hold in zip(unique, holds):
            try:
                hold.close()
            except Exception as exc:
                warning(controller, exc)

    try:
        for controller in unique:
            holds.append(controller.hold_audio())
    except Exception:
        release()
        raise
    deadline = _clock() + 3.0

    def attempt():
        try:
            if not alive():
                release()
                return
            if not all(hold.ready for hold in holds):
                if _clock() >= deadline:
                    raise RuntimeError('Progress audio has not confirmed silence. Playback was not started.')
                schedule(25, attempt)
                return
            start()
            # Update actual transport state before removing its quiet hold.
            # Once start succeeds, observer faults must not report it as failed.
            for controller in unique:
                try:
                    controller.refresh_audio_state()
                except Exception as exc:
                    warning(controller, exc)
            release()
        except Exception as exc:
            release()
            if alive():
                on_error(exc)

    attempt()
