"""Private isolated local music worker. No downloads, sockets or device access."""
from __future__ import annotations

import ctypes
from ctypes.util import find_library
import json
import math
from pathlib import Path
import random
import socket
import struct
import sys


def _deny_network(*args, **kwargs):
    raise OSError('Network access is disabled in the local music worker.')


def _header(stream, rate, channels, frames):
    data_size = frames * channels * 2
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * channels * 2, channels * 2, 16)
    if data_size + 36 <= 0xffffffff:
        stream.write(b'RIFF' + struct.pack('<I', data_size + 36) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt)
        stream.write(b'data' + struct.pack('<I', data_size))
    else:
        stream.write(b'RF64\xff\xff\xff\xffWAVEds64' + struct.pack('<I', 28))
        stream.write(struct.pack('<QQQI', data_size + 72, data_size, frames, 0))
        stream.write(b'fmt ' + struct.pack('<I', 16) + fmt + b'data\xff\xff\xff\xff')


def _fluidsynth(data):
    request = data['request']
    library = data.get('fluidsynth_library')
    if not library:
        raise RuntimeError('FluidSynth is unavailable in the local worker.')
    fluid = ctypes.CDLL(library)
    ptr, integer = ctypes.c_void_p, ctypes.c_int
    declarations = {
        'new_fluid_settings': (ptr, []), 'delete_fluid_settings': (None, [ptr]),
        'fluid_settings_setnum': (integer, [ptr, ctypes.c_char_p, ctypes.c_double]),
        'fluid_settings_setint': (integer, [ptr, ctypes.c_char_p, integer]),
        'new_fluid_synth': (ptr, [ptr]), 'delete_fluid_synth': (None, [ptr]),
        'fluid_synth_sfload': (integer, [ptr, ctypes.c_char_p, integer]),
        'fluid_synth_program_change': (integer, [ptr, integer, integer]),
        'fluid_synth_noteon': (integer, [ptr, integer, integer, integer]),
        'fluid_synth_noteoff': (integer, [ptr, integer, integer]),
        'fluid_synth_write_s16': (integer, [ptr, integer, ptr, integer, integer, ptr, integer, integer]),
    }
    for name, (result, args) in declarations.items():
        fn = getattr(fluid, name)
        fn.restype, fn.argtypes = result, args
    settings = fluid.new_fluid_settings()
    synth = None
    if not settings:
        raise RuntimeError('FluidSynth settings allocation failed.')
    try:
        rate = request['sample_rate']
        fluid.fluid_settings_setnum(settings, b'synth.sample-rate', rate)
        fluid.fluid_settings_setnum(settings, b'synth.gain', .35)
        fluid.fluid_settings_setint(settings, b'synth.reverb.active', 0)
        fluid.fluid_settings_setint(settings, b'synth.chorus.active', 0)
        synth = fluid.new_fluid_synth(settings)
        if not synth or fluid.fluid_synth_sfload(synth, str(data['soundfont']).encode('utf-8'), 1) < 0:
            raise RuntimeError('FluidSynth could not load the approved SoundFont.')
        if fluid.fluid_synth_program_change(synth, 0, request['program']) < 0:
            raise RuntimeError('The selected General MIDI instrument is unavailable.')
        frames = round(request['duration_seconds'] * rate)
        buffer = (ctypes.c_short * 2048)()
        minor = request['mode'] == 'minor'
        progression = (0, 8 if minor else 9, 5, 7)
        intervals = (0, 3 if minor else 4, 7, 12)
        # Compositional MIDI pattern only. It neither interprets the natural
        # language prompt nor pretends to be a neural composer.
        variation = random.Random(request['seed']).randrange(4)
        beat_length = rate * 60 / request['tempo']
        position, beat, next_event, playing = 0, 0, 0, None
        phase = 'on'
        with Path(data['output']).open('xb') as out:
            _header(out, rate, 2, frames)
            while position < frames:
                if position == next_event:
                    if playing is not None:
                        fluid.fluid_synth_noteoff(synth, 0, playing)
                        playing = None
                    if phase == 'on':
                        note = request['root_note'] + progression[(beat // 4) % 4] + intervals[(beat + variation) % 4]
                        playing = min(127, note)
                        if fluid.fluid_synth_noteon(synth, 0, playing, 66) < 0:
                            raise RuntimeError('FluidSynth rejected a validated MIDI note.')
                        next_event = max(position + 1, round((beat + .82) * beat_length))
                        phase = 'off'
                    else:
                        beat += 1
                        next_event = max(position + 1, round(beat * beat_length))
                        phase = 'on'
                count = min(1024, frames - position, next_event - position)
                if fluid.fluid_synth_write_s16(synth, count, buffer, 0, 2, buffer, 1, 2) < 0:
                    raise RuntimeError('FluidSynth failed while rendering a PCM block.')
                if sys.byteorder == 'little':
                    out.write(ctypes.string_at(buffer, count * 4))
                else:
                    out.write(struct.pack('<' + 'h' * (count * 2), *buffer[:count * 2]))
                position += count
                if position % rate < count:
                    print('out_time_us=' + str(round(position / rate * 1_000_000)), flush=True)
        print('out_time_us=' + str(round(frames / rate * 1_000_000)), flush=True)
    finally:
        if synth:
            fluid.delete_fluid_synth(synth)
        fluid.delete_fluid_settings(settings)


def _musicgen(data):
    # Imports occur only after the network deny hooks below are installed.
    import torch
    from transformers import AutoProcessor, MusicgenForConditionalGeneration
    request = data['request']
    local = data['model_dir']
    processor = AutoProcessor.from_pretrained(local, local_files_only=True, trust_remote_code=False)
    model = MusicgenForConditionalGeneration.from_pretrained(local, local_files_only=True,
                                                            trust_remote_code=False, use_safetensors=True)
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = model.to(device)
    model.eval()
    torch.manual_seed(request['seed'])
    inputs = processor(text=[request['prompt']], padding=True, return_tensors='pt')
    inputs = {key: value.to(device) for key, value in inputs.items()}
    # MusicGen uses 50 codec tokens per second; its reviewed HF API documents a
    # 30-second positional context. Add only decoder delay tokens, bounded by it.
    tokens = min(1503, math.ceil(request['duration_seconds'] * 50) + 3)
    with torch.inference_mode():
        audio = model.generate(**inputs, do_sample=True, guidance_scale=3, max_new_tokens=tokens)[0]
    rate = int(model.config.audio_encoder.sampling_rate)
    channels = int(audio.shape[0])
    frames = round(request['duration_seconds'] * rate)
    if channels not in (1, 2) or audio.shape[1] < frames:
        raise RuntimeError('MusicGen returned fewer audio samples than requested; no output was accepted.')
    # One engine chunk is bounded at 30 seconds; final many-hour audio is streamed
    # by the scene scheduler and FFmpeg assembler, never one enormous tensor.
    audio = audio[:, :frames].detach().to('cpu')
    if not torch.isfinite(audio).all():
        raise RuntimeError('MusicGen returned non-finite audio samples.')
    audio = (audio.clamp(-.95, .95) * 32767).round().to(torch.int16).transpose(0, 1).contiguous()
    with Path(data['output']).open('xb') as out:
        _header(out, rate, channels, frames)
        payload = audio.numpy()
        out.write(payload.astype('<i2', copy=False).tobytes())
    print('out_time_us=' + str(round(frames / rate * 1_000_000)), flush=True)


def main():
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    socket.create_connection = _deny_network
    socket.getaddrinfo = _deny_network
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if data['request']['engine'] == 'fluidsynth':
        _fluidsynth(data)
    elif data['request']['engine'] == 'musicgen':
        _musicgen(data)
    else:
        raise ValueError('Unsupported music engine.')


if __name__ == '__main__':
    main()
