"""Real FFmpeg recording with a modeled output sink; native output is separate."""
from copy import deepcopy
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import recording, recording_monitor as monitor
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.models import AudioError, AudioUnavailable
from carr_library.speech.output_devices import OutputDevice


# Reuse the existing real-FFmpeg source adapter without changing package origin
# in the installed --import-mode=importlib lane.
_spec = spec_from_file_location('_recording_capture_fixture',
                               Path(__file__).with_name('test_recording.py'))
_helpers = module_from_spec(_spec)
_spec.loader.exec_module(_helpers)

DEVICE = OutputDevice(2, 'Explicit monitoring fixture', 'monitor-fixture-interface')


@pytest.fixture
def capture(tmp_path, monkeypatch):
    return _helpers.capture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def sinks(monkeypatch):
    created = []

    class Sink:
        def __init__(self):
            self.blocks, self.closed, self.device = [], False, None
            created.append(self)
        def open(self, sample_rate, channels, device, *, cancelled):
            assert sample_rate == 24000 and channels == 1
            assert not cancelled()
            self.device = device
        def submit(self, data):
            assert not self.closed and type(data) is bytes and len(data) <= 2400
            self.blocks.append(data)
            return True
        def close(self):
            self.closed = True

    monkeypatch.setattr(monitor, 'DryMonitor', Sink)
    return created


def pcm(path):
    with wave.open(str(path), 'rb') as reader:
        return reader.getparams(), reader.readframes(reader.getnframes())


def receipt():
    return {'schema': 'carr-recording/3', 'id': 'a' * 32, 'filename': 'a' * 32 + '.wav',
        'title': 'Synthetic monitored receipt', 'created': '2026-11-27T12:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64,
                   'name': 'Explicit fixture device'},
        'ffmpeg_sha256': 'c' * 64, 'completion': 'device-ended',
        'audio': {'sha256': 'd' * 64, 'size_bytes': 50, 'sample_rate': 24000,
                  'channels': 1, 'frames': 3, 'sample_width': 2, 'duration_seconds': 3 / 24000},
        'limits': {'max_seconds': 1, 'sample_rate': 24000, 'channels': 1, 'max_bytes': 100000},
        'monitoring': {'enabled': True, 'output_identity': DEVICE.identity,
                       'submitted_frames': 2, 'dropped_frames': 1}}


def test_monitored_receipt_is_pure_and_preserves_historical_envelopes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Receipt validation accessed files or devices.')
    for name in ('local_path', 'regular', 'read_json'):
        monkeypatch.setattr(recording, name, forbidden)
    original = receipt()
    for retake in (False, True):
        value = deepcopy(original)
        if retake:
            value['retake'] = {'origin_id': 'e' * 32, 'previous_id': 'f' * 32,
                               'previous_sha256': '1' * 64}
        before = deepcopy(value)
        assert recording.validate_recording_receipt(value) is value and value == before
        value.pop('monitoring')
        value['schema'] = 'carr-recording/2' if retake else 'carr-recording/1'
        before = deepcopy(value)
        assert recording.validate_recording_receipt(value) is value and value == before


@pytest.mark.parametrize('change', [
    {'enabled': False}, {'enabled': 1}, {'output_identity': 'A' * 64},
    {'output_identity': 'a' * 64 + '\0suffix'},
    {'submitted_frames': True}, {'submitted_frames': -1},
    {'dropped_frames': 0.5}, {'dropped_frames': -1},
    {'submitted_frames': 3, 'dropped_frames': 1}, {'unexpected': 1},
], ids=['disabled', 'integer-enabled', 'uppercase-identity', 'nul-identity',
        'boolean-submitted', 'negative-submitted', 'fractional-dropped',
        'negative-dropped', 'more-than-capture', 'unknown-field'])
def test_monitor_receipt_rejects_invalid_counter_or_identity_claims(change):
    value = receipt()
    value['monitoring'].update(change)
    with pytest.raises(ValueError, match='monitoring receipt'):
        recording.validate_recording_receipt(value)


@pytest.mark.parametrize('schema', ['carr-recording/1', 'carr-recording/2'])
def test_historical_schema_cannot_smuggle_monitoring_fields(schema):
    value = receipt()
    value['schema'] = schema
    if schema.endswith('/2'):
        value['retake'] = {'origin_id': 'e' * 32, 'previous_id': 'f' * 32,
                           'previous_sha256': '1' * 64}
    with pytest.raises(ValueError, match='receipt'):
        recording.validate_recording_receipt(value)


def test_schema_three_requires_its_monitoring_declaration():
    value = receipt()
    value.pop('monitoring')
    with pytest.raises(ValueError, match='receipt'):
        recording.validate_recording_receipt(value)


def test_actual_capture_pcm_is_unchanged_by_dry_monitoring(capture, sinks):
    plain = capture.service.capture(capture.device.id, options=capture.options)
    assert plain['schema'] == 'carr-recording/1' and not sinks
    measured = []
    monitored = capture.service.capture(capture.device.id, options=capture.options,
        monitor_output=DEVICE, progress=measured.append)
    assert len(sinks) == 1 and sinks[0].closed and sinks[0].blocks and measured
    assert monitored['schema'] == 'carr-recording/3'
    details = monitored['monitoring']
    assert details['enabled'] is True and details['output_identity'] == DEVICE.identity
    assert details['submitted_frames'] > 0
    assert details['submitted_frames'] + details['dropped_frames'] == monitored['audio']['frames']
    assert pcm(plain['path']) == pcm(monitored['path'])
    assert capture.service.get(monitored['id']) == monitored
    assert capture.service.get(plain['id']) == plain
    assert not list(capture.service.root.glob('.pending-*'))


def test_actual_monitored_and_unmonitored_retakes_keep_the_same_original_lineage(capture, sinks):
    first = capture.service.capture(capture.device.id, options=capture.options, monitor_output=DEVICE)
    first_bytes = Path(first['path']).read_bytes()
    second = capture.service.capture(capture.device.id, options=capture.options, retake_of=first['id'])
    third = capture.service.capture(capture.device.id, options=capture.options,
                                    retake_of=second['id'], monitor_output=DEVICE)
    assert [item['schema'] for item in (first, second, third)] == [
        'carr-recording/3', 'carr-recording/2', 'carr-recording/3']
    assert second['retake']['origin_id'] == third['retake']['origin_id'] == first['id']
    assert third['retake']['previous_id'] == second['id']
    assert third['retake']['previous_sha256'] == second['audio']['sha256']
    assert Path(first['path']).read_bytes() == first_bytes
    assert all(capture.service.get(item['id']) == item for item in (first, second, third))
    assert len(sinks) == 2 and all(sink.closed for sink in sinks)
    path = capture.service.root / (third['id'] + '.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    value['retake']['previous_sha256'] = '0' * 64
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='retained original identity'):
        capture.service.get(third['id'])


def test_monitor_failure_is_not_swallowed_as_an_optional_progress_observer(capture, monkeypatch):
    closed, submitted = [], []
    class FailedMonitor:
        def open(self, *args, **kwargs): pass
        def submit(self, data):
            submitted.append(data)
            raise AudioError('Native monitor stopped accepting buffers.')
        def close(self): closed.append(True)
    monkeypatch.setattr(monitor, 'DryMonitor', FailedMonitor)
    with pytest.raises(AudioError, match='stopped accepting'):
        capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=3),
                                monitor_output=DEVICE, progress=lambda seconds: None)
    assert submitted and closed
    assert not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('*.json'))
    assert not list(capture.service.root.glob('.pending-*'))


def test_removed_output_fails_before_a_capture_child_can_start(capture, monkeypatch):
    closed = []
    class MissingMonitor:
        def open(self, *args, **kwargs):
            raise AudioUnavailable('The selected monitoring output was removed.')
        def close(self): closed.append(True)
    monkeypatch.setattr(monitor, 'DryMonitor', MissingMonitor)
    monkeypatch.setattr(recording, 'run_process',
                        lambda *args, **kwargs: pytest.fail('A microphone child was launched.'))
    with pytest.raises(AudioUnavailable, match='removed'):
        capture.service.capture(capture.device.id, options=capture.options, monitor_output=DEVICE)
    assert closed and not list(capture.service.root.glob('.pending-*'))


@pytest.mark.parametrize('wrapped', [False, True], ids=['direct-owner', 'masked-owner'])
@pytest.mark.parametrize('first', ['monitor', 'process'], ids=['monitor-first', 'process-first'])
def test_capture_locks_and_private_stage_wait_for_both_process_and_monitor(
        capture, monkeypatch, wrapped, first):
    from filelock import FileLock, Timeout
    from carr_library.operations import ResourceBroker
    child_done, driver_done = threading.Event(), threading.Event()
    process_owner = CleanupOwner(child_done.is_set)
    owners, stages = [], []

    class HeldMonitor:
        def open(self, *args, **kwargs): pass
        def close(self):
            if not driver_done.is_set():
                raise AudioError('Native buffers are still held by the output driver.')
    monkeypatch.setattr(monitor, 'DryMonitor', HeldMonitor)

    def controlled_pending(attempt, *, message):
        value = AudioCleanupPending(message, cleanup_owner=CleanupOwner(attempt))
        owners.append(value.cleanup_owner)
        return value
    monkeypatch.setattr(monitor, 'pending_cleanup', controlled_pending)

    def pending_capture(arguments, **kwargs):
        assert kwargs['owned_tree'] is True
        staged = Path(arguments[-1])
        staged.write_bytes(b'This private incomplete WAV remains owned.')
        stages.append(staged)
        pending = AudioCleanupPending('The capture process is still exiting.',
                                      cleanup_owner=process_owner)
        if wrapped:
            raise OSError('Status serialization failed during cleanup.') from pending
        raise pending
    monkeypatch.setattr(recording, 'run_process', pending_capture)
    try:
        with pytest.raises(AudioCleanupPending) as caught:
            capture.service.capture(capture.device.id, options=capture.options, monitor_output=DEVICE)
        owner = caught.value.cleanup_owner
        assert len(owners) == 1 and owner is not process_owner
        # capture joins every causal owner, including the original process
        # error below the monitor's combined error. Its public join need not
        # be the monitor's private token, but must retain both exact owners.
        assert set(recording._pending_owners(caught.value)) == {owner, owners[0], process_owner}
        assert not owner.quiescent and not owners[0].quiescent and not process_owner.quiescent
        assert caught.value.recording_reservation.owner is owner
        assert stages[0].is_file() and recording._CAPTURE.locked()
        lock = FileLock(capture.service.root / '.recording.lock', timeout=0)
        with pytest.raises(Timeout):
            lock.acquire()
        broker = ResourceBroker.for_app(capture.app.root)
        assert len(broker.snapshot()['leases']) == 1
        first_done, last_done = ((driver_done, child_done) if first == 'monitor'
                                 else (child_done, driver_done))
        first_done.set()
        assert owner.close() is False and stages[0].is_file() and recording._CAPTURE.locked()
        assert not owner.quiescent and len(broker.snapshot()['leases']) == 1
        with pytest.raises(Timeout):
            lock.acquire()
        last_done.set()
        assert owner.close() is True and not stages[0].exists() and not recording._CAPTURE.locked()
        assert owner.quiescent and owners[0].quiescent and process_owner.quiescent
        assert broker.snapshot()['leases'] == []
        with lock:
            assert lock.is_locked
        assert not list(capture.service.root.glob('*.wav'))
        assert not list(capture.service.root.glob('*.json'))
    finally:
        driver_done.set()
        child_done.set()
        for owner in owners:
            assert owner.close()
