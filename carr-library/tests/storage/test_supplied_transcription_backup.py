"""Real backup/restore of synthetic managed-worker state; no ASR inference claim."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import carr_library
from carr_library.application import LibraryApp
from carr_library.audio.jobs import project_lock
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.supplied_transcription_backup import (state_paths, validate_state_tree,
    verify_saved_completion)
from carr_library.transcription_receipt import (PROTOCOL, OUTPUT_SETTINGS_KEYS,
    canonical_digest, validate_settings)
from carr_library.transcription_state import SCHEMA, read_definition, validate_definition

IDENTITY = '1' * 32
RUN_ID = '2' * 32


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    path.write_bytes(raw)
    return raw


def envelope(kind, payload):
    return dict(schema='supplied-whisper-v43-integrity/1/' + kind,
                payload=payload, sha256=canonical_digest(payload))


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic transcription recovery')
    original = tmp_path / 'original' / 'Synthetic.wav'
    original.parent.mkdir()
    # A queued retained source is not yet decoder-qualified. A full backup must
    # preserve it even if inference would later reject these synthetic bytes.
    raw = b'Owned source bytes before any decoder has been launched.\n'
    original.write_bytes(raw)
    job = app.root / 'transcription-jobs' / IDENTITY
    owned = job / 'inputs/root-0/Synthetic.wav'
    owned.parent.mkdir(parents=True)
    owned.write_bytes(raw)
    model_files = {name: sha(name.encode()) for name in ('model.bin', 'config.json', 'tokenizer.json')}
    settings = validate_settings({})
    definition = dict(schema=SCHEMA, id=IDENTITY, title='Synthetic saved transcription',
        created='2026-11-27T00:00:00+00:00', settings=settings,
        runtime_manifest_sha256='a' * 64, model_sha256=canonical_digest(model_files),
        inputs=[dict(path='inputs/root-0/Synthetic.wav', root='inputs/root-0',
                     relative='Synthetic.wav', bytes=len(raw), sha256=sha(raw))],
        source_provenance=[dict(path=str(original), root=str(original.parent),
                     relative='Synthetic.wav', bytes=len(raw), sha256=sha(raw))])
    validate_definition(definition, identity=IDENTITY)
    write_json(job / 'job.json', definition)
    return app, job, definition, original, model_files


def artifacts(job, definition, model_files, *, staged=False, linked=False, empty=False, complete=False,
              output='group/Synthetic.txt'):
    engine = dict(identity=dict(model_sha256=definition['model_sha256'],
        runtime_sha256=definition['runtime_manifest_sha256'],
        evidence=dict(manifest_sha256=definition['runtime_manifest_sha256'], model_files=model_files,
            files=[dict(path='models/small/' + name, bytes=len(name), sha256=checksum)
                   for name, checksum in sorted(model_files.items())])),
        execution=dict(device='cpu', compute_type='int8', device_index=0, num_workers=1),
        protocol=PROTOCOL)
    engine_id = canonical_digest(engine)
    write_json(job / 'logs/integrity-v1/engines' / (engine_id + '.json'), envelope('engine', engine))
    row = definition['inputs'][0]
    context = dict(source=dict(path=str(job / row['path']), root=str(job / row['root']),
        relative=row['relative'], group='root-0', bytes=row['bytes'], sha256=row['sha256']),
        settings={key: value for key, value in definition['settings'].items() if key not in OUTPUT_SETTINGS_KEYS},
        model_sha256=definition['model_sha256'], runtime_sha256=definition['runtime_manifest_sha256'],
        engine_sha256=engine_id, execution=engine['execution'])
    inference = canonical_digest(context)
    text = '' if empty else 'Synthetic retained transcript.'
    payload = dict(context=context, text=text, detected_language='en', duration=0 if empty else 1,
        segments=[] if empty else [dict(id=0, seek=0, start=0, end=1, text=text, words=[])],
        created='2026-11-27T00:00:00Z')
    inference_path = job / 'logs/integrity-v1/inference' / inference[:2] / (inference + '.json')
    write_json(inference_path, envelope('inference', payload))
    owner = dict(job_dir=str(job), protocol=PROTOCOL)
    output_root = job / 'results/verified-v43' / canonical_digest(owner)[:24]
    write_json(output_root / 'owner.json', envelope('owner', owner))
    exports = {key: definition['settings'][key] for key in OUTPUT_SETTINGS_KEYS}
    request = dict(owner=owner, selection=dict(inference=inference, exports=exports), paths=[output])
    request_id = canonical_digest(request)
    raw = (json.dumps({'text': text}, ensure_ascii=False) if output.endswith('.json') else text).encode('utf-8')
    target = output_root / output
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = None
    if staged:
        relative_stage = '.transactions/staging-' + '3' * 32 + '/000000.part'
        stage = output_root / relative_stage
        stage.parent.mkdir(parents=True)
        stage.write_bytes(raw)
        info = stage.stat()
        entry = dict(path=output, stage=relative_stage, bytes=len(raw), sha256=sha(raw),
                     identity=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
        intent_path = output_root / '.transactions' / (request_id + '.json')
        write_json(intent_path, envelope('output-intent', dict(request=request, entries=[entry])))
        if linked:
            os.link(stage, target)
    else:
        target.write_bytes(raw)
        info = target.stat()
        entry = dict(path=output, bytes=len(raw), sha256=sha(raw),
                     identity=[info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
        write_json(output_root / '.receipts' / (request_id + '.json'),
                   envelope('outputs', dict(request=request, outputs=[entry])))
    if complete:
        assert not staged
        config = dict(run_id=RUN_ID, job_name=definition['title'],
            folders=[str(job / row['root'])],
            inputs=[dict(row, path=str(job / row['path']), root=str(job / row['root']))],
            source_provenance=definition['source_provenance'], settings=definition['settings'],
            results_root=str(job / 'results'), models_dir=str(job.parent.parent.parent / 'absent-models'),
            runtime_manifest=str(job.parent.parent.parent / 'absent-runtime' / 'manifest.json'),
            runtime_manifest_sha256=definition['runtime_manifest_sha256'])
        config_raw = write_json(job / 'job_config.json', config)
        finished = dict(run_id=RUN_ID, job_root=str(job), job_config_sha256=sha(config_raw),
            results_root=str(output_root), output_settings=exports,
            inputs=[dict(config['inputs'][0], effective_settings=definition['settings'],
                         model_sha256=definition['model_sha256'],
                         runtime_sha256=definition['runtime_manifest_sha256'])],
            outputs=[dict(path=output, format=Path(output).suffix[1:], source_indexes=[0], bytes=len(raw), sha256=sha(raw))])
        completion = dict(schema=PROTOCOL, payload=finished, sha256=canonical_digest(finished))
        completion_raw = write_json(job / 'logs/completion-v1.json', completion)
        write_json(job / 'job_status.json', dict(protocol=PROTOCOL, run_id=RUN_ID,
            state='completed', done=1, total=1, completion_sha256=sha(completion_raw)))
        write_json(job / 'execution.json', dict(schema=SCHEMA, state='complete',
            run_id=RUN_ID, updated='2026-11-27T00:00:00+00:00', exit_code=0))
    return dict(root=output_root, target=target, stage=stage, inference=inference_path,
                output=output, request=request, request_id=request_id)


def capture(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def test_prepared_job_survives_lost_original_restore_and_rebackup(tmp_path, monkeypatch):
    app, job, definition, original, _ = prepared(tmp_path)
    original.unlink()
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Backup launched a runtime.'))
    destination = tmp_path / 'backup'
    digest = create_backup(app, destination)
    before = capture(destination)
    verify_backup(destination, digest)
    assert capture(destination) == before
    restored = restore_backup(destination, tmp_path / 'restored', digest)
    restored_job = restored / 'transcription-jobs' / IDENTITY
    assert read_definition(restored_job) == definition
    assert (restored_job / definition['inputs'][0]['path']).read_bytes().startswith(b'Owned source')
    assert not original.exists()
    assert not list(restored_job.rglob('*.lock'))
    second = tmp_path / 'backup-again'
    second_digest = create_backup(LibraryApp(restored), second)
    verify_backup(second, second_digest)
    assert capture(destination) == before


@pytest.mark.parametrize('empty', [False, True], ids=['text-output', 'empty-no-speech-output'])
def test_complete_output_is_readable_after_relocation_without_runtime_or_original(tmp_path, empty):
    app, job, definition, original, models = prepared(tmp_path)
    records = artifacts(job, definition, models, empty=empty, complete=True)
    original.unlink()
    assert not (tmp_path / 'absent-runtime').exists()
    assert not (tmp_path / 'absent-models').exists()
    accepted = verify_saved_completion(job, definition)
    assert accepted['historical'] is True and accepted['runtime_current_verified'] is False
    destination = tmp_path / 'backup'
    digest = create_backup(app, destination)
    restored = restore_backup(destination, tmp_path / 'different-root', digest)
    moved = restored / 'transcription-jobs' / IDENTITY
    value = verify_saved_completion(moved, read_definition(moved))
    assert value['historical'] is True
    assert Path(value['job_root']) == moved
    assert Path(value['results_root']).is_relative_to(moved / 'results')
    assert value['outputs'] == accepted['outputs']
    target = Path(value['results_root']) / value['outputs'][0]['path']
    assert target.read_bytes() == records['target'].read_bytes()
    assert not (tmp_path / 'absent-runtime').exists()
    assert not (tmp_path / 'absent-models').exists()


def test_current_and_historical_execution_bytes_are_preserved_without_reactivation(tmp_path):
    app, job, definition, _, models = prepared(tmp_path)
    artifacts(job, definition, models, complete=True)
    history = job / 'history' / ('4' * 32)
    history.mkdir(parents=True)
    for name in ('job_config.json', 'job_status.json', 'execution.json'):
        (history / name).write_bytes((job / name).read_bytes())
    (history / 'completion-v1.json').write_bytes((job / 'logs/completion-v1.json').read_bytes())
    write_json(job / 'settings_live.json', {'language': 'uk'})
    write_json(history / 'settings_live.json', {'language': 'sk'})
    expected = {str(path.relative_to(job)): path.read_bytes() for path in history.iterdir()}
    target = tmp_path / 'backup'
    digest = create_backup(app, target)
    restored = restore_backup(target, tmp_path / 'restored', digest)
    moved = restored / 'transcription-jobs' / IDENTITY
    for relative, raw in expected.items():
        assert (moved / relative).read_bytes() == raw
    assert read_definition(moved) == definition
    assert verify_saved_completion(moved, definition)['historical'] is True


@pytest.mark.parametrize('linked', [False, True], ids=['unpublished-stage', 'posix-link-crash-window'])
def test_only_valid_intent_staging_and_owned_link_pair_are_copied(tmp_path, linked):
    app, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models, staged=True, linked=linked)
    orphan = records['root'] / '.transactions' / ('staging-' + '5' * 32) / '000001.part'
    orphan.parent.mkdir()
    orphan.write_bytes(b'Unreferenced temporary bytes')
    transient = job / 'logs/integrity-v1/inputs/input-synthetic.wav'
    transient.parent.mkdir()
    transient.write_bytes(b'Disposable inference-only copy')
    (job / 'stop.flag').write_bytes(b'')
    files = {p.relative_to(app.root).as_posix() for p in state_paths(app.root)}
    assert records['stage'].relative_to(app.root).as_posix() in files
    assert orphan.relative_to(app.root).as_posix() not in files
    assert transient.relative_to(app.root).as_posix() not in files
    validate_state_tree(None, app.root)
    target = tmp_path / 'backup'
    digest = create_backup(app, target)
    restored = restore_backup(target, tmp_path / 'restored', digest)
    copied_stage = restored / records['stage'].relative_to(app.root)
    assert copied_stage.read_bytes() == records['stage'].read_bytes()
    assert copied_stage.stat().st_nlink == 1
    assert not (restored / orphan.relative_to(app.root)).exists()
    assert not (restored / transient.relative_to(app.root)).exists()
    if linked:
        copied_output = restored / records['target'].relative_to(app.root)
        assert copied_output.read_bytes() == copied_stage.read_bytes()
        assert copied_output.stat().st_nlink == 1
        assert copied_output.stat().st_ino != copied_stage.stat().st_ino


def test_external_hardlink_alias_cannot_enter_an_archive(tmp_path):
    app, job, _, _, models = prepared(tmp_path)
    definition = read_definition(job)
    records = artifacts(job, definition, models)
    os.link(records['target'], tmp_path / 'external-alias.txt')
    with pytest.raises(ValueError, match='hard.link|alias'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_missing_or_foreign_intent_stage_fails_before_publication(tmp_path):
    app, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models, staged=True)
    intent = records['root'] / '.transactions' / (records['request_id'] + '.json')
    value = json.loads(intent.read_text())
    value['payload']['entries'][0]['stage'] = '../outside.part'
    value['sha256'] = canonical_digest(value['payload'])
    write_json(intent, value)
    with pytest.raises(ValueError, match='stage'):
        create_backup(app, tmp_path / 'bad-stage')
    assert not (tmp_path / 'bad-stage').exists()


def test_inference_content_and_engine_bindings_are_checked(tmp_path):
    app, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models)
    value = json.loads(records['inference'].read_text())
    value['payload']['text'] = 'Edited text that does not match its segments'
    value['sha256'] = canonical_digest(value['payload'])
    write_json(records['inference'], value)
    with pytest.raises(ValueError, match='segments'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('field', ['job_config', 'status', 'execution', 'input', 'output', 'exports', 'counts'],
                         ids=['raw-config-sha', 'status-run', 'accepted-exit', 'input-bytes', 'output-bytes',
                              'captured-output-settings', 'accepted-input-count'])
def test_saved_completion_rejects_changed_binding_or_data(tmp_path, field):
    _, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models, complete=True)
    if field == 'job_config':
        path = job / 'job_config.json'
        path.write_bytes(path.read_bytes() + b'\n')  # Same JSON, different approved raw bytes.
    elif field == 'status':
        path = job / 'job_status.json'
        value = json.loads(path.read_text())
        value['run_id'] = '7' * 32
        write_json(path, value)
    elif field == 'exports':
        path = job / 'logs/completion-v1.json'
        value = json.loads(path.read_text())
        value['payload']['output_settings']['include_headers'] = True
        value['sha256'] = canonical_digest(value['payload'])
        raw = write_json(path, value)
        status = json.loads((job / 'job_status.json').read_text())
        status['completion_sha256'] = sha(raw)
        write_json(job / 'job_status.json', status)
    elif field == 'counts':
        path = job / 'job_status.json'
        value = json.loads(path.read_text())
        value['done'] = 0
        write_json(path, value)
    elif field == 'execution':
        path = job / 'execution.json'
        value = json.loads(path.read_text())
        value['exit_code'] = 1
        write_json(path, value)
    else:
        path = job / definition['inputs'][0]['path'] if field == 'input' else records['target']
        info, raw = path.stat(), path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(ValueError):
        verify_saved_completion(job, definition)


def test_saved_completion_cannot_follow_foreign_absolute_or_relative_output(tmp_path):
    _, job, definition, _, models = prepared(tmp_path)
    artifacts(job, definition, models, complete=True)
    receipt = job / 'logs/completion-v1.json'
    value = json.loads(receipt.read_text())
    value['payload']['outputs'][0]['path'] = '../../outside.txt'
    value['sha256'] = canonical_digest(value['payload'])
    raw = write_json(receipt, value)
    status = json.loads((job / 'job_status.json').read_text())
    status['completion_sha256'] = sha(raw)
    write_json(job / 'job_status.json', status)
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'Must remain unchanged')
    with pytest.raises(ValueError):
        verify_saved_completion(job, definition)
    assert outside.read_bytes() == b'Must remain unchanged'


def test_unregistered_preparation_or_input_is_not_silently_omitted(tmp_path):
    app, job, _, _, _ = prepared(tmp_path)
    unknown = job / 'inputs/root-0/unregistered.wav'
    unknown.write_bytes(b'Owned but unregistered bytes')
    with pytest.raises(ValueError, match='unregistered'):
        create_backup(app, tmp_path / 'backup')
    unknown.unlink()
    abandoned = job.parent / '.prepare-abandoned'
    abandoned.mkdir()
    (abandoned / 'original.wav').write_bytes(b'Incomplete preparation')
    with pytest.raises(ValueError, match='incomplete preparation'):
        create_backup(app, tmp_path / 'backup')
    assert abandoned.is_dir()


def test_cancellation_and_tight_budget_leave_originals_and_no_completed_backup(tmp_path):
    app, job, definition, original, _ = prepared(tmp_path)
    original_bytes = original.read_bytes()
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'cancelled', cancelled=lambda: True)
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'budget', max_sidecar_bytes=1)
    assert not (tmp_path / 'cancelled').exists()
    assert not (tmp_path / 'budget').exists()
    assert original.read_bytes() == original_bytes
    assert read_definition(job) == definition


@pytest.mark.parametrize('name', ['../outside.lock', 'nested/worker.lock', '.worker:stream.lock', 'worker.lock'])
def test_native_lock_name_is_one_checked_local_basename(tmp_path, name):
    selected = tmp_path / 'not-created'
    with pytest.raises(ValueError):
        with project_lock(selected, lock_name=name):
            pytest.fail('An invalid lock name was accepted.')
    assert not selected.exists()


def test_actual_independent_worker_lock_blocks_complete_backup(tmp_path):
    app, job, _, _, _ = prepared(tmp_path)
    ready = tmp_path / 'worker-ready'
    package_parent = str(Path(carr_library.__file__).resolve().parent.parent)
    code = """
import pathlib, sys
sys.path.insert(0, sys.argv[3])
from carr_library.audio.jobs import project_lock
with project_lock(pathlib.Path(sys.argv[1]), lock_name='.worker-v5.lock'):
    pathlib.Path(sys.argv[2]).write_bytes(b'locked')
    if sys.stdin.readline().strip() != 'release':
        raise SystemExit(3)
"""
    process = subprocess.Popen([sys.executable, '-I', '-c', code,
        str(job / 'logs'), str(ready), package_parent], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        end = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < end:
            time.sleep(.02)
        if not ready.exists():
            process.terminate()
            _, error = process.communicate(timeout=5)
            pytest.fail('The independent native lock did not start: ' + error[-500:])
        with pytest.raises(ValueError, match='worker is active'):
            create_backup(app, tmp_path / 'active-backup')
        assert not (tmp_path / 'active-backup').exists()
        _, error = process.communicate('release\n', timeout=5)
        assert process.returncode == 0, error[-500:]
        digest = create_backup(app, tmp_path / 'idle-backup')
        verify_backup(tmp_path / 'idle-backup', digest)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


def test_real_worker_timestamp_contract_preserves_overlapping_segments_and_words(tmp_path):
    app, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models)
    value = json.loads(records['inference'].read_text())
    value['payload']['segments'] = [
        dict(id=0, seek=0, start=0, end=2, text='First segment.',
             words=[dict(start=1, end=1.5, word='First'),
                    dict(start=.5, end=1, word='segment.')]),
        dict(id=1, seek=0, start=1, end=3, text='Second overlapping segment.', words=[])]
    value['payload']['text'] = 'First segment.\nSecond overlapping segment.'
    value['payload']['duration'] = 3
    value['sha256'] = canonical_digest(value['payload'])
    write_json(records['inference'], value)
    # These are accepted data shapes, not proof that a model was executed.
    validate_state_tree(None, app.root)
    digest = create_backup(app, tmp_path / 'overlap-backup')
    verify_backup(tmp_path / 'overlap-backup', digest)


def test_managed_inference_group_is_bound_to_historical_input_root(tmp_path):
    from carr_library.supplied_transcription_backup import _inference
    _, job, definition, _, models = prepared(tmp_path)
    records = artifacts(job, definition, models)
    value = json.loads(records['inference'].read_text())
    engine_path = next((job / 'logs/integrity-v1/engines').glob('*.json'))
    engine = json.loads(engine_path.read_text())['payload']
    value['payload']['context']['source']['group'] = 'another-group'
    name = canonical_digest(value['payload']['context'])
    value['sha256'] = canonical_digest(value['payload'])
    with pytest.raises(ValueError, match='group'):
        _inference(value, definition, name,
                   {value['payload']['context']['engine_sha256']: engine}, lambda: False)


def test_nested_owner_named_json_output_is_retained_as_data(tmp_path):
    app, job, definition, _, models = prepared(tmp_path)
    definition['settings'].update(output_txt=False, output_json=True)
    write_json(job / 'job.json', definition)
    records = artifacts(job, definition, models, complete=True, output='root-0/owner.json')
    digest = create_backup(app, tmp_path / 'backup')
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)
    saved = verify_saved_completion(restored / 'transcription-jobs' / IDENTITY, definition)
    assert saved['outputs'][0]['format'] == 'json'
    target = Path(saved['results_root']) / saved['outputs'][0]['path']
    assert json.loads(target.read_text()) == json.loads(records['target'].read_text())



def test_all_three_explicit_profiles_share_one_typed_complete_backup(tmp_path):
    import wave
    from carr_library.transcription import RECEIPTS_PROTOCOL, V5_PROTOCOL
    app, managed_job, definition, _, model_files = prepared(tmp_path)
    managed = artifacts(managed_job, definition, model_files, complete=True)
    workspace = app.create_workspace('Workspace-scoped compatibility jobs')
    local = app.transcription(workspace)
    runtime = tmp_path / 'existing-profile-runtime'
    runtime.mkdir()
    worker = runtime / 'worker.py'
    worker.write_text('# Synthetic configuration only; never executed.\n', encoding='utf-8')
    for name in ('local_models.py', 'transcript_features.py'):
        (runtime / name).write_text('# Synthetic reviewed helper.\n', encoding='utf-8')
    models = tmp_path / 'existing-profile-models'
    models.mkdir()
    source = tmp_path / 'existing-profile.wav'
    with wave.open(str(source), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\0\0' * 160)
    original = source.read_bytes()
    jobs = {}
    for protocol in (RECEIPTS_PROTOCOL, V5_PROTOCOL):
        local.configure(Path(sys.executable).absolute(), worker, models, protocol=protocol)
        jobs[protocol] = local.submit([source])
    # Backup must remain passive for both profile families.
    worker.unlink()
    source.unlink()
    target = tmp_path / 'all-profile-backup'
    checksum = create_backup(app, target)
    verify_backup(target, checksum)
    restored_root = restore_backup(target, tmp_path / 'all-profile-restored', checksum)
    restored = LibraryApp(restored_root).transcription(workspace)
    for protocol, identity in jobs.items():
        job = restored.get(identity)
        assert job['protocol'] == protocol and job['status'] == 'ready'
        retained = restored._path(identity) / 'input' / job['sources'][0]['filename']
        assert retained.read_bytes() == original
    saved = verify_saved_completion(restored_root / 'transcription-jobs' / IDENTITY, definition)
    assert saved['protocol'] == PROTOCOL and saved['historical']
    assert not saved['runtime_current_verified']
    output = Path(saved['results_root']) / saved['outputs'][0]['path']
    assert output.read_bytes() == managed['target'].read_bytes()
    assert not worker.exists() and not source.exists()
