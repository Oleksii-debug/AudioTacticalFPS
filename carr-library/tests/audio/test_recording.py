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
from carr_library.audio.recording import AudioCancelled, AudioError, AudioUnavailable
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.progress import ProgressHub
from carr_library.audio import recording
from carr_library.audio.recording import RecordingDevice, RecordingOptions, RecordingService, parse_devices
from carr_library.library_backup import restore_backup
from carr_library.ingest.restore import RestoreError


INVENTORY = br'''[dshow @ 123] "Camera" (video)
[dshow @ 123]   Alternative name "@device_pnp_camera"
[dshow @ 123] "Microphone USB" (audio)
[dshow @ 123]   Alternative name "@device_cm_{123}\wave_{456}"
[dshow @ 123] "Microphone USB" (audio)
[dshow @ 123]   Alternative name "@device_cm_{123}\wave_{789}"
'''


@pytest.fixture
def capture(tmp_path, monkeypatch):
    binary = resolve_ffmpeg()
    if binary is None:
        pytest.fail('Actual FFmpeg is required for synthetic capture.')
    ffmpeg = str(binary)
    app = LibraryApp(tmp_path / 'library')
    hub = ProgressHub()
    service = RecordingService(app.root, runtime=SimpleNamespace(ffmpeg=ffmpeg), progress_hub=hub)
    device = parse_devices(INVENTORY)[0]
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(ffmpeg)
    # Explicitly test-only source adapter. Production exposes only enumerated
    # DirectShow devices, never arbitrary lavfi expressions or input argv.
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    return SimpleNamespace(app=app, service=service, device=device, hub=hub,
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
    from carr_library.audio.cleanup import AudioCleanupPending
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
            assert capture.hub.media_active
            from carr_library.operations.broker import ResourceBroker
            active = ResourceBroker.for_app(capture.app.root).snapshot()
            assert active['exclusive_devices'] == ['microphone:' + capture.device.id]
            assert len(active['leases']) == 1
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
    assert not capture.hub.media_active
    assert ResourceBroker.for_app(capture.app.root).snapshot()['leases'] == []
    assert recording._CAPTURE.acquire(blocking=False)
    recording._CAPTURE.release()
    with FileLock(capture.service.root / '.recording.lock', timeout=0):
        pass


def test_pcm_validation_cancellation_uses_the_shared_bounded_wave_reader(capture):
    result = capture.service.capture(capture.device.id, options=capture.options)
    with pytest.raises(AudioCancelled):
        capture.service._audio(Path(result['path']), capture.options, lambda: True)


def test_actual_retake_retains_originals_and_uses_current_managed_sound_assets(capture, tmp_path, monkeypatch):
    from carr_library.audio.models import Sound
    from carr_library.ui.audio_studio import StudioController

    original = capture.service.capture(capture.device.id, options=capture.options)
    original_bytes = Path(original['path']).read_bytes()
    monkeypatch.setattr(capture.service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=24000'])
    retake = capture.service.capture(capture.device.id, options=capture.options,
                                    title='Explicit second take', retake_of=original['id'])
    assert retake['schema'] == 'carr-recording/2'
    assert retake['retake'] == {'origin_id': original['id'], 'previous_id': original['id'],
                                'previous_sha256': original['audio']['sha256']}
    assert retake['audio']['sha256'] != original['audio']['sha256']
    assert Path(original['path']).read_bytes() == original_bytes
    assert capture.service.get(original['id']) == original
    controller = StudioController(private_root=capture.app.root)
    controller.new('[pause 1]', input_kind='script')
    controller.save_in_library()
    attached = capture.service.retain(retake['id'], controller.project_id)
    assert attached['asset'] == retake['audio']['sha256'] + '.wav'
    controller.edit_script('[sound ' + attached['asset'] + ']')
    controller.save()
    event, = controller.plan().scenes[0].events
    assert isinstance(event, Sound) and event.asset == attached['asset']
    retained = capture.app.root / 'audio-projects' / controller.project_id / 'audio' / 'assets' / attached['asset']
    assert retained.read_bytes() == Path(retake['path']).read_bytes()
    assert retained.stat().st_nlink == Path(retake['path']).stat().st_nlink == 1

    # Remove every old library recording and asset after making the real backup.
    backup, restored = tmp_path / 'takes-backup', tmp_path / 'takes-restored'
    checksum = capture.app.backup(backup)
    old_root = capture.app.root
    relative_asset = retained.relative_to(old_root)
    shutil.rmtree(old_root)
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    recovered = RecordingService(restored, runtime=capture.service.runtime)
    assert Path(recovered.get(original['id'])['path']).read_bytes() == original_bytes
    assert recovered.get(retake['id'])['retake'] == retake['retake']
    assert (restored / relative_asset).read_bytes() == Path(recovered.get(retake['id'])['path']).read_bytes()
    assert recovered._devices == {} and recovered._engine_hash is None


@pytest.mark.parametrize('field', ['previous_sha256', 'origin_id'])
def test_retake_binding_cannot_be_changed_or_backed_up(capture, tmp_path, field):
    original = capture.service.capture(capture.device.id, options=capture.options)
    result = capture.service.capture(capture.device.id, options=capture.options,
                                     retake_of=original['id'])
    retained = Path(original['path']).read_bytes()
    receipt = capture.service.root / (result['id'] + '.json')
    value = json.loads(receipt.read_text(encoding='utf-8'))
    value['retake'][field] = '0' * (64 if field.endswith('sha256') else 32)
    receipt.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='retake'):
        capture.service.get(result['id'])
    with pytest.raises(ValueError, match='retake'):
        capture.app.backup(tmp_path / 'rejected-take')
    assert not (tmp_path / 'rejected-take').exists()
    assert Path(original['path']).read_bytes() == retained


def test_actual_capture_holds_shared_media_and_resource_admission_until_completion(capture):
    from carr_library.operations.broker import ResourceBroker
    seen = []

    def progress(seconds):
        assert capture.hub.media_active
        snapshot = ResourceBroker.for_app(capture.app.root).snapshot()
        assert snapshot['exclusive_devices'] == ['microphone:' + capture.device.id]
        assert len(snapshot['leases']) == 1
        seen.append(seconds)

    result = capture.service.capture(capture.device.id, options=capture.options, progress=progress)
    assert seen and capture.service.get(result['id']) == result
    assert not capture.hub.media_active
    assert ResourceBroker.for_app(capture.app.root).snapshot()['leases'] == []


def test_capture_and_pending_cleanup_are_excluded_from_complete_backup(capture, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    original = recording.run_process

    def held(args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(args, **kwargs)

    monkeypatch.setattr(recording, 'run_process', held)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(capture.service.capture, capture.device.id, options=capture.options)
        try:
            assert entered.wait(5)
            with pytest.raises(RestoreError, match='recording|cleanup'):
                capture.app.backup(tmp_path / 'busy-backup')
            assert not (tmp_path / 'busy-backup').exists()
        finally:
            release.set()
        result = future.result(timeout=15)
    assert capture.service.get(result['id']) == result


def test_receipt_write_failure_keeps_an_inert_original_in_backup(capture, tmp_path, monkeypatch):
    publish = recording.atomic_json

    def fail_receipt(path, value, *args, **kwargs):
        if Path(path).parent == capture.service.root:
            raise OSError('Synthetic receipt storage failure')
        return publish(path, value, *args, **kwargs)

    monkeypatch.setattr(recording, 'atomic_json', fail_receipt)
    with pytest.raises(AudioError, match='WAV was retained'):
        capture.service.capture(capture.device.id, options=capture.options)
    orphan, = recording.unverified_recording_files(capture.app.root)
    original = orphan.read_bytes()
    assert original[:4] == b'RIFF'
    assert capture.service.list() == ()
    backup, restored = tmp_path / 'orphan-backup', tmp_path / 'orphan-restored'
    checksum = capture.app.backup(backup)
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    restored_orphan, = recording.unverified_recording_files(restored)
    assert restored_orphan.read_bytes() == original
    assert not restored_orphan.with_suffix('.json').exists()


@pytest.mark.parametrize('raw', [b'', b'Retained original without a valid WAV header'])
def test_backup_preserves_unverified_original_without_decoding_it(tmp_path, monkeypatch, raw):
    app = LibraryApp(tmp_path / 'orphan-library')
    service = RecordingService(app.root)
    original = service.root / ('a' * 32 + '.wav')
    original.write_bytes(raw)

    def no_audio(*args, **kwargs):
        pytest.fail('An unreceipted retained original was probed or activated')

    monkeypatch.setattr(RecordingService, '_audio', no_audio)
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    checksum = app.backup(backup)
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    recovered, = recording.unverified_recording_files(restored)
    assert recovered.read_bytes() == raw
    assert not recovered.with_suffix('.json').exists()


def test_backup_rejects_unknown_recording_state_instead_of_omitting_it(tmp_path):
    app = LibraryApp(tmp_path / 'unknown-library')
    service = RecordingService(app.root)
    unregistered = service.root / 'unknown-original.wav'
    unregistered.write_bytes(b'Original data without a supported retained identity')
    with pytest.raises(ValueError, match='Unknown retained recording'):
        app.backup(tmp_path / 'refused-backup')
    assert not (tmp_path / 'refused-backup').exists()
    assert unregistered.read_bytes().startswith(b'Original data')


def test_backup_checks_copied_recording_against_its_receipt_before_publication(capture, tmp_path, monkeypatch):
    from carr_library import library_backup

    result = capture.service.capture(capture.device.id, options=capture.options)
    original_bytes = Path(result['path']).read_bytes()
    copy = library_backup._copy_sidecar

    def corrupted_copy(root, name, destination, *args, **kwargs):
        entry = copy(root, name, destination, *args, **kwargs)
        if str(name).startswith('state/studio/recordings/') and str(name).endswith('.wav'):
            value = bytearray(Path(destination).read_bytes())
            value[-1] ^= 1
            Path(destination).write_bytes(value)
        return entry

    monkeypatch.setattr(library_backup, '_copy_sidecar', corrupted_copy)
    with pytest.raises(ValueError, match='recording receipt|original PCM'):
        capture.app.backup(tmp_path / 'corrupt-copy')
    assert not (tmp_path / 'corrupt-copy').exists()
    assert Path(result['path']).read_bytes() == original_bytes



@pytest.mark.parametrize('extension,size,message', [
    ('wav', 2 * 1024**3 + 1, 'recording sidecar byte limit'),
    ('json', 64 * 1024 + 1, 'recording sidecar byte limit'),
    ('json', 0, 'invalid backup sidecar manifest entry'),
])
def test_recording_manifest_limits_fail_before_opening_payload_files(tmp_path, monkeypatch, extension, size, message):
    from carr_library import library_backup
    app = LibraryApp(tmp_path / 'manifest-library')
    service = RecordingService(app.root)
    original = service.root / ('a' * 32 + '.wav')
    original.write_bytes(b'Unverified original remains unchanged')
    backup = tmp_path / 'backup'
    app.backup(backup)
    path = backup / library_backup.MANIFEST
    manifest = json.loads(path.read_text(encoding='utf-8'))
    entry = next(item for item in manifest['sidecars']
                 if item['path'].startswith('state/studio/recordings/'))
    entry['path'] = 'state/studio/recordings/' + 'a' * 32 + '.' + extension
    entry['size_bytes'] = size
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    path.write_bytes(raw)
    monkeypatch.setattr(library_backup, '_verify_sidecar_tree',
        lambda *args, **kwargs: pytest.fail('Invalid typed metadata reached payload inspection'))
    with pytest.raises(RestoreError, match=message):
        library_backup._read_manifest(backup, sha256(raw).hexdigest())
    assert original.read_bytes() == b'Unverified original remains unchanged'
