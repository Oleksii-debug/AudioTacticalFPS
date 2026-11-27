"""Actual subprocess/file/backup tests using an explicitly synthetic protocol worker.

No Whisper model, acoustic accuracy, diarization or multilingual quality is
claimed by these tests. The private supplied worker has a separate test pack.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json, atomic_json
from carr_library.library_backup import restore_backup
from carr_library.transcription import TranscriptionService, settings
from carr_library.ui.operation_progress import ProgressTracker, operation_outcome


SYNTHETIC_WORKER = r'''
import hashlib, json, os, pathlib, socket, subprocess, sys, time, uuid
CARR_TRANSCRIPTION_CAPABILITIES = {'version': 1, 'protocol': 'carr-transcription-receipts/1',
    'word_timestamps': True, 'multilingual': True, 'local_models_only': True,
    'content_bound_cache': True, 'no_language_fallback': True}
MODE = __MODE__
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
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
def publish():
    temp = job / 'status.tmp'
    temp.write_text(json.dumps(status), encoding='utf-8')
    os.replace(temp, job / 'job_status.json')
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
    model = cfg['expected_model_identity']
    if MODE == 'model-mismatch':
        model = bind({**model, 'resolved_revision': '0' * 40, 'sha256': None})
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
        if kind == 'json' and MODE == 'missing-words':
            value = json.dumps({'segments':[{'start':0,'end':1,'text':'Synthetic','words':[]}]})
        if kind == 'json' and MODE == 'invalid-time':
            value = json.dumps({'segments':[{'start':2,'end':1,'text':'Synthetic','words':[]}]})
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


def synthetic_models(root):
    """Inert byte identity fixtures, never a usable acoustic model."""
    model = root / 'small'
    model.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'):
        (model / name).write_bytes(b'Synthetic protocol asset, not a usable model.\n')
    return root


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic transcription library')
    service = app.transcription(workspace)
    models = tmp_path / 'models'
    synthetic_models(models)
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


def test_folder_intake_retains_nested_media_and_reports_nonmedia_without_merging_names(prepared, tmp_path):
    _, _, service, _, _ = prepared
    folder = tmp_path / 'Записи'
    (folder / 'nested').mkdir(parents=True)
    first, second = folder / 'Лекція.WAV', folder / 'nested' / 'Лекція.WAV'
    first.write_bytes(b'First synthetic recording.')
    second.write_bytes(b'Second synthetic recording.')
    ignored = folder / 'notes.txt'
    ignored.write_text('Private sidecar, not audio.')
    originals = {path: path.read_bytes() for path in (first, second, ignored)}

    intake = service.submit_folder(folder, title='Folder intake')
    assert intake['files'] == 2 and intake['excluded_files'] == 1
    job = service.get(intake['id'])
    assert job['status'] == 'ready' and len(service.list()) == 1
    assert {row['original_name'] for row in job['sources']} == {'Лекція.WAV', 'nested/Лекція.WAV'}
    assert len({row['filename'] for row in job['sources']}) == 2
    assert {row['sha256'] for row in job['sources']} == {sha256(originals[path]).hexdigest() for path in (first, second)}
    result = service.run(intake['id'], timeout=10)
    assert result['status'] == 'completed' and len(result['artifacts']) == 12
    assert service.verify(intake['id'])['done'] == 2
    assert originals == {path: path.read_bytes() for path in originals}


@pytest.mark.parametrize('unsupported', [False, True], ids=['empty', 'nonmedia-only'])
def test_folder_without_supported_media_fails_without_creating_a_job(prepared, tmp_path, unsupported):
    service = prepared[2]
    folder = tmp_path / 'folder'
    folder.mkdir()
    if unsupported:
        (folder / 'notes.txt').write_text('Not a recording.')
    with pytest.raises(ValueError, match=f'No supported audio or video files.*{int(unsupported)} unsupported'):
        service.submit_folder(folder)
    assert service.list() == () and not service.jobs_root.exists()


@pytest.mark.parametrize('kind', ['file', 'directory', 'root', 'ancestor', 'hardlink'])
def test_folder_intake_refuses_real_links_without_following_or_retaining_them(prepared, tmp_path, monkeypatch, kind):
    from carr_library import transcription
    service = prepared[2]
    folder, external = tmp_path / 'selected', tmp_path / 'external'
    folder.mkdir()
    (external / 'nested').mkdir(parents=True)
    media = external / 'nested' / 'private.wav'
    media.write_bytes(b'Synthetic external recording that must not be followed.')
    selected = folder
    if kind == 'file':
        (folder / 'link.wav').symlink_to(media)
    elif kind == 'directory':
        (folder / 'linked-folder').symlink_to(external, target_is_directory=True)
    elif kind == 'hardlink':
        os.link(media, folder / 'hardlink.wav')
    else:
        selected = tmp_path / 'selected-alias'
        selected.symlink_to(external, target_is_directory=True)
        if kind == 'ancestor':
            selected /= 'nested'
    monkeypatch.setattr(transcription, '_checked_digest',
                        lambda *args, **kwargs: pytest.fail('A linked input was opened.'))
    with pytest.raises(ValueError, match='[Ll]ink|[Rr]eparse'):
        service.submit_folder(selected)
    assert service.list() == () and not service.jobs_root.exists()
    assert media.read_bytes().startswith(b'Synthetic external')


@pytest.mark.parametrize('when', ['before', 'scan', 'retain', 'retain-last'])
def test_folder_intake_cancellation_leaves_no_partial_job(prepared, tmp_path, monkeypatch, when):
    from carr_library import transcription
    from carr_library.audio.models import AudioCancelled
    service = prepared[2]
    folder = tmp_path / 'folder'
    folder.mkdir()
    names = ('first.wav',) if when == 'retain-last' else ('first.wav', 'second.wav')
    for name in names:
        (folder / name).write_bytes(name.encode())
    stopped, processed = [when == 'before'], []
    name = '_copy' if when.startswith('retain') else '_checked_digest'
    original = getattr(transcription, name)
    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        processed.append(args[0])
        stopped[0] = True
        return result
    monkeypatch.setattr(transcription, name, capture)
    with pytest.raises(AudioCancelled, match='cancelled'):
        service.submit_folder(folder, cancelled=lambda: stopped[0])
    assert len(processed) == (0 if when == 'before' else 1)
    assert service.list() == ()
    assert not service.jobs_root.exists() or not any(path.name.startswith('.pending-') for path in service.jobs_root.iterdir())
    for name in names:
        assert (folder / name).read_bytes() == name.encode()


@pytest.mark.parametrize('budget', ['files', 'source-bytes', 'batch-bytes', 'entries', 'depth'])
def test_folder_intake_limits_reject_the_whole_selection(prepared, tmp_path, monkeypatch, budget):
    from carr_library import transcription
    from carr_library.ingest import batch_scan
    service = prepared[2]
    folder = tmp_path / 'folder'
    (folder / 'nested').mkdir(parents=True)
    for name in ('first.wav', 'second.wav'):
        (folder / name).write_bytes(b'123')
    (folder / 'nested' / 'third.wav').write_bytes(b'123')
    module, name, limit = {
        'files': (transcription, 'MAX_FILES', 2),
        'source-bytes': (transcription, 'MAX_SOURCE_BYTES', 2),
        'batch-bytes': (transcription, 'MAX_BATCH_BYTES', 5),
        'entries': (batch_scan, 'MAX_SCAN_ENTRIES', 1),
        'depth': (batch_scan, 'MAX_DEPTH', 1),
    }[budget]
    monkeypatch.setattr(module, name, limit)
    with pytest.raises(ValueError, match='budget|limit'):
        service.submit_folder(folder)
    assert service.list() == () and not service.jobs_root.exists()


def test_create_folder_cli_uses_the_same_retained_job_and_exclusion_counts(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    folder = tmp_path / 'folder'
    (folder / 'nested').mkdir(parents=True)
    (folder / 'nested' / 'clip.mp4').write_bytes(b'Synthetic video container placeholder.')
    (folder / 'notes.txt').write_text('Excluded nonmedia sidecar.')
    created = subprocess.run([sys.executable, '-m', 'carr_library', 'transcribe',
        '--data-dir', str(app.root), '--workspace', workspace, 'create-folder', str(folder),
        '--title', 'CLI folder', '--language', 'uk', '--no-word-timestamps'],
        check=True, capture_output=True, text=True, encoding='utf-8')
    result = json.loads(created.stdout)
    assert result['files'] == 1 and result['excluded_files'] == 1
    job = service.get(result['id'])
    assert job['status'] == 'ready' and job['title'] == 'CLI folder'
    assert job['sources'][0]['original_name'] == 'nested/clip.mp4'
    assert job['settings']['language'] == 'uk' and not job['settings']['word_timestamps']


@pytest.mark.parametrize('replacement', [b'new!', b'longer replacement'])
def test_folder_intake_rejects_media_changed_after_preflight(prepared, tmp_path, monkeypatch, replacement):
    from carr_library.ingest import batch_scan
    service = prepared[2]
    folder = tmp_path / 'folder'
    folder.mkdir()
    source = folder / 'recording.wav'
    source.write_bytes(b'old!')
    scan = batch_scan.scan_paths
    def changed(*args, **kwargs):
        rows = scan(*args, **kwargs)
        source.write_bytes(replacement)
        return rows
    monkeypatch.setattr(batch_scan, 'scan_paths', changed)
    with pytest.raises(ValueError, match='changed after the folder scan'):
        service.submit_folder(folder)
    assert service.list() == ()
    assert not any(path.name.startswith('.pending-') for path in service.jobs_root.iterdir())
    assert source.read_bytes() == replacement


def test_folder_intake_rejects_scanned_parent_replaced_by_an_outside_link(prepared, tmp_path, monkeypatch):
    from carr_library import transcription
    from carr_library.ingest import batch_scan
    service = prepared[2]
    folder, external = tmp_path / 'folder', tmp_path / 'outside'
    nested = folder / 'nested'
    nested.mkdir(parents=True)
    external.mkdir()
    (nested / 'recording.wav').write_bytes(b'Selected synthetic recording.')
    (external / 'recording.wav').write_bytes(b'Outside synthetic file must not be followed.')
    scan = batch_scan.scan_paths
    def replaced(*args, **kwargs):
        rows = scan(*args, **kwargs)
        nested.rename(tmp_path / 'original-directory')
        nested.symlink_to(external, target_is_directory=True)
        return rows
    monkeypatch.setattr(batch_scan, 'scan_paths', replaced)
    monkeypatch.setattr(transcription, '_copy',
                        lambda *args, **kwargs: pytest.fail('An outside link reached retention.'))
    with pytest.raises(ValueError, match='[Ll]inks'):
        service.submit_folder(folder)
    assert service.list() == ()
    assert not any(path.name.startswith('.pending-') for path in service.jobs_root.iterdir())
    assert (external / 'recording.wav').read_bytes() == b'Outside synthetic file must not be followed.'


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


@pytest.mark.parametrize('mode', ['missing-output', 'metadata-mismatch', 'escape',
                                  'model-mismatch', 'missing-words', 'invalid-time'])
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
    created = subprocess.run([sys.executable, '-m', 'carr_library', 'transcribe',
        '--data-dir', str(app.root), '--workspace', workspace, 'create', str(original)],
        check=True, capture_output=True, text=True, encoding='utf-8')
    identity = json.loads(created.stdout)['id']
    assert service.get(identity)['status'] == 'ready'
    completed_job = subprocess.run([sys.executable, '-m', 'carr_library', 'transcribe',
        '--data-dir', str(app.root), '--workspace', workspace, 'run', identity, '--timeout', '30'],
        capture_output=True, text=True, encoding='utf-8')
    assert completed_job.returncode == 0, completed_job.stderr
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
    assert notices
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
    tracker = ProgressTracker()
    tracker.start('Recognizing retained inputs', 2)
    calls, events, observer_errors = [], [], []

    def progress(completed, total, phase):
        calls.append((completed, total, phase))
        try:
            events.append(tracker.update(completed, total, phase))
        except Exception as exc:
            observer_errors.append(str(exc))
            raise

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
    assert measured == sorted(measured), f'Progress regressed: {calls!r}; {observer_errors!r}'
    assert calls[-1] == (1, 2, 'stopped')
    assert observer_errors == []
    assert operation_outcome(result) == 'cancelled'
    assert all(event.percent < 100 for event in events)
    assert tracker.transition('cancelled', 'Stopped; saved files await verification').percent < 100


def test_partial_progress_requires_verified_resume_before_host_success(prepared, tmp_path):
    _, _, service, original, configure = prepared
    configure('partial')
    second = tmp_path / 'second-partial-progress.wav'
    second.write_bytes(b'Second synthetic retained input for partial and resume.')
    identity = service.submit([original, second])
    tracker, events = ProgressTracker(), []
    tracker.start('Recognizing retained inputs', 2)

    def progress(completed, total, phase):
        events.append(tracker.update(completed, total, phase))

    partial = service.run(identity, timeout=30, progress=progress)
    assert partial['status'] == 'partial' and partial['done'] == 1
    assert operation_outcome(partial) == 'paused'
    assert events and all(event.percent < 100 for event in events)
    assert tracker.transition(operation_outcome(partial), 'Review remaining input').percent < 100

    configure('complete')
    tracker.start('Resuming retained inputs', 2)
    completed = service.run(identity, timeout=30, progress=progress)
    assert completed['status'] == 'completed' and completed['done'] == 2
    assert operation_outcome(completed) == 'succeeded'
    assert all(event.percent < 100 for event in events)
    assert tracker.event.percent == 99
    assert tracker.transition(operation_outcome(completed), 'Host accepted all results').percent == 100


def test_completed_job_receipt_revalidation_obeys_late_cancellation(prepared, monkeypatch):
    from carr_library.audio.process import AudioCancelled

    identity, result = completed(prepared)
    service = prepared[2]
    path = service._path(identity)
    before_job = (path / 'job.json').read_bytes()
    before_status = (path / 'job_status.json').read_bytes()
    original_receipts = service._receipts
    verifying = False

    def receipt_boundary(*args, **kwargs):
        nonlocal verifying
        verifying = True
        return original_receipts(*args, **kwargs)

    def unexpected_child(*args, **kwargs):
        raise AssertionError('A completed result must not launch recognition again.')

    monkeypatch.setattr(service, '_receipts', receipt_boundary)
    monkeypatch.setattr('carr_library.transcription.run_process', unexpected_child)
    with pytest.raises(AudioCancelled):
        service.run(identity, cancelled=lambda: verifying, timeout=10)
    assert verifying
    assert service.get(identity) == result
    assert (path / 'job.json').read_bytes() == before_job
    assert (path / 'job_status.json').read_bytes() == before_status


def test_folder_intake_denied_scope_does_not_enumerate_private_files(prepared, tmp_path, monkeypatch):
    from carr_library.ingest import batch_scan

    app, workspace, service, _, _ = prepared
    folder = tmp_path / 'private-recordings'
    folder.mkdir()
    (folder / 'original.wav').write_bytes(b'Private recording remains uninspected.')
    app.archive_workspace(workspace)
    monkeypatch.setattr(batch_scan, 'scan_paths',
                        lambda *args, **kwargs: pytest.fail('An archived scope enumerated sources.'))
    with pytest.raises(ValueError, match='archived'):
        service.submit_folder(folder)
    assert not service.jobs_root.exists()


def test_folder_intake_rechecks_scope_after_last_real_copy(prepared, tmp_path, monkeypatch):
    from carr_library import transcription

    app, workspace, service, _, _ = prepared
    folder = tmp_path / 'recordings'
    folder.mkdir()
    source = folder / 'original.wav'
    source.write_bytes(b'Private original before scope revocation.')
    copy = transcription._copy

    def archive_after_retention(*args, **kwargs):
        result = copy(*args, **kwargs)
        app.archive_workspace(workspace)
        return result

    monkeypatch.setattr(transcription, '_copy', archive_after_retention)
    with pytest.raises(ValueError, match='archived'):
        service.submit_folder(folder)
    assert not any(service.jobs_root.iterdir())
    assert source.read_bytes() == b'Private original before scope revocation.'


def test_folder_intake_permission_failure_rejects_entire_selection(prepared, tmp_path, monkeypatch):
    from carr_library.ingest import batch_scan

    service = prepared[2]
    folder = tmp_path / 'recordings'
    denied = folder / 'restricted'
    denied.mkdir(parents=True)
    (folder / 'first.wav').write_bytes(b'Readable original must not form a partial job.')
    (denied / 'private.wav').write_bytes(b'Unreadable source.')
    scan = batch_scan.os.scandir

    def cannot_list(path):
        if Path(path) == denied:
            raise PermissionError('The selected recording folder is not readable.')
        return scan(path)

    monkeypatch.setattr(batch_scan.os, 'scandir', cannot_list)
    with pytest.raises(ValueError, match='not readable'):
        service.submit_folder(folder)
    assert service.list() == () and not service.jobs_root.exists()


def test_folder_intake_reparse_media_is_rejected_before_content_read(prepared, tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from carr_library import transcription

    service = prepared[2]
    folder = tmp_path / 'recordings'
    folder.mkdir()
    source = folder / 'remote-placeholder.wav'
    source.write_bytes(b'Synthetic reparse-point boundary.')
    lstat = Path.lstat

    def reparse_info(path, *args, **kwargs):
        info = lstat(path, *args, **kwargs)
        if path == source:
            fields = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
            fields['st_file_attributes'] = stat.FILE_ATTRIBUTE_REPARSE_POINT
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, 'lstat', reparse_info)
    monkeypatch.setattr(transcription, '_checked_digest',
                        lambda *args, **kwargs: pytest.fail('Reparse media content was opened.'))
    with pytest.raises(ValueError, match='[Rr]eparse'):
        service.submit_folder(folder)
    assert service.list() == () and not service.jobs_root.exists()


def test_folder_intake_equal_bytes_keep_distinct_requested_source_names(prepared, tmp_path):
    service = prepared[2]
    folder = tmp_path / 'recordings'
    (folder / 'second').mkdir(parents=True)
    original = b'Two separately selected recording identities with equal bytes.'
    (folder / 'lesson.wav').write_bytes(original)
    (folder / 'second' / 'lesson.wav').write_bytes(original)
    intake = service.submit_folder(folder)
    job = service.get(intake['id'])
    assert intake['files'] == 2
    assert {item['original_name'] for item in job['sources']} == {'lesson.wav', 'second/lesson.wav'}
    assert len({item['filename'] for item in job['sources']}) == 2
    assert {item['sha256'] for item in job['sources']} == {sha256(original).hexdigest()}
    assert service.run(intake['id'], timeout=10)['done'] == 2


def test_pending_folder_intake_backup_retains_relative_names_and_originals(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    folder = tmp_path / 'recordings'
    (folder / 'nested').mkdir(parents=True)
    original = b'Exact pending folder input survives complete backup.'
    (folder / 'nested' / 'lesson.wav').write_bytes(original)
    intake = service.submit_folder(folder)
    checksum = app.backup(tmp_path / 'backup-folder-job')
    destination = tmp_path / 'restored-folder-job'
    restore_backup(tmp_path / 'backup-folder-job', destination, expected_manifest_sha256=checksum)
    restored = LibraryApp(destination).transcription(workspace)
    job = restored.get(intake['id'])
    source, = job['sources']
    assert source['original_name'] == 'nested/lesson.wav'
    assert (restored._path(job['id']) / 'input' / source['filename']).read_bytes() == original
    assert job['restore_requires_review'] is True
    with pytest.raises(PermissionError, match='review'):
        restored.run(job['id'], timeout=10)

