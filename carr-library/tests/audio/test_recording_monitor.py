"""Real FFmpeg live PCM with an explicit synthetic output-device boundary.

These checks prove pipe/queue/receipt behavior. They do not qualify a physical
microphone, an installed output driver, acoustic latency, headphones or NVDA.
"""
from contextlib import ExitStack
import ctypes
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import playback, recording
from carr_library.audio.recording import (
    AudioCancelled, AudioUnavailable, RecordingOptions, RecordingService, parse_devices)
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations.broker import ResourceBroker
from carr_library.progress import ProgressHub


INVENTORY = br'''[dshow @ 123] "Synthetic microphone" (audio)
[dshow @ 123]   Alternative name "@device_cm_{123}\wave_{456}"
'''


class OutputBoundary:
    """A deterministic consumer of actual incoming PCM, not an OS driver."""
    def __init__(self, info, device_id, *, on_submit=None):
        self.info = info
        self.device = playback.OutputDevice(device_id, 'Synthetic monitor output', 2, True)
        self.active, self.samples, self.threads = [], [], set()
        self.paused, self.closed, self.maximum_bytes, self.frames = True, False, 0, 0
        self.on_submit = on_submit

    def submit(self, data):
        self.threads.add(threading.get_ident())
        if self.on_submit is not None:
            self.on_submit(data)
        token = object()
        self.samples.append(data)
        self.active.append((token, len(data)))
        self.maximum_bytes = max(self.maximum_bytes, sum(size for _, size in self.active))
        return token

    def completed(self, token):
        return not self.paused

    def release(self, token):
        size = next(size for candidate, size in self.active if candidate is token)
        self.frames += size // (self.info.channels * 2)
        self.active = [(candidate, size) for candidate, size in self.active if candidate is not token]

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def reset(self):
        self.active.clear()

    def position(self):
        return self.frames

    def close(self):
        self.threads.add(threading.get_ident())
        self.reset()
        self.closed = True


@pytest.fixture
def monitored(tmp_path, monkeypatch):
    binary = resolve_ffmpeg()
    if binary is None:
        pytest.fail('Actual FFmpeg is required for live PCM tests.')
    app, hub = LibraryApp(tmp_path / 'monitor-library'), ProgressHub()
    service = RecordingService(app.root, ffmpeg=binary, progress_hub=hub)
    service._runtime()
    device, = parse_devices(INVENTORY)
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(binary)
    service._outputs = {3: playback.OutputDevice(3, 'Synthetic monitor output', 2, True)}
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    boundaries, players = [], []

    def factory(info, identity):
        boundary = OutputBoundary(info, identity)
        boundaries.append(boundary)
        return boundary

    def player():
        value = playback.PCMPlayer(buffer_ms=20, buffer_count=2,
                                   _backend_factory=factory, progress_hub=hub)
        players.append(value)
        return value

    monkeypatch.setattr(service, '_new_monitor', player)
    return SimpleNamespace(app=app, hub=hub, service=service, device=device,
        options=RecordingOptions(max_seconds=1, sample_rate=24000),
        boundaries=boundaries, players=players)


@pytest.mark.parametrize('channels', [1, 2])
def test_actual_live_pipe_precedes_publication_and_equals_saved_pcm(monitored, monkeypatch, channels):
    seen = []
    submit = OutputBoundary.submit

    def observed(boundary, data):
        if not seen:
            assert not list(monitored.service.root.glob('*.wav'))
            snapshot = ResourceBroker.for_app(monitored.app.root).snapshot()
            assert snapshot['exclusive_devices'] == sorted([
                'microphone:' + monitored.device.id, 'winmm-output:3'])
            assert monitored.hub.media_active
            seen.append(True)
        return submit(boundary, data)

    monkeypatch.setattr(OutputBoundary, 'submit', observed)
    options = replace(monitored.options, channels=channels)
    result = monitored.service.capture(monitored.device.id, options=options,
                                       monitor=True, monitor_device_id=3)
    with wave.open(result['path'], 'rb') as source:
        pcm = source.readframes(source.getnframes())
    boundary, = monitored.boundaries
    assert seen and any(pcm)
    assert b''.join(boundary.samples) == pcm
    assert boundary.maximum_bytes <= 2 * (24000 * 20 // 1000) * channels * 2
    assert boundary.closed and not boundary.active and len(boundary.threads) == 1
    assert result['monitor']['pcm_matches_original'] is True
    assert result['monitor']['stream_frames'] == 24000 and result['monitor']['error'] is None
    receipt = {key: value for key, value in result.items() if key != 'monitor'}
    assert monitored.service.get(result['id']) == receipt
    assert 'monitor' not in json.loads(Path(result['path']).with_suffix('.json').read_text(encoding='utf-8'))
    assert not monitored.hub.media_active
    assert ResourceBroker.for_app(monitored.app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('identity', [None, True, 9])
def test_monitor_requires_an_explicit_current_output_before_any_capture(monitored, monkeypatch, identity):
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Microphone was opened'))
    with pytest.raises(ValueError, match='explicitly select'):
        monitored.service.capture(monitored.device.id, monitor=True, monitor_device_id=identity)
    assert not monitored.players and not monitored.hub.media_active


def test_unavailable_monitor_fails_before_the_microphone_opens(monitored, monkeypatch):
    def unavailable(info, identity):
        raise playback.PlaybackUnavailable('Synthetic output is unavailable')
    monkeypatch.setattr(monitored.service, '_new_monitor', lambda: playback.PCMPlayer(
        _backend_factory=unavailable, progress_hub=monitored.hub))
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Microphone was opened'))
    with pytest.raises(AudioUnavailable, match='output is unavailable'):
        monitored.service.capture(monitored.device.id, monitor=True, monitor_device_id=3)
    assert not list(monitored.service.root.glob('*.wav'))
    assert not monitored.hub.media_active
    assert ResourceBroker.for_app(monitored.app.root).snapshot()['leases'] == []


def test_monitor_output_failure_gracefully_retains_the_actual_recording(monitored, monkeypatch):
    def failed(boundary, data):
        raise playback.PlaybackError('Synthetic output failed while consuming real PCM')
    monkeypatch.setattr(OutputBoundary, 'submit', failed)
    result = monitored.service.capture(monitored.device.id,
        options=replace(monitored.options, max_seconds=10), monitor=True, monitor_device_id=3)
    assert result['completion'] == 'stopped'
    assert 0 < result['audio']['duration_seconds'] < 3
    assert 'Synthetic output failed' in result['monitor']['error']
    assert result['monitor']['pcm_matches_original'] is True
    assert monitored.service.get(result['id'])['audio'] == result['audio']
    assert monitored.boundaries[0].closed and not monitored.hub.media_active
    assert ResourceBroker.for_app(monitored.app.root).snapshot()['leases'] == []


def test_cancelled_actual_monitor_discards_incomplete_capture_and_releases_both_devices(monitored, monkeypatch):
    cancelled = threading.Event()
    submit = OutputBoundary.submit

    def observed(boundary, data):
        result = submit(boundary, data)
        cancelled.set()
        return result

    monkeypatch.setattr(OutputBoundary, 'submit', observed)
    with pytest.raises(AudioCancelled):
        monitored.service.capture(monitored.device.id,
            options=replace(monitored.options, max_seconds=10), monitor=True,
            monitor_device_id=3, cancelled=cancelled.is_set)
    assert not list(monitored.service.root.glob('*.wav'))
    assert not list(monitored.service.root.glob('*.json'))
    assert monitored.boundaries[0].closed and not monitored.hub.media_active
    assert ResourceBroker.for_app(monitored.app.root).snapshot()['leases'] == []


def test_actual_monitor_stream_must_match_finalized_wav_before_publication(monitored, monkeypatch):
    original = recording.run_process

    def wrong_binding(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, pcm_sha256='0' * 64)

    monkeypatch.setattr(recording, 'run_process', wrong_binding)
    with pytest.raises(ValueError, match='live PCM stream differs'):
        monitored.service.capture(monitored.device.id, options=monitored.options,
                                   monitor=True, monitor_device_id=3)
    assert not list(monitored.service.root.glob('*.wav'))
    assert not list(monitored.service.root.glob('*.json'))
    assert monitored.boundaries[0].closed and not monitored.hub.media_active


def test_monitor_cleanup_retains_saved_recording_and_shared_leases_until_exact_output_is_closed(monitored, monkeypatch):
    release = threading.Event()
    retained = []
    monkeypatch.setattr(playback, '_UNRELEASED_DEVICES', retained)

    class RetainedOutput(OutputBoundary, playback._WinMMBackend):
        def __init__(self, info, identity):
            OutputBoundary.__init__(self, info, identity)
            self.handle = ctypes.c_void_p(123)
            self._media_activities = ExitStack()

        def close(self):
            if not release.is_set():
                if self not in retained:
                    retained.append(self)
                raise playback.PlaybackError('Synthetic native output close remains pending')
            OutputBoundary.close(self)
            self.handle = ctypes.c_void_p()
            self._media_activities.close()

    boundaries = []

    def factory(info, identity):
        value = RetainedOutput(info, identity)
        boundaries.append(value)
        return value

    monkeypatch.setattr(monitored.service, '_new_monitor', lambda: playback.PCMPlayer(
        buffer_ms=20, buffer_count=2, _backend_factory=factory, progress_hub=monitored.hub))
    owner = None
    try:
        with pytest.raises(recording.AudioCleanupPending) as failure:
            monitored.service.capture(monitored.device.id, options=monitored.options,
                                       monitor=True, monitor_device_id=3)
        owner = failure.value.cleanup_owner
        saved = failure.value.saved_recording
        assert monitored.service.get(saved['id'])['audio'] == saved['audio']
        assert Path(saved['path']).is_file()
        assert not owner.quiescent and monitored.hub.media_active
        snapshot = ResourceBroker.for_app(monitored.app.root).snapshot()
        assert snapshot['exclusive_devices'] == sorted([
            'microphone:' + monitored.device.id, 'winmm-output:3'])
        assert len(snapshot['leases']) == 1
        done = threading.Event()
        owner.add_quiescent_callback(done.set)
        release.set()
        assert done.wait(10)
        assert not monitored.hub.media_active and not boundaries[0].handle.value
        assert ResourceBroker.for_app(monitored.app.root).snapshot()['leases'] == []
    finally:
        release.set()
        if owner is not None:
            owner.close()
            owner.quiescent_event.wait(10)
        for boundary in boundaries:
            boundary.close()


def test_monitor_output_inventory_never_opens_or_selects_a_device(monitored, monkeypatch):
    installed = (playback.OutputDevice(7, 'Explicit output', 2, True),)
    monkeypatch.setattr(playback, 'output_devices', lambda: installed)
    monkeypatch.setattr(monitored.service, '_new_monitor', lambda: pytest.fail('Output opened during inspection'))
    monkeypatch.setattr(recording, 'run_process', lambda *a, **k: pytest.fail('Microphone opened during inspection'))
    assert monitored.service.monitor_devices() == installed
    assert monitored.service._outputs == {7: installed[0]}
    with pytest.raises(AudioCancelled):
        monitored.service.monitor_devices(cancelled=lambda: True)
    assert monitored.service._outputs == {}


def test_actual_monitor_stream_stays_bounded_across_device_pause_and_resume(monitored):
    pause, requested, snapshots, errors = threading.Event(), [False], [], []
    def progress(seconds):
        if not requested[0] and seconds >= .15:
            requested[0] = True
            pause.set()
    def changed(state, seconds):
        if state != 'paused':
            return
        try:
            import time
            time.sleep(.06)  # Drain only the existing two bounded output buffers.
            boundary, = monitored.boundaries
            before = b''.join(boundary.samples)
            time.sleep(.06)
            snapshots.append((before, b''.join(boundary.samples)))
        except Exception as exc:
            errors.append(exc)
        finally:
            pause.clear()
    result = monitored.service.capture(monitored.device.id, options=monitored.options,
        monitor=True, monitor_device_id=3, pause_requested=pause.is_set,
        state_changed=changed, progress=progress)
    assert not errors and len(snapshots) == 1
    assert snapshots[0][0] == snapshots[0][1] and snapshots[0][0]
    boundary, = monitored.boundaries
    with wave.open(result['path'], 'rb') as source:
        pcm = source.readframes(source.getnframes())
    assert b''.join(boundary.samples) == pcm
    assert len(result['capture']['parts']) == 2
    assert result['monitor']['pcm_matches_original'] and result['monitor']['error'] is None
    assert result['monitor']['stream_frames'] == 24000
    assert boundary.maximum_bytes <= 2 * (24000 * 20 // 1000) * 2
    assert boundary.closed and not boundary.active
