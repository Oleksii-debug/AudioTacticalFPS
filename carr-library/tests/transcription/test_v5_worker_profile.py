"""Independent V5 boundary checks with an explicitly synthetic child worker.

No private supplied source, model, recording, inference or acoustic qualification
is used here. The fixture implements only the documented file protocol.
"""
from pathlib import Path
from hashlib import sha256
import builtins
import io
import json
import os
import runpy
import subprocess
import sys
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup


PROTOCOL = 'whisper-v5-manifest-v1'
PROVENANCE = 'package_versions_only_not_a_qualified_runtime_pack'

SYNTHETIC_V5_WORKER = r'''
import copy, hashlib, json, os, pathlib, sys, time

job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
cfg = json.loads((job / 'job_config.json').read_text(encoding='utf-8'))
settings = cfg['settings']
mode_path = job / 'fixture-mode.json'
mode = json.loads(mode_path.read_text()) if mode_path.exists() else 'success'
result = pathlib.Path(cfg['results_root']) / 'Whisper_V5' / job.name
result.mkdir(parents=True, exist_ok=True)
sources = sorted(pathlib.Path(cfg['folders'][0]).iterdir())
history_path = job / 'fixture-runs.json'
history = json.loads(history_path.read_text()) if history_path.exists() else []
previous = history[-1] if history else None
history.append({'results_root':cfg['results_root'], 'results_dir':str(result)})
history_path.write_text(json.dumps(history), encoding='utf-8')
cache = job / 'logs' / 'cache_v5'
cache.mkdir(parents=True, exist_ok=True)
(cache / 'synthetic-retained.txt').write_text('Synthetic cache; no inference.', encoding='utf-8')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def bound(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode('utf-8')).hexdigest()

def identity(path):
    info = path.stat()
    return {'path':os.path.normcase(str(path.resolve())), 'size':info.st_size,
            'mtime_ns':info.st_mtime_ns, 'ctime_ns':info.st_ctime_ns,
            'device':info.st_dev, 'inode':info.st_ino, 'sha256':digest(path)}

def status(done, failed, directory=result, state='Обробка'):
    temporary = job / 'fixture-status.tmp'
    temporary.write_text(json.dumps({'state':state, 'done':done, 'total':len(sources),
        'failed':failed, 'results_dir':str(directory)}, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, job / 'job_status.json')

status(0, 0)
if mode == 'stale':
    old = pathlib.Path(previous['results_dir'])
    manifest = json.loads((old / 'result_manifest.json').read_text(encoding='utf-8'))
    status(len(manifest['records']), manifest['failed'], old, 'Старий результат')
    raise SystemExit(0)
if mode == 'no-manifest':
    status(0, len(sources), state='Завершено без маніфесту')
    raise SystemExit(0)
if mode == 'wait-before-results':
    (job / 'fixture-ready').write_text('Synthetic child ready.', encoding='utf-8')
    while not (job / 'fixture-release').exists():
        time.sleep(.02)

wrapper = {name:digest(pathlib.Path(__file__).parent / name)
           for name in ('worker.py','local_models.py','transcript_features.py')}
if mode == 'wrong-helper':
    wrapper['local_models.py'] = '0' * 64
model_path = pathlib.Path(settings['model_path'])
model = {'path':str(model_path.resolve()),
         'files':{path.relative_to(model_path).as_posix():identity(path)
                  for path in sorted(model_path.rglob('*')) if path.is_file()}}
if mode == 'wrong-model':
    model['files']['model.bin']['sha256'] = '0' * 64
backend = {'engine':'synthetic-no-inference', 'version':'fixture-1'}
provenance = 'package_versions_only_not_a_qualified_runtime_pack'
recognition_keys = ('task','language','multilingual','model','model_path','model_cache_dir',
                    'device','compute_type','beam_size','vad_filter','condition_on_previous_text')
records, artifacts = [], []
for index, source in enumerate(sources):
    if mode == 'partial' and index:
        break
    reported = copy.deepcopy(settings)
    if mode == 'wrong-settings':
        reported['language'] = 'sk' if reported['language'] != 'sk' else 'en'
    attempt = 'original'
    effective = copy.deepcopy(reported)
    if mode in ('retry', 'retry-last'):
        attempt = 'beam1_no_previous' if mode == 'retry' else 'beam1_no_vad'
        effective.update(beam_size=1, condition_on_previous_text=False)
        if mode == 'retry-last':
            effective['vad_filter'] = False
    if mode == 'unsupported-retry':
        attempt = 'made-up-retry'
    recognition = {key:effective[key] for key in recognition_keys}
    source_identity = identity(source)
    if mode == 'wrong-source':
        source_identity['sha256'] = '0' * 64
    cache_identity = {'schema':5, 'content_identity_version':1,
        'source':source_identity, 'recognition':{key:reported[key] for key in recognition_keys}, 'model':model,
        'backend':backend, 'wrapper_sources_sha256':wrapper,
        'runtime_provenance':provenance}
    fingerprint = bound(cache_identity)
    compute = effective['compute_type']
    if compute == 'auto':
        compute = 'int8' if effective['device'] == 'cpu' else 'float16'
    runtime = {'device':effective['device'], 'compute_type':compute,
               'parallel_files':effective['parallel_files']}
    text = 'Synthetic V5 fixture.'
    words = [{'start':0.0, 'end':0.3, 'word':'Synthetic'},
             {'start':0.3, 'end':0.6, 'word':'V5'},
             {'start':0.6, 'end':1.0, 'word':'fixture.'}]
    if mode == 'missing-words' or not effective['output_words']:
        words = []
    segments = [{'start':0.0, 'end':1.0, 'text':text, 'words':words}]
    if mode == 'invalid-timing':
        segments[0]['end'] = -1
    meta = {'cache_schema':5, 'complete':True, 'fingerprint':fingerprint,
        'cache_identity':cache_identity, 'source_identity':source_identity,
        'model_identity':model, 'requested_settings':reported, 'settings':effective,
        'attempt':attempt, 'model_runtime':runtime, 'selected_language':effective['language'],
        'multilingual':effective['multilingual'], 'task':effective['task'],
        'word_timestamps':effective['output_words'], 'segments':segments,
        'text':text, 'text_sha256':hashlib.sha256((text+'\n').encode()).hexdigest(), 'duration':1.0}
    meta['metadata_sha256'] = bound(meta)
    output = {
        'txt':text+'\n',
        'srt':'1\n00:00:00,000 --> 00:00:01,000\n'+text+'\n',
        'vtt':'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n'+text+'\n',
        'json':json.dumps(meta, ensure_ascii=False),
    }
    if effective['output_words']:
        output['words.txt']='0.000 --> 0.300 Synthetic\n0.300 --> 0.600 V5\n0.600 --> 1.000 fixture.\n'
    if mode == 'missing-output':
        del output['vtt']
    names=[]
    for suffix,content in output.items():
        name=f'source_{index+1:03d}.'+suffix
        path=result/name
        path.write_text(content, encoding='utf-8', newline='\n')
        names.append(name)
        artifacts.append({'path':name, 'bytes':path.stat().st_size, 'sha256':digest(path)})
    if mode == 'changed-output':
        (result/names[0]).write_text('Tampered synthetic output.', encoding='utf-8')
    record = {'source_identity':source_identity, 'model_identity':model, 'backend':backend,
        'model_runtime':runtime, 'fingerprint':fingerprint,
        'metadata_sha256':meta['metadata_sha256'], 'requested_settings':reported,
        'effective_settings':{**recognition, 'word_timestamps':effective['output_words']},
        'attempt':attempt, 'artifacts':names}
    records.append(record)
    status(len(records),0)
    if mode == 'stop-after-first':
        while not (job/'stop.flag').exists():
            time.sleep(.02)
        break
failed = len(sources)-len(records)
manifest={'format':'whisper-result-manifest', 'version':1, 'app_version':'5.0.0',
    'complete':failed==0, 'total':len(sources), 'failed':failed,
    'runtime_provenance':provenance, 'wrapper_sources_sha256':wrapper,
    'records':records, 'artifacts':artifacts}
if mode == 'manifest-count':
    manifest['complete'] = False
if mode == 'escape-output':
    artifacts[0]['path'] = '../' + artifacts[0]['path']
    records[0]['artifacts'][0] = artifacts[0]['path']
if mode == 'unassigned-output':
    extra = result / 'unassigned.txt'
    extra.write_text('Unassigned synthetic output.', encoding='utf-8')
    artifacts.append({'path':extra.name, 'bytes':extra.stat().st_size, 'sha256':digest(extra)})
if mode == 'shared-output':
    records[1]['artifacts'] = records[0]['artifacts']
(result/'result_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False),encoding='utf-8')
status(len(records)+(mode == 'counter-mismatch'),failed,state='Завершено' if not failed else 'Частково')
raise SystemExit(2 if mode=='stop-after-first' else 1 if failed else 0)
'''


@pytest.fixture
def v5(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Independent synthetic V5 boundary')
    service = app.transcription(workspace)
    runtime = tmp_path / 'synthetic-runtime'
    runtime.mkdir()
    worker = runtime / 'worker.py'
    worker.write_text(SYNTHETIC_V5_WORKER, encoding='utf-8')
    for name in ('local_models.py', 'transcript_features.py'):
        (runtime / name).write_text('# Synthetic protocol fixture only.\n', encoding='utf-8')
    models = tmp_path / 'models'
    (models / 'small').mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (models / 'small' / name).write_bytes(('Synthetic, non-executable model placeholder: '+name).encode())
    source = tmp_path / 'Запис.wav'
    source.write_bytes(b'Synthetic recording placeholder; no acoustic inference.')
    service.configure(Path(sys.executable).resolve(), worker, models, protocol=PROTOCOL)
    return app, workspace, service, source, runtime, models


def mode(service, identity, value):
    atomic_json(service._path(identity) / 'fixture-mode.json', value, replace=True)


def run(v5, *, settings=None, scenario='success', files=None):
    service, source = v5[2:4]
    identity = service.submit(files or [source], options=settings)
    mode(service, identity, scenario)
    return identity, service.run(identity, timeout=10)


@pytest.mark.parametrize('word_timestamps', [False, True])
def test_v5_actual_child_retains_sources_and_validated_requested_exports(v5, tmp_path, word_timestamps):
    service, source = v5[2:4]
    original = source.read_bytes()
    identity, result = run(v5, settings={'word_timestamps':word_timestamps})
    assert result['status'] == 'completed', result['last_error']
    assert result['protocol'] == PROTOCOL and result['settings']['max_attempts'] == 3
    assert result['done'] == 1
    formats = {item['format'] for item in result['artifacts']}
    assert {'txt','srt','vtt','json'} <= formats
    assert {'words.srt','words.vtt'} <= formats if word_timestamps else formats == {'txt','srt','vtt','json'}
    retained = result['sources'][0]['filename']
    assert service.text(identity, retained) == 'Synthetic V5 fixture.\n'
    edit = service.save_edit(identity, retained, 'Separate human correction.')
    assert service.edits(identity)[0]['id'] == edit
    destination = service.export(identity, tmp_path / 'export')
    assert destination.exists() and source.read_bytes() == original
    assert service.verify(identity)['status'] == 'completed'


@pytest.mark.parametrize('scenario', ['wrong-source','wrong-model','wrong-helper','wrong-settings',
    'missing-output','changed-output','missing-words','invalid-timing','unsupported-retry','no-manifest'])
def test_v5_forged_or_incomplete_actual_worker_results_never_complete(v5, scenario):
    identity, result = run(v5, scenario=scenario)
    assert result['status'] == 'failed', (scenario, result)
    assert result['done'] == 0 and result['artifacts'] == []
    assert result['last_error']
    assert v5[2].get(identity)['status'] == 'failed'


@pytest.mark.parametrize('scenario', ['retry', 'retry-last'])
def test_v5_allowed_retry_records_actual_effective_settings(v5, scenario):
    identity, result = run(v5, settings={'beam_size':5}, scenario=scenario)
    assert result['status'] == 'completed', result['last_error']
    assert result['settings']['beam_size'] == 5
    assert v5[2].verify(identity)['status'] == 'completed'


@pytest.mark.parametrize('scenario', ['manifest-count', 'counter-mismatch', 'escape-output',
                                    'unassigned-output', 'shared-output'])
def test_v5_manifest_counters_paths_and_output_assignment_are_bound(v5, tmp_path, scenario):
    second = tmp_path / 'second.wav'
    second.write_bytes(b'A second independent synthetic source.')
    identity, result = run(v5, scenario=scenario, settings={'word_timestamps':False},
                           files=[v5[3], second])
    assert result['status'] == 'failed', (scenario, result)
    assert result['done'] == 0 and result['artifacts'] == []
    assert v5[2].get(identity)['status'] == 'failed'


@pytest.mark.parametrize('changed', ['helper', 'model', 'retained-source'])
def test_v5_detects_identity_changes_while_the_actual_child_is_running(v5, changed):
    _, _, service, source, runtime, models = v5
    identity = service.submit([source])
    path = service._path(identity)
    target = {'helper':runtime / 'local_models.py', 'model':models / 'small' / 'model.bin',
              'retained-source':path / 'input' / service.get(identity)['sources'][0]['filename']}[changed]
    mode(service, identity, 'wait-before-results')
    def change_after_child_started():
        if (path / 'fixture-ready').exists() and not (path / 'fixture-release').exists():
            target.write_bytes(target.read_bytes() + b' Modified during synthetic child run.')
            (path / 'fixture-release').write_text('Continue synthetic protocol.')
        return False
    result = service.run(identity, timeout=10, cancelled=change_after_child_started)
    assert (path / 'fixture-release').is_file()
    assert result['status'] == 'failed' and result['artifacts'] == [], result
    assert result['done'] == 0


@pytest.mark.parametrize('changed', ['manifest', 'text'])
def test_v5_reading_completed_results_rejects_later_changes(v5, changed):
    identity, result = run(v5)
    assert result['status'] == 'completed', result['last_error']
    path = v5[2]._path(identity)
    if changed == 'manifest':
        history = read_json(path / 'fixture-runs.json')
        target = Path(history[-1]['results_dir']) / 'result_manifest.json'
        value = read_json(target)
        value['extra'] = 'Changed after successful admission.'
        atomic_json(target, value, replace=True)
    else:
        target = path / 'results' / next(row['path'] for row in result['artifacts'] if row['format'] == 'txt')
        target.write_text('Changed after successful admission.')
    with pytest.raises(ValueError):
        v5[2].text(identity, result['sources'][0]['filename'])


def test_v5_helper_change_after_configuration_is_rejected_before_child_start(v5):
    service, source, runtime = v5[2:5]
    identity = service.submit([source])
    (runtime / 'local_models.py').write_text('# Changed synthetic helper.\n')
    with pytest.raises(ValueError, match='helper|changed'):
        service.run(identity, timeout=10)
    assert not (service._path(identity) / 'fixture-runs.json').exists()
    assert service.get(identity)['status'] == 'ready'


@pytest.mark.parametrize('options', [{'max_attempts':1}, {'model_revision':'a'*40},
    {'multilingual':True,'language':'uk'}, {'include_headers':True}])
def test_v5_rejects_options_the_supplied_profile_cannot_honor(v5, options):
    with pytest.raises(ValueError):
        v5[2].submit([v5[3]], options=options)
    assert v5[2].list() == ()


def test_v5_unknown_existing_configuration_never_falls_back_to_receipts(v5):
    service = v5[2]
    path = service.root / 'engine.json'
    config = read_json(path)
    config['protocol'] = 'unknown-future-protocol'
    atomic_json(path, config, replace=True)
    with pytest.raises(ValueError):
        service.submit([v5[3]])
    assert service.list() == ()


def test_v5_folder_intake_persists_the_selected_profile_and_capabilities(v5, tmp_path):
    service = v5[2]
    folder = tmp_path / 'folder'
    (folder / 'nested').mkdir(parents=True)
    (folder / 'nested' / 'clip.wav').write_bytes(b'Synthetic V5 folder input.')
    result = service.submit_folder(folder, options={'multilingual':True,'language':'auto'})
    job = service.get(result['id'])
    assert job['protocol'] == PROTOCOL and job['settings']['max_attempts'] == 3
    assert job['settings']['multilingual'] and job['sources'][0]['original_name'] == 'nested/clip.wav'
    assert service.run(result['id'], timeout=10)['status'] == 'completed'


@pytest.mark.parametrize('scenario', ['success', 'stale'])
def test_v5_resume_uses_a_new_result_root_and_rejects_old_manifest_replay(v5, tmp_path, scenario):
    service, source = v5[2:4]
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Second synthetic V5 input.')
    identity, first = run(v5, scenario='partial', files=[source, second])
    assert first['status'] == 'partial' and first['done'] == 1, first['last_error']
    mode(service, identity, scenario)
    result = service.run(identity, timeout=10)
    history = read_json(service._path(identity) / 'fixture-runs.json')
    assert len(history) == 2 and history[0]['results_root'] != history[1]['results_root']
    assert (Path(history[0]['results_dir']) / 'result_manifest.json').is_file()
    if scenario == 'stale':
        assert result['status'] == 'failed' and result['artifacts'] == []
    else:
        assert result['status'] == 'completed' and result['done'] == 2, result['last_error']


def test_v5_stop_preserves_cache_and_resume_inputs_then_completes(v5, tmp_path):
    service, source = v5[2:4]
    second = tmp_path / 'second.wav'
    second.write_bytes(b'Second synthetic V5 input.')
    identity = service.submit([source, second])
    mode(service, identity, 'stop-after-first')
    path = service._path(identity)
    def cancelled():
        status = path / 'job_status.json'
        return status.exists() and read_json(status).get('done') == 1
    stopped = service.run(identity, timeout=10, cancelled=cancelled)
    assert stopped['status'] == 'stopped' and stopped['artifacts'] == []
    assert len(list((path / 'input').iterdir())) == 2
    assert (path / 'logs' / 'cache_v5' / 'synthetic-retained.txt').read_text() == 'Synthetic cache; no inference.'
    mode(service, identity, 'success')
    assert service.run(identity, timeout=10)['status'] == 'completed'


def test_v5_completed_results_restore_and_remain_readable_without_engine(v5, tmp_path):
    app, workspace, service, _, runtime, _ = v5
    identity, result = run(v5)
    assert result['status'] == 'completed', result['last_error']
    checksum = app.backup(tmp_path / 'backup')
    for name in ('worker.py','local_models.py','transcript_features.py'):
        (runtime / name).unlink()
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=checksum)
    recovered = LibraryApp(tmp_path / 'restored').transcription(workspace)
    assert recovered.text(identity, result['sources'][0]['filename']) == 'Synthetic V5 fixture.\n'
    assert recovered.run(identity, timeout=10)['status'] == 'completed'
    recovered.export(identity, tmp_path / 'restored-export')


def test_old_receipts_job_keeps_its_protocol_after_v5_is_configured(v5, tmp_path):
    _, _, service, source, _, models = v5
    legacy = runpy.run_path(str(Path(__file__).with_name('test_supplied_worker_bridge.py')))
    worker = tmp_path / 'legacy-worker.py'
    worker.write_text(legacy['SYNTHETIC_WORKER'].replace('__MODE__', repr('complete')), encoding='utf-8')
    service.configure(Path(sys.executable).resolve(), worker, models, protocol='receipts-v1')
    identity = service.submit([source])
    assert service.run(identity, timeout=10)['status'] == 'completed'
    job_path = service._path(identity) / 'job.json'
    job = read_json(job_path)
    job.pop('protocol', None)
    atomic_json(job_path, job, replace=True)
    service.configure(Path(sys.executable).resolve(), v5[4] / 'worker.py', models, protocol=PROTOCOL)
    assert service.get(identity)['protocol'] == 'receipts-v1'
    assert service.text(identity, job['sources'][0]['filename']).startswith('Synthetic transcript.')
    assert service.run(identity, timeout=10)['status'] == 'completed'


def test_v5_cli_configure_create_run_export_share_the_persisted_profile(v5, tmp_path):
    app, workspace, service, source, runtime, models = v5
    original = source.read_bytes()
    command = [sys.executable, '-B', '-m', 'carr_library', 'transcribe',
               '--data-dir', str(app.root), '--workspace', workspace]
    def invoke(*arguments, expected=0):
        result = subprocess.run([*command, *map(str, arguments)], capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        assert result.returncode == expected, result.stdout + result.stderr
        return json.loads(result.stdout) if expected == 0 else result

    configured = invoke('configure', '--python', Path(sys.executable).resolve(),
        '--worker', runtime / 'worker.py', '--models-dir', models, '--protocol', PROTOCOL)
    assert configured['protocol'] == PROTOCOL and configured['inference_qualified'] is False
    assert configured['wrapper_sources_sha256'] == {
        name: sha256((runtime / name).read_bytes()).hexdigest()
        for name in ('worker.py', 'local_models.py', 'transcript_features.py')}
    identity = invoke('create', source, '--protocol', PROTOCOL, '--language', 'uk',
                      '--compute-type', 'auto', '--no-word-timestamps')['id']
    saved = invoke('show', identity)
    assert saved['protocol'] == PROTOCOL and saved['settings']['max_attempts'] == 3
    assert saved['settings']['language'] == 'uk' and saved['settings']['compute_type'] == 'auto'
    assert saved['settings']['word_timestamps'] is False
    result = invoke('run', identity, '--timeout', 15)
    assert result['status'] == 'completed' and result['done'] == 1
    assert {row['format'] for row in result['artifacts']} == {'txt', 'srt', 'vtt', 'json'}
    destination = tmp_path / 'cli-export'
    assert invoke('export', identity, destination)['exported_to'] == str(destination)
    assert read_json(destination / 'transcription-manifest.json')['status'] == 'completed'
    rejected = invoke('create', source, '--max-attempts', 1, expected=1)
    assert 'max_attempts=3' in rejected.stderr
    assert len(service.list()) == 1 and invoke('show', identity)['status'] == 'completed'
    assert service.text(identity, result['sources'][0]['filename']) == 'Synthetic V5 fixture.\n'
    assert source.read_bytes() == original


@pytest.mark.parametrize('protocol', ['receipts-v1', PROTOCOL])
def test_live_progress_reader_allows_actual_worker_status_replacement(v5, tmp_path, monkeypatch, protocol):
    """Keep the host's old snapshot open until the real child replaces its name."""
    _, _, service, source, _, models = v5
    if protocol == 'receipts-v1':
        legacy = runpy.run_path(str(Path(__file__).with_name('test_supplied_worker_bridge.py')))
        worker = tmp_path / 'legacy-progress-worker.py'
        worker.write_text(legacy['SYNTHETIC_WORKER'].replace('__MODE__', repr('progress-wait')),
                          encoding='utf-8')
        service.configure(Path(sys.executable).resolve(), worker, models, protocol=protocol)
    identity = service.submit([source])
    job = service._path(identity)
    if protocol == PROTOCOL:
        mode(service, identity, 'wait-before-results')
    release = job / ('progress-observed' if protocol == 'receipts-v1' else 'fixture-release')
    status_path = job / 'job_status.json'
    observed = {'claimed': False, 'replacement_while_open': False, 'old_snapshot': None}

    class HeldSnapshot:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *arguments):
            return self.stream.__exit__(*arguments)

        def fileno(self):
            return self.stream.fileno()

        def read(self, *arguments):
            held = os.fstat(self.stream.fileno())
            release.write_text('Release the real synthetic child while its old status is open.',
                               encoding='utf-8')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                current = status_path.stat()
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    assert not self.stream.closed
                    observed['replacement_while_open'] = True
                    break
                time.sleep(.02)
            raw = self.stream.read(*arguments)
            observed['old_snapshot'] = json.loads(raw)
            return raw

    def hold_first_status_reader(original):
        def opened(file, *arguments, **keywords):
            stream = original(file, *arguments, **keywords)
            if (not observed['claimed'] and isinstance(file, (str, bytes, os.PathLike))
                    and Path(os.fsdecode(file)).absolute() == status_path):
                observed['claimed'] = True
                return HeldSnapshot(stream)
            return stream
        return opened

    # Cover both the original Path.open implementation and the shared-read
    # opener. Neither the actual child nor os.replace is mocked.
    monkeypatch.setattr(builtins, 'open', hold_first_status_reader(builtins.open))
    monkeypatch.setattr(io, 'open', hold_first_status_reader(io.open))
    notices = []
    result = service.run(identity, timeout=20, progress=lambda *notice: notices.append(notice))
    assert observed['claimed'] and release.is_file()
    assert observed['replacement_while_open'], result['last_error']
    assert observed['old_snapshot']['done'] == 0
    assert result['status'] == 'completed' and result['done'] == 1, result['last_error']
    assert {'txt', 'srt', 'vtt', 'json'} <= {item['format'] for item in result['artifacts']}
    assert service.verify(identity)['status'] == 'completed'
    assert service.text(identity, result['sources'][0]['filename']).startswith('Synthetic')
    assert notices[-1][-1] == 'completed'
