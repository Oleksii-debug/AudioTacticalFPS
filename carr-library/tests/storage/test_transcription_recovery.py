"""Actual public protocol processes and typed restore guards, not ASR inference."""
import importlib.util
from pathlib import Path
import shutil
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup
from carr_library.transcription_contract import declared_capabilities


@pytest.fixture
def prepared(tmp_path):
    test = Path(__file__).parents[1] / 'transcription' / 'test_supplied_worker_bridge.py'
    spec = importlib.util.spec_from_file_location('backup_transcription_fixture', test)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recovery protocol fixture')
    service = app.transcription(workspace)
    models = module.synthetic_models(tmp_path / 'models')
    worker = tmp_path / 'worker.py'
    worker.write_text(module.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    service.configure(Path(sys.executable), worker, models)
    original = tmp_path / 'original.wav'
    original.write_bytes(b'Inert protocol-only source.')
    identity = service.submit([original])
    return app, workspace, service, identity, worker, models


def test_restore_keeps_verified_results_but_revokes_external_execution(prepared, tmp_path):
    app, workspace, service, identity, worker, models = prepared
    completed = service.run(identity, timeout=30)
    assert completed['status'] == 'completed'
    receipt = service._path(identity) / completed['receipts'][0]
    receipt_bytes = receipt.read_bytes()
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    worker.unlink()
    shutil.rmtree(models)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    recovered = LibraryApp(root).transcription(workspace)
    assert recovered.configuration()['restore_requires_revalidation'] is True
    assert recovered.get(identity)['restore_requires_review'] is True
    assert (recovered._path(identity) / completed['receipts'][0]).read_bytes() == receipt_bytes
    assert recovered.verify(identity)['done'] == 1
    source = completed['sources'][0]['filename']
    assert recovered.text(identity, source).startswith('Synthetic transcript.')
    assert recovered.export(identity, tmp_path / 'exported').is_dir()
    with pytest.raises(PermissionError, match='Restored transcription job'):
        recovered.run(identity)
    with pytest.raises(PermissionError, match='Restored transcription engine'):
        recovered.review_restored(identity)


def test_restore_requires_explicit_runtime_and_job_revalidation(prepared, tmp_path):
    app, workspace, service, identity, worker, models = prepared
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    recovered = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum)).transcription(workspace)
    recovered.configure(Path(sys.executable), worker, models)
    with pytest.raises(PermissionError, match='Restored transcription job'):
        recovered.run(identity)
    assert recovered.review_restored(identity)['restore_requires_review'] is False
    assert recovered.run(identity, timeout=30)['status'] == 'completed'


@pytest.mark.parametrize('record,field', [('engine','version'), ('engine','restore_requires_revalidation'),
                                        ('job','version'), ('job','restore_requires_review')])
def test_backup_rejects_malformed_typed_restoration_markers(prepared, tmp_path, record, field):
    app, _, service, identity, _, _ = prepared
    path = service.root / 'engine.json' if record == 'engine' else service._path(identity) / 'job.json'
    value = read_json(path)
    value[field] = True if field == 'version' else 1
    atomic_json(path, value, replace=True)
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_unmodified_worker_is_rejected_before_execution(prepared, tmp_path):
    _, _, service, _, _, models = prepared
    original = tmp_path / 'old-worker.py'
    original.write_text("raise AssertionError('This file must never execute during registration')\n", encoding='utf-8')
    with pytest.raises(ValueError, match='incompatible'):
        service.configure(Path(sys.executable), original, models)
    with pytest.raises(ValueError):
        declared_capabilities('CARR_TRANSCRIPTION_CAPABILITIES = make_unreviewed_capabilities()')


def test_missing_local_model_fails_without_launching_worker(prepared, monkeypatch):
    from carr_library import transcription
    _, _, service, identity, _, models = prepared
    (models / 'small' / 'model.bin').unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('Worker must not launch for an incomplete model.')
    monkeypatch.setattr(transcription, 'run_process', forbidden)
    with pytest.raises(ValueError, match='complete selected local model'):
        service.run(identity)
    assert service.get(identity)['status'] == 'ready'


def test_configured_interpreter_preserves_virtual_environment_launch_path(prepared):
    service = prepared[2]
    assert service.configuration()['python'] == str(Path(sys.executable).absolute())


def test_export_preserves_concurrently_created_empty_destination(prepared, tmp_path, monkeypatch):
    from carr_library import transcription
    service, identity = prepared[2:4]
    assert service.run(identity, timeout=30)['status'] == 'completed'
    destination = tmp_path / 'raced-export'
    original = transcription.atomic_json
    raced = []
    def publish_manifest(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.name == 'transcription-manifest.json':
            destination.mkdir()
            raced.append(destination.stat().st_ino)
        return result
    monkeypatch.setattr(transcription, 'atomic_json', publish_manifest)
    with pytest.raises(FileExistsError):
        service.export(identity, destination)
    assert raced and destination.stat().st_ino == raced[0]
    assert list(destination.iterdir()) == []


def test_edit_publication_rechecks_scope_and_retains_provenance(prepared, monkeypatch):
    app, workspace, service, identity, _, _ = prepared
    job = service.run(identity, timeout=30)
    source = job['sources'][0]['filename']
    original = service.verify
    def archive_after_verify(*args, **kwargs):
        result = original(*args, **kwargs)
        app.archive_workspace(workspace)
        return result
    monkeypatch.setattr(service, 'verify', archive_after_verify)
    with pytest.raises(ValueError, match='archived'):
        service.save_edit(identity, source, 'Unpublished text')
    assert not (service.jobs_root / identity / 'edits').exists()

