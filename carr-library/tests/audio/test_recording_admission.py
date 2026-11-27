"""Actual child cleanup failures retain capture admission and shared resources.

The injected fault affects native shutdown confirmation, never capture bytes.
Portable audio-notice observers do not claim physical output/microphone proof.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import capture_process, recording
from carr_library.audio.capture_types import AudioBudget, AudioCleanupPending
from carr_library.audio.player import WavePlayer
from carr_library.audio.recording import RecordingDevice, RecordingOptions, RecordingService
from carr_library.audio.runtime import find_ffmpeg
from carr_library.audio.windows_job import JobProcess
from carr_library import progress


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Owned cleanup did not reach the required state.'
        time.sleep(.02)


def pcm(path):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\x01\x00' * 800)
    return path


def test_real_capture_cancel_failure_keeps_device_resources_and_all_app_audio_closed(tmp_path, monkeypatch):
    ffmpeg = find_ffmpeg()
    assert ffmpeg is not None, 'Actual FFmpeg is required; synthetic capture must not skip.'
    app = LibraryApp(tmp_path / 'library')
    project = tmp_path / 'audio-project'
    project.mkdir()
    service = RecordingService(app.root, project, ffmpeg=ffmpeg)
    service._runtime()
    alternative = '@device_test_retained_capture'
    device = RecordingDevice(sha256(alternative.encode()).hexdigest(), 'Synthetic capture fixture', alternative)
    service._devices, service._engine_hash = {device.id: device}, recording._digest(service.runtime.ffmpeg)
    monkeypatch.setattr(service, '_input_arguments', lambda unused: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    options = RecordingOptions(max_seconds=5, sample_rate=24000)

    class Backend:
        def __init__(self):
            self.played, self.stopped = threading.Event(), threading.Event()
            self.play_count = 0
        def prepare(self, raw):
            assert raw.startswith(b'RIFF')
            return raw
        def play(self, raw):
            self.play_count += 1
            self.played.set()
        def stop(self):
            self.stopped.set()
        def close(self):
            self.stop()

    backend = Backend()
    dispatcher = progress.ToneDispatcher(backend_factory=lambda: backend)
    monkeypatch.setattr(progress, '_dispatcher', dispatcher)
    # Exercise the real Windows-facing tone admission on both source platforms;
    # backend playback is a policy observer, never a claim about a physical device.
    monkeypatch.setattr(progress, 'os', SimpleNamespace(name='nt'))
    assert progress.request_progress_tone(((440, 20),), 20)
    assert backend.played.wait(2)
    cancelled, permit = threading.Event(), threading.Event()
    attempted, notices, owners = [], [], []
    target, name = (JobProcess, 'close') if os.name == 'nt' else (capture_process, '_terminate_owned_linux')
    actual = getattr(target, name)

    def fail_native_stop_until_released(child):
        attempted.append(child)
        if not permit.is_set():
            raise OSError('Injected stop confirmation failure with an actual capture child.')
        return actual(child)

    monkeypatch.setattr(target, name, fail_native_stop_until_released)
    real_run = recording.run_process

    def observe_actual_launch(args, **kwargs):
        assert backend.stopped.is_set(), 'The existing tone was not stopped before capture admission.'
        return real_run(args, **kwargs)

    monkeypatch.setattr(recording, 'run_process', observe_actual_launch)

    def report(seconds):
        notices.append((progress.recording_audio_notice(),
            progress.request_progress_tone(((880, 20),), 20),
            progress.request_progress_voice(object(), 'Recording progress', 20)))
        if seconds >= .1:
            cancelled.set()

    preview = WavePlayer()
    original = pcm(tmp_path / 'existing-input.wav')
    original_bytes = original.read_bytes()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(service.capture, device.id, options=options,
                                     cancelled=cancelled.is_set, progress=report)
            with pytest.raises(AudioCleanupPending) as error:
                future.result(timeout=15)
        owner = error.value.cleanup_owner
        owners.append(owner)
        assert attempted and not owner.quiescent
        assert notices and all('recording is active' in message and not tone and not speech
                               for message, tone, speech in notices)
        assert backend.play_count == 1
        assert 'cleanup is still pending' in progress.recording_audio_notice()
        lease, = service.broker.snapshot()['leases']
        assert lease['request']['cpu_slots'] == 1
        assert lease['request']['devices'] == ['microphone:' + device.id]
        stages = list(project.glob('.pending-recording-*'))
        assert len(stages) == 1 and (stages[0] / 'capture.wav').exists()
        assert not list(service.assets.glob('*.wav')) and not list(service.root.glob('*.json'))
        with pytest.raises(RuntimeError, match='cleanup'):
            service.capture(device.id, options=options)
        launched = []
        monkeypatch.setattr(preview, '_start_owned', lambda *args, **kwargs: launched.append(args))
        with pytest.raises(RuntimeError, match='cleanup is still pending'):
            preview.play(original)
        assert launched == []
        assert not progress.request_progress_tone(((880, 20),), 20)
        assert not progress.request_progress_voice(object(), 'Must remain silent', 20)
        if sys.platform == 'linux':
            # Do not poll/wait the leader here: WNOWAIT leaves its group identity pinned.
            assert attempted[0].returncode is None
            os.waitid(os.P_PID, attempted[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        permit.set()
        assert owner.quiescent_event.wait(8)
        wait_until(lambda: not service.broker.snapshot()['leases'])
        wait_until(lambda: not progress.recording_audio_notice())
        assert not stages[0].exists()
        assert attempted[0].returncode is not None
        assert original.read_bytes() == original_bytes
        assert not list(service.assets.glob('*.wav')) and not list(service.root.glob('*.json'))
    finally:
        permit.set()
        for owner in owners:
            owner.close()
            assert owner.quiescent_event.wait(8)
        preview.close()
        dispatcher.close()
        dispatcher.thread.join(3)


def test_failed_native_group_observation_preserves_exact_owner_until_recovery(tmp_path, monkeypatch):
    permit = threading.Event()
    children, owners = [], []
    if sys.platform == 'linux':
        from carr_library.operations import process as shared
        actual_wait = shared._wait_linux_group
        actual_popen = capture_process.subprocess.Popen
        def launch(*args, **kwargs):
            child = actual_popen(*args, **kwargs)
            children.append(child)
            return child
        def observe(group, *, timeout):
            if not permit.is_set():
                raise shared.ProcessError('Injected unknown group observation after actual termination.')
            return actual_wait(group, timeout=timeout)
        monkeypatch.setattr(capture_process.subprocess, 'Popen', launch)
        monkeypatch.setattr(shared, '_wait_linux_group', observe)
    else:
        assert os.name == 'nt', 'This owned capture lifecycle is qualified on Windows and Linux.'
        actual_close = JobProcess.close
        def close(child):
            if child not in children:
                children.append(child)
            if not permit.is_set():
                raise OSError('Injected unconfirmed native Job Object shutdown.')
            return actual_close(child)
        monkeypatch.setattr(JobProcess, 'close', close)
    try:
        with pytest.raises(AudioCleanupPending) as error:
            capture_process.run_process([str(Path(sys.executable).resolve()), '-I', '-c', "print('complete')"],
                owned_tree=True, budget=AudioBudget(timeout=10, reserve_disk_bytes=0))
        owner = error.value.cleanup_owner
        owners.append(owner)
        assert len(children) == 1 and not owner.quiescent
        child = children[0]
        if sys.platform == 'linux':
            assert child.returncode is None
            observed = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            assert observed is not None and observed.si_pid == child.pid
        else:
            assert child._job is not None and child._process.hProcess is not None
        permit.set()
        assert owner.quiescent_event.wait(8)
        wait_until(lambda: child.stdout.closed and child.stderr.closed)
        assert child.returncode == 0
        assert owner.close()
    finally:
        permit.set()
        for owner in owners:
            owner.close()
            assert owner.quiescent_event.wait(8)


def test_active_capture_and_pending_cleanup_have_distinct_playback_refusals(tmp_path):
    original = pcm(tmp_path / 'selection.wav')
    owner, player = object(), WavePlayer()
    try:
        progress.set_recording_active(owner, True)
        with pytest.raises(RuntimeError, match='recording is active'):
            player.play(original)
        progress.set_recording_active(owner, True, cleanup_pending=True)
        with pytest.raises(RuntimeError, match='cleanup is still pending'):
            player.play(original)
    finally:
        progress.set_recording_active(owner, False)
        player.close()
