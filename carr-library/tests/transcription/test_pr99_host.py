"""Real host process budgets and typed restore boundaries; synthetic media only."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup
from carr_library.transcription_host import AudioBudget, run_process

_spec = importlib.util.spec_from_file_location('supplied_worker_protocol_for_host',
    Path(__file__).with_name('test_supplied_worker_bridge.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
prepared = _fixture.prepared


def test_stopped_receipt_deferral_respects_existing_monotonic_progress():
    from carr_library.progress import ProgressHub
    from carr_library.transcription_host import report_progress
    task = ProgressHub().start('Transcription', total=2)
    report_progress(task, 1, 2, 'running')
    report_progress(task, 0, 2, 'stopped')
    assert task.snapshot().completed == 1
    assert task.snapshot().phase == 'stopped'
    assert not task.snapshot().terminal


@pytest.mark.parametrize('code,budget,message', [
    ("from pathlib import Path; Path('large.bin').write_bytes(b'x'*200)",
     AudioBudget(max_output_bytes=64), 'disk budget'),
    ("from pathlib import Path; Path('one').touch(); Path('two').touch()",
     AudioBudget(max_staging_files=1), 'file budget'),
])
def test_actual_fast_child_cannot_evade_final_staging_budget(tmp_path, code, budget, message):
    with pytest.raises(RuntimeError, match=message):
        run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', code],
                    cwd=tmp_path, watch=(tmp_path,), budget=budget)


def test_ready_job_backup_restores_retained_media_without_engine_execution(prepared, tmp_path):
    app, workspace, service, original, _ = prepared
    identity = service.submit([original])
    expected = original.read_bytes()
    worker = Path(service.configuration()['worker'])
    worker.unlink()
    checksum = app.backup(tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum))
    recovered = restored.transcription(workspace)
    job = recovered.get(identity)
    assert job['status'] == 'ready'
    assert (recovered._path(identity) / 'input' / job['sources'][0]['filename']).read_bytes() == expected
    assert not worker.exists()
    assert recovered.configuration()['inference_qualified'] is False


@pytest.mark.parametrize('field', ['source_sha256', 'recognition_identity', 'original_text_sha256'])
def test_foreign_edit_binding_blocks_backup_without_publication(prepared, tmp_path, field):
    app, _, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    revision = service.save_edit(identity, job['sources'][0]['filename'], 'User edit.')
    path = service._path(identity) / 'edits' / (revision + '.json')
    value = read_json(path)
    value[field] = '0' * 64
    atomic_json(path, value, replace=True)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='source binding'):
        app.backup(destination)
    assert not destination.exists()


def test_unknown_transcription_state_cannot_disappear_from_backup(prepared, tmp_path):
    app, _, service, _, _ = prepared
    path = service.root / 'unrecognized.bin'
    path.write_bytes(b'Required unknown data must not be omitted.')
    with pytest.raises(ValueError, match='Unknown transcription'):
        app.backup(tmp_path / 'rejected')
    assert path.read_bytes() == b'Required unknown data must not be omitted.'
    assert not (tmp_path / 'rejected').exists()


def test_completed_backup_keeps_engine_receipts_and_omits_only_declared_cache(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    cache = service._path(identity) / 'logs' / 'cache'
    cache.mkdir(parents=True)
    (cache / 'rebuildable.json').write_text('{}', encoding='utf-8')
    receipt = service._path(identity) / job['receipts'][0]
    expected = sha256(receipt.read_bytes()).hexdigest()
    digest = app.backup(tmp_path / 'backup')
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)).transcription(workspace)
    assert recovered.verify(identity)['status'] == 'completed'
    assert sha256((recovered._path(identity) / job['receipts'][0]).read_bytes()).hexdigest() == expected
    assert not (recovered._path(identity) / 'logs').exists()
    assert (cache / 'rebuildable.json').read_text(encoding='utf-8') == '{}'


def test_running_job_backup_requires_stop_without_losing_originals(prepared, tmp_path):
    app, _, service, original, _ = prepared
    identity = service.submit([original])
    job = service.get(identity)
    job['status'] = 'running'
    atomic_json(service._path(identity) / 'job.json', job, replace=True)
    with pytest.raises(ValueError, match='Stop transcription'):
        app.backup(tmp_path / 'rejected')
    assert original.exists() and not (tmp_path / 'rejected').exists()


def test_escaped_edit_roundtrip_uses_serialized_json_limit(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    text = '\\' * (9 * 1024**2)
    revision = service.save_edit(identity, job['sources'][0]['filename'], text)
    path = service._path(identity) / 'edits' / (revision + '.json')
    assert len(text.encode('utf-8')) < 16 * 1024**2
    assert 16 * 1024**2 < path.stat().st_size < 32 * 1024**2
    digest = app.backup(tmp_path / 'backup')
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest)).transcription(workspace)
    assert recovered.edits(identity)[0]['text'] == text
    assert recovered.verify(identity)['status'] == 'completed'


@pytest.mark.parametrize('value', [None, 17, {}], ids=['null', 'number', 'object'])
def test_malformed_engine_checksum_is_a_typed_backup_error(prepared, tmp_path, value):
    app, _, service, _, _ = prepared
    configuration = service.configuration()
    configuration['worker_sha256'] = value
    atomic_json(service.root / 'engine.json', configuration, replace=True)
    with pytest.raises(ValueError, match='engine configuration'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()

def test_completed_run_recheck_observes_stop_after_real_output_read(prepared, monkeypatch):
    from carr_library import transcription
    from carr_library.transcription_host import AudioCancelled
    _, _, service, _, _ = prepared
    identity, completed = _fixture.completed(prepared)
    root = service._path(identity)
    names = ['job.json', 'job_status.json']
    names += completed['receipts']
    names += ['input/' + item['filename'] for item in completed['sources']]
    names += ['results/' + item['path'] for item in completed['artifacts']]
    before = {name: (root / name).read_bytes() for name in names}
    first = root / 'results' / completed['artifacts'][0]['path']
    actual_digest = transcription._checked_digest
    stop, observed = [False], []

    def request_stop_after_read(path, size, cancelled):
        result = actual_digest(path, size, cancelled)
        if Path(path).resolve() == first.resolve():
            observed.append(result)
            stop[0] = True
        return result

    with monkeypatch.context() as scope:
        scope.setattr(transcription, '_checked_digest', request_stop_after_read)
        with pytest.raises(AudioCancelled, match='cancelled'):
            service.run(identity, cancelled=lambda: stop[0])

    assert observed == [completed['artifacts'][0]['sha256']]
    assert service.get(identity) == completed
    assert service.get(identity)['status'] == 'completed'
    assert {name: (root / name).read_bytes() for name in names} == before
    assert service.verify(identity) == completed

