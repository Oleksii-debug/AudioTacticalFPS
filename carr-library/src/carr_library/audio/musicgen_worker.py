"""Isolated optional local music workers. No downloads, shell or remote code.

This file is executed with Python -I by the reviewed parent adapter. A worker
never publishes to the user's destination; it writes only private stage files.
"""
import ctypes
from hashlib import sha256
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import socket
import sys
import wave


def deny_network():
    """Deny Python Internet sockets before importing optional model packages."""
    original = socket.socket
    class LocalSocket(original):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise PermissionError('This music worker is offline-only.')
            return super().connect(address)
        def connect_ex(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise PermissionError('This music worker is offline-only.')
            return super().connect_ex(address)
        def sendto(self, *args, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                raise PermissionError('This music worker is offline-only.')
            return super().sendto(*args, **kwargs)
    def denied(*args, **kwargs):
        raise PermissionError('This music worker is offline-only.')
    socket.socket = LocalSocket
    socket.create_connection = denied
    socket.getaddrinfo = denied
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                      HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                      DO_NOT_TRACK='1', TOKENIZERS_PARALLELISM='false')


def digest(path):
    h = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _musicgen(request):
    manifest = request['manifest']
    for distribution, key in (('transformers', 'transformers_version'), ('torch', 'torch_version')):
        if importlib.metadata.version(distribution) != manifest[key]:
            raise ValueError(f'The installed {distribution} version does not match the approved model environment.')
    root = Path(request['model_dir'])
    for entry in manifest['files']:
        if digest(root / entry['path']) != entry['sha256']:
            raise ValueError('The local model changed before worker initialization.')
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration
    spec = request['spec']
    device = request['device']
    if device.startswith('cuda') and not torch.cuda.is_available():
        raise RuntimeError('The selected CUDA device is unavailable; no implicit hardware fallback.')
    torch.manual_seed(spec['seed'])
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    processor = AutoProcessor.from_pretrained(str(root), local_files_only=True, trust_remote_code=False)
    model = MusicgenForConditionalGeneration.from_pretrained(
        str(root), local_files_only=True, trust_remote_code=False, use_safetensors=True)
    model.to(device)
    model.eval()
    text = spec['prompt'] or f"Instrumental {spec['mood']} music, {spec['tempo_bpm']} BPM, {spec['key']} key, no vocals"
    inputs = processor(text=[text], padding=True, return_tensors='pt').to(device)
    rate = int(model.config.audio_encoder.sampling_rate)
    frame_rate = float(model.config.audio_encoder.frame_rate)
    # MusicGen delays codebooks. Account for the tail tokens instead of
    # padding an incomplete model output with invented silent samples.
    delay = model.config.decoder.num_codebooks // model.config.decoder.audio_channels - 1
    tokens = math.ceil(spec['duration_seconds'] * frame_rate) + delay
    if not 1 <= tokens <= 1500:
        raise ValueError('MusicGen segment exceeds its reviewed generation context.')
    with torch.inference_mode():
        waveform = model.generate(**inputs, do_sample=True, guidance_scale=3.0,
                                  max_new_tokens=tokens)[0].detach().float().cpu()
    frames = round(spec['duration_seconds'] * rate)
    if waveform.ndim != 2 or waveform.shape[0] not in (1, 2) or waveform.shape[1] < frames:
        raise RuntimeError('MusicGen returned an incomplete or unsupported waveform.')
    waveform = waveform[:, :frames]
    if not torch.isfinite(waveform).all():
        raise RuntimeError('MusicGen returned non-finite audio samples.')
    peak = float(waveform.abs().max())
    attenuation = min(1., .95 / peak) if peak else 1.
    waveform *= attenuation
    pcm = (waveform.transpose(0, 1) * 32767).round().to(torch.int16).numpy()
    if sys.byteorder != 'little':
        pcm = pcm.byteswap()
    output = Path(request['output'])
    with output.open('xb') as stream:
        with wave.open(stream, 'wb') as audio:
            audio.setparams((int(waveform.shape[0]), 2, rate, 0, 'NONE', 'not compressed'))
            for start in range(0, frames, 4096):
                audio.writeframesraw(pcm[start:start+4096].tobytes())
        stream.flush()
        os.fsync(stream.fileno())
    return dict(engine='musicgen', status='complete', frames=frames, sample_rate=rate,
                channels=int(waveform.shape[0]), gain_to_avoid_clipping=attenuation,
                device=device, seed=spec['seed'], model_id=manifest['model_id'])


def _fluidsynth(request):
    sf2 = Path(request['soundfont'])
    if digest(sf2) != request['soundfont_sha256']:
        raise ValueError('SoundFont identity changed before rendering.')
    lib = ctypes.CDLL(request['library'])
    def function(name, result, *args):
        item = getattr(lib, name)
        item.restype, item.argtypes = result, list(args)
        return item
    ptr, i, c, d = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_double
    new_settings = function('new_fluid_settings', ptr)
    del_settings = function('delete_fluid_settings', None, ptr)
    setting_num = function('fluid_settings_setnum', i, ptr, c, d)
    setting_int = function('fluid_settings_setint', i, ptr, c, i)
    new_synth = function('new_fluid_synth', ptr, ptr)
    del_synth = function('delete_fluid_synth', None, ptr)
    sfload = function('fluid_synth_sfload', i, ptr, c, i)
    program = function('fluid_synth_program_select', i, ptr, i, i, i, i)
    note_on = function('fluid_synth_noteon', i, ptr, i, i, i)
    all_off = function('fluid_synth_all_notes_off', i, ptr, i)
    render = function('fluid_synth_write_s16', i, ptr, i, ptr, i, i, ptr, i, i)
    version = function('fluid_version_str', c)
    rate, spec = request['sample_rate'], request['spec']
    settings, synth = new_settings(), None
    if not settings:
        raise RuntimeError('Could not initialize FluidSynth settings.')
    try:
        for key, value in ((b'synth.sample-rate', rate), (b'synth.gain', .35)):
            if setting_num(settings, key, value) != 0:
                raise RuntimeError('FluidSynth rejected a numeric setting.')
        for key, value in ((b'synth.reverb.active', 0), (b'synth.chorus.active', 0),
                           (b'synth.cpu-cores', 1), (b'synth.threadsafe-api', 0)):
            if setting_int(settings, key, value) != 0:
                raise RuntimeError('FluidSynth rejected a runtime setting.')
        synth = new_synth(settings)
        if not synth:
            raise RuntimeError('Could not initialize FluidSynth.')
        identifier = sfload(synth, os.fsencode(sf2), 1)
        if identifier < 0:
            raise RuntimeError('The selected SoundFont could not be loaded.')
        for channel, preset in ((0, 0), (1, 48), (2, 32)):
            if program(synth, channel, identifier, 0, preset) != 0:
                raise RuntimeError('SoundFont does not provide the required GM instruments.')
        key = {'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4,
               'F': 5, 'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8, 'A': 9,
               'A#': 10, 'Bb': 10, 'B': 11}[spec['key']]
        root = 48 + key
        minor = spec['mood'] in ('suspense', 'dramatic', 'sad')
        scale = (0, 2, 3, 5, 7, 8, 10) if minor else (0, 2, 4, 5, 7, 9, 11)
        randomizer = random.Random(spec['seed'])
        beat = 60 / spec['tempo_bpm']
        total = round(spec['duration_seconds'] * rate)
        position, beat_index = 0, 0
        buffer = (ctypes.c_int16 * (2048 * 2))()
        with Path(request['output']).open('xb') as stream:
            while position < total:
                for channel in range(3):
                    all_off(synth, channel)
                chord = (0, 5, 3 if minor else 4, 7)[(beat_index // 4) % 4]
                notes = ((0, root+12+scale[randomizer.randrange(len(scale))], 62),
                         (1, root+chord, 32), (1, root+chord+(3 if minor else 4), 28),
                         (1, root+chord+7, 25), (2, root+chord-12, 45))
                for channel, pitch, velocity in notes:
                    if note_on(synth, channel, pitch, velocity) != 0:
                        raise RuntimeError('FluidSynth rejected a composed note.')
                end = min(total, round((beat_index + 1) * beat * rate))
                while position < end:
                    count = min(2048, end - position)
                    if render(synth, count, buffer, 0, 2, buffer, 1, 2) != 0:
                        raise RuntimeError('FluidSynth could not render the next audio block.')
                    data = bytes(buffer)[:count*4]
                    if sys.byteorder != 'little':
                        import array
                        samples = array.array('h')
                        samples.frombytes(data)
                        samples.byteswap()
                        data = samples.tobytes()
                    stream.write(data)
                    position += count
                beat_index += 1
            stream.flush()
            os.fsync(stream.fileno())
        return dict(engine='fluidsynth', status='complete', frames=total, sample_rate=rate,
                    channels=2, library_version=version().decode('ascii'), seed=spec['seed'])
    finally:
        if synth:
            del_synth(synth)
        del_settings(settings)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise ValueError('One private worker request is required.')
    path = Path(arguments[0]).resolve(strict=True)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Invalid private worker request.')
    request = json.loads(path.read_text(encoding='utf-8'))
    if request.get('version') != 1 or request.get('engine') not in ('musicgen', 'fluidsynth'):
        raise ValueError('Unsupported worker request.')
    for name in ('output', 'result'):
        destination = Path(request[name])
        if destination.parent.resolve(strict=True) != path.parent or os.path.lexists(destination):
            raise ValueError('Worker outputs must be fresh files in the private stage.')
    deny_network()
    report = _musicgen(request) if request['engine'] == 'musicgen' else _fluidsynth(request)
    with Path(request['result']).open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Local music worker: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
