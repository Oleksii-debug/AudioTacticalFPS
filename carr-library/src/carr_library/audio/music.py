"""Explicit optional local MusicGen and lightweight FluidSynth music providers.

MusicGen model inference requires separately installed torch/transformers and a
reviewed, pinned local safetensors bundle. FluidSynth renders a deterministic
MIDI arpeggio with a user-selected SoundFont; it does not understand the prose
prompt and is not represented as neural music. Neither provider downloads data.
Long-form continuation and scene chunk scheduling belong to the studio jobs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
from ctypes.util import find_library
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

from .ffmpeg import AudioReport, FFmpegAudio, _check_cancel, _local_path, _number, _pcm_header, _runtime_directories, _sha


@dataclass(frozen=True)
class MusicRequest:
    prompt: str
    duration_seconds: float
    engine: str = 'musicgen'
    seed: int = 0
    tempo: float = 90
    root_note: int = 60
    mode: str = 'minor'
    program: int = 0
    sample_rate: int = 24000
    usage: str = 'personal-noncommercial'

    def __post_init__(self):
        if not isinstance(self.prompt, str) or not self.prompt.strip() or len(self.prompt) > 8192:
            raise ValueError('Music prompt must contain 1 to 8192 characters.')
        _number(self.duration_seconds, .1, 2**40, 'Music duration seconds')
        if self.engine not in ('musicgen', 'fluidsynth'):
            raise ValueError('Supported local music providers are musicgen and fluidsynth.')
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError('Music seed must be a 32-bit non-negative integer.')
        _number(self.tempo, 20, 240, 'Tempo BPM')
        if type(self.root_note) is not int or not 24 <= self.root_note <= 96:
            raise ValueError('Root MIDI note must be between 24 and 96.')
        if type(self.program) is not int or not 0 <= self.program <= 127:
            raise ValueError('General MIDI program must be between 0 and 127.')
        if self.mode not in ('major', 'minor'):
            raise ValueError('MIDI mode must be major or minor.')
        if self.usage != 'personal-noncommercial':
            raise ValueError('This configured music profile is private personal noncommercial use only.')
        FFmpegAudio._settings(self.sample_rate, 2)


def _find_fluidsynth():
    if sys.platform != 'win32':
        return find_library('fluidsynth')
    for directory in _runtime_directories():
        for filename in ('libfluidsynth-3.dll', 'libfluidsynth.dll', 'fluidsynth-3.dll', 'fluidsynth.dll'):
            candidate = _local_path(directory / filename)
            if candidate.is_file():
                return str(candidate)
    return None


def available_music_engines():
    """Discover runtimes only; a positive result does not verify model assets."""
    return {
        'musicgen': {'runtime_available': all(importlib.util.find_spec(name) is not None
                      for name in ('torch', 'transformers', 'safetensors')),
                    'requires': 'Pinned local safetensors bundle, model license and manifest SHA256',
                    'generation': 'Neural text-to-music; individual inference window is at most 30 seconds'},
        'fluidsynth': {'runtime_available': _find_fluidsynth() is not None,
                      'requires': 'User-selected SoundFont, SHA256 and recorded SoundFont license',
                      'generation': 'Deterministic MIDI arpeggio using tempo, key, mode, program and seed; no prose interpretation'},
    }


def _fluid_identity():
    name = _find_fluidsynth()
    if not name:
        raise RuntimeError('The local FluidSynth runtime is unavailable.')
    library = ctypes.CDLL(name)
    library.fluid_version_str.restype = ctypes.c_char_p
    library.fluid_version_str.argtypes = []
    version = library.fluid_version_str().decode('ascii')
    binary = None
    if sys.platform.startswith('linux'):
        # Locate the exact mapped binary containing this function, not a guessed
        # file with the same SONAME elsewhere on the loader's search path.
        pointer = ctypes.cast(library.fluid_version_str, ctypes.c_void_p).value
        for line in Path('/proc/self/maps').read_text().splitlines():
            parts = line.split(maxsplit=5)
            start, end = (int(x, 16) for x in parts[0].split('-'))
            if start <= pointer < end and len(parts) == 6:
                candidate = Path(parts[5])
                if candidate.is_file():
                    binary = candidate
                break
    elif sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = kernel.GetModuleFileNameW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        if function(library._handle, buffer, len(buffer)):
            binary = Path(buffer.value)
    return {'version': version, 'binary_sha256': _sha(binary, None) if binary else None}


def music_engine_fingerprint(request: MusicRequest, *, model_dir=None, manifest_path=None,
                             expected_manifest_sha256=None, soundfont=None,
                             soundfont_sha256=None, soundfont_license=None):
    """Identity for durable caches: adapter bytes, real runtime and pinned assets.

    Model files are verified and snapshotted again before actual inference. A
    cache identity never turns an unexecuted optional backend into a passed test.
    """
    if not isinstance(request, MusicRequest):
        raise ValueError('Expected a typed MusicRequest.')
    identity = {'request': asdict(request),
                'adapter_sha256': _sha(Path(__file__), None),
                'worker_sha256': _sha(Path(__file__).with_name('_music_worker.py'), None)}
    if request.engine == 'fluidsynth':
        if soundfont is None or not isinstance(soundfont_license, str) or not soundfont_license.strip():
            raise ValueError('Select a SoundFont and record its license.')
        if not isinstance(soundfont_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', soundfont_sha256):
            raise ValueError('Record the selected SoundFont SHA256.')
        path = _local_path(soundfont)
        if path.is_symlink() or not path.is_file() or _sha(path, None) != soundfont_sha256:
            raise ValueError('The selected SoundFont does not match its approved checksum.')
        identity.update(runtime=_fluid_identity(), soundfont_sha256=soundfont_sha256,
                        soundfont_license=soundfont_license)
    else:
        if model_dir is None or manifest_path is None:
            raise ValueError('MusicGen cache identity requires a pinned local model manifest.')
        _, digest = _manifest(model_dir, manifest_path, expected_manifest_sha256)
        versions = {}
        for package in ('torch', 'transformers', 'safetensors', 'tokenizers'):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        if any(value is None for value in versions.values()):
            raise RuntimeError('The complete approved local MusicGen runtime is unavailable; cached engine identity cannot be verified.')
        identity.update(runtime=versions, model_manifest_sha256=digest)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _manifest(model_dir, manifest_path, expected_hash):
    if not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash):
        raise ValueError('Supply the independently recorded SHA256 of the approved model manifest.')
    root = _local_path(model_dir)
    manifest_path = _local_path(manifest_path)
    if root.is_symlink() or manifest_path.is_symlink():
        raise ValueError('Model directories and manifests must not be symbolic links.')
    root = root.resolve(strict=True)
    with manifest_path.open('rb') as manifest:
        raw = manifest.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('Model manifest checksum did not match the approved manifest.')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('The MusicGen model manifest must be an object.')
    if value.get('schema') != 1 or value.get('engine') != 'musicgen':
        raise ValueError('Expected a schema 1 MusicGen manifest.')
    if (value.get('license') != 'CC-BY-NC-4.0' or not isinstance(value.get('revision'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', value['revision'])):
        raise ValueError('Record the exact MusicGen model revision and CC-BY-NC-4.0 license.')
    if not isinstance(value.get('source'), str) or not value['source'].startswith('https://huggingface.co/facebook/musicgen-'):
        raise ValueError('Record the reviewed official MusicGen source model URL.')
    files = value.get('files')
    if not isinstance(files, dict) or not files or len(files) > 512:
        raise ValueError('A bounded file checksum inventory is required.')
    safe = {}
    for name, digest in files.items():
        if not isinstance(name, str) or '\\' in name or ':' in name or not name:
            raise ValueError('Invalid model inventory path.')
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(p in ('.', '..') for p in relative.parts):
            raise ValueError('Model inventory paths must remain inside the approved directory.')
        if relative.suffix not in ('.json', '.txt', '.model', '.safetensors'):
            raise ValueError('Executable code and pickle model weights are not accepted; use reviewed safetensors.')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Model files require exact SHA256 values.')
        source = root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
            raise ValueError('Model inventory refers to an unavailable or linked file.')
        safe[name] = source, digest
    if 'config.json' not in safe or not any(n.endswith('.safetensors') for n in safe):
        raise ValueError('Model inventory must contain config.json and safetensors weights.')
    # Only inventory-listed bytes will be copied to the parser's private root;
    # local unlisted executable files can never affect the model loader.
    return safe, expected_hash


def render_music(request: MusicRequest, target, *, model_dir=None, manifest_path=None,
                 expected_manifest_sha256=None, soundfont=None, soundfont_sha256=None,
                 soundfont_license=None, cancelled=None, progress=None,
                 allowed_roots=None, timeout_seconds=None, max_output_bytes=None) -> AudioReport:
    if not isinstance(request, MusicRequest):
        raise ValueError('Expected a typed MusicRequest.')
    engine = FFmpegAudio(allowed_roots=allowed_roots, timeout_seconds=timeout_seconds,
                         max_output_bytes=max_output_bytes)
    output = engine._target(target)
    _check_cancel(cancelled)
    if request.engine == 'musicgen':
        if request.duration_seconds > 30:
            raise ValueError('MusicGen has a 30-second inference window. Schedule independently checkpointed chunks for a longer production; the final project has no 30-second limit.')
        if model_dir is None or manifest_path is None:
            raise RuntimeError('MusicGen is optional and needs an approved local model bundle and manifest; no download or remote API was attempted.')
        engine._scope(_local_path(model_dir).resolve(strict=True))
        engine._scope(_local_path(manifest_path).resolve(strict=True))
        files, identity = _manifest(model_dir, manifest_path, expected_manifest_sha256)
        if not available_music_engines()['musicgen']['runtime_available']:
            raise RuntimeError('MusicGen runtime is unavailable. Install a reviewed offline torch/transformers/safetensors runtime separately; no download was attempted.')
    else:
        if soundfont is None or not isinstance(soundfont_license, str) or not soundfont_license.strip():
            raise ValueError('FluidSynth requires a chosen local SoundFont and its recorded license.')
        if not isinstance(soundfont_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', soundfont_sha256):
            raise ValueError('Record the chosen SoundFont SHA256 before rendering.')
        if _find_fluidsynth() is None:
            raise RuntimeError('The local FluidSynth library is unavailable; no download was attempted.')
        source_font = engine._source(soundfont)
        identity = soundfont_sha256
        engine._budget(round(request.duration_seconds * request.sample_rate), request.sample_rate,
                       2, output, extra_bytes=source_font.stat().st_size)
    with tempfile.TemporaryDirectory(prefix='.music-', dir=output.parent) as temp:
        stage = Path(temp)
        data = {'request': asdict(request), 'output': str(stage / 'generated.wav')}
        if request.engine == 'musicgen':
            local_model = stage / 'model'
            local_model.mkdir()
            for name, (source, expected) in files.items():
                snapshot = local_model / name
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                actual = engine._snapshot(source, snapshot, cancelled)
                if actual != expected:
                    raise ValueError('A MusicGen model file did not match its approved SHA256.')
            config = json.loads((local_model / 'config.json').read_text(encoding='utf-8'))
            if config.get('model_type') != 'musicgen':
                raise ValueError('The approved model config is not a MusicGen architecture.')
            model_rate = config.get('audio_encoder', {}).get('sampling_rate', 32000)
            if type(model_rate) is not int or not 8000 <= model_rate <= 192000:
                raise ValueError('MusicGen model declares an unsupported sampling rate.')
            engine._budget(round(request.duration_seconds * model_rate), model_rate, 2, output)
            data['model_dir'] = str(local_model)
        else:
            font = stage / 'instrument.sf2'
            if engine._snapshot(source_font, font, cancelled) != identity:
                raise ValueError('The SoundFont checksum did not match the selected asset.')
            data['soundfont'] = str(font)
            data['fluidsynth_library'] = _find_fluidsynth()
        request_file = stage / 'request.json'
        request_file.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        environment = {key: value for key, value in os.environ.items()
                       if not any(part in key.upper() for part in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY'))}
        environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                           HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1',
                           TOKENIZERS_PARALLELISM='false')
        worker = Path(__file__).with_name('_music_worker.py').resolve(strict=True)
        engine._run([sys.executable, '-I', str(worker), str(request_file)],
                    cancelled=cancelled, progress=progress, environment=environment)
        temporary = Path(data['output'])
        rate, channels, frames, _, _ = _pcm_header(temporary)
        if abs(frames / rate - request.duration_seconds) > 1 / rate:
            raise ValueError('Music engine output duration does not match its requested segment.')
        return engine._publish(temporary, output, rate, channels, frames, (identity,), cancelled)
