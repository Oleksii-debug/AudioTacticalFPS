"""Bounded clip DSP on the production engine's existing FFmpeg boundary."""
from dataclasses import dataclass
import math
from pathlib import Path
import tempfile

from . import ffmpeg as audio
from .jobs import (check_cancelled, file_identity, publish_noreplace,
                   regular_file)
from .models import Clip, MAX_SCENE_CLIPS, SAMPLE_RATES, number


CLIP_MIX_REVISION = 1


@dataclass(frozen=True)
class ClipWindow:
    source_start: int
    source_end: int
    frames: int
    start_frame: int
    fade_in: int
    fade_out: int


def clip_window(clip, info, sample_rate):
    """Resolve seconds once into exact source and output sample intervals."""
    if not isinstance(clip, Clip) or sample_rate not in SAMPLE_RATES:
        raise ValueError('Choose a validated clip and supported project sample rate.')
    if info.frames <= 0:
        raise ValueError('A clip needs a nonempty local PCM source.')
    start = round(clip.trim_start * info.rate)
    count = info.frames - start if clip.duration is None else round(clip.duration * info.rate)
    if start < 0 or start >= info.frames or count < 1 or start + count > info.frames:
        raise ValueError('The clip selection must contain whole samples inside the source.')
    frames = max(1, round(count * sample_rate / info.rate))
    fade_in, fade_out = round(clip.fade_in * sample_rate), round(clip.fade_out * sample_rate)
    if fade_in + fade_out > frames:
        raise ValueError('Clip fades cannot overlap or exceed the selected sample interval.')
    return ClipWindow(start, start + count, frames, round(clip.start * sample_rate), fade_in, fade_out)


def process_clip(source, output, clip, *, ffmpeg, sample_rate, cancelled=lambda: False,
                 expected_identity=None):
    source, output = Path(source).absolute(), audio._destination(output)
    identity = file_identity(regular_file(source))
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('The retained clip source changed before processing.')
    info = audio.wav_info(source)
    window = clip_window(clip, info, sample_rate)
    filters = [f'atrim=start_sample={window.source_start}:end_sample={window.source_end}',
               'asetpts=PTS-STARTPTS', f'aresample={sample_rate}',
               f'apad=whole_len={window.frames}', f'atrim=end_sample={window.frames}']
    if info.channels == 1:
        angle = (clip.pan + 1) * math.pi / 4
        filters.append(f'pan=stereo|c0={math.cos(angle):.12f}*c0|c1={math.sin(angle):.12f}*c0')
    else:
        # Balance preserves both source channels at center. Do not downmix a
        # user's stereo recording merely to reuse speech-only processing.
        left = math.cos(max(0, clip.pan) * math.pi / 2)
        right = math.cos(max(0, -clip.pan) * math.pi / 2)
        filters.append(f'pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c1')
    filters.append(f'volume={10 ** (clip.gain_db / 20):.12f}')
    if window.fade_in:
        filters.append(f'afade=t=in:ss=0:ns={window.fade_in}')
    if window.fade_out:
        filters.append(f'afade=t=out:ss={window.frames-window.fade_out}:ns={window.fade_out}')
    filters.extend(('alimiter=limit=0.95:level=0:latency=1',
                    f'apad=whole_len={window.frames}', f'atrim=end_sample={window.frames}'))
    check_cancelled(cancelled)
    with tempfile.TemporaryDirectory(prefix='.clip-', dir=output.parent) as temporary:
        target = Path(temporary) / 'clip.wav'
        audio.run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source),
            '-af', ','.join(filters), *audio._wav_arguments(target, sample_rate)], cancelled=cancelled)
        actual = audio.wav_info(target)
        if (actual.frames, actual.rate, actual.channels) != (window.frames, sample_rate, 2):
            raise RuntimeError('Clip DSP changed the selected sample interval or PCM format.')
        if file_identity(regular_file(source)) != identity:
            raise RuntimeError('The retained clip source changed during processing.')
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
    return output


def mix_tracks(core, clips, output, *, ffmpeg, music=None, music_gain=0.15,
               duck=True, cancelled=lambda: False):
    """Mix bounded (PCM path, start sample, track) rows on one fixed timeline."""
    core, output = Path(core).absolute(), audio._destination(output)
    rows = tuple(clips)
    if len(rows) > MAX_SCENE_CLIPS:
        raise ValueError('A chapter supports at most thirty-two overlaid clips.')
    info = audio.wav_info(core)
    if info.channels != 2 or info.frames <= 0:
        raise ValueError('A clip mix needs a nonempty stereo chapter timeline.')
    inputs = [(core, file_identity(regular_file(core)))]
    args = ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(core)]
    graphs, foreground, backgrounds = [], ['foreground0'], []

    def normalized(index, label, *, start=0, gain=None):
        chain = f'[{index}:a]'
        if gain is not None:
            chain += f'volume={gain:.12f},'
        if start:
            chain += f'adelay=delays={start}S:all=1,'
        chain += (f'apad=whole_len={info.frames},atrim=end_sample={info.frames},'
                  'aformat=sample_fmts=fltp:channel_layouts=stereo,asetnsamples=n=1024:p=1')
        graphs.append(chain + '[' + label + ']')

    normalized(0, foreground[0])
    for index, row in enumerate(rows, 1):
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            raise ValueError('A positioned clip needs its PCM, sample start and track.')
        path, start, track = row
        path = Path(path).absolute()
        if type(start) is not int or start < 0 or track not in ('sfx', 'ambience', 'music', 'microphone'):
            raise ValueError('Invalid clip sample start or track.')
        item = audio.wav_info(path)
        if item.rate != info.rate or item.channels != 2 or start + item.frames > info.frames:
            raise ValueError('Every clip must fit the fixed stereo chapter timeline.')
        inputs.append((path, file_identity(regular_file(path))))
        args.extend(('-protocol_whitelist', 'file', '-f', 'wav', '-i', str(path)))
        label = 'clip' + str(index)
        normalized(index, label, start=start)
        (backgrounds if track in ('ambience', 'music') else foreground).append(label)
    if music is not None:
        music = Path(music).absolute()
        item = audio.wav_info(music)
        number(music_gain, 0, .8, 'Music gain')
        if item.frames != info.frames or item.rate != info.rate:
            raise ValueError('Generated music must match the chapter sample timeline.')
        inputs.append((music, file_identity(regular_file(music))))
        args.extend(('-protocol_whitelist', 'file', '-f', 'wav', '-i', str(music)))
        normalized(len(inputs) - 1, 'generated', gain=music_gain)
        backgrounds.append('generated')

    def combine(labels, result):
        if len(labels) == 1:
            graphs.append('[' + labels[0] + ']anull[' + result + ']')
        else:
            graphs.append(''.join('[' + label + ']' for label in labels)
                          + f'amix=inputs={len(labels)}:duration=first:normalize=0[' + result + ']')

    combine(foreground, 'foreground')
    if backgrounds:
        combine(backgrounds, 'background')
        if duck:
            graphs.extend(('[foreground]asplit=2[front][side]',
                '[background][side]sidechaincompress=threshold=0.035:ratio=8:attack=10:release=200[bed]',
                '[front][bed]amix=inputs=2:duration=first:normalize=0[mixed]'))
        else:
            graphs.append('[foreground][background]amix=inputs=2:duration=first:normalize=0[mixed]')
    else:
        graphs.append('[foreground]anull[mixed]')
    graphs.append(f'[mixed]alimiter=limit=0.95:level=0:latency=1,apad=whole_len={info.frames},'
                  f'atrim=end_sample={info.frames}[out]')
    with tempfile.TemporaryDirectory(prefix='.clip-mix-', dir=output.parent) as temporary:
        target = Path(temporary) / 'scene.wav'
        audio.run_ffmpeg(ffmpeg, [*args, '-filter_complex', ';'.join(graphs), '-map', '[out]',
            *audio._wav_arguments(target, info.rate)], cancelled=cancelled)
        actual = audio.wav_info(target)
        if (actual.frames, actual.rate, actual.channels) != (info.frames, info.rate, 2):
            raise RuntimeError('Clip mixing changed the measured chapter sample timeline.')
        if any(file_identity(regular_file(path)) != stamp for path, stamp in inputs):
            raise RuntimeError('An audio input changed during clip mixing.')
        check_cancelled(cancelled)
        publish_noreplace(target, output, cancelled=cancelled)
    return output
