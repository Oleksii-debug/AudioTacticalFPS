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
from carr_library.library_backup import MANIFEST, restore_backup
from carr_library.transcription import TranscriptionService, settings
from carr_library.ui.operation_progress import ProgressTracker, operation_outcome


def invoke_cli(*arguments, timeout=30, check=False):
    """Run the real CLI from the parent's exact package, including sealed installs."""
    import carr_library

    package = Path(carr_library.__file__).resolve().parent
    modules = {
        'carr_library': '__init__.py',
        'carr_library.__main__': '__main__.py',
        'carr_library.application': 'application.py',
        'carr_library.transcription': 'transcription.py',
        'carr_library.transcription_cli': 'transcription_cli.py',
    }
    expected = {name: {'path': relative, 'sha256': sha256((package / relative).read_bytes()).hexdigest()}
                for name, relative in modules.items()}
    bootstrap = r'''
import hashlib, importlib, json, pathlib, sys
package = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads(sys.argv[2])
sys.path.insert(0, str(package.parent))
verified = {}
for name, row in expected.items():
    module = importlib.import_module(name)
    actual = pathlib.Path(module.__file__).resolve()
    assert actual == package / row['path'], (name, str(actual), row)
    digest = hashlib.sha256(actual.read_bytes()).hexdigest()
    assert digest == row['sha256'], (name, digest, row)
    verified[name] = row
assert sys.flags.isolated and sys.dont_write_bytecode
sys.stderr.write('CARR_TEST_CLI_ORIGIN=' + json.dumps(dict(package=str(package), modules=verified)) + '\n')
from carr_library.__main__ import main
raise SystemExit(main(sys.argv[3:]))
'''
    environment = dict(os.environ)
    for name in ('PYTHONPATH', 'PYTHONHOME'):
        environment.pop(name, None)
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap,
        str(package), json.dumps(expected), 'transcribe', *map(str, arguments)],
        capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=environment)
    lines = result.stderr.splitlines(keepends=True)
    proofs = [line for line in lines if line.startswith('CARR_TEST_CLI_ORIGIN=')]
    assert len(proofs) == 1, result.stdout + result.stderr
    assert json.loads(proofs[0].split('=', 1)[1]) == {'package': str(package), 'modules': expected}
    result.stderr = ''.join(line for line in lines if line not in proofs)
    if check:
        result.check_returncode()
    return result


SYNTHETIC_WORKER = r'''
import hashlib, json, os, pathlib, socket, subprocess, sys, time, uuid
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
    created = invoke_cli('--data-dir', app.root, '--workspace', workspace,
        'create-folder', folder, '--title', 'CLI folder', '--language', 'uk',
        '--no-word-timestamps', check=True)
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
    retained = service._path(identity) / 'input' / source
    original_bytes = original.read_bytes()
    relative = retained.relative_to(app.root / 'state').as_posix()
    receipts = {path.name: path.read_bytes() for path in (service._path(identity) / 'receipts').glob('*.json')}
    assert receipts
    backup = tmp_path / 'backup'
    expected = app.backup(backup)
    manifest = read_json(backup / MANIFEST)
    assert manifest['generated_audio_included'] is False
    entry, = [item for item in manifest['state'] if item['filename'] == relative]
    assert entry['size_bytes'] == len(original_bytes)
    assert entry['sha256'] == sha256(original_bytes).hexdigest()
    assert (backup / 'state' / relative).read_bytes() == original_bytes
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=expected)
    recovered = LibraryApp(restored).transcription(workspace)
    assert recovered.verify(identity)['done'] == 1
    assert recovered.edits(identity)[0]['id'] == revision
    assert recovered.text(identity, source) == service.text(identity, source)
    recovered.export(identity, tmp_path / 'restored-export')
    assert (recovered._path(identity) / 'input' / source).read_bytes() == original_bytes
    assert {path.name: path.read_bytes() for path in (recovered._path(identity) / 'receipts').glob('*.json')} == receipts
    assert original.read_bytes() == original_bytes


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
    verify = service._verify_locked

    def archive_after_verification(*args, **kwargs):
        verified = verify(*args, **kwargs)
        app.archive_workspace(workspace)
        return verified

    monkeypatch.setattr(service, '_verify_locked', archive_after_verification)
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
    created = invoke_cli('--data-dir', app.root, '--workspace', workspace,
        'create', original, check=True)
    identity = json.loads(created.stdout)['id']
    assert service.get(identity)['status'] == 'ready'
    completed_job = invoke_cli('--data-dir', app.root, '--workspace', workspace,
        'run', identity, '--timeout', '30')
    assert completed_job.returncode == 0, completed_job.stdout + '\n' + completed_job.stderr
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
        return bool(list((path / 'receipts').glob('*.result.json')))
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
        return bool(list((path / 'receipts').glob('*.result.json')))

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
        return (bool(list((path / 'receipts').glob('*.result.json')))
                and (os.name == 'nt' or any(done == 1 for done, _, _ in calls)))

    result = service.run(identity, timeout=30, cancelled=cancelled, progress=progress)
    durable, worker = service.get(identity), read_json(path / 'job_status.json')
    assert result == durable and durable['status'] == 'stopped'
    assert durable['done'] == 0 and durable['receipts'] == [] and durable['artifacts'] == []
    assert worker['state'] == 'stopped' and worker['done'] == 1
    saved = list((path / 'receipts').glob('*.result.json'))
    assert len(saved) == 1 and read_json(saved[0])['run_id'] == worker['run_id']
    assert len(list((path / 'results').rglob('*.*'))) == 6
    assert len(list((path / 'input').iterdir())) == 2
    measured = [done for done, total, _ in calls if done is not None]
    assert measured == sorted(measured), f'Progress regressed: {calls!r}; {observer_errors!r}'
    if os.name == 'nt':
        assert calls[:-1] and all(done is None and total is None for done, total, _ in calls[:-1])
        assert calls[-1] == (0, 2, 'stopped')
    else:
        assert measured and max(measured) == 1 and all(total == 2 for _, total, _ in calls)
        assert calls[-1] == (1, 2, 'stopped')
    assert observer_errors == []
    assert operation_outcome(result) == 'cancelled'
    assert all(event.percent is None or event.percent < 100 for event in events)
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
    assert partial['status'] == 'partial' and partial['done'] == 1, partial
    assert operation_outcome(partial) == 'paused'
    assert events and all(event.percent is None or event.percent < 100 for event in events)
    assert tracker.transition(operation_outcome(partial), 'Review remaining input').percent < 100

    configure('complete')
    tracker.start('Resuming retained inputs', 2)
    completed = service.run(identity, timeout=30, progress=progress)
    assert completed['status'] == 'completed' and completed['done'] == 2
    assert operation_outcome(completed) == 'succeeded'
    assert all(event.percent is None or event.percent < 100 for event in events)
    assert tracker.event.percent == 99
    assert tracker.transition(operation_outcome(completed), 'Host accepted all results').percent == 100


def test_rechecking_completed_job_observes_stop_without_changing_saved_result(prepared, monkeypatch):
    from threading import Event
    from carr_library.audio.models import AudioCancelled
    identity, completed_job = completed(prepared)
    service = prepared[2]
    before = (service._path(identity) / 'job.json').read_bytes()
    stop = Event()
    verify_sources = service._sources
    def stop_after_first_source_check(path, job, cancelled):
        value = verify_sources(path, job, cancelled)
        stop.set()
        return value
    monkeypatch.setattr(service, '_sources', stop_after_first_source_check)
    with pytest.raises(AudioCancelled):
        service.run(identity, cancelled=stop.is_set)
    assert service.get(identity)['status'] == 'completed'
    assert service.get(identity)['artifacts'] == completed_job['artifacts']
    assert (service._path(identity) / 'job.json').read_bytes() == before


def test_deferred_status_is_read_only_after_the_actual_legacy_child_exits(prepared, deferred_status_probe):
    _, _, service, original, configure = prepared
    configure('progress-wait')
    identity = service.submit([original])
    probe = deferred_status_probe(service, identity, release='progress-observed')

    result = service.run(identity, timeout=10, progress=probe.progress)

    assert probe.live and probe.reads
    assert set(probe.public_reads) == {'verify', 'text'} and not probe.public_read_errors
    assert len(probe.children) == 1 and probe.children[0].returncode == 0
    assert result == service.get(identity) and result['status'] == 'completed', result['last_error']
    assert result['done'] == 1 and len(result['artifacts']) == 6
    assert probe.notices[-1] == (1, 1, 'completed')
    assert service.verify(identity)['status'] == 'completed'
    assert 'Український' in service.text(identity, result['sources'][0]['filename'])


def test_deferred_status_is_read_only_after_timeout_terminates_the_actual_child(prepared, deferred_status_probe):
    _, _, service, original, configure = prepared
    configure('timeout')
    identity = service.submit([original])
    probe = deferred_status_probe(service, identity)

    result = service.run(identity, timeout=1, progress=probe.progress)

    assert probe.live and probe.reads
    assert set(probe.public_reads) == {'verify', 'text'} and not probe.public_read_errors
    assert len(probe.children) == 1 and probe.children[0].poll() is not None
    assert result == service.get(identity) and result['status'] == 'failed'
    assert 'time allowance' in result['last_error'] and result['artifacts'] == []


def test_deferred_stop_preserves_results_without_reading_live_status_and_resumes(prepared, deferred_status_probe):
    _, _, service, original, configure = prepared
    configure('pause-once')
    identity = service.submit([original])
    probe = deferred_status_probe(service, identity, stop_on_progress=True)

    stopped = service.run(identity, timeout=10, cancelled=probe.stop.is_set, progress=probe.progress)

    assert probe.live and not probe.reads
    assert set(probe.public_reads) == {'verify', 'text'} and not probe.public_read_errors
    assert len(probe.children) == 1 and probe.children[0].poll() is not None
    assert stopped == service.get(identity) and stopped['status'] == 'stopped'
    assert stopped['done'] == 0 and stopped['artifacts'] == []
    assert (service._path(identity) / 'input' / stopped['sources'][0]['filename']).read_bytes() == original.read_bytes()
    configure('complete')
    resumed = service.run(identity, timeout=10)
    assert resumed['status'] == 'completed' and resumed['done'] == 1, resumed['last_error']
    assert len(probe.children) == 2 and probe.reads
    assert all(child.poll() is not None for child in probe.children)


def test_text_keeps_same_job_locked_through_actual_output_read(prepared, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    _, _, service, original, configure = prepared
    configure('partial')
    second = tmp_path / 'second-locked-read.wav'
    second.write_bytes(b'Second retained synthetic input.')
    identity = service.submit([original, second])
    result = service.run(identity, timeout=10)
    assert result['status'] == 'partial' and result['done'] == 1, result['last_error']
    artifact = next(item for item in result['artifacts'] if item['format'] == 'txt')
    target = service._path(identity) / 'results' / artifact['path']
    original_read = Path.read_text
    refused = []

    def read_with_competing_resume(path, *args, **kwargs):
        if path == target:
            with ThreadPoolExecutor(max_workers=1) as pool:
                competing = pool.submit(service.run, identity, timeout=10)
                with pytest.raises(RuntimeError, match='already being changed'):
                    competing.result(timeout=5)
            refused.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_with_competing_resume)
    assert 'Український' in service.text(identity, artifact['source'])
    assert refused == [target]
    configure('complete')
    resumed = service.run(identity, timeout=10)
    assert resumed['status'] == 'completed' and resumed['done'] == 2, resumed['last_error']
