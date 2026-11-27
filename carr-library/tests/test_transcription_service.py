"""Real filesystem/process proofs for the neutral supplied-worker supervisor.

The child below is a deliberately synthetic protocol peer, not Whisper, and
does not decode audio or qualify any model/language. Private model-loop tests
and actual ASR inference remain separate acceptance gates.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
import time

import pytest

from carr_library.audio.jobs import atomic_json, project_lock, read_json
from carr_library.audio.model_manifest import LocalModelManifest
from carr_library.audio.models import canonical
from carr_library import transcription_service as service
from carr_library.transcription_receipt import DEFAULT_SETTINGS, PROTOCOL
from carr_library.transcription_state import read_definition, validate_definition


CHILD = r"""
import hashlib, json, os, sys, time
from pathlib import Path
def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def digest(value):
    return hashlib.sha256(value).hexdigest()
def write(path, value):
    stage = path.with_name(path.name + '.synthetic-part')
    stage.write_text(canonical(value), encoding='utf-8')
    os.replace(stage, path)
job = Path(sys.argv[sys.argv.index('--job-dir') + 1])
config_path = job / 'job_config.json'
config = json.loads(config_path.read_text(encoding='utf-8'))
settings = config['settings']
runtime = Path(config['runtime_manifest'])
manifest = json.loads(runtime.read_text(encoding='utf-8'))
prefix = manifest['settings']['models_dir'] + '/' + settings['model'] + '/'
model = {name[len(prefix):]: checksum for name, checksum in manifest['files'].items() if name.startswith(prefix)}
model_sha = digest(canonical(model).encode('utf-8'))
mode = (Path(__file__).parent / 'behavior.txt').read_text(encoding='ascii')
protocol = 'supplied-whisper-v43-hardened-1'
status = dict(protocol=protocol, run_id=config['run_id'], state='processing', done=0,
              failed=0, total=len(config['inputs']), active_files=[])
write(job / 'job_status.json', status)
if mode == 'wait':
    while not (job / 'stop.flag').exists():
        time.sleep(.01)
    status['state'] = 'зупинено'
    write(job / 'job_status.json', status)
    raise SystemExit(130)
if mode == 'status_race':
    time.sleep(.25)
if mode == 'zero_only':
    status.update(state='завершено', done=len(config['inputs']))
    write(job / 'job_status.json', status)
    raise SystemExit(0)
owner = dict(job_dir=str(job), protocol=protocol)
root = Path(config['results_root']) / 'verified-v43' / digest(canonical(owner).encode('utf-8'))[:24]
root.mkdir(parents=True, exist_ok=True)
write(root / 'owner.json', dict(schema='supplied-whisper-v43-integrity/1/owner',
    payload=owner, sha256=digest(canonical(owner).encode('utf-8'))))
outputs = []
for format in ('txt', 'srt', 'vtt', 'json'):
    if not settings['output_' + format]:
        continue
    groups = [[index] for index in range(len(config['inputs']))]
    if format == 'txt' and settings['output_mode'] == 'one_whole_job':
        groups = [list(range(len(config['inputs'])))]
    elif format == 'txt' and settings['output_mode'] == 'one_per_folder':
        grouped = {}
        for index, row in enumerate(config['inputs']):
            grouped.setdefault(row['root'], []).append(index)
        groups = list(grouped.values())
    for number, indices in enumerate(groups):
        relative = 'output-' + str(number) + '.' + format
        data = ('Synthetic protocol text; не реальна транскрипція. ' + str(indices) + '\n').encode('utf-8')
        (root / relative).write_bytes(data)
        outputs.append(dict(path=relative, format=format, source_indexes=indices,
                            bytes=len(data), sha256=digest(data)))
if mode == 'nonzero':
    raise SystemExit(3)
payload = dict(run_id=config['run_id'], job_root=str(job),
    job_config_sha256=digest(config_path.read_bytes()), results_root=str(root),
    output_settings={key: settings[key] for key in
        ('output_mode','include_headers','output_txt','output_srt','output_vtt','output_json')},
    inputs=[dict(row, effective_settings=settings, model_sha256=model_sha,
                 runtime_sha256=config['runtime_manifest_sha256']) for row in config['inputs']],
    outputs=outputs)
receipt = dict(schema=protocol, payload=payload, sha256=digest(canonical(payload).encode('utf-8')))
receipt_path = job / 'logs' / 'completion-v1.json'
write(receipt_path, receipt)
status.update(state='завершено', done=len(config['inputs']),
              completion_sha256=digest(receipt_path.read_bytes()))
write(job / 'job_status.json', status)
if mode == 'tamper':
    (root / outputs[0]['path']).write_bytes(b'changed after receipt')
"""


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def prepared_environment(tmp_path):
    def make(mode='success', *, settings=None):
        root = tmp_path / ('runtime-' + mode)
        root.mkdir()
        assets = {
            'python-fixture': b'Nonexecutable inventory fixture; process tests explicitly bind the test interpreter.',
            'worker.py': CHILD.encode('utf-8'),
            'behavior.txt': mode.encode('ascii'),
            'models/small/model.bin': b'SYNTHETIC MODEL BYTES, NOT WEIGHTS',
            'models/small/config.json': b'{}',
            'models/small/tokenizer.json': b'{}',
        }
        for name, raw in assets.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        manifest = dict(schema_version=1, engine='supplied-whisper', model_id='synthetic-protocol-peer',
            revision='a' * 40, license='TEST FIXTURE', license_accepted=True, trusted_weights=True,
            files={name: checksum(raw) for name, raw in assets.items()},
            settings=dict(protocol=PROTOCOL, interpreter='python-fixture', worker='worker.py',
                          models_dir='models', model='small', languages=['uk', 'en'],
                          devices=['cpu'], compute_types=['int8'], tasks=['transcribe', 'translate']))
        manifest_path = root / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        library = tmp_path / ('library-' + mode)
        library.mkdir()
        source = tmp_path / ('Джерело ü ' + mode + '.mp3')
        source.write_bytes(b'Synthetic retained input; no audio decoding is performed.')
        settings = {**DEFAULT_SETTINGS, **(settings or {})}
        return service.TranscriptionService(library), manifest_path, source, settings
    return make


@pytest.fixture
def real_protocol_child(monkeypatch):
    reviewed = service.review_runtime
    calls = []

    def selected(*args, **kwargs):
        value = reviewed(*args, **kwargs)
        calls.append(value.manifest.manifest_sha256)
        # Only this test substitution enables our harmless synthetic protocol
        # script. Production always executes its manifest-listed interpreter.
        return replace(value, interpreter=Path(sys.executable))

    monkeypatch.setattr(service, 'review_runtime', selected)
    return calls


def test_complete_manifest_accepts_only_explicit_reviewed_local_assets(prepared_environment):
    _, manifest, _, settings = prepared_environment()
    runtime = service.review_runtime(manifest, settings)
    assert runtime.manifest.engine == 'supplied-whisper'
    inventory = {name[len('models/small/'):]: digest for name, digest in runtime.manifest.files
                 if name.startswith('models/small/')}
    assert runtime.model_sha256 == checksum(canonical(inventory).encode())
    assert runtime.interpreter.name == 'python-fixture'
    assert runtime.worker.name == 'worker.py'


@pytest.mark.parametrize('change', ['license', 'trust', 'model_bytes', 'extra_file', 'language', 'device', 'protocol'])
def test_runtime_admission_fails_before_any_engine_launch(prepared_environment, change, monkeypatch):
    _, path, _, settings = prepared_environment()
    raw = json.loads(path.read_text())
    if change == 'license':
        raw['license_accepted'] = False
    elif change == 'trust':
        raw['trusted_weights'] = False
    elif change == 'model_bytes':
        (path.parent / 'models/small/model.bin').write_bytes(b'OTHER CHECKPOINT')
    elif change == 'extra_file':
        (path.parent / 'undeclared.py').write_text('raise RuntimeError')
    elif change == 'language':
        settings['language'] = 'pl'
    elif change == 'device':
        settings['device'] = 'cuda'
    elif change == 'protocol':
        raw['settings']['protocol'] = 'legacy-worker'
    path.write_text(json.dumps(raw), encoding='utf-8')
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *a, **k: pytest.fail('Runtime admission must precede execution.'))
    with pytest.raises(ValueError):
        service.review_runtime(path, settings)


def test_pending_job_owns_inputs_and_preserves_originals(prepared_environment):
    app, manifest, source, settings = prepared_environment()
    original = source.read_bytes()
    identity = app.prepare([source], manifest, settings, title='Архів ü')
    job = app._job(identity)
    value = read_definition(job)
    retained = job / value['inputs'][0]['path']
    assert retained.read_bytes() == original
    assert retained.stat().st_ino != source.stat().st_ino
    assert value['source_provenance'][0]['path'] == str(source)
    assert value['source_provenance'][0]['sha256'] == checksum(original)
    source.unlink()
    assert read_definition(job) == value
    assert app.jobs() == ({'id': identity, 'title': 'Архів ü', 'inputs': 1},)


def test_explicit_folder_selection_does_not_adopt_later_files(prepared_environment):
    app, manifest, source, settings = prepared_environment()
    folder = source.parent / 'folder'
    folder.mkdir()
    selected = folder / 'chosen.MP3'
    source.rename(selected)
    (folder / 'ignored.txt').write_text('Not an audio input.')
    identity = app.prepare([folder], manifest, settings)
    (folder / 'later.wav').write_bytes(b'not selected')
    value = read_definition(app._job(identity))
    assert len(value['inputs']) == 1
    assert value['inputs'][0]['relative'] == 'chosen.MP3'


@pytest.mark.parametrize('case', ['overlap', 'empty', 'unsupported', 'cancel'])
def test_invalid_selection_does_not_publish_a_job(prepared_environment, case):
    app, manifest, source, settings = prepared_environment()
    sources = [source, source] if case == 'overlap' else [source]
    if case == 'empty':
        source.write_bytes(b'')
    if case == 'unsupported':
        changed = source.with_suffix('.txt')
        source.rename(changed)
        sources = [changed]
    with pytest.raises((ValueError, InterruptedError)):
        app.prepare(sources, manifest, settings, cancelled=lambda: case == 'cancel')
    assert app.jobs() == ()


def test_copy_failure_removes_only_owned_preparation(prepared_environment, monkeypatch):
    app, manifest, source, settings = prepared_environment()
    original, copy = source.read_bytes(), service.publish_new

    def fail_after_copy(*args, **kwargs):
        copy(*args, **kwargs)
        raise OSError('Synthetic stage failure')

    monkeypatch.setattr(service, 'publish_new', fail_after_copy)
    with pytest.raises(OSError):
        app.prepare([source], manifest, settings)
    assert source.read_bytes() == original
    assert app.jobs() == ()
    assert not list(app.root.glob('.prepare-*'))


def test_post_move_sync_failure_retains_recoverable_published_job(prepared_environment, monkeypatch):
    app, manifest, source, settings = prepared_environment()
    publish = service.publish_noreplace

    def moved_then_failed(stage, target):
        publish(stage, target)
        if target.parent == app.root and target.is_dir():
            raise OSError('Synthetic directory sync failure')

    monkeypatch.setattr(service, 'publish_noreplace', moved_then_failed)
    with pytest.raises(OSError):
        app.prepare([source], manifest, settings)
    rows = app.jobs()
    assert len(rows) == 1
    value = read_definition(app._job(rows[0]['id']))
    assert (app._job(rows[0]['id']) / value['inputs'][0]['path']).read_bytes() == source.read_bytes()


@pytest.mark.parametrize('output_mode,expected', [('separate', 8), ('one_per_folder', 7), ('one_whole_job', 7)])
def test_actual_child_exit_and_complete_receipt_accept_all_selected_formats(
        prepared_environment, real_protocol_child, output_mode, expected):
    settings = dict(output_mode=output_mode, output_srt=True, output_vtt=True, output_json=True)
    app, manifest, source, settings = prepared_environment(settings=settings)
    second = source.with_name('Другий ü.mp3')
    second.write_bytes(b'Second synthetic selected input.')
    identity = app.prepare([source, second], manifest, settings)
    original = source.read_bytes()
    result = app.run(identity, manifest, timeout=10)
    assert result['input_count'] == 2 and result['output_count'] == expected
    assert read_json(app._job(identity) / 'execution.json')['exit_code'] == 0
    assert source.read_bytes() == original
    assert real_protocol_child  # Both preparation and execution reviewed inventories.
    copied = app.reading_copy(identity, result['outputs'][0]['path'])
    assert copied.original_path and copied.current_text.startswith('Synthetic protocol text;')
    assert copied.source_location is None


@pytest.mark.parametrize('mode', ['zero_only', 'nonzero', 'tamper'])
def test_exit_zero_alone_or_partial_outputs_never_complete(prepared_environment, real_protocol_child, mode):
    app, manifest, source, settings = prepared_environment(mode)
    identity = app.prepare([source], manifest, settings)
    with pytest.raises((ValueError, RuntimeError, FileNotFoundError)):
        app.run(identity, manifest, timeout=10)
    state = read_json(app._job(identity) / 'execution.json')
    assert state['state'] == 'failed' and state['exit_code'] is None
    assert read_definition(app._job(identity))['inputs'][0]['sha256'] == checksum(source.read_bytes())
    with pytest.raises((ValueError, FileNotFoundError)):
        app.completed(identity)


@pytest.mark.parametrize('use_event', [False, True])
def test_real_child_stop_is_acknowledged_and_retains_restart_inputs(
        prepared_environment, real_protocol_child, use_event):
    app, manifest, source, settings = prepared_environment('wait')
    identity = app.prepare([source], manifest, settings)
    cancelled = threading.Event()
    requested = []

    def advanced(done, total, phase):
        if requested:
            return
        requested.append(True)
        if use_event:
            cancelled.set()
        else:
            app.control(identity, 'stop')

    started = time.monotonic()
    with pytest.raises(InterruptedError):
        app.run(identity, manifest, cancelled=cancelled.is_set, progress=advanced, timeout=10)
    assert time.monotonic() - started < 8
    assert requested
    assert read_json(app._job(identity) / 'execution.json')['state'] == 'cancelled'
    assert read_definition(app._job(identity))['inputs'][0]['sha256'] == checksum(source.read_bytes())


def test_observational_status_replacement_does_not_kill_healthy_worker(
        prepared_environment, real_protocol_child, monkeypatch):
    app, manifest, source, settings = prepared_environment('status_race')
    identity = app.prepare([source], manifest, settings)
    snapshot, races = service._read_json_snapshot, []

    def replaced(path, **kwargs):
        value, checksum = snapshot(path, **kwargs)
        if Path(path).name == 'job_status.json' and value.get('state') == 'processing':
            races.append(True)
            raise ValueError('Synthetic atomic status replacement during observation.')
        return value, checksum

    monkeypatch.setattr(service, '_read_json_snapshot', replaced)
    assert app.run(identity, manifest, timeout=10)['output_count'] == 1
    assert races


def test_restart_archives_prior_bindings_and_live_settings(prepared_environment, real_protocol_child):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    first = app.run(identity, manifest, timeout=10)
    job = app._job(identity)
    old_definition = (job / 'job.json').read_bytes()
    old_receipt = (job / 'logs/completion-v1.json').read_bytes()
    atomic_json(job / 'settings_live.json', {'language': 'ru'})
    second = app.run(identity, manifest, timeout=10)
    assert first['run_id'] != second['run_id']
    history = list((job / 'history').iterdir())
    assert len(history) == 1
    assert (history[0] / 'completion-v1.json').read_bytes() == old_receipt
    assert read_json(history[0] / 'settings_live.json') == {'language': 'ru'}
    assert not (job / 'settings_live.json').exists()
    assert (job / 'job.json').read_bytes() == old_definition


def test_changed_input_fails_before_restart(prepared_environment, real_protocol_child, monkeypatch):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    job = app._job(identity)
    definition = read_definition(job)
    (job / definition['inputs'][0]['path']).write_bytes(b'changed retained input')
    monkeypatch.setattr(service.subprocess, 'Popen', lambda *a, **k: pytest.fail('Changed inputs must not launch.'))
    with pytest.raises(ValueError):
        app.run(identity, manifest, timeout=10)


def test_existing_project_admission_prevents_second_supervisor(prepared_environment, real_protocol_child):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    with project_lock(app.root):
        with pytest.raises(RuntimeError):
            app.run(identity, manifest, timeout=10)
    assert not (app._job(identity) / 'job_config.json').exists()


def test_saved_config_must_remain_bound_to_immutable_settings(prepared_environment, real_protocol_child):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    app.run(identity, manifest, timeout=10)
    path = app._job(identity) / 'job_config.json'
    config = read_json(path)
    config['settings']['language'] = 'ru'
    atomic_json(path, config)
    with pytest.raises(ValueError):
        app.completed(identity)


@pytest.mark.parametrize('field,value', [
    ('path', '../outside.mp3'), ('root', 'inputs/../outside'), ('bytes', True), ('sha256', 'a' * 63),
])
def test_portable_definition_rejects_invalid_owned_bindings(prepared_environment, field, value):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    definition = read_definition(app._job(identity))
    definition['inputs'][0][field] = value
    with pytest.raises(ValueError):
        validate_definition(definition)


def test_runtime_removed_after_accepted_execution_does_not_destroy_saved_transcript(
        prepared_environment, real_protocol_child):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    result = app.run(identity, manifest, timeout=10)
    manifest.unlink()
    copied = app.reading_copy(identity, result['outputs'][0]['path'])
    assert copied.current_text.startswith('Synthetic protocol text;')
    with pytest.raises((ValueError, FileNotFoundError)):
        app.run(identity, manifest, timeout=10)


def test_restored_owned_inputs_support_fresh_execution_with_historical_bindings(
        prepared_environment, real_protocol_child, tmp_path):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    first = app.run(identity, manifest, timeout=10)
    source.unlink()
    restored_root = tmp_path / 'restored library ü'
    shutil.copytree(app.library_root, restored_root)
    restored = service.TranscriptionService(restored_root)
    second = restored.run(identity, manifest, timeout=10)
    assert first['run_id'] != second['run_id']
    assert second['job_root'] == str(restored._job(identity))
    assert read_definition(restored._job(identity))['source_provenance'][0]['path'] == str(source)
    assert list((restored._job(identity) / 'history').iterdir())


def test_preparation_reserves_worker_capture_space_before_copy(prepared_environment, monkeypatch):
    app, manifest, source, settings = prepared_environment()
    size = source.stat().st_size
    real = service.shutil.disk_usage(app.library_root)
    # Enough for the retained original, but not for the worker's independent
    # immutable inference copy and the same metadata/output reserve.
    monkeypatch.setattr(service.shutil, 'disk_usage',
                        lambda path: real._replace(free=size + 32 * 1024**2))
    with pytest.raises(OSError):
        app.prepare([source], manifest, settings)
    assert app.jobs() == ()
    assert source.exists()


def test_restored_completed_transcript_is_readable_without_old_runtime(
        prepared_environment, real_protocol_child, tmp_path):
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    result = app.run(identity, manifest, timeout=10)
    destination = tmp_path / 'restored readable ü'
    shutil.copytree(app.library_root, destination)
    shutil.rmtree(app.library_root)
    shutil.rmtree(manifest.parent)
    source.unlink()
    restored = service.TranscriptionService(destination)
    accepted = restored.completed(identity)
    assert accepted['historical'] and not accepted['runtime_current_verified']
    copied = restored.reading_copy(identity, result['outputs'][0]['path'])
    assert copied.original_path.startswith(str(destination))
    assert copied.current_text.startswith('Synthetic protocol text;')


def test_progress_observer_failure_does_not_revoke_a_verified_execution(prepared_environment, real_protocol_child):
    app, manifest, source, settings = prepared_environment('status_race')
    identity = app.prepare([source], manifest, settings)
    observed = []
    def broken_observer(*values):
        observed.append(values)
        raise RuntimeError('Synthetic display callback failed.')
    result = app.run(identity, manifest, progress=broken_observer, timeout=10)
    assert observed and result['output_count'] == 1
    assert app.completed(identity)['completion_sha256'] == result['completion_sha256']


def test_parallel_preparation_reserves_each_simultaneous_worker_capture(prepared_environment, monkeypatch):
    from types import SimpleNamespace
    app, manifest, source, settings = prepared_environment(settings={'parallel_files': 2})
    other = source.with_name('Second retained source.mp3')
    other.write_bytes(b'Second synthetic retained input with a different byte size.')
    sizes = [source.stat().st_size, other.stat().st_size]
    # The earlier single-largest-input reserve is insufficient for two workers.
    monkeypatch.setattr(service.shutil, 'disk_usage',
        lambda path: SimpleNamespace(free=sum(sizes) + max(sizes) + 32 * 1024**2))
    with pytest.raises(OSError, match='space'):
        app.prepare([source, other], manifest, settings)
    assert not tuple(app.root.glob('*/job.json'))


def test_stop_during_resource_wait_starts_no_child_and_keeps_owned_inputs(
        prepared_environment, real_protocol_child, monkeypatch):
    from contextlib import contextmanager
    from carr_library.operations import ResourceBroker, ResourceRequest
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    original = (app._job(identity) / 'job.json').read_bytes()
    broker = ResourceBroker.for_app(app.library_root)
    app._broker = broker
    entered = threading.Event()
    errors = []
    reserve = broker.reserve
    @contextmanager
    def observed(request, **options):
        if options['operation_id'] == 'asr:' + identity:
            entered.set()
        with reserve(request, **options) as lease:
            yield lease
    monkeypatch.setattr(broker, 'reserve', observed)
    monkeypatch.setattr(service.subprocess, 'Popen',
        lambda *args, **kwargs: pytest.fail('A stopped waiting job launched a child.'))
    def run():
        try:
            app.run(identity, manifest, timeout=10)
        except BaseException as exc:
            errors.append(exc)
    with reserve(ResourceRequest(cpu_slots=broker.limits.cpu_slots), operation_id='other', timeout=0):
        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(3)
        app.control(identity, 'stop')
        worker.join(3)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
    assert broker.snapshot()['leases'] == []
    assert (app._job(identity) / 'job.json').read_bytes() == original
    assert read_definition(app._job(identity))['inputs'][0]['sha256'] == checksum(source.read_bytes())


def test_resources_are_released_after_success_or_worker_failure(prepared_environment, real_protocol_child):
    from carr_library.operations import ResourceBroker
    for mode in ('success', 'nonzero'):
        app, manifest, source, settings = prepared_environment(mode)
        identity = app.prepare([source], manifest, settings)
        try:
            app.run(identity, manifest, timeout=10)
        except RuntimeError:
            assert mode == 'nonzero'
        assert ResourceBroker.for_app(app.library_root).snapshot()['leases'] == []

def test_standalone_worker_lock_preserves_live_controls_and_execution_before_admission(
        prepared_environment, tmp_path, monkeypatch):
    import subprocess
    app, manifest, source, settings = prepared_environment()
    identity = app.prepare([source], manifest, settings)
    job = app._job(identity)
    logs = job / 'logs'
    logs.mkdir(exist_ok=True)
    (job / 'pause.flag').write_bytes(b'')
    (job / 'stop.flag').write_bytes(b'')
    (job / 'job_config.json').write_bytes(b'{"existing":"live worker"}')
    (job / 'job_status.json').write_bytes(b'{"existing":"running"}')
    protected = ('job.json', 'job_config.json', 'job_status.json', 'execution.json', 'pause.flag', 'stop.flag')
    before = {name: (job / name).read_bytes() for name in protected if (job / name).exists()}
    child = (
        "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
        "from carr_library.audio.jobs import project_lock;"
        "guard=project_lock(Path(sys.argv[2]),lock_name='.worker-v5.lock');"
        "guard.__enter__();print('READY',flush=True);sys.stdin.buffer.read(1);guard.__exit__(None,None,None)"
    )
    holder = subprocess.Popen([sys.executable, '-I', '-B', '-c', child,
        str(Path(service.__file__).resolve().parents[1]), str(logs)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ready, observed = threading.Event(), []
        def announce():
            observed.append(holder.stdout.readline())
            ready.set()
        threading.Thread(target=announce, daemon=True).start()
        assert ready.wait(5) and observed == [b'READY\n']
        monkeypatch.setattr(service.subprocess, 'Popen',
                            lambda *a, **k: pytest.fail('The live worker lock allowed a second launch.'))
        with pytest.raises(RuntimeError, match='already using'):
            app.run(identity, manifest, timeout=10)
        assert before == {name: (job / name).read_bytes() for name in before}
        assert not (job / 'history').exists()
        assert not (app.library_root / 'state' / 'operations').exists()
    finally:
        try:
            holder.communicate(input=b'x', timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.communicate(timeout=5)
