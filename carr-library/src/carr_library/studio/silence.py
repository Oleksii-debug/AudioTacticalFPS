"""Read-only long-silence inspection of a verified completed Studio production.

The bounded PCM scan is adapted from the existing reviewed PR210 implementation
(audio/ffmpeg.py d076e29); ownership/receipts use current ProductionService and
PublicationService. The current RIFF/RF64 PCM header reader is reused unchanged.
"""
from array import array
from fractions import Fraction
from hashlib import sha256
import math
import os
import re
import sys
import time
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled
from ..backup_compat.pcm import _pcm_header_stream
from ..durable_state import local_path, regular
from ..operations import ResourceCancelled, ResourceRequest
from .publishing import PublicationService, _read

_BLOCK = 256 * 1024
_MAX_INTERVALS = 1000


def silence_options(threshold_dbfs, minimum_seconds, max_intervals=1000):
    values = []
    for value, low, high, name in (
            (threshold_dbfs, -120, 0, 'Silence threshold in dBFS'),
            (minimum_seconds, 0, 86400, 'Minimum silence duration')):
        if type(value) not in (int, float):
            raise ValueError(name + ' must be a finite number.')
        try:
            value = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(name + ' must be a finite number.') from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(name + ' is outside the inspection range.')
        values.append(value)
    if values[1] <= 0:
        raise ValueError('Minimum silence duration must be positive.')
    if type(max_intervals) is not int or not 1 <= max_intervals <= _MAX_INTERVALS:
        raise ValueError('Retain between 1 and 1000 silence intervals.')
    return values[0], values[1], max_intervals


def _stamp(info):
    return tuple(getattr(info, name) for name in
                 ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _directory(path):
    value = regular(local_path(path, exists=True), directory=True)
    return value.st_dev, value.st_ino


def _observe(progress, message):
    try:
        progress(message)
    except Exception:
        pass  # A notification failure cannot alter measured audio or its receipt.


def measure_pcm_silence(path, expected, *, threshold_dbfs, minimum_seconds,
                        max_intervals=1000, check=lambda: None, progress=lambda message: None):
    """Scan every PCM16 frame, retaining a bounded set of exact half-open ranges."""
    threshold, seconds, maximum = silence_options(threshold_dbfs, minimum_seconds, max_intervals)
    check()
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size != expected['size']:
        raise ValueError('The retained master size changed.')
    identity = _stamp(before)
    threshold_pcm16 = math.floor(32768 * 10 ** (threshold / 20))
    intervals, count, qualifying_frames = [], 0, 0
    start, position = None, 0
    checksum = sha256()

    def finish(end):
        nonlocal start, count, qualifying_frames
        if start is not None and end - start >= minimum_frames:
            count += 1
            qualifying_frames += end - start
            if len(intervals) < maximum:
                intervals.append({'start_frame': start, 'end_frame': end, 'frames': end - start})
        start = None

    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    try:
        if _stamp(os.fstat(descriptor)) != identity or _stamp(regular(path)) != identity:
            raise ValueError('The retained master changed before inspection.')
        stream = os.fdopen(descriptor, 'rb')
        descriptor = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    with stream:
        rate, channels, frames, offset, size = _pcm_header_stream(stream, before.st_size, check=check)
        if _stamp(os.fstat(stream.fileno())) != identity or _stamp(regular(path)) != identity:
            raise ValueError('The retained master changed while its PCM header was read.')
        stream.seek(0)
        minimum = Fraction(str(seconds)) * rate
        minimum_frames = (minimum.numerator + minimum.denominator - 1) // minimum.denominator

        def hash_bytes(amount):
            while amount:
                check()
                block = stream.read(min(_BLOCK, amount))
                if not block:
                    raise ValueError('The retained master was truncated during inspection.')
                checksum.update(block)
                amount -= len(block)

        hash_bytes(offset)
        remaining = size
        frame_bytes = channels * 2
        block_bytes = max(frame_bytes, _BLOCK // frame_bytes * frame_bytes)
        last_report = time.monotonic()
        while remaining:
            check()
            block = stream.read(min(block_bytes, remaining))
            if not block or len(block) % frame_bytes:
                raise ValueError('The retained master is truncated or not frame aligned.')
            checksum.update(block)
            samples = array('h')
            samples.frombytes(block)
            if sys.byteorder != 'little':
                samples.byteswap()
            values = iter(samples)
            for first in values:
                quiet = abs(first) <= threshold_pcm16
                if channels == 2:
                    quiet = abs(next(values)) <= threshold_pcm16 and quiet
                if quiet:
                    if start is None:
                        start = position
                else:
                    finish(position)
                position += 1
            remaining -= len(block)
            now = time.monotonic()
            if now - last_report >= .5 or not remaining:
                _observe(progress, f'Inspected {position} of {frames} PCM frames.')
                last_report = now
        finish(position)
        hash_bytes(before.st_size - offset - size)
        check()
        if (position != frames or checksum.hexdigest() != expected['sha256']
                or _stamp(os.fstat(stream.fileno())) != identity
                or _stamp(regular(local_path(path, exists=True))) != identity):
            raise ValueError('The retained master changed during inspection.')
    check()
    return {'master_sha256': checksum.hexdigest(), 'sample_rate': rate, 'channels': channels,
        'frames': frames, 'duration_seconds': frames / rate,
        'criteria': {'threshold_dbfs': threshold, 'threshold_pcm16': threshold_pcm16,
            'minimum_seconds': seconds, 'minimum_frames': minimum_frames,
            'channel_rule': 'every-channel-at-or-below', 'frame_bounds': 'start-inclusive-end-exclusive'},
        'interval_count': count, 'retained_interval_count': len(intervals),
        'omitted_interval_count': count - len(intervals), 'qualifying_frames': qualifying_frames,
        'max_intervals': maximum, 'intervals': intervals}


def inspect_completed_silence(production, job_id, *, threshold_dbfs, minimum_seconds,
                              max_intervals=1000, budget=None, cancelled=lambda: False,
                              progress=lambda message: None):
    """Inspect existing receipts and PCM only; never render, play, repair or save a job."""
    from .render import ProductionService
    if not isinstance(production, ProductionService):
        raise TypeError('Use the current Studio production service.')
    silence_options(threshold_dbfs, minimum_seconds, max_intervals)
    budget = budget or AudioBudget(max_output_bytes=production.total_disk_bytes)
    if not isinstance(budget, AudioBudget):
        raise TypeError('Use the shared audio budget for inspection.')
    started, lease = time.monotonic(), None
    root_identity = _directory(production.root)

    def stopping():
        return production._closed.is_set() or cancelled()

    def check():
        if stopping():
            raise AudioCancelled('Silence inspection cancelled. Completed audio is unchanged.')
        if time.monotonic() - started > budget.timeout:
            raise TimeoutError('Silence inspection exceeded its selected time budget.')
        if lease is not None:
            lease.checkpoint()
        if _directory(production.root) != root_identity:
            raise ValueError('The selected Studio root changed during inspection.')

    check()
    state = production.get(job_id)
    if state.get('status') != 'complete':
        raise ValueError('Select a completed Studio job for silence inspection.')
    destination = local_path(state['destination'], exists=True)
    destination_identity = _directory(destination)
    publisher = PublicationService(production, budget=budget)
    request = ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024 ** 2)
    try:
        broker = production._admission_broker(stopping)
        with broker.reserve(request, operation_id='silence-inspection:' + uuid4().hex,
                            cancelled=stopping, timeout=min(60, budget.timeout),
                            run_timeout=budget.timeout) as selected_lease:
            lease = selected_lease
            check()
            project, timeline, paths = publisher._source(
                state, check, lambda message: _observe(progress, message))
            master, receipt = paths['master.wav']
            _observe(progress, 'Scanning the completed PCM master with the explicit silence criteria.')
            measured = measure_pcm_silence(master, receipt, threshold_dbfs=threshold_dbfs,
                minimum_seconds=minimum_seconds, max_intervals=max_intervals,
                check=check, progress=progress)
            if (measured['sample_rate'] != timeline['sample_rate']
                    or measured['channels'] != timeline['channels']
                    or abs(measured['duration_seconds'] - timeline['duration'])
                       > max(2 / measured['sample_rate'], len(timeline['chapters']) * 1e-6)):
                raise ValueError('The retained PCM and measured project timeline differ.')
            # Recheck the exact receipt-bearing metadata and current job after the
            # full scan. Their original source identities are provenance only.
            for name in ('project.json', 'timeline.json'):
                path, record = paths[name]
                _read(path, record, check)
            check()
            if production.get(job_id) != state or _directory(destination) != destination_identity:
                raise ValueError('The completed production changed during inspection.')
            result = {'schema': 'resonance-silence-inspection/1', 'status': 'complete',
                'review_status': 'warnings' if measured['interval_count'] else 'no-matching-intervals',
                'job': state['id'], 'project_fingerprint': project.fingerprint,
                'source_sha256': timeline['source_sha256'],
                'timeline_sha256': paths['timeline.json'][1]['sha256'], **measured,
                'limitations': [
                    'Amplitude and duration are selected criteria, not a quality verdict.',
                    'The complete master was scanned; only the listed number of intervals is retained.',
                    'Quiet spans can be intentional pauses. Listen and review before changing the project.',
                    'No speech recognition, intended-silence inference or acoustic text-accuracy claim is made.',
                    'Recorded source hashes are provenance; this inspection does not reopen external originals.',
                    'Completed audio, source text, render state and checksums are unchanged.']}
            check()
        lease = None
        check()
        return result
    except ResourceCancelled as exc:
        raise AudioCancelled('Silence inspection cancelled while waiting for shared resources.') from exc


def silence_report_text(report):
    if (type(report) is not dict or report.get('schema') != 'resonance-silence-inspection/1'
            or report.get('status') != 'complete'):
        raise ValueError('Use a completed silence inspection report.')
    for key in ('master_sha256', 'timeline_sha256', 'source_sha256', 'project_fingerprint'):
        if type(report.get(key)) is not str or re.fullmatch('[0-9a-f]{64}', report[key]) is None:
            raise ValueError('The silence report has an invalid source identity.')
    criteria, intervals = report.get('criteria'), report.get('intervals')
    if type(criteria) is not dict or type(intervals) is not list:
        raise ValueError('The silence report has invalid criteria or intervals.')
    threshold, minimum, maximum = silence_options(criteria.get('threshold_dbfs'),
                                                   criteria.get('minimum_seconds'), report.get('max_intervals'))
    rate, frames, channels = report.get('sample_rate'), report.get('frames'), report.get('channels')
    if (type(rate) is not int or not 1 <= rate <= 768000
            or type(frames) is not int or not 1 <= frames < 2 ** 63
            or type(channels) is not int or channels not in (1, 2) or len(intervals) > maximum):
        raise ValueError('The silence report has invalid PCM bounds.')
    span = Fraction(str(minimum)) * rate
    minimum_frames = (span.numerator + span.denominator - 1) // span.denominator
    if (type(criteria.get('minimum_frames')) is not int or criteria['minimum_frames'] != minimum_frames
            or type(criteria.get('threshold_pcm16')) is not int
            or criteria['threshold_pcm16'] != math.floor(32768 * 10 ** (threshold / 20))
            or criteria.get('channel_rule') != 'every-channel-at-or-below'
            or criteria.get('frame_bounds') != 'start-inclusive-end-exclusive'):
        raise ValueError('The silence report criteria differ from their numeric bounds.')
    counts = [report.get(key) for key in ('interval_count', 'retained_interval_count',
                                         'omitted_interval_count', 'qualifying_frames')]
    if (any(type(value) is not int or value < 0 for value in counts)
            or counts[1] != len(intervals) or counts[0] != counts[1] + counts[2]
            or counts[0] > frames or counts[3] > frames):
        raise ValueError('The silence report counts are inconsistent.')
    limitations = report.get('limitations')
    if (type(limitations) is not list or len(limitations) > 10
            or any(type(value) is not str or len(value) > 512 for value in limitations)):
        raise ValueError('The silence report exceeds its notice budget.')
    lines = ['Completed audio — silence inspection',
        'Review warnings only; the completed production is unchanged.',
        f'Every channel at or below {threshold:g} dBFS for at least {minimum:g} seconds.',
        f'PCM: {frames} frames at {rate} Hz; {channels} channels.',
        f'Matching intervals: {counts[0]}; shown: {counts[1]}; omitted: {counts[2]}.',
        'Ranges have an inclusive start and exclusive end. Quiet spans can be intentional pauses.']
    previous, retained_frames = 0, 0
    for index, interval in enumerate(intervals, 1):
        if type(interval) is not dict or set(interval) != {'start_frame', 'end_frame', 'frames'}:
            raise ValueError('The silence report has an invalid interval.')
        begin, end, length = interval['start_frame'], interval['end_frame'], interval['frames']
        if (any(type(value) is not int for value in (begin, end, length))
                or begin < previous or not begin < end <= frames or length != end - begin
                or length < minimum_frames):
            raise ValueError('The silence report has invalid interval bounds.')
        previous, retained_frames = end, retained_frames + length
        lines.append(f'{index}. Frames [{begin}, {end}); {begin / rate:.6f}–{end / rate:.6f} seconds.')
    if retained_frames > counts[3] or (counts[2] == 0 and retained_frames != counts[3]):
        raise ValueError('The silence report intervals differ from their counts.')
    lines.extend(['', 'Master SHA256: ' + report['master_sha256'],
                  'Timeline SHA256: ' + report['timeline_sha256'], *limitations])
    return '\n'.join(lines) + '\n'
