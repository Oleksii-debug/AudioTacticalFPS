"""Fixed-timeline stereo mastering through the existing bounded FFmpeg owner."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from . import ffmpeg as audio
from .jobs import (check_cancelled, file_identity, publish_noreplace,
                   regular_file, verified_reader)
from .models import SAMPLE_RATES, master_chain, number


MASTER_REVISION = 1


def process_master(source, output, *, ffmpeg, effects=(), mix=1.0,
                   cancelled=lambda: False):
    """Blend the full processed programme without changing its sample timeline.

    This is linear dry/wet mixing, with no automatic gain normalization. The
    existing 0.95 safety limiter only reduces peaks beyond its ceiling. Tails
    are deliberately trimmed at the final measured programme boundary.
    """
    effects = master_chain(effects)
    number(mix, 0, 1, 'Master dry/wet mix')
    source, output = Path(source).absolute(), audio._destination(output)
    identity = file_identity(regular_file(source))
    with tempfile.TemporaryDirectory(prefix='.master-', dir=output.parent) as folder:
        stage = Path(folder) / 'master.wav'
        # Retain the existing immutable-input lease while the native process
        # reads by path. On Windows it excludes writers/renames, including a
        # same-size write whose timestamps or bytes are subsequently restored.
        with verified_reader(source, expected_identity=identity):
            info = audio.wav_info(source)
            if info.channels != 2 or info.frames < 1 or info.rate not in SAMPLE_RATES:
                raise ValueError('Master processing requires a nonempty stereo PCM programme.')
            selected = audio.effect_filters(effects, sample_rate=info.rate)
            base = 'aformat=sample_fmts=fltp:channel_layouts=stereo'
            aligned = f'aresample={info.rate},apad=whole_len={info.frames},atrim=end_sample={info.frames}'
            if mix == 0 or not selected:
                graph = f'[0:a]{base}[sum];'
            elif mix == 1:
                graph = f'[0:a]{base},' + ','.join(selected) + f',{aligned}[sum];'
            else:
                graph = (f'[0:a]{base},asplit=2[dry][wet_input];'
                         f'[dry]volume={1 - mix:.12g}[dry_level];'
                         '[wet_input]' + ','.join(selected) + f',{aligned},volume={mix:.12g}[wet_level];'
                         '[dry_level][wet_level]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[sum];')
            graph += (f'[sum]alimiter=limit=0.95:level=0:latency=1,'
                      f'{aligned}[out]')
            # The shared owner drains the whole child family before this
            # lease, private stage or outer resource reservation can unwind.
            audio.run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(source),
                '-filter_complex', graph, '-map', '[out]', *audio._wav_arguments(stage, info.rate)],
                cancelled=cancelled)
            result = audio.wav_info(stage)
            if (result.frames, result.rate, result.channels) != (info.frames, info.rate, 2):
                raise RuntimeError('Master processing changed the measured programme timeline.')
            if file_identity(regular_file(source)) != identity:
                raise RuntimeError('The assembled programme changed during master processing.')
        # A failed final input-lease validation must precede any publication.
        check_cancelled(cancelled)
        publish_noreplace(stage, output, cancelled=cancelled)
    return output


def split_master(source, boundaries, directory, *, cancelled=lambda: False):
    """Copy exact consecutive PCM ranges, without resampling or a second mix.

    The renderer calls this only in its private unpublished output directory.
    If any partition fails, the outer render transaction discards that tree.
    """
    source = Path(source).absolute()
    identity = file_identity(regular_file(source))
    info = audio.wav_info(source)
    if info.channels != 2:
        raise ValueError('Chapter exports require the stereo master.')
    if not isinstance(boundaries, (list, tuple)) or not 1 <= len(boundaries) <= 100000:
        raise ValueError('Choose a bounded, complete list of master chapter boundaries.')
    cursor, outputs = 0, []
    for index, pair in enumerate(boundaries, 1):
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(type(value) is not int for value in pair)
                or pair[0] != cursor or not cursor < pair[1] <= info.frames):
            raise ValueError('Chapter partitions must cover consecutive master frames exactly.')
        cursor = pair[1]
        outputs.append(audio._destination(Path(directory) / f'chapter-{index:06d}.wav'))
    if cursor != info.frames:
        raise ValueError('Chapter partitions must reach the end of the master.')
    with verified_reader(source, expected_identity=identity) as stream:
        with tempfile.TemporaryDirectory(prefix='.master-chapters-', dir=directory) as folder:
            stage = Path(folder) / 'chapter.wav'
            for output, (start, end) in zip(outputs, boundaries):
                check_cancelled(cancelled)
                stream.seek(info.data_offset + start * 4)
                with stage.open('xb') as destination:
                    audio._write_header(destination, end - start, info.rate, 2)
                    remaining = (end - start) * 4
                    while remaining:
                        check_cancelled(cancelled)
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise RuntimeError('The mastered programme was truncated during chapter export.')
                        destination.write(block)
                        remaining -= len(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                check_cancelled(cancelled)
                publish_noreplace(stage, output, cancelled=cancelled)
    return tuple(outputs)
