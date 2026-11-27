"""Typed local FFmpeg operations, streamed PCM masters and atomic publication.

No model, codec or binary is downloaded. Callers select the installed FFmpeg
build. Paths are snapshotted before parsing; user text never becomes a filter
graph, command, playlist entry or protocol. This is a batch renderer, not a
real-time player. ``progress`` receives rendered seconds, not a guessed ETA.
"""
from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Iterable, Mapping


class AudioCancelled(RuntimeError):
    """The caller cancelled processing; no completed output was published."""


def _number(value, low, high, name):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name} must be a finite number between {low} and {high}.')
    return float(value)


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise AudioCancelled('Audio processing cancelled. Completed source files are unchanged.')


def _local_path(value):
    """Reject network/device spellings before resolve/stat can touch a share."""
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('Expected a non-empty local filesystem path.')
    if len(raw) >= 2 and raw[0] in ('/', '\\') and raw[1] in ('/', '\\'):
        raise ValueError('Network shares and Windows device paths are unavailable in local-only audio mode.')
    scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*):', raw)
    if scheme and not (len(scheme[1]) == 1 and len(raw) > 2 and raw[2] in ('/', '\\')):
        raise ValueError('URLs and drive-relative paths are unavailable in local-only audio mode.')
    path = Path(raw)
    if sys.platform == 'win32':
        import ctypes
        absolute = path.absolute()
        drive = absolute.drive + '\\'
        function = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        function.argtypes = [ctypes.c_wchar_p]
        function.restype = ctypes.c_uint32
        if function(drive) == 4:  # DRIVE_REMOTE, including mapped SMB drives.
            raise ValueError('Mapped network drives are unavailable in local-only audio mode.')
        # A parent junction can otherwise hide a remote share behind a drive
        # path. Inspect ancestors in order, before accessing the selected file.
        cursor = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                break
            if getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Windows reparse-point paths require a regular local copy for audio processing.')
    return path


def _runtime_directories():
    """Search only absolute local PATH entries, excluding the current folder."""
    cwd = os.path.normcase(os.path.abspath(os.curdir))
    directories = []
    for raw in os.environ.get('PATH', '').split(os.pathsep):
        if not raw:
            continue
        try:
            path = _local_path(raw)
        except (ValueError, OSError):
            continue
        if path.is_absolute() and os.path.normcase(os.path.abspath(path)) != cwd:
            directories.append(path)
    return tuple(directories)


@dataclass(frozen=True)
class Effect:
    """One supported DSP operation; values carry the units in their key names.

    Example: ``Effect('equalizer', {'frequency_hz': 1000, 'gain_db': 3, 'q': 1})``.
    Unknown controls are rejected, rather than silently ignored.
    """
    kind: str
    parameters: tuple[tuple[str, float], ...] | Mapping[str, float] = ()

    def __post_init__(self):
        if not isinstance(self.kind, str) or not isinstance(self.parameters, (Mapping, tuple, list)):
            raise ValueError('Effects require a named kind and a finite parameter mapping or sequence.')
        values = self.parameters.items() if isinstance(self.parameters, Mapping) else self.parameters
        values = tuple(values)
        if len(values) > 16 or any(not isinstance(item, (tuple, list)) or len(item) != 2
                                   or not isinstance(item[0], str) for item in values):
            raise ValueError('Effect parameters must be a bounded sequence of named numeric pairs.')
        if len({item[0] for item in values}) != len(values):
            raise ValueError('Duplicate effect parameter.')
        object.__setattr__(self, 'parameters', tuple(tuple(item) for item in values))
        _effect_filter(self)


@dataclass(frozen=True)
class Track:
    path: Path
    start_seconds: float = 0.0
    gain_db: float = 0.0
    pan: float = 0.0
    role: str = 'speech'
    effects: tuple[Effect, ...] = ()
    # pan downmixes to mono before equal-power panning. preserve retains stereo.
    # left/right route the mono downmix exclusively to the named output channel.
    routing: str = 'pan'

    def __post_init__(self):
        object.__setattr__(self, 'path', _local_path(self.path))
        object.__setattr__(self, 'effects', tuple(self.effects))
        _number(self.start_seconds, 0, 2**40, 'Track start seconds')
        _number(self.gain_db, -120, 24, 'Track gain dB')
        _number(self.pan, -1, 1, 'Track pan')
        if self.role not in ('speech', 'music', 'effect'):
            raise ValueError('Track role must be speech, music or effect.')
        if self.routing not in ('pan', 'preserve', 'left', 'right', 'center'):
            raise ValueError('Track routing must be pan, preserve, left, right or center.')
        if self.routing != 'pan' and self.pan != 0:
            raise ValueError('Pan is only used by pan routing; explicit channel routing does not pan.')
        for effect in self.effects:
            if not isinstance(effect, Effect):
                raise ValueError('Track effects must be typed Effect objects.')


@dataclass(frozen=True)
class AudioReport:
    path: Path
    frames: int
    sample_rate: int
    channels: int
    duration_seconds: float
    peak: float
    sha256: str
    source_sha256: tuple[str, ...] = ()
    format: str = 'wav'
    measured_lufs: float | None = None


@dataclass(frozen=True)
class LoudnessReport:
    integrated_lufs: float | None
    range_lu: float | None
    true_peak_dbfs: float | None
    source_sha256: str


# Numeric controls only. Every filter name and argument name is code-owned.
_EFFECTS = {
    'equalizer': {'frequency_hz': (1000, 20, 20000), 'gain_db': (0, -24, 24), 'q': (1, .1, 20)},
    'highpass': {'frequency_hz': (80, 20, 20000)},
    'lowpass': {'frequency_hz': (16000, 20, 20000)},
    'gate': {'threshold_db': (-45, -96, 0), 'ratio': (4, 1, 20), 'attack_ms': (10, .01, 2000), 'release_ms': (150, .01, 9000)},
    'compressor': {'threshold_db': (-18, -60, 0), 'ratio': (4, 1, 20), 'attack_ms': (10, .01, 2000), 'release_ms': (150, .01, 9000), 'makeup_db': (0, 0, 18)},
    'limiter': {'limit': (.95, .0625, 1), 'attack_ms': (5, .1, 80), 'release_ms': (50, 1, 8000)},
    'echo': {'delay_ms': (100, 1, 90000), 'decay': (.25, 0, .9), 'input_gain': (.8, 0, 1), 'output_gain': (.8, 0, 1)},
    'tempo': {'factor': (1, .5, 2)},
    'denoise': {'reduction_db': (12, .01, 97)},
    'loudness': {'integrated_lufs': (-16, -70, -5), 'true_peak_dbfs': (-1.5, -9, 0),
                 'range_lu': (11, 1, 50)},
    'fade_in': {'seconds': (.1, .001, 3600)},
    'fade_out': {'start_seconds': (0, 0, 2**40), 'seconds': (.1, .001, 3600)},
}


def _effect_filter(effect: Effect) -> str:
    spec = _EFFECTS.get(effect.kind)
    if spec is None:
        raise ValueError(f'Unsupported effect: {effect.kind!r}.')
    params = dict(effect.parameters)
    if set(params) - spec.keys():
        raise ValueError('Unsupported effect parameter: ' + ', '.join(sorted(set(params) - spec.keys())))
    v = {name: _number(params.get(name, default), low, high, name)
         for name, (default, low, high) in spec.items()}
    if effect.kind == 'equalizer':
        return f"equalizer=f={v['frequency_hz']}:t=q:w={v['q']}:g={v['gain_db']}"
    if effect.kind in ('highpass', 'lowpass'):
        return f"{effect.kind}=f={v['frequency_hz']}"
    if effect.kind in ('gate', 'compressor'):
        name = 'agate' if effect.kind == 'gate' else 'acompressor'
        result = (f"{name}=threshold={10 ** (v['threshold_db'] / 20)}:ratio={v['ratio']}:"
                  f"attack={v['attack_ms']}:release={v['release_ms']}")
        if effect.kind == 'compressor':
            result += f":makeup={10 ** (v['makeup_db'] / 20)}"
        return result
    if effect.kind == 'limiter':
        return f"alimiter=limit={v['limit']}:attack={v['attack_ms']}:release={v['release_ms']}:level=0:latency=1"
    if effect.kind == 'echo':
        return f"aecho={v['input_gain']}:{v['output_gain']}:{v['delay_ms']}:{v['decay']}"
    if effect.kind == 'tempo':
        return f"atempo={v['factor']}"
    if effect.kind == 'denoise':
        return f"afftdn=nr={v['reduction_db']}"
    if effect.kind == 'loudness':
        return f"loudnorm=I={v['integrated_lufs']}:TP={v['true_peak_dbfs']}:LRA={v['range_lu']}"
    if effect.kind == 'fade_in':
        return f"afade=t=in:st=0:d={v['seconds']}"
    return f"afade=t=out:st={v['start_seconds']}:d={v['seconds']}"


class FFmpegAudio:
    """An explicitly local installed runtime, with optional approved path roots.

    ``max_tracks`` limits simultaneous inputs, not output duration. Long projects
    should render scenes independently then concatenate; this bounds decoder and
    filter memory independently of the number of chapters. ``max_output_bytes``
    is an optional caller resource budget; absent it, available disk is checked.
    """
    def __init__(self, ffmpeg=None, ffprobe=None, allowed_roots=None, *, max_tracks=128,
                 max_output_bytes=None, timeout_seconds=None):
        if ffmpeg is None or ffprobe is None:
            from .runtime import discover_runtime
            # Verification is deliberately once per construction, without a
            # stale process-wide approval cache for mutable executable files.
            bundled = discover_runtime()
            if bundled is not None:
                ffmpeg = bundled / 'ffmpeg.exe' if ffmpeg is None else ffmpeg
                ffprobe = bundled / 'ffprobe.exe' if ffprobe is None else ffprobe
        self.ffmpeg = self._binary(ffmpeg, 'ffmpeg')
        self.ffprobe = self._binary(ffprobe, 'ffprobe')
        self.allowed_roots = None if allowed_roots is None else tuple(_local_path(p).resolve(strict=True) for p in allowed_roots)
        if self.allowed_roots == ():
            raise ValueError('At least one approved path root is required.')
        if type(max_tracks) is not int or max_tracks < 1:
            raise ValueError('Simultaneous track budget must be a positive integer.')
        self.max_tracks = max_tracks
        if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
            raise ValueError('Output byte budget must be a positive integer.')
        self.max_output_bytes = max_output_bytes
        self.timeout_seconds = None if timeout_seconds is None else _number(timeout_seconds, .01, 2**40, 'Time budget seconds')

    @staticmethod
    def _binary(value, name):
        candidate = value
        if candidate is None:
            for directory in _runtime_directories():
                # An absolute command with its directory prevents which() from
                # adding Windows' implicit CWD search or remote PATH entries.
                # CPython 3.11 does not append PATHEXT when which() receives
                # an explicit directory. Use the actual Windows file name,
                # while retaining the absolute, local-only PATH boundary.
                executable = name + '.exe' if sys.platform == 'win32' else name
                candidate = shutil.which(str(directory / executable))
                if candidate:
                    break
        if candidate is None:
            raise RuntimeError(f'{name} is not installed. Select an existing local FFmpeg runtime; no download was attempted.')
        path = _local_path(candidate).absolute()
        if value is None and path.parent.resolve() == Path.cwd().resolve():
            raise RuntimeError(f'Implicit current-directory {name} executables are not accepted; explicitly select a trusted installed runtime.')
        if not path.is_file():
            raise RuntimeError(f'The selected {name} executable is unavailable.')
        return str(path)

    def engine_fingerprint(self, *, cancelled=None):
        """Identity of the adapter, executables and advertised linked versions.

        A private Windows supplement is a verified static build. For a shared
        system build, the version output also identifies its linked libraries;
        this does not claim to hash every external operating-system dependency.
        Recompute for each render run so runtime upgrades invalidate DSP caches.
        """
        digest = hashlib.sha256()
        for name, path in (('adapter', Path(__file__)), ('ffmpeg', Path(self.ffmpeg)),
                           ('ffprobe', Path(self.ffprobe))):
            _check_cancel(cancelled)
            before = path.stat()
            digest.update(name.encode('ascii') + b'\0')
            with path.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    _check_cancel(cancelled)
                    digest.update(block)
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise RuntimeError('The audio runtime changed while its identity was being verified.')
            if name != 'adapter':
                version = subprocess.run([str(path), '-version'], capture_output=True, timeout=15,
                                         check=True).stdout
                digest.update(b'\0version\0' + version)
        _check_cancel(cancelled)
        return digest.hexdigest()

    def _scope(self, path):
        if self.allowed_roots is not None and not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise ValueError('Audio path is outside the approved directories.')

    def _source(self, source):
        p = _local_path(source).absolute()
        # Reject links instead of resolving a user-supplied link outside scope.
        if p.is_symlink() or not stat.S_ISREG(p.lstat().st_mode):
            raise ValueError('Audio input must be a regular local file, not a symbolic link or device.')
        p = p.resolve(strict=True)
        self._scope(p)
        return p

    def _target(self, target, *, formats=('wav',)):
        p = _local_path(target).absolute()
        if p.suffix.lower().lstrip('.') not in formats:
            raise ValueError('Choose a supported output extension: ' + ', '.join(formats))
        if p.exists() or p.is_symlink():
            raise FileExistsError('The output already exists; choose a new filename.')
        parent = p.parent.resolve(strict=True)
        self._scope(parent)
        if not parent.is_dir():
            raise ValueError('The selected output directory is unavailable.')
        return parent / p.name

    @staticmethod
    def _settings(sample_rate, channels):
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError('Sample rate must be an integer from 8000 to 192000 Hz.')
        if type(channels) is not int or channels not in (1, 2):
            raise ValueError('This renderer supports mono or stereo output.')

    @staticmethod
    def _snapshot(path, target, cancelled):
        """Stream a stable-byte copy; never parse the subsequently mutable path."""
        sha = hashlib.sha256()
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode):
            raise ValueError('Audio input changed to a non-regular file.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        with os.fdopen(descriptor, 'rb') as source, target.open('xb') as out:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('Audio input changed to a non-regular file.')
            while block := source.read(1024 * 1024):
                _check_cancel(cancelled)
                sha.update(block)
                out.write(block)
            after = os.fstat(source.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('Audio input changed while its processing snapshot was being made.')
        return sha.hexdigest()

    def _probe(self, source):
        # Demuxer whitelist excludes playlists, scripts, device and image loaders.
        args = [self.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
                '-format_whitelist', 'wav,flac,mp3,ogg,mov,matroska,webm,aac,aiff,asf',
                '-select_streams', 'a:0', '-show_entries',
                'stream=codec_name,sample_rate,channels,duration,duration_ts,time_base:format=duration',
                '-of', 'json', str(source)]
        proc = subprocess.run(args, capture_output=True, timeout=30, check=False)
        if proc.returncode:
            raise ValueError('FFprobe could not read a supported local audio stream: ' + proc.stderr[-2000:].decode('utf-8', 'replace'))
        data = json.loads(proc.stdout)
        if not data.get('streams'):
            raise ValueError('The input contains no readable audio stream.')
        stream = data['streams'][0]
        duration = stream.get('duration') or data.get('format', {}).get('duration')
        if duration is None or not math.isfinite(float(duration)) or float(duration) <= 0:
            raise ValueError('The audio duration could not be verified.')
        return {'sample_rate': int(stream['sample_rate']), 'channels': int(stream['channels']),
                'duration': float(duration), 'codec': stream.get('codec_name', '')}

    def _budget(self, frames, rate, channels, target, *, extra_bytes=0):
        required = frames * channels * 2 + 4096
        if self.max_output_bytes is not None and required > self.max_output_bytes:
            raise ValueError('Audio output exceeds the configured byte budget. Increase the budget or render chapters separately.')
        if required + extra_bytes > shutil.disk_usage(target.parent).free:
            raise OSError('Insufficient free disk for the audio output and input snapshots.')

    def _run(self, args, *, cancelled=None, progress=None, environment=None):
        _check_cancel(cancelled)
        lines = deque(maxlen=48)
        latest = [None]
        lock = threading.Lock()
        start = time.monotonic()
        # No shell or inherited stdin. Arguments and protocols are code-owned.
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                env=environment)
        def stderr_reader():
            for line in proc.stderr:
                with lock:
                    lines.append(line[-2048:])
        def progress_reader():
            for line in proc.stdout:
                if line.startswith('out_time_us='):
                    try:
                        value = max(0.0, int(line.partition('=')[2]) / 1_000_000)
                    except ValueError:
                        continue
                    with lock:
                        latest[0] = value
        readers = [threading.Thread(target=stderr_reader, daemon=True),
                   threading.Thread(target=progress_reader, daemon=True)]
        for thread in readers:
            thread.start()
        last = None
        try:
            while proc.poll() is None:
                _check_cancel(cancelled)
                if self.timeout_seconds is not None and time.monotonic() - start > self.timeout_seconds:
                    raise TimeoutError('Audio processing exceeded the configured time budget; no output was published.')
                with lock:
                    current = latest[0]
                if progress and current is not None and current != last:
                    progress(current)
                    last = current
                time.sleep(.025)
            for thread in readers:
                thread.join(timeout=2)
            _check_cancel(cancelled)
            if proc.returncode:
                raise RuntimeError('Local audio operation failed: ' + ''.join(lines)[-6000:])
            if progress and latest[0] is not None and latest[0] != last:
                progress(latest[0])
            return ''.join(lines)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            for thread in readers:
                thread.join(timeout=2)
            proc.stdout.close()
            proc.stderr.close()

    def _base(self):
        return [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-nostats',
                '-progress', 'pipe:1', '-filter_complex_threads', '1']

    def capabilities(self):
        """Inspect this installed build; absent codecs/filters are not promised."""
        outputs = {}
        for option in ('-filters', '-encoders'):
            result = subprocess.run([self.ffmpeg, '-hide_banner', option], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=15)
            if result.returncode:
                raise RuntimeError('Could not inspect the selected FFmpeg build.')
            outputs[option] = {line.split()[1] for line in result.stdout.splitlines()
                               if len(line.split()) >= 2 and len(line.split()[0]) in (3, 6)}
        names = {'equalizer': 'equalizer', 'highpass': 'highpass', 'lowpass': 'lowpass',
                 'gate': 'agate', 'compressor': 'acompressor', 'limiter': 'alimiter',
                 'echo': 'aecho', 'tempo': 'atempo', 'denoise': 'afftdn', 'loudness': 'loudnorm',
                 'fade_in': 'afade', 'fade_out': 'afade'}
        encoders = {'wav': 'pcm_s16le', 'flac': 'flac', 'mp3': 'libmp3lame', 'ogg': 'libvorbis',
                    'opus': 'libopus', 'm4a': 'aac', 'm4b': 'aac', 'aac': 'aac', 'aiff': 'pcm_s16be'}
        return {'effects': {name: {'available': backend in outputs['-filters'],
                                  'parameters': dict(_EFFECTS[name])} for name, backend in names.items()},
                'formats': {name: codec in outputs['-encoders'] for name, codec in encoders.items()},
                'ducking': 'sidechaincompress' in outputs['-filters'],
                'playback': False, 'recording': False, 'lufs_metering': 'ebur128' in outputs['-filters'],
                'master_format': 'PCM16 WAV with automatic RF64 for files above 4 GiB'}

    @staticmethod
    def _input(path):
        return ['-protocol_whitelist', 'file', '-format_whitelist',
                'wav,flac,mp3,ogg,mov,matroska,webm,aac,aiff,asf', '-i', str(path)]

    @staticmethod
    def _wav_output(path, sample_rate, channels):
        return ['-map_metadata', '-1', '-ar', str(sample_rate), '-ac', str(channels),
                '-c:a', 'pcm_s16le', '-f', 'wav', '-rf64', 'auto', '-n', str(path)]

    def render_timeline(self, tracks: Iterable[Track], output, *, duration=None,
                        sample_rate=24000, channels=2, cancelled=None, progress=None,
                        ducking=True, master_effects=()) -> AudioReport:
        self._settings(sample_rate, channels)
        tracks = tuple(tracks)
        if len(tracks) > self.max_tracks:
            raise ValueError(f'Supply at most {self.max_tracks} simultaneous tracks; concatenate rendered scenes for longer projects.')
        if not tracks and duration is None:
            raise ValueError('A silence-only scene requires an explicit duration.')
        if any(not isinstance(t, Track) for t in tracks):
            raise ValueError('Timeline requires typed Track objects.')
        master_effects = tuple(master_effects)
        for effect in master_effects:
            if not isinstance(effect, Effect):
                raise ValueError('Master effects must be typed Effect objects.')
        if type(ducking) is not bool:
            raise ValueError('Ducking must be a boolean.')
        if channels == 1 and any(t.routing in ('left', 'right') or t.pan != 0 for t in tracks):
            raise ValueError('Explicit left/right channel placement requires stereo output.')
        target = self._target(output)
        sources = [self._source(t.path) for t in tracks]
        _check_cancel(cancelled)
        with tempfile.TemporaryDirectory(prefix='.audio-', dir=target.parent) as temp:
            root = Path(temp)
            snapshots, hashes, info = [], [], []
            for index, source in enumerate(sources):
                snapshot = root / f'input-{index:06d}.audio'
                hashes.append(self._snapshot(source, snapshot, cancelled))
                snapshots.append(snapshot)
                info.append(self._probe(snapshot))
            durations = []
            for track, metadata in zip(tracks, info):
                seconds = metadata['duration']
                for effect in track.effects:
                    values = dict(effect.parameters)
                    if effect.kind == 'tempo':
                        seconds /= values.get('factor', 1)
                    elif effect.kind == 'echo':
                        seconds += values.get('delay_ms', 100) / 1000
                durations.append(track.start_seconds + seconds)
            natural_duration = max(durations) if durations else duration
            if duration is None:
                duration = natural_duration
                for effect in master_effects:
                    values = dict(effect.parameters)
                    if effect.kind == 'tempo':
                        duration /= values.get('factor', 1)
                    elif effect.kind == 'echo':
                        duration += values.get('delay_ms', 100) / 1000
            duration = _number(duration, 1 / sample_rate, 2**40, 'Output duration seconds')
            frames = round(duration * sample_rate)
            timeline_frames = round(natural_duration * sample_rate)
            if frames >= 2**63 or timeline_frames >= 2**63:
                raise ValueError('Requested timeline exceeds the PCM sample address range.')
            self._budget(frames, sample_rate, channels, target)
            graph, args = [], self._base()
            groups = {'speech': [], 'music': [], 'effect': []}
            layout = 'mono' if channels == 1 else 'stereo'
            for i, (track, snapshot) in enumerate(zip(tracks, snapshots)):
                args += self._input(snapshot)
                chain = [f'aresample={sample_rate}', 'asetpts=PTS-STARTPTS']
                if channels == 1 or track.routing != 'preserve':
                    chain.append('aformat=sample_fmts=fltp:channel_layouts=mono')
                else:
                    chain.append('aformat=sample_fmts=fltp:channel_layouts=stereo')
                chain.extend(_effect_filter(effect) for effect in track.effects)
                # loudnorm can internally raise the rate to 192 kHz. Restore the
                # timeline sample clock before any sample-count trim or prefix.
                chain += [f'aresample={sample_rate}', 'asetpts=PTS-STARTPTS']
                chain.append(f'volume={10 ** (track.gain_db / 20):.12f}')
                if channels == 2 and track.routing != 'preserve':
                    pan = -1 if track.routing == 'left' else 1 if track.routing == 'right' else 0 if track.routing == 'center' else track.pan
                    left, right = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
                    chain.append(f'pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c0')
                # Prefix a pull-driven silence source. Both adelay and
                # aresample(first_pts=0) can allocate a duration-sized delay
                # buffer; concat requests silence incrementally instead.
                offset = round(track.start_seconds * sample_rate)
                if offset:
                    graph.append(f'[{i}:a:0]' + ','.join(chain) + f'[voice{i}]')
                    graph.append(f'anullsrc=r={sample_rate}:cl={layout},'
                                 f'atrim=end_sample={offset},asetpts=PTS-STARTPTS[silence{i}]')
                    prefix = f'[silence{i}][voice{i}]'
                    chain = ['concat=n=2:v=0:a=1']
                else:
                    prefix = f'[{i}:a:0]'
                chain += [f'apad=whole_len={timeline_frames}', f'atrim=end_sample={timeline_frames}',
                          f'aformat=sample_fmts=fltp:channel_layouts={layout}']
                graph.append(prefix + ','.join(chain) + f'[t{i}]')
                groups[track.role].append(f'[t{i}]')
            mixed = []
            for role, labels in groups.items():
                if labels:
                    graph.append(''.join(labels) + f'amix=inputs={len(labels)}:duration=longest:normalize=0[{role}]')
                    mixed.append(f'[{role}]')
            if ducking and groups['speech'] and groups['music']:
                graph.append('[speech]asplit=2[voice][sidechain]')
                graph.append('[music][sidechain]sidechaincompress=threshold=0.05:ratio=8:attack=10:release=100[ducked]')
                mixed = ['[voice]', '[ducked]'] + (['[effect]'] if groups['effect'] else [])
            if not mixed:
                graph.append(f'anullsrc=r={sample_rate}:cl={layout},atrim=end_sample={timeline_frames}[silence]')
                mixed = ['[silence]']
            chain = [f'amix=inputs={len(mixed)}:duration=longest:normalize=0']
            chain.extend(_effect_filter(effect) for effect in master_effects)
            chain += [f'aresample={sample_rate}', 'alimiter=limit=0.95:level=0:latency=1',
                      f'apad=whole_len={frames}', f'atrim=end_sample={frames}']
            graph.append(''.join(mixed) + ','.join(chain) + '[out]')
            graph_path = root / 'filters.txt'
            graph_path.write_text(';'.join(graph), encoding='ascii')
            temporary = root / 'render.wav'
            args += ['-filter_complex_script', str(graph_path), '-map', '[out]']
            args += self._wav_output(temporary, sample_rate, channels)
            self._run(args, cancelled=cancelled, progress=progress)
            return self._publish(temporary, target, sample_rate, channels, frames, hashes, cancelled)

    def inspect(self, path, *, cancelled=None) -> AudioReport:
        """Measure an existing PCM16 WAV/RF64 file, including its actual peak.

        Compressed formats must first be converted to a chosen PCM sample rate;
        container duration alone is not claimed to establish precise samples.
        """
        source = self._source(path)
        rate, channels, frames, offset, size = _pcm_header(source)
        peak = _peak(source, offset, size, cancelled)
        sha = _sha(source, cancelled)
        return AudioReport(source, frames, rate, channels, frames / rate, peak, sha)

    probe = inspect

    def measure_loudness(self, path, *, cancelled=None, progress=None) -> LoudnessReport:
        """Actual EBU R128 integrated loudness and true peak from the local build.

        Silence can have no finite true peak; such values are returned as None.
        This is a measured result, distinct from a normalization target/preset.
        """
        source = self._source(path)
        with tempfile.TemporaryDirectory(prefix='carr-audio-meter-') as temp:
            snapshot = Path(temp) / 'input.audio'
            sha = self._snapshot(source, snapshot, cancelled)
            self._probe(snapshot)
            args = self._base()
            args[args.index('error')] = 'info'
            args += self._input(snapshot) + ['-map', '0:a:0', '-af', 'ebur128=peak=true', '-f', 'null', '-']
            result = self._run(args, cancelled=cancelled, progress=progress)
            def number(label, units):
                matches = re.findall(r'(?m)^\s*' + re.escape(label) +
                                     r':\s*(-?\d+(?:\.\d+)?|-inf)\s+' + re.escape(units), result)
                if not matches:
                    raise ValueError('The selected FFmpeg build did not produce a complete loudness measurement.')
                value = float(matches[-1])
                return value if math.isfinite(value) else None
            return LoudnessReport(number('I', 'LUFS'), number('LRA', 'LU'),
                                  number('Peak', 'dBFS'), sha)

    def concatenate_segments(self, paths, output, *, sample_rate=24000, channels=2,
                             duration=None, cancelled=None, progress=None) -> AudioReport:
        """Stream arbitrarily long chapters through FFmpeg's concat demuxer.

        Every source is normalized separately; only one decoder is live during
        assembly. Repeated paths are explicitly repeated, never invented loops.
        RF64 is selected by FFmpeg automatically above the RIFF/WAV size limit.
        """
        self._settings(sample_rate, channels)
        paths = tuple(paths)
        if not paths:
            raise ValueError('At least one audio segment is required.')
        target = self._target(output)
        sources = [self._source(path) for path in paths]
        _check_cancel(cancelled)
        with tempfile.TemporaryDirectory(prefix='.audio-join-', dir=target.parent) as temp:
            root = Path(temp)
            cache = {}
            hashes, manifest, frames = [], [], 0
            for source in sources:
                _check_cancel(cancelled)
                if source not in cache:
                    index = len(cache)
                    snapshot = root / f'original-{index:08d}.audio'
                    sha = self._snapshot(source, snapshot, cancelled)
                    meta = self._probe(snapshot)
                    normalized = root / f'segment-{index:08d}.wav'
                    # Preserve PCM exactly when it already matches the master.
                    try:
                        wav_info = _pcm_header(snapshot)
                    except ValueError:
                        wav_info = None
                    if wav_info is not None and wav_info[:2] == (sample_rate, channels):
                        snapshot.rename(normalized)
                    else:
                        args = self._base() + self._input(snapshot) + ['-map', '0:a:0']
                        args += self._wav_output(normalized, sample_rate, channels)
                        self._run(args, cancelled=cancelled)
                    header = _pcm_header(normalized)
                    cache[source] = (normalized.name, sha, header[2])
                name, sha, count = cache[source]
                hashes.append(sha)
                manifest.append("file '" + name + "'\n")
                frames += count
            if duration is not None:
                frames = round(_number(duration, 1 / sample_rate, 2**40, 'Output duration seconds') * sample_rate)
            self._budget(frames, sample_rate, channels, target)
            listing = root / 'segments.txt'
            listing.write_text(''.join(manifest), encoding='ascii')
            temporary = root / 'render.wav'
            args = self._base() + ['-protocol_whitelist', 'file', '-f', 'concat', '-safe', '1',
                                   '-i', str(listing), '-map', '0:a:0', '-af',
                                   f'apad=whole_len={frames},atrim=end_sample={frames}']
            args += self._wav_output(temporary, sample_rate, channels)
            self._run(args, cancelled=cancelled, progress=progress)
            return self._publish(temporary, target, sample_rate, channels, frames, hashes, cancelled)

    def convert(self, source, output, *, sample_rate=24000, channels=2, cancelled=None,
                progress=None, bitrate_kbps=128, effects=()) -> AudioReport:
        """Convert one selected local source. Codec availability is build-specific.

        The exact master frame count and peak are measured before compression;
        lossy-format padding is not represented as exact word timing.
        """
        self._settings(sample_rate, channels)
        formats = {'wav': None, 'flac': ('flac', 'flac'), 'mp3': ('libmp3lame', 'mp3'),
                   'ogg': ('libvorbis', 'ogg'), 'opus': ('libopus', 'ogg'),
                   'm4a': ('aac', 'ipod'), 'm4b': ('aac', 'ipod'), 'aac': ('aac', 'adts'),
                   'aiff': ('pcm_s16be', 'aiff')}
        target = self._target(output, formats=tuple(formats))
        selected = target.suffix.lower()[1:]
        if selected == 'wav':
            return self.render_timeline([Track(source, effects=tuple(effects), routing='preserve')], target,
                                        sample_rate=sample_rate, channels=channels,
                                        cancelled=cancelled, progress=progress)
        bitrate = int(_number(bitrate_kbps, 32, 512, 'Bitrate kbit/s'))
        with tempfile.TemporaryDirectory(prefix='.audio-convert-', dir=target.parent) as temp:
            root = Path(temp)
            # Derived private PCM master; no input modification or false lossless claim.
            master = self.render_timeline([Track(source, effects=tuple(effects), routing='preserve')],
                root / 'master.wav', sample_rate=sample_rate, channels=channels,
                cancelled=cancelled)
            codec, container = formats[selected]
            compressed = root / ('render.' + selected)
            args = self._base() + self._input(master.path) + ['-map', '0:a:0', '-map_metadata', '-1',
                    '-c:a', codec, '-f', container]
            if selected in ('mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aac'):
                args += ['-b:a', f'{bitrate}k']
            args += ['-n', str(compressed)]
            self._run(args, cancelled=cancelled, progress=progress)
            metadata = self._probe(compressed)
            # Lossy codecs can change rate, padding and peak. Measure the decoded
            # artifact instead of mislabelling the pre-encode master's counts.
            decoded = root / 'decoded.wav'
            args = self._base() + self._input(compressed) + ['-map', '0:a:0']
            args += self._wav_output(decoded, metadata['sample_rate'], metadata['channels'])
            self._run(args, cancelled=cancelled)
            rate, count, frames, offset, size = _pcm_header(decoded)
            peak = _peak(decoded, offset, size, cancelled)
            _check_cancel(cancelled)
            sha = _sha(compressed, cancelled)
            _publish_file(compressed, target)
            return AudioReport(target, frames, rate, count, frames / rate, peak, sha,
                               master.source_sha256, selected)

    @staticmethod
    def _publish(temporary, target, rate, channels, expected_frames, hashes, cancelled):
        actual_rate, actual_channels, frames, offset, size = _pcm_header(temporary)
        if (actual_rate, actual_channels, frames) != (rate, channels, expected_frames):
            raise ValueError('Rendered PCM format or duration did not match the requested timeline.')
        peak = _peak(temporary, offset, size, cancelled)
        sha = _sha(temporary, cancelled)
        _check_cancel(cancelled)
        _publish_file(temporary, target)
        return AudioReport(target, frames, rate, channels, frames / rate, peak, sha, tuple(hashes))


def _publish_file(source, target):
    with source.open('r+b') as stream:
        os.fsync(stream.fileno())
    # Atomic, no-clobber. Both paths are in the same filesystem. An external
    # writer winning this filename race causes FileExistsError, never overwrite.
    os.link(source, target)
    try:
        fd = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return  # Directory fsync is not supported by all Windows filesystems.
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(path, cancelled):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(1024 * 1024):
            _check_cancel(cancelled)
            digest.update(data)
    return digest.hexdigest()


def _pcm_header(path):
    """Read RIFF or RF64 PCM16 bounds without loading the recording into RAM."""
    size = path.stat().st_size
    with path.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
            raise ValueError('Not a RIFF/RF64 PCM recording.')
        fmt = None
        rf64_data = None
        while stream.tell() + 8 <= size:
            chunk, length = struct.unpack('<4sI', stream.read(8))
            start = stream.tell()
            if chunk == b'ds64':
                if length < 28:
                    raise ValueError('Invalid RF64 size descriptor.')
                _, rf64_data, _, _ = struct.unpack('<QQQI', stream.read(28))
            elif chunk == b'fmt ':
                if length < 16 or length > 4096:
                    raise ValueError('Invalid PCM format descriptor.')
                payload = stream.read(length)
                code, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', payload[:16])
                if code == 0xfffe and (len(payload) < 40 or payload[24:40] != bytes.fromhex('0100000000001000800000aa00389b71')):
                    raise ValueError('Unsupported extensible WAV subtype.')
                if (code not in (1, 0xfffe) or bits != 16 or channels not in (1, 2)
                        or align != channels * 2 or not 0 < rate <= 768000 or byte_rate != rate * align):
                    raise ValueError('Expected mono/stereo PCM16 audio.')
                fmt = rate, channels, align
            elif chunk == b'data':
                if fmt is None:
                    raise ValueError('PCM audio is missing its format header.')
                if length == 0xffffffff:
                    if header[:4] != b'RF64' or rf64_data is None:
                        raise ValueError('PCM data length is not verifiable.')
                    length = rf64_data
                if length <= 0 or start + length > size or length % fmt[2]:
                    raise ValueError('PCM recording is empty, truncated or not sample aligned.')
                return fmt[0], fmt[1], length // fmt[2], start, length
            if start + length > size:
                raise ValueError('Truncated PCM chunk.')
            stream.seek(start + length + (length & 1))
    raise ValueError('The recording has no PCM data.')


def _peak(path, offset, size, cancelled):
    maximum = 0
    with path.open('rb') as stream:
        stream.seek(offset)
        while size:
            _check_cancel(cancelled)
            block = stream.read(min(size, 1024 * 1024))
            if not block:
                raise ValueError('Rendered PCM file was truncated during verification.')
            values = array('h')
            values.frombytes(block)
            if sys.byteorder != 'little':
                values.byteswap()
            maximum = max(maximum, abs(min(values)), abs(max(values)))
            size -= len(block)
    return maximum / 32768
