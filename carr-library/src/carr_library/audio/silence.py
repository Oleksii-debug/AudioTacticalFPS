"""Explicit, warning-only long-silence measurement of actual integer PCM.

FFmpeg decodes RIFF or RF64 into a private signed-32-bit stream without changing
the sample rate or channel count. Extending 8/16/24-bit integer samples to 32 bits
preserves their precision; floating-point and compressed sources are refused.
Every channel in a frame must meet the selected threshold. This is amplitude
measurement, not speech detection, transcription validation or listening QA.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR, localcontext
from fractions import Fraction
from hashlib import sha256
import os
from pathlib import Path
import shutil
import struct
import time

from .cleanup import retained_context, retained_temporary_directory
from .models import AudioBudget, AudioCancelled, AudioError, finite, integer
from .process import run_process
from .recording import _file_identity, _verified_reader
from ..durable_state import local_path, regular

MAX_SILENCE_INTERVALS = 4096
_BLOCK_BYTES = 1024 * 1024
_PCM_WIDTHS = {'pcm_u8': 1, 'pcm_s16le': 2, 'pcm_s24le': 3, 'pcm_s32le': 4}
_NOTICE = ('Intervals at or below the selected amplitude threshold are warnings for review. '
           'They may be intentional pauses; this is not speech detection or a claim '
           'about intelligibility, word accuracy, clipping or listening quality.')


@dataclass(frozen=True)
class SilenceCriteria:
    threshold_dbfs: float
    minimum_seconds: float

    def __post_init__(self):
        threshold = finite(self.threshold_dbfs, 'Silence threshold in dBFS', -192, 0)
        duration = finite(self.minimum_seconds, 'Minimum silence duration', 0, 365 * 86400)
        if duration <= 0:
            raise ValueError('Minimum silence duration must be greater than zero.')
        object.__setattr__(self, 'threshold_dbfs', threshold)
        object.__setattr__(self, 'minimum_seconds', duration)

    def minimum_frames(self, sample_rate):
        integer(sample_rate, 'Measured sample rate', 1000, 768000)
        # str(float) is the submitted number's shortest round-trip decimal.
        # Ceil this exact decimal fraction, not an approximate binary product.
        count = Fraction(str(self.minimum_seconds)) * sample_rate
        return (count.numerator + count.denominator - 1) // count.denominator

    @property
    def amplitude_limit_s32(self):
        # The PCM samples themselves never pass through floating point.
        with localcontext() as context:
            context.prec = 64
            ratio = Decimal(10) ** (Decimal(str(self.threshold_dbfs)) / Decimal(20))
            return int((Decimal(2**31) * ratio).to_integral_value(rounding=ROUND_FLOOR))

    def to_dict(self, sample_rate):
        return {'threshold_dbfs': self.threshold_dbfs,
                'minimum_seconds': self.minimum_seconds,
                'minimum_frames': self.minimum_frames(sample_rate),
                'amplitude_limit_s32': self.amplitude_limit_s32,
                'channel_policy': 'every-channel', 'comparison': 'absolute-sample-at-or-below'}


def _hash_stream(stream, size, check):
    stream.seek(0)
    remaining, digest = size, sha256()
    while remaining:
        check()
        block = stream.read(min(_BLOCK_BYTES, remaining))
        check()
        if not block:
            raise AudioError('The inspected PCM source was truncated.')
        digest.update(block)
        remaining -= len(block)
    check()
    if stream.read(1):
        raise AudioError('The inspected PCM source grew.')
    check()
    return digest.hexdigest()


def _scan_pcm(stream, *, frames, channels, rate, criteria, check):
    """Read bounded signed-32-bit blocks and return exact half-open intervals."""
    width = channels * 4
    expected = frames * width
    if os.fstat(stream.fileno()).st_size != expected:
        raise AudioError('Decoded PCM size differs from the exact measured frame count.')
    minimum, limit = criteria.minimum_frames(rate), criteria.amplitude_limit_s32
    intervals, count, start, cursor = [], 0, None, 0
    digest = sha256()

    def finish(end):
        nonlocal count, start
        if start is not None and end - start >= minimum:
            count += 1
            if len(intervals) < MAX_SILENCE_INTERVALS:
                intervals.append({'start_frame': start, 'end_frame': end,
                    'start_seconds': start / rate, 'end_seconds': end / rate,
                    'duration_seconds': (end - start) / rate})
        start = None

    while cursor < frames:
        check()
        size = min(frames - cursor, _BLOCK_BYTES // width) * width
        block = stream.read(size)
        check()
        if len(block) != size or len(block) % width:
            raise AudioError('Decoded PCM is truncated or not aligned to complete frames.')
        digest.update(block)
        samples = struct.iter_unpack('<' + 'i' * channels, block)
        for values in samples:
            if cursor % 4096 == 0:
                check()
            quiet = all(abs(value) <= limit for value in values)
            if quiet:
                if start is None:
                    start = cursor
            else:
                finish(cursor)
            cursor += 1
    finish(cursor)
    check()
    if stream.read(1):
        raise AudioError('Decoded PCM has extra frames or trailing bytes.')
    check()
    return intervals, count, digest.hexdigest()



def runtime_for_inspection(directory, *, state_root, input_roots=(), budget=None,
                           cancelled=lambda: False, runtime_factory=None):
    """Construct PCM-only tools after the caller has acquired its shared lease.

    Reuse the selected settings' restore/seal checks, but do not configure or
    inspect unrelated music models, voice providers or SoundFonts.
    """
    from .dsp import AudioRuntime
    budget = AudioBudget() if budget is None else budget
    if not isinstance(budget, AudioBudget):
        raise TypeError('Silence inspection requires an AudioBudget.')
    if cancelled():
        raise AudioCancelled('Silence inspection cancelled before runtime validation.')
    if runtime_factory is None:
        from ..runtime_settings import _enabled_settings
        value = _enabled_settings(state_root, cancelled=cancelled)
        runtime = AudioRuntime(directory, ffmpeg=value['ffmpeg'], ffprobe=value['ffprobe'],
                               input_roots=input_roots, budget=budget)
    else:
        runtime = runtime_factory(directory, input_roots=input_roots)
    if not isinstance(runtime, AudioRuntime) or not isinstance(runtime.budget, AudioBudget):
        raise TypeError('Silence inspection requires the existing typed PCM runtime.')
    actual = runtime.budget
    if (actual.threads > budget.threads or actual.max_alloc_bytes > budget.max_alloc_bytes
            or actual.max_output_bytes > budget.max_output_bytes
            or actual.max_log_bytes > budget.max_log_bytes or actual.timeout > budget.timeout
            or actual.max_staging_files > budget.max_staging_files):
        raise ValueError('The inspection runtime exceeds the admitted resource budget.')
    if cancelled():
        raise AudioCancelled('Silence inspection cancelled during runtime validation.')
    return runtime


def inspect_silence(runtime, source, *, threshold_dbfs, minimum_seconds,
                    cancelled=lambda: False, progress=None, deadline=None):
    """Measure without publishing, changing a cache node or editing the source."""
    started = time.monotonic()
    criteria = SilenceCriteria(threshold_dbfs, minimum_seconds)
    maximum_deadline = started + runtime.budget.timeout
    deadline = (maximum_deadline if deadline is None else
                min(finite(deadline, 'Silence inspection deadline'), maximum_deadline))

    def check():
        if cancelled():
            raise AudioCancelled('Silence inspection cancelled.')
        if time.monotonic() >= deadline:
            raise AudioError('Silence inspection exceeded its total time budget.')
        return False

    def phase(message):
        check()
        if progress is not None:
            progress(message)
        check()

    check()
    source = runtime._input(source)
    # Keep the same verified input handle alive through native decoding and all
    # final byte checks, including deferred native cleanup on either error chain.
    with retained_context(_verified_reader(source)) as source_stream:
        source_stamp = _file_identity(os.fstat(source_stream.fileno()))
        phase('Verifying the selected PCM source.')
        source_digest = _hash_stream(source_stream, source_stamp[2], check)
        runtime._inspection_cache.pop(source, None)
        info = runtime.inspect(source, cancelled=check)
        if ('wav' not in info.format.split(',') or info.codec not in _PCM_WIDTHS
                or type(info.frames) is not int or info.frames <= 0
                or info.channels not in (1, 2)):
            raise AudioError('Silence inspection requires measurable mono or stereo integer PCM WAV or RF64.')
        integer(info.sample_rate, 'Measured sample rate', 1000, 768000)
        expected = info.frames * info.channels * 4
        if expected > source_stamp[2] * 4:
            raise AudioError('Measured PCM frames exceed the bounded source byte inventory.')
        if expected > runtime.budget.max_output_bytes:
            raise AudioError('Decoded silence-inspection PCM exceeds the configured staging byte budget.')
        if shutil.disk_usage(runtime.root).free < expected + runtime.budget.reserve_disk_bytes:
            raise AudioError('Not enough free disk for silence inspection and the configured reserve.')
        check()
        with retained_temporary_directory(prefix='.carr-silence-', dir=runtime.root) as temporary:
            stage = Path(temporary)
            raw = stage / 'decoded.s32le'
            phase('Decoding the same PCM frames without resampling or downmixing.')
            remaining = deadline - time.monotonic()
            check()
            # No -ar, -ac, filter, normalization, trim or duration limit: an
            # extra/missing decoded frame must fail instead of being concealed.
            run_process([*runtime._base_args(), '-protocol_whitelist', 'file',
                '-format_whitelist', 'wav', '-i', str(source), '-map', '0:a:0',
                '-vn', '-sn', '-dn', '-map_metadata', '-1', '-map_chapters', '-1',
                '-c:a', 'pcm_s32le', '-f', 's32le', '-bitexact', str(raw)],
                budget=replace(runtime.budget, timeout=max(.05, min(runtime.budget.timeout, remaining))),
                cancelled=check, watch=(stage,), owned_tree=True)
            phase('Measuring intervals at or below the threshold in every channel.')
            with retained_context(_verified_reader(raw)) as pcm:
                intervals, count, pcm_digest = _scan_pcm(pcm, frames=info.frames,
                    channels=info.channels, rate=info.sample_rate, criteria=criteria, check=check)
            phase('Rechecking the original PCM bytes and source identity.')
            if (_hash_stream(source_stream, source_stamp[2], check) != source_digest
                    or _file_identity(os.fstat(source_stream.fileno())) != source_stamp
                    or _file_identity(regular(local_path(source, exists=True))) != source_stamp):
                raise AudioError('The inspected PCM source changed during measurement.')
            check()
            result = {'schema': 'carr-silence-inspection/1', 'status': 'complete',
                'source': {'artifact': source.name, 'sha256': source_digest, 'size': source_stamp[2]},
                'audio': {'sample_rate': info.sample_rate, 'channels': info.channels,
                          'frames': info.frames, 'source_codec': info.codec},
                'criteria': criteria.to_dict(info.sample_rate),
                'intervals': intervals, 'interval_count': count,
                'omitted_count': count - len(intervals), 'pcm_sha256': pcm_digest,
                'notice': _NOTICE}
    check()
    return result
