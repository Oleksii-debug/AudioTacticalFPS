"""Actual subprocess/file/backup tests using an explicitly synthetic protocol worker.

No Whisper model, acoustic accuracy, diarization or multilingual quality is
claimed by these tests. The private supplied worker has a separate test pack.
"""
from hashlib import sha256
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json, atomic_json
from carr_library.library_backup import restore_backup
from carr_library.transcription import TranscriptionService, settings
from carr_library.transcription_host import require_complete


SYNTHETIC_WORKER = r'''
import hashlib, json, os, pathlib, socket, subprocess, sys, time, uuid
MODE = __MODE__
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
def record_unhandled(error_type, error, traceback):
    # Test-only diagnostics: the production runner intentionally hides stderr.
    current = globals().get('status', {})
    diagnostic = {'exception_type': error_type.__name__,
        'errno': getattr(error, 'errno', None), 'winerror': getattr(error, 'winerror', None),
        'message': str(error)[:2000],
        'worker_status': {key: current.get(key) for key in
            ('state', 'result_state', 'run_id', 'total', 'done', 'failed')}}
    try:
        (job / 'synthetic-worker-failure.json').write_text(json.dumps(diagnostic), encoding='utf-8')
    except OSError:
        pass
    sys.__excepthook__(error_type, error, traceback)
sys.excepthook = record_unhandled
cfg = json.loads((job / 'job_config.json').read_text(encoding='utf-8'))
options = cfg['settings']
sources = sorted(pathlib.Path(cfg['folders'][0]).iterdir())
output = pathlib.Path(cfg['results_root'])
receipts = job / 'receipts'
receipts.mkdir(exist_ok=True)
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def bind(value):
    value['sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return value
run_id = uuid.uuid4().hex
status = {'state': 'running', 'result_state': 'running', 'run_id': run_id,
          'total': len(sources), 'done': 0, 'failed': 0, 'receipts': []}
def _replace_published(source, destination):
    # Windows readers may briefly deny replacement of an otherwise valid file.
    deadline = time.monotonic() + .75
    for attempt in range(32):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            remaining = deadline - time.monotonic()
            if (os.name != 'nt' or getattr(error, 'winerror', None) != 5
                    or attempt == 31 or remaining <= 0):
                raise
            time.sleep(min(.025, remaining))
            if time.monotonic() >= deadline:
                raise
def publish():
    temp = job / 'status.tmp'
    temp.write_text(json.dumps(status), encoding='utf-8')
    _replace_published(temp, job / 'job_status.json')
publish()
if MODE == 'progress-wait':
    while not (job / 'progress-observed').exists():
        time.sleep(.02)
if MODE == 'network':
    socket.create_connection(('127.0.0.1', 9))
if MODE == 'outside-write':
    (pathlib.Path(cfg['models_dir']) / 'forbidden.txt').write_text('not allowed')
if MODE == 'child-command':
    subprocess.run([sys.executable, '-c', 'raise SystemExit(0)'], check=True)
if MODE == 'timeout':
    time.sleep(30)
if MODE == 'pause-once' and not (job / 'continued').exists():
    while not (job / 'stop.flag').exists():
        time.sleep(.02)
    (job / 'continued').write_text('completed files will be retained', encoding='utf-8')
    status.update(state='stopped', result_state='stopped')
    publish()
    raise SystemExit(2)
recognition_keys = ('task','language','model','model_revision','device','compute_type',
    'beam_size','parallel_files','vad_filter','multilingual','condition_on_previous_text',
    'word_timestamps','max_attempts')
export_keys = ('output_mode','output_txt','output_srt','output_vtt','output_json','include_headers','word_timestamps')
for index, source in enumerate(sources):
    if MODE == 'partial' and index:
        break
    model = bind({'selected': options['model'], 'requested_revision': options['model_revision'],
                  'resolved_revision': 'synthetic-only', 'files': []})
    runtime = bind({'executable': {'size_bytes': pathlib.Path(sys.executable).stat().st_size,
                                  'sha256': digest(pathlib.Path(sys.executable))},
                    'worker_sha256': digest(pathlib.Path(__file__)),
                    'scope': 'synthetic protocol fixture; no inference'})
    identity = bind({'version': 1,
        'source': {'path': os.path.normcase(str(source.resolve())), 'root': str(source.parent.resolve()),
                   'relative': source.name, 'group': source.parent.name,
                   'size_bytes': source.stat().st_size, 'sha256': digest(source)},
        'model': model, 'runtime': runtime,
        'recognition': {key: options[key] for key in recognition_keys},
        'execution': {'device': 'cpu', 'compute_type': 'int8', 'num_workers': 1}})
    formats = ['txt','srt','vtt','json']
    if options['word_timestamps']:
        formats += ['words.srt', 'words.vtt']
    outputs = []
    for kind in formats:
        if MODE == 'missing-output' and kind == formats[-1]:
            continue
        relative = pathlib.Path('input') / (source.stem + '.' + kind)
        target = output / relative
        target.parent.mkdir(exist_ok=True)
        value = ('Synthetic transcript. Український текст.\n' if kind == 'txt' else
                 json.dumps({'segments':[{'start':0,'end':1,'text':'Synthetic',
                     'words':[{'start':0,'end':1,'word':'Synthetic'}]}]}) if kind == 'json' else
                 'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSynthetic\n' if kind.endswith('vtt') else
                 '1\n00:00:00,000 --> 00:00:01,000\nSynthetic\n')
        target.write_text(value, encoding='utf-8', newline='\n')
        outputs.append({'path': relative.as_posix(), 'size_bytes': target.stat().st_size, 'sha256': digest(target)})
    metadata = next(row['sha256'] for row in outputs if row['path'].endswith('.json'))
    receipt = {'version':1, 'state':'complete', 'run_id':run_id, 'identity':identity,
        'requested_exports':{key:options[key] for key in export_keys},
        'detected_language':'en','language_probability':.9,'attempt':1,'outcome':'speech',
        'artifacts':outputs, 'result_metadata_sha256':metadata}
    if MODE == 'metadata-mismatch':
        receipt['result_metadata_sha256'] = '0' * 64
    if MODE == 'escape':
        receipt['artifacts'][0]['path'] = '../outside.txt'
    name = 'receipts/' + identity['sha256'] + '.result.json'
    (job / name).write_text(json.dumps(receipt), encoding='utf-8')
    status['receipts'].append(name)
    status['done'] += 1
    if MODE == 'cancel-after-first' and index == 0:
        publish()
        while not (job / 'stop.flag').exists():
            time.sleep(.02)
        status.update(state='stopped', result_state='stopped')
        publish()
        raise SystemExit(2)
status['failed'] = status['total'] - status['done']
status.update(state='finished', result_state='partial' if status['failed'] else 'completed')
publish()
raise SystemExit(1 if status['failed'] else 0)
'''


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic transcription library')
    service = app.transcription(workspace)
    models = tmp_path / 'models'
    models.mkdir()
    original = tmp_path / 'Лекція.wav'
    original.write_bytes(b'Synthetic media bytes; deliberately no acoustic inference.')
    def configure(mode='complete'):
        worker = tmp_path / ('worker-' + mode + '.py')
        worker.write_text(SYNTHETIC_WORKER.replace('__MODE__', repr(mode)), encoding='utf-8')
        service.configure(Path(sys.executable).resolve(), worker, models)
        return worker
    configure()
    return app, workspace, service, original, configure


def completed(prepared, *, options=None, files=None):
    app, workspace, service, original, configure = prepared
    identity = service.submit(files or [original], title='Synthetic title — Україна', options=options)
    result = service.run(identity, timeout=30)
    assert result['status'] == 'completed', result.get('last_error')
    return identity, result


def test_real_subprocess_preserves_original_and_exports_separate_edits(prepared, tmp_path):
    app, workspace, service, original, _ = prepared
    before = original.read_bytes()
    identity, result = completed(prepared)
    assert result['done'] == 1 and len(result['artifacts']) == 6
    source = result['sources'][0]['filename']
    recognized = service.text(identity, source)
    assert 'Український' in recognized
    revision = service.save_edit(identity, source, 'Human corrected reading copy.')
    assert service.text(identity, source) == recognized
    assert original.read_bytes() == before
    destination = service.export(identity, tmp_path / 'exported')
    manifest = read_json(destination / 'transcription-manifest.json')
    assert manifest['status'] == 'completed'
    assert len(manifest['artifacts']) == 6
    assert (destination / 'edited' / (revision + '.txt')).read_text() == 'Human corrected reading copy.'
    assert service.edits(identity)[0]['original_text_sha256'] == next(
        row['sha256'] for row in result['artifacts'] if row['format'] == 'txt')
    with pytest.raises(ValueError, match='new export folder'):
        service.export(identity, destination)


def test_duplicate_source_names_remain_separate(prepared, tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    source2 = other / prepared[3].name
    source2.write_bytes(b'A different synthetic source.')
    identity, result = completed(prepared, files=[prepared[3], source2])
    assert result['done'] == 2
    assert len({row['filename'] for row in result['sources']}) == 2
    assert len(result['receipts']) == 2
    assert len(result['artifacts']) == 12


def test_optional_word_timestamps_keep_four_base_formats(prepared):
    _, result = completed(prepared, options={'word_timestamps': False})
    assert {row['format'] for row in result['artifacts']} == {'txt', 'srt', 'vtt', 'json'}


@pytest.mark.parametrize('mode', ['network', 'outside-write', 'child-command'])
def test_actual_child_guards_fail_closed(prepared, mode):
    _, _, service, original, configure = prepared
    configure(mode)
    identity = service.submit([original])
    result = service.run(identity, timeout=30)
    assert result['status'] == 'failed'
    assert result['artifacts'] == []
    assert not (original.parent / 'models' / 'forbidden.txt').exists()


@pytest.mark.parametrize('mode', ['missing-output', 'metadata-mismatch', 'escape'])
def test_worker_success_alone_does_not_publish_incomplete_results(prepared, mode):
    _, _, service, original, configure = prepared
    configure(mode)
    identity = service.submit([original])
    result = service.run(identity, timeout=30)
    assert result['status'] == 'failed'
    assert result['artifacts'] == []
    assert result['last_error']


def test_partial_batch_is_saved_and_can_resume_with_reviewed_engine(prepared, tmp_path):
    _, _, service, original, configure = prepared
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Another synthetic recording.')
    configure('partial')
    identity = service.submit([original, second])
    partial = service.run(identity, timeout=30)
    assert partial['status'] == 'partial'
    assert partial['done'] == 1
    assert len(service.verify(identity)['artifacts']) == 6
    configure('complete')
    resumed = service.run(identity, timeout=30)
    assert resumed['status'] == 'completed' and resumed['done'] == 2


def test_stop_flag_cancellation_and_explicit_resume(prepared):
    _, _, service, original, configure = prepared
    configure('pause-once')
    identity = service.submit([original])
    started = time.monotonic()
    stopped = service.run(identity, timeout=30, cancelled=lambda: time.monotonic() - started > .5)
    assert stopped['status'] == 'stopped'
    assert service.get(identity)['sources'][0]['sha256'] == sha256(original.read_bytes()).hexdigest()
    resumed = service.run(identity, timeout=30)
    assert resumed['status'] == 'completed', resumed['last_error']


def test_actual_child_timeout_is_failed_and_original_remains(prepared):
    _, _, service, original, configure = prepared
    configure('timeout')
    identity = service.submit([original])
    result = service.run(identity, timeout=1)
    assert result['status'] == 'failed'
    assert 'time allowance' in result['last_error']
    assert original.exists()


def test_worker_change_requires_explicit_reconfiguration(prepared):
    _, _, service, original, configure = prepared
    worker = configure()
    identity = service.submit([original])
    worker.write_text(worker.read_text(encoding='utf-8') + '\n# changed after selection\n', encoding='utf-8')
    with pytest.raises(ValueError, match='runtime changed'):
        service.run(identity)
    assert service.get(identity)['status'] == 'ready'


def test_changed_retained_source_refuses_inference(prepared):
    _, _, service, original, _ = prepared
    identity = service.submit([original])
    job = service.get(identity)
    retained = service._path(identity) / 'input' / job['sources'][0]['filename']
    retained.write_bytes(b'x' * retained.stat().st_size)
    with pytest.raises(ValueError, match='original changed'):
        service.run(identity)


def test_changed_output_and_stale_receipt_are_rejected(prepared, tmp_path):
    identity, result = completed(prepared)
    service = prepared[2]
    status_path = service._path(identity) / 'job_status.json'
    status = read_json(status_path)
    old = status['run_id']
    status['run_id'] = 'a' * 32 if old != 'a' * 32 else 'b' * 32
    atomic_json(status_path, status, replace=True)
    with pytest.raises(ValueError, match='saved run'):
        service.verify(identity)
    status['run_id'] = old
    atomic_json(status_path, status, replace=True)
    artifact = service._path(identity) / 'results' / result['artifacts'][0]['path']
    artifact.write_bytes(b'x' * artifact.stat().st_size)
    with pytest.raises(ValueError, match='output is incomplete or changed'):
        service.export(identity, tmp_path / 'not-published')
    assert not (tmp_path / 'not-published').exists()


def test_restore_rebinds_retained_paths_without_modifying_recognition_receipts(prepared, tmp_path):
    app, workspace, service, original, _ = prepared
    identity, result = completed(prepared)
    source = result['sources'][0]['filename']
    revision = service.save_edit(identity, source, 'A retained user edit.')
    backup = tmp_path / 'backup'
    expected = app.backup(backup)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=expected)
    recovered = LibraryApp(restored).transcription(workspace)
    assert recovered.verify(identity)['done'] == 1
    assert recovered.edits(identity)[0]['id'] == revision
    assert recovered.text(identity, source) == service.text(identity, source)
    recovered.export(identity, tmp_path / 'restored-export')


def test_scope_archive_and_unknown_job_do_not_create_junk(prepared):
    app, workspace, service, original, _ = prepared
    identity = service.submit([original])
    missing = 'a' * 32
    if missing == identity:
        missing = 'b' * 32
    with pytest.raises(FileNotFoundError):
        service.run(missing)
    assert not service._path(missing).exists()
    other = app.create_workspace('Unrelated library')
    assert app.transcription(other).list() == ()
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        service.get(identity)


def test_cancelled_intake_publishes_no_partial_job(prepared):
    _, _, service, original, _ = prepared
    with pytest.raises(RuntimeError, match='cancelled'):
        service.submit([original], cancelled=lambda: True)
    assert service.list() == ()


@pytest.mark.parametrize('options', [
    {'model_revision': 'main'}, {'model': '../models'}, {'language': 'english'},
    {'beam_size': True}, {'parallel_files': 100}, {'output_json': False},
    {'output_mode': 'one_whole_job'}, {'task': 'translate'}, {'network': True},
], ids=['unpinned-revision','model-path','bad-language','bool-beam','unbounded-parallel',
        'missing-json','combined-export','translation-task','unknown-option'])
def test_invalid_settings_fail_before_job_creation(prepared, options):
    service, original = prepared[2:4]
    with pytest.raises(ValueError):
        service.submit([original], options=options)
    assert service.list() == ()


def test_cli_and_application_share_one_durable_job_service(prepared, tmp_path):
    app, workspace, service, original, _ = prepared
    import carr_library
    import bootstrap
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join((str(Path(carr_library.__file__).resolve().parents[1]),
                                      str(Path(bootstrap.__file__).resolve().parents[1])))
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    prefix = [sys.executable, '-B', '-s', '-P', '-X', 'utf8']
    origin = subprocess.run(prefix + ['-c', 'import carr_library,bootstrap,json,sys; '
        'print(json.dumps([carr_library.__file__,bootstrap.__file__,sys.dont_write_bytecode]))'],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, encoding='utf-8')
    paths = json.loads(origin.stdout)
    assert [Path(path).resolve() for path in paths[:2]] == [
        Path(carr_library.__file__).resolve(), Path(bootstrap.__file__).resolve()]
    assert paths[2] is True
    created = subprocess.run(prefix + ['-m', 'carr_library', 'transcribe',
        '--data-dir', str(app.root), '--workspace', workspace, 'create', str(original)],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, encoding='utf-8')
    identity = json.loads(created.stdout)['id']
    assert service.get(identity)['status'] == 'ready'
    completed_job = subprocess.run(prefix + ['-m', 'carr_library', 'transcribe',
        '--data-dir', str(app.root), '--workspace', workspace, 'run', identity, '--timeout', '30'],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf-8')
    assert completed_job.returncode == 0, {
        'stdout': completed_job.stdout, 'stderr': completed_job.stderr,
        'saved_job': service.get(identity),
    }
    assert service.verify(identity)['status'] == 'completed'


def test_stop_does_not_rehash_inputs_and_preserves_completed_worker_files(prepared, tmp_path, monkeypatch):
    _, _, service, original, configure = prepared
    configure('cancel-after-first')
    second = tmp_path / 'second-stop.wav'
    second.write_bytes(b'Another synthetic input.')
    identity = service.submit([original, second])
    path = service._path(identity)
    def must_not_rehash(*args, **kwargs):
        raise AssertionError('Stop must defer verification until explicit resume.')
    monkeypatch.setattr(service, '_receipts', must_not_rehash)
    def cancelled():
        status = path / 'job_status.json'
        return status.exists() and read_json(status).get('done') == 1
    stopped = service.run(identity, timeout=30, cancelled=cancelled)
    assert stopped['status'] == 'stopped'
    assert stopped['artifacts'] == [] and stopped['done'] == 0
    receipts = list((path / 'receipts').glob('*.result.json'))
    assert len(receipts) == 1
    assert len(list((path / 'results').rglob('*.*'))) == 6
    assert len(list((path / 'input').iterdir())) == 2
    monkeypatch.undo()
    configure('complete')
    assert service.run(identity, timeout=30)['status'] == 'completed'


def test_progress_observer_failure_during_actual_child_does_not_kill_recognition(prepared):
    _, _, service, original, configure = prepared
    configure('progress-wait')
    identity = service.submit([original])
    notices = []

    def broken(done, total, phase):
        notices.append((done, total, phase))
        (service._path(identity) / 'progress-observed').write_text('release synthetic worker')
        raise RuntimeError('Synthetic detached progress observer')

    result = service.run(identity, timeout=10, progress=broken)
    assert notices and len(notices) == 1
    assert result['status'] == service.get(identity)['status'] == 'completed'
    assert service.verify(identity)['done'] == 1
    assert len(result['artifacts']) == 6 and result['last_error'] == ''


def test_final_progress_observer_failure_cannot_reverse_durable_completed_result(prepared):
    _, _, service, original, _ = prepared
    identity = service.submit([original])
    final_notices = []

    def broken(done, total, phase):
        if phase == 'completed':
            assert service.get(identity)['status'] == 'completed'
            final_notices.append((done, total))
            raise RuntimeError('Synthetic detached completion observer')

    result = service.run(identity, timeout=10, progress=broken)
    assert final_notices == [(1, 1)]
    assert result == service.get(identity)
    assert result['status'] == 'completed' and result['last_error'] == ''
    assert service.verify(identity)['done'] == 1


def test_stopped_progress_observer_failure_preserves_actual_child_resume_inputs(prepared, tmp_path):
    _, _, service, original, configure = prepared
    configure('cancel-after-first')
    second = tmp_path / 'second-observer-stop.wav'
    second.write_bytes(b'Another synthetic stop fixture.')
    identity = service.submit([original, second])
    path = service._path(identity)
    notices = []

    def cancelled():
        status = path / 'job_status.json'
        return status.exists() and read_json(status).get('done') == 1

    def broken(done, total, phase):
        if phase == 'stopped':
            notices.append((done, total))
            raise RuntimeError('Synthetic detached stop observer')

    result = service.run(identity, timeout=10, cancelled=cancelled, progress=broken)
    if not notices:
        print('ASR_STOP_RESULT ' + json.dumps({key: result.get(key) for key in
            ('status', 'done', 'failed', 'last_error', 'worker_run_id')}, ensure_ascii=True), flush=True)
        for name in ('job_status.json', 'synthetic-worker-failure.json'):
            try:
                diagnostic = (path / name).read_text(encoding='utf-8')
            except OSError as exc:
                diagnostic = type(exc).__name__ + ': ' + str(exc)
            print('ASR_STOP_FILE ' + name + ' ' + diagnostic[:5000], flush=True)
    assert notices, result
    assert result == service.get(identity) and result['status'] == 'stopped'
    assert result['done'] == 0 and result['artifacts'] == []
    assert len(list((path / 'receipts').glob('*.result.json'))) == 1
    assert len(list((path / 'results').rglob('*.*'))) == 6
    configure('complete')
    assert service.run(identity, timeout=10)['status'] == 'completed'


@pytest.mark.parametrize('mode, error', [('missing-output', 'export is missing'),
                                        ('timeout', 'time allowance')])
def test_progress_observer_failure_preserves_real_child_or_receipt_failure(prepared, mode, error):
    _, _, service, original, configure = prepared
    configure(mode)
    identity = service.submit([original])

    def broken(done, total, phase):
        raise RuntimeError('Synthetic observer failure must not replace the engine error')

    result = service.run(identity, timeout=1, progress=broken)
    assert result == service.get(identity) and result['status'] == 'failed'
    assert error in result['last_error']
    assert 'observer' not in result['last_error'] and result['artifacts'] == []


def test_stop_keeps_worker_counts_monotonic_and_unverified_results_distinct(prepared, tmp_path):
    _, _, service, original, configure = prepared
    configure('cancel-after-first')
    second = tmp_path / 'second-progress-boundary.wav'
    second.write_bytes(b'Second purpose-written protocol input; no acoustic inference.')
    identity = service.submit([original, second])
    path = service._path(identity)
    calls = []

    def progress(completed, total, phase):
        calls.append((completed, total, phase))

    def cancelled():
        status = path / 'job_status.json'
        return (any(done == 1 for done, _, _ in calls) and status.exists()
                and read_json(status).get('done') == 1)

    result = service.run(identity, timeout=30, cancelled=cancelled, progress=progress)
    durable, worker = service.get(identity), read_json(path / 'job_status.json')
    assert result == durable and durable['status'] == 'stopped'
    assert durable['done'] == 0 and durable['receipts'] == [] and durable['artifacts'] == []
    assert worker['state'] == 'stopped' and worker['done'] == 1
    saved = list((path / 'receipts').glob('*.result.json'))
    assert len(saved) == 1 and read_json(saved[0])['run_id'] == worker['run_id']
    assert len(list((path / 'results').rglob('*.*'))) == 6
    assert len(list((path / 'input').iterdir())) == 2
    measured = [done for done, total, _ in calls]
    assert measured and max(measured) == 1 and all(total == 2 for _, total, _ in calls)
    assert measured == sorted(measured), f'Progress regressed: {calls!r}'
    assert calls[-1] == (1, 2, 'stopped')
    with pytest.raises(InterruptedError, match='stopped'):
        require_complete(result)


def test_partial_progress_requires_verified_resume_before_host_success(prepared, tmp_path):
    _, _, service, original, configure = prepared
    configure('partial')
    second = tmp_path / 'second-partial-progress.wav'
    second.write_bytes(b'Second synthetic retained input for partial and resume.')
    identity = service.submit([original, second])
    events = []

    def progress(completed, total, phase):
        events.append((completed, total, phase))

    partial = service.run(identity, timeout=30, progress=progress)
    assert partial['status'] == 'partial' and partial['done'] == 1
    assert events and all(done <= 1 and total == 2 for done, total, _ in events)
    with pytest.raises(RuntimeError):
        require_complete(partial)

    configure('complete')
    events.clear()
    completed = service.run(identity, timeout=30, progress=progress)
    assert completed['status'] == 'completed' and completed['done'] == 2
    assert events[-1] == (2, 2, 'completed')
    assert require_complete(completed) == completed



@pytest.fixture
def status_publisher():
    """Exercise the actual fixture helper without launching recognition."""
    tree = ast.parse(SYNTHETIC_WORKER.replace('__MODE__', repr('complete')))
    definition = next(node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == '_replace_published')
    clock = SimpleNamespace(now=0.0, sleeps=[])
    def pause(delay):
        clock.sleeps.append(delay)
        clock.now += delay
    operating_system = SimpleNamespace(name='nt', replace=os.replace)
    timer = SimpleNamespace(monotonic=lambda: clock.now, sleep=pause)
    namespace = {'os': operating_system, 'time': timer}
    exec(compile(ast.Module(body=[definition], type_ignores=[]),
                 '<synthetic-status-publisher>', 'exec'), namespace)
    return namespace['_replace_published'], operating_system, timer, clock


def _publication_error(winerror=5):
    error = PermissionError(13, 'Synthetic Windows publication conflict')
    if winerror is not None:
        error.winerror = winerror
    return error


def test_status_publication_retries_only_the_transient_windows_rename(status_publisher, tmp_path):
    replace, operating_system, _, clock = status_publisher
    source, destination = tmp_path / 'pending.json', tmp_path / 'status.json'
    source.write_bytes(b'new complete status')
    destination.write_bytes(b'old complete status')
    attempts = []
    def occupied(pending, published):
        attempts.append(clock.now)
        assert Path(pending).read_bytes() == b'new complete status'
        assert Path(published).read_bytes() == b'old complete status'
        if clock.now < .35:
            raise _publication_error()
        os.replace(pending, published)
    operating_system.replace = occupied
    replace(source, destination)
    assert len(attempts) > 1 and .35 <= clock.now < .75
    assert destination.read_bytes() == b'new complete status' and not source.exists()


@pytest.mark.parametrize('platform,winerror', [('posix', 5), ('nt', 32), ('nt', None)])
def test_status_publication_does_not_retry_unobserved_errors(status_publisher, tmp_path, platform, winerror):
    replace, operating_system, _, clock = status_publisher
    source, destination = tmp_path / 'pending.json', tmp_path / 'status.json'
    source.write_bytes(b'new status')
    destination.write_bytes(b'old status')
    error, attempts = _publication_error(winerror), []
    def denied(*args):
        attempts.append(args)
        raise error
    operating_system.name, operating_system.replace = platform, denied
    with pytest.raises(PermissionError) as caught:
        replace(source, destination)
    assert caught.value is error and len(attempts) == 1 and clock.sleeps == []
    assert destination.read_bytes() == b'old status' and source.read_bytes() == b'new status'


def test_status_publication_exhaustion_preserves_failure_and_old_bytes(status_publisher, tmp_path):
    replace, operating_system, _, clock = status_publisher
    source, destination = tmp_path / 'pending.json', tmp_path / 'status.json'
    source.write_bytes(b'new status')
    destination.write_bytes(b'old status')
    error, attempts = _publication_error(), []
    def occupied(*args):
        attempts.append(clock.now)
        raise error
    operating_system.replace = occupied
    with pytest.raises(PermissionError) as caught:
        replace(source, destination)
    assert caught.value is error and 1 < len(attempts) <= 32
    assert clock.now == pytest.approx(.75) and max(clock.sleeps) <= .025
    assert destination.read_bytes() == b'old status' and source.read_bytes() == b'new status'


def test_status_publication_has_a_separate_attempt_cap(status_publisher, tmp_path):
    replace, operating_system, timer, clock = status_publisher
    source, destination = tmp_path / 'pending.json', tmp_path / 'status.json'
    source.write_bytes(b'new status')
    destination.write_bytes(b'old status')
    error, attempts = _publication_error(), []
    def occupied(*args):
        attempts.append(args)
        raise error
    operating_system.replace = occupied
    timer.sleep = clock.sleeps.append
    with pytest.raises(PermissionError) as caught:
        replace(source, destination)
    assert caught.value is error and len(attempts) == 32 and len(clock.sleeps) == 31
    assert destination.read_bytes() == b'old status' and source.read_bytes() == b'new status'
