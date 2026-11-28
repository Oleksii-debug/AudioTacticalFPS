"""Local, disk-backed audio work through a typed FFmpeg interface.

Inputs are plain local audio files. Filtergraphs are generated from numeric
parameters, never accepted as script text. Outputs are staged, verified and
published without replacing an existing file. Long programmes use the same
streaming path as short previews; duration is governed by disk/job budgets.
"""
from dataclasses import dataclass, field
from fractions import Fraction
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from .cleanup import retained_temporary_directory
from .effects import Effect, compile_effects, effected_duration, number


class RenderCancelled(RuntimeError):
    """The caller cancelled a worker before publication."""


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    sample_rate: int
    channels: int
    frames: int
    duration_seconds: float
    codec: str
    container: str


@dataclass(frozen=True)
class RenderResult:
    path: Path
    sha256: str
    frames: int
    sample_rate: int
    channels: int
    duration_seconds: float
    peak_dbfs: float | None
    metadata: dict = field(default_factory=dict)

    def as_dict(self):
        return dict(path=str(self.path), sha256=self.sha256, frames=self.frames,
                    sample_rate=self.sample_rate, channels=self.channels,
                    duration_seconds=self.duration_seconds, peak_dbfs=self.peak_dbfs,
                    metadata=self.metadata)


@dataclass(frozen=True)
class Track:
    path: Path
    start_seconds: float = 0
    gain_db: float = 0
    pan: float = 0
    kind: str = 'voice'
    fade_in: float = 0
    fade_out: float = 0
    effects: tuple = ()
    preserve_stereo: bool = False


@dataclass(frozen=True)
class Chapter:
    title: str
    start_seconds: float
    end_seconds: float


_DEMUXERS = {
    '.wav': 'wav', '.wave': 'wav', '.flac': 'flac', '.mp3': 'mp3',
    '.ogg': 'ogg', '.opus': 'ogg', '.oga': 'ogg', '.aac': 'aac',
    '.m4a': 'mov', '.m4b': 'mov', '.mp4': 'mov',
    '.aif': 'aiff', '.aiff': 'aiff', '.wma': 'asf',
}
_ENCODERS = {
    '.wav': ('pcm_s16le', 'wav'), '.flac': ('flac', 'flac'),
    '.mp3': ('libmp3lame', 'mp3'), '.ogg': ('libvorbis', 'ogg'),
    '.opus': ('libopus', 'ogg'), '.m4a': ('aac', 'ipod'),
    '.m4b': ('aac', 'ipod'), '.aac': ('aac', 'adts'),
    '.aif': ('pcm_s16be', 'aiff'), '.aiff': ('pcm_s16be', 'aiff'),
    '.wma': ('wmav2', 'asf'),
}
_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def check_cancelled(cancelled=None):
    if cancelled is None:
        return
    active = cancelled.is_set() if hasattr(cancelled, 'is_set') else cancelled()
    if active:
        raise RenderCancelled('Audio operation cancelled; completed outputs were preserved.')


def _require_local_volume(path):
    """Reject Windows drive mappings before opening or creating any audio file."""
    if os.name == 'nt':
        import ctypes
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        get_drive_type.argtypes = (ctypes.c_wchar_p,)
        get_drive_type.restype = ctypes.c_uint
        if get_drive_type(path.anchor) == 4:  # DRIVE_REMOTE, including mapped UNC shares
            raise ValueError('Mapped network drives are unavailable for local audio files.')


def local_file(value):
    """Validate an existing plain local file, including its parent path."""
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError('Expected a local audio file path.')
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or '\x00' in raw or '://' in raw or raw.startswith(('\\\\', '//')):
        raise ValueError('Only explicit local file paths are allowed.')
    path = Path(raw).absolute()
    _require_local_volume(path)
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Audio paths must not contain symbolic links or reparse points.')
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise ValueError('Audio input must be a regular file.')
    if _RESERVED.match(path.name) or (os.name == 'nt' and ':' in path.name):
        raise ValueError('Unsupported local file name.')
    return path.resolve(strict=True)


def output_path(value):
    path = Path(value).absolute()
    raw = str(path)
    if '\x00' in raw or '://' in raw or raw.startswith(('\\\\', '//')) or _RESERVED.match(path.name):
        raise ValueError('Choose an ordinary local output file.')
    _require_local_volume(path)
    if os.name == 'nt' and ':' in path.name:
        raise ValueError('Alternate data streams are not audio output files.')
    for parent in path.parents:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Output folder must not contain symbolic links or reparse points.')
    if not path.parent.is_dir():
        raise ValueError('Create or choose an existing output folder first.')
    if os.path.lexists(path):
        raise FileExistsError(f'Output already exists: {path.name}')
    return path.parent.resolve(strict=True) / path.name


def tool_path(name, value=None):
    from .runtime import resolve_active_audio_tool
    bundled = resolve_active_audio_tool(name)
    if bundled is not None and value is not None and Path(value).absolute() != Path(bundled):
        raise ValueError('An activated reviewed audio runtime cannot be overridden.')
    candidate = bundled or (os.fspath(value) if value is not None else shutil.which(name))
    if not candidate:
        raise RuntimeError(f'{name} is not installed. Configure a reviewed local runtime.')
    path = Path(os.path.abspath(candidate))
    # A system-managed executable symlink is legitimate; user content is never
    # passed here by a script plan. Resolve only the configured runtime binary.
    if not path.is_file():
        raise RuntimeError(f'The configured {name} executable is unavailable.')
    # A virtual environment may use a symlink to its base interpreter. Python
    # needs that lexical executable path to locate pyvenv.cfg and installed
    # local model packages; resolving it would silently select another runtime.
    return str(path if name.casefold() == 'python' else path.resolve(strict=True))


def file_sha256(path, cancelled=None):
    digest = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            check_cancelled(cancelled)
            digest.update(block)
    return digest.hexdigest()


def run_process(args, *, cancelled=None, timeout_seconds=None, cwd=None, env=None,
                maximum_log_bytes=2 * 1024 * 1024, watched_paths=(), max_output_bytes=None,
                reserve_disk_bytes=0, maximum_staging_files=10000):
    """Run a trusted argv without a shell, with bounded logs and cancellation.

    This low-level helper is for reviewed adapters only; it is not an agent tool.
    stdout/stderr go to temporary files so a native worker cannot fill a pipe or
    allocate unbounded Python memory. No process output is a success assertion.
    """
    check_cancelled(cancelled)
    if timeout_seconds is not None:
        number(timeout_seconds, 'worker timeout', .01, 365 * 86400)
    if type(maximum_log_bytes) is not int or not 1 <= maximum_log_bytes <= 128*1024**2:
        raise ValueError('Worker diagnostic allowance must be positive bounded bytes.')
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes <= 0):
        raise ValueError('Worker output allowance must be positive bytes.')
    if type(reserve_disk_bytes) is not int or reserve_disk_bytes < 0:
        raise ValueError('Worker free-disk reserve must be nonnegative bytes.')
    if type(maximum_staging_files) is not int or not 1 <= maximum_staging_files <= 1_000_000:
        raise ValueError('Worker staging file allowance is invalid.')
    watched = tuple(Path(path) for path in watched_paths)
    if any(not path.is_absolute() for path in watched):
        raise ValueError('Watched worker outputs must be absolute local paths.')
    def check_storage():
        total, count = 0, 0
        pending = list(watched)
        while pending:
            path = pending.pop()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError('Audio worker output contains a link or reparse point.')
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        count += 1
                        if count > maximum_staging_files:
                            raise RuntimeError('Audio worker exceeded its staging file budget.')
                        pending.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if max_output_bytes is not None and total > max_output_bytes:
                    raise RuntimeError('Audio worker exceeded the selected output disk budget.')
            else:
                raise RuntimeError('Audio worker output must be an ordinary local file.')
        if watched and reserve_disk_bytes and shutil.disk_usage(watched[0].parent).free < reserve_disk_bytes:
            raise OSError('Audio worker stopped to preserve the selected free-disk reserve.')
    if not isinstance(args, (list, tuple)) or not args or any(not isinstance(x, str) or '\x00' in x for x in args):
        raise ValueError('Worker arguments must be an explicit string list.')
    if os.name == 'nt' and len(subprocess.list2cmdline(args).encode('utf-16-le')) // 2 + 1 > 32767:
        raise ValueError('Audio input paths exceed the Windows process command limit. '
                         'Choose shorter local folders or filenames, or fewer simultaneous tracks.')
    started = time.monotonic()
    check_storage()
    from .process import owned_process
    from .cleanup import retained_context
    with retained_context(tempfile.TemporaryFile()) as stdout, retained_context(tempfile.TemporaryFile()) as stderr:
        options = dict(stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                       cwd=cwd, env=env, shell=False)
        with owned_process(args, **options) as process:
            while process.poll() is None:
                check_cancelled(cancelled)
                check_storage()
                if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                    raise RuntimeError('Audio worker exceeded the selected time budget.')
                if any(os.fstat(stream.fileno()).st_size > maximum_log_bytes for stream in (stdout, stderr)):
                    raise RuntimeError('Audio worker exceeded its diagnostic output budget.')
                time.sleep(.04)
            check_cancelled(cancelled)
            check_storage()
            if any(os.fstat(stream.fileno()).st_size > maximum_log_bytes for stream in (stdout, stderr)):
                raise RuntimeError('Audio worker exceeded its diagnostic output budget.')
            stdout.seek(0)
            stderr.seek(0)
            out = stdout.read(maximum_log_bytes + 1).decode('utf-8', errors='replace')
            err = stderr.read(maximum_log_bytes + 1).decode('utf-8', errors='replace')
            if process.returncode != 0:
                detail = ''.join(c for c in err[-2400:] if c in '\n\t' or ord(c) >= 32).strip()
                raise RuntimeError(f'Local audio worker failed (exit {process.returncode}): {detail}')
            return out, err


def _input_options(path):
    demuxer = _DEMUXERS.get(path.suffix.lower())
    if demuxer is None:
        raise ValueError('Unsupported audio input format. Convert a local WAV, FLAC, MP3, Ogg, AAC, AIFF or WMA file.')
    return ['-protocol_whitelist', 'file', '-f', demuxer, '-i', str(path)]


def probe_audio(path, *, ffprobe=None, cancelled=None):
    path = local_file(path)
    out, _ = run_process([tool_path('ffprobe', ffprobe), '-v', 'error',
        *_input_options(path), '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name,sample_rate,channels,duration_ts,time_base,duration:format=duration,format_name',
        '-of', 'json'], cancelled=cancelled, timeout_seconds=60)
    try:
        data = json.loads(out)
        stream, = data['streams']
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        if not 8000 <= rate <= 384000 or not 1 <= channels <= 16:
            raise ValueError('Unsupported input audio geometry.')
        if stream.get('duration_ts') is not None and stream.get('time_base'):
            samples = Fraction(int(stream['duration_ts'])) * Fraction(stream['time_base']) * rate
            frames = round(samples)
        else:
            duration = float(stream.get('duration', data['format']['duration']))
            frames = round(duration * rate)
        if not 0 < frames <= 2**53:
            raise ValueError('Audio duration is missing or invalid.')
        return AudioInfo(path, rate, channels, frames, frames / rate,
                         str(stream['codec_name']), str(data['format']['format_name']))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError('The local file has no valid, bounded audio duration.') from exc


def _settings(sample_rate, channels):
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
        raise ValueError('Sample rate must be an integer from 8000 to 192000 Hz.')
    if type(channels) is not int or channels not in (1, 2):
        raise ValueError('Choose mono or stereo output.')
    return sample_rate, channels


def _frames(duration, sample_rate):
    number(duration, 'duration in seconds', 1 / sample_rate, (2**53 - 1) / sample_rate)
    return round(duration * sample_rate)


def estimate_pcm_bytes(duration_seconds, *, sample_rate=24000, channels=2, bit_depth=16):
    _settings(sample_rate, channels)
    if bit_depth not in (16, 24, 32):
        raise ValueError('Unsupported PCM bit depth.')
    return _frames(duration_seconds, sample_rate) * channels * (bit_depth // 8) + 4096


def _disk_budget(target, frames, rate, channels, max_output_bytes):
    estimate = estimate_pcm_bytes(frames / rate, sample_rate=rate, channels=channels)
    if max_output_bytes is not None:
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError('Output disk budget must be positive bytes.')
        if estimate > max_output_bytes:
            raise ValueError('The uncompressed output estimate exceeds the chosen job disk budget.')
    if estimate + 1024 * 1024 > shutil.disk_usage(target.parent).free:
        raise OSError('Insufficient free disk space for a safe uncompressed render estimate.')
    return estimate


def _encoder_options(target, *, sample_rate, channels, bitrate_kbps=128):
    suffix = target.suffix.lower()
    if suffix not in _ENCODERS:
        raise ValueError('Unsupported output extension. Choose WAV, FLAC, MP3, Ogg, Opus, AAC/M4A/M4B, AIFF or WMA.')
    codec, container = _ENCODERS[suffix]
    number(bitrate_kbps, 'bitrate kbps', 32, 512)
    options = ['-vn', '-sn', '-dn', '-ar', str(sample_rate), '-ac', str(channels),
               '-c:a', codec, '-f', container]
    if suffix == '.wav':
        options += ['-rf64', 'auto']
    elif suffix == '.flac':
        options += ['-sample_fmt', 's16', '-compression_level', '5']
    elif suffix in ('.aif', '.aiff'):
        pass
    else:
        options += ['-b:a', f'{int(bitrate_kbps)}k']
    if suffix in ('.m4a', '.m4b'):
        options += ['-movflags', '+faststart']
    return options


def _ffmpeg_base(binary=None, *, informational=False):
    return [tool_path('ffmpeg', binary), '-hide_banner', '-loglevel',
            'info' if informational else 'error', '-nostdin', '-nostats',
            '-max_alloc', str(256*1024**2),
            '-threads', '2', '-filter_threads', '1', '-filter_complex_threads', '1']


def _complex_filter_file_options(binary, path, *, cancelled=None):
    # FFmpeg 9 removed the legacy script option. Discover the actual reviewed
    # runtime's option instead of assuming its version from the host platform.
    out, err = run_process([binary, '-hide_banner', '-h', 'long'],
        cancelled=cancelled, timeout_seconds=10, maximum_log_bytes=512*1024)
    legacy = re.search(r'(?m)^\s*-filter_complex_script(?:\s|$)', out + '\n' + err)
    return ['-filter_complex_script' if legacy else '-/filter_complex', str(path)]


def runtime_identity(*, ffmpeg=None, ffprobe=None):
    """JSON-safe cache identity of actual local tools and reviewed adapter code."""
    result = {'interface_version': 1}
    for name, configured in (('ffmpeg', ffmpeg), ('ffprobe', ffprobe)):
        path = tool_path(name, configured)
        text, _ = run_process([path, '-version'], timeout_seconds=20)
        result[name] = {'executable_sha256': file_sha256(path),
                        'version_and_build': text.strip()[:16000]}
    result['render_sha256'] = file_sha256(Path(__file__))
    result['effects_sha256'] = file_sha256(Path(__file__).with_name('effects.py'))
    return result


def analyze_audio(path, *, ffmpeg=None, cancelled=None, measure_loudness=False, timeout_seconds=None):
    """Measure actual decoded output; silence/undefined LUFS are represented by None."""
    path = local_file(path)
    filters = 'volumedetect'
    if measure_loudness:
        filters = 'loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json,volumedetect'
    _, err = run_process([*_ffmpeg_base(ffmpeg, informational=True), *_input_options(path),
                         '-map', '0:a:0', '-af', filters, '-f', 'null', '-'],
                        cancelled=cancelled, timeout_seconds=timeout_seconds)
    def read_number(pattern):
        matches = re.findall(pattern, err)
        if not matches:
            return None
        value = float(matches[-1])
        return value if math.isfinite(value) else None
    result = dict(peak_dbfs=read_number(r'max_volume:\s*([-+\d.einf]+)\s*dB'),
                  rms_dbfs=read_number(r'mean_volume:\s*([-+\d.einf]+)\s*dB'),
                  integrated_lufs=None, true_peak_dbfs=None, loudness_range_lu=None)
    if measure_loudness:
        for raw in re.findall(r'\{[^{}]*"input_i"[^{}]*\}', err, re.S):
            try:
                data = json.loads(raw)
                for name, key in (('integrated_lufs', 'input_i'), ('true_peak_dbfs', 'input_tp'),
                                  ('loudness_range_lu', 'input_lra')):
                    value = float(data[key])
                    result[name] = value if math.isfinite(value) else None
            except (ValueError, KeyError, TypeError):
                raise RuntimeError('FFmpeg returned an invalid loudness measurement.')
        # volumedetect follows loudnorm above; measure ORIGINAL peak/RMS, not
        # the temporary normalized stream discarded into the null sink.
        original = analyze_audio(path, ffmpeg=ffmpeg, cancelled=cancelled,
                                 measure_loudness=False, timeout_seconds=timeout_seconds)
        result.update(peak_dbfs=original['peak_dbfs'], rms_dbfs=original['rms_dbfs'])
    return result


def _sources_unchanged(sources, cancelled):
    for path, digest in sources.items():
        if file_sha256(local_file(path), cancelled) != digest:
            raise RuntimeError('An audio input changed during rendering; no output was published.')


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_audio(staged, target, *, cancelled=None, ffprobe=None, ffmpeg=None,
                  expected_frames=None, expected_rate=None, expected_channels=None,
                  metadata=None, measure=True):
    """Validate a completed local derivative and publish without replacement."""
    staged = local_file(staged)
    target = output_path(target)
    check_cancelled(cancelled)
    info = probe_audio(staged, ffprobe=ffprobe, cancelled=cancelled)
    if expected_rate is not None and info.sample_rate != expected_rate:
        raise RuntimeError('Rendered sample rate differs from the production plan.')
    if expected_channels is not None and info.channels != expected_channels:
        raise RuntimeError('Rendered channel count differs from the production plan.')
    if expected_frames is not None and abs(info.frames - expected_frames) > 1:
        raise RuntimeError('Rendered frame count differs from the production plan.')
    measured = analyze_audio(staged, ffmpeg=ffmpeg, cancelled=cancelled) if measure else {}
    details = dict(metadata or {})
    details.update(measured)
    digest = file_sha256(staged, cancelled)
    # Windows CRT _commit/FlushFileBuffers requires a writable handle. This
    # is our generated staging file; r+b preserves all measured audio bytes.
    with staged.open('r+b') as stream:
        os.fsync(stream.fileno())
    check_cancelled(cancelled)
    os.link(staged, target)  # same output folder/volume; fails if another file won
    _sync_directory(target.parent)
    return RenderResult(target, digest, info.frames, info.sample_rate, info.channels,
                        info.duration_seconds, measured.get('peak_dbfs'), details)


def _chapter_metadata(chapters, directory, rate, total_frames):
    if not isinstance(chapters, (tuple, list)) or len(chapters) > 100000:
        raise ValueError('Invalid chapter list.')
    if not chapters:
        return None
    lines, previous = [';FFMETADATA1'], 0
    for chapter in chapters:
        if (not isinstance(chapter, Chapter) or not isinstance(chapter.title, str)
                or not chapter.title.strip() or len(chapter.title) > 512
                or any(ord(c) < 32 for c in chapter.title)):
            raise ValueError('Chapter title must be plain text.')
        start = round(number(chapter.start_seconds, 'chapter start', 0, total_frames / rate) * rate)
        end = round(number(chapter.end_seconds, 'chapter end', 0, total_frames / rate) * rate)
        if start < previous or end <= start:
            raise ValueError('Chapters must be ordered and non-overlapping.')
        previous = end
        title = re.sub(r'([\\=;#])', r'\\\1', chapter.title)
        lines += ['[CHAPTER]', f'TIMEBASE=1/{rate}', f'START={start}', f'END={end}', f'title={title}']
    path = directory / 'chapters.ffmetadata'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def _track_values(track, info, rate):
    if not isinstance(track, Track) or track.kind not in ('voice', 'music', 'ambience', 'sfx'):
        raise ValueError('Expected a typed voice, music, ambience or sfx track.')
    if type(track.preserve_stereo) is not bool:
        raise ValueError('Stereo preservation must be true or false.')
    start = number(track.start_seconds, 'track start', 0, (2**53 - 1) / rate)
    gain = number(track.gain_db, 'track gain dB', -96, 24)
    pan = number(track.pan, 'track pan', -1, 1)
    duration = effected_duration(info.duration_seconds, track.effects)
    fade_in = number(track.fade_in, 'fade in seconds', 0, duration)
    fade_out = number(track.fade_out, 'fade out seconds', 0, duration)
    effects = compile_effects(track.effects, sample_rate=rate)
    return start, gain, pan, duration, fade_in, fade_out, effects


def mix_tracks(tracks, output, *, duration_seconds=None, sample_rate=24000, channels=2,
               cancelled=None, ffmpeg=None, ffprobe=None, duck_music=True,
               master_effects=(), chapters=(), bitrate_kbps=128,
               max_output_bytes=None, timeout_seconds=None):
    """Render a scene, silence, or a same-length stem using actual local audio.

    Up to 128 simultaneous tracks form one bounded processing graph. A project's
    total scene count/duration is not restricted; concatenate disk-backed scenes.
    Explicit duration never silently truncates speech or foreground effects.
    Stereo music retains its channels; mono/voice pan uses equal-power placement.
    """
    sample_rate, channels = _settings(sample_rate, channels)
    if not isinstance(tracks, (tuple, list)) or len(tracks) > 128:
        raise ValueError('Render scenes in batches of at most 128 simultaneous tracks.')
    if type(duck_music) is not bool:
        raise ValueError('Ducking must be true or false.')
    target = output_path(output)
    _encoder_options(target, sample_rate=sample_rate, channels=channels, bitrate_kbps=bitrate_kbps)
    sources, prepared, probed = {}, [], {}
    for track in tracks:
        if not isinstance(track, Track):
            raise ValueError('Expected typed audio tracks.')
        path = local_file(track.path)
        if path not in probed:
            probed[path] = probe_audio(path, ffprobe=ffprobe, cancelled=cancelled)
            sources[path] = file_sha256(path, cancelled)
        info = probed[path]
        prepared.append((track, info, _track_values(track, info, sample_rate)))
    natural = max((p[2][0] + p[2][3] for p in prepared), default=0)
    if duration_seconds is None:
        duration_seconds = natural
    frames = _frames(duration_seconds, sample_rate)
    duration_seconds = frames / sample_rate
    for track, info, values in prepared:
        start, _, _, length, *_ = values
        if start >= duration_seconds:
            raise ValueError('A track starts after the end of this scene.')
        if track.kind in ('voice', 'sfx') and round((start + length) * sample_rate) > frames + 1:
            raise ValueError('The selected duration would truncate speech or a foreground effect.')
    estimate = _disk_budget(target, frames, sample_rate, channels, max_output_bytes)
    graph, args = [], _ffmpeg_base(ffmpeg)
    for index, (track, info, values) in enumerate(prepared):
        start, gain, pan, length, fade_in, fade_out, effects = values
        keep_stereo = info.channels >= 2 and (track.kind in ('music', 'ambience') or track.preserve_stereo)
        layout = 'stereo' if keep_stereo else 'mono'
        chain = [f'aresample={sample_rate}', f'aformat=sample_fmts=fltp:channel_layouts={layout}']
        if effects:
            chain.append(effects)
        chain.append(f'volume={gain:.9f}dB')
        if keep_stereo:
            left, right = min(1, math.sqrt(1-pan)), min(1, math.sqrt(1+pan))
            chain.append(f'pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c1')
        else:
            left, right = math.sqrt((1-pan)/2), math.sqrt((1+pan)/2)
            chain.append(f'pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c0')
        if fade_in:
            chain.append(f'afade=t=in:st=0:d={fade_in:.9f}')
        if fade_out:
            # Background beds may be longer than the chosen scene.
            ending = min(length, duration_seconds - start)
            chain.append(f'afade=t=out:st={max(0, ending-fade_out):.9f}:d={fade_out:.9f}')
        chain.append('asetpts=N/SR/TB')
        start_frames = round(start * sample_rate)
        if start_frames:
            # adelay allocates a delay buffer proportional to the timestamp.
            # A streamed silence segment keeps a cue at hour ten bounded in RAM.
            graph.append(f'[{index}:a:0]' + ','.join(chain) + f'[clip{index}]')
            graph.append(f'anullsrc=r={sample_rate}:cl=stereo,atrim=end_sample={start_frames},'
                         f'asetpts=N/SR/TB[gap{index}]')
            prefix = f'[gap{index}][clip{index}]concat=n=2:v=0:a=1,'
        else:
            prefix = f'[{index}:a:0]' + ','.join(chain) + ','
        graph.append(prefix + f'apad=whole_len={frames},atrim=end_sample={frames},asetpts=N/SR/TB[t{index}]')
    groups = {'voice': [], 'music': [], 'other': []}
    for index, (track, _, _) in enumerate(prepared):
        key = 'voice' if track.kind == 'voice' else ('music' if track.kind == 'music' else 'other')
        groups[key].append(f'[t{index}]')
    labels = []
    for key, members in groups.items():
        if members:
            graph.append(''.join(members) + f'amix=inputs={len(members)}:duration=longest:normalize=0[{key}]')
            labels.append(f'[{key}]')
    if groups['voice'] and groups['music'] and duck_music:
        graph.append('[voice]asplit=2[spoken][sidechain]')
        graph.append('[music][sidechain]sidechaincompress=threshold=0.05:ratio=8:attack=10:release=200[ducked]')
        labels = ['[spoken]', '[ducked]'] + (['[other]'] if groups['other'] else [])
    if labels:
        final = ''.join(labels) + f'amix=inputs={len(labels)}:duration=longest:normalize=0'
    else:
        final = f'anullsrc=r={sample_rate}:cl=stereo,atrim=end_sample={frames}'
    master = compile_effects(master_effects, sample_rate=sample_rate)
    if any(e.enabled and e.kind in ('tempo', 'delay', 'room_echo') for e in master_effects):
        raise ValueError('Time-changing effects belong to a track, before timeline alignment.')
    if master:
        final += ',' + master
    final += f',alimiter=limit=0.95:level=0:latency=1,aresample={sample_rate},atrim=end_sample={frames}[out]'
    graph.append(final)
    with retained_temporary_directory(prefix='.carr-audio-', dir=target.parent) as temp:
        stage = Path(temp)
        staged = stage / ('render' + target.suffix.lower())
        for _track, info, _values in prepared:
            # Keep repeated project/cache prefixes out of Windows' bounded
            # command line. The source was authenticated above; cwd is owned.
            try:
                argument = Path(os.path.relpath(info.path, stage))
            except ValueError:  # A different Windows drive has no relative path.
                argument = info.path
            args += _input_options(argument)
        chapter_file = _chapter_metadata(chapters, stage, sample_rate, frames)
        if chapter_file:
            args += ['-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(chapter_file)]
        graph_file = stage / 'filtergraph.txt'
        graph_file.write_text(';'.join(graph), encoding='utf-8')
        args += [*_complex_filter_file_options(args[0], graph_file, cancelled=cancelled),
                 '-map', '[out]', '-map_metadata:g', '-1']
        if chapter_file:
            args += ['-map_chapters', str(len(prepared))]
        args += [*_encoder_options(target, sample_rate=sample_rate, channels=channels,
                                   bitrate_kbps=bitrate_kbps), '-n', str(staged)]
        run_process(args, cancelled=cancelled, timeout_seconds=timeout_seconds, cwd=stage,
                    watched_paths=(staged,),max_output_bytes=max_output_bytes,reserve_disk_bytes=1024**2)
        _sources_unchanged(sources, cancelled)
        exact = frames if target.suffix.lower() in ('.wav', '.flac', '.aif', '.aiff') else None
        return publish_audio(staged, target, cancelled=cancelled, ffmpeg=ffmpeg, ffprobe=ffprobe,
                             expected_frames=exact, expected_rate=sample_rate, expected_channels=channels,
                             metadata=dict(operation='mix', input_sha256=list(sources.values()),
                                           duck_music=duck_music, estimated_pcm_bytes=estimate,
                                           tracks=len(tracks), chapter_count=len(chapters),
                                           original_neural_generation=False))


def convert_audio(source, output, *, sample_rate=24000, channels=2, effects=(),
                  start_seconds=0, duration_seconds=None, bitrate_kbps=128, chapters=(),
                  cancelled=None, ffmpeg=None, ffprobe=None,
                  max_output_bytes=None, timeout_seconds=None):
    """Convert or trim a selected local audio file; inputs are never modified."""
    sample_rate, channels = _settings(sample_rate, channels)
    info = probe_audio(source, ffprobe=ffprobe, cancelled=cancelled)
    target = output_path(output)
    digest = file_sha256(info.path, cancelled)
    start = number(start_seconds, 'trim start', 0, info.duration_seconds)
    duration = info.duration_seconds - start if duration_seconds is None else number(
        duration_seconds, 'trim duration', 1 / info.sample_rate, info.duration_seconds - start)
    duration = effected_duration(duration, effects)
    frames = _frames(duration, sample_rate)
    estimate = _disk_budget(target, frames, sample_rate, channels, max_output_bytes)
    filters = [f'aresample={sample_rate}']
    chain = compile_effects(effects, sample_rate=sample_rate)
    if chain:
        filters.append(chain)
    filters.append('alimiter=limit=0.95:level=0:latency=1')
    with retained_temporary_directory(prefix='.carr-audio-', dir=target.parent) as temp:
        stage = Path(temp)
        staged = stage / ('converted' + target.suffix.lower())
        args = [*_ffmpeg_base(ffmpeg), *_input_options(info.path)]
        chapter_file = _chapter_metadata(chapters, stage, sample_rate, frames)
        if chapter_file:
            args += ['-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(chapter_file)]
        args += ['-map', '0:a:0', '-ss', f'{start:.9f}']
        if duration_seconds is not None or start:
            # Input trimming precedes effects. Output-side -ss/-t would remove
            # a delay tail or truncate a slower tempo unexpectedly.
            raw_duration = info.duration_seconds - start if duration_seconds is None else duration_seconds
            filters.insert(0, f'atrim=start={start:.9f}:duration={raw_duration:.9f},asetpts=PTS-STARTPTS')
            del args[-2:]
        args += ['-af', ','.join(filters), '-map_metadata:g', '-1']
        if chapter_file:
            args += ['-map_chapters', '1']
        args += [*_encoder_options(target, sample_rate=sample_rate, channels=channels,
                                   bitrate_kbps=bitrate_kbps), '-n', str(staged)]
        run_process(args, cancelled=cancelled, timeout_seconds=timeout_seconds,
                    watched_paths=(staged,),max_output_bytes=max_output_bytes,reserve_disk_bytes=1024**2)
        _sources_unchanged({info.path: digest}, cancelled)
        return publish_audio(staged, target, cancelled=cancelled, ffprobe=ffprobe, ffmpeg=ffmpeg,
                             expected_rate=sample_rate, expected_channels=channels,
                             metadata=dict(operation='convert', input_sha256=digest,
                                           estimated_pcm_bytes=estimate, chapter_count=len(chapters)))


def concatenate_audio(paths, output, *, sample_rate=None, channels=None, repeat=1,
                      bitrate_kbps=128, chapters=(), cancelled=None, ffmpeg=None,
                      ffprobe=None, max_output_bytes=None, timeout_seconds=None, measure=True):
    """Stream completed chunks in order, optionally repeating an explicit fixture/bed.

    All chunks must already have matching sample rate/channel/codec geometry.
    Paths are staged under generated safe names; no user text enters ffconcat.
    Repetition is recorded explicitly and is never called new neural generation.
    """
    if not isinstance(paths, (tuple, list)) or not 1 <= len(paths) <= 100000:
        raise ValueError('Choose a non-empty bounded chunk list.')
    if type(repeat) is not int or not 1 <= repeat <= 100000 or len(paths) * repeat > 100000:
        raise ValueError('Split this assembly into at most 100000 chunk references per stage.')
    target = output_path(output)
    cache, sources, items = {}, {}, []
    for value in paths:
        path = local_file(value)
        if path not in cache:
            cache[path] = probe_audio(path, ffprobe=ffprobe, cancelled=cancelled)
            sources[path] = file_sha256(path, cancelled)
        items.append(cache[path])
    first = items[0]
    if any((i.sample_rate, i.channels, i.codec) != (first.sample_rate, first.channels, first.codec) for i in items):
        raise ValueError('Convert chunks to identical audio geometry before concatenation.')
    rate = first.sample_rate if sample_rate is None else sample_rate
    count = first.channels if channels is None else channels
    rate, count = _settings(rate, count)
    frames = round(sum(i.frames for i in items) * repeat * rate / first.sample_rate)
    estimate = _disk_budget(target, frames, rate, count, max_output_bytes)
    with retained_temporary_directory(prefix='.carr-audio-', dir=target.parent) as temp:
        stage = Path(temp)
        names = {}
        for index, path in enumerate(cache):
            check_cancelled(cancelled)
            name = f'part{index:06d}' + path.suffix.lower()
            destination = stage / name
            try:
                os.link(path, destination)
            except OSError:
                # Cross-volume source: copy incrementally, checking cancellation.
                with path.open('rb') as src, destination.open('xb') as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        check_cancelled(cancelled)
                        dst.write(block)
            names[path] = name
        list_file = stage / 'chunks.ffconcat'
        with list_file.open('x', encoding='ascii', newline='\n') as stream:
            stream.write('ffconcat version 1.0\n')
            for _ in range(repeat):
                for item in items:
                    stream.write(f"file '{names[item.path]}'\n")
        staged = stage / ('assembled' + target.suffix.lower())
        args = [*_ffmpeg_base(ffmpeg), '-protocol_whitelist', 'file', '-f', 'concat',
                '-safe', '1', '-i', str(list_file)]
        chapter_file = _chapter_metadata(chapters, stage, rate, frames)
        if chapter_file:
            args += ['-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(chapter_file)]
        args += ['-map', '0:a:0', '-af', f'aresample={rate},asetpts=N/SR/TB', '-map_metadata:g', '-1']
        if chapter_file:
            args += ['-map_chapters', '1']
        args += [*_encoder_options(target, sample_rate=rate, channels=count, bitrate_kbps=bitrate_kbps),
                 '-n', str(staged)]
        run_process(args, cancelled=cancelled, timeout_seconds=timeout_seconds,
                    watched_paths=(staged,),max_output_bytes=max_output_bytes,reserve_disk_bytes=1024**2)
        _sources_unchanged(sources, cancelled)
        exact = frames if target.suffix.lower() in ('.wav', '.flac', '.aif', '.aiff') else None
        return publish_audio(staged, target, cancelled=cancelled, ffprobe=ffprobe, ffmpeg=ffmpeg,
                             expected_frames=exact, expected_rate=rate, expected_channels=count, measure=measure,
                             metadata=dict(operation='concatenate', input_sha256=list(sources.values()),
                                           chunks=len(paths), repeat=repeat, estimated_pcm_bytes=estimate,
                                           chapter_count=len(chapters), original_neural_generation=False))
