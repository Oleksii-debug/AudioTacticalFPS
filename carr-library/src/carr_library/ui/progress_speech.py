"""Optional local progress speech through the canonical isolated SAPI adapter.

One background thread owns synthesis and playback. Only the latest pending
announcement is retained; quiet mode cancels it and the current utterance.
The private temporary PCM never enters a project, render graph or backup.
"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading


def _registry():
    from ..speech import VoiceRegistry
    from ..speech.runtime import NativeSapiProvider
    return VoiceRegistry((NativeSapiProvider(),))


def _player():
    from ..speech.player import WavePlayer
    return WavePlayer()


class LocalProgressSpeech:
    def __init__(self, *, registry_factory=None, player_factory=None):
        self.available = registry_factory is not None or sys.platform == 'win32'
        self.registry_factory = registry_factory or _registry
        self.player_factory = player_factory or _player
        self._condition = threading.Condition()
        self._muted = False
        self.closed = False
        self.last_error = None
        self._thread = None
        self._pending = None
        self._generation = 0

    @property
    def muted(self):
        with self._condition:
            return self._muted

    @muted.setter
    def muted(self, value):
        with self._condition:
            value = bool(value)
            if value and not self._muted:
                self._generation += 1
                self._pending = None
                self._condition.notify_all()
            self._muted = value

    def speak(self, text, *, volume):
        if not isinstance(text, str) or not text.strip() or len(text) > 1024:
            raise ValueError('A short progress announcement is required.')
        if type(volume) not in (int, float) or not 0 < volume <= 1:
            raise ValueError('Speech volume must be above zero and no greater than one.')
        with self._condition:
            if not self.available or self.closed or self._muted:
                return False
            self._generation += 1
            self._pending = (text, volume, self._generation)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='carr-progress-speech', daemon=True)
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = self._pending = None
                    self.available = False
                    self.last_error = 'Local progress speech is unavailable (worker could not start).'
                    return False
            self._condition.notify_all()
            return True

    def _cancelled(self, generation):
        with self._condition:
            return self.closed or self._muted or generation != self._generation

    def _run(self):
        registry = player = None
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self.closed or self._pending is not None)
                    if self.closed:
                        return
                    text, volume, generation = self._pending
                    self._pending = None
                try:
                    if registry is None:
                        registry = self.registry_factory()
                    voices = registry.voices()
                    if not voices:
                        raise RuntimeError('No installed native voice is available.')
                    if self._cancelled(generation):
                        continue
                    if player is None:
                        player = self.player_factory()
                    from ..speech import SpeechSettings
                    with TemporaryDirectory(prefix='resonance-progress-') as temporary:
                        path = Path(temporary) / 'progress.wav'
                        registry.synthesize(text, voices[0].id, path,
                            settings=SpeechSettings(volume=max(1, round(volume * 100)),
                                max_output_bytes=8 * 1024**2),
                            cancelled=lambda: self._cancelled(generation))
                        try:
                            if self._cancelled(generation):
                                continue
                            player.play(path)
                            while player.state == 'playing':
                                if self._cancelled(generation):
                                    break
                                with self._condition:
                                    self._condition.wait(timeout=.05)
                        finally:
                            player.close()
                except Exception as exc:
                    if not self._cancelled(generation):
                        # Never echo a provider exception containing private
                        # source paths or voice details to the global status.
                        self.last_error = 'Local progress speech is unavailable (' + type(exc).__name__ + ').'
                        self.available = False
                        with self._condition:
                            self._pending = None
                        return
        finally:
            if player is not None:
                try:
                    player.close()
                except (OSError, RuntimeError):
                    pass
            if registry is not None:
                close = getattr(registry, 'close', None)
                if close is not None:
                    try:
                        close()
                    except (OSError, RuntimeError):
                        pass

    def close(self):
        with self._condition:
            self.closed = self._muted = True
            self._generation += 1
            self._pending = None
            self._condition.notify_all()


def spoken_progress(event, *, resumed=False):
    """Speak measured percent and a bounded phase, never a fabricated ETA."""
    labels = {'running': 'In progress', 'paused': 'Paused', 'succeeded': 'Completed',
              'failed': 'Failed', 'cancelled': 'Cancelled',
              'incomplete': 'Ended without confirmed completion', 'idle': 'Ready'}
    state = 'Resumed' if resumed else labels[event.state]
    progress = f'{event.percent} percent. ' if event.percent is not None else 'Progress is indeterminate. '
    return f'{state}. {progress}{event.phase[:512]}'
