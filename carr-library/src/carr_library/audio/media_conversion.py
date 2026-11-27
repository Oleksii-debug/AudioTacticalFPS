"""Selected local media through the existing owned FFmpeg process boundary.

No ffprobe, PATH discovery, URL, playlist, shell argument, or imported runtime
setting is accepted. The reviewed demuxer and one explicit audio stream are
fixed before decoding. Completed output is decoded again before publication.
"""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
import struct

from .batch_models import (AUDIO_DEMUXERS, VIDEO_DEMUXERS, AudioPreset,
                           MAX_INPUT_BYTES, RuntimeIdentity, count)
from .cleanup import AudioCleanupPending, wait_for_cleanup
from .ffmpeg import wav_info
from .jobs import (check_cancelled, file_identity, regular_file, verified_reader,
                   sha256_file, checked_directory)
from .recording_process import CaptureBudget, run_process


def selected_file(value, kind):
    raw = str(value)
    if (raw.startswith(('//', '\\\\')) or '\x00' in raw
            or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw) and not re.match(r'^[A-Za-z]:[\\/]', raw)):
        raise ValueError('Select an ordinary local file; URLs and network paths are not audio inputs.')
    path = Path(value).absolute()
    demuxers = AUDIO_DEMUXERS if kind == 'audio' else VIDEO_DEMUXERS if kind == 'video' else {}
    if path.suffix.lower() not in demuxers:
        raise ValueError('This local container is outside the reviewed audio/video format list.')
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= MAX_INPUT_BYTES:
        raise ValueError('The selected media must be a bounded independent regular local file.')
    return path, demuxers[path.suffix.lower()]


def _run(args, *, budget, cancelled, watch=()):
    try:
        return run_process(args, budget=budget, cancelled=cancelled, watch=watch, owned_tree=True)
    except AudioCleanupPending as error:
        # Keep every input handle, stage directory and resource lease alive
        # until the existing native owner confirms descendant quiescence.
        wait_for_cleanup(error)
        raise


def runtime_identity(path, *, cancelled=lambda: False):
    if path is None:
        raise ValueError('Choose the installed FFmpeg executable explicitly.')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('The selected FFmpeg executable must have an absolute local path.')
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1:
        raise ValueError('The selected FFmpeg executable cannot be an aliased file.')
    expected = file_identity(info)
    with verified_reader(path, expected_identity=expected):
        binary_hash = sha256_file(path, cancelled=cancelled, expected_identity=expected)
        result = _run([str(path), '-hide_banner', '-nostdin', '-version'],
                      budget=CaptureBudget(timeout=10, max_output_bytes=65536,
                                           max_log_bytes=65536, reserve_disk_bytes=0),
                      cancelled=cancelled)
        output = result.stdout
        lines = output.decode('utf-8', 'strict').splitlines()
        if not lines or not lines[0].startswith('ffmpeg version ') or len(lines[0]) > 4096:
            raise ValueError('The explicit executable did not identify itself as FFmpeg.')
        if sha256_file(path, expected_identity=expected) != binary_hash:
            raise ValueError('The selected FFmpeg executable changed during inspection.')
    return RuntimeIdentity(binary_hash, lines[0], sha256(output).hexdigest())


@contextmanager
def selected_runtime(path, expected, *, cancelled=lambda: False):
    path = Path(path).absolute()
    if runtime_identity(path, cancelled=cancelled) != expected:
        raise ValueError('Resuming requires the exact reviewed FFmpeg bytes and version; create a new plan for another runtime.')
    original = file_identity(regular_file(path))
    with verified_reader(path, expected_identity=original):
        if sha256_file(path, expected_identity=original) != expected.sha256:
            raise ValueError('The selected FFmpeg changed before execution.')
        yield path
        if sha256_file(path, expected_identity=original) != expected.sha256:
            raise ValueError('The selected FFmpeg changed during execution.')


def reject_external_mov_references(path, *, cancelled=lambda: False):
    """Inspect bounded MOV/MP4 data references without following any of them."""
    containers = {b'moov', b'trak', b'mdia', b'minf', b'dinf', b'udta', b'meta'}
    forbidden = {b'rmra', b'rmda', b'rdrf', b'cmov'}
    seen = 0
    with verified_reader(path) as source:
        size = os.fstat(source.fileno()).st_size

        def header(position, end):
            if end - position < 8:
                raise ValueError('Truncated local MOV/MP4 box.')
            source.seek(position)
            raw = source.read(8)
            length, kind = struct.unpack('>I4s', raw)
            width = 8
            if length == 1:
                if end - position < 16:
                    raise ValueError('Truncated extended MOV/MP4 box.')
                length = struct.unpack('>Q', source.read(8))[0]
                width = 16
            elif length == 0:
                length = end - position
            if length < width or position + length > end:
                raise ValueError('A MOV/MP4 box exceeds its retained local input.')
            return kind, position + width, position + length

        def walk(start, end, depth):
            nonlocal seen
            if depth > 16:
                raise ValueError('The MOV/MP4 box nesting exceeds the review allowance.')
            cursor = start
            while cursor < end:
                check_cancelled(cancelled)
                seen += 1
                if seen > 100000:
                    raise ValueError('The MOV/MP4 box inventory exceeds the review allowance.')
                kind, body, stop = header(cursor, end)
                if kind in forbidden:
                    raise ValueError('Reference or compressed-header movies are not accepted.')
                if kind == b'dref':
                    if stop - body < 8:
                        raise ValueError('Truncated MOV/MP4 data references.')
                    source.seek(body)
                    version_flags, entries = struct.unpack('>II', source.read(8))
                    if version_flags != 0 or entries > 10000:
                        raise ValueError('Unsupported MOV/MP4 data-reference table.')
                    position = body + 8
                    for _ in range(entries):
                        entry_kind, data, after = header(position, stop)
                        if entry_kind != b'url ' or after - data != 4:
                            raise ValueError('External MOV/MP4 data references are refused.')
                        source.seek(data)
                        if source.read(4) != b'\x00\x00\x00\x01':
                            raise ValueError('External MOV/MP4 data references are refused.')
                        position = after
                    if position != stop:
                        raise ValueError('Ambiguous MOV/MP4 data references.')
                elif kind in containers:
                    child = body + (4 if kind == b'meta' else 0)
                    if child > stop:
                        raise ValueError('Truncated MOV/MP4 metadata.')
                    walk(child, stop, depth + 1)
                cursor = stop
        walk(0, size, 0)


def input_arguments(source, demuxer):
    if demuxer not in set(AUDIO_DEMUXERS.values()) | set(VIDEO_DEMUXERS.values()):
        raise ValueError('An explicit reviewed local demuxer is required.')
    arguments = ['-protocol_whitelist', 'file', '-format_whitelist', demuxer,
                 '-f', demuxer, '-probesize', '5000000', '-analyzeduration', '5000000']
    if demuxer == 'mov':
        arguments += ['-enable_drefs', '0', '-use_absolute_path', '0']
    return [*arguments, '-i', str(Path(source).absolute())]


def _budget(preset):
    return CaptureBudget(timeout=preset.timeout_seconds,
                         max_output_bytes=preset.pcm_budget * 2,
                         max_log_bytes=1024**2, reserve_disk_bytes=64 * 1024**2,
                         max_staging_files=16, max_alloc_bytes=256 * 1024**2, threads=1)


def _prefix(ffmpeg):
    return [str(Path(ffmpeg).absolute()), '-hide_banner', '-nostdin', '-loglevel', 'error',
            '-max_alloc', str(256 * 1024**2), '-threads', '1', '-filter_threads', '1']


def decode(source, target, *, demuxer, stream_index, ffmpeg, preset,
           cancelled=lambda: False, sample_bounds=None, verification=False):
    """Decode exactly one stream, refusing duration overflow instead of truncating."""
    if not isinstance(preset, AudioPreset):
        raise ValueError('Audio decoding requires an explicit typed preset.')
    count(stream_index, 'Selected audio stream', 0, 63)
    source, target = Path(source).absolute(), Path(target).absolute()
    checked_directory(target.parent)
    if os.path.lexists(target):
        raise FileExistsError('A staged audio filename already exists.')
    expected = file_identity(regular_file(source))
    with verified_reader(source, expected_identity=expected):
        if demuxer == 'mov':
            reject_external_mov_references(source, cancelled=cancelled)
        args = [*_prefix(ffmpeg), *input_arguments(source, demuxer),
                '-map', f'0:a:{stream_index}', '-vn', '-sn', '-dn',
                '-map_metadata', '-1', '-map_chapters', '-1']
        if sample_bounds is not None:
            if demuxer != 'wav' or type(sample_bounds) is not tuple or len(sample_bounds) != 2:
                raise ValueError('Chapter extraction requires exact source WAV sample bounds.')
            info = wav_info(source)
            first, last = sample_bounds
            count(first, 'Chapter first frame', 0, info.frames - 1)
            count(last, 'Chapter end frame', first + 1, info.frames)
            args += ['-af', f'atrim=start_sample={first}:end_sample={last},asetpts=PTS-STARTPTS']
        # One extra second is diagnostic headroom. If reached, the whole item
        # fails; a truncated partial result can never receive a completion receipt.
        limit = preset.max_seconds + (1 if verification else 0)
        args += ['-t', str(limit + 1), '-c:a', 'pcm_s16le', '-threads', '1',
                 '-ar', str(preset.sample_rate), '-ac', str(preset.channels),
                 '-f', 'wav', '-rf64', 'auto', '-n', str(target)]
        _run(args, budget=_budget(preset), cancelled=cancelled, watch=(target,))
        info = wav_info(target)
        if (info.rate != preset.sample_rate or info.channels != preset.channels
                or info.seconds > limit):
            raise ValueError('The input exceeds the selected duration/rate/channel allowance; no truncated result was accepted.')
        return info


def encode(source, target, *, ffmpeg, preset, cancelled=lambda: False):
    """Encode the already verified PCM with fixed current-project codecs."""
    source, target = Path(source).absolute(), Path(target).absolute()
    if os.path.lexists(target):
        raise FileExistsError('An encoded output filename already exists.')
    codecs = {'wav': ('pcm_s16le', 'wav'), 'flac': ('flac', 'flac'),
              'mp3': ('libmp3lame', 'mp3'), 'opus': ('libopus', 'opus'),
              'm4a': ('aac', 'ipod'), 'm4b': ('aac', 'ipod'),
              'aiff': ('pcm_s16be', 'aiff'), 'ogg': ('libvorbis', 'ogg'),
              'aac': ('aac', 'adts'), 'wma': ('wmav2', 'asf')}
    codec, container = codecs[preset.output_format]
    original = file_identity(regular_file(source))
    with verified_reader(source, expected_identity=original):
        info = wav_info(source)
        if info.rate != preset.sample_rate or info.channels != preset.channels:
            raise ValueError('The staged PCM differs from its selected preset.')
        args = [*_prefix(ffmpeg), *input_arguments(source, 'wav'), '-map', '0:a:0',
                '-vn', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
                '-c:a', codec, '-threads', '1', '-ar', str(preset.sample_rate), '-ac', str(preset.channels)]
        if preset.output_format not in ('wav', 'flac', 'aiff'):
            args += ['-b:a', str(preset.bitrate_kbps) + 'k']
        if preset.output_format == 'wav':
            args += ['-rf64', 'auto']
        args += ['-f', container, '-n', str(target)]
        _run(args, budget=_budget(preset), cancelled=cancelled, watch=(target,))
        if regular_file(target).st_size == 0:
            raise ValueError('The encoder produced an empty output.')


def verify_encoded(path, scratch, *, ffmpeg, preset, cancelled=lambda: False):
    path, scratch = Path(path), Path(scratch)
    expected = file_identity(regular_file(path))
    demuxer = AUDIO_DEMUXERS['.' + preset.output_format]
    with verified_reader(path, expected_identity=expected):
        info = decode(path, scratch, demuxer=demuxer, stream_index=0,
                      ffmpeg=ffmpeg, preset=preset, cancelled=cancelled, verification=True)
        result = {'name': path.name, 'bytes': regular_file(path).st_size,
                  'sha256': sha256_file(path, cancelled=cancelled, expected_identity=expected),
                  'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames}
    scratch.unlink()
    return result
