"""Common V4 backup with actual controlled child jobs; no ASR quality claim."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.jobs import project_lock
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.storage.revisions import Storage
from carr_library.transcription import (TranscriptionBudget, TranscriptionControl,
                                       TranscriptionOptions, TranscriptionService)
from carr_library.transcription import backup
from carr_library.transcription.files import canonical
from carr_library.ui.transcription import TranscriptionActions, describe_request


def prepare(setup, *, complete=False, mode='valid'):
    service, workspace, profile, source = setup(mode)
    job = service.prepare(workspace, profile, [source],
        options=TranscriptionOptions(language='uk', formats=('txt', 'srt', 'vtt', 'json')))
    receipt = service.run(job.id, approved_request_sha256=job.request_sha256) if complete else None
    return service, job, receipt, profile, source


def manifest(path):
    return json.loads((path / 'manifest.json').read_text('utf-8'))


def rehash(path, name, raw):
    """A deliberately new independent token must still pass typed validation."""
    (path / name).write_bytes(raw)
    value = manifest(path)
    entry = next(row for row in value['sidecars'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = canonical(value)
    (path / 'manifest.json').write_bytes(encoded)
    return sha256(encoded).hexdigest()


def test_default_common_v4_preserves_completed_and_pending_jobs_without_private_runtime(setup, tmp_path):
    service, job, receipt, profile, source = prepare(setup, complete=True)
    pending = service.prepare(job.workspace, profile, [source])
    directory = service.jobs_root / job.id
    retained = {path.relative_to(service.root).as_posix(): path.read_bytes()
                for path in directory.rglob('*') if path.is_file()
                and (path.name in ('request.json', 'receipt.json')
                     or path.is_relative_to(directory / 'inputs') or path.is_relative_to(directory / 'results'))}
    for name in ('cache/private-state.bin', 'temp/unfinished.tmp', 'logs/diagnostic.txt'):
        (directory / name).write_bytes(b'SYNTHETIC EXCLUDED PRIVATE STATE')
    unavailable = profile.with_name('profile-unavailable.json')
    profile.rename(unavailable)  # Backup and derivative reading never need this runtime.
    source.unlink()  # Owned snapshots, not the original live input, are portable.
    destination = tmp_path / 'snapshot'
    token = service.app.backup(destination)
    value = manifest(destination)
    assert value['version'] == 4 and value['generated_audio_included'] is False
    names = {row['filename'] for row in value['sidecars']}
    assert names == set(retained) | {
        f'transcription-jobs/{pending.id}/request.json',
        f'transcription-jobs/{pending.id}/inputs/source-00001.wav'}
    assert all((destination / name).read_bytes() == raw for name, raw in retained.items())
    before = {path.relative_to(destination).as_posix(): path.read_bytes()
              for path in destination.rglob('*') if path.is_file()}
    assert verify_backup(destination, token) == value
    assert before == {path.relative_to(destination).as_posix(): path.read_bytes()
                      for path in destination.rglob('*') if path.is_file()}
    restored = restore_backup(destination, tmp_path / 'restored', token)
    reopened = TranscriptionService(LibraryApp(restored))
    for saved in (job, pending):
        reviewed, request = reopened.review(saved.id)
        assert reviewed.requires_reapproval and reviewed.request_sha256 == saved.request_sha256
        assert request['data_root'] == str(service.root) and request['profile_path'] == str(profile)
        assert 'RESTORED INERT' in describe_request(reviewed)
        with pytest.raises(PermissionError, match='inert'):
            reopened.run(saved.id, approved_request_sha256=saved.request_sha256)
    assert all((restored / name).read_bytes() == raw for name, raw in retained.items())
    verified = reopened.verify(job.id, expected_receipt_sha256=receipt.receipt_sha256)
    assert verified.quotation_authority is False and len(verified.outputs) == 4
    assert reopened.read_text(verified) == ((job.source_names[0], 'Контрольний текст — synthetic protocol, not ASR.'),)
    assert {item.id for item in reopened.jobs(workspace=job.workspace)} == {job.id, pending.id}
    # A second backup preserves original reviewed bytes, not synthetic rewritten requests.
    second = tmp_path / 'second-snapshot'
    second_token = reopened.app.backup(second)
    assert manifest(second)['sidecars'] == value['sidecars']
    assert verify_backup(second, second_token)['sidecars'] == value['sidecars']
    assert not any(path.name == 'restored.json' for path in second.rglob('*'))


def test_recovered_inputs_need_new_profile_request_and_separate_run_review(setup, tmp_path):
    service, job, _, profile, source = prepare(setup)
    original_request = (service.jobs_root / job.id / 'request.json').read_bytes()
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    restored = restore_backup(snapshot, tmp_path / 'restored', token)
    reopened = TranscriptionService(LibraryApp(restored))
    source.unlink()
    current_profile = profile.with_name('explicit-current-profile.json')
    profile.rename(current_profile)
    frame = SimpleNamespace(library=reopened.app, active=(job.workspace, 'Restored library'), _closing=False)
    actions = TranscriptionActions(frame, service=reopened)
    saved, _ = reopened.review(job.id)
    actions.accept(saved)
    actions.invalidate()  # Editing the profile/options withdraws Run approval, not input selection.
    assert actions.recovered_source is saved and actions.prepared is None
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(PermissionError, match='review'):
            actions.run(executor)
        selected_options = replace(job.options, task='translate', language='en')
        generation, future = actions.reprepare(executor, current_profile,
            options=selected_options, budget=TranscriptionBudget())
        fresh = future.result(timeout=10)
        assert actions.finish(generation)
        assert fresh.id != job.id and fresh.request_sha256 != job.request_sha256
        assert not fresh.requires_reapproval and fresh.source_names == job.source_names
        assert fresh.profile_path == str(current_profile)
        assert (reopened.jobs_root / job.id / 'request.json').read_bytes() == original_request
        assert not (reopened.jobs_root / fresh.id / 'logs' / 'fixture.started').exists()
        with pytest.raises(PermissionError, match='review'):
            actions.run(executor)  # Prepare completion is not consent to execute.
        actions.accept(fresh)
        generation, future = actions.run(executor)
        receipt = future.result(timeout=10)
        assert actions.finish(generation)
    assert reopened.read_text(receipt) == ((job.source_names[0], 'Controlled English translation.'),)
    assert not (reopened.jobs_root / job.id / 'receipt.json').exists()


def test_same_path_restore_cannot_reuse_old_execution_approval(setup, tmp_path):
    service, job, _, _, _ = prepare(setup)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    original = service.root
    original.rename(tmp_path / 'preserved-old-root')
    restored = restore_backup(snapshot, original, token)
    reopened = TranscriptionService(LibraryApp(restored))
    assert reopened.review(job.id)[0].requires_reapproval
    with pytest.raises(PermissionError, match='inert'):
        reopened.run(job.id, approved_request_sha256=job.request_sha256)
    assert not (reopened.jobs_root / job.id / 'job_config.json').exists()


def test_archived_workspace_job_is_preserved_but_not_read_authorized(setup, tmp_path):
    service, job, _, _, _ = prepare(setup)
    service.app.archive_workspace(job.workspace)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    with pytest.raises((PermissionError, ValueError)):
        reopened.review(job.id)
    reopened.app.restore_workspace(job.workspace)
    assert reopened.review(job.id)[0].requires_reapproval


@pytest.mark.parametrize('target', ['input', 'output', 'receipt', 'request', 'unknown', 'hardlink'])
def test_source_corruption_refuses_backup_without_completed_destination(setup, tmp_path, target):
    service, job, _, _, _ = prepare(setup, complete=True)
    directory = service.jobs_root / job.id
    if target in ('input', 'output'):
        name = 'inputs/source-00001.wav' if target == 'input' else 'results/inputs/source-00001.txt'
        path = directory / name
        previous = path.stat()
        raw = path.read_bytes()
        path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif target == 'hardlink':
        os.link(directory / 'inputs/source-00001.wav', tmp_path / 'external-alias.wav')
    elif target == 'unknown':
        (directory / 'unregistered-user-note.txt').write_text('Must not silently discard.', encoding='utf-8')
    else:
        path = directory / (target + '.json')
        value = json.loads(path.read_text('utf-8'))
        if target == 'receipt':
            value['quotation_authority'] = True
        else:
            value['sources'][0]['name'] = '../outside.wav'
        path.write_bytes(canonical(value))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        service.app.backup(destination)
    assert not destination.exists()


@pytest.mark.parametrize('target', ['input', 'output', 'request', 'receipt'])
def test_backup_tamper_fails_independent_digest_and_typed_revalidation(setup, tmp_path, target):
    service, job, _, _, _ = prepare(setup, complete=True)
    snapshot = tmp_path / 'snapshot'
    old_token = service.app.backup(snapshot)
    relative = {'input': 'inputs/source-00001.wav', 'output': 'results/inputs/source-00001.txt',
                'request': 'request.json', 'receipt': 'receipt.json'}[target]
    name = f'transcription-jobs/{job.id}/{relative}'
    raw = (snapshot / name).read_bytes()
    if target in ('input', 'output'):
        changed = bytes([raw[0] ^ 1]) + raw[1:]
    else:
        value = json.loads(raw)
        if target == 'request':
            value['workspace'] = 'missing-workspace'
        else:
            value['outputs'][0]['path'] = 'results/inputs/../../outside.txt'
        changed = canonical(value)
    new_token = rehash(snapshot, name, changed)
    for token in (old_token, new_token):
        with pytest.raises(ValueError):
            restore_backup(snapshot, tmp_path / 'rejected', token)
        assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('lock_child', [False, True])
def test_active_parent_or_orphan_worker_lock_prevents_snapshot(setup, tmp_path, lock_child):
    service, job, _, _, _ = prepare(setup)
    directory = service.jobs_root / job.id
    lock_root = directory / 'worker-lock' if lock_child else directory
    with project_lock(lock_root):
        with pytest.raises(ValueError, match='active'):
            service.app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert service.app.backup(tmp_path / 'retry')


def test_snapshot_byte_budget_and_midstream_cancellation_leave_no_completed_backup(setup, tmp_path):
    service, job, _, _, _ = prepare(setup)
    with closing(Storage(service.app.database)) as store:
        stage = tmp_path / 'stage'
        stage.mkdir()
        with pytest.raises(ValueError, match='budget'):
            backup.snapshot(store.db, service.root, stage, max_bytes=1)
        assert not any(stage.iterdir())
    stopped = False
    def cancelled():
        nonlocal stopped
        # Observe an actual copied input, then request cancellation before publication.
        candidates = tmp_path.glob('.carr-library-backup-*/transcription-jobs/*/inputs/source-00001.wav')
        stopped = stopped or any(candidates)
        return stopped
    with pytest.raises(InterruptedError):
        service.app.backup(tmp_path / 'cancelled', cancelled=cancelled)
    assert stopped and not (tmp_path / 'cancelled').exists()
    assert service.app.backup(tmp_path / 'retry')


def test_pending_worker_files_are_excluded_and_require_fresh_processing(setup, tmp_path):
    service, job, _, profile, _ = prepare(setup, mode='slow')
    control = TranscriptionControl()
    control.pause()
    def progress(event):
        if event.phase.startswith('Pause'):
            control.stop()
    with pytest.raises(InterruptedError):
        service.run(job.id, approved_request_sha256=job.request_sha256,
                    control=control, on_progress=progress)
    directory = service.jobs_root / job.id
    assert not (directory / 'receipt.json').exists()
    partial = directory / 'results' / 'inputs' / 'source-00001.txt'
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b'unaccepted partial derivative')
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    assert {Path(item['filename']).name for item in manifest(snapshot)['sidecars']} == {
        'request.json', 'source-00001.wav'}
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    fresh = reopened.prepare_recovered(job.id, profile, approved_source_request_sha256=job.request_sha256)
    assert fresh.id != job.id and not fresh.requires_reapproval
    assert not (reopened.jobs_root / fresh.id / 'receipt.json').exists()
    assert not (reopened.jobs_root / job.id / 'results').exists()


def test_recovered_preparation_rechecks_input_token_and_cancellation(setup, tmp_path):
    service, job, _, profile, _ = prepare(setup)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    with pytest.raises(PermissionError, match='changed'):
        reopened.prepare_recovered(job.id, profile, approved_source_request_sha256='0' * 64)
    with pytest.raises(InterruptedError):
        reopened.prepare_recovered(job.id, profile, approved_source_request_sha256=job.request_sha256,
                                   cancelled=lambda: True)
    retained = reopened.jobs_root / job.id / 'inputs' / 'source-00001.wav'
    retained.write_bytes(b'changed retained bytes')
    with pytest.raises(ValueError, match='snapshot changed'):
        reopened.prepare_recovered(job.id, profile, approved_source_request_sha256=job.request_sha256)
    assert [path.name for path in reopened.jobs_root.iterdir()] == [job.id]


def test_zero_length_verified_text_and_subtitles_remain_valid_derivatives(setup, tmp_path):
    service, job, receipt, _, _ = prepare(setup, complete=True, mode='empty')
    assert service.read_text(receipt) == ((job.source_names[0], ''),)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    empty = [row['filename'] for row in manifest(snapshot)['sidecars'] if row['size_bytes'] == 0]
    assert {Path(name).suffix for name in empty} == {'.txt', '.srt'}
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    assert reopened.read_text(reopened.verify(job.id,
        expected_receipt_sha256=receipt.receipt_sha256)) == ((job.source_names[0], ''),)


def test_windows_provenance_strings_restore_without_resolving_external_paths(setup, tmp_path):
    service, job, _, _, _ = prepare(setup)
    snapshot = tmp_path / 'snapshot'
    service.app.backup(snapshot)
    name = f'transcription-jobs/{job.id}/request.json'
    value = json.loads((snapshot / name).read_bytes())
    value.update(data_root=r'C:\Historical Library', profile_path=r'D:\Private Runtime\profile.json')
    raw = canonical(value)
    token = rehash(snapshot, name, raw)
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    saved, request = reopened.review(job.id)
    assert request == value and saved.requires_reapproval
    assert (reopened.root / name).read_bytes() == raw
    with pytest.raises(PermissionError, match='inert'):
        reopened.run(job.id, approved_request_sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('field,value', [('request_sha256', '0' * 64), ('requires_reapproval', False),
                                        ('id', '0' * 32)])
def test_modified_restore_marker_never_grants_execution(setup, tmp_path, field, value):
    service, job, _, _, _ = prepare(setup)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    reopened = TranscriptionService(LibraryApp(restore_backup(snapshot, tmp_path / 'restored', token)))
    path = reopened.jobs_root / job.id / 'restored.json'
    marker = json.loads(path.read_bytes())
    marker[field] = value
    path.write_bytes(canonical(marker))
    with pytest.raises(ValueError, match='marker'):
        reopened.run(job.id, approved_request_sha256=job.request_sha256)
    assert not (reopened.jobs_root / job.id / 'job_config.json').exists()


def test_marker_publication_failure_keeps_restored_root_explicitly_incomplete(setup, tmp_path, monkeypatch):
    from carr_library.library_backup import INCOMPLETE
    service, _, _, _, _ = prepare(setup)
    snapshot = tmp_path / 'snapshot'
    token = service.app.backup(snapshot)
    def fail_marker(path, raw):
        assert path.name == 'restored.json'
        assert path.with_name('request.json').stat().st_nlink == 1
        raise OSError('Controlled marker publication interruption')
    monkeypatch.setattr(backup, 'write_new', fail_marker)
    destination = tmp_path / 'restored'
    with pytest.raises(OSError, match='marker publication interruption'):
        restore_backup(snapshot, destination, token)
    assert (destination / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(destination)
    assert verify_backup(snapshot, token)


def test_after_copy_source_mutation_is_detected_even_with_preserved_mtime(setup, tmp_path, monkeypatch):
    service, job, _, _, _ = prepare(setup)
    source = service.jobs_root / job.id / 'inputs' / 'source-00001.wav'
    original = source.read_bytes()
    before = source.stat()
    real_copy = backup._copy
    def change_after_copy(incoming, target, **kwargs):
        result = real_copy(incoming, target, **kwargs)
        if incoming == source:
            source.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result
    monkeypatch.setattr(backup, '_copy', change_after_copy)
    with pytest.raises(ValueError, match='changed'):
        service.app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert source.read_bytes() != original
