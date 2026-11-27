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
    item = capture.service.capture(capture.device.id, title='Synthetic source', options=capture.options)
    assert item['completion'] == 'duration-limit'
    assert item['audio']['frames'] == 24000
    assert capture.service.get(item['id']) == item
    with wave.open(item['path'], 'rb') as audio:
        assert audio.getframerate() == 24000 and audio.getnchannels() == 1
        assert any(audio.readframes(24000))
    assert len(capture.service.list()) == 1
    assert not list(capture.service.root.glob('.pending-*'))


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
    assert recording._CAPTURE.acquire(blocking=False)
    recording._CAPTURE.release()
    with FileLock(capture.service.root / '.recording.lock', timeout=0):
        pass


def test_pcm_validation_cancellation_uses_the_shared_bounded_wave_reader(capture):
    result = capture.service.capture(capture.device.id, options=capture.options)
    with pytest.raises(AudioCancelled):
        capture.service._audio(Path(result['path']), capture.options, lambda: True)


def test_passive_recording_verification_never_activates_runtime_or_device(capture, monkeypatch):
    saved = capture.service.capture(capture.device.id, options=capture.options)
    receipt = {key: value for key, value in saved.items() if key != 'path'}
    def no_runtime(*args, **kwargs):
        pytest.fail('Passive recording verification activated a runtime or device.')
    monkeypatch.setattr(recording, 'run_process', no_runtime)
    monkeypatch.setattr(RecordingService, '_runtime', no_runtime)
    assert recording.verify_recording_audio(saved['path'], receipt) is receipt
    with pytest.raises(AudioCancelled):
        recording.verify_recording_audio(saved['path'], receipt, cancelled=lambda: True)
    original = Path(saved['path']).read_bytes()
    Path(saved['path']).write_bytes(original[:-2] + b'XX')
    with pytest.raises(ValueError, match='differs'):
        recording.verify_recording_audio(saved['path'], receipt)


def test_pcm_verification_can_stop_between_real_bounded_frame_reads(tmp_path):
    path = tmp_path / 'synthetic-pcm.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\x00\x01' * (65536 * 3))
    original = path.read_bytes()
    polls = []
    def cancelled():
        polls.append(True)
        return len(polls) == 3
    with pytest.raises(AudioCancelled, match='PCM verification cancelled'):
        RecordingService._audio(path, RecordingOptions(max_seconds=10, sample_rate=24000), cancelled)
    assert len(polls) == 3
    assert path.read_bytes() == original


@pytest.mark.parametrize('deferred', [False, True])
def test_failed_admission_release_is_retried_only_by_explicit_capture(capture, monkeypatch, deferred):
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

    owner = Owner()
    reservations, releases, launches, staged = [], [], [], []
    original_init = recording._CaptureReservation.__init__
    original_run = recording.run_process

    def reserve(self, root):
        original_init(self, root)
        if not reservations:
            original_release = self.lock.release
            releases.append(original_release)
            self.release_attempts = 0
            def fail_twice(*args, **kwargs):
                self.release_attempts += 1
                if self.release_attempts <= 2:
                    raise OSError('Synthetic transient admission-release failure.')
                return original_release(*args, **kwargs)
            monkeypatch.setattr(self.lock, 'release', fail_twice)
        reservations.append(self)

    def run(args, **kwargs):
        launches.append(tuple(args))
        if deferred and len(launches) == 1:
            path = Path(args[-1])
            path.write_bytes(b'Unfinished capture remains private until native silence')
            staged.append(path)
            raise AudioCleanupPending('Synthetic retained native owner', cleanup_owner=owner)
        return original_run(args, **kwargs)

    monkeypatch.setattr(recording._CaptureReservation, '__init__', reserve)
    monkeypatch.setattr(recording, 'run_process', run)
    first = None
    try:
        if deferred:
            with pytest.raises(AudioCleanupPending):
                capture.service.capture(capture.device.id, options=capture.options)
            assert reservations[0].release_attempts == 0
            assert not reservations[0].release_retry_required
            with pytest.raises(RuntimeError, match='cleanup'):
                capture.service.capture(capture.device.id, options=capture.options)
            assert len(launches) == 1 and reservations[0].release_attempts == 0
            assert staged[0].is_file()
            assert not list(capture.service.root.glob('*.wav'))
            owner.confirm()
            assert not staged[0].parent.exists()
        else:
            first = capture.service.capture(capture.device.id, options=capture.options)
            assert capture.service.get(first['id']) == first

        retained = reservations[0]
        assert retained.release_retry_required and not retained.closed
        assert retained.release_attempts == 1
        code = ("from filelock import FileLock, Timeout\nimport sys\n"
                "try:\n    lock=FileLock(sys.argv[1], timeout=0); lock.acquire()\n"
                "except Timeout:\n    sys.exit(0)\n"
                "else:\n    lock.release(); sys.exit(9)\n")
        child = subprocess.run([sys.executable, '-I', '-B', '-c', code,
            str(capture.service.root / '.recording.lock')], capture_output=True, timeout=10)
        assert child.returncode == 0, child.stderr

        # A second failed release must not open another microphone or discard
        # a committed original. Native silence alone cannot release admission.
        with pytest.raises(RuntimeError, match='admission lock'):
            capture.service.capture(capture.device.id, options=capture.options)
        assert retained.release_attempts == 2 and len(launches) == 1
        assert retained.release_retry_required and not retained.closed
        if first is not None:
            assert capture.service.get(first['id']) == first

        result = capture.service.capture(capture.device.id, options=capture.options)
        assert capture.service.get(result['id']) == result
        assert len(launches) == 2
        assert retained.release_attempts == 3 and retained.closed
        assert not retained.release_retry_required
        assert retained not in recording._PENDING_CAPTURES
        if first is not None:
            assert first['id'] != result['id']
            assert capture.service.get(first['id']) == first
        with FileLock(capture.service.root / '.recording.lock', timeout=0):
            pass
        assert recording._CAPTURE.acquire(blocking=False)
        recording._CAPTURE.release()
    finally:
        # A failing assertion must not poison later tests or leave an owned
        # file reservation behind. This is fixture cleanup, not production retry.
        owner.confirm()
        if reservations:
            monkeypatch.setattr(reservations[0].lock, 'release', releases[0])
        for reservation in reservations:
            reservation.close()
