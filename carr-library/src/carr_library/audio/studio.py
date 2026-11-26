"""Local, checkpointed prepared-track rendering through a trusted FFmpeg.

This module mixes existing PCM speech/music/effects; it does not synthesize
speech or claim neural music generation. Typed plans become fixed filtergraphs,
never shell commands. Inputs stay unchanged, audio outputs are never replaced,
and each accepted scene is durably checkpointed before the next scene begins.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import struct
import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import monotonic
from uuid import uuid4
import wave

from ..data_paths import guard_data_directory
from ..ingest.writer_guard import exclusive_writer


MANIFEST = 'studio-project.json'
PIPELINE_VERSION = 1
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
KINDS = frozenset(('voice', 'music', 'effect'))
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
_DIGEST = re.compile(r'[0-9a-f]{64}')


def _integer(value, label, *, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'Invalid {label}.')
    return value


def _number(value, label, minimum, maximum):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not minimum <= value <= maximum):
        raise ValueError(f'Invalid {label}.')
    return float(value)


def _relative(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 4096
            or '\\' in value or ':' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Audio paths must be ordinary relative local filenames.')
    path = PurePosixPath(value)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    if (path.is_absolute() or str(path) != value or not path.parts
            or any(part in ('.', '..') or part.endswith((' ', '.'))
                   or part.split('.', 1)[0].upper() in reserved for part in path.parts)):
        raise ValueError('Audio paths must stay inside the selected directory.')
    return path


@dataclass(frozen=True)
class Track:
    path: str
    start_ms: int = 0
    pan: float = 0.0
    gain: float = 1.0
    kind: str = 'voice'
    fade_in_ms: int = 0
    fade_out_ms: int = 0

    def __post_init__(self):
        if _relative(self.path).suffix.casefold() != '.wav':
            raise ValueError('Prepared tracks must be PCM WAV files.')
        _integer(self.start_ms, 'track start')
        _integer(self.fade_in_ms, 'fade-in duration')
        _integer(self.fade_out_ms, 'fade-out duration')
        object.__setattr__(self, 'pan', _number(self.pan, 'pan', -1, 1))
        object.__setattr__(self, 'gain', _number(self.gain, 'gain', 0, 1))
        if not isinstance(self.kind, str) or self.kind not in KINDS:
            raise ValueError('Track kind must be voice, music or effect.')


@dataclass(frozen=True)
class Scene:
    id: str
    tracks: tuple[Track, ...]
    duration_ms: int
    duck_music: bool = True

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Scene IDs need 1–64 letters, digits, hyphens or underscores.')
        if (not isinstance(self.tracks, (tuple, list))
                or any(type(track) is not Track for track in self.tracks)):
            raise ValueError('A scene requires a list of validated audio tracks.')
        object.__setattr__(self, 'tracks', tuple(self.tracks))
        _integer(self.duration_ms, 'scene duration', minimum=1)
        if type(self.duck_music) is not bool:
            raise ValueError('Music ducking must be enabled or disabled explicitly.')


@dataclass(frozen=True)
class RenderLimits:
    sample_rate: int = 48000
    max_output_bytes: int = 64 * 1024**3
    max_input_bytes: int = 64 * 1024**3
    max_wall_seconds: float = 86400.0
    max_tracks_per_scene: int = 32
    max_scenes: int = 10000

    def __post_init__(self):
        _integer(self.sample_rate, 'sample rate', minimum=8000, maximum=192000)
        _integer(self.max_output_bytes, 'output byte budget', minimum=1, maximum=2**63 - 1)
        _integer(self.max_input_bytes, 'input byte budget', minimum=1, maximum=2**63 - 1)
        _number(self.max_wall_seconds, 'render time budget', 0.001, 365 * 86400)
        _integer(self.max_tracks_per_scene, 'track budget', minimum=1, maximum=256)
        _integer(self.max_scenes, 'scene budget', minimum=1, maximum=100000)


@dataclass(frozen=True)
class AudioArtifact:
    path: Path
    sha256: str
    frames: int
    sample_rate: int
    channels: int = 2


@dataclass(frozen=True)
class SceneRender:
    scene_id: str
    mix: AudioArtifact
    stems: dict[str, AudioArtifact]
    cache_key: str
    reused: bool


@dataclass(frozen=True)
class RenderResult:
    master: AudioArtifact
    scenes: tuple[SceneRender, ...]
    manifest: Path
    reused_scenes: int


class _Budget:
    def __init__(self, limits, cancelled):
        if not callable(cancelled):
            raise ValueError('A cancellation callback is required.')
        self.limits, self.cancelled = limits, cancelled
        self.deadline = monotonic() + limits.max_wall_seconds

    def check(self):
        if self.cancelled():
            raise InterruptedError('Audio render cancelled; completed scenes remain available for resume.')
        if monotonic() >= self.deadline:
            raise TimeoutError('Audio render time budget reached; completed scenes remain available for resume.')


def _plain(path, *, directory=False, unlinked=True):
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid or getattr(info, 'st_file_attributes', 0) & 0x400
            or (not directory and unlinked and info.st_nlink != 1)):
        raise ValueError('Audio paths must be regular private local files or directories, without links.')
    return info


def _root(value):
    if str(value).startswith(('//', '\\\\')) or PureWindowsPath(str(value)).drive.startswith('\\\\'):
        raise ValueError('Audio rendering requires a local directory, not a network share.')
    path = Path(value).absolute()
    _plain(path, directory=True)
    return path.resolve(strict=True)


def _inside(root, relative):
    parts = _relative(relative).parts
    path = root
    for index, part in enumerate(parts):
        path = path / part
        _plain(path, directory=index < len(parts) - 1)
    return path


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _open_file(path):
    before = _plain(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('An audio file changed while opening it.')
        yield stream, before
        if _identity(os.fstat(stream.fileno())) != _identity(before) or _identity(_plain(path)) != _identity(before):
            raise ValueError('An audio file changed while reading it.')


def _hash(path, budget):
    checksum = sha256()
    with _open_file(path) as (stream, _):
        while block := stream.read(1024 * 1024):
            budget.check()
            checksum.update(block)
    return checksum.hexdigest()


def _input(root, relative, budget):
    path = _inside(root, relative)
    with _open_file(path) as (stream, info):
        if not 0 < info.st_size <= budget.limits.max_input_bytes:
            raise ValueError('Prepared audio exceeds the input byte budget.')
        try:
            with wave.open(stream, 'rb') as audio:
                channels, width, rate, frames, compression, _ = audio.getparams()
                if (channels not in (1, 2) or width != 2 or compression != 'NONE'
                        or not 8000 <= rate <= 192000 or frames < 1):
                    raise ValueError('Prepared tracks require 16-bit mono or stereo PCM WAV at 8–192 kHz.')
                remaining = frames
                while remaining:
                    budget.check()
                    amount = min(65536, remaining)
                    block = audio.readframes(amount)
                    if len(block) != amount * channels * width:
                        raise ValueError('Prepared WAV data is truncated.')
                    remaining -= amount
        except (wave.Error, EOFError) as exc:
            raise ValueError('Prepared audio is not a supported PCM WAV file.') from exc
    return dict(path=relative, sha256=_hash(path, budget), size=info.st_size,
                frames=frames, sample_rate=rate, channels=channels)


def _frames(milliseconds, rate):
    return (milliseconds * rate + 999) // 1000


def _canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate audio-project manifest field.')
        result[key] = value
    return result


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _load_manifest(root):
    path = root / MANIFEST
    if not os.path.lexists(path):
        if any(p.name != '.carr-ingest-writer.lock' for p in root.iterdir()):
            raise FileExistsError('Choose an empty output folder or an existing Audio Studio project.')
        return dict(kind='carr-local-audio-project', version=1, scenes={}, masters={}, active=None), None
    with _open_file(path) as (stream, info):
        if not 0 < info.st_size <= MAX_MANIFEST_BYTES:
            raise ValueError('Audio-project manifest exceeds its size limit.')
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Audio-project manifest is invalid; existing files were preserved.') from exc
    if (type(value) is not dict or set(value) != {'kind', 'version', 'scenes', 'masters', 'active'}
            or value['kind'] != 'carr-local-audio-project' or type(value['version']) is not int
            or value['version'] != 1 or type(value['scenes']) is not dict
            or type(value['masters']) is not dict
            or any(not _DIGEST.fullmatch(key) for key in (*value['scenes'], *value['masters']))):
        raise ValueError('Unsupported audio-project manifest; existing files were preserved.')
    return value, sha256(raw).hexdigest()


def _save_manifest(root, manifest, previous, max_output_bytes):
    raw = _canonical(manifest)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('The audio checkpoint limit was reached. Keep this project and choose a new output folder.')
    digest = sha256(raw).hexdigest()
    path = root / MANIFEST
    old_size = 0
    if os.path.lexists(path):
        with _open_file(path) as (stream, info):
            old_size = info.st_size
            if info.st_size > MAX_MANIFEST_BYTES or sha256(stream.read(MAX_MANIFEST_BYTES + 1)).hexdigest() != previous:
                raise ValueError('The audio-project manifest changed during rendering; existing files were preserved.')
    elif previous is not None:
        raise ValueError('The audio-project manifest disappeared during rendering.')
    if digest == previous:
        return digest
    if _used_bytes(root) - old_size + len(raw) > max_output_bytes:
        raise ValueError('The audio checkpoint exceeds the configured output byte budget.')
    temporary = None
    try:
        with NamedTemporaryFile(prefix='.studio-checkpoint-', suffix='.json', dir=root, delete=False) as output:
            temporary = Path(output.name)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        if previous is None:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        _sync_directory(root)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest


def _output_info(path, rate, frames, budget):
    """Verify only the PCM16 RIFF/RF64 shape emitted by our fixed WAV muxer.

    Python's wave reader cannot open RF64 on all supported Python versions.
    This bounded header check reads ds64 sizes, never loads PCM in memory, and
    rejects malformed/truncated output before its checksum is checkpointed.
    """
    with _open_file(path) as (stream, info):
        header = stream.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
            raise ValueError('FFmpeg did not produce a supported WAV/RF64 master.')
        rf64 = header[:4] == b'RF64'
        outer = struct.unpack('<I', header[4:8])[0]
        if outer != (0xffffffff if rf64 else info.st_size - 8):
            raise ValueError('Rendered WAV length is inconsistent.')
        fmt, ds64 = None, None
        for _ in range(128):
            budget.check()
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise ValueError('Rendered WAV has no complete PCM data chunk.')
            tag, size = struct.unpack('<4sI', chunk)
            if tag == b'data':
                expected = frames * 4
                actual = ds64[1] if rf64 and ds64 is not None else size
                if (fmt != (1, 2, rate, rate * 4, 4, 16) or actual != expected
                        or info.st_size != stream.tell() + expected
                        or (rf64 and (ds64 is None or size != 0xffffffff
                            or ds64[0] != info.st_size - 8 or ds64[2] != frames))):
                    raise ValueError('Rendered audio differs from the exact stereo PCM timeline.')
                break
            if size > 65536 or stream.tell() + size > min(info.st_size, 131072):
                raise ValueError('Rendered WAV header exceeds its bounded format.')
            content = stream.read(size)
            if len(content) != size:
                raise ValueError('Rendered WAV header is truncated.')
            if tag == b'fmt ':
                if fmt is not None or size not in (16, 18):
                    raise ValueError('Unsupported rendered WAV format.')
                fmt = struct.unpack('<HHIIHH', content[:16])
            elif tag == b'ds64':
                if not rf64 or ds64 is not None or size != 28:
                    raise ValueError('Unsupported RF64 size table.')
                ds64 = struct.unpack('<QQQI', content)
                if ds64[3] != 0:
                    raise ValueError('Unsupported RF64 size table.')
            if size & 1:
                stream.read(1)
        else:
            raise ValueError('Rendered WAV has too many header chunks.')
    return AudioArtifact(path, _hash(path, budget), frames, rate)


def _record(artifact, root):
    return dict(file=artifact.path.relative_to(root).as_posix(), sha256=artifact.sha256,
                frames=artifact.frames, sample_rate=artifact.sample_rate, channels=artifact.channels)


def _cached(root, record, frames, rate, budget):
    try:
        if (type(record) is not dict or set(record) != {'file', 'sha256', 'frames', 'sample_rate', 'channels'}
                or type(record['frames']) is not int or record['frames'] != frames
                or type(record['sample_rate']) is not int or record['sample_rate'] != rate
                or type(record['channels']) is not int or record['channels'] != 2
                or not isinstance(record['sha256'], str) or not _DIGEST.fullmatch(record['sha256'])):
            return None
        path = _inside(root, record['file'])
        artifact = _output_info(path, rate, frames, budget)
        return artifact if artifact.sha256 == record['sha256'] else None
    except (InterruptedError, TimeoutError):
        raise
    except (ValueError, OSError, EOFError):
        return None


def _run(args, stage, outputs, budget, available):
    """Bound process output, stop its native process on cancel, and never shell."""
    log_path = stage / 'ffmpeg.log'
    with log_path.open('xb') as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=log, shell=False)
        try:
            while True:
                budget.check()
                if log_path.stat().st_size > MAX_LOG_BYTES:
                    raise ValueError('FFmpeg diagnostic output exceeded its limit.')
                if sum(path.stat().st_size for path in outputs if path.exists()) > available:
                    raise ValueError('Audio output exceeded the configured disk budget.')
                try:
                    code = process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
            budget.check()
            if code:
                log.flush()
                with log_path.open('rb') as incoming:
                    detail = incoming.read(4096).decode('utf-8', errors='replace').strip()
                raise RuntimeError(f'Local FFmpeg render failed (exit {code}): {detail}')
            if sum(path.stat().st_size for path in outputs) > available:
                raise ValueError('Audio output exceeded the configured disk budget.')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _wav_args(path):
    return ['-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', str(path)]


def _scene_command(binary, root, scene, sources, rate, stage):
    frames = _frames(scene.duration_ms, rate)
    args = [str(binary), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-filter_complex_threads', '1', '-threads', '1']
    graph, labels = [], {kind: [] for kind in KINDS}
    for index, track in enumerate(scene.tracks):
        source = sources[track.path]
        args += ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(_inside(root, track.path))]
        if source['channels'] == 1:
            left, right = math.sqrt((1 - track.pan) / 2), math.sqrt((1 + track.pan) / 2)
            pan = f'pan=stereo|c0={left:.12g}*c0|c1={right:.12g}*c0'
        else:
            left, right = min(1.0, 1 - track.pan), min(1.0, 1 + track.pan)
            pan = f'pan=stereo|c0={left:.12g}*c0|c1={right:.12g}*c1'
        filters = [f'aresample={rate}', 'aformat=sample_fmts=fltp',
                   f'volume={track.gain:.12g}', pan]
        source_frames = (source['frames'] * rate + source['sample_rate'] - 1) // source['sample_rate']
        if track.fade_in_ms:
            filters.append(f'afade=t=in:ss=0:ns={_frames(track.fade_in_ms, rate)}')
        if track.fade_out_ms:
            fade = _frames(track.fade_out_ms, rate)
            filters.append(f'afade=t=out:ss={source_frames - fade}:ns={fade}')
        filters += [f'adelay={_frames(track.start_ms, rate)}S:all=1',
                    f'apad=whole_len={frames}', f'atrim=end_sample={frames}', 'asetpts=PTS-STARTPTS']
        label = f't{index}'
        graph.append(f'[{index}:a]' + ','.join(filters) + f'[{label}]')
        labels[track.kind].append(label)

    groups = []
    for kind in ('voice', 'music', 'effect'):
        entries = labels[kind]
        if not entries:
            continue
        filters = (f'amix=inputs={len(entries)}:duration=longest:normalize=0' if len(entries) > 1
                   else 'anull')
        graph.append(''.join(f'[{label}]' for label in entries) + filters + f'[{kind}_group]')
        groups.append(kind)
    if 'voice' in groups and 'music' in groups and scene.duck_music:
        graph.append('[voice_group]asplit=2[voice_ready][detector]')
        graph.append('[music_group][detector]sidechaincompress=threshold=0.05:ratio=8:'
                     'attack=10:release=100[music_ready]')
    else:
        for kind in ('voice', 'music'):
            if kind in groups:
                graph.append(f'[{kind}_group]anull[{kind}_ready]')
    if 'effect' in groups:
        graph.append('[effect_group]anull[effect_ready]')
    finish = (f'alimiter=limit=0.95:level=0:latency=1,apad=whole_len={frames},'
              f'atrim=end_sample={frames},asetpts=PTS-STARTPTS')
    for kind in groups:
        graph.append(f'[{kind}_ready]{finish},asplit=2[{kind}_mix][{kind}_stem]')
    if groups:
        joined = ''.join(f'[{kind}_mix]' for kind in groups)
        combine = f'amix=inputs={len(groups)}:duration=longest:normalize=0' if len(groups) > 1 else 'anull'
        graph.append(joined + combine + ',' + finish + '[master]')
    else:
        graph.append(f'anullsrc=r={rate}:cl=stereo,atrim=end_sample={frames}[master]')
    args += ['-filter_complex', ';'.join(graph)]
    outputs = {'mix': stage / 'mix.wav'} | {kind: stage / f'{kind}.wav' for kind in groups}
    for kind, path in outputs.items():
        args += ['-map', '[master]' if kind == 'mix' else f'[{kind}_stem]',
                 '-ar', str(rate), '-ac', '2', *_wav_args(path)]
    return args, outputs


def _publish(stage_artifact, root, relative):
    path = root / relative
    if os.path.lexists(path):
        path = path.with_name(path.stem + '-repair-' + uuid4().hex + path.suffix)
    with stage_artifact.path.open('r+b') as stream:
        os.fsync(stream.fileno())
    os.link(stage_artifact.path, path)
    _sync_directory(path.parent)
    return AudioArtifact(path, stage_artifact.sha256, stage_artifact.frames,
                         stage_artifact.sample_rate, stage_artifact.channels)


def _used_bytes(root):
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            _plain(Path(directory) / name, directory=True)
        for name in files:
            total += _plain(Path(directory) / name).st_size
    return total


def render_project(input_root, scenes, output_dir, *, ffmpeg, cancelled=lambda: False,
                   progress=None, limits=None):
    """Render scene stems and a continuous master, resuming verified checkpoints.

    Input PCM16 WAVs may be mono/stereo at different sample rates. Mono pan uses
    equal-power placement; stereo pan is balance. Millisecond locations round up
    to the next output sample. A track may not overrun its scene. Empty scenes
    produce exact silence. Existing audio is immutable: changed scenes or cache
    repairs use new paths, and old masters stay available. RF64 handles masters
    beyond RIFF's 4 GiB boundary without loading the waveform into Python memory.
    """
    limits = RenderLimits() if limits is None else limits
    if type(limits) is not RenderLimits:
        raise ValueError('Validated render limits are required.')
    if progress is not None and not callable(progress):
        raise ValueError('The progress handler must be callable.')
    progress = progress if progress is not None else lambda event: None
    budget = _Budget(limits, cancelled)
    budget.check()
    root = _root(input_root)
    if (not isinstance(scenes, (tuple, list)) or not 1 <= len(scenes) <= limits.max_scenes
            or any(type(scene) is not Scene for scene in scenes)
            or len({scene.id for scene in scenes}) != len(scenes)):
        raise ValueError('Choose unique validated scenes within the configured scene budget.')
    scenes = tuple(scenes)
    binary = Path(ffmpeg)
    if (not binary.is_absolute() or str(binary).startswith(('//', '\\\\'))
            or PureWindowsPath(str(binary)).drive.startswith('\\\\')):
        raise ValueError('Choose the absolute path of a trusted local FFmpeg executable.')
    _plain(binary)
    engine = _hash(binary, budget)
    sources, total_input = {}, 0
    plans = []
    for scene in scenes:
        budget.check()
        if len(scene.tracks) > limits.max_tracks_per_scene:
            raise ValueError('A scene exceeds the configured track budget.')
        frames = _frames(scene.duration_ms, limits.sample_rate)
        if frames * 4 > limits.max_output_bytes:
            raise ValueError('The requested scene exceeds the output byte budget.')
        for track in scene.tracks:
            if track.path not in sources:
                sources[track.path] = _input(root, track.path, budget)
                total_input += sources[track.path]['size']
                if total_input > limits.max_input_bytes:
                    raise ValueError('Prepared audio exceeds the total input byte budget.')
            source = sources[track.path]
            audio_frames = (source['frames'] * limits.sample_rate + source['sample_rate'] - 1) // source['sample_rate']
            if _frames(track.start_ms, limits.sample_rate) + audio_frames > frames:
                raise ValueError('A prepared track extends beyond its scene; choose a longer scene explicitly.')
            if any(_frames(fade, limits.sample_rate) > audio_frames
                   for fade in (track.fade_in_ms, track.fade_out_ms)):
                raise ValueError('A fade cannot extend beyond its prepared track.')
        payload = dict(pipeline=PIPELINE_VERSION, engine_binary_sha256=engine,
                       sample_rate=limits.sample_rate, scene=asdict(scene),
                       inputs=[sources[track.path] for track in scene.tracks])
        # Dataclass tuples become JSON arrays on disk. Compare the same JSON
        # shape after restart instead of spuriously invalidating every scene.
        payload = json.loads(_canonical(payload))
        plans.append((scene, sha256(_canonical(payload)).hexdigest(), frames, payload))
    project_key = sha256(_canonical(dict(pipeline=PIPELINE_VERSION,
        scenes=[key for _, key, _, _ in plans], sample_rate=limits.sample_rate))).hexdigest()
    total_frames = sum(frames for _, _, frames, _ in plans)
    if total_frames * 4 > limits.max_output_bytes:
        raise ValueError('The continuous master exceeds the output byte budget.')
    target = guard_data_directory(output_dir, purpose='audio project')
    _plain(target.parent, directory=True)
    planned_target = target.parent.resolve(strict=True) / target.name
    if planned_target.is_relative_to(root) or root.is_relative_to(planned_target):
        raise ValueError('Choose an audio output directory separate from the prepared input directory.')
    if not os.path.lexists(target):
        target.mkdir(mode=0o700)
    target = _root(target)
    with exclusive_writer(target):
        manifest, token = _load_manifest(target)
        cache = target / 'scenes'
        if not os.path.lexists(cache):
            cache.mkdir(mode=0o700)
        _plain(cache, directory=True)
        used = _used_bytes(target)
        if used > limits.max_output_bytes:
            raise ValueError('The existing project exceeds the configured output byte budget.')
        manifest['active'] = dict(key=project_key, scene_keys=[key for _, key, _, _ in plans],
                                  completed=False)
        token = _save_manifest(target, manifest, token, limits.max_output_bytes)
        used = _used_bytes(target)
        rendered = []
        for scene, key, frames, payload in plans:
            budget.check()
            expected_stems = {track.kind for track in scene.tracks}
            prior = manifest['scenes'].get(key)
            mix, stems = None, {}
            if (type(prior) is dict and set(prior) == {'scene_id', 'mix', 'stems', 'plan'}
                    and prior['scene_id'] == scene.id and type(prior['stems']) is dict
                    and set(prior['stems']) == expected_stems
                    and _canonical(prior['plan']) == _canonical(payload)):
                mix = _cached(target, prior['mix'], frames, limits.sample_rate, budget)
                stems = {kind: _cached(target, record, frames, limits.sample_rate, budget)
                         for kind, record in prior['stems'].items()}
            reused = mix is not None and all(artifact is not None for artifact in stems.values())
            if not reused:
                estimate = (frames * 4 + 131072) * (1 + len(expected_stems))
                available = limits.max_output_bytes - used
                if estimate > available or shutil.disk_usage(target).free < estimate + 8 * 1024 * 1024:
                    raise ValueError('Not enough disk space within the configured audio output budget.')
                progress(dict(state='rendering', scene_id=scene.id, completed=len(rendered), total=len(plans)))
                with TemporaryDirectory(prefix='.studio-scene-', dir=target) as temporary:
                    stage = Path(temporary)
                    args, outputs = _scene_command(binary, root, scene, sources, limits.sample_rate, stage)
                    _run(args, stage, tuple(outputs.values()), budget, available)
                    for track in scene.tracks:
                        if _hash(_inside(root, track.path), budget) != sources[track.path]['sha256']:
                            raise ValueError('A prepared track changed while rendering; no scene was accepted.')
                    checked = {kind: _output_info(path, limits.sample_rate, frames, budget)
                               for kind, path in outputs.items()}
                    accepted = {kind: _publish(artifact, target,
                        f'scenes/{scene.id}-{key}-{kind}.wav') for kind, artifact in checked.items()}
                    mix = accepted.pop('mix')
                    stems = accepted
                used = _used_bytes(target)
                manifest['scenes'][key] = dict(scene_id=scene.id, plan=payload, mix=_record(mix, target),
                    stems={kind: _record(artifact, target) for kind, artifact in stems.items()})
                token = _save_manifest(target, manifest, token, limits.max_output_bytes)
                used = _used_bytes(target)
            result = SceneRender(scene.id, mix, stems, key, reused)
            rendered.append(result)
            progress(dict(state='cached' if reused else 'rendered', scene_id=scene.id,
                          completed=len(rendered), total=len(plans)))
        budget.check()
        master = _cached(target, manifest['masters'].get(project_key), total_frames, limits.sample_rate, budget)
        if master is None:
            available = limits.max_output_bytes - used
            estimate = total_frames * 4 + 131072
            if estimate > available or shutil.disk_usage(target).free < estimate + 8 * 1024 * 1024:
                raise ValueError('Not enough disk space for the continuous master; completed scenes were kept.')
            progress(dict(state='assembling', scene_id=None, completed=len(rendered), total=len(plans)))
            with TemporaryDirectory(prefix='.studio-master-', dir=target) as temporary:
                stage = Path(temporary)
                listing = ['ffconcat version 1.0']
                for index, scene_result in enumerate(rendered):
                    name = f'part-{index:06d}.wav'
                    os.link(scene_result.mix.path, stage / name)
                    listing.append(f"file '{name}'")
                playlist = stage / 'parts.ffconcat'
                playlist.write_text('\n'.join(listing) + '\n', encoding='ascii')
                output = stage / 'master.wav'
                args = [str(binary), '-hide_banner', '-loglevel', 'error', '-nostdin',
                        '-protocol_whitelist', 'file', '-f', 'concat', '-safe', '1', '-i', str(playlist),
                        '-map', '0:a:0', '-c:a', 'copy', '-rf64', 'auto', '-f', 'wav', '-n', str(output)]
                _run(args, stage, (output,), budget, available)
                for index, scene_result in enumerate(rendered):
                    # Drop our own temporary hard links before the ordinary
                    # private-file check. A scene changed by an external writer
                    # during assembly must not be accepted as this master.
                    (stage / f'part-{index:06d}.wav').unlink()
                    if _hash(scene_result.mix.path, budget) != scene_result.mix.sha256:
                        raise ValueError('A completed scene changed during master assembly.')
                checked = _output_info(output, limits.sample_rate, total_frames, budget)
                master = _publish(checked, target, f'master-{project_key}.wav')
            manifest['masters'][project_key] = _record(master, target)
        budget.check()
        start = 0
        chapters = []
        for result in rendered:
            chapters.append(dict(scene_id=result.scene_id, start_frame=start,
                                 end_frame=start + result.mix.frames))
            start += result.mix.frames
        manifest['active'] = dict(key=project_key, scene_keys=[key for _, key, _, _ in plans],
                                  chapters=chapters, completed=True)
        _save_manifest(target, manifest, token, limits.max_output_bytes)
        progress(dict(state='complete', scene_id=None, completed=len(rendered), total=len(plans)))
        return RenderResult(master, tuple(rendered), target / MANIFEST,
                            sum(result.reused for result in rendered))
