"""Local, bounded FFmpeg audio editing, rendering, and measurement.

The application constructs every filter and option from typed numeric values.
It never accepts filter graphs, playlists, URLs, or shell arguments from a
script, book, model, or saved audio project. Inputs are regular files within
explicitly selected roots; original audio is never modified.
"""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .models import (AudioBudget, AudioCancelled, AudioError, AudioInfo,
                     AudioUnavailable, Effect, LoudnessInfo, Track, finite, integer)
from .process import publish_file, run_process
from ..durable_state import local_path


_DEMUXERS = 'wav,mp3,flac,ogg,mov,aiff,asf,matroska,webm,aac'
_ENCODINGS = {
    '.wav': ('wav', 'pcm_s16le', ()),
    '.flac': ('flac', 'flac', ()),
    '.mp3': ('mp3', 'libmp3lame', ('-b:a', '192k')),
    '.ogg': ('ogg', 'libvorbis', ('-q:a', '5')),
    '.opus': ('opus', 'libopus', ('-b:a', '128k')),
    '.m4a': ('ipod', 'aac', ('-b:a', '192k')),
    '.m4b': ('ipod', 'aac', ('-b:a', '192k')),
    '.aac': ('adts', 'aac', ('-b:a', '192k')),
    '.aif': ('aiff', 'pcm_s16be', ()),
    '.aiff': ('aiff', 'pcm_s16be', ()),
    '.wma': ('asf', 'wmav2', ('-b:a', '192k')),
}

# name: parameter -> (default, lower bound, upper bound, unit)
_CONTROLS = {
    'gain': {'gain_db': (0, -120, 24, 'dB')},
    'equalizer': {'frequency': (1000, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                  'q': (1, .1, 20, 'Q')},
    'highpass': {'frequency': (80, 20, 20000, 'Hz'), 'q': (.707, .1, 20, 'Q')},
    'lowpass': {'frequency': (10000, 20, 20000, 'Hz'), 'q': (.707, .1, 20, 'Q')},
    'lowshelf': {'frequency': (120, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                 'q': (.707, .1, 20, 'Q')},
    'highshelf': {'frequency': (5000, 20, 20000, 'Hz'), 'gain_db': (0, -24, 24, 'dB'),
                  'q': (.707, .1, 20, 'Q')},
    'notch': {'frequency': (50, 20, 20000, 'Hz'), 'q': (10, .1, 100, 'Q')},
    'gate': {'threshold_db': (-40, -96, 0, 'dBFS'), 'ratio': (6, 1, 100, 'ratio'),
             'attack_ms': (5, .01, 9000, 'ms'), 'release_ms': (150, .01, 9000, 'ms'),
             'reduction_db': (-60, -120, 0, 'dB')},
    'compressor': {'threshold_db': (-18, -60, 0, 'dBFS'), 'ratio': (3, 1, 20, 'ratio'),
                   'attack_ms': (10, .01, 2000, 'ms'), 'release_ms': (150, .01, 9000, 'ms'),
                   'knee': (2.8, 1, 8, 'ratio'), 'makeup_db': (0, 0, 24, 'dB'),
                   'mix': (1, 0, 1, 'fraction')},
    'limiter': {'ceiling_db': (-1, -24, 0, 'dBFS'), 'attack_ms': (5, .1, 80, 'ms'),
                'release_ms': (50, 1, 8000, 'ms')},
    'denoise': {'reduction_db': (12, .01, 48, 'dB'), 'floor_db': (-50, -80, -20, 'dBFS')},
    'declick': {},
    'declip': {},
    'deesser': {'intensity': (.3, 0, 1, 'fraction'), 'max_reduction': (.5, 0, 1, 'fraction'),
                'frequency': (.5, 0, 1, 'normalized frequency')},
    'delay': {'time_ms': (250, 1, 2000, 'ms'), 'decay': (.4, 0, .9, 'fraction'),
              'mix': (.25, 0, 1, 'fraction')},
    # This is an explicitly limited four-reflection room effect, not a promise
    # of a convolution engine or arbitrary room-size/RT60 accuracy.
    'reverb': {'room_ms': (40, 5, 200, 'ms'), 'decay': (.4, .01, .85, 'fraction'),
               'mix': (.2, 0, 1, 'fraction')},
    'chorus': {'delay_ms': (40, 1, 100, 'ms'), 'depth_ms': (2, .1, 10, 'ms'),
               'speed_hz': (.4, .1, 5, 'Hz'), 'mix': (.25, 0, 1, 'fraction')},
    'tempo': {'factor': (1, .25, 4, 'ratio')},
    'pitch': {'semitones': (0, -12, 12, 'semitones')},
    'width': {'factor': (1, 0, 2, 'ratio')},
    'loudness': {'target_lufs': (-18, -40, -9, 'LUFS'), 'peak_db': (-1, -9, 0, 'dBTP'),
                 'range_lu': (7, 1, 20, 'LU')},
}
_FILTERS = {'gain': 'volume', 'equalizer': 'equalizer', 'highpass': 'highpass',
    'lowpass': 'lowpass', 'lowshelf': 'bass', 'highshelf': 'treble', 'notch': 'bandreject',
    'gate': 'agate', 'compressor': 'acompressor', 'limiter': 'alimiter', 'denoise': 'afftdn',
    'declick': 'adeclick', 'declip': 'adeclip', 'deesser': 'deesser', 'delay': 'aecho',
    'reverb': 'aecho', 'chorus': 'chorus', 'tempo': 'atempo', 'pitch': 'asetrate',
    'width': 'extrastereo', 'loudness': 'loudnorm'}


def effect_controls():
    return {kind: {name: {'default': spec[0], 'minimum': spec[1], 'maximum': spec[2],
                         'unit': spec[3]} for name, spec in values.items()}
            for kind, values in _CONTROLS.items()}


def narration_preset():
    """Conservative offline speech cleanup; always inspect the measured result."""
    return (Effect('highpass', frequency=70), Effect('compressor'), Effect('limiter'))


def _n(value):
    return format(float(value), '.12g')


def _linear(db):
    return 10 ** (db / 20)


def _tempo(value):
    parts = []
    while value < .5:
        parts.append('atempo=0.5')
        value /= .5
    while value > 2:
        parts.append('atempo=2')
        value /= 2
    parts.append(f'atempo={_n(value)}')
    return ','.join(parts)


def effect_filter(effect, sample_rate=24000):
    """Validate *all* controls, including bypassed effects, before compiling."""
    if not isinstance(effect, Effect) or effect.kind not in _CONTROLS:
        raise ValueError('Unsupported audio effect; arbitrary filters are not accepted.')
    definitions = _CONTROLS[effect.kind]
    unknown = set(effect.parameters) - set(definitions)
    if unknown:
        raise ValueError(f'Unsupported {effect.kind} controls: {", ".join(sorted(unknown))}.')
    values = {name: finite(effect.parameters.get(name, spec[0]), name, spec[1], spec[2])
              for name, spec in definitions.items()}
    if 'frequency' in values and definitions['frequency'][3] == 'Hz':
        if values['frequency'] >= sample_rate / 2:
            raise ValueError('Filter frequency must be below half the selected sample rate.')
    if not effect.enabled:
        return ''
    v, kind = values, effect.kind
    if kind == 'gain':
        return f'volume={_n(v["gain_db"])}dB'
    if kind == 'equalizer':
        return f'equalizer=f={_n(v["frequency"])}:t=q:w={_n(v["q"])}:g={_n(v["gain_db"])}'
    if kind in ('highpass', 'lowpass', 'notch'):
        return f'{_FILTERS[kind]}=f={_n(v["frequency"])}:t=q:w={_n(v["q"])}'
    if kind in ('lowshelf', 'highshelf'):
        return f'{_FILTERS[kind]}=f={_n(v["frequency"])}:t=q:w={_n(v["q"])}:g={_n(v["gain_db"])}'
    if kind == 'gate':
        return (f'agate=threshold={_n(_linear(v["threshold_db"]))}:ratio={_n(v["ratio"])}'
                f':attack={_n(v["attack_ms"])}:release={_n(v["release_ms"])}'
                f':range={_n(_linear(v["reduction_db"]))}')
    if kind == 'compressor':
        return (f'acompressor=threshold={_n(_linear(v["threshold_db"]))}:ratio={_n(v["ratio"])}'
                f':attack={_n(v["attack_ms"])}:release={_n(v["release_ms"])}:knee={_n(v["knee"])}'
                f':makeup={_n(_linear(v["makeup_db"]))}:mix={_n(v["mix"])}')
    if kind == 'limiter':
        return (f'alimiter=limit={_n(_linear(v["ceiling_db"]))}:level=0:latency=1'
                f':attack={_n(v["attack_ms"])}:release={_n(v["release_ms"])}')
    if kind == 'denoise':
        return f'afftdn=nr={_n(v["reduction_db"])}:nf={_n(v["floor_db"])}'
    if kind in ('declick', 'declip'):
        return _FILTERS[kind]
    if kind == 'deesser':
        return f'deesser=i={_n(v["intensity"])}:m={_n(v["max_reduction"])}:f={_n(v["frequency"])}'
    if kind == 'delay':
        # A single reflection, explicitly not a recursive feedback delay.
        return f'aecho=1:{_n(1 / (1 + v["mix"]))}:{_n(v["time_ms"])}:{_n(v["decay"] * v["mix"])}'
    if kind == 'reverb':
        delays = '|'.join(_n(v['room_ms'] * multiplier) for multiplier in (1, 1.73, 2.39, 3.11))
        decays = '|'.join(_n(v['mix'] * v['decay'] ** i) for i in (1, 2, 3, 4))
        return f'aecho=1:{_n(1 / (1 + v["mix"]))}:{delays}:{decays}'
    if kind == 'chorus':
        return (f'chorus=0.8:0.8:{_n(v["delay_ms"])}:{_n(v["mix"])}:'
                f'{_n(v["speed_hz"])}:{_n(v["depth_ms"])}')
    if kind == 'tempo':
        return _tempo(v['factor'])
    if kind == 'pitch':
        factor = 2 ** (v['semitones'] / 12)
        return f'asetrate={_n(sample_rate * factor)},aresample={sample_rate},{_tempo(1 / factor)}'
    if kind == 'width':
        return f'extrastereo=m={_n(v["factor"])}:c=0'
    if kind == 'loudness':
        return f'loudnorm=I={_n(v["target_lufs"])}:TP={_n(v["peak_db"])}:LRA={_n(v["range_lu"])}'
    raise ValueError('Unsupported audio effect.')


def _digest(path, cancelled=lambda: False):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while data := source.read(1024**2):
            if cancelled():
                raise AudioCancelled('Audio checksum cancelled.')
            digest.update(data)
    return digest.hexdigest()


def _identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _local_path(value):
    text = str(value)
    if '\x00' in text or '://' in text or text.startswith(('\\\\', '//')):
        raise ValueError('Audio paths must be local files, not network paths or addresses.')
    return Path(value)


class AudioRuntime:
    """FFmpeg adapter with explicit local read/write boundaries.

    ``root`` is the only output root. ``input_roots`` extends readable local
    sources selected by the user. No downloaded executable or codec is loaded.
    """
    def __init__(self, root, *, ffmpeg=None, ffprobe=None, input_roots=(), budget=None):
        self.root = local_path(_local_path(root))
        if self.root.is_symlink():
            raise ValueError('Audio output root must not be a symbolic link.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = local_path(self.root, exists=True)
        if not self.root.is_dir():
            raise ValueError('Audio output root must be a directory.')
        self.input_roots = (self.root, *(local_path(_local_path(p), exists=True) for p in input_roots))
        if any(not path.is_dir() for path in self.input_roots):
            raise ValueError('Audio input roots must be existing directories.')
        self.budget = budget or AudioBudget()
        if not isinstance(self.budget, AudioBudget):
            raise ValueError('Audio budget must be an AudioBudget.')
        self.ffmpeg = self._executable(ffmpeg, 'ffmpeg')
        self.ffprobe = self._executable(ffprobe, 'ffprobe')
        self._capabilities = None
        self._inspection_cache = {}
        self.music_models = {}
        self.music_model_status = {}
        self.fluidsynth = None
        self.soundfont = None
        self.soundfont_license = None

    @staticmethod
    def _executable(value, name):
        if value is None:
            value = shutil.which(name)
        if value is None:
            raise AudioUnavailable(f'{name} is not installed. Select a trusted local FFmpeg installation.')
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'{name} must be an existing absolute executable path.')
        return str(path.resolve(strict=True))

    def _input(self, value):
        raw = _local_path(value)
        if not raw.is_absolute():
            raw = self.root / raw
        path = local_path(raw, exists=True)
        if not any(path.is_relative_to(root) for root in self.input_roots):
            raise ValueError('Audio input is outside the explicitly selected input roots.')
        info = raw.lstat()
        if (raw.is_symlink() or not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('Audio inputs must be regular local files, not links or devices.')
        return path

    def _output(self, value):
        raw = _local_path(value)
        if not raw.is_absolute():
            raw = self.root / raw
        parent = local_path(raw.parent, exists=True)
        if not parent.is_relative_to(self.root):
            raise ValueError('Audio output is outside the selected output directory.')
        path = parent / raw.name
        if path.exists() or path.is_symlink():
            raise FileExistsError(f'Audio output already exists: {path.name}. Choose a new filename.')
        if path.suffix.lower() not in _ENCODINGS:
            raise ValueError('Unsupported audio output extension.')
        return path

    def _input_args(self, path):
        # The MOV demuxer is allowed for local M4A/M4B extraction. Its external
        # data-reference options must stay disabled even if a future build
        # changes a default; other demuxers reject these private options.
        info = self.inspect(path)
        options = ['-enable_drefs', '0', '-use_absolute_path', '0'] if 'mov' in info.format.split(',') else []
        return ['-protocol_whitelist', 'file', '-format_whitelist', _DEMUXERS, *options, '-i', str(path)]

    def _base_args(self):
        return [self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-n',
                '-max_alloc', str(self.budget.max_alloc_bytes), '-threads', str(self.budget.threads),
                '-filter_threads', str(self.budget.threads),
                '-filter_complex_threads', str(self.budget.threads)]

    def capabilities(self):
        if self._capabilities is None:
            budget = replace(self.budget, timeout=min(20, self.budget.timeout))
            version = run_process([self.ffmpeg, '-version'], budget=budget).stdout.decode('utf-8', 'replace')
            filters = run_process([self.ffmpeg, '-hide_banner', '-filters'], budget=budget).stdout.decode('utf-8', 'replace')
            encoders = run_process([self.ffmpeg, '-hide_banner', '-encoders'], budget=budget).stdout.decode('utf-8', 'replace')
            muxers = run_process([self.ffmpeg, '-hide_banner', '-muxers'], budget=budget).stdout.decode('utf-8', 'replace')
            # FFmpeg 9 removed the third (command-support) flag from filter
            # rows. Match both table layouts, including the input->output
            # signature, so legend text cannot become an advertised filter.
            filter_names = {match.group(1) for match in re.finditer(
                r'^[ \t]*[T.][S.](?:[C.])?[ \t]+([A-Za-z0-9_]+)[ \t]+'
                r'[AVN|]+->[AVN|]+(?:[ \t]|$)', filters, re.M)}
            encoder_names = {match.group(1) for match in re.finditer(r'^\s*[A-Z.]{6}\s+(\S+)\s', encoders, re.M)}
            filter_names.discard('=')
            encoder_names.discard('=')
            muxer_names = {name for match in re.finditer(r'^\s*[. ]?E[. d ]*\s+(\S+)\s', muxers, re.M)
                           for name in match.group(1).split(',') if name != '='}
            self._capabilities = {'version': version.splitlines()[0] if version else 'unknown',
                'build_configuration': next((line for line in version.splitlines() if line.startswith('configuration:')), ''),
                'filters': sorted(filter_names), 'encoders': sorted(encoder_names), 'muxers': sorted(muxer_names),
                'formats': [ext for ext, (muxer, encoder, _) in _ENCODINGS.items()
                            if encoder in encoder_names and muxer in muxer_names],
                'effects': [kind for kind, name in _FILTERS.items() if name in filter_names],
                'local_only': True, 'realtime_preview': False,
                'reverb_description': 'Four early reflections; no convolution IR or calibrated RT60.',
                'delay_description': 'Single echo; recursive feedback is not supported.',
                'memory_control': 'Streaming pipeline, thread and allocation limits; not an OS memory sandbox.'}
        return json.loads(json.dumps(self._capabilities))

    def _chain(self, effects, rate):
        if not isinstance(effects, (tuple, list)) or len(effects) > 32:
            raise ValueError('Use at most 32 typed effects per chain.')
        result = [effect_filter(effect, rate) for effect in effects]
        available = set(self.capabilities()['filters'])
        for effect, compiled in zip(effects, result):
            if compiled and _FILTERS[effect.kind] not in available:
                raise AudioUnavailable(f'The installed FFmpeg build does not support {effect.kind}.')
        return [value for value in result if value]

    def inspect(self, path, *, cancelled=lambda: False):
        path = self._input(path)
        if cancelled():
            raise AudioCancelled('Audio inspection cancelled.')
        identity = _identity(path)
        cached = self._inspection_cache.get(path)
        if cached is not None and cached[0] == identity:
            return cached[1]
        args = [self.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
                '-format_whitelist', _DEMUXERS, '-enable_drefs', '0', '-use_absolute_path', '0',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name,sample_rate,channels,duration,duration_ts,time_base:format=format_name,duration',
                '-of', 'json', str(path)]
        result = run_process(args, budget=replace(self.budget, timeout=min(30, self.budget.timeout)), cancelled=cancelled)
        try:
            data = json.loads(result.stdout)
            stream, = data['streams']
            rate, channels = int(stream['sample_rate']), int(stream['channels'])
            integer(rate, 'Detected audio rate', 1000, 768000)
            integer(channels, 'Detected audio channels', 1, 64)
            duration = finite(float(stream.get('duration', data['format'].get('duration'))), 'Detected audio duration', 0)
            frames = None
            if 'duration_ts' in stream and 'time_base' in stream:
                samples = int(stream['duration_ts']) * Fraction(stream['time_base']) * rate
                if samples.denominator == 1:
                    frames = int(samples)
            if identity != _identity(path):
                raise AudioError('An audio source changed during inspection.')
            info = AudioInfo(path, duration, rate, channels, str(stream['codec_name']),
                             str(data['format']['format_name']), frames, path.stat().st_size)
            if len(self._inspection_cache) >= 2048:
                self._inspection_cache.clear()  # cache bound, not a library/scene limit
            self._inspection_cache[path] = (identity, info)
            return info
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
            raise AudioError('The selected file has no supported, measurable audio stream.') from exc

    def _encoding(self, output, sample_rate, channels, bit_depth=16, bitrate=192):
        integer(sample_rate, 'Output sample rate', 8000, 192000)
        integer(channels, 'Output channels', 1, 2)
        if bit_depth not in (16, 24, 32):
            raise ValueError('PCM bit depth must be 16, 24 or 32.')
        integer(bitrate, 'Bitrate', 32, 512)
        muxer, codec, options = _ENCODINGS[output.suffix.lower()]
        if muxer == 'opus' and sample_rate != 48000:
            raise ValueError('Opus output uses a 48000 Hz decoded sample rate; select 48000 Hz explicitly.')
        if muxer in ('wav', 'aiff'):
            codec = f'pcm_s{bit_depth}{"le" if muxer == "wav" else "be"}'
        if codec not in self.capabilities()['encoders']:
            raise AudioUnavailable(f'The installed FFmpeg build has no {codec} encoder.')
        if muxer not in self.capabilities()['muxers']:
            raise AudioUnavailable(f'The installed FFmpeg build has no {muxer} output container.')
        args = ['-ar', str(sample_rate), '-ac', str(channels), '-c:a', codec]
        if options and options[0] == '-b:a':
            args += ['-b:a', f'{bitrate}k']
        else:
            args += list(options)
        if muxer == 'wav':
            args += ['-rf64', 'auto']
        elif muxer == 'flac':
            args += ['-sample_fmt', 's16' if bit_depth == 16 else 's32']
        args += ['-f', muxer]
        return args

    def _space(self, duration, rate, channels, bit_depth, parent):
        # Estimate PCM even for compressed output, because staging or DSP may
        # require it. Actual size is monitored independently throughout the job.
        if duration > self.budget.max_output_bytes / (rate * channels * (bit_depth // 8)):
            raise AudioError('Estimated audio exceeds the configured output budget; select a larger budget or split chapters.')
        estimate = math.ceil(duration * rate * channels * (bit_depth // 8)) + 65536
        if estimate > self.budget.max_output_bytes:
            raise AudioError('Estimated audio exceeds the configured output budget; select a larger budget or split chapters.')
        if shutil.disk_usage(parent).free < estimate + self.budget.reserve_disk_bytes:
            raise AudioError('Not enough free disk for the estimated output and configured reserve.')
        return estimate

    def _render(self, args, target, *, expected_duration, rate, channels,
                bit_depth=16, bitrate=192, sources=(), cancelled=lambda: False, progress=None):
        self._space(expected_duration, rate, channels, bit_depth, target.parent)
        encoding = self._encoding(target, rate, channels, bit_depth, bitrate)
        snapshots = [(source, _identity(source)) for source in sources]
        with tempfile.TemporaryDirectory(prefix='.audio-', dir=target.parent) as temporary:
            staged = Path(temporary) / ('render' + target.suffix)
            command = [*args, '-map_metadata', '-1', '-map_chapters', '-1', '-vn', '-sn', '-dn',
                       *encoding]
            if progress is not None:
                command += ['-stats_period', '1', '-progress', 'pipe:1']
            command += [str(staged)]
            run_process(command, budget=self.budget, cancelled=cancelled, watch=(staged,), progress=progress)
            info = self.inspect(staged, cancelled=cancelled)
            if info.channels != channels or info.sample_rate != rate:
                # Opus is intrinsically 48 kHz. Do not silently claim a 24 kHz
                # output: require callers to select its actual supported rate.
                raise AudioError('Encoded audio does not match the requested sample rate or channels.')
            tolerance = 2 / rate if target.suffix.lower() in ('.wav', '.aif', '.aiff', '.flac') else .15
            if abs(info.duration - expected_duration) > tolerance:
                raise AudioError(f'Rendered audio duration differs from the planned {expected_duration:.6f} seconds.')
            if info.size > self.budget.max_output_bytes:
                raise AudioError('Rendered audio exceeds its output budget.')
            for source, snapshot in snapshots:
                if _identity(source) != snapshot:
                    raise AudioError('A source audio file changed during rendering; output was not published.')
            checksum = _digest(staged, cancelled)
            # Resolve the boundary again immediately before no-clobber publish.
            if local_path(target.parent, exists=True) != target.parent or not target.parent.is_relative_to(self.root):
                raise AudioError('The output directory changed during rendering.')
            publish_file(staged, target, cancelled=cancelled)
            return replace(info, path=target, sha256=checksum)

    def convert(self, source, output, *, effects=(), sample_rate=24000, channels=2,
                start=0, duration=None, bit_depth=16, bitrate=192,
                cancelled=lambda: False, progress=None):
        source = self._input(source)
        target = self._output(output)
        info = self.inspect(source, cancelled=cancelled)
        start = finite(start, 'Selection start', 0)
        if start >= info.duration:
            raise ValueError('Selection start is outside the audio.')
        selected = info.duration - start if duration is None else finite(duration, 'Selection duration', .000001)
        if selected > info.duration - start + 1 / info.sample_rate:
            raise ValueError('Selection exceeds the source audio.')
        chain = self._chain(effects, sample_rate)
        tempo = math.prod(float(effect.parameters.get('factor', 1)) for effect in effects
                          if effect.kind == 'tempo' and effect.enabled)
        output_duration = round(selected / tempo * sample_rate) / sample_rate
        frames = round(output_duration * sample_rate)
        filters = [f'atrim=start={_n(start)}:duration={_n(selected)}', 'asetpts=PTS-STARTPTS',
                   f'aresample={sample_rate}', f'aformat=sample_fmts=fltp:channel_layouts={"mono" if channels == 1 else "stereo"}',
                   *chain, f'aresample={sample_rate}', f'apad=whole_len={frames}', f'atrim=end_sample={frames}']
        return self._render([*self._base_args(), *self._input_args(source), '-map', '0:a:0',
                             '-af', ','.join(filters)], target, expected_duration=output_duration,
                            rate=sample_rate, channels=channels, bit_depth=bit_depth, bitrate=bitrate,
                            sources=(source,), cancelled=cancelled, progress=progress)

    def mix(self, tracks, output, *, duration=None, sample_rate=24000, channels=2,
            duck_music=True, effects=(), bit_depth=16, bitrate=192,
            cancelled=lambda: False, progress=None):
        integer(sample_rate, 'Output sample rate', 8000, 192000)
        integer(channels, 'Output channels', 1, 2)
        if type(duck_music) is not bool:
            raise ValueError('Music ducking must be a boolean.')
        if not isinstance(tracks, (list, tuple)) or len(tracks) > self.budget.max_inputs:
            raise ValueError('Too many simultaneous tracks; render scenes or stems in bounded groups.')
        if any(not isinstance(track, Track) for track in tracks):
            raise ValueError('Every mixed track must be a typed Track.')
        target = self._output(output)
        master = self._chain(effects, sample_rate)
        if any(effect.kind == 'tempo' and effect.enabled for effect in effects):
            raise ValueError('Master tempo changes timeline alignment. Change tempo with Convert before mixing.')
        args, graph, prepared = self._base_args(), [], []
        for track in tracks:
            path = self._input(track.path)
            info = self.inspect(path, cancelled=cancelled)
            if track.trim_start >= info.duration:
                raise ValueError('Track trim starts outside its source.')
            selected = info.duration - track.trim_start if track.duration is None else track.duration
            if selected > info.duration - track.trim_start + 1 / info.sample_rate:
                raise ValueError('A track selection exceeds its source.')
            chain = self._chain(track.effects, sample_rate)
            tempo = math.prod(float(effect.parameters.get('factor', 1)) for effect in track.effects
                              if effect.kind == 'tempo' and effect.enabled)
            length = round(selected / tempo * sample_rate) / sample_rate
            if track.fade_in > length or track.fade_out > length:
                raise ValueError('A track fade exceeds its rendered duration.')
            prepared.append((track, path, selected, length, chain))
        minimum_duration = max((track.start + length for track, _, _, length, _ in prepared), default=0)
        total = minimum_duration if duration is None else finite(duration, 'Timeline duration', .000001)
        if not total or total + 1 / sample_rate < minimum_duration:
            raise ValueError('Timeline duration would truncate one or more tracks.')
        self._space(total, sample_rate, channels, bit_depth, target.parent)
        frames = round(total * sample_rate)
        total = frames / sample_rate
        labels = {'voice': [], 'music': [], 'other': []}
        for index, (track, path, selected, length, chain) in enumerate(prepared):
            args += self._input_args(path)
            filters = [f'atrim=start={_n(track.trim_start)}:duration={_n(selected)}', 'asetpts=PTS-STARTPTS',
                       f'aresample={sample_rate}',
                       'aformat=sample_fmts=fltp:channel_layouts=' + ('stereo' if track.routing == 'stereo' else 'mono'),
                       *chain, f'aresample={sample_rate}', f'volume={_n(track.gain_db)}dB']
            if track.routing == 'stereo':
                if track.pan != 0:
                    raise ValueError('Stereo-preserving routing uses its existing channels; select pan routing to reposition.')
            else:
                pan = -1 if track.routing == 'left' else 1 if track.routing == 'right' else track.pan
                left, right = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
                filters += [f'pan=stereo|c0={_n(left)}*c0|c1={_n(right)}*c0']
            if track.fade_in:
                filters += [f'afade=t=in:st=0:d={_n(track.fade_in)}']
            if track.fade_out:
                filters += [f'afade=t=out:st={_n(length-track.fade_out)}:d={_n(track.fade_out)}']
            filters += [f'adelay={round(track.start*sample_rate)}S:all=1',
                        f'apad=whole_len={frames}', f'atrim=end_sample={frames}']
            graph.append(f'[{index}:a:0]' + ','.join(filters) + f'[t{index}]')
            group = 'voice' if track.role in ('voice', 'microphone') else 'music' if track.role in ('music', 'ambience') else 'other'
            labels[group].append(f'[t{index}]')
        groups = []
        for group, values in labels.items():
            if values:
                graph.append(''.join(values) + f'amix=inputs={len(values)}:duration=longest:normalize=0[{group}]')
                groups.append(f'[{group}]')
        if duck_music and labels['voice'] and labels['music']:
            if 'sidechaincompress' not in self.capabilities()['filters']:
                raise AudioUnavailable('This FFmpeg build has no sidechain compressor for music ducking.')
            graph += ['[voice]asplit=2[vout][control]',
                      '[music][control]sidechaincompress=threshold=0.035:ratio=8:attack=10:release=250[ducked]']
            groups = ['[vout]', '[ducked]'] + (['[other]'] if labels['other'] else [])
        if groups:
            graph.append(''.join(groups) + f'amix=inputs={len(groups)}:duration=longest:normalize=0[mixed]')
        else:
            graph.append(f'anullsrc=r={sample_rate}:cl=stereo,atrim=end_sample={frames}[mixed]')
        # Never introduce the limiter's implicit auto-makeup gain. Latency
        # compensation plus final sample trim maintains exact scene alignment.
        graph.append('[mixed]' + ','.join([*master, 'alimiter=limit=0.95:level=0:latency=1',
                                           f'aresample={sample_rate}', f'apad=whole_len={frames}',
                                           f'atrim=end_sample={frames}']) + '[out]')
        return self._render([*args, '-filter_complex', ';'.join(graph), '-map', '[out]'], target,
                            expected_duration=total, rate=sample_rate, channels=channels,
                            bit_depth=bit_depth, bitrate=bitrate,
                            sources=tuple(path for _, path, _, _, _ in prepared),
                            cancelled=cancelled, progress=progress)

    def silence(self, output, *, duration, sample_rate=24000, channels=2,
                cancelled=lambda: False, progress=None):
        return self.mix((), output, duration=duration, sample_rate=sample_rate, channels=channels,
                        cancelled=cancelled, progress=progress)

    def concatenate(self, sources, output, *, sample_rate=24000, channels=2,
                    bit_depth=16, bitrate=192, cancelled=lambda: False, progress=None):
        """Stream any number of compatible PCM scenes without a giant graph.

        A private ffconcat manifest is generated from root-validated filenames;
        user playlists and their directives are never read. All scenes must
        share their input PCM codec/rate/channel layout. Convert heterogeneous
        inputs first, so concatenation cannot silently reinterpret samples.
        """
        target = self._output(output)
        integer(sample_rate, 'Output sample rate', 8000, 192000)
        integer(channels, 'Output channels', 1, 2)
        paths, duration, expected = [], Fraction(0), None
        for source in sources:
            if cancelled():
                raise AudioCancelled('Audio concatenation cancelled before rendering.')
            path = self._input(source)
            if any(character in str(path) for character in ('\n', '\r', '\x00')):
                raise ValueError('Scene filenames must not contain line breaks.')
            info = self.inspect(path, cancelled=cancelled)
            if info.format != 'wav' or not info.codec.startswith('pcm_') or info.frames is None:
                raise ValueError('Concatenation requires measurable PCM WAV scenes; convert each source first.')
            identity = info.codec, info.sample_rate, info.channels
            if expected is None:
                expected = identity
            if identity != expected:
                raise ValueError('Scenes must use identical PCM codec, rate, and channel count before joining.')
            paths.append(path)
            duration += Fraction(info.frames, info.sample_rate)
        if not paths:
            raise ValueError('Select at least one scene to concatenate.')
        samples = round(duration * sample_rate)
        total = samples / sample_rate
        self._space(total, sample_rate, channels, bit_depth, target.parent)
        with tempfile.TemporaryDirectory(prefix='.audio-join-', dir=target.parent) as folder:
            manifest = Path(folder) / 'scenes.ffconcat'
            with manifest.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write('ffconcat version 1.0\n')
                for path in paths:
                    # ffconcat uses its own single-quote lexer, not a shell.
                    # Close the quoted string, backslash the apostrophe outside
                    # quotes, then reopen it. Backslashes inside quotes are literal.
                    escaped = str(path).replace('\\', '/') if os.name == 'nt' else str(path)
                    escaped = escaped.replace("'", "'\\''")
                    stream.write(f"file '{escaped}'\n")
            filters = f'aresample={sample_rate},apad=whole_len={samples},atrim=end_sample={samples}'
            args = [*self._base_args(), '-protocol_whitelist', 'file', '-format_whitelist',
                    'concat,' + _DEMUXERS, '-f', 'concat', '-safe', '0', '-auto_convert', '0',
                    '-i', str(manifest), '-map', '0:a:0', '-af', filters]
            return self._render(args, target, expected_duration=total, rate=sample_rate, channels=channels,
                                bit_depth=bit_depth, bitrate=bitrate, sources=tuple(paths),
                                cancelled=cancelled, progress=progress)

    def measure(self, source, *, cancelled=lambda: False):
        """Measure actual integrated loudness and true peak via FFmpeg loudnorm."""
        source = self._input(source)
        if 'loudnorm' not in self.capabilities()['filters']:
            raise AudioUnavailable('The installed FFmpeg build has no loudness meter.')
        args = [*self._base_args(), *self._input_args(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
                '-af', 'loudnorm=I=-18:TP=-1:LRA=7:print_format=json', '-f', 'null', '-']
        # loudnorm prints its JSON at info level, while unrelated routine
        # progress is disabled. It is still subject to the diagnostic cap.
        args[args.index('error')] = 'info'
        args.insert(1, '-nostats')
        result = run_process(args, budget=self.budget, cancelled=cancelled)
        match = re.search(rb'\{\s*"input_i".*?\}', result.stderr, flags=re.S)
        if not match:
            raise AudioError('The local loudness meter returned no measurement.')
        data = json.loads(match.group())
        def number(key):
            value = float(data[key])
            return value if math.isfinite(value) else None
        return LoudnessInfo(number('input_i'), number('input_tp'), number('input_lra'),
                            number('input_thresh'), number('input_i') is None)

    def render_music(self, spec, output, *, sample_rate=24000, channels=2,
                     cancelled=lambda: False, progress=None):
        from .music import render_music
        return render_music(self, spec, output, sample_rate=sample_rate, channels=channels,
                            cancelled=cancelled, progress=progress)

    measure_loudness = measure

    def music_capabilities(self):
        from .music import music_capabilities
        return music_capabilities(self)

    def configure_music_model(self, config, *, cancelled=lambda: False):
        from .music import configure_model
        configure_model(self, config, cancelled=cancelled)

    def probe_music_model(self, engine, *, cancelled=lambda: False):
        from .music import probe_model
        return probe_model(self, engine, cancelled=cancelled)

    def configure_fluidsynth(self, binary, soundfont, *, sha256, license_notice):
        from .music import configure_fluidsynth
        configure_fluidsynth(self, binary, soundfont, sha256=sha256, license_notice=license_notice)

    def export_midi(self, spec, output, *, cancelled=lambda: False):
        from .music import export_midi
        return export_midi(self, spec, output, cancelled=cancelled)

    def render_sfx(self, name, output, *, duration=1, seed=0, sample_rate=24000,
                   channels=2, cancelled=lambda: False, progress=None):
        from .music import render_sfx
        return render_sfx(self, name, output, duration=duration, seed=seed,
                          sample_rate=sample_rate, channels=channels,
                          cancelled=cancelled, progress=progress)

    render_sound = render_sfx
