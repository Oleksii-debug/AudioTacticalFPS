"""Local original score generation and optional verified MusicGen inference.

The procedural engine composes notes, writes MIDI and delegates all waveform
synthesis/mixing to FFmpeg. It is not neural music or a sampled instrument.
Long scores are disk-backed, hash-checked chunks, never a full in-memory wave.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import stat
import struct
import sys
import tempfile
import wave

from .engines import EngineUnavailable, _binary, _number, _output, _publish, _run
from .models import MUSIC_KEYS, music_key
from .model_manifest import LocalModelManifest, ModelManifestError, digest_file, local_path, offline_environment, read_json, _local_inventory


COMPOSER_VERSION = 'procedural-score-1'
MOODS = ('calm', 'suspense', 'bright', 'hopeful', 'sad', 'neutral', 'energetic', 'dark')
MUSICGEN_SOURCE = '896ec7c47f5e5d1e5aa1e4b260c4405328bf009d'
ACESTEP_SOURCE = 'ca1e85fe9430179831e6bc6be790c332190a3866'
FLUIDSYNTH_API_SOURCE = '5500b60788287ceb7297f628f75c488d2720f61a'


@dataclass(frozen=True)
class Note:
    start: int
    end: int
    pitch: int
    velocity: int
    channel: int


def _settings(duration, mood, tempo, seed, gain, sample_rate):
    duration = _number(duration, 1 / 48000, 1e12, 'Music duration in seconds')
    if mood not in MOODS:
        raise ValueError('Choose a supported music mood: ' + ', '.join(MOODS) + '.')
    tempo = _number(tempo, 30, 240, 'Music tempo in beats per minute')
    _number(gain, 0, 1, 'Music source gain')
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError('Music seed must be a nonnegative 63-bit integer.')
    if type(sample_rate) is not int or sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000, 96000):
        raise ValueError('Choose a supported music sample rate.')
    frames = round(duration * sample_rate)
    if frames < 1:
        raise ValueError('Music duration is shorter than one sample.')
    return frames, tempo


def _keyscale(key, mood):
    music_key(key)
    minor = mood in ('suspense', 'sad', 'dark')
    return (key if key is not None else ('A' if minor else 'C')) + (' minor' if minor else ' major')


def _notes_for_bars(first_bar, bar_count, *, sample_rate, tempo, mood, seed, total_frames, key=None):
    """Compose a bounded group of bars with stable per-bar random decisions."""
    minor = mood in ('suspense', 'sad', 'dark')
    scale = (0, 2, 3, 5, 7, 8, 10) if minor else (0, 2, 4, 5, 7, 9, 11)
    music_key(key)
    root = (57 if minor else 60) if key is None else (48 if minor else 60) + MUSIC_KEYS.index(key)
    progression = (0, 5, 3, 4) if minor else (0, 3, 5, 4)
    beat = sample_rate * 60 / tempo
    notes = []
    for bar in range(first_bar, first_bar + bar_count):
        start = round(bar * 4 * beat)
        if start >= total_frames:
            break
        end = min(total_frames, round((bar + 1) * 4 * beat))
        random_source = random.Random(hashlib.sha256(f'{seed}/{bar}/{mood}'.encode('ascii')).digest())
        degree = progression[bar % len(progression)]
        for step in (0, 2, 4):
            pitch = root - 12 + scale[(degree + step) % 7] + 12 * ((degree + step) // 7)
            notes.append(Note(start, end, pitch, 44 if mood == 'calm' else 50, 1))
        melody_steps = 4 if mood in ('calm', 'sad') else 8
        for position in range(melody_steps):
            begin = round((bar * 4 + position * 4 / melody_steps) * beat)
            stop = min(total_frames, round((bar * 4 + (position + .85) * 4 / melody_steps) * beat))
            if begin >= total_frames or stop <= begin:
                break
            selection = (degree + random_source.choice((0, 2, 4, 6))) % 7
            pitch = root + scale[selection] + (12 if mood in ('bright', 'energetic') and position % 3 == 0 else 0)
            notes.append(Note(begin, stop, pitch, random_source.randrange(54, 79), 0))
    return tuple(notes)


def _vlq(value):
    if type(value) is not int or not 0 <= value <= 0x0fffffff:
        raise ValueError('MIDI event interval exceeds its format limit.')
    result = [value & 0x7f]
    while value >> 7:
        value >>= 7
        result.insert(0, (value & 0x7f) | 0x80)
    return bytes(result)


def _write_midi(path, *, frames, sample_rate, mood, tempo, seed, cancelled,
                first_bar=0, start_frame=0, key=None):
    """Write SMF type 0 incrementally; no full-project event list in RAM."""
    ticks = 480
    micros = round(60_000_000 / tempo)
    end_frame = start_frame + frames
    bar_count = math.ceil(end_frame / (sample_rate * 240 / tempo))
    last_tick = 0
    with Path(path).open('xb') as stream:
        stream.write(b'MThd' + struct.pack('>IHHH', 6, 0, 1, ticks))
        stream.write(b'MTrk\x00\x00\x00\x00')
        stream.write(b'\x00\xff\x51\x03' + micros.to_bytes(3, 'big'))
        stream.write(b'\x00\xc0\x04\x00\xc1\x58')
        for first in range(first_bar, bar_count, 4):
            if cancelled():
                raise InterruptedError('Music generation cancelled.')
            notes = _notes_for_bars(first, min(4, bar_count-first), sample_rate=sample_rate,
                                    tempo=tempo, mood=mood, seed=seed, total_frames=end_frame, key=key)
            events = []
            for note in notes:
                begin = max(0, round((note.start - start_frame) / sample_rate * tempo / 60 * ticks))
                end = max(begin + 1, round((note.end - start_frame) / sample_rate * tempo / 60 * ticks))
                events.extend(((begin, 1, bytes((0x90 + note.channel, note.pitch, note.velocity))),
                               (end, 0, bytes((0x80 + note.channel, note.pitch, 0)))))
            for tick, _, message in sorted(events):
                stream.write(_vlq(tick - last_tick) + message)
                last_tick = tick
        end_tick = max(last_tick, round(frames / sample_rate * tempo / 60 * ticks))
        stream.write(_vlq(end_tick - last_tick) + b'\xff\x2f\x00')
        size = stream.tell() - 22
        if size > 0xffffffff:
            raise ValueError('The MIDI track exceeds the selected file format; split the score into chapters.')
        stream.seek(18)
        stream.write(struct.pack('>I', size))
        stream.flush()
        os.fsync(stream.fileno())


def _wave_frames(path):
    """Use the shared bounded RIFF/RF64 validator for music checkpoints."""
    from .ffmpeg import wav_info
    info = wav_info(path)
    if info.channels != 1:
        raise ValueError('Expected mono PCM16 music output.')
    return {'frames': info.frames, 'sample_rate': info.rate,
            'channels': info.channels, 'sample_width': 2}


def _atomic_json(path, data):
    path = local_path(path, must_exist=False)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.music-state-',
                                     suffix='.json', encoding='utf-8', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _render_procedural_chunk(path, *, first_bar, start_frame, frame_count, total_frames, sample_rate,
                             tempo, mood, seed, gain, ffmpeg, cancelled, key=None):
    notes = _notes_for_bars(first_bar, 4, sample_rate=sample_rate, tempo=tempo,
                            mood=mood, seed=seed, total_frames=total_frames, key=key)
    graph, labels = [], []
    for number, note in enumerate(notes):
        start = max(start_frame, note.start) - start_frame
        end = min(start_frame + frame_count, note.end) - start_frame
        if end <= start:
            continue
        length = end - start
        frequency = 440 * 2 ** ((note.pitch - 69) / 12)
        fade = min(max(1, round(sample_rate * .018)), max(1, length // 4))
        graph.append(f'sine=frequency={frequency:.10f}:sample_rate={sample_rate},'
                     f'atrim=end_sample={length},afade=t=in:ss=0:ns={fade},'
                     f'afade=t=out:ss={max(0,length-fade)}:ns={fade},'
                     f'volume={note.velocity/100:.6f},adelay={start}S:all=1[n{number}]')
        labels.append(f'[n{number}]')
    if not labels:
        raise ValueError('The score contains no notes in this chunk.')
    graph.append(''.join(labels) + f'amix=inputs={len(labels)}:normalize=0,'
                 f'volume={gain:.8f},apad=whole_len={frame_count},atrim=end_sample={frame_count}[music]')
    # Keep the numeric filter graph outside the Windows command-line limit.
    # FFmpeg 9 removed filter_complex_script; the file-valued option is supported.
    filters = ';\n'.join(graph)
    if len(filters) > 24000:
        raise ValueError('The bounded music filter graph exceeds its size budget.')
    script = Path(path).with_suffix('.filters')
    try:
        script.write_text(filters, encoding='ascii')
        _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-filter_complex_threads', '1',
              '-/filter_complex', script, '-map', '[music]', '-ar', str(sample_rate), '-ac', '1',
              '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', path],
             timeout=max(120, frame_count / sample_rate * 10), cancelled=cancelled,
             env=offline_environment())
    finally:
        script.unlink(missing_ok=True)


_MUSICGEN_WORKER = r'''
import json, math, os, pathlib, sys, wave
request = json.loads(sys.stdin.buffer.read())
def offline_audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo',
                 'socket.sendto', 'urllib.Request', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('Local MusicGen worker denies network and child-process execution')
sys.addaudithook(offline_audit)
import torch
from audiocraft.models import MusicGen
torch.manual_seed(request['seed'])
model = MusicGen.get_pretrained(request['root'], device=request['device'])
prompt_audio = None
prompt_frames = 0
if request['previous']:
    import numpy as np
    with wave.open(request['previous'], 'rb') as source:
        count = min(source.getnframes(), source.getframerate() * 5)
        source.setpos(source.getnframes() - count)
        pcm = np.frombuffer(source.readframes(count), dtype='<i2').astype('float32') / 32768
        prompt_audio = torch.from_numpy(pcm).reshape(1, -1)
        prompt_rate = source.getframerate()
        prompt_frames = round(count * model.sample_rate / prompt_rate)
seconds = math.ceil((request['duration'] + prompt_frames / model.sample_rate) * 50) / 50
if seconds > model.max_duration:
    raise ValueError('This model context is shorter than the selected continuation chunk')
model.set_generation_params(duration=seconds, extend_stride=min(18,model.max_duration-1))
with torch.no_grad():
    if prompt_audio is None:
        generated = model.generate([request['prompt']], progress=False)[0]
    else:
        generated = model.generate_continuation(prompt_audio, prompt_rate,
                    descriptions=[request['prompt']], progress=False)[0][:, prompt_frames:]
    samples = generated.detach().float().cpu()
    if samples.ndim != 2 or samples.shape[0] not in (1, 2) or not torch.isfinite(samples).all():
        raise ValueError('MusicGen returned invalid PCM')
    pcm = (samples.clamp(-1,1).transpose(0,1).numpy() * 32767).round().astype('<i2').tobytes()
    with wave.open(request['output'], 'wb') as target:
        target.setnchannels(samples.shape[0]); target.setsampwidth(2)
        target.setframerate(model.sample_rate); target.writeframes(pcm)
print(json.dumps({'frames': samples.shape[1], 'sample_rate': model.sample_rate,
                  'channels':samples.shape[0], 'engine':'musicgen'}))
'''


def _render_musicgen_chunk(path, *, frame_count, sample_rate, mood, tempo, seed, gain, ffmpeg,
                          manifest, previous, cancelled, key=None):
    options = manifest.settings
    if options.get('source_revision') != MUSICGEN_SOURCE:
        raise ModelManifestError('Pin the reviewed AudioCraft source revision in the model profile.')
    device = options.get('device', 'cpu')
    if device not in ('cpu', 'cuda'):
        raise ModelManifestError('Choose the explicitly supported local MusicGen device.')
    python_path = options.get('python')
    if not isinstance(python_path, str):
        raise ModelManifestError('Select the manifest-listed Python executable for local MusicGen.')
    executable = _binary(manifest.asset(python_path))
    raw = Path(path).with_suffix('.neural.wav')
    cache = manifest.root / 'hf-cache'
    if not cache.is_dir():
        raise ModelManifestError('Install the required local text-encoder cache before using MusicGen.')
    for file, info in _local_inventory(cache, cancelled=cancelled):
        if stat.S_ISREG(info.st_mode) and file.relative_to(manifest.root).as_posix() not in dict(manifest.files):
            raise ModelManifestError('Every text-encoder cache asset must be listed in the verified manifest.')
    environment = offline_environment()
    environment.update(HF_HOME=str(cache), HUGGINGFACE_HUB_CACHE=str(cache / 'hub'),
                       AUDIOCRAFT_CACHE_DIR=str(manifest.root), TORCH_HOME=str(manifest.root / 'torch-cache'))
    prompt = f'Original instrumental music, no vocals, {mood} mood, {tempo:g} beats per minute.'
    if key is not None:
        prompt += ' Key: ' + _keyscale(key, mood) + '.'
    request = {'root': str(manifest.root), 'device': device, 'seed': seed % (2**31),
               'duration': frame_count / sample_rate, 'previous': str(previous) if previous else None,
               'prompt': prompt, 'output': str(raw)}
    try:
        _run([executable, '-I', '-B', '-u', '-c', _MUSICGEN_WORKER],
             data=json.dumps(request, ensure_ascii=True).encode('utf-8'),
             timeout=options.get('timeout_seconds', 3600), cancelled=cancelled,
             env=environment, cwd=manifest.root)
        with wave.open(str(raw), 'rb') as source:
            actual = source.getnframes() / source.getframerate()
            if actual + .04 < frame_count / sample_rate:
                raise ValueError('MusicGen returned a truncated chunk.')
        _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-protocol_whitelist', 'file',
              '-f', 'wav', '-i', raw, '-af', f'volume={gain:.8f},aresample={sample_rate},'
              f'apad=whole_len={frame_count},atrim=end_sample={frame_count}', '-ar', str(sample_rate),
              '-ac', '1', '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', path],
             cancelled=cancelled, timeout=120, env=offline_environment())
    finally:
        raw.unlink(missing_ok=True)


# Reused public interfaces from PR103, adapted to this renderer's manifest,
# checkpoint and cancellation contracts. Source/weight preparation is explicit.
_ACESTEP_ENTRYPOINTS = {
    'inference.py': 'f43827def02a1122e2de483961b04f16544e4da9108eed9e42fd529175c15df5',
    'core/generation/handler/init_service_orchestrator.py': 'df1e3ac43db9018793adae343760b43c56e7e81648cfcd057730162352c49d72',
    'core/generation/handler/init_service_downloads.py': '02a95f2293dc0cd82ff5046816503668f8339157ba0b18715e061f3142999f8f',
}


def _reviewed_text(value, label, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ModelManifestError(f'Record the reviewed {label} in the local music profile.')
    return value


def _music_profile(manifest, engine):
    options = manifest.settings
    _number(options.get('timeout_seconds', 3600), 1, 86400, 'Music worker time budget')
    if engine == 'fluidsynth':
        if options.get('api_source_revision') != FLUIDSYNTH_API_SOURCE:
            raise ModelManifestError('Pin the reviewed FluidSynth API source revision.')
        _reviewed_text(options.get('runtime_license'), 'FluidSynth runtime license')
        version = options.get('runtime_version')
        if not isinstance(version, str) or not re.fullmatch(r'2\.\d+\.\d+', version):
            raise ModelManifestError('Pin the installed FluidSynth 2.x runtime version.')
        soundfont = manifest.asset(options.get('soundfont'))
        if soundfont.suffix.lower() not in ('.sf2', '.sf3'):
            raise ModelManifestError('Select an explicitly reviewed SF2 or SF3 SoundFont.')
        _reviewed_text(options.get('output_rights_notice'), 'SoundFont output rights and attribution')
        backend = options.get('backend', 'cli')
        if backend == 'cli':
            _binary(manifest.asset(options.get('executable')))
        elif backend == 'library':
            manifest.asset(options.get('library'))
        else:
            raise ModelManifestError('Choose the FluidSynth CLI or shared-library backend explicitly.')
    elif engine == 'ace-step':
        if options.get('source_revision') != ACESTEP_SOURCE:
            raise ModelManifestError('Pin the reviewed ACE-Step 1.5 source revision.')
        _reviewed_text(options.get('output_rights_notice'), 'ACE-Step output rights')
        _binary(manifest.asset(options.get('python')))
        package = manifest.directory(options.get('package_dir'))
        for name, expected in _ACESTEP_ENTRYPOINTS.items():
            source = package / 'acestep' / name
            if digest_file(source) != expected:
                raise ModelManifestError('Installed ACE-Step entrypoints differ from the reviewed revision.')
        if options.get('device', 'cpu') not in ('cpu', 'cuda'):
            raise ModelManifestError('Choose the explicitly supported ACE-Step CPU or CUDA device.')
        name = options.get('model_name')
        if not isinstance(name, str) or not re.fullmatch(r'acestep-v15-(turbo|base|sft)', name):
            raise ModelManifestError('Choose a reviewed ACE-Step 1.5 turbo, base or SFT checkpoint.')
        for component in ('vae', 'Qwen3-Embedding-0.6B', name):
            manifest.directory('checkpoints/' + component)
        _number(options.get('chunk_seconds', 30), 10, 120, 'ACE-Step chunk seconds')
        threads = options.get('threads', 2)
        steps = options.get('inference_steps', 8 if name.endswith('turbo') else 32)
        if type(threads) is not int or not 1 <= threads <= 64 or type(steps) is not int or not 1 <= steps <= 100:
            raise ModelManifestError('Select bounded integer thread and inference step counts.')


_FLUIDSYNTH_WORKER = r'''
import ctypes as c, json, math, os, pathlib, sys
job = json.loads(sys.stdin.buffer.read())
def audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo',
                 'socket.sendto', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('Local FluidSynth worker denies network and child commands')
sys.addaudithook(audit)
dll_directory = os.add_dll_directory(str(pathlib.Path(job['library']).parent)) if os.name == 'nt' else None
lib = c.CDLL(job['library'])
def function(name, result, *arguments):
    value = getattr(lib, name); value.restype = result; value.argtypes = arguments
    return value
pointer, integer, string = c.c_void_p, c.c_int, c.c_char_p
version = function('fluid_version_str', string)().decode('ascii')
if version != job['runtime_version']:
    raise RuntimeError('The FluidSynth runtime version differs from its reviewed profile')
create_settings = function('new_fluid_settings', pointer)
delete_settings = function('delete_fluid_settings', None, pointer)
setnum = function('fluid_settings_setnum', integer, pointer, string, c.c_double)
setint = function('fluid_settings_setint', integer, pointer, string, integer)
setstr = function('fluid_settings_setstr', integer, pointer, string, string)
create_synth = function('new_fluid_synth', pointer, pointer)
delete_synth = function('delete_fluid_synth', None, pointer)
sfload = function('fluid_synth_sfload', integer, pointer, string, integer)
create_player = function('new_fluid_player', pointer, pointer)
delete_player = function('delete_fluid_player', None, pointer)
add = function('fluid_player_add', integer, pointer, string)
play = function('fluid_player_play', integer, pointer)
stop = function('fluid_player_stop', integer, pointer)
join = function('fluid_player_join', integer, pointer)
create_renderer = function('new_fluid_file_renderer', pointer, pointer)
delete_renderer = function('delete_fluid_file_renderer', None, pointer)
process_block = function('fluid_file_renderer_process_block', integer, pointer)
settings = create_settings()
synth = player = renderer = None
def checked(status):
    if status != 0:
        raise RuntimeError('FluidSynth rejected a fixed rendering operation')
try:
    if not settings:
        raise RuntimeError('FluidSynth settings allocation failed')
    for key, value in [('synth.sample-rate', job['sample_rate']), ('synth.gain', .5)]:
        checked(setnum(settings, key.encode(), value))
    for key, value in [('synth.reverb.active', 0), ('synth.chorus.active', 0),
                       ('synth.cpu-cores', 1), ('audio.period-size', 64)]:
        checked(setint(settings, key.encode(), value))
    for key, value in [('player.timing-source', 'sample'), ('audio.file.name', job['output']),
                       ('audio.file.type', 'wav'), ('audio.file.format', 's16')]:
        checked(setstr(settings, key.encode(), value.encode('utf-8')))
    synth = create_synth(settings)
    if not synth or sfload(synth, job['soundfont'].encode('utf-8'), 1) < 0:
        raise RuntimeError('FluidSynth could not load the reviewed SoundFont')
    player = create_player(synth)
    if not player:
        raise RuntimeError('FluidSynth player allocation failed')
    checked(add(player, job['midi'].encode('utf-8')))
    checked(play(player))
    renderer = create_renderer(synth)
    if not renderer:
        raise RuntimeError('FluidSynth file renderer allocation failed')
    # The public sample-clock file renderer advances MIDI without a live device.
    # Stop at the explicitly selected frame budget instead of an unbounded tail.
    for block in range(math.ceil(job['frames'] / 64)):
        checked(process_block(renderer))
finally:
    if player:
        stop(player); join(player)
    if renderer: delete_renderer(renderer)
    if player: delete_player(player)
    if synth: delete_synth(synth)
    if settings: delete_settings(settings)
    if dll_directory: dll_directory.close()
print(json.dumps({'engine': 'fluidsynth', 'runtime_version': version}))
'''


def _convert_music_chunk(raw, output, *, frame_count, sample_rate, gain, ffmpeg, cancelled,
                         skip_frames=0):
    from .ffmpeg import wav_info
    info = wav_info(raw)
    if info.seconds + .05 < (skip_frames + frame_count) / sample_rate:
        raise ValueError('The local music engine returned a truncated chunk.')
    filters = (f'aresample={sample_rate},atrim=start_sample={skip_frames},asetpts=PTS-STARTPTS,'
               f'volume={gain:.8f},apad=whole_len={frame_count},atrim=end_sample={frame_count}')
    _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-protocol_whitelist', 'file',
          '-f', 'wav', '-i', raw, '-af', filters, '-ar', str(sample_rate), '-ac', '1',
          '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', output],
         cancelled=cancelled, timeout=120, env=offline_environment())


def _stage_budget(stage, cancelled, *, maximum=256 * 1024**2):
    """A worker's generated files and diagnostics share an explicit disk bound."""
    if cancelled():
        return True
    total = 0
    for count, (path, info) in enumerate(_local_inventory(stage), 1):
        if cancelled():
            return True
        if count > 4096:
            raise ValueError('The music worker exceeded its generated file budget.')
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
        if total > maximum:
            raise ValueError('The music worker exceeded its generated byte budget.')
    return False


def _render_fluidsynth_chunk(path, *, first_bar, start_frame, frame_count, sample_rate,
                             tempo, mood, seed, gain, ffmpeg, manifest, cancelled, key=None):
    options = manifest.settings
    # Recreate one preceding bar so release envelopes survive ordinary joins.
    score_first = max(0, first_bar - 1)
    score_start = round(score_first * sample_rate * 240 / tempo)
    preroll = start_frame - score_start
    with tempfile.TemporaryDirectory(prefix='.fluidsynth-', dir=Path(path).parent) as temporary:
        stage = Path(temporary)
        midi, raw = stage / 'score.mid', stage / 'raw.wav'
        _write_midi(midi, frames=frame_count + preroll, sample_rate=sample_rate, mood=mood,
                    tempo=tempo, seed=seed, first_bar=score_first, start_frame=score_start, cancelled=cancelled, key=key)
        check = lambda: _stage_budget(stage, cancelled)
        soundfont = manifest.asset(options['soundfont'])
        if options.get('backend', 'cli') == 'cli':
            executable = _binary(manifest.asset(options['executable']))
            report = _run([executable, '--version'], timeout=15, cancelled=check,
                          env=offline_environment()).decode('utf-8', 'replace')
            if not re.search(r'\b' + re.escape(options['runtime_version']) + r'\b', report):
                raise ModelManifestError('The FluidSynth CLI version differs from its reviewed profile.')
            _run([executable, '-n', '-i', '-q', '-R', '0', '-C', '0', '-g', '0.5',
                  '-o', 'synth.cpu-cores=1', '-r', str(sample_rate), '-F', raw,
                  '-T', 'wav', '-O', 's16', soundfont, midi],
                 timeout=options.get('timeout_seconds', 3600), cancelled=check,
                 env=offline_environment(), cwd=stage)
        else:
            request = {'library': str(manifest.asset(options['library'])),
                       'runtime_version': options['runtime_version'], 'soundfont': str(soundfont),
                       'midi': str(midi), 'output': str(raw), 'frames': frame_count + preroll,
                       'sample_rate': sample_rate}
            # The running application's interpreter supplies only stdlib ctypes.
            result = _run([sys.executable, '-I', '-S', '-B', '-c', _FLUIDSYNTH_WORKER],
                data=json.dumps(request, ensure_ascii=True).encode('utf-8'),
                timeout=options.get('timeout_seconds', 3600), cancelled=check,
                env=offline_environment(), cwd=stage)
            if json.loads(result) != {'engine': 'fluidsynth', 'runtime_version': options['runtime_version']}:
                raise ValueError('Invalid FluidSynth worker receipt.')
        check()
        _convert_music_chunk(raw, path, frame_count=frame_count, sample_rate=sample_rate,
                             gain=gain, ffmpeg=ffmpeg, cancelled=cancelled, skip_frames=preroll)


_ACESTEP_WORKER = r'''
import contextlib, json, os, pathlib, shutil, sys, wave
job = json.loads(sys.stdin.buffer.read())
stage = pathlib.Path(job['stage']).resolve()
def audit(event, args):
    if event in {'socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo',
                 'socket.sendto', 'urllib.Request', 'subprocess.Popen', 'os.system', 'os.posix_spawn'}:
        raise PermissionError('Local ACE-Step worker denies network and child commands')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = ((isinstance(mode, str) and any(c in mode for c in 'wax+')) or
                   (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
        if writing and not pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
            raise PermissionError('ACE-Step worker may only write its staging directory')
sys.addaudithook(audit)
# -S disabled startup .pth and sitecustomize execution. Only this completely
# inventoried package directory is added; no user-site or project import path.
sys.path.insert(0, job['package_dir'])
with contextlib.redirect_stdout(sys.stderr):
    import torch
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationParams, GenerationConfig, generate_music
    from acestep.model_downloader import check_model_exists, _check_code_mismatch
    checkpoints = pathlib.Path(job['root']) / 'checkpoints'
    required = (job['model_name'], 'vae', 'Qwen3-Embedding-0.6B')
    if not all(check_model_exists(name, checkpoints) for name in required):
        raise RuntimeError('The verified offline ACE-Step installation is incomplete')
    if _check_code_mismatch(job['model_name'], checkpoints):
        raise RuntimeError('ACE-Step checkpoint code differs from the reviewed installed code')
    torch.manual_seed(job['seed']); torch.set_num_threads(job['threads'])
    class OfflineAceStepHandler(AceStepHandler):
        # The exact reviewed initializer ordinarily downloads absent components
        # (including an unused LM) and rewrites mismatched checkpoint code.
        # Its two verified extension hooks enforce read-only local assets here.
        def _ensure_models_present(self, *, checkpoint_path, config_path, prefer_source, vae_variant=None):
            if not all(check_model_exists(name, checkpoint_path) for name in required):
                return 'Offline ACE-Step assets are incomplete', False
            return None
        @staticmethod
        def _sync_model_code_if_needed(config_path, checkpoint_path):
            if _check_code_mismatch(config_path, checkpoint_path):
                raise RuntimeError('Checkpoint code differs; installation must be reviewed again')
    handler = OfflineAceStepHandler()
    handler._progress_estimates_path = str(stage / 'progress-estimates.json')
    status, ready = handler.initialize_service(project_root=job['root'], config_path=job['model_name'],
        device=job['device'], compile_model=False, use_mlx_dit=False, vae_checkpoint='official')
    if not ready:
        raise RuntimeError('ACE-Step initialization did not succeed')
    params = GenerationParams(caption=job['prompt'], lyrics='[Instrumental]', instrumental=True,
        bpm=round(job['tempo']), keyscale=job['keyscale'], timesignature='4',
        duration=job['duration'], seed=job['seed'], inference_steps=job['inference_steps'],
        thinking=False, use_cot_metas=False, use_cot_caption=False, use_cot_language=False,
        enable_normalization=False)
    config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[job['seed']], audio_format='wav')
    result = generate_music(handler, None, params, config, save_dir=str(stage))
    if result.success is not True or len(result.audios) != 1:
        raise RuntimeError('ACE-Step did not return one successful audio result')
    raw_path = pathlib.Path(result.audios[0]['path'])
    if raw_path.is_symlink() or not raw_path.is_file():
        raise RuntimeError('ACE-Step returned an invalid output file')
    candidate = raw_path.resolve()
    if not candidate.is_relative_to(stage):
        raise RuntimeError('ACE-Step returned an output outside staging')
    for ancestor in (raw_path, *raw_path.parents):
        if ancestor.is_symlink() or getattr(ancestor.lstat(), 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('ACE-Step output uses a redirected path')
    if candidate.stat().st_size > 128 * 1024**2:
        raise RuntimeError('ACE-Step output exceeds the selected chunk budget')
    with wave.open(str(candidate), 'rb') as source:
        if source.getsampwidth() != 2 or source.getnchannels() not in (1, 2):
            raise RuntimeError('ACE-Step WAV output must be PCM16 mono or stereo')
        receipt = {'engine': 'ace-step', 'frames': source.getnframes(),
                   'sample_rate': source.getframerate(), 'channels': source.getnchannels()}
    shutil.copyfile(candidate, job['output'])
print(json.dumps(receipt))
'''


def _render_acestep_chunk(path, *, frame_count, sample_rate, mood, tempo, seed, gain,
                         ffmpeg, manifest, cancelled, key=None):
    options = manifest.settings
    with tempfile.TemporaryDirectory(prefix='.ace-step-', dir=Path(path).parent) as temporary:
        stage = Path(temporary)
        raw = stage / 'selected.wav'
        request = {'root': str(manifest.root), 'stage': str(stage), 'output': str(raw),
                   'package_dir': str(manifest.directory(options['package_dir'])),
                   'model_name': options['model_name'], 'device': options.get('device', 'cpu'),
                   'duration': max(10, frame_count / sample_rate), 'seed': seed % (2**31),
                   'threads': options.get('threads', 2), 'tempo': tempo,
                   'inference_steps': options.get('inference_steps', 8 if options['model_name'].endswith('turbo') else 32),
                   'keyscale': _keyscale(key, mood),
                   'prompt': f'Original instrumental music, no vocals, {mood} mood, {tempo:g} beats per minute.'}
        environment = offline_environment()
        # Caller environment cannot select another model, backend or remote cache.
        environment = {k: v for k, v in environment.items() if not k.upper().startswith('ACESTEP_')}
        environment.update(ACESTEP_CHECKPOINTS_DIR=str(manifest.root / 'checkpoints'),
            ACESTEP_DISABLE_TQDM='1', HF_HOME=str(stage / 'hf-cache'), TORCH_HOME=str(stage / 'torch-cache'),
            TRANSFORMERS_CACHE=str(stage / 'transformers-cache'), TMPDIR=str(stage), TEMP=str(stage), TMP=str(stage))
        check = lambda: _stage_budget(stage, cancelled)
        result = _run([_binary(manifest.asset(options['python'])), '-I', '-S', '-B', '-u', '-c', _ACESTEP_WORKER],
                      data=json.dumps(request, ensure_ascii=True).encode('utf-8'),
                      timeout=options.get('timeout_seconds', 3600), cancelled=check, env=environment, cwd=stage)
        check()
        receipt = json.loads(result)
        from .ffmpeg import wav_info
        info = wav_info(raw)
        if receipt != {'engine': 'ace-step', 'frames': info.frames,
                       'sample_rate': info.rate, 'channels': info.channels}:
            raise ValueError('ACE-Step worker receipt does not match its actual PCM output.')
        _convert_music_chunk(raw, path, frame_count=frame_count, sample_rate=sample_rate,
                             gain=gain, ffmpeg=ffmpeg, cancelled=cancelled)


def music_capabilities():
    return (
        {'engine': 'procedural', 'implemented': True, 'requires': 'FFmpeg sine/amix filters',
         'kind': 'deterministic original note composition and basic synthesized waveform, with MIDI'},
        {'engine': 'musicgen', 'implemented': True, 'requires': 'explicit verified offline AudioCraft runtime and weights',
         'kind': 'optional neural continuation; no weights or runtime installed automatically',
         'acceptance': 'Real model inference must be verified on the selected machine.'},
        {'engine': 'ace-step', 'implemented': True, 'requires': 'explicit verified offline ACE-Step 1.5 runtime and checkpoints',
         'kind': 'optional neural instrumental generation in bounded chunks; no automatic downloads',
         'acceptance': 'Actual inference, hardware performance and listening must be verified on the selected machine.'},
        {'engine': 'fluidsynth', 'implemented': True, 'requires': 'reviewed FluidSynth 2.x CLI or shared library and licensed SoundFont',
         'kind': 'original MIDI notes rendered through the selected SoundFont; not neural generation',
         'acceptance': 'SoundFont-specific rights, native runtime and listening require explicit review.'},
    )


def _render_music(output, *, duration, mood='calm', tempo=90, seed=0, engine='procedural', ffmpeg,
                 gain=.18, sample_rate=48000, manifest=None, cancelled=lambda: False,
                 on_status=lambda message: None, max_output_bytes=16 * 1024**3, key=None):
    """Compose/render/resume a local cue. Refuse overwrite, silent fallback and egress.

    Checkpoints use stable bar/chunk identities. A changed score needs a new
    destination. Per-chunk limits are worker budgets, not a ten-minute project
    limit. The final WAV automatically switches to RF64 when required.
    """
    frames, tempo = _settings(duration, mood, tempo, seed, gain, sample_rate)
    music_key(key)
    if type(max_output_bytes) is not int or max_output_bytes < 1 or frames * 2 > max_output_bytes:
        raise ValueError('Music exceeds the selected output disk budget; raise it explicitly or split the project.')
    if engine not in ('procedural', 'musicgen', 'fluidsynth', 'ace-step'):
        raise EngineUnavailable('This local music engine is not implemented; select a supported engine explicitly.')
    binary, target = _binary(ffmpeg), _output(output)
    if cancelled():
        raise InterruptedError('Music generation cancelled.')
    model = None
    if engine != 'procedural':
        if manifest is None:
            raise EngineUnavailable(f'{engine} needs an explicitly installed and reviewed local asset manifest.')
        model = manifest if isinstance(manifest, LocalModelManifest) else LocalModelManifest.load(manifest, engine=engine)
        if model.engine != engine:
            raise ModelManifestError('Wrong local music model engine.')
        model.verify(cancelled=cancelled, require_complete=True)
        _music_profile(model, engine)
    elif manifest is not None:
        raise ValueError('Procedural music does not use an installed asset manifest.')
    settings = {'version': COMPOSER_VERSION, 'engine': engine, 'frames': frames, 'sample_rate': sample_rate,
                'mood': mood, 'tempo': tempo, 'seed': seed, 'gain': gain,
                'ffmpeg_sha256': digest_file(binary),
                'build_sha256': hashlib.sha256(_run([binary, '-version'], timeout=15)).hexdigest(),
                'model': model.cache_identity() if model else None}
    if key is not None:
        settings['key'] = key
    settings_hash = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':'),
                                              allow_nan=False).encode('utf-8')).hexdigest()
    work = local_path(target.with_name(target.stem + '.music-work'), must_exist=False)
    work.mkdir(exist_ok=True)
    state_path = work / 'state.json'
    if state_path.exists():
        state = read_json(state_path, max_bytes=16 * 1024 * 1024)
        if not isinstance(state, dict) or state.get('settings_sha256') != settings_hash or not isinstance(state.get('chunks'), dict):
            raise ValueError('Existing music checkpoints belong to another score; choose a new output file.')
    else:
        state = {'schema_version': 1, 'settings_sha256': settings_hash, 'settings': settings, 'chunks': {}}
        _atomic_json(state_path, state)
    bytes_needed = frames * 4 + 8 * 1024 * 1024  # chunks plus final PCM and small metadata
    if shutil.disk_usage(target.parent).free < bytes_needed:
        raise OSError('Insufficient free disk for music chunks and the final output.')
    if engine in ('procedural', 'fluidsynth'):
        chunk_size = sample_rate * 16 * 60 / tempo  # four bars; globally rounded boundaries
    elif engine == 'ace-step':
        chunk_size = sample_rate * model.settings.get('chunk_seconds', 30)
    else:
        chunk_size = sample_rate * 25  # five seconds of context fits a thirty-second model window
    chunk_count = math.ceil(frames / chunk_size)
    previous = None
    previous_digest = None
    reused = 0
    for number in range(chunk_count):
        if cancelled():
            raise InterruptedError('Music generation cancelled; accepted chunks are saved for resume.')
        start = round(number * chunk_size)
        stop = min(frames, round((number + 1) * chunk_size))
        count = stop - start
        name = f'part-{number:08d}.wav'
        chunk = local_path(work / name, must_exist=False)
        record = state['chunks'].get(str(number))
        valid = False
        expected_parent = previous_digest if engine == 'musicgen' else None
        if (chunk.is_file() and isinstance(record, dict) and record.get('frames') == count
                and record.get('parent_sha256') == expected_parent):
            try:
                info = _wave_frames(chunk)
                valid = (info['frames'] == count and info['sample_rate'] == sample_rate and
                         digest_file(chunk, cancelled=cancelled) == record.get('sha256'))
            except (OSError, ValueError, wave.Error):
                valid = False
        if valid:
            reused += 1
        else:
            # Only the deterministic generated cache filename is replaced. Source
            # audio, other files and any linked paths are never removed.
            chunk.unlink(missing_ok=True)
            partial = work / f'part-{number:08d}.partial.wav'
            local_path(partial, must_exist=False)
            partial.unlink(missing_ok=True)
            try:
                if engine == 'procedural':
                    _render_procedural_chunk(partial, first_bar=number * 4, start_frame=start,
                        frame_count=count, total_frames=frames, sample_rate=sample_rate, tempo=tempo,
                        mood=mood, seed=seed, gain=gain, ffmpeg=binary, cancelled=cancelled, key=key)
                elif engine == 'fluidsynth':
                    _render_fluidsynth_chunk(partial, first_bar=number * 4, start_frame=start,
                        frame_count=count, sample_rate=sample_rate, tempo=tempo, mood=mood, seed=seed,
                        gain=gain, ffmpeg=binary, manifest=model, cancelled=cancelled, key=key)
                elif engine == 'ace-step':
                    _render_acestep_chunk(partial, frame_count=count, sample_rate=sample_rate,
                        tempo=tempo, mood=mood, seed=(seed + number) % (2**63), gain=gain,
                        ffmpeg=binary, manifest=model, cancelled=cancelled, key=key)
                else:
                    _render_musicgen_chunk(partial, frame_count=count, sample_rate=sample_rate,
                        mood=mood, tempo=tempo, seed=(seed + number) % (2**63), gain=gain, ffmpeg=binary,
                        manifest=model, previous=previous, cancelled=cancelled, key=key)
                info = _wave_frames(partial)
                if info['frames'] != count or info['sample_rate'] != sample_rate:
                    raise ValueError('Music chunk duration does not match its sample-accurate plan.')
                digest = digest_file(partial, cancelled=cancelled)
                _publish(partial, chunk)
                state['chunks'][str(number)] = {'frames': count, 'sha256': digest,
                                                'parent_sha256': expected_parent}
                _atomic_json(state_path, state)
            finally:
                partial.unlink(missing_ok=True)
        previous = chunk
        previous_digest = state['chunks'][str(number)]['sha256']
        on_status(f'Music chunk {number + 1} of {chunk_count} verified; {stop/sample_rate:.2f} seconds ready.')
    # The concat file contains only fixed internal names, never text supplied by
    # a scene or document. safe=1 and file-only protocols block network inputs.
    playlist = work / 'segments.txt'
    playlist.write_text(''.join(f"file 'part-{n:08d}.wav'\n" for n in range(chunk_count)), encoding='ascii')
    assembled = work / 'assembled.partial.wav'
    local_path(assembled, must_exist=False)
    assembled.unlink(missing_ok=True)
    try:
        _run([binary, '-hide_banner', '-loglevel', 'error', '-nostdin', '-protocol_whitelist', 'file',
              '-f', 'concat', '-safe', '1', '-i', playlist, '-map', '0:a:0', '-c:a', 'copy',
              '-rf64', 'auto', '-f', 'wav', '-n', assembled],
             timeout=max(300, frames / sample_rate), cancelled=cancelled, env=offline_environment())
        info = _wave_frames(assembled)
        if info['frames'] != frames or info['sample_rate'] != sample_rate:
            raise ValueError('Final music duration does not match the requested sample count.')
        midi = None
        reuse_midi = False
        if engine in ('procedural', 'fluidsynth'):
            midi = local_path(target.with_suffix('.mid'), must_exist=False)
            midi_tmp = work / 'score.partial.mid'
            local_path(midi_tmp, must_exist=False)
            midi_tmp.unlink(missing_ok=True)
            _write_midi(midi_tmp, frames=frames, sample_rate=sample_rate, mood=mood, tempo=tempo,
                        seed=seed, cancelled=cancelled, key=key)
            if midi.exists():
                if not midi.is_file() or digest_file(midi) != digest_file(midi_tmp):
                    raise FileExistsError(midi)
                reuse_midi = True
        kinds = {'procedural': 'procedural original notes / FFmpeg sine synthesis',
                 'fluidsynth': 'procedural original MIDI notes / reviewed FluidSynth SoundFont rendering',
                 'musicgen': 'MusicGen neural continuation', 'ace-step': 'ACE-Step 1.5 neural instrumental generation'}
        assemblies = {'procedural': 'new per-chunk synthesis with recurring compositional motifs',
                      'fluidsynth': 'four-bar MIDI windows with one preceding bar of instrument release context',
                      'musicgen': '25-second generations with five-second audio context; no full-project waveform in RAM',
                      'ace-step': 'independent bounded instrumental chunks; shared prompt and sequential seeds; seamless neural continuity unaccepted'}
        provenance = {'schema_version': 1, 'engine': engine, 'settings_sha256': settings_hash,
                      'frames': frames, 'sample_rate': sample_rate, 'channels': 1,
                      'sha256': digest_file(assembled, cancelled=cancelled), 'chunks': chunk_count,
                      'reused_chunks': reused, 'mood': mood, 'tempo': tempo, 'seed': seed,
                      'kind': kinds[engine], 'assembly': assemblies[engine],
                      'model': model.cache_identity() if model else None,
                      'listening_acceptance': 'NOT_RUN', 'rights': model.license if model else 'Original procedural score; no sampled recording or bundled SoundFont'}
        if key is not None:
            provenance.update(key=key, keyscale=_keyscale(key, mood))
        if engine in ('fluidsynth', 'ace-step'):
            provenance['output_rights_notice'] = model.settings['output_rights_notice']
        metadata = local_path(target.with_suffix('.music.json'), must_exist=False)
        reuse_metadata = False
        if metadata.exists():
            try:
                previous_metadata = read_json(metadata)
                comparable = {key: value for key, value in provenance.items() if key != 'reused_chunks'}
                previous_comparable = {key: value for key, value in previous_metadata.items() if key != 'reused_chunks'}
                if previous_comparable != comparable:
                    raise ValueError('Different provenance')
            except (ValueError, OSError, AttributeError) as exc:
                raise FileExistsError(metadata) from exc
            reuse_metadata = True
        if cancelled():
            raise InterruptedError('Music generation cancelled; verified chunks are saved.')
        # Publish companions first and WAV last: the WAV is the completion marker.
        published = []
        try:
            if midi is not None and not reuse_midi:
                os.link(midi_tmp, midi)
                published.append(midi)
            metadata_tmp = work / 'provenance.partial.json'
            _atomic_json(metadata_tmp, provenance)
            if not reuse_metadata:
                os.link(metadata_tmp, metadata)
                published.append(metadata)
            _publish(assembled, target)
        except BaseException:
            for companion in published:
                companion.unlink(missing_ok=True)
            raise
        return {'path': target, 'provenance': provenance, 'frames': frames,
                'sample_rate': sample_rate, 'midi': midi, 'manifest': metadata}
    finally:
        assembled.unlink(missing_ok=True)


def render_music(output, *, duration, mood='calm', tempo=90, seed=0, engine='procedural', ffmpeg,
                 gain=.18, sample_rate=48000, manifest=None, cancelled=lambda: False,
                 on_status=lambda message: None, max_output_bytes=16 * 1024**3, key=None):
    """Serialize writes to one cue while allowing independent cues to render."""
    from filelock import FileLock, Timeout
    target = _output(output)
    lock_path = local_path(target.with_name(target.name + '.music.lock'), must_exist=False)
    lock = FileLock(lock_path)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError('This music cue is already rendering in another worker.') from exc
    try:
        return _render_music(target, duration=duration, mood=mood, tempo=tempo, seed=seed,
                             engine=engine, ffmpeg=ffmpeg, gain=gain, sample_rate=sample_rate,
                             manifest=manifest, cancelled=cancelled, on_status=on_status,
                             max_output_bytes=max_output_bytes, key=key)
    finally:
        lock.release()


def generate_music(output, **kwargs):
    """Convenient Path-returning adapter for a production cue."""
    return render_music(output, **kwargs)['path']
