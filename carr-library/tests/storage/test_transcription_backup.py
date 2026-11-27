"""Real catalog/sidecar round trips with synthetic, sealed ASR records.

No inference runtime is executed. The existing subprocess bridge suite covers
worker execution; these cases exercise the actual typed library backup boundary.
"""
from hashlib import sha256
import importlib.util
import json
import os
import shutil
from pathlib import Path
import wave

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup, transcription_backup
from carr_library.backup_files import portable_relative
from carr_library.durable_state import atomic_json, read_json
from carr_library.transcription import TranscriptionService, RECOGNITION_KEYS, EXPORT_KEYS
from carr_library.transcription_host import read_worker_status


def _bind(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    return {**value, 'sha256': sha256(raw).hexdigest()}


def _media(path):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0\0' * 80)


@pytest.fixture
def saved(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic ASR backup library')
    service = TranscriptionService(app, workspace)
    private = tmp_path / 'private-engine'
    private.mkdir()
    python, worker, models = private / 'python.exe', private / 'worker.py', private / 'models'
    python.write_bytes(b'Synthetic executable identity, never executed.')
    worker.write_text('# Synthetic passive backup identity, never executed.\n', encoding='utf-8')
    models.mkdir()
    (models / 'weights.bin').write_bytes(b'Private weights must not enter the backup.')
    configuration = service.configure(python, worker, models)
    original = tmp_path / 'Лекція.wav'
    _media(original)
    identity = service.submit([original], options={'word_timestamps': False})
    path, job = service._path(identity), service.get(identity)
    source = job['sources'][0]
    run_id = 'a' * 32
    context = {'input_root': str((path / 'input').resolve()),
               'worker_sha256': configuration['worker_sha256'],
               'python_sha256': configuration['python_sha256']}
    model = _bind({'selected': job['settings']['model'], 'files': [],
                   'resolved_revision': 'synthetic-receipt-only'})
    runtime = _bind({'worker_sha256': configuration['worker_sha256'],
                     'executable': {'sha256': configuration['python_sha256']}})
    recognition = _bind({
        'version': 1,
        'source': {'root': context['input_root'],
                   'path': os.path.normcase(str(Path(context['input_root']) / source['filename'])),
                   'relative': source['filename'], 'size_bytes': source['size_bytes'],
                   'sha256': source['sha256']},
        'model': model, 'runtime': runtime,
        'recognition': {key: job['settings'][key] for key in RECOGNITION_KEYS},
    })
    artifacts = []
    for kind, raw in (
        ('txt', b''),
        ('srt', b''),
        ('vtt', b'WEBVTT\n\n'),
        ('json', b'{"segments":[]}\n'),
    ):
        relative = 'input/' + Path(source['filename']).stem + '.' + kind
        output = path / 'results' / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        artifacts.append({'path': relative, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    receipt_name = 'receipts/' + recognition['sha256'] + '.result.json'
    receipt = {
        'version': 1, 'state': 'complete', 'run_id': run_id, 'identity': recognition,
        'requested_exports': {key: job['settings'][key] for key in EXPORT_KEYS},
        'outcome': 'no_speech', 'artifacts': artifacts,
        'result_metadata_sha256': next(row['sha256'] for row in artifacts if row['path'].endswith('.json')),
    }
    atomic_json(path / receipt_name, receipt)
    status = {'state': 'finished', 'result_state': 'completed', 'run_id': run_id,
              'done': 1, 'total': 1, 'failed': 0, 'receipts': [receipt_name]}
    atomic_json(path / 'job_status.json', status)
    job['run_context'] = context
    receipts, accepted = service._receipts(path, job, status)
    job.update(status='completed', done=1, failed=0, worker_run_id=run_id,
               receipts=receipts, artifacts=accepted)
    atomic_json(path / 'job.json', job, replace=True)
    assert service.verify(identity)['done'] == 1
    revision = service.save_edit(identity, source['filename'], 'Окрема авторська правка.')
    return app, workspace, service, identity, revision, private, original


def _names(app):
    return {path.relative_to(app.root).as_posix(): path.read_bytes()
            for path in transcription_backup.state_paths(app.root)}


def _corrupt_receipt(path):
    value = read_json(path)
    value['artifacts'][0]['sha256'] = '0' * 64
    atomic_json(path, value, replace=True)


def test_typed_asr_roundtrip_retains_silent_outputs_receipts_and_edits(saved, tmp_path):
    app, workspace, service, identity, revision, private, original = saved
    before = _names(app)
    original_bytes = original.read_bytes()
    source = service.get(identity)['sources'][0]['filename']
    destination = tmp_path / 'backup'
    digest = library_backup.create_backup(app, destination)
    manifest = read_json(destination / 'manifest.json')
    assert manifest['version'] in (4, 5)
    entries = {row['path']: row for row in manifest['sidecars']
               if row['path'].startswith(transcription_backup.PREFIX)}
    assert set(entries) == set(before)
    assert any(name.endswith('.txt') and entries[name]['size_bytes'] == 0 for name in entries)
    for name, raw in before.items():
        assert entries[name] == {'path': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
    target = library_backup.restore_backup(destination, tmp_path / 'restored', digest)
    assert {name: (target / name).read_bytes() for name in before} == before
    assert all((target / name).stat().st_nlink == 1 for name in before)
    restored = TranscriptionService(LibraryApp(target), workspace)
    assert restored.verify(identity)['done'] == 1
    assert restored.text(identity, source) == ''
    assert restored.edits(identity)[0]['id'] == revision
    assert original.read_bytes() == original_bytes
    assert not tuple((target / transcription_backup.DIRECTORY).rglob('*.lock'))


def test_backup_is_passive_and_excludes_external_engine_and_transient_private_files(saved, tmp_path):
    app, _, service, identity, _, private, _ = saved
    job = service._path(identity)
    for directory in ('runtime-home', 'temporary', 'logs', '.pending-fixture'):
        folder = job / directory
        folder.mkdir()
        (folder / 'private.txt').write_text('This is not durable project state.', encoding='utf-8')
    # Configuration remains as a passive binding even if the private executable,
    # worker or weights are no longer available on this computer.
    for item in private.rglob('*'):
        if item.is_file():
            item.unlink()
    folder = tmp_path / 'backup'
    digest = library_backup.create_backup(app, folder)
    entries = read_json(folder / 'manifest.json')['sidecars']
    names = {row['path'] for row in entries}
    assert transcription_backup.PREFIX + 'engine.json' in names
    assert all(not transcription_backup.ignored(name) for name in names)
    assert not any(name.endswith(('.exe', '.py', '.bin', '.lock')) for name in names)
    library_backup.verify_backup(folder, digest)
    target = library_backup.restore_backup(folder, tmp_path / 'restored', digest)
    for directory in ('runtime-home', 'temporary', 'logs', '.pending-fixture'):
        relative = job.relative_to(app.root) / directory
        assert not (target / relative).exists()


def test_corrupt_current_receipt_refuses_backup_before_publication(saved, tmp_path):
    app, _, service, identity, *_ = saved
    receipt = service._path(identity) / service.get(identity)['receipts'][0]
    _corrupt_receipt(receipt)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='output is incomplete or changed'):
        library_backup.create_backup(app, destination)
    assert not destination.exists()


def test_restore_rejects_corrupt_typed_receipt_even_with_resealed_outer_manifest(saved, tmp_path):
    app, _, service, identity, *_ = saved
    folder = tmp_path / 'backup'
    library_backup.create_backup(app, folder)
    relative = (service._path(identity) / service.get(identity)['receipts'][0]).relative_to(app.root).as_posix()
    receipt = folder / library_backup.SIDECARS / relative
    _corrupt_receipt(receipt)
    manifest = read_json(folder / 'manifest.json')
    raw = receipt.read_bytes()
    row = next(item for item in manifest['sidecars'] if item['path'] == relative)
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    atomic_json(folder / 'manifest.json', manifest, replace=True)
    digest = sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='output is incomplete or changed'):
        library_backup.restore_backup(folder, target, digest)
    assert not target.exists()


def test_staged_asr_receipts_are_revalidated_before_manifest_publication(saved, tmp_path, monkeypatch):
    app, _, service, identity, *_ = saved
    expected = (service._path(identity) / service.get(identity)['receipts'][0]).relative_to(app.root).as_posix()
    copy = library_backup._copy_sidecar
    changed = []
    def corrupt_copied_receipt(root, relative, target, identity, cancelled):
        result = copy(root, relative, target, identity, cancelled)
        if relative == expected:
            _corrupt_receipt(target)
            raw = target.read_bytes()
            result.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
            changed.append(relative)
        return result
    monkeypatch.setattr(library_backup, '_copy_sidecar', corrupt_copied_receipt)
    target = tmp_path / 'backup'
    with pytest.raises(ValueError, match='output is incomplete or changed'):
        library_backup.create_backup(app, target)
    assert changed == [expected]
    assert not target.exists()
    assert service.verify(identity)['done'] == 1


@pytest.mark.parametrize('kind', ['engine', 'submit', 'transcribe'])
def test_active_asr_mutation_refuses_snapshot_before_database_open(saved, tmp_path, monkeypatch, kind):
    app, _, service, identity, *_ = saved
    root = {'engine': service.root, 'submit': service.jobs_root,
            'transcribe': service._path(identity)}[kind]
    lock = FileLock(root / ('.' + kind + '.lock'), timeout=0)
    monkeypatch.setattr(library_backup, 'Storage',
                        lambda *a, **kw: pytest.fail('opened database during active ASR mutation'))
    with lock:
        with pytest.raises(ValueError, match='transcription job or engine is active'):
            library_backup.create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_snapshot_holds_engine_lock_before_another_workspace_namespace_is_created(saved, tmp_path, monkeypatch):
    app, _, _, _, _, _, original = saved
    other = app.create_workspace('Another workspace')
    incoming = TranscriptionService(app, other)
    assert not incoming.jobs_root.exists()
    copy = library_backup._copy_sidecar
    attempted = []
    def inspect(*args, **kwargs):
        if not attempted:
            attempted.append(True)
            with pytest.raises(RuntimeError, match='already being changed'):
                incoming.submit([original])
            assert not incoming.jobs_root.exists()
        return copy(*args, **kwargs)
    monkeypatch.setattr(library_backup, '_copy_sidecar', inspect)
    library_backup.create_backup(app, tmp_path / 'backup')
    assert attempted == [True]


def test_unknown_durable_asr_state_is_not_silently_omitted(saved, tmp_path):
    app, _, service, identity, *_ = saved
    (service._path(identity) / 'unregistered-secret.txt').write_text('unregistered', encoding='utf-8')
    with pytest.raises(ValueError, match='Unknown transcription state file'):
        library_backup.create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_archived_workspace_asr_snapshot_preserves_scope_without_reauthorizing(saved, tmp_path):
    app, workspace, _, identity, *_ = saved
    app.archive_workspace(workspace)
    backup = tmp_path / 'backup'
    digest = library_backup.create_backup(app, backup)
    target = library_backup.restore_backup(backup, tmp_path / 'restored', digest)
    restored = LibraryApp(target)
    with pytest.raises(ValueError, match='archived'):
        TranscriptionService(restored, workspace)
    restored.restore_workspace(workspace)
    assert TranscriptionService(restored, workspace).verify(identity)['done'] == 1


@pytest.mark.parametrize('name', ['../escape', 'a//b', 'a\\b', 'NUL.txt', 'folder/CON.json',
                                  'a/space. ', 'a/\u0080', '/absolute'], ids=[
    'parent', 'empty-part', 'backslash', 'device', 'nested-device',
    'trailing-alias', 'control', 'absolute'])
def test_shared_portable_relative_preserves_existing_backup_boundary(name):
    with pytest.raises(ValueError):
        portable_relative(name)
    with pytest.raises(ValueError, match='invalid audio-project artifact path'):
        library_backup._portable_relative(name)

# Reuse the public synthetic protocol fixture already required by the feature;
# this is neither a model download nor copied private recognition code.
_protocol_spec = importlib.util.spec_from_file_location('asr_backup_protocol_fixture',
    Path(__file__).resolve().parents[1] / 'transcription' / 'test_supplied_worker_bridge.py')
_protocol = importlib.util.module_from_spec(_protocol_spec)
_protocol_spec.loader.exec_module(_protocol)
protocol_prepared = _protocol.prepared

_STOP_WORKER = r'''
import json, os, pathlib, sys, time, uuid
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
status = {'run_id': uuid.uuid4().hex, 'state': 'running', 'result_state': 'running',
          'done': 0, 'total': len(list((job / 'input').iterdir())), 'failed': 0, 'receipts': []}
def publish():
    temporary = job / 'temporary' / 'status.json'
    temporary.write_text(json.dumps(status), encoding='utf-8')
    os.replace(temporary, job / 'job_status.json')
publish()
while not (job / 'stop.flag').exists():
    time.sleep(.01)
status.update(state='stopped', result_state='stopped')
publish()
raise SystemExit(2)
'''


@pytest.fixture
def stopped_history(protocol_prepared, tmp_path):
    app, workspace, service, original, configure = protocol_prepared
    second = tmp_path / 'second-retained.wav'
    second.write_bytes(original.read_bytes())
    configure('partial')
    identity = service.submit([original, second])
    partial = service.run(identity, timeout=30)
    assert partial['status'] == 'partial' and partial['done'] == 1
    source = partial['sources'][0]['filename']
    revision = service.save_edit(identity, source, 'Правка часткового результату.')
    configuration = service.configuration()
    worker = tmp_path / 'cooperative-stop-worker.py'
    worker.write_text(_STOP_WORKER, encoding='utf-8')
    service.configure(configuration['python'], worker, configuration['models_dir'])
    path = service._path(identity)
    def cancelled():
        try:
            status = read_worker_status(path / 'job_status.json')
        except (OSError, ValueError):
            return False
        return status.get('run_id') != partial['worker_run_id']
    stopped = service.run(identity, cancelled=cancelled, timeout=10)
    assert stopped['status'] == 'stopped'
    assert stopped['done'] == 0 and stopped['receipts'] == [] and stopped['artifacts'] == []
    assert service.edits(identity)[0]['id'] == revision
    return app, workspace, service, identity, partial, revision


def test_partial_edit_then_actual_resume_and_stop_roundtrips_without_promoting_results(stopped_history, tmp_path):
    app, workspace, service, identity, partial, revision = stopped_history
    before = _names(app)
    backup = tmp_path / 'backup'
    digest = library_backup.create_backup(app, backup)
    target = library_backup.restore_backup(backup, tmp_path / 'restored', digest)
    assert {name: (target / name).read_bytes() for name in before} == before
    recovered = TranscriptionService(LibraryApp(target), workspace)
    job = recovered.get(identity)
    assert job['status'] == 'stopped' and job['done'] == 0
    assert job['receipts'] == [] and job['artifacts'] == []
    edit, = recovered.edits(identity)
    assert edit['id'] == revision and edit['text'] == 'Правка часткового результату.'
    assert edit['recognition_identity'] == next(row['recognition_identity']
        for row in partial['artifacts'] if row['format'] == 'txt')
    # Full backup inspection also exercises the historical receipt path without
    # needing the original executable or interpreting stopped files as exports.
    Path(recovered.configuration()['worker']).unlink()
    library_backup.verify_backup(backup, digest)
    assert recovered.get(identity) == job


@pytest.mark.parametrize('damage', ['txt', 'source', 'receipt', 'missing-receipt', 'missing-txt'])
def test_historical_edit_requires_retained_receipt_original_media_and_exact_text(stopped_history, tmp_path, damage):
    app, _, service, identity, partial, _ = stopped_history
    path = service._path(identity)
    artifact = next(row for row in partial['artifacts'] if row['format'] == 'txt')
    text = path / 'results' / artifact['path']
    receipt = path / partial['receipts'][0]
    if damage == 'txt':
        text.write_text('Changed after the retained edit.\n', encoding='utf-8')
    elif damage == 'source':
        original = path / 'input' / artifact['source']
        raw = bytearray(original.read_bytes())
        raw[-1] ^= 1
        original.write_bytes(raw)
    elif damage == 'receipt':
        value = read_json(receipt)
        value['identity']['runtime']['worker_sha256'] = '0' * 64
        atomic_json(receipt, value, replace=True)
    elif damage == 'missing-receipt':
        receipt.unlink()
    else:
        text.unlink()
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError):
        library_backup.create_backup(app, destination)
    assert not destination.exists()
    job = service.get(identity)
    assert job['status'] == 'stopped' and job['done'] == 0
    assert job['receipts'] == [] and job['artifacts'] == []


# The public V5 fixture contains only synthetic model placeholders and a child
# implementing the file protocol. Private supplied source is never copied here.
_v5_spec = importlib.util.spec_from_file_location('asr_backup_v5_fixture',
    Path(__file__).resolve().parents[1] / 'transcription' / 'test_v5_worker_profile.py')
_v5 = importlib.util.module_from_spec(_v5_spec)
_v5_spec.loader.exec_module(_v5)
v5_prepared = _v5.v5


def _clean_v5_controls(service, identity):
    for name in ('fixture-mode.json', 'fixture-runs.json'):
        (service._path(identity) / name).unlink(missing_ok=True)


def _completed_v5(prepared, *, words=True):
    app, workspace, service, source, runtime, models = prepared
    identity = service.submit([source], options={'beam_size': 3, 'word_timestamps': words})
    _v5.mode(service, identity, 'retry')
    result = service.run(identity, timeout=30)
    assert result['status'] == 'completed', result['last_error']
    assert result['effective_runs'][0]['attempt'] == 'beam1_no_previous'
    revision = service.save_edit(identity, result['sources'][0]['filename'], 'Окрема правка V5.')
    _clean_v5_controls(service, identity)
    return app, workspace, service, identity, result, revision, runtime, models


@pytest.fixture
def saved_v5(v5_prepared):
    return _completed_v5(v5_prepared)


@pytest.mark.parametrize('words', [False, True], ids=['base-exports', 'word-subtitles'])
def test_v5_actual_child_backup_restores_results_and_edits_without_external_runtime(v5_prepared, tmp_path, words, monkeypatch):
    app, workspace, service, identity, result, revision, runtime, models = _completed_v5(v5_prepared, words=words)
    before = _names(app)
    shutil.rmtree(runtime)
    shutil.rmtree(models)
    from carr_library import transcription
    def external_read(*args):
        raise AssertionError('Passive V5 backup must not read a model or run worker admission.')
    monkeypatch.setattr(transcription, '_v5_model', external_read)
    monkeypatch.setattr(TranscriptionService, '_admit_v5', external_read)
    backup = tmp_path / 'v5-backup'
    checksum = library_backup.create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    entries = {item['path'] for item in manifest['sidecars']}
    assert any(name.endswith('/host_status.json') for name in entries)
    assert any('/results/runs/' in name and name.endswith('/result_manifest.json') for name in entries)
    assert not any('/logs/' in name or '/runtime-home/' in name for name in entries)
    restored_root = library_backup.restore_backup(backup, tmp_path / 'restored-v5', checksum)
    assert {name: (restored_root / name).read_bytes() for name in before} == before
    recovered = LibraryApp(restored_root).transcription(workspace)
    assert recovered.verify(identity)['effective_runs'] == result['effective_runs']
    assert recovered.run(identity, timeout=1)['status'] == 'completed'
    assert recovered.text(identity, result['sources'][0]['filename']) == 'Synthetic V5 fixture.\n'
    assert recovered.edits(identity)[0]['id'] == revision
    recovered.export(identity, tmp_path / 'v5-export')
    assert len(recovered.get(identity)['artifacts']) == (6 if words else 4)


@pytest.mark.parametrize('damage', ['host-status', 'manifest', 'effective-settings',
                                   'context-wrapper', 'word-export', 'source'])
def test_v5_passive_backup_rejects_changed_accepted_state(saved_v5, tmp_path, damage):
    app, _, service, identity, result, _, _, _ = saved_v5
    path = service._path(identity)
    if damage == 'host-status':
        value = read_json(path / 'host_status.json')
        value['done'] = 0
        atomic_json(path / 'host_status.json', value, replace=True)
    elif damage == 'manifest':
        manifest = path / 'results' / result['run_context']['result_directory'] / 'result_manifest.json'
        manifest.write_bytes(manifest.read_bytes() + b'\n')
    elif damage in ('effective-settings', 'context-wrapper'):
        value = read_json(path / 'job.json')
        if damage == 'effective-settings':
            value['effective_runs'][0]['settings']['language'] = 'ru'
        else:
            value['run_context']['wrapper_sources_sha256']['local_models.py'] = '0' * 64
        atomic_json(path / 'job.json', value, replace=True)
    elif damage == 'word-export':
        artifact = next(item for item in result['artifacts'] if item['format'] == 'words.srt')
        (path / 'results' / artifact['path']).write_text('Changed derived timings\n', encoding='utf-8')
    else:
        source = path / 'input' / result['sources'][0]['filename']
        data = bytearray(source.read_bytes())
        data[-1] ^= 1
        source.write_bytes(data)
    target = tmp_path / 'rejected-v5-backup'
    with pytest.raises(ValueError):
        library_backup.create_backup(app, target)
    assert not target.exists()


@pytest.fixture
def stopped_v5_history(v5_prepared, tmp_path):
    app, workspace, service, source, runtime, models = v5_prepared
    second = tmp_path / 'second-v5.wav'
    second.write_bytes(source.read_bytes())
    identity = service.submit([source, second], options={'beam_size': 3})
    _v5.mode(service, identity, 'partial')
    partial = service.run(identity, timeout=30)
    assert partial['status'] == 'partial' and partial['done'] == 1, partial.get('last_error')
    revision = service.save_edit(identity, partial['sources'][0]['filename'], 'Збережена правка до зупинки V5.')
    stop_runtime = tmp_path / 'cooperative-v5-runtime'
    stop_runtime.mkdir()
    (stop_runtime / 'worker.py').write_text(_STOP_WORKER, encoding='utf-8')
    for name in ('local_models.py', 'transcript_features.py'):
        (stop_runtime / name).write_text('# Synthetic stop-only helper identity.\n', encoding='utf-8')
    service.configure(service.configuration()['python'], stop_runtime / 'worker.py', models, protocol=_v5.PROTOCOL)
    path = service._path(identity)
    old_run = partial['run_context']['host_run_id']
    def cancelled():
        try:
            status = read_worker_status(path / 'job_status.json')
        except (OSError, ValueError):
            return False
        return status.get('run_id') is not None
    stopped = service.run(identity, cancelled=cancelled, timeout=30)
    assert stopped['status'] == 'stopped' and stopped['done'] == 0
    assert stopped['artifacts'] == [] and stopped['receipts'] == []
    assert stopped['run_context']['host_run_id'] != old_run
    _clean_v5_controls(service, identity)
    return app, workspace, service, identity, partial, revision, (runtime, stop_runtime, models)


def test_v5_partial_edit_survives_new_run_stop_and_passive_restore(stopped_v5_history, tmp_path):
    app, workspace, service, identity, partial, revision, external = stopped_v5_history
    before = _names(app)
    for directory in external:
        shutil.rmtree(directory)
    backup = tmp_path / 'stopped-v5-backup'
    checksum = library_backup.create_backup(app, backup)
    restored_root = library_backup.restore_backup(backup, tmp_path / 'stopped-v5-restored', checksum)
    assert {name: (restored_root / name).read_bytes() for name in before} == before
    recovered = LibraryApp(restored_root).transcription(workspace)
    saved = recovered.get(identity)
    assert saved['status'] == 'stopped' and saved['done'] == 0
    assert saved['receipts'] == [] and saved['artifacts'] == []
    edit, = recovered.edits(identity)
    assert edit['id'] == revision and edit['text'] == 'Збережена правка до зупинки V5.'
    assert edit['recognition_identity'] == partial['artifacts'][0]['recognition_identity']
    library_backup.verify_backup(backup, checksum)
    assert recovered.get(identity) == saved


@pytest.mark.parametrize('damage', ['record-index', 'record-settings', 'original-text', 'missing-receipt', 'source'])
def test_v5_historical_edit_stays_bound_to_original_manifest_and_recognition(stopped_v5_history, tmp_path, damage):
    app, _, service, identity, partial, _, _ = stopped_v5_history
    path = service._path(identity)
    receipt = path / partial['receipts'][0]
    if damage in ('record-index', 'record-settings'):
        value = read_json(receipt)
        if damage == 'record-index':
            value['v5_record'] = 99
        else:
            manifest = path / 'results' / partial['run_context']['result_directory'] / 'result_manifest.json'
            changed = read_json(manifest)
            changed['records'][0]['effective_settings']['language'] = 'ru'
            atomic_json(manifest, changed, replace=True)
            # Rehashing this altered manifest/outer receipt does not change the
            # recognition identity already sealed in the saved user edit.
            value['v5_manifest_sha256'] = sha256(manifest.read_bytes()).hexdigest()
        atomic_json(receipt, value, replace=True)
    elif damage == 'original-text':
        text = next(item for item in partial['artifacts'] if item['format'] == 'txt')
        (path / 'results' / text['path']).write_text('Not the accepted transcript.\n', encoding='utf-8')
    elif damage == 'missing-receipt':
        receipt.unlink()
    else:
        source = path / 'input' / partial['sources'][0]['filename']
        raw = bytearray(source.read_bytes())
        raw[-1] ^= 1
        source.write_bytes(raw)
    destination = tmp_path / 'rejected-historical-v5'
    with pytest.raises(ValueError):
        library_backup.create_backup(app, destination)
    assert not destination.exists()
    saved = service.get(identity)
    assert saved['status'] == 'stopped' and saved['done'] == 0 and saved['artifacts'] == []


def test_legacy_engine_configuration_without_protocol_still_has_passive_backup(saved, tmp_path):
    app, _, service, _, _, _, _ = saved
    path = service.root / 'engine.json'
    value = read_json(path)
    value.pop('protocol', None)
    atomic_json(path, value, replace=True)
    backup = tmp_path / 'legacy-engine-backup'
    checksum = library_backup.create_backup(app, backup)
    library_backup.verify_backup(backup, checksum)


@pytest.mark.parametrize('damage', ['unknown-protocol', 'missing-helper'])
def test_v5_engine_profile_is_validated_without_external_reads(saved_v5, tmp_path, damage):
    app, _, service, _, _, _, runtime, models = saved_v5
    shutil.rmtree(runtime)
    shutil.rmtree(models)
    path = service.root / 'engine.json'
    value = read_json(path)
    if damage == 'unknown-protocol':
        value['protocol'] = 'unknown-worker-profile'
    else:
        value['wrapper_sources_sha256'].pop('local_models.py')
    atomic_json(path, value, replace=True)
    destination = tmp_path / 'rejected-v5-engine'
    with pytest.raises(ValueError):
        library_backup.create_backup(app, destination)
    assert not destination.exists()
