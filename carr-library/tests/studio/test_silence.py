"""Actual PCM and completed Studio receipt inspection; no acoustic quality claim."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import runpy
import shutil
import struct
import threading
import wave

import pytest

from carr_library.audio.models import AudioBudget, AudioCancelled
from carr_library.durable_state import atomic_json
from carr_library.studio import silence
from carr_library.studio.render import ProductionService


def pcm(path, frames, *, rate=8000, channels=1, rf64=False):
    payload = b''.join(struct.pack('<' + 'h' * channels, *(
        frame if isinstance(frame, tuple) else (frame,) * channels)) for frame in frames)
    if rf64:
        total = 80 + len(payload)
        raw = (b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE'
               + b'ds64' + struct.pack('<IQQQI', 28, total - 8, len(payload), len(frames), 0)
               + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
               + b'data' + struct.pack('<I', 0xffffffff) + payload)
        path.write_bytes(raw)
    else:
        with wave.open(str(path), 'wb') as output:
            output.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
            output.writeframes(payload)
    return {'size': path.stat().st_size, 'sha256': sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize('channels,rf64', [(1, False), (2, False), (1, True), (2, True)])
def test_exact_real_pcm_intervals_include_leading_trailing_and_cross_block_silence(tmp_path, channels, rf64):
    path = tmp_path / 'retained.wav'
    frames = [0] * 800 + [3000] * 80 + [0] * (silence._BLOCK + 17) + [3000] * 80 + [0] * 800
    receipt = pcm(path, frames, channels=channels, rf64=rf64)
    before = path.read_bytes()
    report = silence.measure_pcm_silence(path, receipt, threshold_dbfs=-120, minimum_seconds=.1)
    assert report['intervals'] == [
        {'start_frame': 0, 'end_frame': 800, 'frames': 800},
        {'start_frame': 880, 'end_frame': 880 + silence._BLOCK + 17, 'frames': silence._BLOCK + 17},
        {'start_frame': len(frames) - 800, 'end_frame': len(frames), 'frames': 800}]
    assert report['criteria']['minimum_frames'] == 800
    assert report['master_sha256'] == receipt['sha256']
    assert report['qualifying_frames'] == len(frames) - 160
    assert path.read_bytes() == before


def test_every_channel_must_be_quiet_and_retention_cap_never_stops_full_count(tmp_path):
    path = tmp_path / 'stereo.wav'
    frames = [(0, 0)] * 800 + [(0, 3000)] * 80 + [(0, 0)] * 800 + [(3000, 0)] * 80 + [(0, 0)] * 800
    receipt = pcm(path, frames, channels=2)
    result = silence.measure_pcm_silence(path, receipt, threshold_dbfs=-60, minimum_seconds=.1, max_intervals=1)
    assert result['interval_count'] == 3 and result['retained_interval_count'] == 1
    assert result['omitted_interval_count'] == 2 and result['qualifying_frames'] == 2400
    assert result['frames'] == len(frames) and result['master_sha256'] == receipt['sha256']
    assert result['intervals'] == [{'start_frame': 0, 'end_frame': 800, 'frames': 800}]


@pytest.mark.parametrize('threshold,minimum,maximum', [
    (True, 1, 10), ('-60', 1, 10), (float('nan'), 1, 10), (-121, 1, 10), (1, 1, 10),
    (-60, 0, 10), (-60, float('inf'), 10), (-60, True, 10), (-60, 86401, 10),
    (-60, 1, 0), (-60, 1, 1001), (-60, 1, True)])
def test_invalid_criteria_are_refused_before_open(tmp_path, threshold, minimum, maximum):
    with pytest.raises(ValueError):
        silence.measure_pcm_silence(tmp_path / 'absent.wav', {}, threshold_dbfs=threshold,
                                    minimum_seconds=minimum, max_intervals=maximum)


def test_actual_byte_damage_and_cancel_never_return_a_partial_report(tmp_path):
    path = tmp_path / 'audio.wav'
    receipt = pcm(path, [0] * 5000)
    with path.open('r+b') as output:
        output.seek(-1, 2); output.write(b'\1')
    with pytest.raises(ValueError, match='changed'):
        silence.measure_pcm_silence(path, receipt, threshold_dbfs=-60, minimum_seconds=.1)
    def cancel():
        raise AudioCancelled('Explicit stop')
    with pytest.raises(AudioCancelled):
        silence.measure_pcm_silence(path, receipt, threshold_dbfs=-60, minimum_seconds=.1, check=cancel)


def test_header_and_samples_use_the_same_held_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'original.wav'
    receipt = pcm(path, [0] * 800 + [1000] * 800)
    original = silence._pcm_header_stream
    held = []
    def observe(stream, size, **kwargs):
        held.append((stream.fileno(), os.fstat(stream.fileno()).st_ino, stream.tell(), size))
        return original(stream, size, **kwargs)
    monkeypatch.setattr(silence, '_pcm_header_stream', observe)
    original_open = Path.open
    def refuse_second_path_open(selected, *args, **kwargs):
        if selected == path:
            raise AssertionError('PCM parsing must not reopen the pathname.')
        return original_open(selected, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', refuse_second_path_open)
    result = silence.measure_pcm_silence(path, receipt, threshold_dbfs=-120, minimum_seconds=.1)
    assert len(held) == 1 and held[0][2:] == (0, receipt['size'])
    assert result['intervals'][0]['end_frame'] == 800


def test_replaced_target_before_open_is_refused_without_waiting_on_a_fifo(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    path = tmp_path / 'selected.wav'
    receipt = pcm(path, [0] * 1600)
    original_path = tmp_path / 'retained-original.wav'
    replacement = tmp_path / 'replacement.wav'
    pcm(replacement, [3000] * 1600)
    actual_open, flags_seen = os.open, []
    def open_changed(selected, flags, *args, **kwargs):
        if Path(selected) == path and not flags_seen:
            flags_seen.append(flags)
            path.rename(original_path)
            if hasattr(os, 'mkfifo'):
                os.mkfifo(path)
            else:
                replacement.rename(path)
        return actual_open(selected, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_changed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(silence.measure_pcm_silence, path, receipt,
                               threshold_dbfs=-60, minimum_seconds=.1)
        try:
            with pytest.raises(ValueError, match='changed'):
                future.result(timeout=3)
        except FutureTimeout:
            if hasattr(os, 'mkfifo'):
                writer = actual_open(path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(writer)
            try:
                future.result(timeout=3)
            except Exception:
                pass
            pytest.fail('Inspection blocked while opening a replaced input.')
    assert flags_seen and (not hasattr(os, 'O_NONBLOCK') or flags_seen[0] & os.O_NONBLOCK)
    assert sha256(original_path.read_bytes()).hexdigest() == receipt['sha256']


@pytest.fixture
def completed(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install the selected actual test audio runtime.'
    fixtures = runpy.run_path(str(Path(__file__).with_name('test_interchange.py')))
    output = tmp_path / 'outputs'; output.mkdir()
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio', voices=fixtures['ToneVoices']())
    script = '[Scene: Checked audio]\nNarrator: Exact retained line.\n[Pause: 0.5]\nNarrator: Final exact line.\n'
    project = service.create(script, title='Silence inspection fixture', voice_ids=('tone',))
    identity = service.submit(project, output, stems=False)
    state = service.run(identity)
    assert state['status'] == 'complete'
    before = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest() for row in state['artifacts']}
    yield service, state, before
    service.close()


def inspect(service, state, **kwargs):
    return silence.inspect_completed_silence(service, state['id'],
        threshold_dbfs=-80, minimum_seconds=.3, **kwargs)


def test_actual_completed_audio_is_inspected_under_real_admission_without_mutation(completed):
    service, state, before = completed
    active_counts, messages = [], []
    def progress(message):
        messages.append(message)
        if 'Scanning' in message:
            active_counts.append(len(service.broker._active()))
    result = inspect(service, state, progress=progress)
    assert result['status'] == 'complete' and result['interval_count'] >= 1
    assert any(row['frames'] >= .3 * result['sample_rate'] for row in result['intervals'])
    assert active_counts == [1] and not service.broker._active()
    assert result['project_fingerprint'] == state['fingerprint']
    assert 'Review warnings only' in silence.silence_report_text(result)
    assert service.get(state['id']) == state
    assert before == {path: sha256(Path(path).read_bytes()).hexdigest() for path in before}
    assert any('Inspected' in message for message in messages)


def test_notification_failure_does_not_invent_audio_failure(completed):
    service, state, _ = completed
    def progress(message):
        raise RuntimeError('Controlled observer failure')
    result = inspect(service, state, progress=progress)
    assert result['status'] == 'complete' and service.get(state['id']) == state
    assert not service.broker._active()


def test_actual_cancel_after_scan_releases_resources_and_preserves_completed_job(completed):
    service, state, before = completed
    stopped = threading.Event()
    def progress(message):
        if 'Inspected' in message:
            stopped.set()
    with pytest.raises(AudioCancelled):
        inspect(service, state, progress=progress, cancelled=stopped.is_set)
    assert service.get(state['id']) == state and not service.broker._active()
    assert before == {path: sha256(Path(path).read_bytes()).hexdigest() for path in before}


@pytest.mark.parametrize('boundary', ['master', 'timeline', 'job', 'destination'])
def test_changed_completed_input_is_refused_and_never_repaired(completed, boundary):
    service, state, before = completed
    destination = Path(state['destination'])
    changed = []
    def progress(message):
        if 'Scanning' not in message or changed:
            return
        changed.append(boundary)
        if boundary in ('master', 'timeline'):
            path = destination / ('master.wav' if boundary == 'master' else 'timeline.json')
            with path.open('ab') as output:
                output.write(b'changed')
        elif boundary == 'job':
            value = deepcopy(state); value['progress'] = 'Changed by another operation'
            atomic_json(service.jobs_root / state['id'] / 'job.json', value, replace=True)
        else:
            moved = destination.with_name(destination.name + '-original')
            destination.rename(moved)
            shutil.copytree(moved, destination)
    with pytest.raises((ValueError, OSError)):
        inspect(service, state, progress=progress)
    assert changed == [boundary] and not service.broker._active()


def test_small_budget_refuses_before_a_partial_inspection(completed):
    service, state, _ = completed
    with pytest.raises(ValueError, match='budget'):
        inspect(service, state, budget=AudioBudget(max_output_bytes=4096))
    assert service.get(state['id']) == state and not service.broker._active()


@pytest.mark.parametrize('damage', ['hash', 'count', 'interval', 'threshold', 'notice', 'rate'])
def test_report_formatter_rejects_corrupted_numeric_or_identity_fields(completed, damage):
    service, state, _ = completed
    report = inspect(service, state)
    if damage == 'hash':
        report['master_sha256'] = 'unknown'
    elif damage == 'count':
        report['interval_count'] += 1
    elif damage == 'interval':
        report['intervals'][0]['end_frame'] = report['frames'] + 1
    elif damage == 'threshold':
        report['criteria']['threshold_pcm16'] += 1
    elif damage == 'notice':
        report['limitations'].append('x' * 513)
    else:
        report['sample_rate'] = True
    with pytest.raises(ValueError):
        silence.silence_report_text(report)
