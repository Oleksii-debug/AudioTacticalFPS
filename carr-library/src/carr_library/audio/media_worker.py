"""Fixed local-media demux/encode operations on the existing owned FFmpeg runner.

No URL, playlist, external reference, user filter, executable argument or shell
is admitted. Full decoded output is checked before a result can be published.
"""
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time

from ..backup_files import local_path, regular
from .capture_process import run_process, wait_for_cleanup
from .capture_types import AudioBudget, AudioCleanupPending
from .ffmpeg import wav_info
from .jobs import file_identity, verified_reader
from .media_types import (AudioStream, CONTAINERS, ConversionPreset, MediaInspection,
                          MAX_SNAPSHOT_BYTES, output_name)
from .runtime import resolve_ffmpeg

WHITELIST = ','.join(CONTAINERS)
OUTPUT_CONTAINERS = {'wav': 'wav', 'flac': 'flac', 'mp3': 'mp3', 'ogg': 'ogg',
                     'opus': 'ogg', 'm4a': 'mov', 'm4b': 'mov', 'aiff': 'aiff',
                     'aac': 'aac', 'wma': 'asf'}


def check(cancelled):
    if cancelled():
        raise InterruptedError('Media conversion stopped; completed results are retained.')


def stream_hash(stream, cancelled):
    checksum = sha256()
    remaining = os.fstat(stream.fileno()).st_size
    if not 1 <= remaining <= MAX_SNAPSHOT_BYTES:
        raise ValueError('The selected media exceeds the supported local file budget.')
    while remaining:
        check(cancelled)
        part = stream.read(min(1024**2, remaining))
        if not part:
            raise ValueError('The selected media was truncated while verifying it.')
        checksum.update(part)
        remaining -= len(part)
    check(cancelled)
    return checksum.hexdigest()


@contextmanager
def processors(selected=None, *, expected=None, cancelled=lambda: False):
    binary = local_path(resolve_ffmpeg(selected), exists=True)
    probe = local_path(binary.with_name('ffprobe.exe' if binary.suffix.lower() == '.exe' else 'ffprobe'),
                       exists=True)
    # Both tools are from the same explicitly selected/installed runtime.
    # The existing bundled resolver verifies its pinned complete runtime first.
    with ExitStack() as stack:
        checksums = []
        for path in (binary, probe):
            regular(path)
            stream = stack.enter_context(verified_reader(path))
            checksums.append(stream_hash(stream, cancelled))
        values = tuple(checksums)
        if expected is not None and values != tuple(expected):
            raise ValueError('The installed media processor changed; prepare a new reviewed job.')
        yield binary, probe, values
    # Reader validation completes before callers publish a derived result.


def _run(command, *, cancelled, budget, watch=(), progress=None):
    try:
        return run_process([str(value) for value in command], cancelled=cancelled,
                           budget=budget, watch=watch, progress=progress, owned_tree=True)
    except AudioCleanupPending as error:
        # Keep input/read handles, private stages and resource admission alive
        # until the exact Windows Job/POSIX family has confirmed quiescence.
        wait_for_cleanup(error)
        raise


def _decode(raw):
    if len(raw) > 1024**2:
        raise ValueError('The media stream inventory exceeds its metadata budget.')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate media inventory field.')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('Nonfinite media inventory number.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid bounded media inventory.') from error


def _integer(value, label):
    if type(value) is int:
        return value
    if type(value) is str and re.fullmatch('[0-9]{1,12}', value):
        return int(value)
    raise ValueError('Invalid reported ' + label + '.')


def probe_media(probe, source, *, cancelled, budget):
    # The MOV options forbid reading an external data reference. Other admitted
    # demuxers contain their audio bytes; playlist/concat/network demuxers and
    # protocols are absent. FFprobe accepts these explicit demuxer options.
    command = [probe, '-v', 'error', '-max_alloc', str(budget.max_alloc_bytes),
        '-protocol_whitelist', 'file', '-format_whitelist', WHITELIST,
        '-enable_drefs', '0', '-use_absolute_path', '0', '-max_streams', '64',
        '-probesize', str(16 * 1024**2), '-analyzeduration', '10000000',
        '-show_entries', 'format=format_name,duration:stream=index,codec_type,codec_name,sample_rate,channels,duration,bits_per_sample,bits_per_raw_sample:stream_tags=language,title',
        '-of', 'json', source]
    value = _decode(_run(command, cancelled=cancelled, budget=budget).stdout)
    if (type(value) is not dict or type(value.get('format')) is not dict
            or type(value.get('streams')) is not list or not 1 <= len(value['streams']) <= 64):
        raise ValueError('Choose a local media file with a bounded stream inventory.')
    formats = value['format'].get('format_name')
    if type(formats) is not str or len(formats) > 256:
        raise ValueError('The media container has no supported identity.')
    names = formats.split(',')
    container = next((name for name in CONTAINERS if name in names), None)
    if container is None:
        raise ValueError('This local container is not supported by the media converter.')
    streams, has_video, indexes = [], False, set()
    for row in value['streams']:
        if type(row) is not dict:
            raise ValueError('Invalid reported media stream.')
        index = _integer(row.get('index'), 'stream index')
        if not 0 <= index <= 65535 or index in indexes:
            raise ValueError('Media stream identities must be distinct and bounded.')
        indexes.add(index)
        if row.get('codec_type') == 'video':
            has_video = True
        if row.get('codec_type') != 'audio':
            continue
        tags = row.get('tags', {})
        if type(tags) is not dict:
            raise ValueError('Invalid bounded audio tags.')
        duration = row.get('duration')
        if duration is None or duration == 'N/A':
            duration = value['format'].get('duration')
        try:
            duration = None if duration is None or duration == 'N/A' else float(duration)
        except (ValueError, OverflowError) as error:
            raise ValueError('Invalid reported audio duration.') from error
        bits = row.get('bits_per_raw_sample', row.get('bits_per_sample', 0))
        if bits in (None, 'N/A'):
            bits = 0
        streams.append(AudioStream(index, row.get('codec_name'),
            _integer(row.get('sample_rate'), 'sample rate'), _integer(row.get('channels'), 'channel count'),
            duration, tags.get('language', ''), tags.get('title', ''), _integer(bits, 'bit depth')))
    if not streams:
        raise ValueError('The selected file has no supported audio stream to convert.')
    return container, has_video, tuple(streams)


def inspect_file(path, selected=None, *, cancelled=lambda: False):
    path = local_path(path, exists=True)
    before = regular(path)
    budget = AudioBudget(timeout=30, max_log_bytes=1024**2, max_output_bytes=4096,
                         threads=1, max_alloc_bytes=64 * 1024**2)
    with processors(selected, cancelled=cancelled) as (_, probe, checksums):
        with verified_reader(path, expected_identity=file_identity(before)) as stream:
            checksum = stream_hash(stream, cancelled)
            container, video, streams = probe_media(probe, path, cancelled=cancelled, budget=budget)
        result = MediaInspection(str(path), before.st_size, checksum, file_identity(before),
                                 container, video, streams, checksums)
    return result


def input_options(container):
    values = ['-protocol_whitelist', 'file', '-format_whitelist', WHITELIST,
              '-max_streams', '64', '-probesize', str(16 * 1024**2),
              '-analyzeduration', '10000000', '-f', container]
    if container == 'mov':
        values += ['-enable_drefs', '0', '-use_absolute_path', '0']
    return values


def encoding_options(preset):
    if type(preset) is not ConversionPreset:
        raise ValueError('A typed media encoding preset is required.')
    codecs = {'wav': 'pcm_s%dle' % preset.pcm_bits, 'aiff': 'pcm_s%dbe' % preset.pcm_bits,
              'flac': 'flac', 'mp3': 'libmp3lame', 'ogg': 'libvorbis', 'opus': 'libopus',
              'm4a': 'aac', 'm4b': 'aac', 'aac': 'aac', 'wma': 'wmav2'}
    options = ['-c:a', codecs[preset.format], '-ar', str(preset.sample_rate), '-ac', str(preset.channels)]
    if preset.format == 'flac':
        options += ['-sample_fmt', 's16' if preset.pcm_bits == 16 else 's32',
                    '-bits_per_raw_sample', str(preset.pcm_bits)]
    if preset.format not in ('wav', 'aiff', 'flac'):
        options += ['-b:a', str(preset.bitrate_kbps) + 'k']
    for name in ('title', 'artist', 'album'):
        if getattr(preset, name):
            options += ['-metadata', name + '=' + getattr(preset, name)]
    if preset.format == 'wav':
        options += ['-rf64', 'auto']
    muxer = {'m4a': 'ipod', 'm4b': 'ipod', 'opus': 'opus', 'aac': 'adts', 'wma': 'asf'}.get(preset.format, preset.format)
    return options + ['-f', muxer]


def convert_item(job, definition, row, stage, selected=None, *, cancelled, budget, progress=None):
    started = time.perf_counter_ns()
    duration_ns = int(budget.timeout * 1_000_000_000)
    outer_cancelled = cancelled
    def bounded_cancelled():
        if time.perf_counter_ns() - started >= duration_ns:
            raise TimeoutError('The media item exceeded its selected time allowance.')
        return outer_cancelled()
    cancelled = bounded_cancelled
    check(cancelled)
    source = job / row['retained']
    before = regular(source)
    if before.st_size != row['bytes']:
        raise ValueError('A retained media source changed size.')
    expected = tuple(definition['processor'][name] for name in ('ffmpeg_sha256', 'ffprobe_sha256'))
    preset = ConversionPreset.from_dict(row['preset'])
    target = stage / output_name(row)
    verified = stage / 'verification.wav'
    with processors(selected, expected=expected, cancelled=cancelled) as (binary, probe, _):
        with verified_reader(source, expected_identity=file_identity(before)) as incoming:
            if stream_hash(incoming, cancelled) != row['sha256']:
                raise ValueError('A retained media source differs from its reviewed bytes.')
            common = [binary, '-hide_banner', '-nostdin', '-v', 'error', '-n',
                      '-max_alloc', str(budget.max_alloc_bytes), '-threads', str(budget.threads)]
            command = [*common, *input_options(row['container']), '-i', source,
                '-map', '0:' + str(row['stream_index']), '-vn', '-sn', '-dn',
                '-map_metadata', '-1', '-map_chapters', '-1', '-threads', str(budget.threads),
                *encoding_options(preset), '-progress', 'pipe:1', '-nostats', target]
            _run(command, cancelled=cancelled, budget=budget, watch=(stage,), progress=progress)
            # The result is closed and the complete owned family is gone before
            # another reader or validation process can inspect it.
            with target.open('r+b') as output:
                os.fsync(output.fileno())
            with verified_reader(target) as output:
                container, video, streams = probe_media(probe, target, cancelled=cancelled, budget=budget)
                if (video or len(streams) != 1 or container != OUTPUT_CONTAINERS[preset.format]
                        or streams[0].sample_rate != preset.sample_rate or streams[0].channels != preset.channels):
                    raise ValueError('The encoded output differs from the selected audio profile.')
                # Decode every sample, not only the media header. The temporary
                # verification PCM shares the declared job disk/time allowance.
                command = [*common, *input_options(container), '-i', target, '-map', '0:' + str(streams[0].index),
                    '-vn', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
                    '-c:a', 'pcm_s16le', '-f', 'wav', '-rf64', 'auto', verified]
                _run(command, cancelled=cancelled, budget=budget, watch=(stage,))
                pcm = wav_info(verified)
                if pcm.rate != preset.sample_rate or pcm.channels != preset.channels:
                    raise ValueError('Decoded audio does not match the selected sample format.')
                result = {'path': target.name, 'bytes': os.fstat(output.fileno()).st_size,
                    'sha256': stream_hash(output, cancelled), 'codec': streams[0].codec,
                    'sample_rate': pcm.rate, 'channels': pcm.channels,
                    'pcm_bits': streams[0].pcm_bits if preset.format in ('wav', 'aiff', 'flac') else 0,
                    'decoded_frames': pcm.frames}
            verified.unlink()
    # Both source and executable read leases have validated before publication.
    check(cancelled)
    return result
