"""Controlled admission races with real synthetic child processes, never ASR inference."""
from importlib.util import cache_from_source, module_from_spec, spec_from_file_location
import json
from pathlib import Path
import py_compile
import threading
import time

import pytest

from carr_library.audio.models import AudioBudget
from carr_library.durable_state import read_json
from carr_library.operations import ResourceBroker
from carr_library.transcription import TranscriptionService
from carr_library.transcription import service as module
from carr_library.transcription.models import worker_settings

_spec = spec_from_file_location('admission_fixture_builder', Path(__file__).with_name('fixture_runtime.py'))
_builder = module_from_spec(_spec)
_spec.loader.exec_module(_builder)


@pytest.fixture
def setup(tmp_path):
    runtime = _builder.make_runtime(tmp_path / 'runtime')
    service = TranscriptionService(tmp_path / 'library/state/transcription',
        budget=AudioBudget(timeout=5, reserve_disk_bytes=0), stop_grace=.1)
    profile = _builder.register(service, runtime)
    sources = []
    for name in ('first.wav', 'second.wav'):
        source = tmp_path / name
        source.write_bytes(b'Synthetic retained input: ' + name.encode())
        sources.append(source)
    return service, runtime, profile, sources


def starts(runtime):
    path = runtime.worker.with_name('fixture-starts.log')
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


def historical_job(setup):
    service, runtime, profile, sources = setup
    runtime.options.write_text(json.dumps({'fail_after': 1}), encoding='utf-8')
    created = service.create_job(profile['id'], sources)
    partial = service.run(created['id'])
    assert partial['status'] == 'partial' and partial['done'] == 1, partial
    runtime.options.write_text('{}', encoding='utf-8')
    return partial


@pytest.mark.parametrize('intent', ['pause', 'cancel'])
def test_stop_interrupts_admission_hash_and_reaches_child_cleanup(setup, monkeypatch, intent):
    service, runtime, profile, sources = setup
    state = historical_job(setup)
    accepted = dict(state['results'][0])
    pending = next(source for source in state['sources'] if source['id'] != accepted['source_id'])
    target = service._source_path(state, pending)
    before_starts = starts(runtime)
    runtime.options.write_text(json.dumps({'hold_after_checkpoint': 10, 'ignore_stop': True}), encoding='utf-8')
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = module.file_identity
    callbacks, outcome, progress = [], {}, []
    # Failure-only, in-memory diagnostics for the existing stop requirement.
    # Delegates keep their original arguments, results, errors and deadlines.
    import sys
    from carr_library.audio import process as process_module
    from carr_library.operations.broker import ResourceLease

    phase_started, phase_log = time.monotonic(), []
    diagnostic_active, observed_control = [False], []

    def phase(name, detail=''):
        if diagnostic_active[0] and len(phase_log) < 96:
            phase_log.append((round(time.monotonic() - phase_started, 6), name, detail))

    def traced(name, operation, *args, **kwargs):
        phase(name + ':begin')
        try:
            result = operation(*args, **kwargs)
        except BaseException as error:
            phase(name + ':raised', type(error).__name__)
            raise
        phase(name + ':end')
        return result

    control, save = service._control, service._save
    atomic_flags, stop_owned = module.atomic_bytes, process_module._stop_owned
    release_lease = ResourceLease.release

    def observed_action(*args, **kwargs):
        selected = control(*args, **kwargs)
        if selected != 'run' and not observed_control:
            observed_control.append(selected)
            phase('control:observed', selected)
        return selected

    def observed_save(*args, **kwargs):
        return traced('job-state', save, *args, **kwargs)

    def observed_flag(path, *args, **kwargs):
        name = Path(path).name
        label = name if name in ('pause.flag', 'stop.flag') else 'other-flag'
        return traced(label, atomic_flags, path, *args, **kwargs)

    def observed_stop(*args, **kwargs):
        return traced('owned-family-close', stop_owned, *args, **kwargs)

    def observed_release(lease, *args, **kwargs):
        if lease.operation_id == 'asr:' + state['id']:
            return traced('resource-release', release_lease, lease, *args, **kwargs)
        return release_lease(lease, *args, **kwargs)

    monkeypatch.setattr(service, '_control', observed_action)
    monkeypatch.setattr(service, '_save', observed_save)
    monkeypatch.setattr(module, 'atomic_bytes', observed_flag)
    monkeypatch.setattr(process_module, '_stop_owned', observed_stop)
    monkeypatch.setattr(ResourceLease, 'release', observed_release)

    def report(event):
        progress.append(dict(event))
        phase('progress', (event.get('phase'), event.get('status')))

    def runner_stack():
        # Capture the blocked location before finally releases the synthetic
        # hash barrier. No frame locals, absolute paths or source text are read.
        frame = sys._current_frames().get(runner.ident)
        stack = []
        try:
            while frame is not None and len(stack) < 32:
                stack.append((Path(frame.f_code.co_filename).name,
                              frame.f_code.co_name, frame.f_lineno))
                frame = frame.f_back
            return stack
        finally:
            del frame

    def controlled_hash(path, *, cancel=None, maximum=64 * 1024**3):
        if Path(path) == target and service._load(state['id'])['status'] == 'running':
            assert starts(runtime) == before_starts + 1, 'Admission must follow the new actual child checkpoint.'
            callbacks.append(cancel)
            diagnostic_active[0] = True
            phase('admission-hash:entered')
            entered.set()
            # Simulate many read blocks without a multi-GiB fixture. A missing
            # cancellation callback blocks until the test's cleanup releases it.
            try:
                while not release.wait(.01):
                    module.stopped(cancel)
            except BaseException as error:
                phase('admission-hash:raised', type(error).__name__)
                raise
            phase('admission-hash:released')
        return original(path, cancel=cancel, maximum=maximum)

    monkeypatch.setattr(module, 'file_identity', controlled_hash)
    def run():
        try:
            outcome['state'] = service.resume(state['id'], progress=report)
        except BaseException as error:
            outcome['error'] = error
        finally:
            phase('runner:finished')
            finished.set()

    runner = threading.Thread(target=run)
    # Windows inventories the real base interpreter twice before Popen. Bound
    # that readiness separately from the 1.5-second stop requirement below;
    # neither the five-second child budget nor stop_grace is increased.
    ready_deadline = time.monotonic() + 30

    def await_ready(predicate, phase):
        while not predicate():
            ended = finished.wait(.02)
            if ended or time.monotonic() >= ready_deadline:
                pytest.fail(
                    f'{phase}: runner_finished={ended}; actual_starts={starts(runtime)} '
                    f'(previous={before_starts}); admission_entered={entered.is_set()}; '
                    f'last_progress={progress[-1:]!r}; outcome={outcome!r}'
                )

    runner.start()
    promptly_stopped = False
    stop_requested = False
    timed_out_stack = []
    try:
        await_ready(lambda: starts(runtime) > before_starts, 'Waiting for the new actual child start')
        assert starts(runtime) == before_starts + 1
        await_ready(entered.is_set, 'Waiting for checkpoint source verification')
        phase('stop-request:begin', intent)
        getattr(service, intent)(state['id'])
        phase('stop-request:return', intent)
        stop_requested = True
        runner.join(1.5)
        promptly_stopped = not runner.is_alive()
        if not promptly_stopped:
            timed_out_stack = runner_stack()
            phase('stop-deadline:runner-live')
    finally:
        phase('cleanup:release-hash')
        release.set()
        if not stop_requested and runner.is_alive():
            # A readiness failure must also stop preflight hashing/a live child.
            getattr(service, intent)(state['id'])
        runner.join(6)
    diagnostics = {
        'phases': phase_log, 'stack_before_hash_release': timed_out_stack,
        'final_status': outcome.get('state', {}).get('status'),
        'error_type': type(outcome['error']).__name__ if 'error' in outcome else None,
    }
    assert not runner.is_alive(), ('The child runner was not cleaned up.', diagnostics)
    assert promptly_stopped, ('Checkpoint hashing prevented stop/grace handling.', diagnostics)
    assert callbacks and all(callable(callback) for callback in callbacks)
    assert 'error' not in outcome, outcome
    result = outcome['state']
    assert result['status'] == ('paused' if intent == 'pause' else 'cancelled')
    assert result['done'] == 1 and result['results'] == [accepted]
    assert (service._job_root(state['id']) / 'worker/stop.flag').is_file()
    assert not ResourceBroker.for_app(service.library_root).snapshot()['leases']
    assert service.read_transcript(state['id'], accepted['source_id'])['text']


@pytest.mark.parametrize('timing', ['before_commit', 'after_commit'])
@pytest.mark.parametrize('historical', [False, True])
def test_runtime_change_rejects_attempt_without_resurrecting_cache(setup, monkeypatch, timing, historical):
    service, runtime, profile, sources = setup
    state = historical_job(setup) if historical else service.create_job(profile['id'], sources[:1])
    previous = list(state['results'])
    before_starts = starts(runtime)
    model = runtime.model / 'model.bin'
    original_model = model.read_bytes()
    publish, process = service._publish_result, module.run_process
    archive = getattr(service, '_archive_worker', None)
    active, mutated, archived, committed = [False], [False], [], []

    def actual_child(*args, **kwargs):
        active[0] = True
        try:
            return process(*args, **kwargs)
        finally:
            active[0] = False  # Underlying runner already closed child/readers.

    def mutate():
        model.write_bytes(original_model + b' changed during current attempt')
        mutated[0] = True

    def publication(state, source, result, provenance, **kwargs):
        if not mutated[0] and timing == 'before_commit':
            mutate()
        publish(state, source, result, provenance, **kwargs)
        committed.append(source['id'])
        if not mutated[0]:
            mutate()

    def inactive_archive(state, profile_id):
        assert not active[0], 'A live worker workspace was moved.'
        assert not ResourceBroker.for_app(service.library_root).snapshot()['leases']
        archived.append(True)
        return archive(state, profile_id)

    monkeypatch.setattr(module, 'run_process', actual_child)
    monkeypatch.setattr(service, '_publish_result', publication)
    monkeypatch.setattr(service, '_archive_worker', inactive_archive, raising=False)
    rejected = service.resume(state['id'])
    assert rejected['status'] == ('partial' if historical else 'failed'), rejected
    assert rejected['results'] == previous and rejected['done'] == len(previous)
    assert mutated[0] and archived == [True]
    assert bool(committed) is (timing == 'after_commit')
    assert 'New results from this attempt were rejected' in rejected['error']
    root = service._job_root(state['id'])
    assert list((root / 'worker-history').glob('*/logs/cache/**/*.cache.json'))
    assert list((root / 'artifacts').glob('*/*/receipt.json')), 'Physical result evidence was removed.'
    if previous:
        assert service.read_transcript(state['id'], previous[0]['source_id'])['text']
    else:
        destination = runtime.root / 'rejected-export'; destination.mkdir()
        with pytest.raises(ValueError, match='No verified'):
            service.export(state['id'], destination)
    model.write_bytes(original_model)
    resumed = service.resume(state['id'])
    assert resumed['status'] == 'complete', resumed['error']
    assert starts(runtime) == before_starts + 2, 'A rejected raw checkpoint was resurrected instead of rerunning.'
    assert resumed['results'][:len(previous)] == previous


@pytest.mark.parametrize('interruption', [OSError, SystemExit])
def test_failed_archive_keeps_durable_cache_rejection_until_retry(setup, monkeypatch, interruption):
    service, runtime, profile, sources = setup
    state = service.create_job(profile['id'], sources[:1])
    model = runtime.model / 'model.bin'; before = model.read_bytes()
    publish, archive = service._publish_result, service._archive_worker
    changed = []
    def publication(*args, **kwargs):
        publish(*args, **kwargs)
        if not changed:
            model.write_bytes(before + b' rejected runtime change')
            changed.append(True)
    def unavailable_archive(*args):
        raise interruption('Synthetic archive failure/interruption while preserving worker evidence')
    monkeypatch.setattr(service, '_publish_result', publication)
    monkeypatch.setattr(service, '_archive_worker', unavailable_archive)
    if interruption is SystemExit:
        with pytest.raises(SystemExit, match='archive'):
            service.run(state['id'])
        failed = service._load(state['id'])
    else:
        failed = service.run(state['id'])
    assert failed['results'] == [] and service._load(state['id'])['worker_cache_rejected'] is True
    model.write_bytes(before)
    monkeypatch.setattr(service, '_archive_worker', archive)
    recovered = service.resume(state['id'])
    assert recovered['status'] == 'complete' and starts(runtime) == 2, recovered
    assert not recovered.get('worker_cache_rejected')


def test_idle_inventory_is_cancellable_without_rehashing_runtime(setup, monkeypatch):
    service, runtime, profile, sources = setup
    state = service.create_job(profile['id'], sources[:1])
    cache = service._job_root(state['id']) / 'worker/logs/cache'
    cache.mkdir(parents=True)
    for number in range(12):
        (cache / f'unfinished-{number}').mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError('An idle cache inventory rehashed the model.')
    monkeypatch.setattr(service, '_verify_profile', forbidden)
    expected = worker_settings(state['settings'], profile['model_name'])
    service._admit_results(state, profile, expected, cancel=lambda: False)
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 7
    with pytest.raises(InterruptedError):
        service._admit_results(state, profile, expected, cancel=cancelled)
    assert len(calls) == 7 and state['results'] == []


@pytest.mark.parametrize('existing_cache', [False, True])
def test_actual_child_import_never_writes_or_replaces_python_bytecode(setup, existing_cache):
    service, runtime, profile, sources = setup
    probe = runtime.root / 'synthetic_stdlib_probe.py'
    probe.write_text("VALUE = 'old'\n", encoding='utf-8')
    cached = Path(cache_from_source(str(probe)))
    if existing_cache:
        py_compile.compile(str(probe), doraise=True)
    before = cached.read_bytes() if cached.exists() else None
    probe.write_text("VALUE = 'current synthetic source value'\n", encoding='utf-8')
    runtime.options.write_text(json.dumps({'import_probe': str(probe)}), encoding='utf-8')
    result = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert result['status'] == 'complete', result['error']
    observed = read_json(service._job_root(result['id']) / 'worker/bytecode-probe.json')
    assert observed == {'dont_write_bytecode': True, 'value': 'current synthetic source value'}
    assert (cached.read_bytes() if cached.exists() else None) == before


@pytest.mark.parametrize('intent', ['pause', 'cancel'])
def test_known_runtime_rejection_takes_precedence_over_simultaneous_stop(setup, monkeypatch, intent):
    service, runtime, profile, sources = setup
    state = service.create_job(profile['id'], sources[:1])
    publish, verify = service._publish_result, service._verify_profile
    changed, detected = [], []
    def publication(*args, **kwargs):
        publish(*args, **kwargs)
        if not changed:
            model = runtime.model / 'model.bin'
            model.write_bytes(model.read_bytes() + b' changed after receipt')
            changed.append(True)
    def verification(*args, **kwargs):
        try:
            return verify(*args, **kwargs)
        except module._RuntimeProfileChanged:
            detected.append(True)
            getattr(service, intent)(state['id'])
            raise
    monkeypatch.setattr(service, '_publish_result', publication)
    monkeypatch.setattr(service, '_verify_profile', verification)
    result = service.run(state['id'])
    assert detected and result['status'] == 'failed', result
    assert result['results'] == [] and result['done'] == 0
    assert list((service._job_root(state['id']) / 'worker-history').iterdir())


def test_cancelled_post_admission_check_preserves_committed_valid_receipt(setup, monkeypatch):
    service, runtime, profile, sources = setup
    state = service.create_job(profile['id'], sources[:1])
    verify, paused = service._verify_profile, []
    def verification(*args, **kwargs):
        if not paused and service._load(state['id'])['results']:
            paused.append(True)
            service.pause(state['id'])
            raise InterruptedError('Synthetic cancellation after a verified commit; no mutation evidence.')
        return verify(*args, **kwargs)
    monkeypatch.setattr(service, '_verify_profile', verification)
    result = service.run(state['id'])
    assert paused and result['status'] == 'paused' and result['done'] == 1, result
    accepted = dict(result['results'][0])
    assert service.read_transcript(state['id'])['text']
    resumed = service.resume(state['id'])
    assert resumed['status'] == 'complete' and resumed['results'] == [accepted], resumed
    assert starts(runtime) == 1
