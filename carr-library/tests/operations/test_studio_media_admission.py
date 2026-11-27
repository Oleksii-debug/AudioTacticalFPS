"""Real native ownership/admission and canonical voice API boundaries.

Purpose-written PCM is used only for the voice-provider interface regression.
FFmpeg, FFprobe and cleanup children are actual selected local processes.
"""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import sys
import threading
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio import process
from carr_library.audio.models import AudioCleanupPending
from carr_library.audio.windows_job import JobProcess
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceTimeout
from carr_library.speech import Voice, VoiceRegistry
from carr_library.speech.runtime import _wave_info
from carr_library.studio import render
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def pcm(path, *, frames=24000, rate=24000):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b'\x10\x00' * frames)
    return path


def service_at(tmp_path, *, voices=None, inputs=()):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.fail('Installed FFmpeg and FFprobe are required for media acceptance.')
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    made = []
    def factory(root, **options):
        runtime = AudioRuntime(root, budget=AudioBudget(threads=1), **options)
        made.append(runtime)
        return runtime
    service = ProductionService(tmp_path / 'state/studio', broker=broker,
        voices=voices if voices is not None else VoiceRegistry([]),
        runtime_factory=factory, input_roots=inputs)
    outputs = tmp_path / 'output'
    outputs.mkdir()
    return service, broker, outputs, made


@pytest.mark.parametrize('change_voice', [False, True])
def test_canonical_registry_api_renders_pcm_and_rejects_changed_voice_identity(tmp_path, change_voice):
    executable = tmp_path / 'purpose-written-provider-identity'
    executable.write_bytes(b'fixture-provider-version-one')
    spoken = []
    class Provider:
        name = 'Purpose-written PCM API fixture'
        def __init__(self):
            self.executable = executable
        def voices(self):
            return (Voice('fixture:pcm', 'PCM interface fixture', 'fixture', 'en'),)
        def render(self, text, voice, output, settings, cancelled):
            assert voice.id == 'fixture:pcm' and not cancelled()
            spoken.append(text)
            pcm(output, frames=6000)
            if change_voice:
                executable.write_bytes(b'fixture-provider-version-two')

    registry = VoiceRegistry([Provider()])
    service, broker, outputs, _ = service_at(tmp_path, voices=registry)
    project = service.create('Narrator: Exact authored wording.', voice_ids=('fixture:pcm',))
    identity = service.submit(project, outputs, stems=False)
    if change_voice:
        with pytest.raises(ValueError, match='voice changed during'):
            service.run(identity)
        state = service.get(identity)
        assert state['status'] == 'failed' and not state['nodes']
        assert not Path(state['destination']).exists()
    else:
        result = service.run(identity)
        assert result['status'] == 'complete'
        assert _wave_info(Path(result['destination']) / 'master.wav')[2] > 0
        assert any(value['kind'] == 'speech' for value in result['nodes'].values())
    assert spoken == ['Exact authored wording.']
    assert broker.snapshot()['leases'] == []


def test_pcm_inspection_preserves_old_call_and_enforces_optional_budget_and_cancel(tmp_path):
    source = pcm(tmp_path / 'actual.wav', frames=100000)
    expected = (24000, 1, 100000, 2)
    assert _wave_info(source) == expected
    assert _wave_info(source, max_output_bytes=source.stat().st_size) == expected
    with pytest.raises(ValueError, match='audio-output budget'):
        _wave_info(source, max_output_bytes=source.stat().st_size - 1)
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 2
    with pytest.raises(InterruptedError):
        _wave_info(source, cancelled=cancelled, max_output_bytes=source.stat().st_size)
    assert len(calls) == 2


def test_clip_probe_is_admitted_and_measured_end_sizes_render_reservation(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'
    selected.mkdir()
    source = pcm(selected / 'retained.wav')
    service, broker, outputs, _ = service_at(tmp_path, inputs=(selected,))
    project = Project('measured-clips', 'Measured resource plan', (),
        (Scene('scene-1', 'Clip only', (), cues=(Cue('cue-1', str(source),
             start=12, trim_start=.25, duration=.5, role='microphone'),)),),
        original_script='Explicit media without synthetic narration.')
    observed = []
    inspect = AudioRuntime.inspect
    def inspect_admitted(runtime, path, **options):
        if Path(path) == source:
            snapshot = broker.snapshot()
            assert snapshot['usage']['cpu_slots'] == 1
            assert len(snapshot['leases']) == 1
            observed.append(snapshot['leases'][0]['operation_id'])
        return inspect(runtime, path, **options)
    monkeypatch.setattr(AudioRuntime, 'inspect', inspect_admitted)
    identity = service.submit(project, outputs, stems=True)
    result = service.run(identity)
    assert observed and observed[0] == 'studio:' + identity + ':input-inspection'
    request = result['resource_plan']['request']
    expected_pcm = round(12.5 * project.sample_rate) * 2 * 2
    assert request['disk_bytes'] >= expected_pcm * 8
    assert request['additional_disks'][0][1] >= expected_pcm * 3
    assert abs(result['result']['duration'] - 12.5) <= 1 / project.sample_rate
    assert (Path(result['destination']) / 'stems' / 'clips' / 'microphone.wav').is_file()
    assert broker.snapshot()['leases'] == []


@pytest.mark.parametrize('boundary', ['input-inspection', 'capabilities', 'render'])
def test_real_pending_child_retains_studio_lease_and_job_lock_despite_failed_diagnostics(
        tmp_path, monkeypatch, boundary):
    from filelock import FileLock, Timeout

    selected = tmp_path / 'selected'
    selected.mkdir()
    source = pcm(selected / 'cue.wav')
    service, broker, outputs, _ = service_at(tmp_path, inputs=(selected,))
    project = (Project('probe-owner', 'Pending probe', (),
        (Scene('scene-1', 'Input', (), cues=(Cue('cue-1', str(source)),)),),
        original_script='Retain native ownership.') if boundary == 'input-inspection'
        else service.create('[Pause: 0.1]', title='Pending native owner'))
    identity = service.submit(project, outputs, stems=False)
    ready = tmp_path / 'child-ready'
    allowed = threading.Event()
    armed, attempts, forbidden = [], [], []
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_terminate')
    real_close = getattr(target, name)
    def deny_cleanup(child):
        if armed and not allowed.is_set():
            attempts.append(child)
            raise OSError('injected native termination failure')
        return real_close(child)
    monkeypatch.setattr(target, name, deny_cleanup)
    save, read = service._save, render.read_json
    def diagnostic_save(state):
        if armed:
            forbidden.append('save')
            raise OSError('injected diagnostic save failure')
        return save(state)
    def diagnostic_read(path):
        if armed and Path(path).name == 'control.json':
            forbidden.append('read')
            raise OSError('injected diagnostic read failure')
        return read(path)
    monkeypatch.setattr(service, '_save', diagnostic_save)
    monkeypatch.setattr(render, 'read_json', diagnostic_read)
    def pending_child(*args, **kwargs):
        armed.append(True)
        atomic_json(service.jobs_root / identity / 'control.json', {'action': 'pause'})
        return process.run_process([str(Path(sys.executable).resolve()), '-I', '-c',
            'import pathlib,time;pathlib.Path(' + repr(str(ready)) + ').write_text("ready");time.sleep(30)'],
            budget=AudioBudget(timeout=20, reserve_disk_bytes=0),
            owned_tree=True, cancelled=ready.exists)
    if boundary == 'input-inspection':
        inspect = AudioRuntime.inspect
        def inspect_or_pending(runtime, path, **options):
            return pending_child() if Path(path) == source else inspect(runtime, path, **options)
        monkeypatch.setattr(AudioRuntime, 'inspect', inspect_or_pending)
    else:
        monkeypatch.setattr(AudioRuntime, 'capabilities' if boundary == 'capabilities' else 'mix',
                            pending_child)
    owner = None
    competitor = FileLock(service.jobs_root / identity / '.render.lock', timeout=0)
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            service.run(identity)
        owner = failure.value.cleanup_owner
        released = threading.Event()
        owner.add_quiescent_callback(released.set)
        assert ready.exists() and attempts and attempts[0].poll() is None
        assert not owner.quiescent and not forbidden
        assert len(broker.snapshot()['leases']) == 1
        with pytest.raises(Timeout):
            competitor.acquire()
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='blocked-competitor', timeout=0):
                pytest.fail('Native work still owns shared admission.')
        assert not Path(service.get(identity)['destination']).exists()
    finally:
        allowed.set()
        if owner is not None:
            owner.close()
            assert released.wait(10) and owner.quiescent
    assert not forbidden
    assert broker.snapshot()['leases'] == []
    with competitor:
        pass
