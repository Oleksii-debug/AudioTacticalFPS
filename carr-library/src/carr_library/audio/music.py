"""Genuine local procedural composition and optional installed music workers.

The default is a modest deterministic instrumental synthesizer implemented with
FFmpeg sources, not a neural model or a recording library. MIDI export preserves
the note plan, and an explicitly installed/licensed FluidSynth SoundFont can
render it. MusicGen and ACE-Step are separately installed optional engines;
selecting either never downloads weights or silently chooses another provider.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import random
import re
import struct
import tempfile

from .models import (AudioCancelled, AudioError, AudioUnavailable, LocalModelConfig,
                     MusicSpec, finite, integer)
from .process import publish_file, run_process
from ..durable_state import local_path, regular


MUSICGEN_REVISION = '896ec7c47f5e5d1e5aa1e4b260c4405328bf009d'
ACESTEP_REVISION = 'ca1e85fe9430179831e6bc6be790c332190a3866'
_KEYS = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
_REVIEWED_ENTRYPOINTS = {
    'musicgen': {
        'models/musicgen.py': 'c1394ee5a16414bcc6a4a0ca5555259607e0540035ea3d970884a9838b8f374a',
        'models/loaders.py': '1d81e8860c07571de8a3f218d40bab5e06df0b6c221fb60ab493c30294247946',
    },
    'ace-step': {
        'inference.py': 'f43827def02a1122e2de483961b04f16544e4da9108eed9e42fd529175c15df5',
        'core/generation/handler/init_service_orchestrator.py': 'df1e3ac43db9018793adae343760b43c56e7e81648cfcd057730162352c49d72',
        'core/generation/handler/init_service_downloads.py': '02a95f2293dc0cd82ff5046816503668f8339157ba0b18715e061f3142999f8f',
    },
}

# -S avoids executable .pth/sitecustomize hooks before our worker audit hook.
# Add the selected interpreter's package directory explicitly, preserving a
# virtual environment's original executable path instead of resolving its
# symlink back to the base interpreter.
_SITE_BOOTSTRAP = r'''
import pathlib, sys, sysconfig
executable = pathlib.Path(sys.executable).absolute()
prefix = executable.parent.parent if executable.parent.name in ('bin','Scripts') else executable.parent
if (prefix / 'pyvenv.cfg').is_file():
    package_path = prefix / ('Lib/site-packages' if sys.platform == 'win32' else 'lib/python%d.%d/site-packages' % sys.version_info[:2])
    sys.path.insert(0,str(package_path))
else:
    for key in ('purelib','platlib'):
        directory = sysconfig.get_path(key)
        if directory not in sys.path:
            sys.path.append(directory)
'''


def music_capabilities(runtime):
    def state(engine):
        result = {'configured': engine in runtime.music_models, 'assets_verified': False,
                  'runtime_probed': False, 'runtime_code_verified': False, 'inference_tested': False}
        result.update(runtime.music_model_status.get(engine, {}))
        result['available'] = result['runtime_code_verified'] and result['inference_tested']
        return result
    return [
        {'engine': 'procedural', 'available': 'aevalsrc' in runtime.capabilities()['filters'],
         'kind': 'deterministic instrumental synthesis', 'network': False,
         'limitation': 'Simple pitched instruments and a repeating motif; not neural music or general prompt understanding.'},
        {'engine': 'midi', 'available': bool(runtime.fluidsynth and runtime.soundfont),
         'kind': 'symbolic composition with FluidSynth', 'network': False,
         'limitation': 'Requires a separately installed FluidSynth binary and reviewed SoundFont.'},
        {'engine': 'musicgen', **state('musicgen'),
         'kind': 'optional local neural model', 'network': False,
         'limitation': 'Verified local weights/cache and compatible AudioCraft required; private noncommercial profile.'},
        {'engine': 'ace-step', **state('ace-step'),
         'kind': 'optional local neural model', 'network': False,
         'limitation': 'Verified local checkpoints and compatible ACE-Step 1.5 required; no automatic downloads.'},
    ]


def _motif(spec):
    # A deterministic symbolic composer; no pretrained data or copied songs.
    scale = (0, 2, 3, 5, 7, 8, 10) if spec.mood in ('suspense', 'sad') else (0, 2, 4, 5, 7, 9, 11)
    randomizer = random.Random(spec.seed)
    degrees = [0, 2, 4, 2, 3, 1, 4, 0]
    if spec.mood == 'energetic':
        degrees = [0, 4, 2, 5, 4, 1, 6, 4]
    # Seed chooses local rhythmic/melodic variations, keeping the cadence.
    degrees[2] = randomizer.choice((2, 4, 5))
    degrees[5] = randomizer.choice((1, 2, 3))
    root = 48 + _KEYS.index(spec.key)
    melody = tuple(root + 12 + scale[degree] for degree in degrees)
    bass = tuple(root - 12 + scale[degree] for degree in (0, 5, 3, 4))
    return melody, bass


def _f(value):
    return format(float(value), '.12g')


def _choice(values, clock):
    result = _f(values[-1])
    for index in range(len(values) - 2, -1, -1):
        result = f'if(eq({clock},{index}),{_f(values[index])},{result})'
    return result


def _procedural(runtime, spec, output, *, sample_rate, channels, cancelled, progress):
    target = runtime._output(output)
    runtime._space(spec.duration, sample_rate, channels, 16, target.parent)
    melody, bass = _motif(spec)
    beat = 60 / spec.tempo
    # Oscillator phase resets at note boundaries; sine-shaped note envelopes
    # reach zero at joins. FFmpeg performs source synthesis and output DSP.
    clock = f'mod(floor(t/{_f(beat)}),8)'
    bass_clock = f'mod(floor(t/{_f(4 * beat)}),4)'
    frequency = _choice(tuple(440 * 2**((note - 69) / 12) for note in melody), clock)
    bass_frequency = _choice(tuple(440 * 2**((note - 69) / 12) for note in bass), bass_clock)
    local_time = f'mod(t,{_f(beat)})'
    bass_time = f'mod(t,{_f(4 * beat)})'
    envelope = f'pow(sin(PI*{local_time}/{_f(beat)}),2)'
    bass_envelope = f'pow(sin(PI*{bass_time}/{_f(4 * beat)}),2)'
    amplitude = .08 + .14 * spec.intensity
    expr = (f'{_f(amplitude)}*{envelope}*(sin(2*PI*({frequency})*{local_time})'
            f'+0.25*sin(4*PI*({frequency})*{local_time}))'
            f'+{_f(amplitude * .7)}*{bass_envelope}*sin(2*PI*({bass_frequency})*{bass_time})')
    frames = round(spec.duration * sample_rate)
    duration = frames / sample_rate
    graph = f"aevalsrc=exprs='{expr}':s={sample_rate}:d={_f(duration)},aformat=channel_layouts=stereo"
    if spec.fade_in:
        graph += f',afade=t=in:d={_f(spec.fade_in)}'
    if spec.fade_out:
        graph += f',afade=t=out:st={_f(duration - spec.fade_out)}:d={_f(spec.fade_out)}'
    graph += f',alimiter=limit=0.9:level=0:latency=1,apad=whole_len={frames},atrim=end_sample={frames}[out]'
    return runtime._render([*runtime._base_args(), '-filter_complex', graph, '-map', '[out]'], target,
                           expected_duration=duration, rate=sample_rate, channels=channels,
                           cancelled=cancelled, progress=progress)


def _variable(value):
    integer(value, 'MIDI delta', 0, 0x0FFFFFFF)
    data = [value & 0x7F]
    while value >> 7:
        value >>= 7
        data.insert(0, (value & 0x7F) | 0x80)
    return bytes(data)


def _write_midi(path, spec, *, cancelled=lambda: False, max_bytes=32 * 1024**2):
    """Stream standard MIDI events; at most one beat's events are in memory."""
    melody, bass = _motif(spec)
    ticks = 480
    total_ticks = round(spec.duration * spec.tempo / 60 * ticks)
    tempo = round(60_000_000 / spec.tempo)
    velocity = round(35 + 65 * spec.intensity)
    with Path(path).open('xb') as stream:
        stream.write(b'MThd' + struct.pack('>IHHH', 6, 0, 1, ticks))
        stream.write(b'MTrk' + b'\x00\x00\x00\x00')
        start = stream.tell()
        stream.write(b'\x00\xff\x51\x03' + tempo.to_bytes(3, 'big'))
        stream.write(b'\x00\xff\x58\x04\x04\x02\x18\x08')
        stream.write(b'\x00\xc0\x0b\x00\xc1\x20')  # vibraphone and acoustic bass
        last = 0
        for beat_index in range(math.ceil(total_ticks / ticks)):
            if cancelled():
                raise AudioCancelled('Local composition cancelled.')
            beat_start = beat_index * ticks
            beat_end = min(total_ticks, beat_start + ticks)
            on = ((0, melody[beat_index % 8], velocity),
                  (1, bass[(beat_index // 4) % 4], max(1, velocity - 12)))
            # Keep releases inside the beat, so events stay monotonic without
            # building a multi-hour list in memory.
            for channel, note, level in on:
                stream.write(_variable(beat_start - last) + bytes((0x90 + channel, note, level)))
                last = beat_start
            release = min(beat_end, beat_start + round(ticks * .85))
            for channel, note, _ in on:
                stream.write(_variable(release - last) + bytes((0x80 + channel, note, 0)))
                last = release
            if stream.tell() - start > max_bytes:
                raise AudioError('Symbolic composition exceeded its explicit file budget.')
        stream.write(_variable(total_ticks - last) + b'\xff\x2f\x00')
        length = stream.tell() - start
        stream.seek(start - 4)
        stream.write(struct.pack('>I', length))
        stream.flush()
        os.fsync(stream.fileno())


def export_midi(runtime, spec, output, *, cancelled=lambda: False):
    if not isinstance(spec, MusicSpec):
        raise ValueError('Music requires a typed MusicSpec.')
    raw = Path(output)
    if not raw.is_absolute():
        raw = runtime.root / raw
    parent = local_path(raw.parent, exists=True)
    if not parent.is_relative_to(runtime.root) or raw.suffix.lower() not in ('.mid', '.midi'):
        raise ValueError('MIDI output must be a new .mid file inside the selected project directory.')
    target = parent / raw.name
    if target.exists() or target.is_symlink():
        raise FileExistsError('MIDI output already exists.')
    with tempfile.TemporaryDirectory(prefix='.midi-', dir=parent) as temporary:
        staged = Path(temporary) / 'score.mid'
        _write_midi(staged, spec, cancelled=cancelled, max_bytes=runtime.budget.max_output_bytes)
        publish_file(staged, target, cancelled=cancelled)
    return target


def configure_fluidsynth(runtime, binary, soundfont, *, sha256, license_notice):
    from .dsp import _digest
    if not isinstance(license_notice, str) or not license_notice.strip() or len(license_notice) > 4000:
        raise ValueError('Supply the reviewed SoundFont license and attribution notice.')
    if not isinstance(sha256, str) or not re.fullmatch('[0-9a-f]{64}', sha256):
        raise ValueError('Supply the expected SHA256 of the reviewed SoundFont.')
    source = runtime._input(soundfont)
    if source.suffix.lower() not in ('.sf2', '.sf3') or _digest(source) != sha256:
        raise ValueError('SoundFont identity does not match the reviewed installation.')
    runtime.fluidsynth = runtime._executable(binary, 'fluidsynth')
    runtime.soundfont = source
    runtime.soundfont_license = license_notice
    runtime.soundfont_sha256 = sha256


def _fluidsynth(runtime, spec, output, *, sample_rate, channels, cancelled, progress):
    from .dsp import _digest
    if not runtime.fluidsynth or not runtime.soundfont:
        raise AudioUnavailable('Install and configure FluidSynth plus a reviewed SoundFont, or select procedural music.')
    if _digest(runtime.soundfont, cancelled) != runtime.soundfont_sha256:
        raise AudioError('The configured SoundFont changed; review the installation again.')
    target = runtime._output(output)
    runtime._space(spec.duration, sample_rate, channels, 16, target.parent)
    with tempfile.TemporaryDirectory(prefix='.midi-render-', dir=target.parent) as temporary:
        midi, pcm = Path(temporary) / 'score.mid', Path(temporary) / 'score.wav'
        _write_midi(midi, spec, cancelled=cancelled, max_bytes=runtime.budget.max_output_bytes)
        # -i/-n disable interactive commands and MIDI input; -F is fast file
        # rendering, so no live audio/MIDI device is opened.
        run_process([runtime.fluidsynth, '-n', '-i', '-q', '-R', '0', '-C', '0',
                     '-r', str(sample_rate), '-F', str(pcm), '-T', 'wav',
                     str(runtime.soundfont), str(midi)], budget=runtime.budget,
                    cancelled=cancelled, watch=(pcm, midi))
        # FluidSynth includes a release tail. The selected score duration is
        # explicit; conversion cuts the tail and creates a fresh final file.
        return runtime.convert(pcm, target, sample_rate=sample_rate, channels=channels,
                               duration=spec.duration, cancelled=cancelled, progress=progress)


_MAX_INVENTORY_ENTRIES = 100000


def _inventory(root, *, cancelled, python_only=False):
    """Enumerate every directory or fail; bound all entries, including empty dirs."""
    found = set()
    pending = [root]
    count = 0

    def check_cancel():
        if cancelled():
            raise AudioCancelled('Model verification cancelled.')

    try:
        while pending:
            check_cancel()
            directory = pending.pop()
            regular(directory, directory=True)
            # scandir propagates enumeration errors that pathlib.rglob can suppress.
            # Count all entries even when only Python files are retained.
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancel()
                    count += 1
                    if count > _MAX_INVENTORY_ENTRIES:
                        raise ValueError('Local music inventory exceeds its entry limit.')
                    path = directory / entry.name
                    if entry.is_dir(follow_symlinks=False):
                        regular(path, directory=True)
                        pending.append(path)
                    else:
                        regular(path)
                        if not python_only or path.name.endswith('.py'):
                            found.add(path.relative_to(root).as_posix())
        check_cancel()
    except OSError as exc:
        raise ValueError('Local music inventory is unreadable or incomplete.') from exc
    return found


def validate_model(config, *, cancelled=lambda: False):
    """Verify a user-installed model bundle, including complete file inventory."""
    from .dsp import _digest
    if not isinstance(config, LocalModelConfig):
        raise ValueError('Expected a typed local model installation.')
    local_path(config.directory, exists=True)
    local_path(config.manifest, exists=True)
    if config.directory.is_symlink() or not config.directory.is_dir():
        raise ValueError('Local model directory must be a regular directory.')
    if config.manifest.is_symlink() or not config.manifest.is_file() or config.manifest.stat().st_size > 8 * 1024**2:
        raise ValueError('A bounded reviewed model manifest is required.')
    data = json.loads(config.manifest.read_text(encoding='utf-8'))
    expected = MUSICGEN_REVISION if config.engine == 'musicgen' else ACESTEP_REVISION
    if (data.get('schema') != 1 or data.get('engine') != config.engine
            or data.get('code_revision') != expected):
        raise ValueError('The model manifest does not match the reviewed engine revision.')
    if config.engine == 'musicgen' and (not config.private_noncommercial or
            data.get('weights_license') != 'CC-BY-NC-4.0' or data.get('noncommercial_only') is not True):
        raise ValueError('MusicGen requires reviewed CC-BY-NC-4.0 weights and private noncommercial use.')
    if not isinstance(data.get('weights_license'), str) or not data['weights_license'].strip():
        raise ValueError('Model weight license evidence is required.')
    if not isinstance(data.get('output_rights_notice'), str) or not data['output_rights_notice'].strip():
        raise ValueError('Document the reviewed model output rights before enabling generation.')
    files = data.get('files')
    if not isinstance(files, dict) or not files or len(files) > 100000:
        raise ValueError('Model manifest must contain its bounded complete SHA256 file inventory.')
    root = config.directory.resolve(strict=True)
    actual = _inventory(root, cancelled=cancelled)
    if config.manifest.resolve().is_relative_to(root):
        actual.discard(config.manifest.resolve().relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError('The local model file inventory differs from its reviewed manifest.')
    for name, expected_hash in files.items():
        relative = Path(name)
        if (not isinstance(name, str) or relative.is_absolute() or '..' in relative.parts or '\\' in name
                or not re.fullmatch('[0-9a-f]{64}', str(expected_hash))):
            raise ValueError('Unsafe model manifest entry.')
        path = root / relative
        if not path.resolve(strict=True).is_relative_to(root) or _digest(path, cancelled) != expected_hash:
            raise ValueError('Model asset failed SHA256 verification.')
    if config.engine == 'musicgen' and not {'state_dict.bin', 'compression_state_dict.bin'} <= set(files):
        raise ValueError('MusicGen requires local language and compression checkpoints.')
    if config.engine == 'ace-step':
        model_name = data.get('model_name')
        if not isinstance(model_name, str) or not re.fullmatch('[a-zA-Z0-9_.-]{1,100}', model_name):
            raise ValueError('ACE-Step requires a simple reviewed local model directory name.')
        if not any(name.startswith('checkpoints/' + model_name + '/') for name in files):
            raise ValueError('ACE-Step checkpoint directory is missing from the verified inventory.')
    if not config.python.is_file():
        raise AudioUnavailable('The selected local model Python runtime is not installed.')
    return data


def configure_model(runtime, config, *, cancelled=lambda: False):
    manifest = validate_model(config, cancelled=cancelled)
    runtime.music_models[config.engine] = (config, manifest)
    runtime.music_model_status[config.engine] = {'assets_verified': True,
        'runtime_probed': False, 'runtime_code_verified': False, 'inference_tested': False}


def probe_model(runtime, engine, *, cancelled=lambda: False):
    """Verify the selected installed SDK source against a reviewed manifest.

    No engine import or model inference happens here. A matching declaration of
    a Git revision is insufficient: the complete package Python file inventory
    must match, including hard-pinned upstream adapter entrypoint bytes.
    """
    from .dsp import _digest
    if engine not in runtime.music_models:
        raise AudioUnavailable('Configure the explicit local music installation first.')
    config, configured_manifest = runtime.music_models[engine]
    # A failed recheck must not leave the previous installation advertised as
    # available. Only an unchanged, successfully reverified identity can
    # retain evidence of earlier inference.
    status = runtime.music_model_status[engine]
    prior_inference = status.get('inference_tested', False)
    status.update(assets_verified=False, runtime_probed=False,
                  runtime_code_verified=False, inference_tested=False)
    data = validate_model(config, cancelled=cancelled)
    if data != configured_manifest:
        raise ValueError('The configured model manifest changed; review and configure the installation again.')
    status['assets_verified'] = True
    records = data.get('runtime_code_files')
    if not isinstance(records, dict) or not records or len(records) > 100000:
        raise AudioUnavailable('The reviewed model manifest needs a complete runtime_code_files SHA256 inventory before inference.')
    for name, checksum in _REVIEWED_ENTRYPOINTS[engine].items():
        if records.get(name) != checksum:
            raise ValueError('The runtime code manifest differs from the reviewed upstream adapter entrypoints.')
    package = 'audiocraft' if engine == 'musicgen' else 'acestep'
    code = _SITE_BOOTSTRAP + '\nimport importlib.util,json\n' + (
        f'spec=importlib.util.find_spec({package!r})\n'
        'print(json.dumps({"origin":spec.origin if spec else None}))\n')
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG') if key in os.environ}
    result = run_process([str(config.python), '-I', '-S', '-B', '-c', code],
                         budget=runtime.budget, env=env, cancelled=cancelled)
    origin = json.loads(result.stdout).get('origin')
    if not isinstance(origin, str):
        raise AudioUnavailable(f'The selected Python does not contain the {package} package.')
    root = local_path(Path(origin).parent, exists=True)
    actual = _inventory(root, cancelled=cancelled, python_only=True)
    if actual != set(records):
        raise ValueError('The installed engine Python file inventory differs from the reviewed manifest.')
    for name, expected_hash in records.items():
        if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                or '\\' in name or not name.endswith('.py')
                or not re.fullmatch('[0-9a-f]{64}', str(expected_hash))):
            raise ValueError('Unsafe installed engine code manifest.')
        source = local_path(root / name, root=root, exists=True)
        if _digest(source, cancelled) != expected_hash:
            raise ValueError('Installed engine code failed its reviewed SHA256 check.')
    status.update(runtime_probed=True, runtime_code_verified=True,
                  inference_tested=prior_inference)
    return dict(status)


# Trusted application worker code, never constructed from a user's prompt or
# provider response. Offline cache flags are combined with Python network audit
# denial. This is defense in depth, not a claim of an OS sandbox for native code.
_MODEL_WORKER = _SITE_BOOTSTRAP + r'''
import json, os, pathlib, socket, sys, wave
job = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
stage = pathlib.Path(job['stage']).resolve()
def audit(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise PermissionError('Local music worker denies network and child commands.')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing and not pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
            raise PermissionError('Local music worker may only write its staging directory.')
sys.addaudithook(audit)
import numpy as np
import torch
torch.manual_seed(job['seed'])
torch.set_num_threads(job['threads'])
if job['engine'] == 'musicgen':
    from audiocraft.models import MusicGen
    model = MusicGen.get_pretrained(job['directory'], device=job['device'])
    model.set_generation_params(duration=job['duration'])
    with torch.inference_mode():
        audio = model.generate([job['prompt']], progress=False)[0].detach().cpu().float().numpy()
    rate = int(model.sample_rate)
else:
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationParams, GenerationConfig, generate_music
    from acestep.model_downloader import check_main_model_exists, check_model_exists, _check_code_mismatch
    checkpoints = pathlib.Path(job['directory']) / 'checkpoints'
    if not check_main_model_exists(checkpoints) or not check_model_exists(job['model_name'], checkpoints):
        raise RuntimeError('Incomplete offline ACE-Step model installation.')
    if _check_code_mismatch(job['model_name'], checkpoints):
        raise RuntimeError('ACE-Step model code differs from the installed runtime; review installation.')
    handler = AceStepHandler()
    handler._progress_estimates_path = str(stage / 'progress-estimates.json')
    status, ready = handler.initialize_service(project_root=job['directory'], config_path=job['model_name'], device=job['device'], compile_model=False, use_mlx_dit=False)
    if not ready:
        raise RuntimeError(status)
    params = GenerationParams(caption=job['prompt'], lyrics='[Instrumental]', instrumental=True,
        bpm=round(job['tempo']), keyscale=job['key'] + ' minor' if job['mood'] in ('suspense','sad') else job['key'] + ' major',
        duration=job['duration'], seed=job['seed'], thinking=False, use_cot_metas=False,
        use_cot_caption=False, use_cot_language=False, enable_normalization=False)
    config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[job['seed']], audio_format='wav')
    result = generate_music(handler, None, params, config, save_dir=str(stage))
    if not result.success or len(result.audios) != 1:
        raise RuntimeError(result.error or 'ACE-Step returned no single audio result.')
    import soundfile as sf
    candidate = pathlib.Path(result.audios[0]['path']).resolve()
    if not candidate.is_relative_to(stage):
        raise RuntimeError('ACE-Step returned an output outside staging.')
    values, rate = sf.read(candidate, always_2d=True, dtype='float32')
    audio = values.T
if audio.ndim != 2 or audio.shape[0] not in (1,2) or not np.isfinite(audio).all():
    raise RuntimeError('The local model returned invalid audio.')
expected = round(job['duration'] * rate)
if audio.shape[1] < expected - max(1,round(rate*.05)):
    raise RuntimeError('The local model returned truncated audio.')
audio = audio[:,:expected]
if audio.shape[1] < expected:
    audio = np.pad(audio,((0,0),(0,expected-audio.shape[1])))
peak = float(np.max(np.abs(audio)))
if peak <= 1e-7:
    raise RuntimeError('The local model returned silence.')
if peak > .95:
    audio *= .95 / peak
pcm = np.rint(np.clip(audio,-1,1).T*32767).astype('<i2')
with wave.open(job['output'],'wb') as destination:
    destination.setparams((audio.shape[0],2,rate,0,'NONE','not compressed'))
    destination.writeframes(pcm.tobytes())
'''


def _neural(runtime, spec, output, *, sample_rate, channels, cancelled, progress):
    if spec.engine not in runtime.music_models:
        raise AudioUnavailable(f'{spec.engine} is not configured. Install and verify its local runtime and weights explicitly.')
    config, manifest = runtime.music_models[spec.engine]
    probe_model(runtime, spec.engine, cancelled=cancelled)
    if spec.duration > config.max_chunk_seconds:
        raise ValueError('This request exceeds the configured neural chunk allowance. Schedule multiple resumable scenes; do not request an unlimited single inference.')
    target = runtime._output(output)
    with tempfile.TemporaryDirectory(prefix='.music-worker-', dir=target.parent) as temporary:
        stage = Path(temporary)
        pcm = stage / 'generated.wav'
        job = {'engine': spec.engine, 'directory': str(config.directory.resolve()),
               'stage': str(stage), 'output': str(pcm), 'duration': spec.duration,
               'prompt': spec.prompt or f'{spec.mood} instrumental music, no vocals, {spec.tempo} BPM, key {spec.key}',
               'seed': spec.seed, 'tempo': spec.tempo, 'key': spec.key, 'mood': spec.mood,
               'device': config.device, 'threads': runtime.budget.threads,
               'model_name': manifest.get('model_name')}
        request = stage / 'request.json'
        request.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        # Do not inherit API tokens, proxy settings, arbitrary Python paths or
        # provider credentials. All caches are explicit local locations.
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG',
                                               'LD_LIBRARY_PATH', 'CUDA_PATH') if key in os.environ}
        env.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
                    'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                    'HF_HOME': str(config.directory / 'hf-cache'), 'TORCH_HOME': str(stage / 'torch-cache'),
                    'TMPDIR': str(stage), 'TEMP': str(stage), 'TMP': str(stage),
                    'ACESTEP_CHECKPOINTS_DIR': str(config.directory / 'checkpoints'),
                    'ACESTEP_DISABLE_TQDM': '1'})
        run_process([str(config.python), '-I', '-S', '-B', '-c', _MODEL_WORKER, str(request)],
                    budget=runtime.budget, cancelled=cancelled, watch=(stage,), env=env, cwd=stage)
        result = runtime.convert(pcm, target, sample_rate=sample_rate, channels=channels,
                                 duration=spec.duration, cancelled=cancelled, progress=progress)
        # A real output proves this operation, not an audit of the complete
        # installed Python distribution. The registry keeps those facts separate.
        runtime.music_model_status[spec.engine].update(runtime_probed=True, inference_tested=True)
        return result


def render_music(runtime, spec, output, *, sample_rate=24000, channels=2,
                 cancelled=lambda: False, progress=None):
    if not isinstance(spec, MusicSpec):
        raise ValueError('Music generation requires a typed MusicSpec.')
    integer(sample_rate, 'Music sample rate', 8000, 192000)
    integer(channels, 'Music channels', 1, 2)
    if cancelled():
        raise AudioCancelled('Music generation cancelled before starting.')
    renderer = _procedural if spec.engine == 'procedural' else _fluidsynth if spec.engine == 'midi' else _neural
    return renderer(runtime, spec, output, sample_rate=sample_rate, channels=channels,
                    cancelled=cancelled, progress=progress)


def render_sfx(runtime, name, output, *, duration=1, seed=0, sample_rate=24000,
               channels=2, cancelled=lambda: False, progress=None):
    """Synthesized cues, labelled procedural rather than real-world recordings."""
    if name not in ('door', 'click', 'chime', 'noise'):
        raise ValueError('Supported procedural sound cues are door, click, chime, and noise.')
    duration = finite(duration, 'Sound cue duration', .01)
    integer(seed, 'Sound seed', 0, 2**31 - 1)
    integer(sample_rate, 'Sound sample rate', 8000, 192000)
    integer(channels, 'Sound channels', 1, 2)
    target = runtime._output(output)
    runtime._space(duration, sample_rate, channels, 16, target.parent)
    frames = round(duration * sample_rate)
    duration = frames / sample_rate
    if name == 'noise':
        graph = f'anoisesrc=c=pink:r={sample_rate}:a=0.2:d={_f(duration)}:s={seed}'
    elif name == 'door':
        graph = (f'anoisesrc=c=brown:r={sample_rate}:a=0.6:d={_f(duration)}:s={seed},'
                 f"lowpass=f=500,afade=t=out:st=0:d={_f(min(duration,.4))}")
    else:
        frequency = 1700 if name == 'click' else 880
        envelope = 50 if name == 'click' else 5
        expr = f'0.35*sin(2*PI*{frequency}*t)*exp(-{envelope}*t)'
        if name == 'chime':
            expr += '+0.12*sin(2*PI*1320*t)*exp(-4*t)'
        graph = f"aevalsrc=exprs='{expr}':s={sample_rate}:d={_f(duration)}"
    graph += f',aformat=channel_layouts=stereo,alimiter=limit=0.9:level=0:latency=1,apad=whole_len={frames},atrim=end_sample={frames}[out]'
    return runtime._render([*runtime._base_args(), '-filter_complex', graph, '-map', '[out]'], target,
                           expected_duration=duration, rate=sample_rate, channels=channels,
                           cancelled=cancelled, progress=progress)
