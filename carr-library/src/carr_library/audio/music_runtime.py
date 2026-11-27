"""Project-confined facade for shared composition and installed SDK workers.

The default is a modest deterministic instrumental synthesizer implemented with
FFmpeg sources, not a neural model or a recording library. MIDI export preserves
the note plan, and an explicitly installed/licensed FluidSynth SoundFont can
render it. MusicGen and ACE-Step are separately installed optional engines;
selecting either never downloads weights or silently chooses another provider.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .models import (AudioCancelled, AudioError, AudioUnavailable, LocalModelConfig,
                     MusicSpec, finite, integer)
from .process import publish_file, run_process
from .musicgen_worker import plain_local, require_network_isolation
from ..durable_state import local_path


MUSICGEN_REVISION = '896ec7c47f5e5d1e5aa1e4b260c4405328bf009d'
ACESTEP_REVISION = 'ca1e85fe9430179831e6bc6be790c332190a3866'
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
    try:
        isolation = require_network_isolation()
        isolation_error = ''
    except RuntimeError as exc:
        isolation, isolation_error = None, str(exc)
    def state(engine):
        result = {'configured': engine in runtime.music_models, 'assets_verified': False,
                  'runtime_probed': False, 'runtime_code_verified': False, 'inference_tested': False}
        result.update(runtime.music_model_status.get(engine, {}))
        result['network_isolation_verified'] = isolation is not None
        result['unavailable_reason'] = isolation_error
        result['available'] = (result['runtime_code_verified'] and result['inference_tested']
                               and isolation is not None)
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
         'limitation': 'Reviewed local AudioCraft checkpoints and code required; private noncommercial profile. Inference also requires a separate Linux network namespace with only loopback; Windows inference is unavailable.'},
        {'engine': 'ace-step', **state('ace-step'),
         'kind': 'optional local neural model', 'network': False,
         'limitation': 'Reviewed local ACE-Step 1.5 checkpoints and code required. Inference also requires a separate Linux network namespace with only loopback; Windows inference is unavailable. No automatic downloads.'},
    ]


def _procedural(runtime, spec, output, *, sample_rate, channels, cancelled, progress):
    from .music import _procedural_graph
    target = runtime._output(output)
    source, filters, frames = _procedural_graph(spec, sample_rate, channels)
    return runtime._render([*runtime._base_args(), '-f', 'lavfi', '-i', source,
                            '-af', filters], target, expected_duration=frames / sample_rate,
                           rate=sample_rate, channels=channels, cancelled=cancelled,
                           progress=progress)


def _write_midi(path, spec, *, cancelled=lambda: False, max_bytes=32 * 1024**2):
    from .music import _write_midi_file
    def check():
        if cancelled():
            raise AudioCancelled('Local composition cancelled.')
    try:
        return _write_midi_file(path, spec, check=check, max_output_bytes=max_bytes)
    except ValueError as exc:
        if 'budget' in str(exc):
            raise AudioError(str(exc)) from exc
        raise


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


def _plain_model_files(root, *, cancelled=lambda: False):
    """Inspect each entry without following it, before deciding to descend."""
    pending = [plain_local(root, directory=True)]
    entries = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for entry in children:
                if cancelled():
                    raise AudioCancelled('Model verification cancelled.')
                entries += 1
                if entries > 200000:
                    raise ValueError('The local model inventory exceeds its entry budget.')
                # Windows DirEntry caches can report st_nlink=0 for ordinary
                # files. Read fresh no-follow identity before trusting it or
                # descending, retaining the exact one-link materialization rule.
                path = Path(entry.path)
                info = path.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('Local model files and code must not use links or reparse points.')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    yield path, info
                else:
                    raise ValueError('Local model files and code must be ordinary materialized files.')


def validate_model(config, *, cancelled=lambda: False):
    """Verify a user-installed model bundle, including complete file inventory."""
    from .dsp import _digest
    if not isinstance(config, LocalModelConfig):
        raise ValueError('Expected a typed local model installation.')
    # Check mapped-volume and reparse boundaries before any model metadata or
    # inventory access, just as for the direct Transformers adapter.
    plain_local(config.directory, directory=True)
    plain_local(config.manifest)
    if config.directory.is_symlink() or not config.directory.is_dir():
        raise ValueError('Local model directory must be a regular directory.')
    if config.manifest.is_symlink() or not config.manifest.is_file() or config.manifest.stat().st_size > 8 * 1024**2:
        raise ValueError('A bounded reviewed model manifest is required.')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate model manifest key.')
            value[key] = item
        return value
    def nonfinite(_):
        raise ValueError('Non-finite model manifest value.')
    raw = config.manifest.read_bytes()
    if len(raw) > 8 * 1024**2:
        raise ValueError('Model manifest exceeded its file budget.')
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    if type(data) is not dict:
        raise ValueError('The model manifest must be an object.')
    expected = MUSICGEN_REVISION if config.engine == 'musicgen' else ACESTEP_REVISION
    if (type(data.get('schema')) is not int or data.get('schema') != 1 or data.get('engine') != config.engine
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
    actual, total = set(), 0
    for path, info in _plain_model_files(root, cancelled=cancelled):
        total += info.st_size
        if total > 64 * 1024**3:
            raise ValueError('The local model exceeds its reviewed storage budget.')
        actual.add(path.relative_to(root).as_posix())
    if config.manifest.resolve().is_relative_to(root):
        actual.discard(config.manifest.resolve().relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError('The local model file inventory differs from its reviewed manifest.')
    for name, expected_hash in files.items():
        if not isinstance(name, str):
            raise ValueError('Unsafe model manifest entry.')
        relative = Path(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
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
    from .music import _model_python
    try:
        _model_python(config.python)
    except (FileNotFoundError, RuntimeError) as exc:
        raise AudioUnavailable('The selected local model Python runtime is not installed.') from exc
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
    root = plain_local(Path(origin).parent, directory=True)
    actual = set()
    for path, _ in _plain_model_files(root, cancelled=cancelled):
        if path.suffix != '.py':
            continue
        actual.add(path.relative_to(root).as_posix())
        if len(actual) > 100000:
            raise ValueError('The installed engine exceeds the reviewed code inventory allowance.')
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
# denial and the same fail-closed Linux network namespace requirement as the
# Transformers worker. No assertion in an installation manifest substitutes for
# observing the running process's network isolation.
_MODEL_WORKER = _SITE_BOOTSTRAP + r'''
import json, os, pathlib, socket, sys, wave
job = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
stage = pathlib.Path(job['stage']).resolve()
import runpy
guard = runpy.run_path(job['guard_module'])
guard['require_network_isolation']()
guard['deny_network']()
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
if audio.shape[1] < expected:
    raise RuntimeError('The local model returned truncated audio.')
audio = audio[:,:expected]

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
    try:
        require_network_isolation()
    except RuntimeError as exc:
        raise AudioUnavailable(str(exc)) from exc
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
               'model_name': manifest.get('model_name'),
               'guard_module': str(Path(__file__).with_name('musicgen_worker.py'))}
        request = stage / 'request.json'
        request.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        # Do not inherit API tokens, proxy settings, arbitrary Python paths or
        # provider credentials. All caches are explicit local locations.
        from .music import _worker_environment, _model_python
        env = _worker_environment(stage)
        env.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
                    'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                    'HF_HOME': str(config.directory / 'hf-cache'),
                    'HF_HUB_CACHE': str(config.directory / 'hf-cache' / 'hub'),
                    'HUGGINGFACE_HUB_CACHE': str(config.directory / 'hf-cache' / 'hub'),
                    'TRANSFORMERS_CACHE': str(config.directory / 'hf-cache' / 'hub'),
                    'TORCH_HOME': str(stage / 'torch-cache'),
                    'TMPDIR': str(stage), 'TEMP': str(stage), 'TMP': str(stage),
                    'ACESTEP_CHECKPOINTS_DIR': str(config.directory / 'checkpoints'),
                    'ACESTEP_DISABLE_TQDM': '1'})
        run_process([_model_python(config.python), '-I', '-S', '-B', '-c', _MODEL_WORKER, str(request)],
                    budget=runtime.budget, cancelled=cancelled, watch=(stage,), env=env, cwd=stage)
        if validate_model(config, cancelled=cancelled) != manifest:
            raise AudioError('The reviewed model installation changed during generation.')
        probe_model(runtime, spec.engine, cancelled=cancelled)
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
    from .music import _sfx_graph
    graph, frames = _sfx_graph(name, duration, seed, sample_rate)
    return runtime._render([*runtime._base_args(), '-filter_complex', graph, '-map', '[out]'], target,
                           expected_duration=frames / sample_rate, rate=sample_rate, channels=channels,
                           cancelled=cancelled, progress=progress)
