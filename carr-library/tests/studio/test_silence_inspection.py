"""Actual completed Studio PCM inspection without new output or stored authority."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioError, AudioRuntime
from carr_library.audio import silence
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, _pending_owners
from carr_library.audio.process import owned_process
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations._locks import NativeLock
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Cue, Line, Project, Role, Scene
from carr_library.studio.render import ProductionService, _operation_sha256


pytestmark = pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
                                reason='Actual local FFmpeg and ffprobe are required.')
RATE = 16000


@pytest.fixture
def completed(tmp_path):
    inputs, output = tmp_path / 'input', tmp_path / 'output'
    inputs.mkdir()
    output.mkdir()
    source = inputs / 'purpose-written-pcm.wav'
    values = [8000] * 4000 + [0] * 8000 + [-8000] * 4000
    with wave.open(str(source), 'wb') as writer:
        writer.setparams((1, 2, RATE, 0, 'NONE', 'PCM'))
        writer.writeframes(b''.join(struct.pack('<h', value) for value in values))
    runtimes = []
    budget = AudioBudget(timeout=30, threads=1, max_output_bytes=16 * 1024**2, reserve_disk_bytes=0)
    def factory(root, *, input_roots):
        runtime = AudioRuntime(root, input_roots=input_roots,
            budget=budget)
        runtimes.append(runtime)
        return runtime
    voices = VoiceRegistry([])
    service = ProductionService(tmp_path / 'library' / 'state' / 'studio',
        voices=voices, runtime_factory=factory, input_roots=(inputs,))
    project = Project('silence-review', 'Explicit measured pause', (),
        (Scene('scene', 'Original PCM', (), cues=(
            Cue('original', str(source), gain_db=0, duration=1, role='microphone'),)),),
        sample_rate=RATE, schema=2)
    job = service.submit(project, output, operation_id='silence-inspection')
    state = service.run(job)
    assert state['status'] == 'complete'
    job_file = service.jobs_root / job / 'job.json'
    master = Path(state['destination']) / 'master.wav'
    value = SimpleNamespace(service=service, project=project, state=state,
        source=source, master=master, job_file=job_file, runtimes=runtimes,
        factory=factory, job=job, root=tmp_path, budget=budget)
    try:
        yield value
    finally:
        service.close()
        voices.close()


def inspect(value, **options):
    return value.service.inspect_silence(value.job,
        threshold_dbfs=-90, minimum_seconds=.25, budget=value.budget, **options)


def retained_files(value):
    roots = (value.service.cache, Path(value.state['destination']))
    return {str(path): (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for root in roots for path in root.rglob('*') if path.is_file()}


def locked(value):
    lock = NativeLock(value.job_file.parent / '.render.lock')
    acquired = lock.acquire()
    if acquired:
        lock.release()
    return not acquired


def test_actual_completed_master_is_measured_under_one_lease_without_state_or_cache_changes(completed, monkeypatch):
    value = completed
    before, files, original = value.job_file.read_bytes(), retained_files(value), value.source.read_bytes()
    verified, admission, bound = [], [], []
    original_verify = value.service._verify_artifacts

    def verify(state, **options):
        leases = value.service.broker.snapshot()['leases']
        assert len(leases) == 1 and locked(value)
        verified.append(state['id'])
        return original_verify(state, **options)

    def bind_runtime(*, cancelled):
        assert cancelled() is False and locked(value)
        assert len(value.service.broker.snapshot()['leases']) == 1
        bound.append(True)

    def factory(root, **options):
        assert bound == [True]
        runtime = value.factory(root, **options)
        actual = runtime.inspect_silence
        def measured(path, **kwargs):
            leases = value.service.broker.snapshot()['leases']
            assert len(leases) == 1 and locked(value)
            request = leases[0]['request']
            assert request['cpu_slots'] == 1
            assert request['memory_bytes'] >= 512 * 1024**2
            assert Path(request['disk_path']).samefile(value.service.cache)
            assert request['disk_bytes'] == min(value.budget.max_output_bytes,
                                                value.master.stat().st_size * 4) + 65536
            assert request['disk_bytes'] < 1024**2  # This small file never reserves the 32-GiB default cap.
            admission.append(request)
            return actual(path, **kwargs)
        runtime.inspect_silence = measured
        return runtime

    monkeypatch.setattr(value.service, '_verify_artifacts', verify)
    monkeypatch.setattr(value.service, 'runtime_factory', factory)
    report = inspect(value, runtime_binding=bind_runtime)
    assert report['job_id'] == value.job and bound == [True]
    assert report['operation_sha256'] == value.state['operation_sha256']
    assert report['project_fingerprint'] == value.project.fingerprint
    assert [(row['start_frame'], row['end_frame']) for row in report['intervals']] == [(4000, 12000)]
    assert report['audio'] == {'sample_rate': RATE, 'channels': 2, 'frames': RATE,
                                'source_codec': 'pcm_s16le'}
    assert report['source']['sha256'] == sha256(value.master.read_bytes()).hexdigest()
    assert verified == [value.job, value.job] and len(admission) == 1
    assert value.job_file.read_bytes() == before and retained_files(value) == files
    assert value.source.read_bytes() == original
    assert not locked(value) and value.service.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('state_change', ['restored', 'pending', 'bad-operation'])
def test_inert_or_invalid_job_refuses_before_any_external_or_runtime_access(completed, monkeypatch, state_change):
    value = completed
    state = read_json(value.job_file)
    if state_change == 'restored':
        state['restore_requires_new_output'] = True
    elif state_change == 'pending':
        state['status'] = 'pending'
    else:
        state['operation_sha256'] = '0' * 64
    atomic_json(value.job_file, state, replace=True)
    before = value.job_file.read_bytes()
    monkeypatch.setattr(value.service, 'runtime_factory', lambda *a, **k: pytest.fail('Inert job constructed an audio runtime.'))
    monkeypatch.setattr(value.service, '_verify_artifacts', lambda *a, **k: pytest.fail('Inert job read its former output.'))
    with pytest.raises(ValueError):
        inspect(value)
    assert value.job_file.read_bytes() == before
    assert value.service.broker.snapshot()['leases'] == []


def test_source_bound_job_without_its_original_guard_refuses_before_external_reads(completed, monkeypatch):
    value = completed
    project = replace(value.project, roles=(Role('narrator', 'Narrator', 'retained:voice'),),
        scenes=(replace(value.project.scenes[0], lines=(
            Line('line', 'narrator', 'Literal retained text.', source_ref='carr-book-audio:original'),)),))
    state = read_json(value.job_file)
    state['project'], state['fingerprint'] = project.to_dict(), project.fingerprint
    state['operation_sha256'] = _operation_sha256(state)
    atomic_json(value.job_file, state, replace=True)
    monkeypatch.setattr(value.service, 'runtime_factory', lambda *a, **k: pytest.fail('Unguarded Book job activated audio.'))
    monkeypatch.setattr(value.service, '_verify_artifacts', lambda *a, **k: pytest.fail('Unguarded Book job read output.'))
    with pytest.raises(ValueError, match='original source guard'):
        inspect(value)
    assert value.service.broker.snapshot()['leases'] == []


def test_tampered_master_is_rejected_before_decoder_and_keeps_failed_source(completed, monkeypatch):
    value = completed
    with value.master.open('r+b') as stream:
        stream.seek(-2, os.SEEK_END)
        stream.write(b'\x01\x00')
    changed, state = value.master.read_bytes(), value.job_file.read_bytes()
    monkeypatch.setattr(silence, 'run_process', lambda *a, **k: pytest.fail('Changed receipt reached decoder.'))
    with pytest.raises(ValueError, match='checksum changed'):
        inspect(value)
    assert value.master.read_bytes() == changed and value.job_file.read_bytes() == state
    assert value.service.broker.snapshot()['leases'] == []


def test_saved_job_change_after_actual_measurement_refuses_the_report(completed, monkeypatch):
    value = completed
    before, calls = value.master.read_bytes(), []
    def factory(root, **options):
        runtime = value.factory(root, **options)
        actual = runtime.inspect_silence
        def changed(path, **kwargs):
            result = actual(path, **kwargs)
            calls.append(result['pcm_sha256'])
            state = read_json(value.job_file)
            state['progress'] = 'Deliberately changed after real measurement.'
            atomic_json(value.job_file, state, replace=True)
            return result
        runtime.inspect_silence = changed
        return runtime
    monkeypatch.setattr(value.service, 'runtime_factory', factory)
    with pytest.raises(ValueError, match='saved production job changed'):
        inspect(value)
    assert len(calls) == 1 and value.master.read_bytes() == before
    assert read_json(value.job_file)['progress'] == 'Deliberately changed after real measurement.'
    assert value.service.broker.snapshot()['leases'] == []


def test_after_final_artifact_hash_source_write_cannot_be_accepted(completed, monkeypatch):
    value = completed
    actual = value.service._verify_artifacts
    calls, job = [], value.job_file.read_bytes()
    def changed(state, **options):
        result = actual(state, **options)
        calls.append(True)
        if len(calls) == 2:
            # Windows's retained verified handle refuses this write. On POSIX
            # its final identity check rejects the changed source after hashing.
            with value.master.open('r+b') as stream:
                stream.seek(-2, os.SEEK_END)
                stream.write(b'\x02\x00')
        return result
    monkeypatch.setattr(value.service, '_verify_artifacts', changed)
    with pytest.raises((ValueError, PermissionError)):
        inspect(value)
    assert len(calls) == 2 and value.master.is_file()
    assert value.job_file.read_bytes() == job
    assert value.service.broker.snapshot()['leases'] == []


def test_cancellation_after_real_scan_cannot_report_completion_or_change_receipts(completed, monkeypatch):
    value = completed
    stopped, observed = threading.Event(), []
    before, actual = value.job_file.read_bytes(), silence._scan_pcm
    def scanned(*args, **kwargs):
        result = actual(*args, **kwargs)
        observed.append(result[1])
        stopped.set()
        return result
    monkeypatch.setattr(silence, '_scan_pcm', scanned)
    with pytest.raises(AudioCancelled):
        inspect(value, cancelled=stopped.is_set)
    assert observed == [1] and value.job_file.read_bytes() == before
    assert value.service.broker.snapshot()['leases'] == []
    assert not list(value.service.cache.glob('.carr-silence-*'))


@pytest.mark.parametrize('masked', [False, True], ids=['direct', 'masked'])
@pytest.mark.parametrize('order', [(0, 1), (1, 0)], ids=['first-owner-first', 'second-owner-first'])
def test_two_actual_native_families_retain_job_lock_stage_source_and_shared_lease(
        completed, monkeypatch, masked, order):
    value = completed
    before, files = value.job_file.read_bytes(), retained_files(value)
    original = silence.run_process
    permits = [threading.Event(), threading.Event()]
    observed = {'children': [], 'contexts': [], 'owners': []}

    def pending(args, **kwargs):
        original(args, **kwargs)
        raw = Path(args[-1])
        observed['raw'] = raw
        for index in range(2):
            ready = value.root / ('native-ready-' + str(index))
            code = ('import pathlib,time\n'
                    f'stream=open({str(raw)!r}, "rb")\n'
                    f'pathlib.Path({str(ready)!r}).write_bytes(b"ready")\n'
                    'time.sleep(60)\n')
            context = owned_process([sys.executable, '-I', '-c', code],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child = context.__enter__()
            observed['contexts'].append(context)
            observed['children'].append(child)
            deadline = time.monotonic() + 5
            while not ready.exists():
                assert time.monotonic() < deadline and child.poll() is None
                time.sleep(.01)
            def cleanup(index=index, context=context):
                if not permits[index].is_set():
                    return False
                context.__exit__(None, None, None)
                return True
            observed['owners'].append(CleanupOwner(cleanup))
        first = AudioCleanupPending('First actual child remains owned.', cleanup_owner=observed['owners'][0])
        second = AudioCleanupPending('Second actual child remains owned.', cleanup_owner=observed['owners'][1])
        second.__cause__ = first
        if masked:
            raise OSError('Synthetic outer cleanup error') from second
        raise second

    monkeypatch.setattr(silence, 'run_process', pending)
    try:
        with pytest.raises(OSError if masked else AudioCleanupPending) as caught:
            inspect(value)
        assert set(_pending_owners(caught.value)) == set(observed['owners'])
        assert locked(value) and len(value.service.broker.snapshot()['leases']) == 1
        assert observed['raw'].stat().st_size == RATE * 2 * 4
        assert all(child.poll() is None for child in observed['children'])
        with pytest.raises(RuntimeError, match='already being changed'):
            inspect(value)
        first, last = order
        permits[first].set()
        assert observed['owners'][first].close() is True
        assert locked(value) and len(value.service.broker.snapshot()['leases']) == 1
        assert observed['raw'].is_file() and observed['children'][last].poll() is None
        permits[last].set()
        assert observed['owners'][last].close() is True
        assert all(child.poll() is not None for child in observed['children'])
        assert not observed['raw'].parent.exists() and not locked(value)
        assert value.service.broker.snapshot()['leases'] == []
        assert value.job_file.read_bytes() == before and retained_files(value) == files
    finally:
        for permit in permits:
            permit.set()
        for owner in observed['owners']:
            owner.close()
        for context in observed['contexts'][len(observed['owners']):]:
            context.__exit__(None, None, None)


@pytest.mark.parametrize('mode', ['rejected', 'cancelled', 'busy'])
def test_runtime_construction_waits_for_actual_shared_admission(completed, monkeypatch, mode):
    from carr_library.operations import ResourceRequest, ResourceUnavailable
    value = completed
    original = value.job_file.read_bytes()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        pytest.fail('No runtime may be constructed without admission.')
    monkeypatch.setattr(value.service, 'runtime_factory', forbidden)
    if mode == 'rejected':
        budget = replace(value.budget, threads=value.service.broker.limits.cpu_slots + 1)
        with pytest.raises(ResourceUnavailable):
            value.service.inspect_silence(value.job, threshold_dbfs=-50,
                                          minimum_seconds=.1, budget=budget, runtime_binding=forbidden)
    elif mode == 'cancelled':
        with pytest.raises(AudioCancelled):
            inspect(value, cancelled=lambda: True, runtime_binding=forbidden)
    else:
        entered, stopped, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        broker = value.service.broker
        with broker.reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots),
                            operation_id='occupied-silence-cpu'):
            prior = broker.snapshot()['leases']
            actual_reason = broker._reason
            def reason(*args, **kwargs):
                result = actual_reason(*args, **kwargs)
                if result == 'cpu_slots':
                    entered.set()
                return result
            monkeypatch.setattr(broker, '_reason', reason)
            def worker():
                try:
                    inspect(value, cancelled=stopped.is_set, runtime_binding=forbidden)
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                assert entered.wait(5) and calls == []
                assert not finished.is_set() and broker.snapshot()['leases'] == prior
                stopped.set()
                thread.join(5)
                assert not thread.is_alive() and len(errors) == 1
                assert isinstance(errors[0], AudioCancelled)
                assert broker.snapshot()['leases'] == prior
            finally:
                stopped.set()
                thread.join(5)
    assert calls == [] and value.job_file.read_bytes() == original
    assert value.service.broker.snapshot()['leases'] == []


def test_injected_runtime_cannot_exceed_the_already_admitted_budget(completed, monkeypatch):
    value = completed
    called = []
    def factory(root, *, input_roots):
        assert len(value.service.broker.snapshot()['leases']) == 1
        called.append(True)
        return AudioRuntime(root, input_roots=input_roots,
                            budget=replace(value.budget, threads=2))
    monkeypatch.setattr(value.service, 'runtime_factory', factory)
    monkeypatch.setattr(silence, 'run_process', lambda *a, **k: pytest.fail('Unadmitted runtime decoded PCM.'))
    with pytest.raises(ValueError, match='admitted resource budget'):
        inspect(value)
    assert called == [True] and value.service.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('restored', [False, True], ids=['current-audio-settings', 'restored-settings'])
def test_pcm_only_runtime_preserves_selected_settings_and_never_opens_unused_music_assets(
        completed, monkeypatch, restored):
    from carr_library import runtime_settings
    value = completed
    private = value.root / 'unrelated-private-assets'
    actual_runtime = value.runtimes[-1]
    settings = {'schema': 1, 'ffmpeg': str(actual_runtime.ffmpeg),
        'ffprobe': str(actual_runtime.ffprobe), 'piper': [], 'voice_helpers': [],
        'music_models': [{'engine': 'musicgen', 'python': str(private / 'python.exe'),
                          'directory': str(private / 'model'), 'manifest': str(private / 'model.json')}],
        'fluidsynth': {'binary': str(private / 'fluidsynth.exe'),
                      'soundfont': str(private / 'soundfont.sf2'),
                      'sha256': '1' * 64, 'license_notice': 'Private fixture declaration only.'}}
    if restored:
        settings['restore_requires_revalidation'] = True
    path = runtime_settings.settings_path(value.service.root.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, settings)
    before, validated = path.read_bytes(), []
    original_verify = runtime_settings.verify_configured_audio_runtime
    def verify(configuration, **options):
        assert len(value.service.broker.snapshot()['leases']) == 1
        validated.append(True)
        return original_verify(configuration, **options)
    original_open = Path.open
    def open_file(candidate, *args, **kwargs):
        if candidate.is_relative_to(private):
            pytest.fail('PCM inspection opened an unrelated private music asset.')
        return original_open(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(runtime_settings, 'verify_configured_audio_runtime', verify)
    monkeypatch.setattr(runtime_settings, '_review_fluidsynth',
                        lambda *a, **k: pytest.fail('PCM inspection reviewed an unused SoundFont.'))
    monkeypatch.setattr(AudioRuntime, 'configure_music_model',
                        lambda *a, **k: pytest.fail('PCM inspection configured an unused music model.'))
    monkeypatch.setattr(value.service, '_runtime',
                        lambda *a, **k: pytest.fail('PCM inspection activated the whole production runtime.'))
    monkeypatch.setattr(value.service, 'runtime_factory', None)
    if restored:
        with pytest.raises(ValueError, match='Restored local engine settings'):
            inspect(value)
        assert validated == []
    else:
        report = inspect(value)
        assert report['interval_count'] == 1 and validated == [True]
        assert report['source']['sha256'] == sha256(value.master.read_bytes()).hexdigest()
    assert path.read_bytes() == before and value.service.broker.snapshot()['leases'] == []
