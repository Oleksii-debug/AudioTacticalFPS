"""Real admitted FFmpeg segments and retained PCM, without a hardware claim."""
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioCancelled, AudioError, AudioRuntime
from carr_library.audio import recording, recording_monitor
from carr_library.audio.process import CaptureCleanupError
from carr_library.audio.recording import RecordingDevice, RecordingOptions, RecordingService
from carr_library.library_backup import restore_backup
from carr_library.speech.playback_devices import PlaybackDevice


@pytest.fixture
def capture(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    assert ffmpeg and ffprobe, 'This required actual-media suite needs FFmpeg and FFprobe.'
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root, runtime=AudioRuntime(tmp_path / 'runtime', ffmpeg=ffmpeg, ffprobe=ffprobe))
    alternative = '@device_synthetic_controlled_capture'
    device = RecordingDevice(sha256(alternative.encode()).hexdigest(), 'Synthetic PCM', alternative)
    service._devices, service._engine_hash = {device.id: device}, recording._digest(ffmpeg)
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    actual = recording.run_process
    runs, lock = [], threading.Lock()

    def run(command, **kwargs):
        item = {'path': Path(command[-1]), 'active': True, 'pcm': None}
        with lock:
            assert not any(part['active'] for part in runs), 'Capture trees must never overlap.'
            runs.append(item)
        try:
            result = actual(command, **kwargs)
            with wave.open(str(item['path']), 'rb') as reader:
                item['pcm'] = reader.readframes(reader.getnframes())
            return result
        finally:
            item['active'] = False

    monkeypatch.setattr(recording, 'run_process', run)
    return SimpleNamespace(app=app, service=service, device=device, runs=runs,
        options=RecordingOptions(max_seconds=4, sample_rate=24000))


class Session:
    def __init__(self, capture, *, monitor=None):
        self.capture = capture
        self.pause, self.stop, self.cancel, self.paused = (threading.Event() for _ in range(4))
        self.result, self.error, self.states, self.progress = None, None, [], []
        self.thread = threading.Thread(target=self.run, kwargs={'monitor': monitor})

    def state(self, value):
        self.states.append(value)
        if value.state == 'paused':
            self.paused.set()

    def observed(self, seconds):
        self.progress.append(seconds)
        if not self.paused.is_set() and seconds >= .2:
            self.pause.set()
        elif self.paused.is_set() and len(self.capture.runs) >= 2 and seconds >= 1.3:
            self.stop.set()

    def run(self, *, monitor):
        try:
            self.result = self.capture.service.capture(self.capture.device.id,
                options=self.capture.options, pause_requested=self.pause.is_set,
                stop_requested=self.stop.is_set, cancelled=self.cancel.is_set,
                state_changed=self.state, progress=self.observed, monitor_output=monitor)
        except BaseException as exc:
            self.error = exc

    def __enter__(self):
        self.thread.start()
        if not self.paused.wait(6):
            self.cancel.set()
            self.thread.join(7)
            pytest.fail(f'Actual capture did not acknowledge Pause: {self.error!r}')
        return self

    def finish(self):
        self.thread.join(7)
        assert not self.thread.is_alive(), 'The owned capture did not finish within its cleanup allowance.'
        return self.result

    def __exit__(self, *args):
        self.cancel.set()
        self.thread.join(7)
        assert not self.thread.is_alive(), 'Test cleanup left a capture owner alive.'


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        return reader.readframes(reader.getnframes())


def test_actual_pause_closes_capture_then_resume_retains_exact_segment_frames(capture):
    with Session(capture) as session:
        assert len(capture.runs) == 1 and not capture.runs[0]['active']
        before = capture.runs[0]['path'].read_bytes()
        verified_frames = session.states[-1].frames
        time.sleep(.18)
        assert len(capture.runs) == 1 and not capture.runs[0]['active']
        assert capture.runs[0]['path'].read_bytes() == before
        assert session.states[-1].state == 'paused' and session.states[-1].frames == verified_frames
        session.pause.clear()
        result = session.finish()
    assert session.error is None
    assert result['schema'] == 'carr-recording/2' and result['completion'] == 'stopped'
    expected = b''.join(part['pcm'] for part in capture.runs)
    assert len(capture.runs) == 2 and pcm(result['path']) == expected
    assert result['audio']['frames'] == len(expected) // 2
    assert result['audio']['duration_seconds'] == len(expected) / 48000
    assert [part['ended_by'] for part in result['segments']] == ['paused', 'stopped']
    offset = 0
    for receipt, part in zip(result['segments'], capture.runs):
        assert receipt['start_frame'] == offset
        assert receipt['frames'] * 2 == len(part['pcm'])
        assert receipt['pcm_sha256'] == sha256(part['pcm']).hexdigest()
        offset += receipt['frames']
    assert session.progress == sorted(session.progress)
    assert capture.service.get(result['id']) == result
    assert not list(capture.service.root.glob('.pending-*'))


def test_stop_while_paused_publishes_retained_frames_without_reopening_input(capture):
    with Session(capture) as session:
        session.stop.set()
        result = session.finish()
    assert session.error is None and len(capture.runs) == 1
    assert result['completion'] == 'stopped'
    assert pcm(result['path']) == capture.runs[0]['pcm']
    assert capture.service.get(result['id']) == result


def test_cancel_while_paused_discards_only_unfinished_take(capture):
    original = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1))
    original_bytes = Path(original['path']).read_bytes()
    capture.runs.clear()
    with Session(capture) as session:
        session.cancel.set()
        session.finish()
    assert isinstance(session.error, AudioCancelled) and session.result is None
    assert len(capture.runs) == 1 and not capture.runs[0]['active']
    assert Path(original['path']).read_bytes() == original_bytes
    assert capture.service.list() == (original,)
    assert not list(capture.service.root.glob('.pending-*'))


def test_changed_runtime_while_paused_refuses_before_second_input(capture):
    with Session(capture) as session:
        capture.service._engine_hash = '0' * 64
        session.pause.clear()
        session.finish()
    assert isinstance(session.error, ValueError) and 'runtime changed' in str(session.error)
    assert len(capture.runs) == 1 and not list(capture.service.root.glob('*.wav'))


def test_initial_pause_never_opens_input_and_has_bounded_wait(capture, monkeypatch):
    monkeypatch.setattr(recording, 'MAX_PAUSED_SECONDS', .02)
    started = time.monotonic()
    with pytest.raises(AudioError, match='pause allowance'):
        capture.service.capture(capture.device.id, options=capture.options, pause_requested=lambda: True)
    assert time.monotonic() - started < 1 and capture.runs == []
    assert not list(capture.service.root.glob('.pending-*'))


def test_controlled_default_never_opens_monitor_and_legacy_receipt_stays_v1(capture, monkeypatch):
    monkeypatch.setattr(recording_monitor, '_api', lambda: pytest.fail('Default capture opened an output.'))
    result = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1),
                                     pause_requested=lambda: False)
    assert result['schema'] == 'carr-recording/2' and result['monitoring'] == {
        'enabled': False, 'output_identity': None, 'submitted_frames': 0, 'dropped_frames': 0}
    legacy = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1))
    assert legacy['schema'] == 'carr-recording/1' and 'segments' not in legacy
    assert capture.service.get(legacy['id']) == legacy


def selected_output():
    return PlaybackDevice(0, 'Explicit test output', 'test-output-interface', 1, 2, 3, 2)


def test_monitor_taps_actual_same_pcm_with_bounded_blocks_and_no_second_input(capture, monkeypatch):
    monitors = []
    class Sink:
        def __init__(self):
            self.blocks, self.closed, self.calls = [], False, 0
            monitors.append(self)
        def open(self, rate, channels, selected, *, cancelled):
            assert rate == 24000 and channels == 1 and selected == selected_output()
        def submit(self, data):
            assert len(data) <= 2400 and len(data) % 2 == 0
            self.calls += 1
            if self.calls % 2:
                return False
            self.blocks.append(data)
            return True
        def close(self): self.closed = True
    monkeypatch.setattr(recording_monitor, 'DryMonitor', Sink)
    with Session(capture, monitor=selected_output()) as session:
        assert monitors[0].closed and not capture.runs[0]['active']
        session.pause.clear()
        result = session.finish()
    assert session.error is None and len(monitors) == len(capture.runs) == 2
    assert all(sink.closed and sink.blocks for sink in monitors)
    for sink, part in zip(monitors, capture.runs):
        assert all(block in part['pcm'] for block in sink.blocks)
    evidence = result['monitoring']
    assert evidence['enabled'] and evidence['output_identity'] == recording_monitor.output_identity(selected_output())
    assert evidence['submitted_frames'] == sum(len(block) // 2 for sink in monitors for block in sink.blocks)
    assert evidence['dropped_frames'] > 0
    assert evidence['submitted_frames'] + evidence['dropped_frames'] <= result['audio']['frames']


def test_unconfirmed_monitor_close_retains_owned_state_and_blocks_another_capture(capture, monkeypatch):
    lock = threading.Lock()
    monkeypatch.setattr(recording, '_CAPTURE', lock)
    class Sink:
        def open(self, *args, **kwargs): pass
        def submit(self, data): return True
        def close(self): raise CaptureCleanupError('Synthetic unacknowledged driver close')
    monkeypatch.setattr(recording_monitor, 'DryMonitor', Sink)
    with pytest.raises(CaptureCleanupError, match='unconfirmed'):
        capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1),
                                monitor_output=selected_output())
    assert len(capture.runs) == 1 and not capture.runs[0]['active'] and lock.locked()
    pending = list(capture.service.root.glob('.pending-*'))
    assert len(pending) == 1 and list(pending[0].glob('segment-*.wav'))
    assert not list(capture.service.root.glob('*.json'))
    with pytest.raises(RuntimeError, match='Another microphone'):
        capture.service.capture(capture.device.id, options=capture.options)


def test_changed_completed_segment_refuses_assembly_and_publication(capture, monkeypatch):
    original = capture.service._join_segments
    def tamper(parts, staged, options, cancelled):
        path = parts[0][0]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        return original(parts, staged, options, cancelled)
    monkeypatch.setattr(capture.service, '_join_segments', tamper)
    with pytest.raises(ValueError, match='segment changed'):
        capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1),
                                pause_requested=lambda: False)
    assert not list(capture.service.root.glob('*.wav'))


def test_segment_changed_immediately_after_genuine_hash_cannot_mint_new_frame_receipt(capture, monkeypatch):
    actual_digest, actual_join = recording._digest, capture.service._join_segments
    boundary = {'assembling': False, 'changed': False}
    def join(*args):
        boundary['assembling'] = True
        return actual_join(*args)
    def verified_then_changed(path, cancelled=lambda: False):
        digest = actual_digest(path, cancelled)
        if boundary['assembling'] and Path(path).name.startswith('segment-') and not boundary['changed']:
            data = bytearray(Path(path).read_bytes())
            data[-1] ^= 1
            Path(path).write_bytes(data)
            boundary['changed'] = True
        return digest
    monkeypatch.setattr(capture.service, '_join_segments', join)
    monkeypatch.setattr(recording, '_digest', verified_then_changed)
    with pytest.raises(ValueError, match='segment changed during assembly'):
        capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1),
                                pause_requested=lambda: False)
    assert boundary['changed'] and len(capture.runs) == 1 and not capture.runs[0]['active']
    assert not list(capture.service.root.glob('*.wav')) and not list(capture.service.root.glob('*.json'))
    assert not list(capture.service.root.glob('.pending-*'))


def test_late_cancel_after_controlled_publication_retains_committed_receipt(capture, monkeypatch):
    cancelled, actual = threading.Event(), recording.publish_file
    def publish(*args, **kwargs):
        actual(*args, **kwargs)
        cancelled.set()
    monkeypatch.setattr(recording, 'publish_file', publish)
    result = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=1),
        pause_requested=lambda: False, cancelled=cancelled.is_set)
    assert cancelled.is_set() and capture.service.get(result['id']) == result


def test_v2_backup_restore_verifies_retained_frames_without_engine_or_input_authority(capture, tmp_path, monkeypatch):
    with Session(capture) as session:
        session.pause.clear()
        result = session.finish()
    assert session.error is None
    archive = tmp_path / 'recording-v2.carrbackup'
    manifest = capture.app.backup(archive)
    shutil.rmtree(capture.app.root)
    destination = tmp_path / 'restored'
    restore_backup(archive, destination, expected_manifest_sha256=manifest)
    service = RecordingService(destination, runtime=capture.service.runtime)
    monkeypatch.setattr(recording, 'run_process', lambda *args, **kwargs: pytest.fail('Restore opened an input.'))
    monkeypatch.setattr(recording_monitor, '_api', lambda: pytest.fail('Restore opened an output.'))
    item = service.get(result['id'])
    assert item['segments'] == result['segments'] and item['audio'] == result['audio']
    assert service._devices == {} and service._engine_hash is None
    assert Path(item['path']).is_relative_to(destination)


@pytest.fixture
def receipt(tmp_path):
    options = RecordingOptions(max_seconds=1, sample_rate=24000)
    identity, path = 'a' * 32, tmp_path / ('a' * 32 + '.wav')
    data = b'\x01\x02' * 240
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(data)
    value = {'schema': 'carr-recording/2', 'id': identity, 'filename': path.name,
        'title': 'Recorded take', 'created': '2026-11-27T00:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64, 'name': 'Selected input'},
        'ffmpeg_sha256': 'c' * 64, 'audio': RecordingService._audio(path, options, lambda: False),
        'limits': asdict(options), 'completion': 'stopped', 'timeline': recording._FRAME_NOTICE,
        'segments': [{'start_frame': 0, 'frames': 240, 'pcm_sha256': sha256(data).hexdigest(), 'ended_by': 'stopped'}],
        'monitoring': {'enabled': False, 'output_identity': None, 'submitted_frames': 0, 'dropped_frames': 0}}
    return path, value


@pytest.mark.parametrize('change', [
    lambda value: value.update(schema='carr-recording/3'),
    lambda value: value['segments'][0].update(start_frame=True),
    lambda value: value['segments'][0].update(start_frame=1),
    lambda value: value['segments'][0].update(frames=239),
    lambda value: value['segments'][0].update(pcm_sha256='unknown'),
    lambda value: value['segments'][0].update(path='/external.wav'),
    lambda value: value['monitoring'].update(enabled=1),
    lambda value: value['monitoring'].update(submitted_frames=True),
    lambda value: value['monitoring'].update(enabled=True, output_identity='a' * 64, submitted_frames=240, dropped_frames=1),
    lambda value: value['monitoring'].update(output_identity='a' * 64),
])
def test_receipt_rejects_unknown_authority_noncontiguous_frames_and_invalid_monitor_counts(receipt, change):
    _, value = receipt
    change(value)
    with pytest.raises(ValueError):
        recording.validate_recording_receipt(value, value['id'])


def test_updated_whole_file_receipt_cannot_hide_changed_retained_pcm_range(receipt):
    path, value = receipt
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    value['audio']['sha256'] = sha256(data).hexdigest()
    with pytest.raises(ValueError, match='segment'):
        recording.verify_recording_segments(path, value)


def test_unchanged_v1_receipt_remains_passively_readable(receipt):
    path, value = receipt
    value = {key: item for key, item in value.items() if key not in {'segments', 'monitoring', 'timeline'}}
    value['schema'] = 'carr-recording/1'
    assert recording.validate_recording_receipt(value, value['id']) == value
    recording.verify_recording_segments(path, value)
