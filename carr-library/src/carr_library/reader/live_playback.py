"""Independent prepared-utterance tempo/pitch using the existing FFmpeg effects.

This module owns no player, engine, permission or resource lease. Its caller
retains those existing owners while processing the private synthesized PCM.
Default playback values return the original PCM without running FFmpeg.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import math
from pathlib import Path

from ..audio import ffmpeg as processing
from ..audio.jobs import check_cancelled, file_identity, regular_file, verified_reader
from ..audio.models import Effect, EFFECT_CONTROLS, SAMPLE_RATES
from ..audio.runtime import resolve_ffmpeg


TEMPO_CONTROL = EFFECT_CONTROLS['tempo']['factor']
PITCH_CONTROL = EFFECT_CONTROLS['pitch']['semitones']


@dataclass(frozen=True)
class PlaybackSettings:
    tempo: float = TEMPO_CONTROL[0]
    pitch: float = PITCH_CONTROL[0]

    def __post_init__(self):
        # The shared effect contract supplies the only admitted bounds.
        Effect('tempo', (('factor', self.tempo),))
        Effect('pitch', (('semitones', self.pitch),))

    @property
    def effects(self):
        values = []
        if self.pitch != PITCH_CONTROL[0]:
            values.append(Effect('pitch', (('semitones', self.pitch),)))
        if self.tempo != TEMPO_CONTROL[0]:
            values.append(Effect('tempo', (('factor', self.tempo),)))
        return tuple(values)


@dataclass(frozen=True)
class PlaybackRuntime:
    """A selected local executable and its independently captured byte identity."""
    path: Path
    identity: tuple
    sha256: str
    version: str

    @contextmanager
    def opened(self, *, cancelled=lambda: False):
        # Keep the existing immutable reader open across process startup and
        # completion. Windows cannot replace/write the executable in this span.
        with verified_reader(self.path, expected_identity=self.identity) as stream:
            actual = sha256()
            while True:
                check_cancelled(cancelled)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                actual.update(block)
            if actual.hexdigest() != self.sha256:
                raise ValueError('The selected FFmpeg runtime changed. Refresh and start a new reading request.')
            check_cancelled(cancelled)
            yield self.path

    def process(self, source, output, settings, *, expected_identity, cancelled=lambda: False):
        if type(settings) is not PlaybackSettings or not settings.effects:
            raise ValueError('Choose explicit nondefault playback settings before processing.')
        info = processing.wav_info(source)
        rate = info.rate if info.rate in SAMPLE_RATES else 48000
        # Retain both actual inputs until the shared supervised processor has
        # completed or settled cancellation. Only then may the caller clean up.
        with self.opened(cancelled=cancelled) as binary:
            with verified_reader(source, expected_identity=expected_identity):
                return processing.process_voice(source, output, ffmpeg=binary,
                    sample_rate=rate, effects=settings.effects,
                    expected_identity=expected_identity, cancelled=cancelled)


def discover_playback_runtime(explicit=None, *, cancelled=lambda: False):
    """Use the existing installed/configured resolver; never download a runtime."""
    check_cancelled(cancelled)
    path = resolve_ffmpeg(explicit)
    if path is None:
        return None
    identity = file_identity(regular_file(path))
    with verified_reader(path, expected_identity=identity):
        proof = processing.ffmpeg_identity(path, cancelled=cancelled)
    return PlaybackRuntime(Path(path), identity, proof['binary_sha256'], proof['version'])


def playback_request(request, settings):
    """Extend the existing declared disk reservation for slower prepared PCM.

    Duration is not inferred as measured speech. The production request's
    existing conservative estimate is expanded by the selected tempo factor.
    CPU and memory ownership remain the same shared resource reservation.
    """
    if type(settings) is not PlaybackSettings:
        raise ValueError('Validated playback settings are required.')
    if not settings.effects:
        return request
    return replace(request, disk_bytes=math.ceil(request.disk_bytes * max(1, 1 / settings.tempo)))
