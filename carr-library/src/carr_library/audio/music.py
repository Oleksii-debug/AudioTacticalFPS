"""Explicit local instrumental engines: algorithmic, FluidSynth and MusicGen.

The lightweight engine composes a seeded note pattern and asks FFmpeg to
render numeric oscillators. It is not a neural model or a recorded soundtrack.
Optional native/model workers are isolated, use locally installed assets only,
and never acquire models or silently choose a different engine.
"""
from dataclasses import dataclass, asdict
import ctypes.util
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import sys
import tempfile

from .effects import number
from .musicgen_worker import plain_local, validate_model_assets
from .render import (RenderResult, _disk_budget, _encoder_options, _ffmpeg_base,
                     _frames, _settings, check_cancelled, convert_audio,
                     file_sha256, local_file, output_path, publish_audio,
                     run_process, tool_path)


_KEYS = {'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4,
         'F': 5, 'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8, 'A': 9,
         'A#': 10, 'Bb': 10, 'B': 11}
_MOODS = {'calm', 'suspense', 'bright', 'dramatic', 'ambient', 'neutral', 'sad', 'focused'}


@dataclass(frozen=True)
class MusicSpec:
    duration_seconds: float
    tempo_bpm: float = 80
    key: str = 'A'
    mood: str = 'calm'
    seed: int = 0
    engine: str = 'procedural'
    prompt: str = ''

    def __post_init__(self):
        number(self.duration_seconds, 'music duration', .05, (2**53 - 1) / 192000)
        number(self.tempo_bpm, 'tempo bpm', 30, 240)
        if self.key not in _KEYS or self.mood not in _MOODS:
            raise ValueError('Choose a supported musical key and mood.')
        if type(self.seed) is not int or not 0 <= self.seed <= 2**32 - 1:
            raise ValueError('Music seed must be an integer from 0 to 4294967295.')
        if self.engine not in ('procedural', 'fluidsynth', 'musicgen'):
            raise ValueError('Unsupported local music engine; no automatic fallback is used.')
        if not isinstance(self.prompt, str) or len(self.prompt) > 4096 or '\x00' in self.prompt:
            raise ValueError('Music description is invalid or too long.')


@dataclass(frozen=True)
class MusicGenConfig:
    model_dir: Path
    manifest: Path
    manifest_sha256: str
    python_executable: str = sys.executable
    device: str = 'cpu'
    timeout_seconds: float | None = None


def _note_sequence(spec):
    rng = random.Random(spec.seed)
    minor = spec.mood in ('suspense', 'dramatic', 'sad')
    scale = (0, 2, 3, 5, 7, 8, 10) if minor else (0, 2, 4, 5, 7, 9, 11)
    root = 48 + _KEYS[spec.key]
    notes = [root + 12 + scale[rng.choice((0, 2, 3, 4, 6))] for _ in range(16)]
    notes[0], notes[7], notes[15] = root + 12, root + 19, root + 12
    return root, notes, minor


def _frequency(note):
    return 440 * 2 ** ((note - 69) / 12)


def _choice(values, step):
    # All terms are reviewed numeric values; no prompt text enters the graph.
    position = f'mod(t,{len(values)*step:.12g})'
    expression = f'{values[-1]:.12g}'
    for index in reversed(range(len(values)-1)):
        expression = f'if(lt({position},{(index+1)*step:.12g}),{values[index]:.12g},{expression})'
    return expression


def _procedural(spec, target, *, sample_rate, channels, cancelled, ffmpeg, ffprobe,
                max_output_bytes, timeout_seconds):
    frames = _frames(spec.duration_seconds, sample_rate)
    estimate = _disk_budget(target, frames, sample_rate, channels, max_output_bytes)
    root, notes, minor = _note_sequence(spec)
    beat = 60 / spec.tempo_bpm
    melody = _choice([_frequency(n) for n in notes], beat)
    bass = _choice([_frequency(root + n - 12) for n in (0, 5, 3 if minor else 4, 7)], beat * 4)
    third = 3 if minor else 4
    # Beat envelopes return to zero at every note boundary, avoiding a phase
    # discontinuity click. This is intentional algorithmic motif recurrence.
    envelope = f'pow(max(0,sin(PI*mod(t,{beat:.12g})/{beat:.12g})),1.5)'
    low = f'0.055*sin(2*PI*({bass})*t)'
    pad = f'0.025*sin(2*PI*{_frequency(root+third):.12g}*t)+0.02*sin(2*PI*{_frequency(root+7):.12g}*t)'
    lead = f'0.10*({envelope})*sin(2*PI*({melody})*t)'
    left = f'{lead}+{low}+{pad}'
    right = f'0.095*({envelope})*sin(2*PI*({melody})*t)+{low}+{pad}'
    expressions = left if channels == 1 else left + '|' + right
    source = f"aevalsrc=exprs='{expressions}':s={sample_rate}:c={'mono' if channels == 1 else 'stereo'}"
    fade = min(1.5, spec.duration_seconds / 4)
    filters = (f'atrim=end_sample={frames},afade=t=in:d={fade:.9f},'
               f'afade=t=out:st={spec.duration_seconds-fade:.9f}:d={fade:.9f},'
               'alimiter=limit=0.95:level=0:latency=1')
    with tempfile.TemporaryDirectory(prefix='.carr-music-', dir=target.parent) as temp:
        staged = Path(temp) / ('instrumental' + target.suffix.lower())
        args = [*_ffmpeg_base(ffmpeg), '-f', 'lavfi', '-i', source,
                '-af', filters, '-map_metadata', '-1',
                *_encoder_options(target, sample_rate=sample_rate, channels=channels),
                '-n', str(staged)]
        run_process(args, cancelled=cancelled, timeout_seconds=timeout_seconds)
        return publish_audio(staged, target, cancelled=cancelled, ffmpeg=ffmpeg, ffprobe=ffprobe,
                             expected_rate=sample_rate, expected_channels=channels,
                             expected_frames=frames if target.suffix.lower() in ('.wav', '.flac') else None,
                             metadata=dict(operation='music', engine='procedural',
                                composition='Seeded original note motif; FFmpeg oscillators; not neural generation',
                                motif_recurrence=True, original_neural_generation=False,
                                seed=spec.seed, tempo_bpm=spec.tempo_bpm, key=spec.key, mood=spec.mood,
                                estimated_pcm_bytes=estimate))


def _worker_environment(stage):
    # -I does not neutralize LD_PRELOAD or native library injection variables.
    # Start from required OS paths, not the application's secrets and loader flags.
    permitted = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'PATH', 'PATHEXT',
                 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE',
                 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'CUDA_VISIBLE_DEVICES'}
    env = {key: value for key, value in os.environ.items() if key.upper() in permitted}
    cache = str(Path(stage) / 'private-cache')
    env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               HF_HUB_DISABLE_IMPLICIT_TOKEN='1', ORT_DISABLE_TELEMETRY='1',
               DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false',
               HF_HOME=cache, HF_HUB_CACHE=cache, HUGGINGFACE_HUB_CACHE=cache,
               TRANSFORMERS_CACHE=cache, TORCH_HOME=cache, XDG_CACHE_HOME=cache,
               TEMP=str(stage), TMP=str(stage), TMPDIR=str(stage))
    return env


def music_runtime_identity(options=None):
    """Identity for director caches; configuration never exposes a credential."""
    options = dict(options or {})
    from .render import runtime_identity
    identity = {'interface_version': 1,
                'audio_runtime': runtime_identity(ffmpeg=options.get('ffmpeg'), ffprobe=options.get('ffprobe')),
                'music_sha256': file_sha256(Path(__file__)),
                'worker_sha256': file_sha256(Path(__file__).with_name('musicgen_worker.py'))}
    config = options.get('musicgen_config')
    if config is not None:
        if not isinstance(config, MusicGenConfig):
            raise ValueError('Invalid installed MusicGen configuration.')
        if file_sha256(plain_local(config.manifest)) != config.manifest_sha256:
            raise ValueError('Installed MusicGen manifest identity changed.')
        identity['musicgen'] = {'manifest_sha256': config.manifest_sha256,
                               'device': config.device,
                               'python_sha256': file_sha256(plain_local(tool_path('Python', config.python_executable)))}
    if options.get('soundfont'):
        identity['soundfont_sha256'] = file_sha256(local_file(options['soundfont']))
        identity['soundfont_license'] = options.get('soundfont_license')
        library = options.get('fluidsynth_library')
        if library:
            identity['fluidsynth_sha256'] = file_sha256(local_file(library))
        else:
            name = ctypes.util.find_library('fluidsynth')
            if not name:
                raise RuntimeError('Configured FluidSynth runtime is unavailable.')
            runtime = __import__('ctypes').CDLL(name)
            runtime.fluid_version_str.restype = __import__('ctypes').c_char_p
            identity['fluidsynth_soname'] = name
            identity['fluidsynth_version'] = runtime.fluid_version_str().decode('ascii')
    return identity


def validate_musicgen_config(config, *, cancelled=None):
    """Bind every local model byte and installed worker version to an external manifest."""
    if not isinstance(config, MusicGenConfig):
        raise ValueError('Configure an explicitly installed MusicGen model and rights manifest first.')
    manifest_path = plain_local(config.manifest)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError('MusicGen manifest is too large.')
    raw = manifest_path.read_bytes()
    if not re.fullmatch(r'[0-9a-f]{64}', config.manifest_sha256 or '') or sha256(raw).hexdigest() != config.manifest_sha256:
        raise ValueError('MusicGen manifest checksum does not match the approved installation.')
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('Duplicate model manifest key.')
                value[key] = item
            return value
        manifest = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, ValueError) as exc:
        raise ValueError('Invalid MusicGen manifest.') from exc
    required = {'version', 'engine', 'model_id', 'revision', 'weights_license', 'code_license',
                'transformers_version', 'torch_version', 'files'}
    if (type(manifest) is not dict or set(manifest) != required
            or type(manifest.get('version')) is not int or manifest.get('version') != 1
            or manifest.get('engine') != 'musicgen-transformers'
            or manifest.get('weights_license') != 'CC-BY-NC-4.0'
            or manifest.get('code_license') != 'Apache-2.0'
            or not isinstance(manifest.get('model_id'), str)
            or not re.fullmatch(r'facebook/musicgen-(?:stereo-)?(?:small|medium|large)', manifest['model_id'])
            or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', str(manifest.get('revision', '')))
            or any(not re.fullmatch(r'[0-9][A-Za-z0-9.+-]{1,40}', str(manifest.get(key, '')))
                   for key in ('transformers_version', 'torch_version'))):
        raise ValueError('Unsupported MusicGen model/rights/version manifest.')
    root = plain_local(config.model_dir, directory=True)
    if manifest_path.is_relative_to(root):
        raise ValueError('Keep the approved model manifest outside the model directory.')
    validate_model_assets(root, manifest, cancelled=lambda: check_cancelled(cancelled))
    if config.device != 'cpu' and not re.fullmatch(r'cuda(?::[0-7])?', config.device):
        raise ValueError('Choose CPU or an explicitly selected CUDA device.')
    python = tool_path('Python', config.python_executable)
    plain_local(python)
    return root, manifest, python


def _musicgen(spec, target, *, config, sample_rate, channels, cancelled, ffmpeg, ffprobe,
              max_output_bytes, timeout_seconds):
    if spec.duration_seconds > 25:
        raise ValueError('Schedule MusicGen in chunks of at most 25 seconds; final projects may span many hours.')
    model, manifest, python = validate_musicgen_config(config, cancelled=cancelled)
    validate_model_assets(model, manifest, cancelled=lambda: check_cancelled(cancelled), require_inference_assets=True)
    plain_local(target.parent, directory=True)
    frames = _frames(spec.duration_seconds, sample_rate)
    estimate = _disk_budget(target, frames, sample_rate, channels, max_output_bytes)
    with tempfile.TemporaryDirectory(prefix='.carr-musicgen-', dir=target.parent) as temp:
        stage = Path(temp)
        raw_audio, response = stage / 'generated.wav', stage / 'result.json'
        request = dict(version=1, engine='musicgen', spec=asdict(spec), model_dir=str(model),
                       manifest=manifest, device=config.device, output=str(raw_audio),
                       result=str(response))
        request_file = stage / 'request.json'
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        run_process([python, '-I', '-B', str(Path(__file__).with_name('musicgen_worker.py')),
                     str(request_file)], cancelled=cancelled,
                    timeout_seconds=timeout_seconds if timeout_seconds is not None else config.timeout_seconds,
                    env=_worker_environment(stage))
        if not response.is_file() or response.stat().st_size > 65536:
            raise RuntimeError('MusicGen worker did not provide a valid output report.')
        report = json.loads(response.read_text(encoding='utf-8'))
        if report.get('engine') != 'musicgen' or report.get('status') != 'complete':
            raise RuntimeError('MusicGen worker did not complete real synthesis.')
        validate_musicgen_config(config, cancelled=cancelled)
        converted = stage / ('converted' + target.suffix.lower())
        converted_result = convert_audio(raw_audio, converted, sample_rate=sample_rate, channels=channels,
                                         cancelled=cancelled, ffmpeg=ffmpeg, ffprobe=ffprobe)
        return publish_audio(converted, target, cancelled=cancelled, ffmpeg=ffmpeg, ffprobe=ffprobe,
                             expected_rate=sample_rate, expected_channels=channels,
                             expected_frames=frames if target.suffix.lower() in ('.wav', '.flac') else None,
                             metadata=dict(operation='music', engine='musicgen',
                                 original_neural_generation=True, seed=spec.seed,
                                 model_id=manifest['model_id'], model_revision=manifest['revision'],
                                 model_manifest_sha256=config.manifest_sha256,
                                 weights_license=manifest['weights_license'], private_noncommercial_only=True,
                                 estimated_pcm_bytes=estimate, worker=report,
                                 converted_sha256=converted_result.sha256))


def _fluidsynth(spec, target, *, soundfont, soundfont_license, fluidsynth_library,
                sample_rate, channels, cancelled, ffmpeg, ffprobe, max_output_bytes, timeout_seconds):
    if soundfont is None or not isinstance(soundfont_license, str) or not soundfont_license.strip():
        raise ValueError('Select a reviewed local SoundFont and record its license before FluidSynth generation.')
    sf2 = local_file(soundfont)
    if sf2.suffix.lower() != '.sf2' or sf2.stat().st_size > 2 * 1024**3:
        raise ValueError('Choose a bounded SF2 SoundFont.')
    library = os.fspath(fluidsynth_library) if fluidsynth_library else ctypes.util.find_library('fluidsynth')
    if not library:
        raise RuntimeError('The optional local FluidSynth runtime is not installed.')
    if not isinstance(library, str) or '\x00' in library or '://' in library:
        raise ValueError('Invalid FluidSynth library path.')
    if fluidsynth_library:
        library = str(local_file(library))
    frames = _frames(spec.duration_seconds, sample_rate)
    estimate = _disk_budget(target, frames, sample_rate, channels, max_output_bytes)
    digest = file_sha256(sf2, cancelled)
    with tempfile.TemporaryDirectory(prefix='.carr-midi-', dir=target.parent) as temp:
        stage = Path(temp)
        raw, response = stage / 'instrumental.pcm', stage / 'result.json'
        request = dict(version=1, engine='fluidsynth', spec=asdict(spec), sample_rate=sample_rate,
                       soundfont=str(sf2), soundfont_sha256=digest, library=library,
                       output=str(raw), result=str(response))
        request_file = stage / 'request.json'
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        run_process([sys.executable, '-I', '-B', str(Path(__file__).with_name('musicgen_worker.py')),
                     str(request_file)], cancelled=cancelled, timeout_seconds=timeout_seconds,
                    env=_worker_environment(stage))
        if not raw.is_file() or raw.stat().st_size != frames * 4 or file_sha256(sf2, cancelled) != digest:
            raise RuntimeError('FluidSynth output length or SoundFont identity failed verification.')
        report = json.loads(response.read_text(encoding='utf-8'))
        staged = stage / ('converted' + target.suffix.lower())
        fade = min(1.5, spec.duration_seconds / 4)
        run_process([*_ffmpeg_base(ffmpeg), '-protocol_whitelist', 'file', '-f', 's16le',
                     '-ar', str(sample_rate), '-ac', '2', '-i', str(raw),
                     '-af', f'afade=t=in:d={fade:.9f},afade=t=out:st={spec.duration_seconds-fade:.9f}:d={fade:.9f},alimiter=limit=0.95:level=0:latency=1',
                     *_encoder_options(target, sample_rate=sample_rate, channels=channels),
                     '-n', str(staged)], cancelled=cancelled, timeout_seconds=timeout_seconds)
        return publish_audio(staged, target, cancelled=cancelled, ffprobe=ffprobe, ffmpeg=ffmpeg,
                             expected_frames=frames if target.suffix.lower() in ('.wav', '.flac') else None,
                             expected_rate=sample_rate, expected_channels=channels,
                             metadata=dict(operation='music', engine='fluidsynth',
                                 composition='Seeded symbolic note composition rendered with FluidSynth',
                                 original_neural_generation=False, seed=spec.seed,
                                 tempo_bpm=spec.tempo_bpm, key=spec.key, mood=spec.mood,
                                 soundfont_sha256=digest, soundfont_license=soundfont_license,
                                 estimated_pcm_bytes=estimate, worker=report))


def generate_music(spec, output, *, sample_rate=24000, channels=2, cancelled=None,
                   ffmpeg=None, ffprobe=None, musicgen_config=None, soundfont=None,
                   soundfont_license=None, fluidsynth_library=None,
                   max_output_bytes=None, timeout_seconds=None):
    """Generate actual local audio with the explicitly chosen engine only."""
    if not isinstance(spec, MusicSpec):
        raise ValueError('Expected a typed local music specification.')
    _settings(sample_rate, channels)
    target = output_path(output)
    check_cancelled(cancelled)
    common = dict(sample_rate=sample_rate, channels=channels, cancelled=cancelled,
                  ffmpeg=ffmpeg, ffprobe=ffprobe, max_output_bytes=max_output_bytes,
                  timeout_seconds=timeout_seconds)
    if spec.engine == 'procedural':
        return _procedural(spec, target, **common)
    if spec.engine == 'fluidsynth':
        return _fluidsynth(spec, target, soundfont=soundfont, soundfont_license=soundfont_license,
                          fluidsynth_library=fluidsynth_library, **common)
    return _musicgen(spec, target, config=musicgen_config, **common)
