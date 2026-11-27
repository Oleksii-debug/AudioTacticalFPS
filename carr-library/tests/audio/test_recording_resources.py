"""Real synthetic capture and native-lock admission; no microphone qualification."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioCancelled, AudioCleanupPending, AudioRuntime
from carr_library.audio import process, recording
from carr_library.audio.recording import RecordingDevice, RecordingOptions, RecordingService
from carr_library.operations import ResourceBroker, ResourceRequest, ResourceStateError, ResourceUnavailable


@pytest.fixture
def capture(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.fail('Actual FFmpeg and FFprobe are required for recording admission acceptance.')
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    runtime = AudioRuntime(tmp_path / 'runtime', ffmpeg=ffmpeg, ffprobe=ffprobe)
    service = RecordingService(app.root, runtime=runtime, broker=broker)
    alternative = '@device_synthetic_resource_acceptance'
    device = RecordingDevice(sha256(alternative.encode()).hexdigest(),
                             'Explicit synthetic input', alternative)
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(ffmpeg)
    # This adapter exists only in the test. Production still accepts only a
    # freshly enumerated DirectShow device and never arbitrary input arguments.
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    return service, broker, device, RecordingOptions(
        max_seconds=1, sample_rate=24000, max_bytes=1024 * 1024)


def observe_wait(broker, monkeypatch):
    waiting = threading.Event()
    actual = broker._reason

    def reason(*arguments, **keywords):
        result = actual(*arguments, **keywords)
        if result is not None:
            waiting.set()
        return result

    monkeypatch.setattr(broker, '_reason', reason)
    return waiting


def observe_launch(service, broker, device, monkeypatch):
    launched = threading.Event()
    actual = recording.run_process

    def launch(*arguments, **keywords):
        snapshot = broker.snapshot()
        leases = [value for value in snapshot['leases']
                  if value['operation_id'].startswith('recording:')]
        assert len(leases) == 1
        request = leases[0]['request']
        assert request['cpu_slots'] == 1 and request['memory_bytes'] >= 512 * 1024**2
        assert request['disk_path'] == str(service.root) and request['disk_bytes'] > 0
        assert request['devices'] == ['microphone:' + device.id]
        launched.set()
        return actual(*arguments, **keywords)

    monkeypatch.setattr(recording, 'run_process', launch)
    return launched


HOLDER = r'''
from pathlib import Path
import sys
import time
sys.path.insert(0, sys.argv[1])
from carr_library.operations import ResourceBroker, ResourceRequest

root, device, ready, release = sys.argv[2:]
broker = ResourceBroker.for_app(Path(root))
with broker.reserve(ResourceRequest(cpu_slots=0, devices=('microphone:' + device,)),
                    operation_id='fixture-recording-device', timeout=5):
    Path(ready).write_text('held', encoding='utf-8')
    deadline = time.monotonic() + 20
    while not Path(release).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('The parent did not release its synthetic holder.')
        time.sleep(.02)
'''


@pytest.mark.parametrize('outcome', ['complete', 'stop-waiter', 'cancel-waiter'])
def test_actual_process_device_contention_and_wait_stop(capture, tmp_path, monkeypatch, outcome):
    service, broker, device, options = capture
    ready, release = tmp_path / 'held', tmp_path / 'release'
    script = tmp_path / 'holder.py'
    script.write_text(HOLDER, encoding='utf-8')
    child = subprocess.Popen([sys.executable, '-I', '-B', str(script),
        str(Path(recording.__file__).resolve().parents[2]),
        str(service.library_root), device.id, str(ready), str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stop, cancel = threading.Event(), threading.Event()
    waiting = observe_wait(broker, monkeypatch)
    launched = observe_launch(service, broker, device, monkeypatch)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            if child.poll() is not None:
                pytest.fail(child.communicate()[1].decode('utf-8', errors='replace'))
            if time.monotonic() >= deadline:
                pytest.fail('The actual device holder did not acquire its reservation.')
            time.sleep(.02)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(service.capture, device.id, options=options,
                                     stop_requested=stop.is_set, cancelled=cancel.is_set)
            try:
                assert waiting.wait(10), 'Capture did not reach actual blocked admission.'
                assert not launched.is_set()
                assert len(broker.snapshot()['leases']) == 1
                if outcome == 'complete':
                    release.write_text('release', encoding='utf-8')
                    value = future.result(timeout=15)
                    assert launched.is_set()
                    assert service.get(value['id']) == value
                    assert value['completion'] == 'duration-limit'
                else:
                    (stop if outcome == 'stop-waiter' else cancel).set()
                    with pytest.raises(AudioCancelled, match='waiting'):
                        future.result(timeout=10)
                    assert not launched.is_set()
                    assert not list(service.root.glob('*.wav'))
                    assert not list(service.root.glob('*.json'))
                    # Cancelling a waiter cannot revoke the other process.
                    assert len(broker.snapshot()['leases']) == 1
                    assert child.poll() is None
            finally:
                cancel.set()
                release.write_text('release', encoding='utf-8')
    finally:
        release.write_text('release', encoding='utf-8')
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=5)
        child.communicate()
    assert child.returncode == 0
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('changed', ['runtime', 'device'])
def test_waiting_capture_rechecks_the_selected_input_before_launch(capture, monkeypatch, changed):
    service, broker, device, options = capture
    waiting, cancel = observe_wait(broker, monkeypatch), threading.Event()
    monkeypatch.setattr(recording, 'run_process',
                        lambda *a, **k: pytest.fail('Changed input opened a microphone.'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = None
        try:
            with broker.reserve(ResourceRequest(cpu_slots=0, devices=('microphone:' + device.id,)),
                                operation_id='fixture-input-change', timeout=0):
                future = executor.submit(service.capture, device.id, options=options,
                                         cancelled=cancel.is_set)
                assert waiting.wait(10)
                if changed == 'runtime':
                    service._engine_hash = '0' * 64
                else:
                    service._devices[device.id] = replace(device, name='Refreshed selection')
            with pytest.raises(ValueError, match='changed while waiting'):
                future.result(timeout=10)
        finally:
            cancel.set()
    assert broker.snapshot()['leases'] == []
    assert not list(service.root.glob('*.wav'))


def test_impossible_memory_policy_refuses_before_native_launch(capture, monkeypatch):
    service, broker, device, options = capture
    broker.configure(replace(broker.limits, memory_bytes=64 * 1024**2))
    monkeypatch.setattr(recording, 'run_process',
                        lambda *a, **k: pytest.fail('An impossible request opened a microphone.'))
    with pytest.raises(ResourceUnavailable):
        service.capture(device.id, options=options)
    assert broker.snapshot()['leases'] == []


def test_capture_rejects_a_broker_from_another_data_folder(capture, tmp_path, monkeypatch):
    service, broker, device, options = capture
    other = LibraryApp(tmp_path / 'other')
    service._broker = ResourceBroker.for_app(other.root)
    monkeypatch.setattr(recording, 'run_process',
                        lambda *a, **k: pytest.fail('The wrong policy opened a microphone.'))
    with pytest.raises(ResourceStateError, match='another data folder'):
        service.capture(device.id, options=options)
    assert service._broker.snapshot()['leases'] == []
    assert broker.snapshot()['leases'] == []


def test_actual_graceful_stop_retains_its_device_until_verified_publication(capture, monkeypatch):
    service, broker, device, options = capture
    stop = threading.Event()
    launched = observe_launch(service, broker, device, monkeypatch)
    value = service.capture(device.id, options=replace(options, max_seconds=10),
        stop_requested=stop.is_set,
        progress=lambda seconds: stop.set() if seconds >= .2 else None)
    assert launched.is_set() and value['completion'] == 'stopped'
    assert .15 <= value['audio']['duration_seconds'] < 3
    assert service.get(value['id']) == value
    assert broker.snapshot()['leases'] == []


def test_actual_unconfirmed_capture_keeps_capacity_device_and_capture_lock(capture, monkeypatch):
    from carr_library.audio.windows_job import JobProcess
    service, broker, device, options = capture
    permitted, cancel, notified = threading.Event(), threading.Event(), threading.Event()
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_terminate')
    actual = getattr(target, name)
    attempts = []

    def close_when_permitted(child):
        attempts.append(child)
        if not permitted.is_set():
            raise OSError('Synthetic uncertainty before the actual owned child stops.')
        return actual(child)

    monkeypatch.setattr(target, name, close_when_permitted)
    monkeypatch.setattr(service, '_audio',
                        lambda *a, **k: pytest.fail('Unconfirmed capture reached verification.'))
    owner = None
    try:
        with pytest.raises(AudioCleanupPending) as error:
            service.capture(device.id, options=replace(options, max_seconds=10),
                cancelled=cancel.is_set,
                progress=lambda seconds: cancel.set() if seconds >= .1 else None)
        owner = error.value.cleanup_owner
        owner.add_quiescent_callback(notified.set)
        assert attempts and attempts[0].poll() is None and not owner.quiescent
        assert not notified.is_set()
        snapshot = broker.snapshot()
        assert len(snapshot['leases']) == 1
        assert snapshot['exclusive_devices'] == ['microphone:' + device.id]
        assert snapshot['leases'][0]['request']['cpu_slots'] == 1
        assert list(service.root.glob('.pending-recording-*'))
        assert not list(service.root.glob('*.json'))
        with pytest.raises(RuntimeError, match='cleanup'):
            service.capture(device.id, options=options)
        assert owner.close() is False and attempts[0].poll() is None
        permitted.set()
        assert owner.quiescent_event.wait(10) and notified.wait(10)
        assert broker.snapshot()['leases'] == []
        assert not list(service.root.glob('.pending-recording-*'))
        assert not list(service.root.glob('*.wav'))
        assert not list(service.root.glob('*.json'))
        assert error.value.recording_reservation.closed
    finally:
        permitted.set()
        if owner is not None:
            owner.close()
            owner.quiescent_event.wait(10)


def test_disposable_admission_cleanup_does_not_hide_saved_recording(capture, monkeypatch):
    service, broker, device, options = capture
    actual, denied = Path.unlink, []

    def unlink(path, *arguments, **keywords):
        if path.parent == broker.leases_root and path.suffix == '.json' and not denied:
            denied.append(path)
            raise OSError('Synthetic disposable metadata cleanup failure.')
        return actual(path, *arguments, **keywords)

    monkeypatch.setattr(Path, 'unlink', unlink)
    value = service.capture(device.id, options=options)
    assert denied and 'resource_notice' in value
    retained = dict(value)
    retained.pop('resource_notice')
    assert service.get(value['id']) == retained
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('view', ['saved', 'unverified'])
def test_invalid_recording_entries_are_bounded_before_materialization(tmp_path, monkeypatch, view):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    for index in range(5):
        (service.root / ('ignored-' + str(index))).write_text('synthetic', encoding='utf-8')
    monkeypatch.setattr(recording, '_MAX_RECORDING_ENTRIES', 3)
    with pytest.raises(ValueError, match='inventory exceeds'):
        if view == 'saved':
            service.list()
        else:
            recording.unverified_recording_files(app.root)


@pytest.mark.parametrize('view', ['saved', 'unverified'])
def test_recording_inventory_cancels_between_ignored_entries(tmp_path, monkeypatch, view):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    for index in range(5):
        (service.root / ('ignored-' + str(index))).write_text('synthetic', encoding='utf-8')
    actual, seen = recording.os.scandir, []

    class ObservedDirectory:
        def __init__(self, path):
            self.inner = actual(path)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.inner.close()
        def __iter__(self):
            for entry in self.inner:
                seen.append(entry.name)
                yield entry

    monkeypatch.setattr(recording.os, 'scandir', ObservedDirectory)
    with pytest.raises(AudioCancelled, match='inventory cancelled'):
        if view == 'saved':
            service.list(cancelled=lambda: len(seen) >= 2)
        else:
            recording.unverified_recording_files(app.root, cancelled=lambda: len(seen) >= 2)
    assert len(seen) == 2


@pytest.mark.parametrize('view', ['saved', 'unverified'])
def test_unreadable_recording_inventory_fails_closed(tmp_path, monkeypatch, view):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    actual = recording.os.scandir

    def scan(path):
        if Path(path) == service.root:
            raise PermissionError('Synthetic unavailable recording directory.')
        return actual(path)

    monkeypatch.setattr(recording.os, 'scandir', scan)
    with pytest.raises(PermissionError, match='unavailable recording'):
        if view == 'saved':
            service.list()
        else:
            recording.unverified_recording_files(app.root)
