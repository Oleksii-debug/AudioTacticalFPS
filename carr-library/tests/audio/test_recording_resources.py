"""Real FFmpeg test-source captures under shared cooperative resource leases."""
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading

import pytest

from carr_library.audio import AudioCancelled
from carr_library.audio import process, recording
from carr_library.operations import (ResourceBroker, ResourceLimits, ResourceRequest,
    ResourceStateError, ResourceTimeout)


_spec = importlib.util.spec_from_file_location('resource_recording_fixtures',
    Path(__file__).with_name('test_recording.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
capture = _fixtures.capture


def policy(capture):
    broker = ResourceBroker.for_app(capture.app.root, limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1024**3, disk_bytes=2 * 1024**3, min_free_disk_bytes=0))
    capture.service._broker = broker
    return broker


@contextmanager
def capturing(capture, **kwargs):
    cancel, stop, result, errors = threading.Event(), threading.Event(), [], []
    def execute():
        try:
            result.append(capture.service.capture(capture.device.id,
                options=capture.options, cancelled=cancel.is_set,
                stop_requested=stop.is_set, **kwargs))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    try:
        yield thread, cancel, stop, result, errors
    finally:
        cancel.set()
        thread.join(8)
        assert not thread.is_alive(), 'The owned capture did not finish cleanup.'


def observe_wait(broker, monkeypatch):
    waiting, original = threading.Event(), broker._reason
    def reason(request, volumes, active, limits):
        value = original(request, volumes, active, limits)
        if request.devices and value is not None:
            waiting.set()
        return value
    monkeypatch.setattr(broker, '_reason', reason)
    return waiting


def test_actual_capture_holds_microphone_and_budgets_through_cleanup_and_publication(capture, monkeypatch):
    broker = policy(capture)
    original_run, original_stop, original_publish = recording.run_process, process._terminate, recording.publish_file
    observed = []
    def held(stage):
        snapshot = broker.snapshot()
        assert len(snapshot['leases']) == 1
        request = snapshot['leases'][0]['request']
        assert request['cpu_slots'] == 1 and request['memory_bytes'] > 0
        assert request['disk_bytes'] >= 2 * 24000 * 2
        assert snapshot['exclusive_devices'] == ['microphone:' + capture.device.id]
        observed.append(stage)
    def run(*args, **kwargs):
        held('launch')
        return original_run(*args, **kwargs)
    def cleanup(child):
        try:
            held('cleanup')
        finally:
            original_stop(child)
        assert child.poll() is not None
    def publish(*args, **kwargs):
        held('publish')
        return original_publish(*args, **kwargs)
    monkeypatch.setattr(recording, 'run_process', run)
    monkeypatch.setattr(process, '_terminate', cleanup)
    monkeypatch.setattr(recording, 'publish_file', publish)
    result = capture.service.capture(capture.device.id, options=capture.options)
    assert {'launch', 'cleanup', 'publish'} <= set(observed)
    assert result['audio']['frames'] == 24000
    assert capture.service.get(result['id']) == result
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('action', ['stop', 'cancel'])
def test_stop_and_cancel_during_actual_capture_keep_distinct_commit_behavior(capture, action):
    broker = policy(capture)
    stop, cancel, observed = threading.Event(), threading.Event(), []
    def progress(seconds):
        assert broker.snapshot()['exclusive_devices'] == ['microphone:' + capture.device.id]
        if seconds >= .2:
            observed.append(seconds)
            (stop if action == 'stop' else cancel).set()
    kwargs = dict(options=replace(capture.options, max_seconds=8), progress=progress,
        stop_requested=stop.is_set, cancelled=cancel.is_set)
    if action == 'stop':
        result = capture.service.capture(capture.device.id, **kwargs)
        assert result['completion'] == 'stopped'
        assert .15 <= result['audio']['duration_seconds'] < 4
        assert capture.service.get(result['id']) == result
    else:
        with pytest.raises(AudioCancelled):
            capture.service.capture(capture.device.id, **kwargs)
        assert not list(capture.service.root.glob('*.wav'))
        assert not list(capture.service.root.glob('*.json'))
    assert observed and broker.snapshot()['leases'] == []
    assert not list(capture.service.root.glob('.pending-*'))


@pytest.mark.parametrize('action', ['stop', 'cancel'])
def test_stop_or_cancel_while_waiting_never_opens_microphone(capture, monkeypatch, action):
    broker = policy(capture)
    waiting = observe_wait(broker, monkeypatch)
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('A waiting microphone was opened.'))
    request = ResourceRequest(cpu_slots=0, devices=('microphone:' + capture.device.id,))
    with broker.reserve(request, operation_id='other-microphone-owner', timeout=0):
        with capturing(capture) as state:
            thread, cancel, stop, result, errors = state
            assert waiting.wait(5)
            (stop if action == 'stop' else cancel).set()
            thread.join(5)
            assert not result and len(errors) == 1 and isinstance(errors[0], AudioCancelled)
            assert 'no microphone was opened' in str(errors[0])
            assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['other-microphone-owner']
    assert not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('.pending-*'))
    assert broker.snapshot()['leases'] == []


def test_wait_timeout_releases_capture_admission_for_the_next_real_capture(capture, monkeypatch):
    broker = policy(capture)
    original = recording.run_process
    with monkeypatch.context() as patch:
        patch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Timed-out capture started.'))
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='other-heavy-work', timeout=0):
            with pytest.raises(ResourceTimeout):
                capture.service.capture(capture.device.id, options=capture.options, resource_timeout=.06)
    assert recording.run_process is original
    result = capture.service.capture(capture.device.id, options=capture.options, resource_timeout=0)
    assert result['audio']['frames'] == 24000
    assert broker.snapshot()['leases'] == []


def test_changed_selected_inventory_during_wait_requires_new_selection(capture, monkeypatch):
    broker = policy(capture)
    waiting = observe_wait(broker, monkeypatch)
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Changed inventory opened a device.'))
    holder = broker.reserve(ResourceRequest(cpu_slots=2), operation_id='other-heavy-work', timeout=0)
    holder.__enter__()
    held = True
    try:
        with capturing(capture) as state:
            thread, _, _, result, errors = state
            assert waiting.wait(5)
            capture.service._devices.clear()
            holder.__exit__(None, None, None)
            held = False
            thread.join(5)
            assert not result and len(errors) == 1 and isinstance(errors[0], ValueError)
            assert 'inventory changed' in str(errors[0])
        assert broker.snapshot()['leases'] == []
        assert not list(capture.service.root.glob('*.wav'))
    finally:
        if held:
            holder.__exit__(None, None, None)


def test_recording_rejects_another_library_policy(capture, tmp_path, monkeypatch):
    other = tmp_path / 'another-library'
    other.mkdir()
    capture.service._broker = ResourceBroker.for_app(other)
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Another policy opened a microphone.'))
    with pytest.raises(ResourceStateError, match='another data folder'):
        capture.service.capture(capture.device.id, options=capture.options)
    assert not list(capture.service.root.glob('*.wav'))
