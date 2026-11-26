"""Typed control descriptions for the existing local FFmpeg effects backend.

This adapter owns no signal processing. Numeric bounds come from ``EFFECTS``;
filter names come from its compiler, and conversion uses ``convert_audio``.
"""
from pathlib import Path
import re

from .effects import EFFECTS, Effect, compile_effects
from . import render


def parameter_unit(name):
    if name.endswith('_hz'):
        return 'Hz'
    if name.endswith('_ms'):
        return 'ms'
    if name.endswith('_dbfs'):
        return 'dBFS'
    if name.endswith('_db'):
        return 'dB'
    if name.endswith('_lufs'):
        return 'LUFS'
    if name.endswith('_lu'):
        return 'LU'
    if name == 'semitones':
        return 'semitones'
    if name == 'q':
        return 'Q'
    if name == 'ratio':
        return ':1'
    if name in ('factor', 'width'):
        return 'times'
    return 'fraction'


class EffectsBackend:
    """Inventory the selected local tools and delegate each actual render."""

    def __init__(self, ffmpeg=None, ffprobe=None, *, sample_rate=44100):
        self.ffmpeg = render.tool_path('ffmpeg', ffmpeg)
        self.ffprobe = render.tool_path('ffprobe', ffprobe)
        self.sample_rate = sample_rate
        self._filters = self._formats = None

    def _inventory(self, cancelled=None):
        if self._filters is not None:
            render.check_cancelled(cancelled)
            return
        values = {}
        for flag in ('-filters', '-encoders', '-muxers'):
            values[flag], _ = render.run_process(
                [self.ffmpeg, '-hide_banner', flag], cancelled=cancelled,
                timeout_seconds=20)
        render.run_process([self.ffprobe, '-version'], cancelled=cancelled,
                           timeout_seconds=20)
        # These inventories describe the selected executable, not web metadata.
        # FFmpeg 9.1 prints two filter flags; earlier supported releases print
        # a third command-capability flag. Both are native inventory formats.
        filters = set(re.findall(r'^\s*[T.][S.](?:[C.])?\s+([A-Za-z0-9_]+)\s',
                                 values['-filters'], re.M))
        encoders = set(re.findall(r'^\s*A[A-Z.]{5}\s+([A-Za-z0-9_]+)\s',
                                  values['-encoders'], re.M))
        muxers = set(re.findall(r'^\s*[D ]?E[d ]?\s+([A-Za-z0-9_]+)\s',
                                values['-muxers'], re.M))
        required = {'aresample', 'alimiter', 'volumedetect'}
        if not required <= filters:
            raise RuntimeError('The selected local FFmpeg lacks required conversion and verification filters: '
                               + ', '.join(sorted(required - filters)) + '.')
        formats = tuple(extension[1:] for extension, (encoder, container)
                        in render._ENCODERS.items()
                        if encoder in encoders and container in muxers)
        if not formats:
            raise RuntimeError('The selected local FFmpeg has no supported audio output encoder and container.')
        self._filters, self._formats = filters, formats

    def effect_schema(self, cancelled=None):
        self._inventory(cancelled)
        result = {}
        for name, parameters in EFFECTS.items():
            compiled = compile_effects((Effect(name),), sample_rate=48000)
            required = set(re.findall(r'(?:^|,)([a-z0-9_]+)(?:=|$)', compiled))
            result[name] = {
                'available': required <= self._filters,
                'parameters': {
                    key: {'default': values[0], 'minimum': values[1],
                          'maximum': values[2], 'unit': parameter_unit(key)}
                    for key, values in parameters.items()},
            }
        return result

    def capabilities(self, cancelled=None):
        self._inventory(cancelled)
        return {'formats': self._formats}

    def convert(self, source, output, *, format, effects=(), cancel=None):
        if (not isinstance(format, str) or '.' + format not in render._ENCODERS
                or Path(output).suffix.lower() != '.' + format):
            raise ValueError('The output filename extension must match the selected audio format.')
        if format == 'opus' and self.sample_rate != 48000:
            raise ValueError('Choose 48000 Hz for the Opus output format.')
        return render.convert_audio(source, output, sample_rate=self.sample_rate,
            channels=2, effects=effects, cancelled=cancel,
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
