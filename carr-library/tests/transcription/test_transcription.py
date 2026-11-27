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
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import carr_library.transcription as module
from carr_library.transcription import EngineSpec, TranscriptionService, TranscriptionSettings
from carr_library.durable_state import atomic_json, read_json


FIXTURE_WORKER = r'''
import hashlib, json, os, pathlib, subprocess, sys, time, uuid
MODE = __MODE__
root = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
config = json.loads((root / 'job_config.json').read_text(encoding='utf-8'))
if MODE == 'utf8':
    assert sys.flags.isolated and sys.dont_write_bytecode and sys.flags.utf8_mode == 1
    assert config['job_name'] == 'Перевірка Україна — 日本語'
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
write('test_started.json', {'job_id':config['job_id'],'run_id':run_id})
if MODE == 'live-replacements':
    for update in range(50):
        status('recognizing')
        time.sleep(.005)
    write('test_progress_updates.json', {'count':50})
if MODE == 'flood':
    sys.stdout.write('x' * (3 * 1024 * 1024)); sys.stdout.flush(); time.sleep(30)
if MODE == 'early-failure':
    sys.stderr.write('Synthetic startup error: Unicode configuration accepted; worker failed deliberately.\n')
    sys.stderr.flush(); sys.exit(17)
if MODE in {'unresponsive','cooperative','pause'}:
    child = None
    if MODE == 'unresponsive':
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', 'import time; time.sleep(90)'])
        write('test_child.json', {'pid':child.pid})
    pause_seen, continued = False, False
    while MODE == 'unresponsive' or not (root / 'stop.flag').exists():
        paused = (root / 'pause.flag').exists()
        status('paused' if paused else 'recognizing')
        if MODE == 'pause' and paused and not pause_seen:
            write('test_pause_seen.json', {'job_id':config['job_id'],'run_id':run_id})
            pause_seen = True
        if MODE == 'pause' and pause_seen and not paused and not continued:
            write('test_continue_seen.json', {'job_id':config['job_id'],'run_id':run_id})
            continued = True
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
if MODE.startswith('terminal-status-'):
    final=json.loads((root/'job_status.json').read_text(encoding='utf-8'))
    if MODE=='terminal-status-job': final['job_id']='0'*32
    if MODE=='terminal-status-run': final['run_id']='0'*32
    if MODE=='terminal-status-phase': final['phase']='recognizing'
    if MODE=='terminal-status-state': final['result_state']='failed'
    write('job_status.json',final)
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


def patch_worker_popen(monkeypatch, replacement):
    # The stdlib object must remain a class: filelock lazily imports asyncio,
    # whose Windows implementation subclasses subprocess.Popen. Patch only the
    # service's module reference, never the shared subprocess module object.
    facade = SimpleNamespace(**vars(subprocess))
    facade.Popen = replacement
    monkeypatch.setattr(module, 'subprocess', facade)


def test_unknown_worker_never_executes(service, source, engine, monkeypatch):
    spec = engine()
    monkeypatch.setattr(module, 'REVIEWED_WORKER_SHA256', frozenset())
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('unknown worker executed'))
    assert isinstance(subprocess.Popen, type)
    identity = service.submit([source])
    with pytest.raises(ValueError, match='not been admitted'):
        service.run(identity, engine=spec)
    assert service.get(identity)['state'] == 'queued'


def test_changed_reviewed_worker_fails_before_exec(service, source, engine, monkeypatch):
    spec = engine()
    Path(spec.worker_path).write_text('raise Exception("different")', encoding='utf-8')
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('changed worker executed'))
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


def test_isolated_worker_explicit_utf8_preserves_unicode_library_and_title(tmp_path, source, engine, monkeypatch):
    service = TranscriptionService(tmp_path / 'Бібліотека 日本語' / 'state' / 'transcription')
    identity = service.submit([source], title='Перевірка Україна — 日本語')
    spec = engine('utf8')
    # A user's Python environment must neither opt this isolated protocol out
    # of UTF-8 nor add ambient imports. Validate the actual child flags too.
    monkeypatch.setenv('PYTHONUTF8', '0')
    actual_popen = subprocess.Popen
    calls = []
    def launch(args, **kwargs):
        assert args[1:5] == ['-I', '-B', '-X', 'utf8']
        calls.append(args)
        return actual_popen(args, **kwargs)
    patch_worker_popen(monkeypatch, launch)
    result = service.run(identity, engine=spec)
    assert calls and result['state'] == 'succeeded'
    config = read_json(service.jobs / identity / 'job_config.json')
    assert config['job_name'] == 'Перевірка Україна — 日本語'
    assert Path(config['files'][0]).is_relative_to(service.directory)


def test_live_progress_policy_matches_host():
    assert module._READ_LIVE_PROGRESS is (os.name != 'nt')


def test_nonpolling_run_never_opens_live_status_and_verifies_after_exit(
        service, source, engine, monkeypatch):
    # Exercise the Windows policy on every platform with a real, repeatedly
    # publishing worker. No timing luck or skipped Win32 assertion is involved.
    monkeypatch.setattr(module, '_READ_LIVE_PROGRESS', False)
    monkeypatch.setattr(module, '_read_progress',
        lambda path: pytest.fail('opened the worker live status destination'))
    owned, terminal_reads, events = [], [], []
    actual_popen, actual_read, actual_regular = subprocess.Popen, module.read_json, module.regular
    def launch(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        owned.append(process)
        return process
    def read(path):
        if Path(path).name == 'job_status.json':
            assert owned and owned[0].poll() is not None
            terminal_reads.append(Path(path))
        return actual_read(path)
    def regular(path, **kwargs):
        if Path(path).name == 'job_status.json':
            assert owned and owned[0].poll() is not None
        return actual_regular(path, **kwargs)
    patch_worker_popen(monkeypatch, launch)
    monkeypatch.setattr(module, 'read_json', read)
    monkeypatch.setattr(module, 'regular', regular)
    identity = service.submit([source])
    result = service.run(identity, engine=engine('live-replacements'),
                         progress=lambda *args: events.append(args))
    assert result['state'] == result['receipt']['state'] == 'succeeded'
    assert len(owned) == 1 and owned[0].returncode == 0 and len(terminal_reads) == 1
    assert read_json(service.jobs / identity / 'test_progress_updates.json') == {'count': 50}
    assert len(result['receipt']['artifacts']) == 4
    assert service.get(identity) == result
    assert any(phase == 'Running locally; progress verified after worker exit'
               for done, total, phase in events)
    assert all(done is None and total == 1 for done, total, phase in events[:-1])
    assert events[-1] == (1, 1, 'Verified every requested transcript output')


@pytest.mark.parametrize('mode', [
    'wrong-job', 'stale-run', 'missing-result', 'wrong-exit', 'missing-format',
    'artifact-hash', 'source-hash', 'terminal-status-job', 'terminal-status-run',
    'terminal-status-phase', 'terminal-status-state',
])
def test_nonpolling_run_preserves_strict_terminal_verification(
        service, source, engine, monkeypatch, mode):
    monkeypatch.setattr(module, '_READ_LIVE_PROGRESS', False)
    monkeypatch.setattr(module, '_read_progress',
        lambda path: pytest.fail('opened the worker live status destination'))
    identity = service.submit([source])
    with pytest.raises((ValueError, RuntimeError, OSError)):
        service.run(identity, engine=engine(mode))
    job = service.get(identity, verify=False)
    assert job['state'] == 'failed' and job['receipt'] is None


@pytest.mark.parametrize('raw', [b'{"done":0,"done":1}', b'{"done":NaN}'])
def test_live_progress_rejects_ambiguous_json(tmp_path, raw):
    path = tmp_path / 'job_status.json'; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module._read_progress(path)


def test_live_progress_remains_bounded_and_cannot_follow_links(tmp_path, monkeypatch):
    path = tmp_path / 'job_status.json'; path.write_bytes(b'{"done":0}')
    monkeypatch.setattr(module, 'MAX_DIAGNOSTIC_BYTES', 4)
    with pytest.raises(ValueError, match='bounded'):
        module._read_progress(path)
    link = tmp_path / 'linked.json'; os.link(path, link)
    with pytest.raises(ValueError, match='without links'):
        module._read_progress(link)


def test_early_child_failure_retains_decisive_private_diagnostic(service, source, engine):
    identity = service.submit([source])
    with pytest.raises(RuntimeError, match='exit 17.*Synthetic startup error'):
        service.run(identity, engine=engine('early-failure'))
    result = service.get(identity, verify=False)
    assert result['state'] == 'failed' and result['receipt'] is None
    assert 'Unicode configuration accepted; worker failed deliberately' in result['error']


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
    assert target.read_text(encoding='utf-8') == 'Відредагована похідна версія.'
    assert (root / artifact['path']).read_bytes() == raw
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        service.export(identity, artifact['path'], target)
    assert target.read_text(encoding='utf-8') == 'Відредагована похідна версія.'
    assert str(root) not in json.dumps(result)
    assert str(Path(spec.worker_path)) not in json.dumps(result)
    assert service.get(identity)['state'] == 'succeeded'


def test_final_progress_failure_cannot_rewrite_verified_completion(service, source, engine, monkeypatch):
    identity = service.submit([source])
    spec = engine()
    called = []
    def failed_observer(done, total, phase):
        if phase == 'Verified every requested transcript output':
            called.append((done, total))
            raise RuntimeError('Synthetic final progress observer failure')
    result = service.run(identity, engine=spec, progress=failed_observer)
    assert called == [(1, 1)]
    assert result['state'] == result['receipt']['state'] == 'succeeded'
    assert len(result['receipt']['artifacts']) == 4
    assert service.get(identity) == result  # Full source/output verification.
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('completed work was executed again'))
    assert service.run(identity, engine=spec) == result


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
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('invalid budget executed'))
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
                progress=lambda done, total, phase: seen.set()
                    if phase == ('paused' if module._READ_LIVE_PROGRESS
                                 else 'Pause requested; waiting for worker') else None))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        directory = service.jobs / identity
        deadline = time.monotonic() + 4
        while not (directory / 'test_started.json').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (directory / 'test_started.json').exists()
        service.set_paused(identity, True)
        assert seen.wait(4)
        # The synthetic worker publishes each acknowledgement exactly once.
        # Never read its replaceable live status to prove pause/continue.
        deadline = time.monotonic() + 4
        while not (directory / 'test_pause_seen.json').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (directory / 'test_pause_seen.json').exists()
        service.set_paused(identity, False)
        assert not (directory / 'pause.flag').exists()
        deadline = time.monotonic() + 4
        while not (directory / 'test_continue_seen.json').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (directory / 'test_continue_seen.json').exists()
    finally:
        cancel.set(); thread.join(5)
    assert not thread.is_alive() and not errors
    assert outcome[0]['state'] == 'cancelled'
    started = read_json(directory / 'test_started.json')
    paused = read_json(directory / 'test_pause_seen.json')
    continued = read_json(directory / 'test_continue_seen.json')
    terminal = read_json(directory / 'job_result.json')
    assert started == paused == continued == {'job_id': identity, 'run_id': terminal['run_id']}


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
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('invalid engine executed'))
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
        entrypoint = Path(args[args.index('--job-dir') - 1])
        assert not entrypoint.is_relative_to(app.root)
        assert sha256(entrypoint.read_bytes()).hexdigest() == spec.worker_sha256
        observed.append(entrypoint)
        # The private snapshot already exists while the public job lock is
        # held. Backup must reject this interval without opening worker files.
        with pytest.raises(RuntimeError, match='stop the active job'):
            create_backup(app, tmp_path / 'while-launching')
        assert not (tmp_path / 'while-launching').exists()
        return original_popen(args, **kwargs)
    patch_worker_popen(monkeypatch, capture)
    assert service.run(identity, engine=spec)['state'] == 'succeeded'
    assert observed and not observed[0].exists()
    create_backup(app, tmp_path / 'completed')
    manifest = read_json(tmp_path / 'completed' / 'manifest.json')
    names = [entry['filename'] for entry in manifest['state']]
    assert any('/inputs/' in name for name in names)
    assert not any(name.endswith('.py') or 'runner' in name for name in names)


def test_runtime_temp_inside_library_is_rejected_before_execution(service, source, engine, monkeypatch):
    spec = engine()
    monkeypatch.setattr(module.tempfile, 'tempdir', str(service.directory))
    patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('private runtime inside library executed'))
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
        validate_result = service._validate_result
        terminal_verification = threading.Event()
        def terminal(*args, **kwargs):
            terminal_verification.set()
            return validate_result(*args, **kwargs)
        def verify(root, job, cancelled=lambda: False):
            # Admission now rechecks inputs after waiting too. Bind this
            # cancellation to final receipt validation, not a call count.
            if terminal_verification.is_set():
                cancellation.set()
            return original(root, job, cancelled)
        monkeypatch.setattr(service, '_validate_result', terminal)
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


@pytest.mark.parametrize('paused', [False, True])
def test_active_worker_backup_rejects_before_reads_and_releases_prior_locks(
        tmp_path, source, engine, monkeypatch, paused):
    from carr_library.application import LibraryApp
    from carr_library.durable_state import state_lock
    from carr_library.library_backup import create_backup
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state' / 'transcription')
    idle, active = sorted([service.submit([source]), service.submit([source])])
    directory = service.jobs / active
    spec = engine('pause')
    monkeypatch.setattr(module, '_READ_LIVE_PROGRESS', False)
    stopped, errors, outcome = threading.Event(), [], []
    def run_owned():
        try:
            outcome.append(service.run(active, engine=spec, cancelled=stopped.is_set))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run_owned)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not (directory / 'test_started.json').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (directory / 'test_started.json').exists() and thread.is_alive()
        if paused:
            service.set_paused(active, True)
            deadline = time.monotonic() + 4
            while not (directory / 'test_pause_seen.json').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (directory / 'test_pause_seen.json').exists()
        original_open = Path.open
        def no_worker_read(path, mode='r', *args, **kwargs):
            if Path(path).is_relative_to(service.jobs) and 'r' in mode:
                pytest.fail('Backup opened job bytes before obtaining every run lock.')
            return original_open(path, mode, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', no_worker_read)
            with pytest.raises(RuntimeError, match='stop the active job.*pausing is not sufficient'):
                create_backup(app, tmp_path / 'blocked-backup')
        assert not (tmp_path / 'blocked-backup').exists()
        assert thread.is_alive() and not stopped.is_set() and not errors
        # The idle job sorts first; partial admission must release its run lock
        # and the catalogue lock while leaving the active worker untouched.
        with state_lock(service.directory, 'jobs'), state_lock(service.jobs / idle, 'transcription'):
            pass
    finally:
        stopped.set()
        thread.join(6)
    assert not thread.is_alive() and not errors and outcome[0]['state'] == 'cancelled'
    assert (directory / outcome[0]['inputs'][0]['path']).read_bytes() == source.read_bytes()
    create_backup(app, tmp_path / 'after-stop')
    assert (tmp_path / 'after-stop' / 'manifest.json').is_file()


@pytest.mark.parametrize('state', ['queued', 'interrupted', 'succeeded'])
def test_quiescent_transcription_backup_restores_same_job_and_explicit_rebind(
        tmp_path, source, engine, state):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    service = TranscriptionService(app.root / 'state' / 'transcription')
    spec = engine()
    service.configure_engine(spec)
    identity = service.submit([source])
    if state == 'succeeded':
        assert service.run(identity)['state'] == 'succeeded'
    elif state == 'interrupted':
        # Owner death can leave this value while the OS lock is free. It must
        # remain portable and resumable, not be treated as a live operation.
        job = service.get(identity, verify=False)
        job['state'] = 'running'
        atomic_json(service.jobs / identity / 'job.json', job, replace=True)
    before = service.get(identity, verify=False)
    digest = create_backup(app, tmp_path / 'backup')
    restored_root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    restored = TranscriptionService(restored_root / 'state' / 'transcription')
    assert restored.get(identity, verify=False) == before
    assert restored.capability()['state'] == 'waiting-input'
    source.unlink()
    restored.configure_engine(spec)
    result = restored.run(identity)
    assert result['id'] == identity and result['state'] == 'succeeded'
    assert len(result['receipt']['artifacts']) == 4
    assert restored.get(identity) == result
    if state != 'succeeded':
        config = read_json(restored.jobs / identity / 'job_config.json')
        assert all(Path(path).is_relative_to(restored_root) for path in config['files'])


def test_backup_holds_admission_and_run_locks_through_copy_and_final_hash(
        service, source, engine, tmp_path, monkeypatch):
    from carr_library.state_backup import snapshot_state
    identity = service.submit([source])
    spec = engine()
    original_open, observed, errors = Path.open, [], []
    def attempts():
        for operation in (lambda: service.submit([source]),
                          lambda: service.run(identity, engine=spec)):
            try:
                operation()
            except RuntimeError as exc:
                errors.append(str(exc))
            except BaseException as exc:
                errors.append(exc)
            else:
                errors.append('operation incorrectly admitted')
    def held_read(path, mode='r', *args, **kwargs):
        if Path(path) == service.jobs / identity / 'job.json' and 'r' in mode:
            observed.append(Path(path))
            thread = threading.Thread(target=attempts)
            thread.start(); thread.join(4)
            assert not thread.is_alive()
        return original_open(path, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', held_read)
        patch_worker_popen(patch, lambda *a, **k: pytest.fail('Backup allowed a worker launch.'))
        entries = snapshot_state(service.directory.parent, tmp_path / 'snapshot')
    assert len(observed) == 2  # Initial copy and the final source-byte rehash.
    assert len(errors) == 4 and all(isinstance(error, str) and 'already being changed' in error for error in errors)
    assert any(entry['filename'].endswith('/job.json') for entry in entries)
    assert len(service.list()) == 1 and service.get(identity)['state'] == 'queued'
    # Both locks have left their contexts after snapshot completion.
    assert service.run(identity, engine=spec)['state'] == 'succeeded'
    assert service.submit([source]) != identity


@pytest.mark.parametrize('existing_namespace', [False, True])
def test_late_transcription_namespace_or_job_is_rejected_before_inventory_reads(
        tmp_path, source, monkeypatch, existing_namespace):
    import carr_library.state_backup as backup
    root = tmp_path / 'state'; root.mkdir()
    if existing_namespace:
        service = TranscriptionService(root / 'transcription')
        service.submit([source])
    new_job = root / 'transcription' / 'jobs' / ('f' * 32)
    status = new_job / 'job_status.json'
    actual_walk, actual_regular, inserted = os.walk, backup.regular, []
    def arriving(*args, **kwargs):
        if not inserted:
            new_job.mkdir(parents=True)
            status.write_text('{"phase":"recognizing"}', encoding='utf-8')
            inserted.append(True)
        return actual_walk(*args, **kwargs)
    def no_unadmitted_file(path, **kwargs):
        if Path(path) == status:
            pytest.fail('Inventory inspected an unadmitted worker status file.')
        return actual_regular(path, **kwargs)
    facade = SimpleNamespace(**vars(os)); facade.walk = arriving
    monkeypatch.setattr(backup, 'os', facade)
    monkeypatch.setattr(backup, 'regular', no_unadmitted_file)
    with pytest.raises(RuntimeError, match='appeared or changed'):
        backup.snapshot_state(root, tmp_path / 'snapshot')
    assert inserted and not (tmp_path / 'snapshot').exists()


def test_transcription_backup_admission_is_bounded_and_cancellation_releases_locks(
        service, source, tmp_path, monkeypatch):
    from contextlib import contextmanager
    import carr_library.state_backup as backup
    from carr_library.durable_state import state_lock
    identities = [service.submit([source]), service.submit([source])]
    with monkeypatch.context() as patch:
        patch.setattr(module, 'MAX_JOBS', 1)
        with pytest.raises(ValueError, match='admission budget'):
            backup.snapshot_state(service.directory.parent, tmp_path / 'too-many')
    assert not any((service.jobs / identity / '.transcription.lock').exists() for identity in identities)
    stopped = threading.Event()
    @contextmanager
    def cancelling_lock(path, name='state'):
        with state_lock(path, name):
            if name == 'transcription':
                stopped.set()
            yield
    with monkeypatch.context() as patch:
        patch.setattr(backup, 'state_lock', cancelling_lock)
        with pytest.raises(InterruptedError, match='Backup cancelled'):
            backup.snapshot_state(service.directory.parent, tmp_path / 'cancelled', cancelled=stopped.is_set)
    with state_lock(service.directory, 'jobs'):
        for identity in identities:
            with state_lock(service.jobs / identity, 'transcription'):
                pass
    assert not (tmp_path / 'too-many').exists() and not (tmp_path / 'cancelled').exists()


def test_non_transcription_backup_does_not_create_a_transcription_namespace(tmp_path):
    from carr_library.state_backup import snapshot_state
    root = tmp_path / 'state'; root.mkdir()
    atomic_json(root / 'notes.json', {'text': 'Existing private application state'})
    entries = snapshot_state(root, tmp_path / 'snapshot')
    assert [entry['filename'] for entry in entries] == ['notes.json']
    assert not (root / 'transcription').exists()


def test_transcription_backup_rejects_link_before_target_classification_or_job_lock(
        service, tmp_path, monkeypatch):
    import carr_library.state_backup as backup
    outside = tmp_path / 'external'; outside.mkdir()
    (outside / 'job_status.json').write_text('{"private":"untouched"}', encoding='utf-8')
    link = service.jobs / ('f' * 32)
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    actual_is_dir, actual_lock = Path.is_dir, backup.state_lock
    locks = []
    def no_follow(path):
        if Path(path) == link:
            pytest.fail('Backup followed the linked job directory during admission.')
        return actual_is_dir(path)
    def no_job_lock(path, name='state'):
        locks.append((Path(path), name))
        assert Path(path) != link
        return actual_lock(path, name)
    monkeypatch.setattr(Path, 'is_dir', no_follow)
    monkeypatch.setattr(backup, 'state_lock', no_job_lock)
    with pytest.raises(ValueError, match='without links'):
        backup.snapshot_state(service.directory.parent, tmp_path / 'snapshot')
    assert locks == [(service.directory, 'jobs')]
    assert not (tmp_path / 'snapshot').exists()
    assert not (outside / '.transcription.lock').exists()
