"""Real subprocess protocol checks, not acoustic recognition qualification.

The deliberately synthetic worker hashes below are injected only in tests. No
private worker, private source audio or downloaded model is part of this suite.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

import pytest

import carr_library.transcription as module
from carr_library.transcription import EngineSpec, TranscriptionService, TranscriptionSettings
from carr_library.durable_state import atomic_json, read_json


FIXTURE_WORKER = r'''
import hashlib, json, os, pathlib, subprocess, sys, time, uuid
MODE = __MODE__
root = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
config = json.loads((root / 'job_config.json').read_text())
run_id = uuid.uuid4().hex
results = pathlib.Path(config['results_root'])
results.mkdir(parents=True, exist_ok=True)
protocol = 'carr-asr-v43-hardened-1'
def write(name, value):
    path = root / name
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(value), encoding='utf-8')
    os.replace(pending, path)
def status(phase, done=0, state=None):
    write('job_status.json', {'schema_version':1,'protocol':protocol,'job_id':config['job_id'],
      'run_id':run_id,'phase':phase,'result_state':state,'done':done})
status('starting')
if MODE == 'flood':
    sys.stdout.write('x' * (3 * 1024 * 1024)); sys.stdout.flush(); time.sleep(30)
if MODE in {'unresponsive','cooperative','pause'}:
    child = None
    if MODE == 'unresponsive':
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(90)'])
        write('test_child.json', {'pid':child.pid})
    while MODE == 'unresponsive' or not (root / 'stop.flag').exists():
        status('paused' if (root / 'pause.flag').exists() else 'recognizing')
        time.sleep(.02)
    value={'schema_version':1,'protocol':protocol,'job_id':config['job_id'],'run_id':run_id,
      'state':'cancelled','done':0,'failed':0,'total':len(config['files']),
      'effective_parallel_files':1,'results_dir':str(results),'files':[],'artifacts':[],'errors':[]}
    write('job_result.json', value); status('terminal',state='cancelled'); sys.exit(3)
records=[]
for index, name in enumerate(config['files']):
    path=pathlib.Path(name); raw=path.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
    source={'path':str(path),'sha256':digest,'bytes':len(raw)}
    artifacts=[]
    for fmt in ('txt','srt','vtt','json'):
        if not config['settings']['output_'+fmt] or MODE == 'missing-format' and fmt=='json': continue
        body=('Synthetic transcript '+str(index)+'\n').encode()
        if fmt=='srt': body=b'1\n00:00:00,000 --> 00:00:01,000\nSynthetic fixture\n'
        if fmt=='vtt': body=b'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSynthetic fixture\n'
        if fmt=='json': body=json.dumps({'text':'Synthetic fixture','segments':[]}).encode()
        name='output-'+str(index)+'.'+fmt
        (results/name).write_bytes(body)
        artifacts.append({'path':name,'format':fmt,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    records.append({'source':source,'cache_key':hashlib.sha256(str(index).encode()).hexdigest(),
       'state':'succeeded','error':None,'warnings':['Synthetic fixture; no acoustic quality claim'],
       'artifacts':artifacts})
artifacts=[item for row in records for item in row['artifacts']]
value={'schema_version':1,'protocol':protocol,'job_id':config['job_id'],'run_id':run_id,
    'state':'succeeded','done':len(records),'failed':0,'total':len(config['files']),
    'effective_parallel_files':1,'results_dir':str(results),'files':records,'artifacts':artifacts,'errors':[]}
if MODE=='wrong-job': value['job_id']='0'*32
if MODE=='stale-run': value['run_id']='0'*32
if MODE=='wrong-count': value['done']=0
if MODE=='source-hash': records[0]['source']['sha256']='0'*64
if MODE=='source-path': records[0]['source']['path']=str(root.parent/'outside.wav')
if MODE=='source-duplicate': records.append(records[0]); value['total']+=1
if MODE=='artifact-hash': artifacts[0]['sha256']='0'*64
if MODE=='artifact-size': artifacts[0]['bytes']+=1
if MODE=='artifact-escape': artifacts[0]['path']='../outside.txt'
if MODE=='artifact-absolute': artifacts[0]['path']=str(results/artifacts[0]['path'])
if MODE=='artifact-duplicate': artifacts.append(dict(artifacts[0]))
if MODE=='artifact-unlisted': value['artifacts']=artifacts[1:]
if MODE=='artifact-case': artifacts.append({**artifacts[0],'path':artifacts[0]['path'].upper()})
if MODE=='artifact-link':
    target=results/artifacts[0]['path']; target.unlink(); target.symlink_to(pathlib.Path(config['files'][0]))
if MODE=='input-changed':
    target=pathlib.Path(config['files'][0]); target.chmod(0o600); target.write_bytes(b'changed')
if MODE=='missing-result': sys.exit(0)
if MODE=='wrong-exit':
    write('job_result.json',value); status('terminal',len(records),'succeeded'); sys.exit(2)
write('job_result.json',value)
status('terminal',len(records),'succeeded')
'''


@pytest.fixture
def service(tmp_path):
    return TranscriptionService(tmp_path / 'state' / 'transcription')


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'Оригінал.wav'
    path.write_bytes(b'RIFF synthetic audio fixture, not private or an acoustic test')
    return path


@pytest.fixture
def engine(tmp_path, monkeypatch):
    def make(mode='good'):
        worker = tmp_path / ('synthetic-' + mode + '.py')
        worker.write_text(FIXTURE_WORKER.replace('__MODE__', repr(mode)), encoding='utf-8')
        digest = sha256(worker.read_bytes()).hexdigest()
        monkeypatch.setattr(module, 'REVIEWED_WORKER_SHA256', module.REVIEWED_WORKER_SHA256 | {digest})
        models = tmp_path / 'synthetic-models'
        (models / 'tiny.en').mkdir(parents=True, exist_ok=True)
        (models / 'tiny.en' / 'model.bin').write_bytes(b'SYNTHETIC - no inference weights')
        return EngineSpec(str(Path(sys.executable).resolve()), str(worker), digest, str(models))
    return make


def run(service, source, engine):
    identity = service.submit([source])
    return identity, service.run(identity, engine=engine)


def test_unknown_worker_never_executes(service, source, engine, monkeypatch):
    spec = engine()
    monkeypatch.setattr(module, 'REVIEWED_WORKER_SHA256', frozenset())
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('unknown worker executed'))
    identity = service.submit([source])
    with pytest.raises(ValueError, match='not been admitted'):
        service.run(identity, engine=spec)
    assert service.get(identity)['state'] == 'queued'


def test_changed_reviewed_worker_fails_before_exec(service, source, engine, monkeypatch):
    spec = engine()
    Path(spec.worker_path).write_text('raise Exception("different")')
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('changed worker executed'))
    with pytest.raises(ValueError, match='SHA-256'):
        service.run(service.submit([source]), engine=spec)


def test_capability_waits_for_explicit_engine_and_local_model(service, source, engine):
    assert service.capability()['state'] == 'waiting-input'
    spec = engine()
    service.configure_engine(spec)
    assert service.capability()['state'] == 'ready'
    (Path(spec.models_dir) / 'tiny.en' / 'model.bin').unlink()
    with pytest.raises(FileNotFoundError):
        service.run(service.submit([source]))


@pytest.mark.parametrize('change', [
    {'model':'../model'}, {'model':'/model'}, {'model':''}, {'language':'klingon'},
    {'language':'en_US'}, {'device':'tpu'}, {'compute_type':'garbage'}, {'task':'summarize'},
    {'translation_target':'uk'}, {'parallel_files':2}, {'parallel_files':True},
    {'beam_size':True}, {'beam_size':0}, {'beam_size':10**400}, {'output_mode':'append'},
    {'output_txt':'yes'}, {'vad_filter':1}, {'multilingual':None},
    {'output_txt':False, 'output_srt':False, 'output_vtt':False, 'output_json':False},
])
def test_invalid_settings_are_rejected(change):
    with pytest.raises(ValueError):
        TranscriptionSettings(**change)


def test_snapshot_preserves_original_and_uses_relative_content_identity(service, source):
    before = source.read_bytes()
    identity = service.submit([source])
    job = service.get(identity)
    managed = service.jobs / identity / job['inputs'][0]['path']
    assert managed.read_bytes() == before == source.read_bytes()
    assert str(source) not in json.dumps(job)
    assert job['inputs'][0]['sha256'] == sha256(before).hexdigest()
    assert managed.name.startswith(sha256(before).hexdigest())
    assert managed.stat().st_nlink == 1
    if os.name != 'nt':
        assert managed.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize('kind', ['same-path','same-content','symlink','hardlink','oversize','empty'])
def test_unsafe_or_duplicate_input_never_publishes_job(service, source, tmp_path, monkeypatch, kind):
    files = [source]
    if kind == 'same-path': files.append(source)
    if kind == 'same-content':
        copy = tmp_path / 'copy.wav'; copy.write_bytes(source.read_bytes()); files.append(copy)
    if kind in ('symlink','hardlink'):
        link = tmp_path / 'link.wav'
        if kind == 'symlink': link.symlink_to(source)
        else: os.link(source, link)
        files = [link]
    if kind == 'oversize': monkeypatch.setattr(module, 'MAX_INPUT_BYTES', 2)
    if kind == 'empty': source.write_bytes(b'')
    with pytest.raises((ValueError, OSError)):
        service.submit(files)
    assert service.list() == []


def test_cancelled_snapshot_leaves_no_published_job(service, source):
    with pytest.raises(InterruptedError):
        service.submit([source], cancelled=lambda: True)
    assert list(service.jobs.iterdir()) == []


def test_original_removed_after_submission_does_not_change_managed_run(service, source, engine):
    identity = service.submit([source])
    source.unlink()
    result = service.run(identity, engine=engine())
    assert result['state'] == 'succeeded'
    assert len(result['receipt']['artifacts']) == 4


def test_actual_process_receipts_progress_and_text_revision_export(service, source, engine, tmp_path):
    spec = engine()
    original = source.read_bytes()
    identity = service.submit([source])
    events = []
    result = service.run(identity, engine=spec, progress=lambda *args: events.append(args))
    assert result['state'] == 'succeeded'
    assert result['receipt']['done'] == result['receipt']['total'] == 1
    assert result['receipt']['files'][0]['warnings']
    assert events[-1][:2] == (1, 1)
    root = service.jobs / identity
    assert not list(root.glob('.pending-runner-*'))
    artifact = next(a for a in result['receipt']['artifacts'] if a['format'] == 'txt')
    raw = (root / artifact['path']).read_bytes()
    assert service.read_transcript(identity, artifact['path']) == raw.decode()
    revision = service.revise(identity, artifact['path'], 'Відредагована похідна версія.')
    target = service.export(identity, artifact['path'], tmp_path / 'new.txt', revision_id=revision['id'])
    assert target.read_text() == 'Відредагована похідна версія.'
    assert (root / artifact['path']).read_bytes() == raw
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        service.export(identity, artifact['path'], target)
    assert target.read_text() == 'Відредагована похідна версія.'
    assert str(root) not in json.dumps(result)
    assert str(Path(spec.worker_path)) not in json.dumps(result)
    assert service.get(identity)['state'] == 'succeeded'


@pytest.mark.parametrize('mode', ['wrong-job','stale-run','wrong-count','source-hash','source-path',
    'source-duplicate','artifact-hash','artifact-size','artifact-escape','artifact-absolute',
    'artifact-duplicate','artifact-unlisted','artifact-case','artifact-link','input-changed',
    'missing-result','missing-format','wrong-exit'])
def test_untrusted_terminal_claim_never_becomes_public_success(service, source, engine, mode):
    identity = service.submit([source])
    with pytest.raises((ValueError, RuntimeError, OSError)):
        service.run(identity, engine=engine(mode))
    job = service.get(identity, verify=False)
    assert job['state'] == 'failed'
    assert job['receipt'] is None


@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan'), 10**400])
def test_invalid_runtime_budget_never_launches(service, source, engine, monkeypatch, value):
    spec = engine()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid budget executed'))
    with pytest.raises(ValueError):
        service.run(service.submit([source]), engine=spec, timeout=value)


def test_diagnostic_flood_terminates_worker(service, source, engine):
    identity = service.submit([source])
    started = time.monotonic()
    with pytest.raises(ValueError, match='diagnostic byte budget'):
        service.run(identity, engine=engine('flood'), timeout=5)
    assert time.monotonic() - started < 5
    assert service.get(identity)['state'] == 'failed'


def test_cooperative_cancellation_is_terminal_and_keeps_inputs(service, source, engine):
    identity = service.submit([source])
    started = time.monotonic()
    result = service.run(identity, engine=engine('cooperative'),
        cancelled=lambda: time.monotonic() - started > .12, stop_grace=.5)
    assert result['state'] == 'cancelled'
    assert result['receipt'] is None  # cancelled source verification never admits a partial receipt
    assert read_json(service.jobs / identity / 'job_result.json')['state'] == 'cancelled'
    assert (service.jobs / identity / result['inputs'][0]['path']).is_file()


def test_unresponsive_worker_and_owned_child_are_stopped(service, source, engine):
    identity = service.submit([source])
    started = time.monotonic()
    result = service.run(identity, engine=engine('unresponsive'),
        cancelled=lambda: time.monotonic() - started > .2, stop_grace=.15)
    assert result['state'] == 'cancelled'
    assert time.monotonic() - started < 5
    child = read_json(service.jobs / identity / 'test_child.json')['pid']
    if os.name != 'nt':
        process_state = Path('/proc') / str(child) / 'stat'
        assert not process_state.exists() or process_state.read_text().split()[2] == 'Z'
    assert not list((service.jobs / identity).glob('.pending-runner-*'))


def test_pause_uses_worker_flag_and_shared_cancellation(service, source, engine):
    identity = service.submit([source])
    seen = threading.Event()
    cancel = threading.Event()
    errors = []
    outcome = []
    spec = engine('pause')
    def worker():
        try:
            outcome.append(service.run(identity, engine=spec, cancelled=cancel.is_set,
                progress=lambda done, total, phase: seen.set() if phase == 'paused' else None))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        deadline = time.monotonic() + 4
        while service.get(identity, verify=False)['state'] != 'running' and time.monotonic() < deadline:
            time.sleep(.02)
        service.set_paused(identity, True)
        assert seen.wait(4)
        service.set_paused(identity, False)
        assert not (service.jobs / identity / 'pause.flag').exists()
    finally:
        cancel.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert outcome[0]['state'] == 'cancelled'


def test_full_state_backup_restore_rebases_inputs_and_requires_explicit_engine(tmp_path, engine):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state' / 'transcription')
    source = tmp_path / 'source.wav'; source.write_bytes(b'portable synthetic audio')
    spec = engine()
    service.configure_engine(spec)
    identity, result = run(service, source, spec)
    digest = create_backup(app, tmp_path / 'backup')
    restored_root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    restored = TranscriptionService(restored_root / 'state' / 'transcription')
    assert restored.capability()['state'] == 'waiting-input'
    artifact = result['receipt']['artifacts'][0]
    assert restored.read_transcript(identity, artifact['path']) == service.read_transcript(identity, artifact['path'])
    # Explicit resume after an interrupted state regenerates absolute config.
    jobpath = restored.jobs / identity / 'job.json'
    job = read_json(jobpath); job['state']='cancelled'; atomic_json(jobpath,job,replace=True)
    restored.configure_engine(spec)
    source.unlink()
    assert restored.run(identity)['state'] == 'succeeded'
    config = read_json(restored.jobs / identity / 'job_config.json')
    assert all(Path(name).is_relative_to(restored_root) for name in config['files'])
    assert Path(config['results_root']).is_relative_to(restored_root)
    assert not str(app.root) in json.dumps(config)


def test_changed_completed_artifact_is_never_exported(service, source, engine, tmp_path):
    identity, result = run(service, source, engine())
    artifact = result['receipt']['artifacts'][0]
    (service.jobs / identity / artifact['path']).write_bytes(b'changed')
    with pytest.raises(ValueError):
        service.export(identity, artifact['path'], tmp_path / 'export.txt')
    assert not (tmp_path / 'export.txt').exists()


@pytest.mark.parametrize('settings', [False, {}, '', 0, []])
def test_falsey_settings_are_not_silently_replaced_by_defaults(service, source, settings):
    with pytest.raises(ValueError, match='validated transcription settings'):
        service.submit([source], settings)
    assert not service.list()


@pytest.mark.parametrize('spec', [False, {}, '', 0, []])
def test_falsey_engine_never_falls_back_to_saved_profile(service, source, engine, spec, monkeypatch):
    service.configure_engine(engine())
    identity = service.submit([source])
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('invalid engine executed'))
    with pytest.raises(ValueError, match='explicit reviewed engine'):
        service.run(identity, engine=spec)


def test_runtime_script_is_outside_library_even_during_backup(tmp_path, engine, monkeypatch):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state' / 'transcription')
    source = tmp_path / 'audio.wav'; source.write_bytes(b'synthetic fixture')
    identity = service.submit([source])
    spec = engine()
    original_popen = module.subprocess.Popen
    observed = []
    def capture(args, **kwargs):
        entrypoint = Path(args[3])
        assert not entrypoint.is_relative_to(app.root)
        assert sha256(entrypoint.read_bytes()).hexdigest() == spec.worker_sha256
        observed.append(entrypoint)
        # The snapshot is present and private when this backup executes.
        create_backup(app, tmp_path / 'while-launching')
        manifest = read_json(tmp_path / 'while-launching' / 'manifest.json')
        names = [entry['filename'] for entry in manifest['state']]
        assert any('/inputs/' in name for name in names)
        assert not any(name.endswith('.py') or 'runner' in name for name in names)
        return original_popen(args, **kwargs)
    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    assert service.run(identity, engine=spec)['state'] == 'succeeded'
    assert observed and not observed[0].exists()


def test_runtime_temp_inside_library_is_rejected_before_execution(service, source, engine, monkeypatch):
    spec = engine()
    monkeypatch.setattr(module.tempfile, 'tempdir', str(service.directory))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: pytest.fail('private runtime inside library executed'))
    with pytest.raises(ValueError, match='outside the backed-up library'):
        service.run(service.submit([source]), engine=spec)
    assert not list(service.directory.glob('carr-asr-reviewed-*'))


@pytest.mark.parametrize('damage', ['source', 'artifact-graph', 'count', 'state', 'duplicate', 'run-id', 'revision'])
def test_restored_saved_receipt_is_revalidated_as_a_complete_graph(service, source, engine, damage):
    identity, result = run(service, source, engine())
    path = service.jobs / identity / 'job.json'
    job = read_json(path)
    receipt = job['receipt']
    if damage == 'source': receipt['files'][0]['source']['sha256'] = '0' * 64
    if damage == 'artifact-graph': receipt['files'][0]['artifacts'] = []
    if damage == 'count': receipt['done'] = 0
    if damage == 'state': receipt['state'] = 'cancelled'
    if damage == 'duplicate': receipt['artifacts'].append(dict(receipt['artifacts'][0]))
    if damage == 'run-id': receipt['run_id'] = '../bad'
    if damage == 'revision': job['revisions'] = [{'id':'bad','path':'../outside.txt'}]
    atomic_json(path, job, replace=True)
    with pytest.raises(ValueError):
        service.get(identity)


def test_english_only_model_explains_language_scope():
    with pytest.raises(ValueError, match='English-only'):
        TranscriptionSettings(model='tiny.en', language='uk')
    assert TranscriptionSettings(model='tiny', language='uk').language == 'uk'


@pytest.mark.parametrize('during', ['final-source-verification', 'artifact-loop'])
def test_cancel_after_child_exit_preserves_outputs_without_admitting_completion(service, source, engine, monkeypatch, during):
    identity = service.submit([source])
    spec = engine()
    cancellation = threading.Event()
    observed = []
    if during == 'final-source-verification':
        original = service._verify_inputs
        calls = []
        def verify(root, job, cancelled=lambda: False):
            calls.append(root)
            if len(calls) == 2:
                cancellation.set()
            return original(root, job, cancelled)
        monkeypatch.setattr(service, '_verify_inputs', verify)
    else:
        original = service._artifact
        def verify(*args, **kwargs):
            value = original(*args, **kwargs)
            cancellation.set()
            return value
        monkeypatch.setattr(service, '_artifact', verify)
    result = service.run(identity, engine=spec, cancelled=cancellation.is_set,
                         progress=lambda *args: observed.append(args))
    assert result['state'] == 'cancelled' and result['receipt'] is None
    root = service.jobs / identity
    assert read_json(root / 'job_result.json')['state'] == 'succeeded'  # worker finished, parent verification was cancelled
    assert len(list((root / 'results').iterdir())) == 4
    assert (root / result['inputs'][0]['path']).read_bytes() == source.read_bytes()
    assert not any(phase == 'Verified every requested transcript output' for done, total, phase in observed)
    monkeypatch.undo()
    cancellation.clear()
    # Re-admit the explicitly synthetic fixture after undoing test patches.
    monkeypatch.setattr(module, 'REVIEWED_WORKER_SHA256', module.REVIEWED_WORKER_SHA256 | {spec.worker_sha256})
    assert service.run(identity, engine=spec)['state'] == 'succeeded'


def test_read_transcript_rechecks_the_bytes_actually_shown(service, source, engine, monkeypatch):
    identity, result = run(service, source, engine())
    item = result['receipt']['artifacts'][0]
    original = service._selected_artifact
    def selected(*args, **kwargs):
        receipt = original(*args, **kwargs)
        path = service.jobs / identity / receipt['path']
        path.write_bytes(b'x' * receipt['bytes'])
        return receipt
    monkeypatch.setattr(service, '_selected_artifact', selected)
    with pytest.raises(ValueError, match='before display'):
        service.read_transcript(identity, item['path'])


def test_saved_output_verification_and_reading_accept_shared_cancellation(service, source, engine):
    identity, result = run(service, source, engine())
    item = result['receipt']['artifacts'][0]
    with pytest.raises(InterruptedError):
        service.get(identity, cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        service.read_transcript(identity, item['path'], cancelled=lambda: True)
    assert service.get(identity)['state'] == 'succeeded'
