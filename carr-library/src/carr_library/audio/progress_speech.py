"""Session-selected progress speech through the existing local CPU voice adapter.

Reuses canonical speech/progress_speech.py at 1eff701fe848a3bd9eb088c85e5372a7c0a73861
(blob 761717f8bde4b39168b61e570bf5e34ea557c5b2): localized notices, independent
private PCM/player ownership, bounded delivery and no substitution. The product's
VoiceRegistry, engine processes, WavePlayer and ResourceBroker remain authority.
"""
from contextlib import nullcontext
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

from ..durable_state import local_path, regular
from .engines import VoiceRegistry, _wave_info
from .player import WavePlayer

MAX_NOTIFICATION_BYTES = 2 * 1024 * 1024
MAX_STAGING_BYTES = MAX_NOTIFICATION_BYTES + 32768
CPU_NOTIFICATION_ENGINES = frozenset(('sapi5', 'sapi5-32', 'espeak-ng', 'flite'))

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


def _language(value):
    if not isinstance(value, str):
        raise ValueError('Select an advertised language for the progress voice.')
    return value.replace('_', '-').split('-')[0].lower()


def _staging_size(root):
    """Cooperative inventory check inside this notification's private directory."""
    pending, count, total = [Path(root)], 0, 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                count += 1
                if count > 32:
                    raise ValueError('The progress voice exceeded its private file budget.')
                path = Path(entry.path)
                directory = entry.is_dir(follow_symlinks=False)
                # DirEntry identities are zero on Windows. Use the existing
                # lstat-based primitive to reject links/reparse points and count
                # every delivered regular file instead of deduplicating zeros.
                info = regular(path, directory=directory)
                if directory:
                    pending.append(path)
                else:
                    total += info.st_size
                if total > MAX_STAGING_BYTES:
                    raise ValueError('The progress voice exceeded its private byte budget.')
    return total


class LocalProgressSpeech:
    """One explicit CPU voice, bounded private PCM and an independently owned player."""

    def __init__(self, registry, voice_id, *, language=None, library_root=None,
                 _player_factory=WavePlayer, _clock=time.monotonic, _guard=lambda: None):
        if not isinstance(registry, VoiceRegistry):
            raise ValueError('Use the configured local voice registry for progress speech.')
        matches = tuple(voice for voice in registry.voices() if voice.id == voice_id and voice.available)
        if len(matches) != 1:
            raise ValueError('Choose one available installed progress voice; no substitute was used.')
        voice = matches[0]
        languages = tuple(dict.fromkeys(_language(value) for value in voice.languages))
        selected = _language(language) if language is not None else (languages[0] if len(languages) == 1 else None)
        if selected not in _NOTICES or selected not in languages:
            raise ValueError('Select an advertised English, Ukrainian or Slovak language for progress notices.')
        if 'volume' not in voice.controls:
            raise ValueError('This voice does not expose volume control. Choose another progress voice.')
        if voice.engine not in CPU_NOTIFICATION_ENGINES:
            raise ValueError('Progress notices currently use installed CPU SAPI5, eSpeak NG or Flite voices. '
                             'Neural notification device placement is not qualified; no substitute was used.')
        self.registry, self.voice = registry, voice
        self.library_root = local_path(library_root, exists=True) if library_root is not None else None
        if self.library_root is not None:
            regular(self.library_root, directory=True)
        self.language = selected
        self.label = f'{voice.name}; {selected}; {voice.engine}'
        self._player_factory, self._clock, self._guard = _player_factory, _clock, _guard
        self._run_lock, self._closed = threading.Lock(), threading.Event()
        self._possibly_audible, self._retained = False, None
        self.last_notice = ''

    @property
    def quiescent(self):
        return not self._possibly_audible

    def close(self):
        """Cancel this selection; owned device cleanup stays on the feedback worker."""
        self._closed.set()

    def copy_selection(self):
        self._guard()
        return LocalProgressSpeech(self.registry, self.voice.id, language=self.language,
            library_root=self.library_root, _player_factory=self._player_factory,
            _clock=self._clock, _guard=self._guard)

    def notification_text(self, event, *, state=None):
        words = _NOTICES[self.language]
        key = state or event.state
        key = 'succeeded' if key == 'completed' else key
        if key not in words:
            raise ValueError('Unknown progress state.')
        text = words[key] + '. '
        if event.percent is not None:
            text += f'{event.percent} {words["percent"]}. '
        elif event.state == 'running':
            text += words['unknown'] + ' '
        # Arbitrary English phase text is not silently translated or read by
        # a Ukrainian/Slovak voice. Its full accessible label remains in the UI.
        if self.language == 'en':
            text += ''.join(' ' if ord(c) < 32 else c for c in event.phase[:160]) + '.'
        return text.strip()

    def _reservation(self, directory, cancelled):
        if self.library_root is None:  # Explicit standalone adapter/test use.
            return nullcontext(None)
        from ..operations import ResourceBroker, ResourceRequest
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024 * 1024,
                                  disk_bytes=MAX_STAGING_BYTES, disk_path=directory)
        return ResourceBroker.for_app(self.library_root).reserve(
            request, operation_id='progress-voice:' + uuid4().hex,
            cancelled=cancelled, timeout=.05)

    def speak_progress(self, text, *, volume, cancelled, admit=lambda operation: operation()):
        if (not isinstance(text, str) or not text.strip() or len(text) > 240
                or any(ord(character) < 32 for character in text)):
            raise ValueError('Progress speech needs at most 240 readable characters.')
        if type(volume) not in (int, float) or not 0 <= volume <= 1 or not math.isfinite(volume):
            raise ValueError('Progress speech volume must be between zero and one.')
        if not callable(cancelled) or not callable(admit):
            raise ValueError('Progress speech needs cancellation and playback admission callbacks.')
        if volume == 0 or self._closed.is_set() or cancelled():
            return False
        if not self._run_lock.acquire(blocking=False):
            return False
        temporary = player = None
        try:
            if not self.quiescent:
                raise RuntimeError('The previous progress audio device has not confirmed silence.')
            self._guard()
            if self.voice not in self.registry.voices():
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
            directory = Path(temporary.name)
            output = directory / 'notification.wav'
            synth_deadline = self._clock() + 20.0

            def synthesis_stopped():
                _staging_size(directory)
                return stopped() or self._clock() >= synth_deadline

            from ..operations import ResourceCancelled, ResourceTimeout, ResourceUnavailable
            try:
                with self._reservation(directory, synthesis_stopped) as lease:
                    result = self.registry.synthesize(text, output, voice=self.voice.id,
                        language=self.language, volume=volume,
                        cancelled=lambda: synthesis_stopped() or (lease is not None and lease.cancelled()))
            except (ResourceCancelled, ResourceTimeout, ResourceUnavailable):
                self.last_notice = 'A progress notice was dropped because local resources were busy.'
                return False
            _staging_size(directory)
            if synthesis_stopped():
                return False
            if Path(result) != output or not 0 < regular(output).st_size <= MAX_NOTIFICATION_BYTES:
                raise ValueError('The progress voice exceeded its private notification byte budget.')
            info = _wave_info(output)
            if not 0 < info['frames'] / info['sample_rate'] <= 20:
                raise ValueError('The progress voice exceeded its private notification duration.')
            self._guard()

            def start():
                if stopped():
                    return False
                self._possibly_audible = True
                if stopped():
                    self._possibly_audible = False
                    return False
                player.play(output)
                return True

            if not admit(start):
                return False
            while True:
                if stopped():
                    return False
                state = player.state
                if state == 'stopped':
                    self.last_notice = ''
                    return True
                if state not in ('starting', 'playing'):
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
                    except Exception:
                        # Includes a real ffplay subprocess.TimeoutExpired: an
                        # unsuccessful close never proves native ownership ended.
                        # An error status alone is not proof that a native driver
                        # released its buffers. Retain ownership and keep content
                        # admission closed until silence is actually confirmed.
                        self._possibly_audible = True
                        self._retained = (player, temporary)
                        temporary = None
                        raise
            finally:
                try:
                    if temporary is not None:
                        temporary.cleanup()
                finally:
                    self._run_lock.release()


def activate_progress_voice(library_root, registry, voice_id, *, language, guard=lambda: None):
    """Called only by an explicit selection, never from a progress tick."""
    root = local_path(library_root, exists=True)
    regular(root, directory=True)
    guard()
    return LocalProgressSpeech(registry, voice_id, language=language, library_root=root, _guard=guard)
