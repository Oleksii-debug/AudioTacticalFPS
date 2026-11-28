"""Actual PCM/RF64 silence intervals, precision, cancellation and native ownership."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioError, AudioRuntime
from carr_library.audio import silence
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.process import owned_process


pytestmark = pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
                                reason='Actual local FFmpeg and ffprobe are required.')
RATE = 8000


def pcm_file(path, frames, *, width=2, channels=1, rf64=False):
    raw = bytearray()
    for frame in frames:
        values = (frame,) if channels == 1 else frame
        assert len(values) == channels
        for value in values:
            raw.extend((value + 128).to_bytes(1, 'little') if width == 1
                       else value.to_bytes(width, 'little', signed=True))
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((channels, width, RATE, 0, 'NONE', 'PCM'))
        writer.writeframes(raw)
    if rf64:
        original = path.read_bytes()
        payload = original[12:]
        offset = payload.index(b'data')
        payload = payload[:offset + 4] + b'\xff' * 4 + payload[offset + 8:]
        size = 12 + 8 + 28 + len(payload)
        header = b'RF64' + b'\xff' * 4 + b'WAVE'
        ds64 = b'ds64' + struct.pack('<IQQQI', 28, size - 8, len(raw), len(frames), 0)
        path.write_bytes(header + ds64 + payload)
    return path


@pytest.fixture
def runtime(tmp_path):
    return AudioRuntime(tmp_path, budget=AudioBudget(
        timeout=30, threads=1, max_output_bytes=16 * 1024**2, reserve_disk_bytes=0))


def intervals(report):
    return [(row['start_frame'], row['end_frame']) for row in report['intervals']]


@pytest.mark.parametrize('width', [1, 2, 3, 4])
@pytest.mark.parametrize('rf64', [False, True], ids=['riff', 'rf64'])
def test_actual_integer_pcm_keeps_exact_frames_channels_and_source(runtime, tmp_path, width, rf64):
    loud = 1 << (width * 8 - 3)
    frames = [(0, 0)] * 400 + [(loud, -loud)] * 80
    frames += [(0, 0)] * 800 + [(loud, 0)] * 40 + [(0, 0)] * 481
    source = pcm_file(tmp_path / 'original.wav', frames, width=width, channels=2, rf64=rf64)
    before, stamp = source.read_bytes(), source.stat()
    phases = []
    report = runtime.inspect_silence(source, threshold_dbfs=-90, minimum_seconds=.05,
                                     progress=phases.append)
    assert report['status'] == 'complete'
    assert report['audio']['frames'] == len(frames)
    assert report['audio']['sample_rate'] == RATE and report['audio']['channels'] == 2
    assert intervals(report) == [(0, 400), (480, 1280), (1320, 1801)]
    assert report['interval_count'] == 3 and report['omitted_count'] == 0
    assert report['criteria']['minimum_frames'] == 400
    expected = b''.join(struct.pack('<ii', left << (32 - width * 8), right << (32 - width * 8))
                        for left, right in frames)
    assert report['pcm_sha256'] == sha256(expected).hexdigest()
    assert report['source']['sha256'] == sha256(before).hexdigest()
    assert source.read_bytes() == before and source.stat().st_mtime_ns == stamp.st_mtime_ns
    assert phases and all(isinstance(value, str) for value in phases)
    assert 'not speech detection' in report['notice']
    assert not list(tmp_path.glob('.carr-silence-*'))


@pytest.mark.parametrize('width', [3, 4])
def test_lowest_integer_bits_are_not_downquantized_into_silence(runtime, tmp_path, width):
    frames = [0] * 3 + [1] * 4 + [0] * 5 + [-1] * 3
    path = pcm_file(tmp_path / 'least-significant-bit.wav', frames, width=width)
    report = runtime.inspect_silence(path, threshold_dbfs=-192, minimum_seconds=1 / RATE)
    assert report['criteria']['amplitude_limit_s32'] == 0
    assert intervals(report) == [(0, 3), (7, 12)]
    expected = b''.join(struct.pack('<i', value << (32 - width * 8)) for value in frames)
    assert report['pcm_sha256'] == sha256(expected).hexdigest()


def test_exact_decimal_minimum_ceil_and_trailing_frame_interval(runtime, tmp_path):
    source = pcm_file(tmp_path / 'ceil.wav', [0, 3000, 0, 0, 3000, 0, 0, 0])
    report = runtime.inspect_silence(source, threshold_dbfs=-100, minimum_seconds=.0001251)
    assert report['criteria']['minimum_frames'] == 2
    assert intervals(report) == [(2, 4), (5, 8)]
    assert report['intervals'][-1]['end_seconds'] == 8 / RATE


def test_no_matching_interval_is_only_the_selected_measurement(runtime, tmp_path):
    source = pcm_file(tmp_path / 'audible.wav', [5000, -5000] * 100)
    report = runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert report['intervals'] == [] and report['interval_count'] == report['omitted_count'] == 0
    assert 'quality_pass' not in report and 'not speech detection' in report['notice']


def test_zero_dbfs_includes_both_signed_full_scale_limits(runtime, tmp_path):
    source = pcm_file(tmp_path / 'full-scale.wav', [-2**31, 2**31 - 1] * 20, width=4)
    report = runtime.inspect_silence(source, threshold_dbfs=0, minimum_seconds=.001)
    assert report['criteria']['amplitude_limit_s32'] == 2**31
    assert report['criteria']['comparison'] == 'absolute-sample-at-or-below'
    assert intervals(report) == [(0, 40)]


def test_interval_retention_is_bounded_but_total_count_includes_omitted(runtime, tmp_path):
    count = silence.MAX_SILENCE_INTERVALS + 2
    source = pcm_file(tmp_path / 'many-intervals.wav', [0, 1] * count, width=4)
    report = runtime.inspect_silence(source, threshold_dbfs=-192, minimum_seconds=1 / RATE)
    assert len(report['intervals']) == silence.MAX_SILENCE_INTERVALS
    assert report['interval_count'] == count and report['omitted_count'] == 2
    assert intervals(report)[-1] == (2 * (silence.MAX_SILENCE_INTERVALS - 1),
                                    2 * silence.MAX_SILENCE_INTERVALS - 1)


@pytest.mark.parametrize('threshold,duration', [
    (True, 1), ('-50', 1), (float('nan'), 1), (float('inf'), 1),
    (-193, 1), (.1, 1), (-50, False), (-50, 0), (-50, -1),
    (-50, float('nan')), (-50, float('inf')), (-50, 365 * 86400 + 1),
])
def test_invalid_explicit_criteria_refuse_before_source_or_native_access(runtime, monkeypatch, threshold, duration):
    monkeypatch.setattr(runtime, '_input', lambda path: pytest.fail('Invalid criteria reached the source.'))
    monkeypatch.setattr(silence, 'run_process', lambda *a, **k: pytest.fail('Invalid criteria reached FFmpeg.'))
    with pytest.raises(ValueError):
        runtime.inspect_silence('never.wav', threshold_dbfs=threshold, minimum_seconds=duration)


@pytest.mark.parametrize('delta', [-4, -1, 1, 4])
def test_actual_decoded_missing_extra_or_misaligned_bytes_refuse_report(runtime, tmp_path, monkeypatch, delta):
    source = pcm_file(tmp_path / 'original.wav', [0] * 100)
    before = source.read_bytes()
    actual, decoded = silence.run_process, []

    def corrupt(args, **kwargs):
        result = actual(args, **kwargs)
        target = Path(args[-1])
        decoded.append(target)
        content = target.read_bytes()
        target.write_bytes(content[:delta] if delta < 0 else content + b'\0' * delta)
        return result

    monkeypatch.setattr(silence, 'run_process', corrupt)
    with pytest.raises(AudioError, match='Decoded PCM'):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert len(decoded) == 1 and not decoded[0].parent.exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize('phase', ['scan', 'post-hash'])
def test_total_deadline_still_applies_after_real_decoder_exit(runtime, tmp_path, monkeypatch, phase):
    source = pcm_file(tmp_path / 'source.wav', [0] * 100)
    offset, calls = [0], []
    actual_time = time.monotonic
    monkeypatch.setattr(silence, 'time', SimpleNamespace(monotonic=lambda: actual_time() + offset[0]))
    if phase == 'scan':
        actual = silence._scan_pcm
        def scan(*args, **kwargs):
            calls.append('decoded')
            offset[0] = runtime.budget.timeout + 1
            return actual(*args, **kwargs)
        monkeypatch.setattr(silence, '_scan_pcm', scan)
    else:
        actual = silence._hash_stream
        def hashed(*args, **kwargs):
            result = actual(*args, **kwargs)
            calls.append(result)
            if len(calls) == 2:
                offset[0] = runtime.budget.timeout + 1
            return result
        monkeypatch.setattr(silence, '_hash_stream', hashed)
    with pytest.raises(AudioError, match='total time budget'):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert calls and not list(tmp_path.glob('.carr-silence-*'))


def test_cancellation_inside_python_scan_follows_actual_decode(runtime, tmp_path, monkeypatch):
    source = pcm_file(tmp_path / 'source.wav', [0] * 20000)
    stopped, observed = threading.Event(), []
    actual = silence._scan_pcm
    def scan(stream, **kwargs):
        observed.append(os.fstat(stream.fileno()).st_size)
        stopped.set()
        return actual(stream, **kwargs)
    monkeypatch.setattr(silence, '_scan_pcm', scan)
    with pytest.raises(AudioCancelled):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001,
                                 cancelled=stopped.is_set)
    assert observed == [80000] and source.is_file()
    assert not list(tmp_path.glob('.carr-silence-*'))


def test_source_write_during_actual_decode_is_refused_or_detected(runtime, tmp_path, monkeypatch):
    source = pcm_file(tmp_path / 'source.wav', [0] * 100)
    before, attempted = source.read_bytes(), []
    actual = silence.run_process
    def mutate(args, **kwargs):
        result = actual(args, **kwargs)
        attempted.append(True)
        # The existing Windows verified reader denies write sharing. POSIX
        # permits the mutation, so the final captured-size/full-hash check must
        # reject it. Neither platform may return a successful inspection.
        with source.open('r+b') as writer:
            writer.seek(-2, os.SEEK_END)
            writer.write(b'\x01\x00')
        return result
    monkeypatch.setattr(silence, 'run_process', mutate)
    with pytest.raises((AudioError, ValueError, PermissionError)):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert attempted == [True] and source.is_file()
    if os.name == 'nt':
        assert source.read_bytes() == before
    assert not list(tmp_path.glob('.carr-silence-*'))


def test_decoder_staging_budget_refuses_before_ffmpeg(runtime, tmp_path, monkeypatch):
    source = pcm_file(tmp_path / 'source.wav', [0] * 3000)
    runtime.budget = replace(runtime.budget, max_output_bytes=4096)
    monkeypatch.setattr(silence, 'run_process', lambda *a, **k: pytest.fail('Oversized decode launched.'))
    with pytest.raises(AudioError, match='staging byte budget'):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert source.is_file() and not list(tmp_path.glob('.carr-silence-*'))


@pytest.mark.parametrize('masked', [False, True], ids=['direct', 'masked'])
def test_actual_owned_child_keeps_decoded_stage_and_verified_source_until_quiet(runtime, tmp_path, monkeypatch, masked):
    source = pcm_file(tmp_path / 'source.wav', [0] * 100)
    before, original = source.read_bytes(), silence.run_process
    permit = threading.Event()
    observed = {}

    def pending(args, **kwargs):
        result = original(args, **kwargs)
        raw = Path(args[-1])
        ready = tmp_path / 'actual-child-ready'
        code = ('import pathlib,time\n'
                f'stream=open({str(raw)!r}, "rb")\n'
                f'pathlib.Path({str(ready)!r}).write_bytes(b"ready")\n'
                'time.sleep(60)\n')
        context = owned_process([sys.executable, '-I', '-c', code],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = context.__enter__()
        observed.update(raw=raw, child=child, context=context)
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < deadline and child.poll() is None
            time.sleep(.01)
        def cleanup():
            if not permit.is_set():
                return False
            context.__exit__(None, None, None)
            return True
        owner = CleanupOwner(cleanup)
        observed['owner'] = owner
        failure = AudioCleanupPending('Actual child still owns the decoded stage.', cleanup_owner=owner)
        if masked:
            raise OSError('Synthetic outer failure') from failure
        raise failure

    monkeypatch.setattr(silence, 'run_process', pending)
    try:
        with pytest.raises(OSError if masked else AudioCleanupPending):
            runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
        assert observed['child'].poll() is None and observed['raw'].stat().st_size == 400
        assert source.read_bytes() == before
        assert observed['owner'].close() is False and observed['raw'].parent.is_dir()
        permit.set()
        assert observed['owner'].close() is True
        assert observed['child'].poll() is not None
        assert not observed['raw'].parent.exists() and source.read_bytes() == before
    finally:
        permit.set()
        if 'owner' in observed:
            observed['owner'].close()
        elif 'context' in observed:
            observed['context'].__exit__(None, None, None)


@pytest.mark.parametrize('kind', ['floating-point', 'empty'])
def test_non_integer_or_unmeasurable_wav_never_reaches_integer_decoder(runtime, tmp_path, monkeypatch, kind):
    source = tmp_path / 'unsupported.wav'
    if kind == 'floating-point':
        raw = struct.pack('<f', .25) * 100
        fmt = struct.pack('<HHIIHH', 3, 1, RATE, RATE * 4, 4, 32)
        payload = b'fmt ' + struct.pack('<I', len(fmt)) + fmt
        payload += b'data' + struct.pack('<I', len(raw)) + raw
        source.write_bytes(b'RIFF' + struct.pack('<I', len(payload) + 4) + b'WAVE' + payload)
    else:
        pcm_file(source, [])
    before = source.read_bytes()
    monkeypatch.setattr(silence, 'run_process', lambda *a, **k: pytest.fail('Unsupported PCM reached decoding.'))
    with pytest.raises(AudioError):
        runtime.inspect_silence(source, threshold_dbfs=-50, minimum_seconds=.001)
    assert source.read_bytes() == before and not list(tmp_path.glob('.carr-silence-*'))


@pytest.mark.parametrize('channels', [1, 2])
def test_actual_pcm_scan_crosses_real_block_boundaries(runtime, tmp_path, channels):
    # Reuse PR209 e9/df9a2ae's actual cross-block signal; our existing decoder
    # extends the source to s32, so the real block boundary is four bytes/channel.
    block_frames = silence._BLOCK_BYTES // (4 * channels)
    loud = 100 if channels == 1 else (100, 100)
    quiet = 0 if channels == 1 else (0, 0)
    frames = [loud] * (block_frames - 2) + [quiet] * 8 + [loud] * 3
    source = pcm_file(tmp_path / 'actual-block-boundary.wav', frames, channels=channels)
    before = source.read_bytes()
    report = runtime.inspect_silence(source, threshold_dbfs=-192, minimum_seconds=8 / RATE)
    assert report['audio']['frames'] == block_frames + 9
    assert report['audio']['channels'] == channels and report['audio']['sample_rate'] == RATE
    assert intervals(report) == [(block_frames - 2, block_frames + 6)]
    assert report['interval_count'] == 1 and report['omitted_count'] == 0
    assert report['criteria']['minimum_frames'] == 8
    assert report['criteria']['amplitude_limit_s32'] == 0
    expected = (struct.pack('<' + 'i' * channels, *([100 << 16] * channels)) * (block_frames - 2)
                + b'\0' * (8 * channels * 4)
                + struct.pack('<' + 'i' * channels, *([100 << 16] * channels)) * 3)
    assert len(expected) > silence._BLOCK_BYTES
    assert report['pcm_sha256'] == sha256(expected).hexdigest()
    assert report['source']['sha256'] == sha256(before).hexdigest()
    assert source.read_bytes() == before and not list(tmp_path.glob('.carr-silence-*'))
