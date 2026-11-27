"""Actual canonical assistant snapshots, native ownership and restored authority."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
from threading import Event

from filelock import FileLock, Timeout
import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.state import (
    read_json, snapshot_job_guard, state_write_guard,
)
from carr_library.assistant.validation import validate_state_document
from carr_library.library_backup import create_backup, restore_backup, verify_backup


@pytest.fixture
def local_job(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scoped canonical workflow')
    app.add_text(workspace, 'Exact source', 'Незмінне джерело. Exact source.')
    output = tmp_path / 'selected-output'
    output.mkdir()
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace, output_root=output)))
    return app, workspace, service, identity, output


def _relative(app, service, identity):
    return service.store.path(identity).relative_to(app.root).as_posix()


def _resign(backup, relative, value):
    path = backup / 'sidecars' / relative
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['path'] == relative)
    entry.update(size_bytes=path.stat().st_size, sha256=sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    return sha256(manifest_path.read_bytes()).hexdigest()


def test_actual_self_backup_restore_rebackup_preserves_scope_and_checkpoint(local_job, tmp_path):
    app, workspace, service, _, output = local_job
    identity = service.submit(service.preview(
        'List books\nBackup "checked"',
        Scope(workspace, output_root=output, allow_full_backup=True)))
    result = service.run(identity)
    assert result.status == 'complete', result.message
    backup = output / 'checked'
    digest = result.results[1]['manifest_sha256']
    manifest = verify_backup(backup, digest)
    assert manifest['version'] == 5
    relative = _relative(app, service, identity)
    raw = (backup / 'sidecars' / relative).read_bytes()
    saved = validate_state_document(Path(relative).name, raw)
    assert saved['status'] == 'running'
    assert [step['status'] for step in saved['steps']] == ['complete', 'running']
    assert saved['data_dir'] == str(app.root)
    assert service.resume(identity).status == 'complete'

    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert (restored / relative).read_bytes() == raw
    reopened = AssistantService(LibraryApp(restored))
    assert reopened.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='another data directory'):
        reopened.resume(identity)
    assert (restored / relative).read_bytes() == raw
    assert not reopened.store.path(identity).with_suffix('.control.json').exists()

    second = tmp_path / 'second-backup'
    second_digest = create_backup(LibraryApp(restored), second)
    verify_backup(second, second_digest)
    assert (second / 'sidecars' / relative).read_bytes() == raw


def test_self_backup_pauses_during_streaming_copy_and_resumes_without_overwrite(local_job, monkeypatch):
    from carr_library import library_backup
    app, workspace, service, _, output = local_job
    # A valid queued plan is large enough to exercise several actual disk reads;
    # none of these saved searches is executed or replaced by a fake snapshot.
    saved_id = service.submit(Plan(Scope(workspace, output_root=output), tuple(
        Step('library.catalog', {'title': 'Saved filter ' + 'x' * 32750}) for _ in range(65))))
    saved_path = service.store.path(saved_id)
    original_bytes = saved_path.read_bytes()
    assert len(original_bytes) > 2 * 1024 * 1024
    validate_state_document(saved_path.name, original_bytes)
    identity = service.submit(service.preview(
        'List books\nBackup "paused-copy"',
        Scope(workspace, output_root=output, allow_full_backup=True)))
    event = Event()
    original_copy = library_backup._copy_sidecar
    partials = []

    def interrupt_copy(root, name, target, stamp, cancelled):
        if root != app.root or name != _relative(app, service, saved_id):
            return original_copy(root, name, target, stamp, cancelled)
        polls = 0
        def stop_after_first_chunk():
            nonlocal polls
            polls += 1
            if polls == 2:
                assert target.stat().st_size == 1024 * 1024
                partials.append(target)
                event.set()
            return cancelled()
        return original_copy(root, name, target, stamp, stop_after_first_chunk)

    with monkeypatch.context() as during_copy:
        during_copy.setattr(library_backup, '_copy_sidecar', interrupt_copy)
        result = service.run(identity, cancelled=event.is_set)
    assert result.status == 'paused', result.message
    assert result.completed == 1
    assert 'cancelled' in result.message
    assert len(partials) == 1 and not partials[0].exists()
    target = output / 'paused-copy'
    assert not target.exists()
    assert not list(output.glob('.assistant-backup-*'))
    assert not list(output.glob('.carr-library-backup-*'))
    assert saved_path.read_bytes() == original_bytes
    durable = service.store.load(identity)
    assert durable['status'] == 'paused'
    assert [item['status'] for item in durable['steps']] == ['complete', 'running']
    # The worker released both native guards before saving its paused state.
    with state_write_guard(service.store.directory), service.store.lock(identity):
        pass

    reopened = AssistantService(LibraryApp(app.root))
    resumed = reopened.resume(identity)
    assert resumed.status == 'complete', resumed.message
    verify_backup(target, resumed.results[1]['manifest_sha256'])
    published = {path.relative_to(target): sha256(path.read_bytes()).digest()
                 for path in target.rglob('*') if path.is_file()}
    # Completed resume verifies the existing bytes; it must not take a new copy.
    monkeypatch.setattr(library_backup, 'create_backup',
                        lambda *a, **k: pytest.fail('A published backup was overwritten.'))
    assert reopened.resume(identity).status == 'complete'
    assert {path.relative_to(target): sha256(path.read_bytes()).digest()
            for path in target.rglob('*') if path.is_file()} == published


@pytest.mark.parametrize('command', ['pause', 'cancel'])
def test_control_json_roundtrip_and_stale_scope_cannot_resume(local_job, tmp_path, command):
    app, _, service, identity, _ = local_job
    getattr(service, command)(identity)
    job = service.store.path(identity)
    control = job.with_suffix('.control.json')
    original = {path.name: path.read_bytes() for path in (job, control)}
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    reopened = AssistantService(LibraryApp(restored))
    for name, raw in original.items():
        assert (reopened.store.directory / name).read_bytes() == raw
    assert reopened.store.control(identity) == command
    assert reopened.get(identity).status == 'attention'
    with pytest.raises(PermissionError):
        reopened.run(identity)


@pytest.mark.parametrize('lock_name', ['job', 'state'])
def test_busy_native_job_or_publication_lock_refuses_before_database(local_job, tmp_path, monkeypatch, lock_name):
    from carr_library import library_backup
    app, _, service, identity, _ = local_job
    path = (service.store.path(identity).with_suffix('.lock') if lock_name == 'job'
            else service.store.directory / '.state.lock')
    monkeypatch.setattr(library_backup, 'Storage',
                        lambda *a, **k: pytest.fail('opened database before assistant lock'))
    with FileLock(path):
        with pytest.raises(ValueError, match='assistant.*Retry'):
            create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('command', ['pause', 'cancel'])
def test_concurrent_control_cannot_change_checkpoint_during_real_copy(local_job, tmp_path, monkeypatch, command):
    from carr_library import library_backup
    app, _, service, identity, _ = local_job
    service.store.control(identity, 'run')
    relative = _relative(app, service, identity)
    before = service.store.path(identity).with_suffix('.control.json').read_bytes()
    original = library_backup._copy_sidecar
    checked = []
    def guarded(root, name, target, stamp, cancelled):
        if root == app.root and name == relative:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with pytest.raises(RuntimeError, match='state is being saved'):
                    pool.submit(getattr(service, command), identity).result(timeout=5)
            checked.append(name)
        return original(root, name, target, stamp, cancelled)
    monkeypatch.setattr(library_backup, '_copy_sidecar', guarded)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    assert checked == [relative]
    verify_backup(backup, digest)
    control = service.store.path(identity).with_suffix('.control.json')
    assert control.read_bytes() == before
    assert (backup / 'sidecars' / control.relative_to(app.root)).read_bytes() == before
    getattr(service, command)(identity)
    assert service.store.control(identity) == command


def test_snapshot_borrows_only_actual_guard_and_execution_remains_nonreentrant(local_job):
    _, _, service, identity, _ = local_job
    native_path = service.store.path(identity).with_suffix('.lock')
    with service.store.lock(identity):
        with snapshot_job_guard(service.store.directory, identity):
            with pytest.raises(Timeout):
                with FileLock(native_path).acquire(timeout=0):
                    pytest.fail('Native lock was released during a borrowed snapshot.')
        with pytest.raises(RuntimeError, match='already running'):
            with service.store.lock(identity):
                pytest.fail('Nested job execution borrowed a snapshot exception.')


def _enter_snapshot(directory, identity):
    with snapshot_job_guard(directory, identity):
        raise AssertionError('Another owner borrowed the active native guard.')


def test_copied_thread_released_context_and_different_pid_cannot_borrow(local_job, monkeypatch):
    import carr_library.assistant.state as module
    _, _, service, identity, _ = local_job
    directory = service.store.directory
    native_path = service.store.path(identity).with_suffix('.lock')
    process_id = module.os.getpid()
    with service.store.lock(identity):
        copied = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(RuntimeError):
                pool.submit(copied.run, _enter_snapshot, directory, identity).result(timeout=5)
        with monkeypatch.context() as changed:
            changed.setattr(module.os, 'getpid', lambda: process_id + 1)
            with pytest.raises(RuntimeError):
                _enter_snapshot(directory, identity)
    with FileLock(native_path):
        with pytest.raises(RuntimeError):
            copied.run(_enter_snapshot, directory, identity)


def _child_snapshot(directory, identity, receiver, sender):
    receiver.close()
    try:
        try:
            with snapshot_job_guard(directory, identity):
                message = 'unexpected acquisition'
        except RuntimeError:
            message = 'native lock retained'
        except BaseException as exc:
            message = f'{type(exc).__name__}: {exc}'
        sender.send(message)
    finally:
        sender.close()


def test_native_snapshot_lock_survives_fork_or_spawn_boundary(local_job):
    _, _, service, identity, _ = local_job
    method = 'fork' if 'fork' in multiprocessing.get_all_start_methods() else 'spawn'
    context = multiprocessing.get_context(method)
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_snapshot,
                            args=(service.store.directory, identity, receiver, sender))
    try:
        with service.store.lock(identity):
            child.start()
            sender.close()
            assert receiver.poll(10), 'Child did not report its native-lock outcome.'
            assert receiver.recv() == 'native lock retained'
            child.join(10)
            assert child.exitcode == 0
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
                child.join(10)
            child.close()
        receiver.close()
        sender.close()


def test_verification_streams_assistant_bytes_without_source_write_or_lock(local_job, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, _, service, identity, _ = local_job
    service.pause(identity)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    before = {path.relative_to(backup): path.read_bytes()
              for path in backup.rglob('*') if path.is_file()}
    original = library_backup._copy_sidecar
    targets = []
    def read_only(root, name, target, stamp, cancelled):
        assert target is None
        targets.append(name)
        return original(root, name, target, stamp, cancelled)
    monkeypatch.setattr(library_backup, '_copy_sidecar', read_only)
    verify_backup(backup, digest)
    assert len(targets) == 2
    assert {path.relative_to(backup): path.read_bytes()
            for path in backup.rglob('*') if path.is_file()} == before


def test_resigned_assistant_workspace_must_exist_in_snapshot(local_job, tmp_path):
    app, _, service, identity, _ = local_job
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    relative = _relative(app, service, identity)
    value = json.loads((backup / 'sidecars' / relative).read_bytes())
    value['workspace'] = value['plan']['scope']['workspace_id'] = 'forged-workspace'
    value['plan_sha256'] = sha256(canonical(value['plan']).encode('utf-8')).hexdigest()
    digest = _resign(backup, relative, value)
    assert validate_state_document(Path(relative).name, (backup / 'sidecars' / relative).read_bytes())
    with pytest.raises(ValueError, match='workspace disagrees with the backup database'):
        verify_backup(backup, digest)
    with pytest.raises(ValueError, match='workspace disagrees with the backup database'):
        restore_backup(backup, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()


def test_orphan_controls_and_byte_or_count_limits_fail_before_publication(local_job, tmp_path):
    app, _, service, identity, _ = local_job
    service.pause(identity)
    with pytest.raises(ValueError, match='file budget'):
        create_backup(app, tmp_path / 'count-limited', max_sidecar_files=1)
    assert not (tmp_path / 'count-limited').exists()
    service.store.path(identity).unlink()
    with pytest.raises(ValueError, match='no saved job'):
        create_backup(app, tmp_path / 'orphan')
    assert not (tmp_path / 'orphan').exists()


@pytest.mark.parametrize('raw', [
    b'{"command":"run","command":"pause"}',
    b'{"command":NaN}',
], ids=('duplicate', 'nonfinite'))
def test_live_checkpoint_reader_matches_strict_backup_json(raw, tmp_path):
    path = tmp_path / 'record.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        read_json(path)


def test_assistant_namespace_excludes_only_known_transients_and_external_folders(local_job, tmp_path):
    app, _, service, identity, output = local_job
    temporary = '.' + identity + '.json.' + 'a' * 32 + '.tmp'
    (service.store.directory / temporary).write_text('unpublished', encoding='utf-8')
    (output / 'external.txt').write_text('not a backup input', encoding='utf-8')
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = verify_backup(backup, digest)
    assert [item['path'] for item in manifest['sidecars']] == [_relative(app, service, identity)]
    # The managed checkpoint namespace is stricter than the library root:
    # unknown committed-looking files cannot silently disappear from recovery.
    (service.store.directory / 'neighbor.txt').write_text('not registered', encoding='utf-8')
    with pytest.raises(ValueError, match='Unexpected file'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
