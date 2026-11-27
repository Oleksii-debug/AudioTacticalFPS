"""Real catalog/sidecar round trips with synthetic, sealed ASR records.

No inference runtime is executed. The existing subprocess bridge suite covers
worker execution; these cases exercise the actual typed library backup boundary.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import wave

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup, transcription_backup
from carr_library.backup_files import portable_relative
from carr_library.durable_state import atomic_json, read_json
from carr_library.transcription import TranscriptionService, RECOGNITION_KEYS, EXPORT_KEYS


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
