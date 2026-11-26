"""Bounded local FFmpeg operations and lossless streaming PCM assembly.

User strings never enter a filter graph, shell command or media URL. PCM
concatenation only writes a standard RIFF/RF64 container; DSP stays in FFmpeg.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time

from .jobs import checked_directory, check_cancelled, publish_new, regular_file, sha256_file
from .models import Effect, FORMATS, SAMPLE_RATES, number


@dataclass(frozen=True)
class WaveInfo:
    rate: int
    channels: int
    frames: int
    data_offset: int
    data_size: int

    @property
    def seconds(self):
        return self.frames / self.rate


def wav_info(path):
    """Read PCM16 RIFF or RF64 without loading the waveform into memory."""
    total = regular_file(path).st_size
    with open(path, 'rb') as stream:
        head = stream.read(12)
        if len(head) != 12 or head[:4] not in (b'RIFF', b'RF64') or head[8:] != b'WAVE':
            raise ValueError('Audio must be a PCM WAV file.')
        data64 = None
        format_info = None
        # An attacker cannot make us traverse millions of tiny metadata chunks.
        for _ in range(1024):
            header = stream.read(8)
            if len(header) != 8:
                break
            name, size = struct.unpack('<4sI', header)
            at = stream.tell()
            if name == b'ds64':
                if size < 28 or size > 65536 or at + size > total:
                    raise ValueError('Invalid RF64 size table.')
                _, data64, _, table = struct.unpack('<QQQI', stream.read(28))
                if table:
                    raise ValueError('Unsupported RF64 table.')
            elif name == b'fmt ':
                if not 16 <= size <= 65536 or at + size > total:
                    raise ValueError('Invalid WAV format.')
                encoding, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', stream.read(16))
                if encoding == 65534 and size >= 40:
                    extended = stream.read(24)
                    if extended[8:] != bytes.fromhex('0100000000001000800000aa00389b71'):
                        raise ValueError('Expected uncompressed integer PCM.')
                    encoding = 1
                if encoding != 1 or channels not in (1, 2) or bits != 16 or not 8000 <= rate <= 192000 or align != channels * 2 or byte_rate != rate * align:
                    raise ValueError('Expected mono or stereo PCM16 WAV.')
                format_info = (rate, channels, align)
            elif name == b'data':
                size = data64 if size == 0xffffffff and head[:4] == b'RF64' else size
                if format_info is None or size is None or size <= 0 or at + size > total:
                    raise ValueError('Truncated or empty WAV audio.')
                rate, channels, align = format_info
                if size % align:
                    raise ValueError('Incomplete audio frame.')
                return WaveInfo(rate, channels, size // align, at, size)
            if at + size > total:
                raise ValueError('Truncated WAV metadata.')
            stream.seek(at + size + (size & 1))
    raise ValueError('No valid PCM audio data found.')


def trusted_ffmpeg(path):
    from .runtime import trusted_executable
    return str(trusted_executable(path))


def run_ffmpeg(ffmpeg, arguments, *, cancelled=lambda: False, loglevel='error'):
    """No shell, no network input protocols, bounded diagnostic output."""
    binary = trusted_ffmpeg(ffmpeg)
    check_cancelled(cancelled)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([binary, '-hide_banner', '-nostdin', '-loglevel', loglevel, *arguments],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                check_cancelled(cancelled)
                if os.fstat(log.fileno()).st_size > 1024 * 1024:
                    raise RuntimeError('FFmpeg returned excessive diagnostic output.')
                time.sleep(0.03)
            check_cancelled(cancelled)
            log.seek(0)
            message = log.read(1024 * 1024).decode('utf-8', errors='replace')
            if process.returncode:
                raise RuntimeError('FFmpeg could not complete the audio operation: ' + message[-4000:])
            return message
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def ffmpeg_identity(path):
    binary = trusted_ffmpeg(path)
    process = subprocess.run([binary, '-version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=10, check=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    first = process.stdout[:16384].decode('utf-8', 'replace').splitlines()[0]
    return {'binary_sha256': sha256_file(path), 'version': first}


def effect_filters(effects):
    filters = []
    for effect in effects:
        if not isinstance(effect, Effect):
            raise ValueError('Expected a typed audio effect.')
        if not effect.enabled:
            continue
        p = dict(effect.parameters)
        if effect.kind == 'highpass':
            filters.append(f"highpass=f={p.get('frequency', 80):.8f}")
        elif effect.kind == 'lowpass':
            filters.append(f"lowpass=f={p.get('frequency', 12000):.8f}")
        elif effect.kind == 'equalizer':
            filters.append(f"equalizer=f={p.get('frequency', 1000):.8f}:t=q:w={p.get('q', 1):.8f}:g={p.get('gain_db', 0):.8f}")
        elif effect.kind == 'gate':
            threshold = 10 ** (p.get('threshold_db', -45) / 20)
            filters.append(f"agate=threshold={threshold:.10f}:attack={p.get('attack_ms', 10):.8f}:release={p.get('release_ms', 200):.8f}")
        elif effect.kind == 'compressor':
            threshold = 10 ** (p.get('threshold_db', -20) / 20)
            filters.append(f"acompressor=threshold={threshold:.10f}:ratio={p.get('ratio', 3):.8f}:attack={p.get('attack_ms', 20):.8f}:release={p.get('release_ms', 250):.8f}")
        elif effect.kind == 'delay':
            filters.append(f"aecho=0.8:0.9:{p.get('delay_ms', 250):.8f}:{p.get('decay', 0.25):.8f}")
        elif effect.kind == 'denoise':
            filters.append(f"afftdn=nr={p.get('reduction_db', 10):.8f}")
    return filters


def _destination(path):
    path = Path(path)
    checked_directory(path.parent, create=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f'Audio output already exists: {path.name}')
    return path


def _wav_arguments(output, rate):
    return ['-ar', str(rate), '-ac', '2', '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', str(output)]


def process_voice(source, output, *, ffmpeg, sample_rate=48000, pan=0.0, gain=1.0,
                  effects=(), cancelled=lambda: False):
    source, output = Path(source).absolute(), _destination(output)
    original = wav_info(source)
    if sample_rate not in SAMPLE_RATES:
        raise ValueError('Unsupported sample rate.')
    number(pan, -1, 1, 'Pan')
    number(gain, 0, 2, 'Gain')
    left, right = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
    graph = ['aformat=sample_fmts=fltp:channel_layouts=mono', f'volume={gain:.9f}', *effect_filters(effects),
             f'pan=stereo|c0={left:.9f}*c0|c1={right:.9f}*c0',
             'alimiter=limit=0.95:level=0:latency=1']
    with tempfile.TemporaryDirectory(prefix='.process-', dir=output.parent) as temp:
        target = Path(temp) / 'chunk.wav'
        run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source),
                           '-vn', '-af', ','.join(graph), *_wav_arguments(target, sample_rate)], cancelled=cancelled)
        result = wav_info(target)
        if result.channels != 2 or result.rate != sample_rate or result.frames <= 0:
            raise RuntimeError('The voice processor returned an invalid waveform.')
        publish_new(target, output)
    return output


def _write_header(stream, frames, rate, channels):
    data_size = frames * channels * 2
    fmt = struct.pack('<HHIIHH', 1, channels, rate, rate * channels * 2, channels * 2, 16)
    if data_size + 36 <= 0xffffffff:
        stream.write(b'RIFF' + struct.pack('<I', data_size + 36) + b'WAVEfmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', data_size))
    else:
        # RF64 data starts at byte 80. ds64 stores the 64-bit sizes and count.
        stream.write(b'RF64\xff\xff\xff\xffWAVEds64' + struct.pack('<IQQQI', 28, data_size + 72, data_size, frames, 0))
        stream.write(b'fmt ' + struct.pack('<I', 16) + fmt + b'data\xff\xff\xff\xff')


def silence(output, frames, *, sample_rate=48000, channels=2, cancelled=lambda: False):
    output = _destination(output)
    if type(frames) is not int or frames < 1 or sample_rate not in SAMPLE_RATES or channels not in (1, 2):
        raise ValueError('Invalid silence format or duration.')
    with tempfile.TemporaryDirectory(prefix='.silence-', dir=output.parent) as temp:
        target = Path(temp) / 'silence.wav'
        with target.open('wb') as stream:
            _write_header(stream, frames, sample_rate, channels)
            remaining, zero = frames * channels * 2, b'\0' * (1024 * 1024)
            while remaining:
                check_cancelled(cancelled)
                size = min(remaining, len(zero))
                stream.write(zero[:size])
                remaining -= size
            stream.flush()
            os.fsync(stream.fileno())
        publish_new(target, output)
    return output


def concatenate(inputs, output, *, cancelled=lambda: False, on_status=lambda message: None):
    """Exact PCM concatenation, bounded RAM, and RIFF/RF64 selected by size.

    Inputs must already share rate/channels. No ceiling on final duration;
    callers enforce their user-selected storage budget before starting.
    """
    output = _destination(output)
    inputs = tuple(Path(p) for p in inputs)
    if not inputs:
        raise ValueError('Choose at least one audio segment.')
    descriptions = [wav_info(p) for p in inputs]
    rate, channels = descriptions[0].rate, descriptions[0].channels
    if any((i.rate, i.channels) != (rate, channels) for i in descriptions):
        raise ValueError('Audio segments have different formats; convert them first.')
    frames = sum(i.frames for i in descriptions)
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temp:
        target = Path(temp) / 'assembled.wav'
        with target.open('wb') as dest:
            _write_header(dest, frames, rate, channels)
            for index, (path, info) in enumerate(zip(inputs, descriptions)):
                check_cancelled(cancelled)
                on_status(f'Assembling segment {index + 1} of {len(inputs)}')
                with path.open('rb') as source:
                    source.seek(info.data_offset)
                    remaining = info.data_size
                    while remaining:
                        check_cancelled(cancelled)
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError('An audio segment was truncated during assembly.')
                        dest.write(block)
                        remaining -= len(block)
            dest.flush()
            os.fsync(dest.fileno())
        if wav_info(target).frames != frames:
            raise RuntimeError('The assembled duration is incorrect.')
        publish_new(target, output)
    return output


def mix_scene(voices, music, output, *, ffmpeg, music_gain=0.15, cancelled=lambda: False):
    """Duck a music bed beneath an already timed stereo narration stem."""
    voices, output = Path(voices).absolute(), _destination(output)
    info = wav_info(voices)
    if info.channels != 2:
        raise ValueError('A scene needs a stereo narration stem.')
    number(music_gain, 0, 0.8, 'Music gain')
    args = ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(voices)]
    if music is None:
        graph = f'[0:a]alimiter=limit=0.95:level=0:latency=1,atrim=end_sample={info.frames}[out]'
    else:
        music = Path(music).absolute()
        background = wav_info(music)
        if background.rate != info.rate or background.frames != info.frames:
            raise ValueError('Music and narration timing must match exactly.')
        args += ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(music)]
        fade = min(0.25, info.seconds / 2)
        graph = (f'[0:a]asplit=2[v][side];[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 f'volume={music_gain:.9f},afade=t=in:d={fade:.9f},'
                 f'afade=t=out:st={max(0, info.seconds-fade):.9f}:d={fade:.9f}[bed];'
                 '[bed][side]sidechaincompress=threshold=0.035:ratio=8:attack=10:release=200[ducked];'
                 f'[v][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1,'
                 f'atrim=end_sample={info.frames}[out]')
    with tempfile.TemporaryDirectory(prefix='.mix-', dir=output.parent) as temp:
        target = Path(temp) / 'scene.wav'
        run_ffmpeg(ffmpeg, [*args, '-filter_complex', graph, '-map', '[out]', *_wav_arguments(target, info.rate)], cancelled=cancelled)
        result = wav_info(target)
        if (result.frames, result.rate, result.channels) != (info.frames, info.rate, 2):
            raise RuntimeError('Mix duration or format changed unexpectedly.')
        publish_new(target, output)
    return output


def measure_peak(path, *, cancelled=lambda: False):
    """Integer peak measurement in 1 MiB blocks, independent of project length."""
    info, peak = wav_info(path), 0
    with open(path, 'rb') as source:
        source.seek(info.data_offset)
        remaining = info.data_size
        while remaining:
            check_cancelled(cancelled)
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('Truncated audio while measuring peak.')
            values = array('h')
            values.frombytes(block)
            if sys.byteorder != 'little':
                values.byteswap()
            peak = max(peak, abs(min(values)), abs(max(values)))
            remaining -= len(block)
    return {'peak': peak / 32768, 'clipped': peak >= 32767,
            'frames': info.frames, 'sample_rate': info.rate, 'channels': info.channels,
            'duration_seconds': info.seconds}


def measure_loudness(path, *, ffmpeg, cancelled=lambda: False):
    wav_info(path)
    log = run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(Path(path).absolute()),
                             '-af', 'loudnorm=I=-18:TP=-1:LRA=11:print_format=json', '-f', 'null', '-'],
                     cancelled=cancelled, loglevel='info')
    match = re.search(r'\{\s*"input_i".*?\}', log, re.DOTALL)
    if not match:
        raise RuntimeError('FFmpeg did not return a loudness measurement.')
    data = json.loads(match.group())
    # Silence is explicitly represented, not converted to a fabricated finite level.
    def finite(key):
        value = float(data[key])
        return value if math.isfinite(value) else None
    return {'integrated_lufs': finite('input_i'), 'true_peak_dbfs': finite('input_tp'),
            'loudness_range_lu': finite('input_lra'), 'measurement': 'FFmpeg loudnorm input measurements'}


def transcode(source, output, *, ffmpeg, format, metadata=None, cancelled=lambda: False):
    source, output = Path(source).absolute(), _destination(output)
    info = wav_info(source)
    if format not in FORMATS or format == 'wav':
        raise ValueError('Choose a supported compressed audio format.')
    codecs = {'flac': ('flac', 'flac'), 'mp3': ('libmp3lame', 'mp3'),
              'opus': ('libopus', 'opus'), 'm4a': ('aac', 'ipod'),
              'm4b': ('aac', 'ipod'), 'aiff': ('pcm_s16be', 'aiff')}
    codec, container = codecs[format]
    args = ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source)]
    if metadata is not None:
        regular_file(metadata)
        args += ['-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(Path(metadata).absolute()), '-map_metadata', '1', '-map_chapters', '1']
    args += ['-map', '0:a:0', '-c:a', codec]
    if format in ('mp3', 'm4a', 'm4b'):
        args += ['-b:a', '192k']
    if format == 'opus':
        args += ['-ar', '48000', '-b:a', '128k']
    with tempfile.TemporaryDirectory(prefix='.encode-', dir=output.parent) as temp:
        target = Path(temp) / ('master.' + format)
        run_ffmpeg(ffmpeg, [*args, '-f', container, '-n', str(target)], cancelled=cancelled)
        if regular_file(target).st_size == 0:
            raise RuntimeError('The encoder produced no audio.')
        publish_new(target, output)
    return output
