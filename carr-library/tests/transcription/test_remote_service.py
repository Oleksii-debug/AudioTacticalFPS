"""Actual retained jobs and receipts; provider replies are explicitly synthetic."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.integrations import transcription_remote as module
from carr_library.integrations import transcription_remote_values as values
from carr_library.library_backup import create_backup, restore_backup

_spec = importlib.util.spec_from_file_location('remote_stt_job_fixture', Path(__file__).with_name('remote_fixture.py'))
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def complete(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    returned = owner.remote.run(identity, approved)
    return owner, approved, identity, returned, calls


def test_retained_remote_result_reuses_exact_editor_exports_and_existing_ledger(tmp_path, monkeypatch):
    owner, approved, identity, returned, calls = complete(tmp_path, monkeypatch)
    assert returned['version'] == 2 and returned['protocol'] == 'elevenlabs-stt-v1'
    assert returned['status'] == 'completed' and returned['done'] == 1
    assert returned['run_context'] is None and len(calls) == 1
    source = returned['sources'][0]['filename']
    original = owner.service._path(identity) / 'input' / source
    assert original.read_bytes() == owner.selected.read_bytes() == fixture.RAW
    assert owner.service.text(identity, source).strip() == fixture.TEXT
    revision = owner.service.save_edit(identity, source, 'Reviewed human derivative.')
    assert owner.service.edits(identity)[0]['id'] == revision
    assert owner.service.text(identity, source).strip() == fixture.TEXT
    exported = tmp_path / 'exports'
    owner.service.export(identity, exported)
    assert (exported / (source + '.txt')).read_text().strip() == fixture.TEXT
    assert (exported / (source + '.srt')).read_text().startswith('1\n00:00:00,000 --> 00:00:01,800')
    assert (exported / (source + '.vtt')).read_text().startswith('WEBVTT\n')
    raw = (exported / (source + '.json')).read_text()
    assert '"provider reported; not independently aligned"' in raw and 'synthetic-stt-secret' not in raw
    receipt = read_json(owner.service._path(identity) / returned['receipts'][0])
    assert receipt['local'] is False and receipt['actual_cost'] is None
    assert receipt['profile']['credential_ref'] is None and receipt['profile']['enabled'] is False
    assert owner.service.run(identity)['status'] == 'completed'
    assert len(calls) == 1 and len(fixture.Connection.requests) == 1
    assert owner.broker.snapshot()['leases'] == []
    assert owner.app.search(owner.workspace, 'unchanged')[0].quote == 'unchanged'


def test_actual_owned_child_uses_current_gateway_then_closes_before_return(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.real_child(monkeypatch)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    result = owner.remote.run(identity, approved, timeout=25)
    assert result['status'] == 'completed' and len(calls) == 1
    command, options = calls[0]
    assert options['owned_windows_job'] is (os.name == 'nt')
    assert not Path(command[-2]).parent.exists(), 'Request/approval staging survives child cleanup.'
    assert owner.broker.snapshot()['leases'] == []
    assert owner.service.verify(identity)['artifacts'] == result['artifacts']
    assert owner.selected.read_bytes() == fixture.RAW


def test_actual_child_total_deadline_retains_source_and_never_publishes_result(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.real_child(monkeypatch, delay=True)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    started = time.monotonic()
    with pytest.raises((RuntimeError, InterruptedError)):
        owner.remote.run(identity, approved, timeout=8.0)
    assert time.monotonic() - started < 30
    assert len(calls) == 1
    assert calls[0][1]['fixture_ready'] is True
    request_path = Path(calls[0][0][-2])
    assert not request_path.parent.exists()
    assert not (owner.service._path(identity) / 'results').exists()
    assert owner.service.get(identity)['status'] in ('failed', 'stopped')
    assert owner.broker.snapshot()['leases'] == []
    assert (owner.service._path(identity) / 'input' / owner.service.get(identity)['sources'][0]['filename']).read_bytes() == fixture.RAW


@pytest.mark.parametrize('phase', ['review', 'prepare', 'run'])
def test_cancelled_admission_never_creates_the_next_effect(tmp_path, monkeypatch, phase):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    if phase == 'review':
        with pytest.raises(InterruptedError):
            owner.remote.review(owner.selected, profile_id='stt_account', max_cost='1', cancelled=lambda: True)
        assert not owner.service.jobs_root.exists()
    else:
        approval = fixture.approval(owner)
        if phase == 'prepare':
            with pytest.raises(InterruptedError):
                owner.remote.submit(approval, cancelled=lambda: True)
            assert not owner.service.jobs_root.exists()
        else:
            identity = owner.remote.submit(approval)
            before = (owner.service._path(identity) / 'job.json').read_bytes()
            with pytest.raises(InterruptedError):
                owner.remote.run(identity, approval, cancelled=lambda: True)
            assert (owner.service._path(identity) / 'job.json').read_bytes() == before
            assert not (owner.service._path(identity) / 'results').exists()
    assert not calls and not fixture.Connection.requests


@pytest.mark.parametrize('changed', ['recording', 'profile', 'language', 'cost'])
def test_review_is_bound_to_exact_recording_profile_parameters_and_budget(tmp_path, monkeypatch, changed):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    review = owner.remote.review(owner.selected, profile_id='stt_account', max_cost='1')
    approved = module.approve(review, consent=True)
    if changed == 'recording':
        owner.selected.write_bytes(b'X' * len(fixture.RAW))
        action = lambda: owner.remote.submit(approved)
    elif changed == 'profile':
        from carr_library.assistant.profiles import ProfileStore
        ProfileStore(owner.app.root / 'state' / 'providers').save(fixture.profile(name='Changed account label'))
        action = lambda: owner.remote.submit(approved)
    else:
        review[changed if changed == 'language' else 'max_cost'] = 'uk' if changed == 'language' else '2'
        action = lambda: module.approve(review, consent=True)
    with pytest.raises((ValueError, PermissionError)):
        action()
    assert not calls and not fixture.Connection.requests
    assert not list(owner.service.jobs_root.glob('*/job.json'))


def test_review_mutation_after_approval_cannot_change_the_captured_transfer(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    reviewed = owner.remote.review(owner.selected, profile_id='stt_account', max_cost='1')
    approval = module.approve(reviewed, consent=True)
    reviewed['audio']['sha256'] = '0' * 64
    reviewed['language'] = 'uk'
    identity = owner.remote.submit(approval)
    result = owner.remote.run(identity, approval)
    assert result['remote']['payload']['language'] == 'auto'
    assert result['sources'][0]['sha256'] == sha256(fixture.RAW).hexdigest()
    assert len(calls) == 1


@pytest.mark.parametrize('member', ['source', 'text', 'json', 'receipt', 'ledger'])
def test_source_receipt_exports_and_exact_usage_tampering_are_refused(tmp_path, monkeypatch, member):
    owner, approval, identity, completed, calls = complete(tmp_path, monkeypatch)
    path = owner.service._path(identity)
    source = completed['sources'][0]['filename']
    if member == 'source': target = path / 'input' / source
    elif member == 'text': target = path / 'results' / (source + '.txt')
    elif member == 'json': target = path / 'results' / (source + '.json')
    elif member == 'receipt': target = path / completed['receipts'][0]
    else: target = owner.app.root / 'state' / 'providers' / 'usage' / (approval.authorization.id + '.usage.json')
    raw = target.read_bytes()
    target.write_bytes(raw.replace(b'complete', b'uncertain') if member == 'ledger' else raw + b'changed')
    with pytest.raises((ValueError, OSError)):
        owner.service.verify(identity)
    assert len(calls) == 1 and len(fixture.Connection.requests) == 1


def test_immutable_result_commit_survives_failed_checkpoint_and_reconciles_without_network(tmp_path, monkeypatch):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    approval = fixture.approval(owner)
    identity = owner.remote.submit(approval)
    actual_save = owner.service._save
    def fail_completion(path, value):
        if value['status'] == 'completed':
            raise OSError('Synthetic final checkpoint refusal.')
        return actual_save(path, value)
    monkeypatch.setattr(owner.service, '_save', fail_completion)
    with pytest.raises(module.RemoteTranscriptionCommitted) as failure:
        owner.remote.run(identity, approval)
    assert failure.value.result['status'] == 'completed'
    assert owner.service.get(identity)['status'] == 'running'
    assert owner.broker.snapshot()['leases'] == []
    monkeypatch.setattr(owner.service, '_save', actual_save)
    recovered = owner.service.run(identity)
    assert recovered['artifacts'] == failure.value.result['artifacts']
    assert recovered['receipts'] == failure.value.result['receipts']
    assert recovered['status'] == 'completed'
    assert len(calls) == 1 and len(fixture.Connection.requests) == 1


@pytest.mark.parametrize('completed', [False, True])
def test_complete_backup_restores_exact_source_and_inactive_remote_consent(tmp_path, monkeypatch, completed):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    approval = fixture.approval(owner)
    identity = owner.remote.submit(approval)
    if completed:
        original_result = owner.remote.run(identity, approval)
        source = original_result['sources'][0]['filename']
        owner.service.save_edit(identity, source, 'Independent edited derivative.')
    backup = tmp_path / 'backup'
    checksum = create_backup(owner.app, backup)
    destination = restore_backup(backup, tmp_path / 'restored', checksum)
    restored_app = LibraryApp(destination)
    from carr_library.transcription import TranscriptionService
    restored = TranscriptionService(restored_app, owner.workspace)
    saved = restored.get(identity)
    assert saved['remote']['restored'] is True
    assert saved['remote']['consent'] == values.REVIEW_REQUIRED
    retained = restored._path(identity) / 'input' / saved['sources'][0]['filename']
    assert retained.read_bytes() == fixture.RAW
    with pytest.raises(PermissionError, match='old consent is inactive'):
        restored.remote_transcription().run(identity, approval)
    if completed:
        assert restored.verify(identity)['artifacts'] == original_result['artifacts']
        assert restored.text(identity, source).strip() == fixture.TEXT
        assert restored.edits(identity)[0]['text'] == 'Independent edited derivative.'
    assert len(calls) == int(completed)
    assert restored_app.search(owner.workspace, 'unchanged')[0].quote == 'unchanged'


def test_real_same_path_library_replacement_is_refused_before_new_state(tmp_path):
    owner = fixture.prepared(tmp_path)
    approval = fixture.approval(owner)
    original = owner.app.root
    moved = original.with_name('moved-original')
    original.rename(moved)
    original.mkdir()
    canary = original / 'canary'
    canary.write_bytes(b'unchanged')
    try:
        with pytest.raises((PermissionError, ValueError, OSError)):
            owner.remote.submit(approval)
        assert list(original.iterdir()) == [canary]
        assert canary.read_bytes() == b'unchanged'
        assert not (moved / 'state' / 'transcription').exists()
    finally:
        canary.unlink()
        original.rmdir()
        moved.rename(original)


def test_unrelated_worker_metadata_cannot_enter_remote_backup_inventory(tmp_path, monkeypatch):
    owner, _, identity, completed, _ = complete(tmp_path, monkeypatch)
    path = owner.service._path(identity)
    foreign = path / 'job_status.json'
    foreign.write_text('{"state":"complete"}', encoding='utf-8')
    names = ['job.json', 'input/' + completed['sources'][0]['filename'], *completed['receipts'],
             *['results/' + row['path'] for row in completed['artifacts']], 'job_status.json']
    with pytest.raises(ValueError, match='undeclared worker'):
        values.verify_files(path, completed, names=names)


def test_approved_recording_cannot_be_retargeted_to_another_library_directory(tmp_path, monkeypatch):
    first = fixture.prepared(tmp_path / 'one')
    second = fixture.prepared(tmp_path / 'two')
    approved = fixture.approval(first)
    # Even identical workspace text, selected profile and original bytes do not
    # let a caller move a confirmed operation to another library object/root.
    calls = fixture.in_process(monkeypatch)
    with pytest.raises(PermissionError, match='another workspace'):
        second.remote.submit(approved)
    assert not calls and not second.service.jobs_root.exists()


@pytest.mark.parametrize('entry', ['worker', 'package'])
def test_actual_child_rejects_changed_current_entry_before_any_provider_effect(tmp_path, monkeypatch, entry):
    owner = fixture.prepared(tmp_path)
    calls = fixture.real_child(monkeypatch)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    original_command = module._worker_command
    def changed_command(*args):
        command = original_command(*args)
        assert command[1:4] == ['-I', '-B', '-c']
        command[10 if entry == 'worker' else 6] = '0' * 64
        return command
    monkeypatch.setattr(module, '_worker_command', changed_command)
    with pytest.raises(RuntimeError):
        owner.remote.run(identity, approved, timeout=25)
    assert len(calls) == 1
    assert not list((owner.app.root / 'state/providers').rglob('*.usage.json'))
    assert not (owner.service._path(identity) / 'results').exists()
    assert not Path(calls[0][0][-2]).parent.exists()
    assert owner.broker.snapshot()['leases'] == []
    assert owner.selected.read_bytes() == fixture.RAW


@pytest.mark.parametrize('change', ['missing', 'tampered'])
def test_returned_transcript_requires_current_completed_usage_before_publication(tmp_path, monkeypatch, change):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    actual = module.run_process
    observed = []
    def change_usage_after_child(command, **options):
        returned = actual(command, **options)
        response_path = Path(command[-2]).with_name('response.json')
        assert response_path.is_file(), 'The real Gateway result must exist before this race.'
        ledger = owner.app.root / 'state/providers/usage' / (approved.authorization.id + '.usage.json')
        value = read_json(ledger)
        row = value['operations'][json.loads(approved.review_json)['operation_id']]
        assert row['status'] == 'complete'
        observed.append(row['output_sha256'])
        if change == 'missing':
            ledger.unlink()
        else:
            row['status'] = 'uncertain'
            atomic_json(ledger, value, replace=True)
        return returned
    monkeypatch.setattr(module, 'run_process', change_usage_after_child)
    with pytest.raises((OSError, ValueError)):
        owner.remote.run(identity, approved)
    assert len(observed) == len(calls) == len(fixture.Connection.requests) == 1
    assert not (owner.service._path(identity) / 'results').exists()
    assert not list(owner.service._path(identity).glob('.pending-remote-*'))
    assert owner.service.get(identity)['status'] == 'failed'
    assert owner.broker.snapshot()['leases'] == []
    with pytest.raises(PermissionError, match='never automatically retried'):
        owner.remote.run(identity, approved)
    assert len(calls) == 1 and owner.selected.read_bytes() == fixture.RAW


@pytest.mark.parametrize('malformed', [None, [], 7])
def test_malformed_saved_job_is_refused_by_normal_and_passive_readers(tmp_path, monkeypatch, malformed):
    owner = fixture.prepared(tmp_path)
    calls = fixture.in_process(monkeypatch)
    approved = fixture.approval(owner)
    identity = owner.remote.submit(approved)
    path = owner.service._path(identity) / 'job.json'
    path.write_text(json.dumps(malformed), encoding='utf-8')
    with pytest.raises(ValueError):
        owner.service.get(identity)
    from carr_library.backup_transcription import _job
    with owner.app.read(owner.workspace) as database:
        with pytest.raises(ValueError):
            _job(database, malformed, sha256(owner.workspace.encode('utf-8')).hexdigest(), identity)
    assert path.read_text(encoding='utf-8') == json.dumps(malformed)
    assert not calls and not fixture.Connection.requests
