"""Synthetic-only prepared WAV track probe; not an application audio backend."""
from dataclasses import dataclass
from pathlib import Path
import math
import os
import re
import stat
import subprocess
import tempfile
import wave

RATE = 48000
MAX_MS = 60000  # single compatibility fixture, not a product duration limit


@dataclass(frozen=True)
class Track:
    name: str
    start_ms: int = 0
    pan: float = 0.0
    gain: float = 1.0


def _number(x, low, high):
    if type(x) not in (int, float) or not low <= x <= high or not math.isfinite(x):
        raise ValueError('invalid numeric track parameter')
    return float(x)


def _input(root, track, duration_ms):
    if not isinstance(track, Track) or not re.fullmatch(r'[A-Za-z0-9_-]+[.]wav', track.name):
        raise ValueError('expected simple WAV filename')
    if type(track.start_ms) is not int or not 0 <= track.start_ms < duration_ms:
        raise ValueError('invalid start')
    pan, gain = _number(track.pan, -1, 1), _number(track.gain, 0, 1)
    path = root / track.name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('expected regular unlinked input')
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                audio.getcomptype()) != (1, 2, RATE, 'NONE'):
            raise ValueError('probe expects 48kHz mono PCM16 WAV')
        if not 0 < audio.getnframes() <= (duration_ms - track.start_ms) * 48:
            raise ValueError('track exceeds planned timeline')
    return path, pan, gain


def mix_prepared(root, voices, music, output, *, duration_ms, ffmpeg):
    """Mix prepared fixture tracks through FFmpeg; explicit trusted binary only."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('expected existing fixture directory')
    root = root.resolve()
    binary = Path(ffmpeg)
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError('expected trusted absolute FFmpeg path')
    if type(duration_ms) is not int or not 1 <= duration_ms <= MAX_MS:
        raise ValueError('invalid fixture duration')
    if not isinstance(voices, (tuple, list)) or not 1 <= len(voices) <= 8:
        raise ValueError('expected one to eight voice fixtures')
    if not isinstance(output, str) or not re.fullmatch(r'[A-Za-z0-9_-]+[.]wav', output):
        raise ValueError('expected simple output WAV filename')
    target = root / output
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    tracks = [*voices, music]
    if any(not isinstance(t, Track) for t in tracks):
        raise ValueError('invalid track')
    if len({t.name for t in tracks}) != len(tracks) or output in {t.name for t in tracks}:
        raise ValueError('track/output names must be distinct')
    inputs = [_input(root, t, duration_ms) for t in tracks]
    samples = duration_ms * 48
    graph, args = [], [str(binary), '-hide_banner', '-loglevel', 'error', '-nostdin']
    for i, (track, (path, pan, gain)) in enumerate(zip(tracks, inputs)):
        args += ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(path)]
        left, right = math.sqrt((1-pan)/2), math.sqrt((1+pan)/2)
        graph.append(f'[{i}:a]aformat=sample_fmts=fltp:channel_layouts=mono,'
                     f'volume={gain:.9f},pan=stereo|c0={left:.9f}*c0|c1={right:.9f}*c0,'
                     f'adelay={track.start_ms*48}S:all=1,apad=whole_len={samples},'
                     f'atrim=end_sample={samples}[t{i}]')
    n = len(voices)
    labels = ''.join(f'[t{i}]' for i in range(n))
    graph.append(f'{labels}amix=inputs={n}:duration=longest:normalize=0,asplit=2[v][sc]')
    graph.append(f'[t{n}][sc]sidechaincompress=threshold=0.05:ratio=8:attack=10:release=100[m]')
    graph.append(f'[v][m]amix=inputs=2:duration=longest:normalize=0,'
                 f'alimiter=limit=0.95:level=0:latency=1,atrim=end_sample={samples}[out]')
    # Use a private staging directory and no-clobber link after a successful render.
    with tempfile.TemporaryDirectory(prefix='.mix-', dir=root) as stage:
        tmp = Path(stage) / 'render.wav'
        args += ['-filter_complex', ';'.join(graph), '-map', '[out]', '-ar', str(RATE),
                 '-c:a', 'pcm_s16le', '-f', 'wav', '-n', str(tmp)]
        subprocess.run(args, check=True, timeout=120, capture_output=True)
        with wave.open(str(tmp), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                    audio.getnframes()) != (2, 2, RATE, samples):
                raise ValueError('unexpected rendered WAV format/duration')
        os.link(tmp, target)
    return target
