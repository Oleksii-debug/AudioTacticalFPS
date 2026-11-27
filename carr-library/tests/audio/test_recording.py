"""Actual FFmpeg synthetic capture; no physical microphone qualification."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioCancelled, AudioError, AudioRuntime, AudioUnavailable
from carr_library.audio import recording
from carr_library.audio.recording import RecordingDevice, RecordingOptions, RecordingService, parse_devices
from carr_library.library_backup import restore_backup


INVENTORY = br'''[dshow @ 123] "Camera" (video)
[dshow @ 123]   Alternative name "@device_pnp_camera"
[dshow @ 123] "Microphone USB" (audio)
[dshow @ 123]   Alternative name "@device_cm_{123}\wave_{456}"
[dshow @ 123] "Microphone USB" (audio)
[dshow @ 123]   Alternative name "@device_cm_{123}\wave_{789}"
'''


@pytest.fixture
def capture(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.fail('Actual FFmpeg tools are required for synthetic capture.')
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root, runtime=AudioRuntime(tmp_path / 'runtime', ffmpeg=ffmpeg, ffprobe=ffprobe))
    device = parse_devices(INVENTORY)[0]
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(ffmpeg)
    # Explicitly test-only source adapter. Production exposes only enumerated
    # DirectShow devices, never arbitrary lavfi expressions or input argv.
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    return SimpleNamespace(app=app, service=service, device=device,
                           options=RecordingOptions(max_seconds=1, sample_rate=24000))


def test_device_inventory_excludes_video_and_preserves_unique_microphone_identity():
    first, second = parse_devices(INVENTORY)
    assert first.name == second.name == 'Microphone USB'
    assert first.id != second.id and first.alternative.endswith('wave_{456}')
    assert parse_devices(b'No audio devices found') == ()


@pytest.mark.parametrize('identity', ['@device_x:video=Camera', '@device_x=other', "@device_'x", 'https://host/device'])
def test_device_cannot_inject_another_stream_or_external_source(identity):
    with pytest.raises(ValueError):
        RecordingDevice(sha256(identity.encode()).hexdigest(), 'Microphone', identity)


@pytest.mark.parametrize('options', [dict(max_seconds=True), dict(channels=3), dict(sample_rate=192000),
                                   dict(max_seconds=99999999999999999999999), dict(max_bytes=4096)])
def test_capture_limits_are_validated_before_opening_any_device(options):
    with pytest.raises(ValueError):
        RecordingOptions(**options)


def test_actual_pcm_publication_is_verified_and_reopenable(capture):
    observed = []
    item = capture.service.capture(capture.device.id, title='Synthetic source', options=capture.options,
        progress=lambda seconds: observed.append(capture.service._broker.snapshot()))
    assert item['completion'] == 'duration-limit'
    assert item['audio']['frames'] == 24000
    assert capture.service.get(item['id']) == item
    with wave.open(item['path'], 'rb') as audio:
        assert audio.getframerate() == 24000 and audio.getnchannels() == 1
        assert any(audio.readframes(24000))
    assert len(capture.service.list()) == 1
    assert not list(capture.service.root.glob('.pending-*'))
    assert observed and observed[0]['exclusive_devices'] == ['microphone:' + capture.device.id]
    assert observed[0]['usage']['cpu_slots'] == 1 and observed[0]['usage']['disk_bytes'] > 0
    assert observed[0]['leases'][0]['operation_id'].startswith('recording:')
    assert not capture.service._broker.snapshot()['leases']


def test_actual_graceful_stop_finalizes_short_pcm(capture):
    stop = threading.Event()
    def observed(seconds):
        if seconds >= .2:
            stop.set()
    result = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=10),
                                     progress=observed, stop_requested=stop.is_set)
    assert result['completion'] == 'stopped'
    assert .15 <= result['audio']['duration_seconds'] < 3
    assert capture.service.get(result['id']) == result


def test_actual_cancel_discards_incomplete_capture(capture):
    cancel = threading.Event()
    with pytest.raises(AudioCancelled):
        capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=10),
            progress=lambda seconds: cancel.set() if seconds >= .1 else None, cancelled=cancel.is_set)
    assert not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('*.json'))
    assert not list(capture.service.root.glob('.pending-*'))


def test_unknown_device_and_early_stop_do_not_launch_any_capture(capture, monkeypatch):
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('A capture was opened'))
    with pytest.raises(ValueError, match='explicitly select'):
        capture.service.capture('not-enumerated')
    with pytest.raises(AudioCancelled, match='before opening'):
        capture.service.capture(capture.device.id, stop_requested=lambda: True)


def test_new_capture_never_overwrites_an_existing_identity(capture, monkeypatch):
    identity = 'a' * 32
    target = capture.service.root / (identity + '.wav')
    target.write_bytes(b'Original sentinel')
    monkeypatch.setattr(recording, 'uuid4', lambda: SimpleNamespace(hex=identity))
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Existing identity opened microphone'))
    with pytest.raises(FileExistsError):
        capture.service.capture(capture.device.id, options=capture.options)
    assert target.read_bytes() == b'Original sentinel'


def test_bad_pcm_and_changed_runtime_do_not_publish(capture, monkeypatch):
    def wrong_pcm(args, **kwargs):
        Path(args[-1]).write_bytes(b'Not recorded PCM')
        return SimpleNamespace(stopped=False)
    monkeypatch.setattr(recording, 'run_process', wrong_pcm)
    with pytest.raises((wave.Error, EOFError, ValueError)):
        capture.service.capture(capture.device.id, options=capture.options)
    assert not list(capture.service.root.glob('*.wav'))
    capture.service._engine_hash = '0' * 64
    with pytest.raises(ValueError, match='runtime changed'):
        capture.service.capture(capture.device.id, options=capture.options)


def test_observer_failure_cannot_erase_valid_recording(capture):
    def failed(seconds):
        raise RuntimeError('Detached progress label')
    result = capture.service.capture(capture.device.id, options=capture.options, progress=failed)
    assert capture.service.get(result['id']) == result


def test_receipt_does_not_grant_external_path_authority(capture):
    result = capture.service.capture(capture.device.id, options=capture.options)
    receipt = capture.service.root / (result['id'] + '.json')
    value = json.loads(receipt.read_text())
    value['filename'] = '../external.wav'
    receipt.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='receipt'):
        capture.service.get(result['id'])


@pytest.mark.parametrize('change', [dict(frames=True), dict(sample_rate=0), dict(duration_seconds='1'),
                                  dict(sha256='unverified'), dict(size_bytes=10**30)])
def test_listing_rejects_malformed_audio_receipts_without_opening_saved_pcm(capture, change, monkeypatch):
    result = capture.service.capture(capture.device.id, options=capture.options)
    receipt = capture.service.root / (result['id'] + '.json')
    value = json.loads(receipt.read_text())
    value['audio'].update(change)
    receipt.write_text(json.dumps(value))
    monkeypatch.setattr(capture.service, '_audio', lambda *args: pytest.fail('Listing unexpectedly opened PCM'))
    with pytest.raises(ValueError, match='audio receipt'):
        capture.service.list()


def test_actual_capture_backup_delete_original_root_restore_and_verify(capture, tmp_path):
    result = capture.service.capture(capture.device.id, options=capture.options)
    original = Path(result['path']).read_bytes()
    backup, restored = tmp_path / 'backup', tmp_path / 'Відновлення'
    manifest = capture.app.backup(backup)
    shutil.rmtree(capture.app.root)
    restore_backup(backup, restored, expected_manifest_sha256=manifest)
    recovered = RecordingService(restored, runtime=capture.service.runtime)
    saved = recovered.get(result['id'])
    assert saved['audio'] == result['audio']
    assert Path(saved['path']).is_relative_to(restored)
    assert Path(saved['path']).read_bytes() == original
    assert recovered._devices == {}  # Restore never authorizes a microphone.
    Path(saved['path']).write_bytes(original[:-4] + b'bad!')
    with pytest.raises(ValueError, match='differs'):
        recovered.get(result['id'])


def test_capture_admission_is_exclusive_and_released_after_failure(capture):
    assert recording._CAPTURE.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match='Another microphone'):
            capture.service.capture(capture.device.id, options=capture.options)
    finally:
        recording._CAPTURE.release()
    with pytest.raises(AudioCancelled):
        capture.service.capture(capture.device.id, cancelled=lambda: True)
    assert recording._CAPTURE.acquire(blocking=False)
    recording._CAPTURE.release()


def test_recording_receipt_validation_is_pure_and_rejects_incomplete_limits(monkeypatch):
    value = {'schema': 'carr-recording/1', 'id': 'a' * 32, 'filename': 'a' * 32 + '.wav',
        'title': 'Synthetic receipt', 'created': '2026-11-27T12:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64, 'name': 'Explicit device'},
        'ffmpeg_sha256': 'c' * 64, 'completion': 'device-ended',
        'audio': {'sha256': 'd' * 64, 'size_bytes': 50, 'sample_rate': 24000,
                  'channels': 1, 'frames': 3, 'sample_width': 2, 'duration_seconds': 3 / 24000},
        'limits': {'max_seconds': 1, 'sample_rate': 24000, 'channels': 1, 'max_bytes': 100000}}
    def no_io(*args, **kwargs):
        pytest.fail('A pure receipt validator touched external state')
    monkeypatch.setattr(recording, 'local_path', no_io)
    monkeypatch.setattr(recording, 'regular', no_io)
    monkeypatch.setattr(recording, 'read_json', no_io)
    assert recording.validate_recording_receipt(value, recording_id='a' * 32) is value
    del value['limits']['channels']
    with pytest.raises(ValueError, match='receipt'):
        recording.validate_recording_receipt(value)


def test_unverified_original_listing_is_read_only_and_never_probes_audio(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    assert recording.unverified_recording_files(root) == ()
    assert not (root / 'state').exists()
    originals = root / 'state' / 'studio' / 'recordings'
    originals.mkdir(parents=True)
    one, two = originals / ('a' * 32 + '.wav'), originals / ('b' * 32 + '.wav')
    one.write_bytes(b'Unverified retained original, even without a valid WAV header')
    two.write_bytes(b'An existing receipt prevents orphan classification')
    two.with_suffix('.json').write_bytes(b'Receipt validation is a separate operation')
    (originals / 'unrelated.wav').write_bytes(b'Not a canonical saved identity')
    def no_audio(*args, **kwargs):
        pytest.fail('Orphan listing probed or activated audio')
    monkeypatch.setattr(recording, '_digest', no_audio)
    monkeypatch.setattr(recording, 'read_json', no_audio)
    monkeypatch.setattr(RecordingService, '_runtime', no_audio)
    assert recording.unverified_recording_files(root) == (one,)
    assert one.read_bytes().startswith(b'Unverified retained original')


def test_pending_cleanup_returns_worker_and_keeps_staging_and_both_admission_locks(capture, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import subprocess
    import sys
    from carr_library.audio.models import AudioCleanupPending
    from filelock import FileLock

    class Owner:
        def __init__(self):
            self.quiescent_event = threading.Event()
            self.callbacks = []
        @property
        def quiescent(self):
            return self.quiescent_event.is_set()
        def add_quiescent_callback(self, callback):
            if self.quiescent:
                callback()
            else:
                self.callbacks.append(callback)
        def confirm(self):
            self.quiescent_event.set()
            callbacks, self.callbacks = self.callbacks, []
            for callback in callbacks:
                callback()

    owner, staged = Owner(), []
    def unfinished(args, **kwargs):
        assert kwargs['owned_tree'] is True
        path = Path(args[-1])
        path.write_bytes(b'Incomplete capture remains owned until native silence')
        staged.append(path)
        raise AudioCleanupPending('Synthetic retained native owner', cleanup_owner=owner)
    monkeypatch.setattr(recording, 'run_process', unfinished)
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(capture.service.capture, capture.device.id, options=capture.options)
        try:
            with pytest.raises(AudioCleanupPending):
                pending.result(timeout=5)
            assert staged and staged[0].read_bytes().startswith(b'Incomplete capture')
            assert not list(capture.service.root.glob('*.wav'))
            assert not list(capture.service.root.glob('*.json'))
            assert capture.service._broker.snapshot()['exclusive_devices'] == ['microphone:' + capture.device.id]
            with pytest.raises(RuntimeError, match='cleanup'):
                capture.service.capture(capture.device.id, options=capture.options)
            code = ("from filelock import FileLock, Timeout\nimport sys\n"
                    "try:\n    lock=FileLock(sys.argv[1], timeout=0); lock.acquire()\n"
                    "except Timeout:\n    sys.exit(0)\n"
                    "else:\n    lock.release(); sys.exit(9)\n")
            check = subprocess.run([sys.executable, '-I', '-B', '-c', code,
                str(capture.service.root / '.recording.lock')], timeout=10, capture_output=True)
            assert check.returncode == 0, check.stderr
        finally:
            owner.confirm()
    assert not staged[0].parent.exists()
    assert not capture.service._broker.snapshot()['leases']
    assert recording._CAPTURE.acquire(blocking=False)
    recording._CAPTURE.release()
    with FileLock(capture.service.root / '.recording.lock', timeout=0):
        pass


def test_pcm_validation_cancellation_uses_the_shared_bounded_wave_reader(capture):
    result = capture.service.capture(capture.device.id, options=capture.options)
    with pytest.raises(AudioCancelled):
        capture.service._audio(Path(result['path']), capture.options, lambda: True)


@pytest.mark.parametrize('action', ['cancel', 'stop'])
def test_shared_device_wait_can_end_before_microphone_open(capture, monkeypatch, action):
    from carr_library.operations import ResourceRequest
    broker = capture.service._admission_broker()
    ended = threading.Event()
    monkeypatch.setattr(recording, 'run_process', lambda *args, **kwargs: pytest.fail('Microphone opened before admission'))
    with broker.reserve(ResourceRequest(cpu_slots=0, devices=('microphone:' + capture.device.id,)),
                        operation_id='other-microphone-owner', timeout=0):
        timer = threading.Timer(.1, ended.set)
        timer.start()
        try:
            with pytest.raises(AudioCancelled, match='shared local resources|before opening'):
                capture.service.capture(capture.device.id, options=capture.options,
                    **{('cancelled' if action == 'cancel' else 'stop_requested'): ended.is_set})
        finally:
            timer.cancel()
            timer.join(2)
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['other-microphone-owner']
    assert not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('.pending-*'))
    assert not broker.snapshot()['leases']


def test_foreign_data_folder_policy_cannot_open_a_microphone(capture, tmp_path, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceStateError
    other = tmp_path / 'another-library'
    other.mkdir()
    capture.service._broker = ResourceBroker.for_app(other)
    monkeypatch.setattr(recording, 'run_process', lambda *args, **kwargs: pytest.fail('Foreign policy opened a microphone'))
    with pytest.raises(ResourceStateError, match='another data folder'):
        capture.service.capture(capture.device.id, options=capture.options)
    assert not capture.service._broker.snapshot()['leases']
