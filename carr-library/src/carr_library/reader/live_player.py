"""Measured live-controller facade over the canonical WavePlayer.

The canonical player remains the sole native audio implementation. Opening a
chunk here validates it without starting sound; playback begins only after the
source controller's final authorization and UI acknowledgement.
"""
from __future__ import annotations

import math
from pathlib import Path

from ..speech._io import digest, local_file
from ..speech.player import WavePlayer
from ..speech.runtime import _wave_info


class CanonicalPlayer:
    def __init__(self, backend=None):
        self.backend = backend if backend is not None else WavePlayer()
        self.path = None
        self.duration = 0.0
        self._digest = None
        self._started = False
        self._listening = False

    def select_output(self, selected):
        if self.path is not None:
            raise ValueError('Close the current speech chunk before changing its output.')
        self.backend.select_output(selected)

    def available(self):
        return bool(self.backend.available)

    def status(self):
        if self.path is None:
            state, position = 'closed', 0.0
        elif not self._started:
            state, position = 'stopped', 0.0
        else:
            state = self.backend.state
            position = self.backend.position_seconds
            if state == 'stopped' and position >= self.duration - .02:
                state = 'ended'
        return dict(state=state, position_seconds=position, duration_seconds=self.duration,
                    available=self.available(), backend=self.backend.backend,
                    position_is_approximate=self.backend.position_is_approximate,
                    reason='' if self.available() else 'No installed local playback device is available.')

    def open(self, path):
        self.close()
        target = local_file(path)
        rate, _channels, frames, _width = _wave_info(target)
        self.path, self.duration = target, frames / rate
        self._digest = digest(target)
        return self.status()

    def play(self):
        if self.path is None or digest(local_file(self.path)) != self._digest:
            raise ValueError('The checked speech chunk changed before native playback.')
        if self._listening:
            self.backend.play_current()
        else:
            self.backend.play(self.path)
        self._started = True
        return self.status()

    @property
    def listening_duration_seconds(self):
        return (self.backend.listening_duration_seconds if self._listening else self.duration)

    def prepare_listening(self, settings, directory, *, state_root, cancelled, budget):
        """Use this source worker for a derivative; native admission stays silent."""
        from ..speech.listening import ListeningSettings
        if (type(settings) is not ListeningSettings or self.path is None
                or self._started or digest(local_file(self.path)) != self._digest):
            raise ValueError('Prepare listening settings for the current unopened speech chunk.')
        self.backend.open(self.path)
        request = self.backend.listening_request(settings.speed, settings.pitch_semitones)
        prepared = self.backend.prepare_listening(request, directory, state_root=state_root,
                                                   cancelled=cancelled, budget=budget)
        try:
            from ..audio.models import AudioCancelled
            if cancelled():
                raise AudioCancelled('Listening preparation stopped before native admission.')
            if prepared.source_sha256 != self._digest:
                raise ValueError('The speech chunk changed before listening adoption.')
            self.backend.apply_listening(prepared)
            self._listening = True
        except BaseException:
            if prepared._owner is None:
                prepared.close()
            raise
        return self.status()

    def set_levels(self, volume, *, muted=False, balance=0):
        return self.backend.set_levels(volume, muted=muted, balance=balance)

    def pause(self):
        self.backend.pause()
        return self.status()

    def resume(self):
        self.backend.resume()
        return self.status()

    def seek(self, seconds):
        if (not self._started or type(seconds) not in (int, float)
                or not math.isfinite(seconds) or not 0 <= seconds <= self.duration):
            raise ValueError('Choose a position within the active speech chunk.')
        self.backend.seek(seconds)
        return self.status()

    def stop(self):
        self.backend.stop()
        self._started = False
        return self.status()

    def close(self):
        if self.backend.close() is False:
            raise RuntimeError('The native speech player has not confirmed closure.')
        self.path, self.duration, self._digest, self._started = None, 0.0, None, False
        self._listening = False
        return self.status()
