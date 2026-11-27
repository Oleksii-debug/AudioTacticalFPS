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
    assert task.event.completed == 1
    assert task.event.phase == 'stopped'
    assert not task.event.terminal


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
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=checksum))
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
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=digest)).transcription(workspace)
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
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=digest)).transcription(workspace)
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


def test_shared_reporter_binds_actual_child_until_verified_completion(prepared):
    from carr_library.progress import ProgressHub, current_progress
    from carr_library.transcription_host import observe_operation, report_progress, require_completed
    service, original = prepared[2:4]
    identity = service.submit([original])
    task = ProgressHub().start('Transcription', total=1)
    def operation():
        assert current_progress() is task
        return require_completed(service.run(identity, timeout=30,
            progress=lambda done, total, phase: report_progress(task, done, total, phase)))
    result = observe_operation(task, operation)
    assert result == service.get(identity) and result['status'] == 'completed'
    assert current_progress() is None
    assert task.event.state == 'complete' and task.event.verified
    assert task.event.percent == 100


@pytest.mark.parametrize('kind, expected', [
    ('interrupted', 'cancelled'), ('audio_cancelled', 'cancelled'), ('failure', 'failed'),
])
def test_shared_reporter_restores_context_for_cancelled_and_failed_operations(kind, expected):
    from carr_library.progress import ProgressHub, current_progress
    from carr_library.transcription_host import AudioCancelled, observe_operation
    error_type = {'interrupted': InterruptedError, 'audio_cancelled': AudioCancelled,
                  'failure': RuntimeError}[kind]
    parent = ProgressHub().start('Unrelated parent')
    task = parent.child('Transcription')
    error = error_type('Purpose-written boundary failure.')
    def operation():
        assert current_progress() is task
        raise error
    with parent.bind():
        with pytest.raises(error_type) as caught:
            observe_operation(task, operation)
        assert caught.value is error
        assert current_progress() is parent
    assert current_progress() is None
    assert task.event.state == expected and task.event.percent is None


def test_terminal_observer_failure_cannot_reverse_a_committed_actual_child(prepared, monkeypatch):
    from carr_library import progress
    from carr_library.transcription_host import observe_operation, require_completed
    service, original = prepared[2:4]
    identity = service.submit([original])
    task = progress.ProgressHub().start('Transcription')
    calls = []
    def failed_observer(*args, **kwargs):
        raise RuntimeError('Synthetic detached progress observer.')
    monkeypatch.setattr(progress, 'finish_result', failed_observer)
    def operation():
        calls.append(identity)
        return require_completed(service.run(identity, timeout=30))
    result = observe_operation(task, operation)
    assert calls == [identity]
    assert result == service.get(identity) and result['status'] == 'completed'
    assert service.verify(identity)['status'] == 'completed'
    assert progress.current_progress() is None


def test_new_json_publication_preserves_concurrent_destination(tmp_path, monkeypatch):
    from carr_library import durable_state
    target = tmp_path / 'new-state.json'
    publish = durable_state.publish_directory_no_replace
    def concurrent(stage, destination):
        Path(destination).write_bytes(b'Already owned by another operation.')
        return publish(stage, destination)
    monkeypatch.setattr(durable_state, 'publish_directory_no_replace', concurrent)
    with pytest.raises(FileExistsError):
        durable_state.atomic_json(target, {'version': 1})
    assert target.read_bytes() == b'Already owned by another operation.'
    assert not list(tmp_path.glob('.pending-*'))


def test_submit_holds_engine_lock_before_creating_a_workspace_namespace(prepared):
    from carr_library.durable_state import state_lock
    _, _, service, original, _ = prepared
    assert not service.jobs_root.exists()
    with state_lock(service.root, 'engine'):
        with pytest.raises(RuntimeError, match='already being changed'):
            service.submit([original])
    assert not service.jobs_root.exists()
    assert original.exists()
