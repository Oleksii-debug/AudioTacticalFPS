"""Actual FFmpeg synthetic capture; no physical microphone qualification."""
from dataclasses import replace
from hashlib import sha256
import json
import os
import subprocess
import sys
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
        pytest.skip('Actual FFmpeg tools are required for synthetic capture.')
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


def test_capture_holds_shared_audio_budget_and_microphone_until_native_completion(capture):
    from carr_library.operations import ResourceBroker
    broker = ResourceBroker.for_app(capture.app.root)
    seen = []
    def inspect_resources(seconds):
        if not seen:
            seen.append(broker.snapshot())
    result = capture.service.capture(capture.device.id, options=capture.options, progress=inspect_resources)
    assert result['output_verified'] and result['receipt_finalized']
    assert seen and len(seen[0]['leases']) == 1
    request = seen[0]['leases'][0]['request']
    assert request['devices'] == ['microphone:' + capture.device.id]
    assert request['cpu_slots'] == 1 and request['memory_bytes'] >= 512 * 1024**2
    assert request['disk_bytes'] >= 24000 * 2 + 32 * 1024**2
    assert broker.snapshot()['leases'] == []


def test_refused_shared_budget_never_opens_a_microphone(capture, monkeypatch):
    from carr_library.operations import ResourceBroker, ResourceUnavailable
    broker = ResourceBroker.for_app(capture.app.root)
    broker.configure(replace(broker.limits, memory_bytes=1))
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Admission opened a microphone'))
    with pytest.raises(ResourceUnavailable):
        capture.service.capture(capture.device.id, options=capture.options)
    assert broker.snapshot()['leases'] == []
    assert not list(capture.service.root.glob('*.wav'))


def test_complete_backup_refuses_an_active_capture_then_retains_completed_input(capture, tmp_path):
    denied, unexpected = [], []
    backup = tmp_path / 'during-capture'
    def attempt_backup(seconds):
        if denied or unexpected:
            return
        try:
            unexpected.append(capture.app.backup(backup))
        except RuntimeError as exc:
            denied.append(str(exc))
    result = capture.service.capture(capture.device.id, options=capture.options, progress=attempt_backup)
    assert denied and not unexpected
    assert 'another operation' in denied[0]
    digest = capture.app.backup(tmp_path / 'after-capture')
    restored = restore_backup(tmp_path / 'after-capture', tmp_path / 'restored-capture', digest)
    reopened = RecordingService(restored).get(result['id'])
    assert reopened['audio'] == result['audio'] and reopened['output_verified']


@pytest.mark.parametrize('exception', [OSError, KeyboardInterrupt])
def test_post_link_writer_interruption_reports_the_verified_saved_recording(capture, monkeypatch, exception):
    actual = recording.publish_file
    def interrupted(staged, output, **options):
        actual(staged, output, **options)
        raise exception('Synthetic writer interruption after publication')
    monkeypatch.setattr(recording, 'publish_file', interrupted)
    result = capture.service.capture(capture.device.id, options=capture.options)
    assert result['status'] == 'complete' and result['output_verified']
    assert result['receipt_finalized'] and result['receipt_status'] == 'complete'
    assert any('writer reported a later interruption' in text for text in result['warnings'])
    assert Path(result['path']).stat().st_nlink == 1
    reopened = capture.service.get(result['id'])
    assert reopened['audio'] == result['audio']
    assert not list(capture.service.root.glob('.pending-*'))


def test_cancel_after_publication_preserves_verified_output_without_starting_another_capture(capture, monkeypatch):
    actual, cancel = recording.publish_file, threading.Event()
    def late_cancel(staged, output, **options):
        actual(staged, output, **options)
        cancel.set()
    monkeypatch.setattr(recording, 'publish_file', late_cancel)
    result = capture.service.capture(capture.device.id, options=capture.options, cancelled=cancel.is_set)
    assert cancel.is_set() and result['status'] == 'complete' and result['output_verified']
    assert capture.service.get(result['id'])['audio'] == result['audio']


@pytest.mark.parametrize('after_replace', [False, True])
def test_recording_receipt_finalization_is_distinct_from_saved_pcm(capture, monkeypatch, after_replace):
    actual = recording.atomic_json
    def finalization(path, value, **options):
        if value.get('schema') == 'carr-recording/1' and value.get('status') == 'complete':
            if after_replace:
                actual(path, value, **options)
            raise OSError('Synthetic receipt interruption')
        return actual(path, value, **options)
    monkeypatch.setattr(recording, 'atomic_json', finalization)
    result = capture.service.capture(capture.device.id, options=capture.options)
    assert result['status'] == 'complete' and result['output_verified']
    assert result['receipt_finalized'] is after_replace
    assert result['receipt_status'] == ('complete' if after_replace else 'prepared')
    assert Path(result['path']).is_file()
    monkeypatch.setattr(recording, 'atomic_json', actual)
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Recovery started a new capture'))
    recovered = capture.service.get(result['id'])
    assert recovered['receipt_finalized'] and recovered['output_verified']
    assert recovered['audio'] == result['audio']


def test_other_equal_byte_file_cannot_complete_this_capture_attempt(capture, monkeypatch):
    def unrelated_copy(staged, output, **options):
        shutil.copyfile(staged, output)
        raise OSError('A competing actor wrote a different inode')
    monkeypatch.setattr(recording, 'publish_file', unrelated_copy)
    with pytest.raises(OSError, match='different inode'):
        capture.service.capture(capture.device.id, options=capture.options)
    saved, = capture.service.list()
    assert saved['status'] == 'prepared'
    assert Path(saved['path']).stat().st_nlink == 1
    assert not list(capture.service.root.glob('.pending-*'))


def test_actual_resource_release_error_does_not_erase_recording_completion(capture, monkeypatch):
    from carr_library.operations.broker import ResourceLease
    from carr_library.operations import ResourceBroker
    actual = ResourceLease.release
    def interrupted(lease):
        actual(lease)
        raise OSError('Synthetic release notification failure after the lock closed')
    monkeypatch.setattr(ResourceLease, 'release', interrupted)
    result = capture.service.capture(capture.device.id, options=capture.options)
    assert result['status'] == 'complete' and result['output_verified']
    assert result['resource_release_confirmed'] is False
    assert any('Shared resource release' in message for message in result['warnings'])
    assert ResourceBroker.for_app(capture.app.root).snapshot()['leases'] == []
    assert capture.service.get(result['id'])['audio'] == result['audio']


def killed_capture(capture, tmp_path, boundary):
    marker = tmp_path / 'native-capture-boundary.json'
    child = r'''
import json, os, sys
from hashlib import sha256
from pathlib import Path
from carr_library.audio import AudioRuntime
from carr_library.audio import recording

root, ffmpeg, ffprobe, marker, boundary, expected_origin = sys.argv[1:]
origin = Path(recording.__file__).resolve()
assert origin == Path(expected_origin).resolve(), (origin, expected_origin)
service = recording.RecordingService(root, runtime=AudioRuntime(Path(root) / "runtime" / "capture",
    ffmpeg=ffmpeg, ffprobe=ffprobe))
alternative = "@device_synthetic_boundary"
device = recording.RecordingDevice(sha256(alternative.encode()).hexdigest(), "Synthetic child", alternative)
service._devices = {device.id: device}
service._engine_hash = recording._digest(ffmpeg)
service._input_arguments = lambda selected: ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000"]
publish = recording.publish_file
def exit_at_publication(staged, output, **options):
    if boundary == "after-link":
        publish(staged, output, **options)
        assert os.path.samefile(staged, output) and output.stat().st_nlink == 2
    Path(marker).write_text(json.dumps({"origin": str(origin), "staged": str(staged),
        "output": str(output), "id": output.stem}), encoding="utf-8")
    os._exit(73)
recording.publish_file = exit_at_publication
service.capture(device.id, options=recording.RecordingOptions(max_seconds=1, sample_rate=24000))
raise AssertionError("The publication boundary was not reached")
'''
    result = subprocess.run([sys.executable, '-B', '-c', child,
        str(capture.app.root), str(capture.service.runtime.ffmpeg), str(capture.service.runtime.ffprobe),
        str(marker), boundary, str(Path(recording.__file__).resolve())],
        cwd=Path(recording.__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    observed = json.loads(marker.read_text(encoding='utf-8'))
    assert Path(observed['origin']) == Path(recording.__file__).resolve()
    return observed


@pytest.mark.parametrize('boundary', ['before-link', 'after-link'])
def test_real_child_exit_recovers_only_the_published_owned_wav(capture, tmp_path, monkeypatch, boundary):
    observed = killed_capture(capture, tmp_path, boundary)
    staged, output = Path(observed['staged']), Path(observed['output'])
    receipt = capture.service.root / (observed['id'] + '.json')
    assert json.loads(receipt.read_text())['status'] == 'prepared'
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Recovery opened a microphone'))
    if boundary == 'before-link':
        assert staged.is_file() and not output.exists()
        before = staged.read_bytes()
        with pytest.raises(FileNotFoundError):
            capture.service.get(observed['id'])
        assert staged.read_bytes() == before and not output.exists()
        assert json.loads(receipt.read_text())['status'] == 'prepared'
        return
    original = output.read_bytes()
    assert output.stat().st_nlink == 2 and os.path.samefile(staged, output)
    recovered = RecordingService(capture.app.root).get(observed['id'])
    assert recovered['status'] == 'complete' and recovered['output_verified']
    assert recovered['receipt_finalized'] and not staged.exists() and not staged.parent.exists()
    assert output.stat().st_nlink == 1 and output.read_bytes() == original
    # The recovered input is accepted by ordinary strict source/backup guards.
    backup = tmp_path / 'recovered-capture-backup'
    digest = capture.app.backup(backup)
    shutil.rmtree(capture.app.root)
    restored = restore_backup(backup, tmp_path / 'restored-after-process-death', digest)
    saved = RecordingService(restored).get(observed['id'])
    assert Path(saved['path']).read_bytes() == original and saved['audio'] == recovered['audio']


@pytest.mark.parametrize('alteration', ['foreign-alias', 'extra-entry', 'third-link', 'changed-bytes'])
def test_restart_preserves_foreign_aliases_and_unowned_staging_entries(
        capture, tmp_path, monkeypatch, alteration):
    observed = killed_capture(capture, tmp_path, 'after-link')
    staged, output = Path(observed['staged']), Path(observed['output'])
    foreign = tmp_path / 'unrelated-owner.wav'
    extra = staged.parent / 'unrelated-owner.txt'
    if alteration in ('foreign-alias', 'third-link'):
        os.link(output, foreign)
        if alteration == 'foreign-alias':
            staged.unlink()
    elif alteration == 'extra-entry':
        extra.write_bytes(b'Preserve this unrelated content')
    else:
        with staged.open('ab') as stream:
            stream.write(b'changed through the surviving alias')
    paths = [path for path in (staged, output, foreign, extra) if path.exists()]
    before = {path: path.read_bytes() for path in paths}
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Recovery opened a microphone'))
    with pytest.raises(ValueError):
        capture.service.get(observed['id'])
    assert {path: path.read_bytes() for path in paths} == before
    assert json.loads((capture.service.root / (observed['id'] + '.json')).read_text())['status'] == 'prepared'


def test_legacy_complete_recording_receipt_is_read_without_rewriting(capture):
    result = capture.service.capture(capture.device.id, options=capture.options)
    path = capture.service.root / (result['id'] + '.json')
    value = json.loads(path.read_text())
    value.pop('status')
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    path.write_bytes(raw)
    verified = capture.service.get(result['id'])
    assert verified['output_verified'] and verified['receipt_finalized']
    assert verified['receipt_status'] == 'complete' and path.read_bytes() == raw


def test_recording_inventory_checks_all_entries_incrementally(capture, monkeypatch):
    # Non-receipt files still count; filtering cannot bypass the total bound.
    for number in range(4):
        (capture.service.root / ('unrelated-' + str(number))).write_bytes(b'Preserve')
    monkeypatch.setattr(recording, '_MAX_RECORDING_ENTRIES', 3)
    with pytest.raises(ValueError, match='total entry allowance'):
        capture.service.list()
    assert len(tuple(capture.service.root.iterdir())) == 4


def test_recording_inventory_cancellation_and_scandir_failure_are_explicit(capture, monkeypatch):
    (capture.service.root / 'unrelated').write_bytes(b'Preserve')
    with pytest.raises(AudioCancelled, match='list cancelled'):
        capture.service.list(cancelled=lambda: True)
    actual = recording.os.scandir
    def denied(path):
        if Path(path) == capture.service.root:
            raise PermissionError('Synthetic directory enumeration denial')
        return actual(path)
    monkeypatch.setattr(recording.os, 'scandir', denied)
    with pytest.raises(PermissionError, match='enumeration denial'):
        capture.service.list()
    assert (capture.service.root / 'unrelated').read_bytes() == b'Preserve'


def test_owned_stage_read_failure_preserves_both_names_and_reports_cleanup(capture, tmp_path, monkeypatch):
    observed = killed_capture(capture, tmp_path, 'after-link')
    staged, output = Path(observed['staged']), Path(observed['output'])
    before = output.read_bytes()
    actual = recording.os.scandir
    def denied(path):
        if Path(path) == staged.parent:
            raise PermissionError('Synthetic owned staging enumeration denial')
        return actual(path)
    monkeypatch.setattr(recording.os, 'scandir', denied)
    recovered = capture.service.get(observed['id'])
    assert recovered['status'] == 'complete' and recovered['output_verified']
    assert recovered['cleanup_pending']
    assert os.path.samefile(staged, output) and output.read_bytes() == before
    monkeypatch.setattr(recording.os, 'scandir', actual)
    final = capture.service.get(observed['id'])
    assert final['output_verified'] and output.stat().st_nlink == 1 and not staged.parent.exists()


def test_failed_owned_unlink_is_saved_output_with_recoverable_cleanup(capture, monkeypatch):
    actual = Path.unlink
    def deny_owned(path, *args, **kwargs):
        if path.name == 'capture.wav':
            raise PermissionError('Synthetic owned capture-link cleanup denial')
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', deny_owned)
    saved = capture.service.capture(capture.device.id, options=capture.options)
    assert saved['status'] == 'complete' and saved['output_verified']
    assert saved['cleanup_pending'] and saved['receipt_finalized']
    output = Path(saved['path'])
    staged = capture.service.root / ('.pending-recording-' + saved['id']) / 'capture.wav'
    assert os.path.samefile(output, staged) and output.stat().st_nlink == 2
    monkeypatch.setattr(Path, 'unlink', actual)
    recovered = capture.service.get(saved['id'])
    assert recovered['output_verified'] and output.stat().st_nlink == 1
    assert not staged.parent.exists()
