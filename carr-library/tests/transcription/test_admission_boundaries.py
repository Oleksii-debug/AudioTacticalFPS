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
from carr_library.integrations import transcription_protocol as module
from carr_library.integrations.transcription_values import worker_settings

_spec = spec_from_file_location('admission_fixture_builder', Path(__file__).with_name('fixture_runtime.py'))
_builder = module_from_spec(_spec)
_spec.loader.exec_module(_builder)
TranscriptionService = _builder.make_service


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


@pytest.mark.parametrize('mutation', ['same_size', 'grow', 'truncate'])
def test_export_rejects_changed_payload_before_external_publication(setup, monkeypatch, mutation):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    record = service._receipt(state, state['results'][0])['files']['txt']
    path = service._job_root(state['id']) / record['path']
    before = path.read_bytes()
    assert before
    changed = {'same_size': bytes([before[0] ^ 1]) + before[1:],
               'grow': before + b'x', 'truncate': before[:-1]}[mutation]
    verify = service._verify_source

    def mutate_after_receipt(*args, **kwargs):
        verify(*args, **kwargs)
        path.write_bytes(changed)

    destination = runtime.root / 'export-mutation'
    destination.mkdir()
    monkeypatch.setattr(service, '_verify_source', mutate_after_receipt)
    with pytest.raises(ValueError, match='changed before export'):
        service.export(state['id'], destination, formats=['txt'])
    assert not list(destination.iterdir())
    assert path.read_bytes() == changed
    assert service._load(state['id'])['results'] == state['results']


def test_export_receipt_verification_observes_cancellation(setup, monkeypatch):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    destination = runtime.root / 'export-cancel'
    destination.mkdir()
    receipt = service._receipt
    entered = []

    def cancel():
        return bool(entered)

    def verify(*args, **kwargs):
        entered.append(True)
        assert callable(kwargs.get('cancel')) and kwargs['cancel']() is True
        return receipt(*args, **kwargs)

    monkeypatch.setattr(service, '_receipt', verify)
    with pytest.raises(InterruptedError):
        service.export(state['id'], destination, formats=['txt'], cancel=cancel)
    assert not list(destination.iterdir())
    assert service._load(state['id'])['results'] == state['results']


def test_export_payload_is_bounded_and_interruptible(tmp_path):
    from hashlib import sha256
    path = tmp_path / 'synthetic.txt'
    payload = b'x' * (2 * 1024 * 1024 + 17)
    path.write_bytes(payload)
    record = {'size': len(payload), 'sha256': sha256(payload).hexdigest()}
    assert module._export_payload(path, record) == payload
    polls = []

    def cancel():
        polls.append(True)
        return len(polls) >= 3

    with pytest.raises(InterruptedError):
        module._export_payload(path, record, cancel=cancel)
    assert len(polls) == 3
    assert path.read_bytes() == payload


@pytest.mark.parametrize('field', ['text', 'provenance', 'inventory'])
def test_saved_receipt_decodes_only_verified_bytes(setup, monkeypatch, field):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    record = state['results'][0]
    path = service._job_root(state['id']) / record['receipt']
    original = service._receipt(state, record)
    changed = json.loads(json.dumps(original))
    if field == 'text':
        changed['result']['text'] = 'Unverified text'
    elif field == 'provenance':
        changed['provenance']['unverified'] = True
    else:
        changed['files'] = {}
    open_path = Path.open
    reads = []

    def swap_on_second_read(self, mode='r', *args, **kwargs):
        if self == path and mode == 'rb':
            reads.append(True)
            if len(reads) == 2:
                with open_path(path, 'wb') as stream:
                    stream.write(json.dumps(changed).encode('utf-8'))
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', swap_on_second_read)
    assert service._receipt(state, record) == original
    assert len(reads) == 1


def test_saved_receipt_corruption_blocks_external_export(setup):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    record = state['results'][0]
    path = service._job_root(state['id']) / record['receipt']
    raw = path.read_bytes()
    path.write_bytes(raw + b' ')
    destination = runtime.root / 'receipt-corruption-export'
    destination.mkdir()
    with pytest.raises(ValueError, match='saved transcript receipt changed'):
        service.export(state['id'], destination, formats=['txt'])
    assert not list(destination.iterdir())
    assert service._load(state['id'])['results'] == state['results']


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_verified_json_preserves_strict_state_parsing(tmp_path, raw):
    from hashlib import sha256
    path = tmp_path / 'synthetic.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        module._verified_json(path, sha256(raw).hexdigest())


@pytest.mark.parametrize('operation', ['read', 'export'])
def test_edited_copy_decodes_only_the_bytes_it_verifies(setup, monkeypatch, operation):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    source_id = state['results'][0]['source_id']
    edited = service.save_edit(state['id'], source_id, 'Verified edit')
    path = service._job_root(state['id']) / edited['path']
    original = path.read_bytes()
    changed = original.replace(b'Verified edit', b'Injected edit')
    assert changed != original and len(changed) == len(original)
    open_path = Path.open
    reads = []

    def swap_on_second_read(self, mode='r', *args, **kwargs):
        if self == path and mode == 'rb':
            reads.append(True)
            if len(reads) == 2:
                with open_path(path, 'wb') as stream:
                    stream.write(changed)
        return open_path(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', swap_on_second_read)
    if operation == 'read':
        assert service.read_transcript(state['id'], source_id, edited=True)['text'] == 'Verified edit'
    else:
        destination = runtime.root / 'edit-export'
        destination.mkdir()
        manifest = service.export(state['id'], destination, formats=['txt'])
        exported = next(item for item in manifest['files'] if item['edited'])
        assert Path(exported['path']).read_bytes() == b'Verified edit'
    assert len(reads) == 1


def test_edited_copy_export_uses_the_captured_revision(setup, monkeypatch):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    source_id = state['results'][0]['source_id']
    service.save_edit(state['id'], source_id, 'Selected revision')
    verify = service._verify_source
    changed = []

    def update_during_export(*args, **kwargs):
        verify(*args, **kwargs)
        if not changed:
            changed.append(True)
            service.save_edit(state['id'], source_id, 'Later revision')

    monkeypatch.setattr(service, '_verify_source', update_during_export)
    destination = runtime.root / 'edit-revision-export'
    destination.mkdir()
    manifest = service.export(state['id'], destination, formats=['txt'])
    exported = next(item for item in manifest['files'] if item['edited'])
    assert Path(exported['path']).read_bytes() == b'Selected revision'
    assert service.read_transcript(state['id'], source_id, edited=True)['text'] == 'Later revision'


def test_edited_copy_rejects_changed_bytes_and_observes_cancel(setup):
    service, runtime, profile, sources = setup
    state = service.run(service.create_job(profile['id'], sources[:1])['id'])
    assert state['status'] == 'complete', state['error']
    source_id = state['results'][0]['source_id']
    edited = service.save_edit(state['id'], source_id, 'Verified edit')
    state = service._load(state['id'])
    path = service._job_root(state['id']) / edited['path']
    polls = []

    def cancel():
        polls.append(True)
        return len(polls) >= 3

    with pytest.raises(InterruptedError):
        service._read_edit(state, source_id, cancel=cancel)
    path.write_bytes(path.read_bytes().replace(b'Verified edit', b'Injected edit'))
    with pytest.raises(ValueError, match='changed before export'):
        service.read_transcript(state['id'], source_id, edited=True)
    assert service._load(state['id'])['edits'] == state['edits']


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

    def controlled_hash(path, *, cancel=None, maximum=64 * 1024**3):
        if Path(path) == target and service._load(state['id'])['status'] == 'running':
            assert starts(runtime) == before_starts + 1, 'Admission must follow the new actual child checkpoint.'
            callbacks.append(cancel)
            entered.set()
            # Simulate many read blocks without a multi-GiB fixture. A missing
            # cancellation callback blocks until the test's cleanup releases it.
            while not release.wait(.01):
                module.stopped(cancel)
        return original(path, cancel=cancel, maximum=maximum)

    monkeypatch.setattr(module, 'file_identity', controlled_hash)
    def run():
        try:
            outcome['state'] = service.resume(state['id'], progress=lambda event: progress.append(dict(event)))
        except BaseException as error:
            outcome['error'] = error
        finally:
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
    try:
        await_ready(lambda: starts(runtime) > before_starts, 'Waiting for the new actual child start')
        assert starts(runtime) == before_starts + 1
        await_ready(entered.is_set, 'Waiting for checkpoint source verification')
        getattr(service, intent)(state['id'])
        stop_requested = True
        runner.join(1.5)
        promptly_stopped = not runner.is_alive()
    finally:
        release.set()
        if not stop_requested and runner.is_alive():
            # A readiness failure must also stop preflight hashing/a live child.
            getattr(service, intent)(state['id'])
        runner.join(6)
    assert not runner.is_alive(), 'The child runner was not cleaned up.'
    assert promptly_stopped, 'Checkpoint hashing prevented stop/grace handling.'
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


@pytest.mark.parametrize('intent', ['pause', 'cancel', 'callback'])
@pytest.mark.parametrize('completed', [False, True])
def test_stop_during_historical_receipt_hash_preserves_durable_results(setup, monkeypatch, intent, completed):
    service, runtime, profile, sources = setup
    if completed:
        state = service.run(service.create_job(profile['id'], sources[:1])['id'])
        assert state['status'] == 'complete', state
    else:
        state = historical_job(setup)
    previous = list(state['results'])
    before_starts = starts(runtime)
    target = service._job_root(state['id']) / previous[0]['receipt']
    entered, release, token = threading.Event(), threading.Event(), threading.Event()
    original = module._verified_json
    callbacks, outcome = [], {}

    def controlled_hash(path, digest, *, cancel=None, **kwargs):
        if Path(path) == target:
            callbacks.append(cancel)
            entered.set()
            while not release.wait(.01):
                module.stopped(cancel)
        return original(path, digest, cancel=cancel, **kwargs)

    monkeypatch.setattr(module, '_verified_json', controlled_hash)

    def run():
        try:
            outcome['state'] = service.resume(state['id'], cancel=token.is_set)
        except BaseException as error:
            outcome['error'] = error

    runner = threading.Thread(target=run)
    runner.start()
    promptly_stopped = False
    try:
        assert entered.wait(4), 'Historical receipt verification was not reached.'
        if intent == 'callback':
            token.set()
        else:
            getattr(service, intent)(state['id'])
        runner.join(1.5)
        promptly_stopped = not runner.is_alive()
    finally:
        release.set()
        runner.join(6)
    assert not runner.is_alive()
    assert promptly_stopped, 'Historical receipt verification blocked stop.'
    assert callbacks and all(callable(callback) for callback in callbacks)
    saved = service.get_job(state['id'])
    assert saved['results'] == previous and saved['done'] == len(previous)
    assert starts(runtime) == before_starts
    assert not ResourceBroker.for_app(service.library_root).snapshot()['leases']
    if completed:
        assert isinstance(outcome.get('error'), InterruptedError), outcome
        assert saved['status'] == 'complete'
    else:
        assert 'error' not in outcome, outcome
        expected = 'paused' if intent == 'pause' else 'cancelled'
        assert saved['status'] == outcome['state']['status'] == expected
    # A fresh explicit resume revalidates the original receipt; no accepted
    # historical source is transcribed twice.
    resumed = service.resume(state['id'])
    assert resumed['status'] == 'complete', resumed
    assert resumed['results'][:len(previous)] == previous
    assert starts(runtime) == before_starts + (0 if completed else 1)


def test_terminal_only_status_policy_keeps_real_child_checkpoint_progress(setup, monkeypatch):
    service, runtime, profile, sources = setup
    runtime.options.write_text(json.dumps({'hold_after_checkpoint': .35}), encoding='utf-8')
    state = service.create_job(profile['id'], sources[:1])
    run_process, read_status = module.run_process, module._worker_status
    active, reads, updates = [False], [], []

    def actual_child(*args, **kwargs):
        active[0] = True
        try:
            return run_process(*args, **kwargs)
        finally:
            active[0] = False  # The actual runner already owns/reaps its family.

    def status_after_exit(path, total):
        reads.append((Path(path), active[0]))
        assert not active[0], 'The host opened mutable status while the real worker was alive.'
        return read_status(path, total)

    def observe(event):
        updates.append((dict(event), active[0]))

    # This exercises the Windows policy on every test host without replacing
    # os.name, the process runner, the child, its writes or checkpoint admission.
    monkeypatch.setattr(module, '_LIVE_STATUS_SNAPSHOTS', False)
    monkeypatch.setattr(module, 'run_process', actual_child)
    monkeypatch.setattr(module, '_worker_status', status_after_exit)
    result = service.run(state['id'], progress=observe)
    assert result['status'] == 'complete', result
    assert reads and not any(live for _, live in reads)
    assert any(live and event['done'] == 1 for event, live in updates)
    assert starts(runtime) == 1
    assert service.read_transcript(state['id'])['text']


@pytest.mark.parametrize('payload', [
    b'{"state":"done","done":-1,"failed":0,"total":1}',
    b'{"state":"done","done":1,"failed":0,"total":1,"done":0}',
    b' ' * (256 * 1024 + 1),
])
def test_terminal_status_retains_strict_schema_and_exact_budget(tmp_path, payload):
    path = tmp_path / 'job_status.json'
    path.write_bytes(payload)
    from carr_library.ingest.restore import RestoreError
    with pytest.raises((RestoreError, ValueError)):
        module._worker_status(path, 1)
