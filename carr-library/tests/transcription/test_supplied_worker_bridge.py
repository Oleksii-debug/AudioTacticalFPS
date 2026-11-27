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
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json, atomic_json
from carr_library.library_backup import restore_backup
from carr_library.transcription import TranscriptionService, settings
from carr_library.progress import ProgressHub
from carr_library.transcription_host import require_completed, read_worker_status


SYNTHETIC_WORKER = r'''
import hashlib, json, os, pathlib, socket, subprocess, sys, time, uuid
MODE = __MODE__
job = pathlib.Path(sys.argv[sys.argv.index('--job-dir') + 1])
def unexpected_error(kind, value, trace):
    try:
        while trace is not None and trace.tb_next is not None:
            trace = trace.tb_next
        detail = {'type': kind.__name__, 'errno': getattr(value, 'errno', None),
                  'winerror': getattr(value, 'winerror', None),
                  'function': trace.tb_frame.f_code.co_name if trace else None,
                  'line': trace.tb_lineno if trace else None,
                  'mode': MODE}
        (job / 'synthetic-worker-error.json').write_text(json.dumps(detail), encoding='utf-8')
    except Exception:
        pass
    sys.__excepthook__(kind, value, trace)
sys.excepthook = unexpected_error
cfg = json.loads((job / 'job_config.json').read_text(encoding='utf-8'))
assert set(cfg) == {'job_name', 'folders', 'results_root', 'models_dir', 'settings', 'expected_model_identity'}
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
if MODE in ('status-outside-source', 'status-outside-target'):
    try:
        if MODE == 'status-outside-source':
            os.replace(pathlib.Path(cfg['models_dir']) / 'guarded.json', job / 'job_status.json')
        else:
            temp = job / 'status.tmp'
            temp.write_text('Unapproved replacement.', encoding='utf-8')
            os.replace(temp, pathlib.Path(cfg['models_dir']) / 'guarded.json')
    except PermissionError as error:
        expected = 'Transcription writes must remain inside its private job.'
        assert str(error) == expected
        (job / 'logs').mkdir(exist_ok=True)
        (job / 'logs' / 'status-write-refused.txt').write_text(expected, encoding='utf-8')
        raise
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
        changed = {key: value for key, value in model.items() if key != 'sha256'}
        changed['resolved_revision'] = '0' * 40
        model = bind(changed)
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
    models = synthetic_models(tmp_path / 'models')
    original = tmp_path / 'Лекція.wav'
    with wave.open(str(original), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * 160)
    # Structurally valid synthetic silence; no acoustic inference is performed.
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
    from carr_library.transcription_host import AudioCancelled
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
    created = subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'transcribe',
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


@pytest.mark.parametrize('mode', ['missing-output', 'metadata-mismatch', 'escape', 'model-mismatch'])
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
    assert completed_job.returncode == 0, completed_job.stdout + '\n' + completed_job.stderr
    assert service.verify(identity)['status'] == 'completed'


def _protocol_diagnostic(path):
    """Bounded fields from this synthetic job only; never dump transcript bytes."""
    result = {}
    for name in ('job_status.json', 'synthetic-worker-error.json'):
        try:
            value = read_json(path / name)
            result[name] = {key: value.get(key) for key in (
                'state', 'result_state', 'done', 'total', 'failed', 'type', 'errno',
                'winerror', 'function', 'line', 'mode') if key in value}
            if isinstance(value.get('receipts'), list):
                result[name]['receipt_count'] = len(value['receipts'])
        except (OSError, ValueError, TypeError) as error:
            result[name] = type(error).__name__
    return result


def test_stop_does_not_rehash_inputs_and_preserves_completed_worker_files(prepared, tmp_path, monkeypatch):
    _, _, service, original, configure = prepared
    configure('cancel-after-first')
    second = tmp_path / 'second-stop.wav'
    second.write_bytes(b'Another synthetic input.')
    identity = service.submit([original, second])
    path = service._path(identity)
    from carr_library import transcription
    process_errors = []
    runner = transcription.run_process
    def diagnostic_runner(*args, **kwargs):
        try:
            return runner(*args, **kwargs)
        except Exception as error:
            process_errors.append((type(error).__name__, str(error)[:300],
                                   getattr(error, 'errno', None), getattr(error, 'winerror', None)))
            raise
    monkeypatch.setattr(transcription, 'run_process', diagnostic_runner)
    def must_not_rehash(*args, **kwargs):
        raise AssertionError('Stop must defer verification until explicit resume: ' +
                             repr((process_errors, _protocol_diagnostic(path))))
    monkeypatch.setattr(service, '_receipts', must_not_rehash)
    def cancelled():
        status = path / 'job_status.json'
        return status.exists() and read_worker_status(status).get('done') == 1
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
        return status.exists() and read_worker_status(status).get('done') == 1

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
    tracker = ProgressHub().start('Recognizing retained inputs', total=2)
    calls, events, observer_errors = [], [], []

    def progress(completed, total, phase):
        calls.append((completed, total, phase))
        try:
            events.append(tracker.update(completed=completed, total=total, phase=phase))
        except Exception as exc:
            observer_errors.append(str(exc))
            raise

    def cancelled():
        status = path / 'job_status.json'
        return (any(done == 1 for done, _, _ in calls) and status.exists()
                and read_worker_status(status).get('done') == 1)

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
    with pytest.raises(InterruptedError, match='stopped'):
        require_completed(result)
    assert all(event.percent < 100 for event in events)
    assert tracker.cancel('Stopped; saved files await verification').percent < 100


def test_partial_progress_requires_verified_resume_before_host_success(prepared, tmp_path):
    _, _, service, original, configure = prepared
    configure('partial')
    second = tmp_path / 'second-partial-progress.wav'
    second.write_bytes(b'Second synthetic retained input for partial and resume.')
    identity = service.submit([original, second])
    tracker, events = ProgressHub().start('Recognizing retained inputs', total=2), []

    def progress(completed, total, phase):
        events.append(tracker.update(completed=completed, total=total, phase=phase))

    partial = service.run(identity, timeout=30, progress=progress)
    assert partial['status'] == 'partial' and partial['done'] == 1, (
        partial.get('last_error'), _protocol_diagnostic(service._path(identity)))
    with pytest.raises(RuntimeError):
        require_completed(partial)
    assert events and all(event.percent < 100 for event in events)
    assert tracker.fail('Incomplete operation; review remaining input').percent < 100

    configure('complete')
    tracker = ProgressHub().start('Resuming retained inputs', total=2)
    completed = service.run(identity, timeout=30, progress=progress)
    assert completed['status'] == 'completed' and completed['done'] == 2
    assert require_completed(completed) is completed
    assert all(event.percent < 100 for event in events)
    assert tracker.event.percent == 99
    assert tracker.complete(verified=True, message='Host accepted all results').percent == 100


def test_completed_result_receipt_verification_observes_cancellation(prepared, monkeypatch):
    from threading import Event
    from carr_library.transcription_host import AudioCancelled
    service = prepared[2]
    identity, before = completed(prepared)
    stop = Event()
    sources = service._sources
    def checked_sources(path, job, cancelled):
        value = sources(path, job, cancelled)
        stop.set()  # Cancellation arrives after the first retained-source pass.
        return value
    monkeypatch.setattr(service, '_sources', checked_sources)
    with pytest.raises(AudioCancelled):
        service.run(identity, timeout=30, cancelled=stop.is_set)
    assert service.get(identity) == before
    assert before['status'] == 'completed'


def test_export_preserves_an_empty_destination_created_at_final_publication(prepared, tmp_path, monkeypatch):
    service = prepared[2]
    identity, _ = completed(prepared)
    destination = tmp_path / 'concurrent-export'
    authorize = service._authorize
    observed = []
    def concurrent_destination():
        authorize()
        if any(path.is_dir() for path in tmp_path.glob('.pending-*')):
            destination.mkdir()
            observed.append(destination.stat().st_ino)
    monkeypatch.setattr(service, '_authorize', concurrent_destination)
    with pytest.raises(FileExistsError):
        service.export(identity, destination)
    assert observed == [destination.stat().st_ino]
    assert list(destination.iterdir()) == []
    assert not list(tmp_path.glob('.pending-*'))


def test_selected_virtualenv_remains_the_actual_worker_runtime(prepared, tmp_path):
    import venv

    _, _, service, original, _ = prepared
    runtime = tmp_path / 'selected-environment'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(runtime)
    executable = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    packages = runtime / ('Lib/site-packages' if os.name == 'nt' else
                         f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    packages.mkdir(parents=True, exist_ok=True)
    marker = 'carr_synthetic_selected_environment_marker'
    (packages / (marker + '.py')).write_text('VALUE = "selected runtime"\n', encoding='utf-8')
    # This package exists only in the deliberately selected environment.
    probe = subprocess.run([str(executable), '-I', '-c', f'import {marker}; print({marker}.VALUE)'],
                           capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert probe.returncode == 0 and probe.stdout.strip() == 'selected runtime', probe.stderr
    worker = tmp_path / 'venv-worker.py'
    worker.write_text(f'import {marker}\n' +
                      SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    configured = service.configure(executable, worker, tmp_path / 'models')
    identity = service.submit([original])
    result = service.run(identity, timeout=15)
    assert result['status'] == 'completed', result['last_error']
    assert configured['python'] == str(executable)
    source = result['sources'][0]['filename']
    recognized = service.text(identity, source)
    assert 'Український' in recognized

    # Change only this fixture's entry, never the real interpreter behind it.
    executable.unlink()
    executable.write_bytes(b'Synthetic changed interpreter; never executed.')
    with pytest.raises(ValueError, match='runtime changed'):
        service.configuration(verify=True)
    assert service.text(identity, source) == recognized


@pytest.mark.parametrize('mode', ['status-outside-source', 'status-outside-target'])
def test_status_publication_keeps_actual_child_writes_inside_its_job(prepared, mode):
    _, _, service, original, configure = prepared
    guarded = original.parent / 'models' / 'guarded.json'
    guarded.write_bytes(b'External state must remain unchanged.')
    retained = original.read_bytes()
    configure(mode)
    identity = service.submit([original])
    result = service.run(identity, timeout=10)
    assert result['status'] == 'failed' and result['artifacts'] == []
    # The current host deliberately hides raw child diagnostics, which may
    # contain private source text or paths. Prove the exact audit rejection
    # inside the real child without changing that public error boundary.
    assert result['last_error'] == 'Local engine failed with exit code 1.'
    proof = service._path(identity) / 'logs' / 'status-write-refused.txt'
    assert proof.read_text(encoding='utf-8') == 'Transcription writes must remain inside its private job.'
    assert guarded.read_bytes() == b'External state must remain unchanged.'
    assert original.read_bytes() == retained
    assert read_json(service._path(identity) / 'job_status.json')['done'] == 0


def test_edit_rechecks_workspace_after_receipt_verification(prepared, monkeypatch):
    app, workspace, service, original, _ = prepared
    identity, result = completed(prepared)
    source = result['sources'][0]['filename']
    retained = service.save_edit(identity, source, 'Previously saved correction.')
    directory = service._path(identity)
    saved_edit = directory / 'edits' / (retained + '.json')
    original_edit = saved_edit.read_bytes()
    original_job = (directory / 'job.json').read_bytes()
    original_media = original.read_bytes()
    verify = service.verify

    def archive_after_verification(*args, **kwargs):
        verified = verify(*args, **kwargs)
        app.archive_workspace(workspace)
        return verified

    monkeypatch.setattr(service, 'verify', archive_after_verification)
    with pytest.raises(ValueError, match='archived'):
        service.save_edit(identity, source, 'Must not be published after archival.')

    assert list((directory / 'edits').iterdir()) == [saved_edit]
    assert saved_edit.read_bytes() == original_edit
    assert (directory / 'job.json').read_bytes() == original_job
    assert original.read_bytes() == original_media


def test_edit_publication_excludes_concurrent_workspace_archival(prepared, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from carr_library import transcription
    from carr_library.ingest.writer_guard import WriterBusy

    app, workspace, service, _, _ = prepared
    identity, result = completed(prepared)
    source = result['sources'][0]['filename']
    publish = transcription.atomic_json
    attempts = []

    def publish_while_archive_attempts(path, value, **kwargs):
        if path.parent.name == 'edits':
            with ThreadPoolExecutor(max_workers=1) as pool:
                competing = pool.submit(app.archive_workspace, workspace)
                with pytest.raises(WriterBusy):
                    competing.result(timeout=5)
            attempts.append(path)
        return publish(path, value, **kwargs)

    monkeypatch.setattr(transcription, 'atomic_json', publish_while_archive_attempts)
    revision = service.save_edit(identity, source, 'An authorized published correction.')
    assert attempts == [service._path(identity) / 'edits' / (revision + '.json')]
    assert service.edits(identity)[0]['id'] == revision
    assert service.verify(identity)['status'] == 'completed'
    # The guard ends with publication; archival is not permanently blocked.
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        service.get(identity)


def test_receipt_model_identity_binds_actual_selected_bytes_and_revision(prepared):
    from carr_library.transcription_contract import bound_identity

    _, _, service, _, _ = prepared
    model = Path(service.configuration()['models_dir']) / 'small'
    marker = model / 'revision.txt'
    marker.write_text('1' * 40 + '\n', encoding='ascii')
    nested = model / 'assets'
    nested.mkdir()
    (nested / 'extra.bin').write_bytes(b'Additional retained model inventory bytes.')
    (model / '__pycache__').mkdir()
    (model / '__pycache__' / 'ignored.bin').write_bytes(b'Interpreter cache is excluded.')
    (model / 'ignored.pyc').write_bytes(b'Excluded bytecode.')
    (model / 'ignored.pyo').write_bytes(b'Excluded bytecode.')
    (model / 'retained.PYC').write_bytes(b'Case-sensitive suffix stays in the inventory.')
    expected = bound_identity({
        'selected': 'small', 'requested_revision': '1' * 40,
        'resolved_revision': '1' * 40,
        'files': sorted([
            {'path': path.relative_to(model).as_posix(),
             'size_bytes': len(path.read_bytes()), 'sha256': sha256(path.read_bytes()).hexdigest()}
            for path in model.rglob('*') if (path.is_file()
                and '__pycache__' not in path.relative_to(model).parts
                and not path.name.endswith(('.pyc', '.pyo')))
        ], key=lambda item: item['path']),
    })
    identity, result = completed(prepared, options={'model_revision': '1' * 40})
    assert result['run_context']['model_identity'] == expected
    path = service._path(identity)
    config = read_json(path / 'job_config.json')
    assert config['expected_model_identity'] == expected
    assert set(config) == {'job_name', 'folders', 'results_root', 'models_dir',
                           'settings', 'expected_model_identity'}
    for receipt in result['receipts']:
        assert read_json(path / receipt)['identity']['model'] == expected


@pytest.mark.parametrize('name', ['model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json'])
@pytest.mark.parametrize('damage', ['missing', 'empty'])
def test_incomplete_receipt_model_is_refused_before_worker_start(prepared, monkeypatch, name, damage):
    from carr_library import transcription

    _, _, service, original, _ = prepared
    model = Path(service.configuration()['models_dir']) / 'small'
    selected = model / name
    if damage == 'missing':
        selected.unlink()
    else:
        selected.write_bytes(b'')
    identity = service.submit([original])
    started = []
    monkeypatch.setattr(transcription, 'run_process', lambda *args, **kwargs: started.append(args))
    with pytest.raises(ValueError, match='complete selected local model'):
        service.run(identity, timeout=10)
    assert started == []
    assert service.get(identity)['status'] == 'ready'
    assert not (service._path(identity) / 'job_config.json').exists()
    assert original.exists()


@pytest.mark.parametrize('marker', [None, '2' * 40, 'invalid revision'])
def test_receipt_model_exact_revision_is_checked_before_worker_start(prepared, monkeypatch, marker):
    from carr_library import transcription

    _, _, service, original, _ = prepared
    if marker is not None:
        model = Path(service.configuration()['models_dir']) / 'small'
        (model / 'revision.txt').write_text(marker + '\n', encoding='ascii')
    identity = service.submit([original], options={'model_revision': '1' * 40})
    started = []
    monkeypatch.setattr(transcription, 'run_process', lambda *args, **kwargs: started.append(args))
    with pytest.raises(ValueError, match='revision'):
        service.run(identity, timeout=10)
    assert started == []
    assert service.get(identity)['status'] == 'ready'
    assert not (service._path(identity) / 'job_config.json').exists()


@pytest.mark.parametrize('limit', ['MAX_MODEL_BYTES', 'MAX_MODEL_FILES'])
def test_receipt_model_inventory_has_actual_byte_and_file_budgets(prepared, monkeypatch, limit):
    from carr_library import transcription_contract

    service = prepared[2]
    models = Path(service.configuration()['models_dir'])
    monkeypatch.setattr(transcription_contract, limit, 3)
    with pytest.raises(ValueError, match='verification allowance'):
        transcription_contract.model_inventory(models, settings())


@pytest.mark.parametrize('change', ['same-size', 'growth'])
def test_receipt_model_change_during_streaming_is_rejected(prepared, monkeypatch, change):
    from carr_library import transcription_contract

    service = prepared[2]
    models = Path(service.configuration()['models_dir'])
    selected = models / 'small' / 'model.bin'
    original_open = Path.open
    changed = []

    class ChangingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            value = self.stream.read(size)
            if value and not changed:
                with original_open(selected, 'wb') as writer:
                    writer.write(b'x' * (len(value) + (1 if change == 'growth' else 0)))
                changed.append(True)
            return value

    def open_snapshot(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return ChangingReader(stream) if path == selected and mode == 'rb' else stream

    monkeypatch.setattr(Path, 'open', open_snapshot)
    with pytest.raises(ValueError, match='Local model bytes (changed|grew)'):
        transcription_contract.model_inventory(models, settings())
    assert changed == [True]


def test_receipt_model_cancellation_precedes_worker_and_checkpoint(prepared, monkeypatch):
    from carr_library import transcription
    from carr_library.transcription_host import AudioCancelled

    _, _, service, original, _ = prepared
    identity = service.submit([original])
    started = []
    monkeypatch.setattr(transcription, 'run_process', lambda *args, **kwargs: started.append(args))
    with pytest.raises(AudioCancelled, match='model verification was cancelled'):
        service.run(identity, cancelled=lambda: True, timeout=10)
    assert started == []
    assert service.get(identity)['status'] == 'ready'
    assert not (service._path(identity) / 'job_config.json').exists()


def test_bound_receipt_backup_needs_no_external_model_or_worker(prepared, tmp_path):
    import shutil

    app, workspace, service, original, _ = prepared
    identity, result = completed(prepared)
    expected = result['run_context']['model_identity']
    config = service.configuration()
    shutil.rmtree(config['models_dir'])
    Path(config['worker']).unlink()
    original.unlink()
    assert service.verify(identity)['run_context']['model_identity'] == expected
    destination = tmp_path / 'bound-model-backup'
    checksum = app.backup(destination)
    restored = restore_backup(destination, tmp_path / 'bound-model-restored',
                              expected_manifest_sha256=checksum)
    recovered = LibraryApp(restored).transcription(workspace)
    assert recovered.verify(identity)['run_context']['model_identity'] == expected
    assert read_json(recovered._path(identity) / 'job_config.json')['expected_model_identity'] == expected
    assert not Path(config['models_dir']).exists()
    assert not Path(config['worker']).exists()
