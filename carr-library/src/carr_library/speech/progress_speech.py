"""Private progress speech through an explicitly selected, existing local voice.

This adapter does not discover or install an engine on a progress tick. Explicit
activation uses the same approved runtime configuration as Reader and Studio.
The small temporary WAV belongs only to the notification and never enters a
reading copy, production graph, cache or exported recording.
"""
from contextlib import ExitStack
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

from ..durable_state import local_path, regular
from ..audio.cleanup import _pending_owners, retained_context
from ..operations.models import ResourceCancelled
from .player import WavePlayer
from .runtime import SpeechSettings, VoiceRegistry
from .synthesis_admission import ReaderSynthesisRequest

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


class _OwnedProgressRegistry:
    """Share one explicitly created registry until its last selection finishes."""
    def __init__(self, registry):
        self.registry = registry
        self._lock = threading.Lock()
        self._selections, self._active = 1, 0
        self._closing = False

    def retain(self):
        with self._lock:
            if not self._selections or self._closing:
                raise RuntimeError('The progress voice selection is closed.')
            self._selections += 1

    def enter(self):
        with self._lock:
            if not self._selections or self._closing:
                return False
            self._active += 1
            return True

    def leave(self):
        with self._lock:
            self._active -= 1
        self._close_if_unused()

    def release(self):
        with self._lock:
            self._selections -= 1
        self._close_if_unused()

    def _close_if_unused(self):
        with self._lock:
            if self._selections or self._active or self._closing:
                return
            self._closing = True
        # VoiceRegistry.close may wait for its owned neural worker. Never run
        # that wait on the GUI thread or while a surviving selection uses it.
        # A non-daemon cleanup finishes before normal interpreter shutdown.
        threading.Thread(target=self.registry.close, name='progress-voice-cleanup',
                         daemon=False).start()


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
        # Directly injected registries remain borrowed. Only explicit runtime
        # activation creates an owner, shared by copies without rediscovery.
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
        with self._selection_lock:
            if self._closed.is_set():
                raise RuntimeError('The progress voice selection is closed.')
            if self._registry_owner is not None:
                self._registry_owner.retain()
        try:
            return LocalProgressSpeech(self.registry, self.voice.id, library_root=self.library_root,
                _player_factory=self._player_factory, _clock=self._clock, _guard=self._guard,
                _registry_owner=self._registry_owner)
        except BaseException:
            if self._registry_owner is not None:
                self._registry_owner.release()
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
        lifetime = ExitStack()
        lifetime.callback(self._run_lock.release)
        # These contexts already belong to this notification. A pending native
        # family retains the exact outer files, serialization and registry use
        # until its existing cleanup owners confirm quiescence.
        with retained_context(lifetime):
            temporary = player = None
            try:
                if self._registry_owner is not None:
                    if not self._registry_owner.enter():
                        return False
                    lifetime.callback(self._registry_owner.leave)
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

                def verify(*, rendering):
                    # Activation's guard reads saved settings only. This request
                    # is created on the existing feedback worker, so its direct
                    # owner check never accesses wx or another window's controls.
                    self._guard()
                    if self.registry.get(self.voice.id) != self.voice:
                        raise ValueError('The selected progress voice changed. Select it again.')

                request = ReaderSynthesisRequest(self.registry, self.voice,
                    SpeechSettings(volume=round(volume * 100), timeout_seconds=20,
                                   max_output_bytes=2 * 1024 * 1024),
                    root=self.library_root, cancelled=stopped,
                    dispatch=lambda callback: callback(), verify=verify)
                result = request.run(text, output,
                    admission_timeout=max(0.0, deadline - self._clock()))
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
            except (InterruptedError, ResourceCancelled) as error:
                if _pending_owners(error):
                    raise
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
                    if temporary is not None:
                        lifetime.callback(temporary.cleanup)


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

    owner = _OwnedProgressRegistry(registry)
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
