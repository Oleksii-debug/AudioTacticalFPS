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
import shutil
import struct
import sys
import tempfile
import wave

from .engines import EngineUnavailable, _binary, _number, _output, _publish, _run
from .model_manifest import LocalModelManifest, ModelManifestError, digest_file, local_path, offline_environment, read_json


COMPOSER_VERSION = 'procedural-score-1'
MOODS = ('calm', 'suspense', 'bright', 'sad', 'neutral', 'energetic', 'dark')
MUSICGEN_SOURCE = '896ec7c47f5e5d1e5aa1e4b260c4405328bf009d'


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


def _notes_for_bars(first_bar, bar_count, *, sample_rate, tempo, mood, seed, total_frames):
    """Compose a bounded group of bars with stable per-bar random decisions."""
    minor = mood in ('suspense', 'sad', 'dark')
    scale = (0, 2, 3, 5, 7, 8, 10) if minor else (0, 2, 4, 5, 7, 9, 11)
    root = 57 if minor else 60
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


def _write_midi(path, *, frames, sample_rate, mood, tempo, seed, cancelled):
    """Write SMF type 0 incrementally; no full-project event list in RAM."""
    ticks = 480
    micros = round(60_000_000 / tempo)
    bar_count = math.ceil(frames / (sample_rate * 240 / tempo))
    last_tick = 0
    with Path(path).open('xb') as stream:
        stream.write(b'MThd' + struct.pack('>IHHH', 6, 0, 1, ticks))
        stream.write(b'MTrk\x00\x00\x00\x00')
        stream.write(b'\x00\xff\x51\x03' + micros.to_bytes(3, 'big'))
        stream.write(b'\x00\xc0\x04\x00\xc1\x58')
        for first in range(0, bar_count, 4):
            if cancelled():
                raise InterruptedError('Music generation cancelled.')
            notes = _notes_for_bars(first, min(4, bar_count-first), sample_rate=sample_rate,
                                    tempo=tempo, mood=mood, seed=seed, total_frames=frames)
            events = []
            for note in notes:
                begin = round(note.start / sample_rate * tempo / 60 * ticks)
                end = max(begin + 1, round(note.end / sample_rate * tempo / 60 * ticks))
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
    """Read PCM RIFF/RF64 metadata without loading samples (including >4 GiB)."""
    with Path(path).open('rb') as source:
        header = source.read(12)
        if len(header) != 12 or header[:4] not in (b'RIFF', b'RF64') or header[8:] != b'WAVE':
            raise ValueError('Music output is not a RIFF/RF64 WAV file.')
        frames64, data64, format_info = None, None, None
        size_on_disk = Path(path).stat().st_size
        for _ in range(64):
            tag = source.read(8)
            if len(tag) != 8:
                raise ValueError('Music output has no complete PCM data chunk.')
            kind, count = struct.unpack('<4sI', tag)
            if kind == b'ds64':
                if not 28 <= count <= 4096:
                    raise ValueError('Invalid RF64 size metadata.')
                chunk = source.read(count)
                if len(chunk) != count:
                    raise ValueError('Truncated RF64 metadata.')
                _, data64, frames64, _ = struct.unpack('<QQQI', chunk[:28])
            elif kind == b'fmt ':
                if not 16 <= count <= 4096:
                    raise ValueError('Invalid WAV audio format.')
                chunk = source.read(count)
                if len(chunk) != count:
                    raise ValueError('Truncated WAV format.')
                encoding, channels, rate, _, block_align, bits = struct.unpack('<HHIIHH', chunk[:16])
                if encoding == 65534 and len(chunk) >= 40:
                    encoding = struct.unpack('<I', chunk[24:28])[0]
                if encoding != 1 or channels != 1 or bits != 16 or block_align != 2:
                    raise ValueError('Expected mono PCM16 music output.')
                format_info = (channels, rate)
            elif kind == b'data':
                length = data64 if count == 0xffffffff else count
                if format_info is None or length is None or length % 2 or source.tell() + length > size_on_disk:
                    raise ValueError('Music output is truncated or has inconsistent sample alignment.')
                frames = length // 2
                if frames64 not in (None, 0, frames):
                    raise ValueError('RF64 duration metadata disagrees with the data length.')
                return {'frames': frames, 'sample_rate': format_info[1], 'channels': 1, 'sample_width': 2}
            else:
                if count > size_on_disk or source.tell() + count > size_on_disk:
                    raise ValueError('Invalid WAV chunk size.')
                source.seek(count, 1)
            if count % 2:
                source.seek(1, 1)
        raise ValueError('Too many WAV metadata chunks.')


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
                             tempo, mood, seed, gain, ffmpeg, cancelled):
    notes = _notes_for_bars(first_bar, 4, sample_rate=sample_rate, tempo=tempo,
                            mood=mood, seed=seed, total_frames=total_frames)
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
    # The pinned FFmpeg 9 build removed -filter_complex_script. Each score
    # chunk contains at most four bars, so its numeric graph stays small.
    # Pass that bounded graph directly through the stable -filter_complex
    # option, also supported by older reviewed FFmpeg runtimes. No shell or
    # version-dependent retry is involved and the whole score is never inline.
    graph_text = ';'.join(graph)
    if not graph_text.isascii() or len(graph_text) > 16 * 1024:
        raise ValueError('The procedural music chunk exceeds its filter graph budget.')
    _run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-filter_complex_threads', '1',
          '-filter_complex', graph_text, '-map', '[music]', '-ar', str(sample_rate), '-ac', '1',
          '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', path],
         timeout=max(120, frame_count / sample_rate * 10), cancelled=cancelled,
         env=offline_environment())


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
                          manifest, previous, cancelled):
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
    local_path(cache)
    for file in cache.rglob('*'):
        if file.is_file() and file.relative_to(manifest.root).as_posix() not in dict(manifest.files):
            raise ModelManifestError('Every text-encoder cache asset must be listed in the verified manifest.')
    environment = offline_environment()
    environment.update(HF_HOME=str(cache), HUGGINGFACE_HUB_CACHE=str(cache / 'hub'),
                       AUDIOCRAFT_CACHE_DIR=str(manifest.root), TORCH_HOME=str(manifest.root / 'torch-cache'))
    prompt = f'Original instrumental music, no vocals, {mood} mood, {tempo:g} beats per minute.'
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


def music_capabilities():
    return (
        {'engine': 'procedural', 'implemented': True, 'requires': 'FFmpeg sine/amix filters',
         'kind': 'deterministic original note composition and basic synthesized waveform, with MIDI'},
        {'engine': 'musicgen', 'implemented': True, 'requires': 'explicit verified offline AudioCraft runtime and weights',
         'kind': 'optional neural continuation; no weights or runtime installed automatically',
         'acceptance': 'Real model inference must be verified on the selected machine.'},
        {'engine': 'ace-step', 'implemented': False,
         'reason': 'A verified local ACE-Step runtime adapter is not yet implemented; no substitute is selected.'},
        {'engine': 'fluidsynth', 'implemented': False,
         'reason': 'MIDI export is available; rendering with a reviewed SoundFont requires a separate adapter.'},
    )


def _render_music(output, *, duration, mood='calm', tempo=90, seed=0, engine='procedural', ffmpeg,
                 gain=.18, sample_rate=48000, manifest=None, cancelled=lambda: False,
                 on_status=lambda message: None, max_output_bytes=16 * 1024**3):
    """Compose/render/resume a local cue. Refuse overwrite, silent fallback and egress.

    Checkpoints use stable bar/chunk identities. A changed score needs a new
    destination. Per-chunk limits are worker budgets, not a ten-minute project
    limit. The final WAV automatically switches to RF64 when required.
    """
    frames, tempo = _settings(duration, mood, tempo, seed, gain, sample_rate)
    if type(max_output_bytes) is not int or max_output_bytes < 1 or frames * 2 > max_output_bytes:
        raise ValueError('Music exceeds the selected output disk budget; raise it explicitly or split the project.')
    if engine not in ('procedural', 'musicgen'):
        raise EngineUnavailable('This local music engine is not implemented; select a supported engine explicitly.')
    binary, target = _binary(ffmpeg), _output(output)
    if cancelled():
        raise InterruptedError('Music generation cancelled.')
    model = None
    if engine == 'musicgen':
        if manifest is None:
            raise EngineUnavailable('MusicGen needs an explicitly installed and reviewed local model manifest.')
        model = manifest if isinstance(manifest, LocalModelManifest) else LocalModelManifest.load(manifest, engine='musicgen')
        if model.engine != 'musicgen':
            raise ModelManifestError('Wrong local music model engine.')
        model.verify(cancelled=cancelled, require_complete=True)
    elif manifest is not None:
        raise ValueError('A model manifest is only used by the selected neural music engine.')
    settings = {'version': COMPOSER_VERSION, 'engine': engine, 'frames': frames, 'sample_rate': sample_rate,
                'mood': mood, 'tempo': tempo, 'seed': seed, 'gain': gain,
                'ffmpeg_sha256': digest_file(binary),
                'build_sha256': hashlib.sha256(_run([binary, '-version'], timeout=15)).hexdigest(),
                'model': model.cache_identity() if model else None}
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
    if engine == 'procedural':
        chunk_size = sample_rate * 16 * 60 / tempo  # four bars; globally rounded boundaries
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
                        mood=mood, seed=seed, gain=gain, ffmpeg=binary, cancelled=cancelled)
                else:
                    _render_musicgen_chunk(partial, frame_count=count, sample_rate=sample_rate,
                        mood=mood, tempo=tempo, seed=(seed + number) % (2**63), gain=gain, ffmpeg=binary,
                        manifest=model, previous=previous, cancelled=cancelled)
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
        if engine == 'procedural':
            midi = local_path(target.with_suffix('.mid'), must_exist=False)
            midi_tmp = work / 'score.partial.mid'
            local_path(midi_tmp, must_exist=False)
            midi_tmp.unlink(missing_ok=True)
            _write_midi(midi_tmp, frames=frames, sample_rate=sample_rate, mood=mood, tempo=tempo,
                        seed=seed, cancelled=cancelled)
            if midi.exists():
                if not midi.is_file() or digest_file(midi) != digest_file(midi_tmp):
                    raise FileExistsError(midi)
                reuse_midi = True
        provenance = {'schema_version': 1, 'engine': engine, 'settings_sha256': settings_hash,
                      'frames': frames, 'sample_rate': sample_rate, 'channels': 1,
                      'sha256': digest_file(assembled, cancelled=cancelled), 'chunks': chunk_count,
                      'reused_chunks': reused, 'mood': mood, 'tempo': tempo, 'seed': seed,
                      'kind': 'procedural original notes / FFmpeg sine synthesis' if engine == 'procedural' else 'MusicGen neural continuation',
                      'assembly': 'new per-chunk synthesis with recurring compositional motifs' if engine == 'procedural'
                                  else '25-second generations with five-second audio context; no full-project waveform in RAM',
                      'model': model.cache_identity() if model else None,
                      'listening_acceptance': 'NOT_RUN', 'rights': model.license if model else 'Original procedural score; no sampled recording or bundled SoundFont'}
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
                 on_status=lambda message: None, max_output_bytes=16 * 1024**3):
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
                             max_output_bytes=max_output_bytes)
    finally:
        lock.release()


def generate_music(output, **kwargs):
    """Convenient Path-returning adapter for a production cue."""
    return render_music(output, **kwargs)['path']
