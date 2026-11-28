"""Bounded local FFmpeg operations and lossless streaming PCM assembly.

User strings never enter a filter graph, shell command or media URL. PCM
concatenation only writes a standard RIFF/RF64 container; DSP stays in FFmpeg.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
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

from .jobs import (checked_directory, check_cancelled, file_identity, publish_noreplace,
                   regular_file, sha256_file, verified_reader)
from .models import Effect, EFFECT_CONTROLS, FORMATS, SAMPLE_RATES, number
from .paths import filename_component


# Part of the scene-mix and completed-production cache identity. Increment when
# a mixing algorithm changes, without invalidating expensive speech/music cues.
MIX_REVISION = 2


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


def wav_info(path, *, allow_empty=False):
    """Validate a complete PCM16 RIFF/RF64 container with bounded header reads.

    Inspect trailing chunks too: FFmpeg can select a later data/fmt chunk,
    which must never disagree with the duration accepted during preflight.
    """
    if type(allow_empty) is not bool:
        raise ValueError('Choose explicitly whether an unfinished empty PCM part is admissible.')
    with verified_reader(path) as stream:
        total = os.fstat(stream.fileno()).st_size
        head = stream.read(12)
        if len(head) != 12 or head[:4] not in (b'RIFF', b'RF64') or head[8:] != b'WAVE':
            raise ValueError('Audio must be a PCM WAV file.')
        rf64 = head[:4] == b'RF64'
        declared = struct.unpack('<I', head[4:8])[0]
        if (rf64 and declared != 0xffffffff) or (not rf64 and declared + 8 != total):
            raise ValueError('WAV size declaration does not match the file.')
        data64, frames64, format_info, result = None, None, None, None
        # An attacker cannot make us traverse millions of tiny metadata chunks.
        for _ in range(1024):
            if stream.tell() == total:
                break
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError('Truncated WAV chunk header.')
            name, size = struct.unpack('<4sI', header)
            declared_chunk_size = size
            at = stream.tell()
            if size == 0xffffffff:
                if name != b'data' or not rf64 or data64 is None:
                    raise ValueError('Invalid RF64 size reference.')
                size = data64
            if at + size + (size & 1) > total:
                raise ValueError('Truncated WAV chunk.')
            if name == b'ds64':
                if not rf64 or data64 is not None or not 28 <= size <= 65536:
                    raise ValueError('Invalid RF64 size table.')
                riff_size, data64, frames64, table = struct.unpack('<QQQI', stream.read(28))
                if not table and riff_size + 8 > total:
                    raise ValueError('RF64 audio is truncated.')
                if table or riff_size + 8 != total:
                    raise ValueError('Unsupported or inconsistent RF64 size table.')
            elif name == b'fmt ':
                if format_info is not None or not 16 <= size <= 65536:
                    raise ValueError('Duplicate or invalid WAV format.')
                encoding, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', stream.read(16))
                if encoding == 65534 and size >= 40:
                    extended = stream.read(24)
                    extension_size, valid_bits = struct.unpack('<HH', extended[:4])
                    if extension_size < 22 or valid_bits not in (0, 16) or extended[8:] != bytes.fromhex('0100000000001000800000aa00389b71'):
                        raise ValueError('Expected uncompressed integer PCM.')
                    encoding = 1
                if encoding != 1 or channels not in (1, 2) or bits != 16 or not 8000 <= rate <= 192000 or align != channels * 2 or byte_rate != rate * align:
                    raise ValueError('Expected mono or stereo PCM16 WAV.')
                format_info = (rate, channels, align)
            elif name == b'data':
                if rf64 and declared_chunk_size != 0xffffffff:
                    raise ValueError('RF64 data requires its 64-bit size sentinel.')
                if (result is not None or format_info is None or (size == 0 and not allow_empty)
                        or (rf64 and data64 is None)):
                    raise ValueError('Duplicate, empty or invalid WAV audio data.')
                rate, channels, align = format_info
                if size % align:
                    raise ValueError('Incomplete audio frame.')
                if frames64 not in (None, 0, size // align):
                    raise ValueError('RF64 frame count does not match its PCM data.')
                if rf64 and size != data64:
                    raise ValueError('RF64 data size does not match its size table.')
                result = WaveInfo(rate, channels, size // align, at, size)
            stream.seek(at + size + (size & 1))
        if stream.tell() != total:
            raise ValueError('Too many WAV metadata chunks.')
        if result is not None:
            return result
    raise ValueError('No valid PCM audio data found.')


def trusted_ffmpeg(path):
    path = Path(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError('Select an installed FFmpeg executable using its absolute path.')
    return str(path)


def _ffmpeg_environment():
    environment = os.environ.copy()
    # FFREPORT can otherwise write an arbitrary inherited report filename even
    # when all explicit argv use only the selected local inputs and outputs.
    environment.pop('FFREPORT', None)
    return environment


def run_ffmpeg(ffmpeg, arguments, *, cancelled=lambda: False, loglevel='error'):
    """No shell, no network input protocols, bounded diagnostic output."""
    binary = trusted_ffmpeg(ffmpeg)
    check_cancelled(cancelled)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([binary, '-hide_banner', '-nostdin', '-loglevel', loglevel, *arguments],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                                   env=_ffmpeg_environment(),
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                check_cancelled(cancelled)
                if os.fstat(log.fileno()).st_size > 1024 * 1024:
                    raise RuntimeError('FFmpeg returned excessive diagnostic output.')
                time.sleep(0.03)
            check_cancelled(cancelled)
            # A fast child can exit before the polling loop checks its log.
            # Apply the same bound at EOF before accepting either exit status.
            if os.fstat(log.fileno()).st_size > 1024 * 1024:
                raise RuntimeError('FFmpeg returned excessive diagnostic output.')
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


def ffmpeg_identity(path, *, cancelled=lambda: False):
    check_cancelled(cancelled)
    binary = trusted_ffmpeg(path)
    process = subprocess.run([binary, '-version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=10, check=True,
                             env=_ffmpeg_environment(),
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = process.stdout[:16384].decode('utf-8', 'replace').splitlines()
    if not lines:
        raise RuntimeError('The selected FFmpeg executable returned no version information.')
    import hashlib
    return {'binary_sha256': sha256_file(path, cancelled=cancelled), 'version': lines[0],
            'version_sha256': hashlib.sha256(process.stdout).hexdigest()}


def _tempo_filters(value):
    """Keep each atempo stage within its well supported 0.5..2 interval."""
    filters = []
    while value < 0.5:
        filters.append('atempo=0.5')
        value /= 0.5
    while value > 2:
        filters.append('atempo=2')
        value /= 2
    filters.append(f'atempo={value:.12g}')
    return filters


def _extended_effect(effect, sample_rate):
    p = dict(effect.parameters)
    values = {name: p.get(name, spec[0])
              for name, spec in EFFECT_CONTROLS[effect.kind].items()}
    kind, v = effect.kind, values
    if kind == 'gain':
        return [f'volume={v["gain_db"]:.8f}dB']
    if kind in ('lowshelf', 'highshelf', 'notch'):
        if v['frequency'] >= sample_rate / 2:
            raise ValueError('Filter frequency must be below half the input sample rate.')
        if kind == 'notch':
            return [f'bandreject=f={v["frequency"]:.8f}:t=q:w={v["q"]:.8f}']
        name = 'bass' if kind == 'lowshelf' else 'treble'
        return [f'{name}=f={v["frequency"]:.8f}:t=q:w={v["q"]:.8f}:g={v["gain_db"]:.8f}']
    if kind == 'limiter':
        ceiling = 10 ** (v['ceiling_db'] / 20)
        return [f'alimiter=limit={ceiling:.10f}:level=0:latency=1'
                f':attack={v["attack_ms"]:.8f}:release={v["release_ms"]:.8f}']
    if kind in ('declick', 'declip'):
        return ['adeclick' if kind == 'declick' else 'adeclip']
    if kind == 'deesser':
        return [f'deesser=i={v["intensity"]:.8f}:m={v["max_reduction"]:.8f}:f={v["frequency"]:.8f}']
    if kind == 'reverb':
        # Four bounded reflections, not convolution or an RT60 simulation.
        delays = '|'.join(f'{v["room_ms"] * factor:.8f}' for factor in (1, 1.73, 2.39, 3.11))
        decays = '|'.join(f'{v["mix"] * v["decay"] ** i:.8f}' for i in (1, 2, 3, 4))
        return [f'aecho=1:{1 / (1 + v["mix"]):.8f}:{delays}:{decays}']
    if kind == 'chorus':
        if v['mix'] == 0:
            return []
        return [f'chorus=1:{1 / (1 + v["mix"]):.8f}:{v["delay_ms"]:.8f}'
                f':{v["mix"]:.8f}:{v["speed_hz"]:.8f}:{v["depth_ms"]:.8f}']
    if kind == 'tempo':
        return _tempo_filters(v['factor'])
    if kind == 'pitch':
        factor = 2 ** (v['semitones'] / 12)
        # Establish the true input rate even when an earlier loudnorm stage
        # negotiated a higher rate. A different output rate is not pitch.
        return [f'aresample={sample_rate}', f'asetrate={sample_rate * factor:.12g}',
                f'aresample={sample_rate}', *_tempo_filters(1 / factor)]
    if kind == 'width':
        return [f'extrastereo=m={v["factor"]:.8f}:c=0']
    if kind == 'loudness':
        return [f'loudnorm=I={v["target_lufs"]:.8f}:TP={v["peak_db"]:.8f}:LRA={v["range_lu"]:.8f}']
    raise ValueError('Unsupported audio effect.')


def effect_filters(effects, *, sample_rate=48000):
    if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
        raise ValueError('Unsupported effect input sample rate.')
    filters = []
    for effect in effects:
        if not isinstance(effect, Effect):
            raise ValueError('Expected a typed audio effect.')
        if not effect.enabled:
            continue
        p = dict(effect.parameters)
        if effect.kind in ('highpass', 'lowpass'):
            default = 80 if effect.kind == 'highpass' else 12000
            selected = f"{effect.kind}=f={p.get('frequency', default):.8f}"
            if 'q' in p:
                selected += f":t=q:w={p['q']:.8f}"
            filters.append(selected)
        elif effect.kind == 'equalizer':
            filters.append(f"equalizer=f={p.get('frequency', 1000):.8f}:t=q:w={p.get('q', 1):.8f}:g={p.get('gain_db', 0):.8f}")
        elif effect.kind == 'gate':
            threshold = 10 ** (p.get('threshold_db', -45) / 20)
            selected = f"agate=threshold={threshold:.10f}:attack={p.get('attack_ms', 10):.8f}:release={p.get('release_ms', 200):.8f}"
            if 'ratio' in p:
                selected += f":ratio={p['ratio']:.8f}"
            if 'reduction_db' in p:
                selected += f":range={10 ** (p['reduction_db'] / 20):.10f}"
            filters.append(selected)
        elif effect.kind == 'compressor':
            threshold = 10 ** (p.get('threshold_db', -20) / 20)
            selected = f"acompressor=threshold={threshold:.10f}:ratio={p.get('ratio', 3):.8f}:attack={p.get('attack_ms', 20):.8f}:release={p.get('release_ms', 250):.8f}"
            if 'knee' in p:
                selected += f":knee={p['knee']:.8f}"
            if 'makeup_db' in p:
                selected += f":makeup={10 ** (p['makeup_db'] / 20):.10f}"
            if 'mix' in p:
                selected += f":mix={p['mix']:.8f}"
            filters.append(selected)
        elif effect.kind == 'delay':
            if 'mix' in p:
                filters.append(f"aecho=1:{1 / (1 + p['mix']):.8f}:{p.get('delay_ms', 250):.8f}:{p.get('decay', 0.25) * p['mix']:.8f}")
            else:
                filters.append(f"aecho=0.8:0.9:{p.get('delay_ms', 250):.8f}:{p.get('decay', 0.25):.8f}")
        elif effect.kind == 'denoise':
            selected = f"afftdn=nr={p.get('reduction_db', 10):.8f}"
            if 'floor_db' in p:
                selected += f":nf={p['floor_db']:.8f}"
            filters.append(selected)
        else:
            filters.extend(_extended_effect(effect, sample_rate))
    return filters


def _destination(path):
    path = Path(path)
    filename_component(path.name)
    checked_directory(path.parent, create=True)
    if path.exists() or path.is_symlink():
        raise FileExistsError(f'Audio output already exists: {path.name}')
    return path


def _wav_arguments(output, rate):
    return ['-ar', str(rate), '-ac', '2', '-c:a', 'pcm_s16le', '-rf64', 'auto', '-f', 'wav', '-n', str(output)]


def process_voice(source, output, *, ffmpeg, sample_rate=48000, pan=0.0, gain=1.0,
                  effects=(), cancelled=lambda: False, expected_identity=None):
    source, output = Path(source).absolute(), _destination(output)
    identity = file_identity(regular_file(source))
    if expected_identity is not None and identity != expected_identity:
        raise RuntimeError('An audio input changed after it was validated. Restart the production.')
    source_info = wav_info(source)
    if sample_rate not in SAMPLE_RATES:
        raise ValueError('Unsupported sample rate.')
    number(pan, -1, 1, 'Pan')
    number(gain, 0, 2, 'Gain')
    effects = tuple(effects)
    selected_filters = effect_filters(effects, sample_rate=source_info.rate)
    left, right = math.sqrt((1 - pan) / 2), math.sqrt((1 + pan) / 2)
    placement = f'pan=stereo|c0={left:.9f}*c0|c1={right:.9f}*c0'
    graph = ['aformat=sample_fmts=fltp:channel_layouts=mono', f'volume={gain:.9f}']
    if any(effect.kind == 'width' and effect.enabled for effect in effects):
        # A stereo width control needs the actual stereo placement. Preserve
        # the declared chain order after placement when this new control is used.
        graph.extend((placement, *selected_filters))
    else:
        # Existing projects keep their previous mono-effects-then-pan graph.
        graph.extend((*selected_filters, placement))
    graph.append('alimiter=limit=0.95:level=0:latency=1')
    with tempfile.TemporaryDirectory(prefix='.process-', dir=output.parent) as temp:
        target = Path(temp) / 'chunk.wav'
        run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source),
                           '-vn', '-af', ','.join(graph), *_wav_arguments(target, sample_rate)], cancelled=cancelled)
        result = wav_info(target)
        if result.channels != 2 or result.rate != sample_rate or result.frames <= 0:
            raise RuntimeError('The voice processor returned an invalid waveform.')
        if file_identity(regular_file(source)) != identity:
            raise RuntimeError('An audio input changed during processing. Restart the production.')
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
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
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
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
    identities = [file_identity(regular_file(p)) for p in inputs]
    descriptions = [wav_info(p) for p in inputs]
    rate, channels = descriptions[0].rate, descriptions[0].channels
    if any((i.rate, i.channels) != (rate, channels) for i in descriptions):
        raise ValueError('Audio segments have different formats; convert them first.')
    frames = sum(i.frames for i in descriptions)
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temp:
        target = Path(temp) / 'assembled.wav'
        with target.open('wb') as dest:
            _write_header(dest, frames, rate, channels)
            for index, (path, info, identity) in enumerate(zip(inputs, descriptions, identities)):
                check_cancelled(cancelled)
                on_status(f'Assembling segment {index + 1} of {len(inputs)}')
                with verified_reader(path, expected_identity=identity) as source:
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
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
    return output


def mix_scene(voices, music, output, *, ffmpeg, music_gain=0.15, cancelled=lambda: False):
    """Duck a music bed beneath an already timed stereo narration stem."""
    voices, output = Path(voices).absolute(), _destination(output)
    identities = [(voices, file_identity(regular_file(voices)))]
    info = wav_info(voices)
    if info.channels != 2:
        raise ValueError('A scene needs a stereo narration stem.')
    number(music_gain, 0, 0.8, 'Music gain')
    args = ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(voices)]
    if music is None:
        graph = f'[0:a]alimiter=limit=0.95:level=0:latency=1,atrim=end_sample={info.frames}[out]'
    else:
        music = Path(music).absolute()
        identities.append((music, file_identity(regular_file(music))))
        background = wav_info(music)
        if background.rate != info.rate or background.frames != info.frames:
            raise ValueError('Music and narration timing must match exactly.')
        args += ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(music)]
        fade = min(0.25, info.seconds / 2)
        # The sidechain filter can stop at the shorter queued block stream
        # when a stereo WAV and mono WAV arrive in different decoder block
        # sizes. Normalize both inputs to equal sample blocks and trim the
        # padded tail back to the exact measured narration duration.
        graph = (f'[0:a]aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 'asetnsamples=n=1024:p=1,asplit=2[v][side];'
                 '[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 f'volume={music_gain:.9f},afade=t=in:d={fade:.9f},'
                 f'afade=t=out:st={max(0, info.seconds-fade):.9f}:d={fade:.9f},'
                 'asetnsamples=n=1024:p=1[bed];'
                 '[bed][side]sidechaincompress=threshold=0.035:ratio=8:attack=10:release=200[ducked];'
                 f'[v][ducked]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1,'
                 f'atrim=end_sample={info.frames}[out]')
    with tempfile.TemporaryDirectory(prefix='.mix-', dir=output.parent) as temp:
        target = Path(temp) / 'scene.wav'
        run_ffmpeg(ffmpeg, [*args, '-filter_complex', graph, '-map', '[out]', *_wav_arguments(target, info.rate)], cancelled=cancelled)
        result = wav_info(target)
        if (result.frames, result.rate, result.channels) != (info.frames, info.rate, 2):
            raise RuntimeError('Mix duration or format changed unexpectedly.')
        if any(file_identity(regular_file(path)) != identity for path, identity in identities):
            raise RuntimeError('An audio input changed during mixing. Restart the production.')
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
    return output


def measure_peak(path, *, cancelled=lambda: False):
    """Integer peak measurement in 1 MiB blocks, independent of project length."""
    identity = file_identity(regular_file(path))
    info, peak = wav_info(path), 0
    with verified_reader(path, expected_identity=identity) as source:
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



_SILENCE_BLOCK_BYTES = 1024 * 1024
_SILENCE_MAX_INTERVALS = 1000


def _silence_criteria(threshold_dbfs, minimum_seconds, max_intervals):
    """Explicit inspection criteria, not acoustic or broadcast quality targets."""
    values = []
    for value, low, high, name in (
            (threshold_dbfs, -120, 0, 'Silence threshold in dBFS'),
            (minimum_seconds, 0, 86400, 'Minimum silence duration')):
        if type(value) not in (int, float):
            raise ValueError(name + ' must be a finite number.')
        try:
            value = float(value)
        except (OverflowError, ValueError) as error:
            raise ValueError(name + ' must be a finite number.') from error
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(name + ' is outside the inspection range.')
        values.append(value)
    if values[1] <= 0:
        raise ValueError('Minimum silence duration must be positive.')
    if type(max_intervals) is not int or not 1 <= max_intervals <= _SILENCE_MAX_INTERVALS:
        raise ValueError('Retain between 1 and 1000 silence intervals.')
    return values[0], values[1], max_intervals


def _silence_minimum_frames(seconds, rate):
    # Interpret the displayed decimal criterion without binary-float rounding
    # turning exactly 0.1 seconds at 8 kHz into an 801-frame minimum.
    from fractions import Fraction
    duration = Fraction(str(seconds)) * rate
    return (duration.numerator + duration.denominator - 1) // duration.denominator


def measure_silence(path, *, threshold_dbfs, minimum_seconds,
                    cancelled=lambda: False, on_status=lambda message: None,
                    max_intervals=1000, expected_sha256=None, expected_identity=None):
    """Stream PCM16; every channel must meet the caller's amplitude criterion.

    Intervals are maximal, half-open frame ranges. The cap limits only retained
    rows: the complete file is scanned and all qualifying spans are counted.
    No decoder, audio device, engine or mutation is involved.
    """
    threshold_dbfs, minimum_seconds, max_intervals = _silence_criteria(
        threshold_dbfs, minimum_seconds, max_intervals)
    if expected_sha256 is not None and (
            type(expected_sha256) is not str or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None):
        raise ValueError('Expected PCM checksum must be a lowercase SHA256.')
    check_cancelled(cancelled)
    identity = file_identity(regular_file(path))
    if expected_identity is not None and identity != expected_identity:
        raise RuntimeError('The selected PCM changed before silence inspection.')
    info = wav_info(path)
    minimum_frames = _silence_minimum_frames(minimum_seconds, info.rate)
    threshold_pcm16 = math.floor(32768 * 10 ** (threshold_dbfs / 20))
    intervals, count, qualifying_frames = [], 0, 0
    start, position = None, 0
    checksum = hashlib.sha256()

    def finish(end):
        nonlocal start, count, qualifying_frames
        if start is not None and end - start >= minimum_frames:
            count += 1
            qualifying_frames += end - start
            if len(intervals) < max_intervals:
                intervals.append({'start_frame': start, 'end_frame': end, 'frames': end - start})
        start = None

    with verified_reader(path, expected_identity=identity) as stream:
        total = os.fstat(stream.fileno()).st_size

        def hash_bytes(amount):
            while amount:
                check_cancelled(cancelled)
                block = stream.read(min(_SILENCE_BLOCK_BYTES, amount))
                if not block:
                    raise RuntimeError('The PCM was truncated during silence inspection.')
                checksum.update(block)
                amount -= len(block)

        hash_bytes(info.data_offset)
        remaining = info.data_size
        frame_bytes = info.channels * 2
        block_bytes = max(frame_bytes, _SILENCE_BLOCK_BYTES // frame_bytes * frame_bytes)
        on_status('Inspecting retained PCM with the selected silence criteria.')
        last_report = time.monotonic()
        while remaining:
            check_cancelled(cancelled)
            block = stream.read(min(block_bytes, remaining))
            if not block or len(block) % frame_bytes:
                raise RuntimeError('The PCM was truncated during silence inspection.')
            checksum.update(block)
            samples = array('h')
            samples.frombytes(block)
            if sys.byteorder != 'little':
                samples.byteswap()
            values = iter(samples)
            for first in values:
                quiet = abs(first) <= threshold_pcm16
                if info.channels == 2:
                    quiet = abs(next(values)) <= threshold_pcm16 and quiet
                if quiet:
                    if start is None:
                        start = position
                else:
                    finish(position)
                position += 1
            remaining -= len(block)
            now = time.monotonic()
            if now - last_report >= 0.5 or not remaining:
                on_status(f'Inspected {position} of {info.frames} PCM frames.')
                last_report = now
        finish(position)
        if position != info.frames:
            raise RuntimeError('The inspected PCM frame count changed.')
        hash_bytes(total - info.data_offset - info.data_size)
        check_cancelled(cancelled)
        actual_sha256 = checksum.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise RuntimeError('The retained PCM checksum changed during silence inspection.')
    check_cancelled(cancelled)
    return {
        'sha256': actual_sha256, 'sample_rate': info.rate, 'channels': info.channels,
        'frames': info.frames, 'duration_seconds': info.seconds,
        'criteria': {
            'threshold_dbfs': threshold_dbfs, 'threshold_pcm16': threshold_pcm16,
            'minimum_seconds': minimum_seconds, 'minimum_frames': minimum_frames,
            'channel_rule': 'every-channel-at-or-below',
            'frame_bounds': 'start-inclusive-end-exclusive',
        },
        'interval_count': count, 'retained_interval_count': len(intervals),
        'omitted_interval_count': count - len(intervals), 'qualifying_frames': qualifying_frames,
        'max_intervals': max_intervals, 'intervals': intervals,
    }


def measure_loudness(path, *, ffmpeg, cancelled=lambda: False):
    identity = file_identity(regular_file(path))
    wav_info(path)
    log = run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(Path(path).absolute()),
                             '-af', 'loudnorm=I=-18:TP=-1:LRA=11:print_format=json', '-f', 'null', '-'],
                     cancelled=cancelled, loglevel='info')
    if file_identity(regular_file(path)) != identity:
        raise RuntimeError('An audio input changed during loudness measurement.')
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
    identities = [(source, file_identity(regular_file(source)))]
    wav_info(source)
    if format not in FORMATS or format == 'wav':
        raise ValueError('Choose a supported compressed audio format.')
    codecs = {'flac': ('flac', 'flac'), 'mp3': ('libmp3lame', 'mp3'),
              'opus': ('libopus', 'opus'), 'm4a': ('aac', 'ipod'),
              'm4b': ('aac', 'ipod'), 'aiff': ('pcm_s16be', 'aiff'),
              'ogg': ('libvorbis', 'ogg'), 'aac': ('aac', 'adts'), 'wma': ('wmav2', 'asf')}
    codec, container = codecs[format]
    args = ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source)]
    if metadata is not None:
        identities.append((metadata, file_identity(regular_file(metadata))))
        args += ['-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(Path(metadata).absolute()), '-map_metadata', '1', '-map_chapters', '1']
    args += ['-map', '0:a:0', '-c:a', codec]
    if format in ('mp3', 'm4a', 'm4b', 'aac', 'wma'):
        args += ['-b:a', '192k']
    if format == 'wma':
        # WMAv2 cannot encode above 48 kHz; make its rate explicit at every
        # accepted project rate instead of relying on encoder negotiation.
        args += ['-ar', '48000']
    if format == 'ogg':
        args += ['-q:a', '5']
    if format == 'opus':
        args += ['-ar', '48000', '-b:a', '128k']
    with tempfile.TemporaryDirectory(prefix='.encode-', dir=output.parent) as temp:
        target = Path(temp) / ('master.' + format)
        run_ffmpeg(ffmpeg, [*args, '-f', container, '-n', str(target)], cancelled=cancelled)
        if regular_file(target).st_size == 0:
            raise RuntimeError('The encoder produced no audio.')
        if any(file_identity(regular_file(path)) != identity for path, identity in identities):
            raise RuntimeError('An audio input changed during encoding. Restart the production.')
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
    return output
