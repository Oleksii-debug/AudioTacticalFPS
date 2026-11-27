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


class _RegistryOwner:
    """Keep an activated registry until all selections and active calls release it."""

    def __init__(self, registry):
        self.registry = registry
        self._lock = threading.Lock()
        self._references = 1

    def retain(self):
        with self._lock:
            if not self._references:
                raise ValueError('The selected progress voice is closed.')
            self._references += 1

    def release(self):
        with self._lock:
            self._references -= 1
            last = self._references == 0
        if last:
            # Registry.close releases every provider, including a warm private
            # worker. Never run it while another selection is synthesizing.
            self.registry.close()


class LocalProgressSpeech:
    """One selected voice, bounded private PCM, and an independently owned player."""

    def __init__(self, registry, voice_id, *, library_root=None,
                 _player_factory=WavePlayer, _clock=time.monotonic, _guard=lambda: None,
                 _registry_owner=None):
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
        # Direct construction borrows a caller's registry. Explicit activation
        # owns its new registry; copied views share that lifetime safely.
        self._registry_owner = _registry_owner
        self._possibly_audible = False
        self._retained = None

    @property
    def quiescent(self):
        # Set before the final cancellation check and play call. A caller that
        # cancels first can safely admit content playback while synthesis exits.
        return not self._possibly_audible

    def close(self):
        """Cancel this selection without touching any other audio device/session."""
        with self._selection_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        if self._registry_owner is not None:
            self._registry_owner.release()

    def copy_selection(self):
        """An independent notification player for another native progress view."""
        self._guard()
        owner = self._registry_owner
        with self._selection_lock:
            if self._closed.is_set():
                raise ValueError('The selected progress voice is closed.')
            if owner is not None:
                owner.retain()
        try:
            return LocalProgressSpeech(self.registry, self.voice.id, library_root=self.library_root,
                _player_factory=self._player_factory, _clock=self._clock, _guard=self._guard,
                _registry_owner=owner)
        except BaseException:
            if owner is not None:
                owner.release()
            raise

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
        temporary = player = owner = None
        try:
            with self._selection_lock:
                if self._closed.is_set():
                    return False
                if self._registry_owner is not None:
                    self._registry_owner.retain()
                    owner = self._registry_owner
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
                    self._run_lock.release()
                    if owner is not None:
                        owner.release()


def activate_progress_voice(library_root, voice_id, *, _registry_factory=None):
    """Explicitly reauthorize the selected ID against this library's settings."""
    from ..runtime_settings import load_settings, voice_registry

    root = local_path(library_root, exists=True)
    regular(root, directory=True)
    state_root = root / 'state'
    settings = load_settings(state_root)
    registry = (_registry_factory or voice_registry)(state_root)

    def guard():
        if load_settings(state_root) != settings:
            raise ValueError('Local engine settings changed. Select the progress voice again.')

    owner = _RegistryOwner(registry)
    try:
        guard()
        return LocalProgressSpeech(registry, voice_id, library_root=root, _guard=guard,
                                   _registry_owner=owner)
    except BaseException:
        owner.release()
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
