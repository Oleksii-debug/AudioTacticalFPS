"""Actual FFmpeg pause/resume and retained-byte contracts; synthetic input only."""
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
from carr_library.audio import recording
from carr_library.audio.recording import AudioCancelled, RecordingOptions, RecordingService
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.library_backup import restore_backup
from carr_library.operations.broker import ResourceBroker


@pytest.fixture
def capture(tmp_path, monkeypatch):
    binary = resolve_ffmpeg()
    if binary is None:
        pytest.fail('Actual FFmpeg is required for the paused capture acceptance.')
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root, runtime=SimpleNamespace(ffmpeg=str(binary)))
    alternative = '@device_test_explicit_pause_fixture'
    device = recording.RecordingDevice(sha256(alternative.encode()).hexdigest(), 'Synthetic source', alternative)
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(binary)
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    calls, real_run = [], recording.run_process
    def run(*args, **kwargs):
        item = {'active': True, 'owned_tree': kwargs.get('owned_tree')}
        calls.append(item)
        try:
            return real_run(*args, **kwargs)
        finally:
            item['active'] = False
    monkeypatch.setattr(recording, 'run_process', run)
    return SimpleNamespace(app=app, service=service, device=device, calls=calls,
        options=RecordingOptions(max_seconds=1, sample_rate=24000))


def paused_capture(capture, *, on_pause=None, stop=None, cancel=None, **kwargs):
    pause, states, observation_errors = threading.Event(), [], []
    stop = stop or threading.Event()
    cancel = cancel or threading.Event()
    requested = False
    def progress(seconds):
        nonlocal requested
        if not requested and seconds >= .15:
            requested = True
            pause.set()
    def changed(state, seconds):
        states.append((state, seconds))
        if state == 'paused':
            try:
                assert not any(item['active'] for item in capture.calls)
                assert all(item['owned_tree'] for item in capture.calls)
                if on_pause is not None:
                    on_pause(pause, seconds)
                else:
                    pause.clear()
            except Exception as error:
                observation_errors.append(error)
                pause.clear()
                stop.set()
    result = capture.service.capture(capture.device.id, options=capture.options,
        pause_requested=pause.is_set, state_changed=changed, progress=progress,
        stop_requested=stop.is_set, cancelled=cancel.is_set, **kwargs)
    assert not observation_errors, observation_errors
    return result, states


def test_actual_pause_closes_owned_capture_and_resumes_exact_recorded_duration(capture):
    observations = []
    def paused(pause, seconds):
        assert recording._CAPTURE.locked()
        snapshot = ResourceBroker.for_app(capture.app.root).snapshot()
        assert snapshot['exclusive_devices'] == ['microphone:' + capture.device.id]
        assert not list(capture.service.root.glob('*.wav'))
        with pytest.raises(RuntimeError, match='Another microphone'):
            capture.service.capture(capture.device.id, options=capture.options)
        time.sleep(.06)
        observations.append(seconds)
        pause.clear()
    result, states = paused_capture(capture, on_pause=paused)
    assert len(observations) == 1 and len(capture.calls) == 2
    assert result['schema'] == 'carr-recording/3'
    assert result['completion'] == 'duration-limit'
    assert result['audio']['frames'] == 24000
    assert result['capture']['pause_count'] == 1 and len(result['capture']['parts']) == 2
    assert sum(part['frames'] for part in result['capture']['parts']) == 24000
    with wave.open(result['path'], 'rb') as source:
        pcm = source.readframes(source.getnframes())
    assert len(pcm) == 48000 and sha256(pcm).hexdigest() == result['capture']['pcm_sha256']
    assert capture.service.get(result['id']) == result
    assert not list(capture.service.root.glob('.pending-*'))
    assert [state for state, _ in states] == ['recording', 'paused', 'recording', 'finalizing']


def test_stop_during_pause_saves_existing_parts_without_reopening_device(capture):
    stop = threading.Event()
    result, states = paused_capture(capture, stop=stop, on_pause=lambda pause, seconds: stop.set())
    assert len(capture.calls) == 1 and result['completion'] == 'stopped'
    assert 0 < result['audio']['frames'] < 24000
    assert len(result['capture']['parts']) == 1 and result['capture']['pause_count'] == 1
    assert capture.service.get(result['id']) == result


def test_cancel_during_pause_discards_parts_and_releases_device_admission(capture):
    cancel = threading.Event()
    with pytest.raises(AudioCancelled):
        paused_capture(capture, cancel=cancel, on_pause=lambda pause, seconds: cancel.set())
    assert len(capture.calls) == 1
    assert not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('*.json'))
    assert not list(capture.service.root.glob('.pending-*'))
    assert not ResourceBroker.for_app(capture.app.root).snapshot()['exclusive_devices']
    assert not recording._CAPTURE.locked()


def test_initial_pause_does_not_open_device_until_explicit_resume(capture):
    pause, observed = threading.Event(), []
    pause.set()
    def changed(state, seconds):
        if state == 'paused':
            observed.append((len(capture.calls), seconds))
            pause.clear()
    result = capture.service.capture(capture.device.id, options=capture.options,
        pause_requested=pause.is_set, state_changed=changed)
    assert observed == [(0, 0)] and len(capture.calls) == 1
    assert result['audio']['frames'] == 24000 and result['capture']['pause_count'] == 1


@pytest.mark.parametrize('action', ['stop', 'cancel'])
def test_initial_pause_can_finish_without_opening_any_capture(capture, action):
    pause, stop, cancel = threading.Event(), threading.Event(), threading.Event()
    pause.set()
    def changed(state, seconds):
        if state == 'paused':
            (stop if action == 'stop' else cancel).set()
    with pytest.raises(AudioCancelled):
        capture.service.capture(capture.device.id, options=capture.options,
            pause_requested=pause.is_set, state_changed=changed,
            stop_requested=stop.is_set, cancelled=cancel.is_set)
    assert not capture.calls and not list(capture.service.root.glob('*.wav'))
    assert not list(capture.service.root.glob('.pending-*'))


def test_changed_runtime_before_resume_refuses_another_device_open(capture, monkeypatch):
    real_digest, changed = recording._digest, threading.Event()
    def digest(path, cancelled=lambda: False):
        if str(path) == capture.service.runtime.ffmpeg and changed.is_set():
            return '0' * 64
        return real_digest(path, cancelled)
    monkeypatch.setattr(recording, '_digest', digest)
    def paused(pause, seconds):
        changed.set()
        pause.clear()
    with pytest.raises(ValueError, match='runtime changed before resume'):
        paused_capture(capture, on_pause=paused)
    assert len(capture.calls) == 1
    assert not list(capture.service.root.glob('*.wav'))


def test_paused_retake_roundtrips_complete_backup_with_original_identity(capture, tmp_path):
    original = capture.service.capture(capture.device.id, options=capture.options)
    stop = threading.Event()
    result, _ = paused_capture(capture, stop=stop,
        on_pause=lambda pause, seconds: stop.set(), retake_of=original['id'])
    expected = {item['id']: Path(item['path']).read_bytes() for item in (original, result)}
    assert result['retake']['previous_id'] == original['id']
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    manifest = capture.app.backup(backup)
    shutil.rmtree(capture.app.root)
    restore_backup(backup, restored, expected_manifest_sha256=manifest)
    service = RecordingService(restored)
    for identity, data in expected.items():
        item = service.get(identity)
        assert Path(item['path']).read_bytes() == data
    assert service.get(result['id'])['capture'] == result['capture']
    assert service._devices == {}


@pytest.mark.parametrize('field', ['part-digest', 'whole-digest', 'frames', 'pause-count'])
def test_paused_receipt_cannot_claim_another_sample_sequence(capture, field):
    result, _ = paused_capture(capture)
    path = Path(result['path']).with_suffix('.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    if field == 'part-digest':
        value['capture']['parts'][0]['pcm_sha256'] = '0' * 64
    elif field == 'whole-digest':
        value['capture']['pcm_sha256'] = '0' * 64
    elif field == 'frames':
        value['capture']['parts'][0]['frames'] += 1
    else:
        value['capture']['pause_count'] = True
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='recording|retained PCM'):
        capture.service.get(result['id'])
    from carr_library.recording_backup import validate_state_tree
    with pytest.raises(ValueError, match='recording|retained PCM'):
        validate_state_tree(capture.app.root)


def test_state_observer_failure_cannot_destroy_valid_recorded_pcm(capture):
    def failed(state, seconds):
        raise RuntimeError('A detached presentation observer')
    result = capture.service.capture(capture.device.id, options=capture.options,
        pause_requested=lambda: False, state_changed=failed)
    assert result['audio']['frames'] == 24000 and capture.service.get(result['id']) == result


def test_segment_limit_saves_verified_pcm_instead_of_losing_the_recording(capture, monkeypatch):
    monkeypatch.setattr(recording, 'MAX_CAPTURE_PARTS', 2)
    pause = threading.Event()
    last = [0.0]
    def progress(seconds):
        if seconds - last[0] >= .15:
            pause.set()
    def changed(state, seconds):
        if state == 'paused':
            last[0] = seconds
            pause.clear()
    result = capture.service.capture(capture.device.id, options=replace(capture.options, max_seconds=10),
        pause_requested=pause.is_set, state_changed=changed, progress=progress)
    assert len(capture.calls) == 2 and result['completion'] == 'segment-limit'
    assert len(result['capture']['parts']) == 2 and result['capture']['pause_count'] == 2
    assert capture.service.get(result['id']) == result


def test_pause_timeout_saves_captured_pcm_without_reopening_device(capture, monkeypatch):
    monkeypatch.setattr(recording, 'MAX_PAUSED_SECONDS', .01)
    result, _ = paused_capture(capture, on_pause=lambda pause, seconds: None)
    assert len(capture.calls) == 1 and result['completion'] == 'pause-timeout'
    assert capture.service.get(result['id']) == result


def test_pause_callback_validation_precedes_device_open(capture):
    for arguments in ({'pause_requested': True}, {'state_changed': 'paused'}):
        with pytest.raises(ValueError, match='callback|callable'):
            capture.service.capture(capture.device.id, options=capture.options, **arguments)
    assert not capture.calls


def test_empty_pcm_is_only_admitted_for_explicit_unfinished_parts(tmp_path):
    from carr_library.audio.ffmpeg import wav_info
    path = tmp_path / 'empty.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
    with pytest.raises(ValueError, match='empty'):
        wav_info(path)
    info = wav_info(path, allow_empty=True)
    assert info.frames == 0 and info.data_size == 0
    with pytest.raises(ValueError, match='explicitly'):
        wav_info(path, allow_empty=1)


def test_cancel_after_finalizing_signal_keeps_prior_completed_original(capture):
    original = capture.service.capture(capture.device.id, options=capture.options)
    cancel = threading.Event()
    def changed(state, seconds):
        if state == 'finalizing':
            cancel.set()
    with pytest.raises(AudioCancelled):
        capture.service.capture(capture.device.id, options=capture.options,
            pause_requested=lambda: False, state_changed=changed, cancelled=cancel.is_set)
    assert capture.service.get(original['id']) == original
    assert len(capture.service.list()) == 1
    assert not list(capture.service.root.glob('.pending-*'))


def test_late_cancel_after_paused_publication_keeps_the_committed_original(capture, monkeypatch):
    cancel, original_publish = threading.Event(), recording.publish_noreplace
    def publish(source, destination):
        result = original_publish(source, destination)
        cancel.set()
        return result
    monkeypatch.setattr(recording, 'publish_noreplace', publish)
    result = capture.service.capture(capture.device.id, options=capture.options,
        pause_requested=lambda: False, cancelled=cancel.is_set)
    assert cancel.is_set() and capture.service.get(result['id']) == result


def test_pending_pause_cleanup_retains_reservation_without_claiming_paused(capture, monkeypatch):
    from carr_library.audio.cleanup import AudioCleanupPending
    class Owner:
        def __init__(self):
            self.quiescent = False
            self.callbacks = []
        def add_quiescent_callback(self, callback):
            self.callbacks.append(callback)
        def confirm(self):
            self.quiescent = True
            for callback in self.callbacks:
                callback()
    owner, states = Owner(), []
    def unfinished(command, **kwargs):
        Path(command[-1]).write_bytes(b'Owned unfinished capture part')
        raise AudioCleanupPending('Pending capture shutdown', cleanup_owner=owner)
    monkeypatch.setattr(recording, 'run_process', unfinished)
    try:
        with pytest.raises(AudioCleanupPending):
            capture.service.capture(capture.device.id, options=capture.options,
                pause_requested=lambda: False, state_changed=lambda state, seconds: states.append(state))
        assert states == ['recording'] and recording._CAPTURE.locked()
        assert list(capture.service.root.glob('.pending-*'))
        assert ResourceBroker.for_app(capture.app.root).snapshot()['exclusive_devices']
        assert not list(capture.service.root.glob('*.wav'))
    finally:
        owner.confirm()
    assert not recording._CAPTURE.locked()
    assert not list(capture.service.root.glob('.pending-*'))
