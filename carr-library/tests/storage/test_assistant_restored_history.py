"""Actual assistant self-backup, inert restored history, and review gates."""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import Plan, Scope, Step
from carr_library.assistant.service import AssistantService
from carr_library.assistant.backup_state import validate_state_bytes
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup


@pytest.fixture
def self_backup(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    app.create_workspace('Synthetic checkpoint library')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    plan = Plan(Scope(output_root=output, allow_full_backup=True,
                      max_bytes=4 * 1024 * 1024, max_files=100),
                (Step('library.list'), Step('library.backup', {'name': 'snapshot'})))
    identity = service.submit(plan)
    service.store.control(identity, 'run')
    result = service.run(identity)
    assert result.status == 'complete'
    assert result.completed == result.total == 2
    source = output / 'snapshot'
    digest = sha256((source / 'manifest.json').read_bytes()).hexdigest()
    name = 'state/assistant/' + identity + '.json'
    raw = (source / name).read_bytes()
    saved = json.loads(raw)
    assert saved['status'] == 'running'
    assert [step['status'] for step in saved['steps']] == ['complete', 'running']
    assert saved['steps'][0]['result']['libraries'][0]['name'] == 'Synthetic checkpoint library'
    return app, source, digest, identity, name, raw


def _restored(self_backup, tmp_path):
    _, source, digest, identity, name, raw = self_backup
    target = restore_backup(source, tmp_path / 'restored', digest)
    assert (target / name).read_bytes() == raw
    control = 'state/assistant/' + identity + '.control.json'
    assert (target / control).read_bytes() == (source / control).read_bytes()
    return LibraryApp(target), identity


def test_self_backup_restores_read_only_history_without_old_scope(self_backup, tmp_path, monkeypatch):
    original, _, _, _, name, raw = self_backup
    app, identity = _restored(self_backup, tmp_path)
    assert json.loads((app.root / name).read_bytes())['data_dir'] == str(original.root)
    marker_path = app.root / 'state' / 'assistant' / (identity + '.restore-review.json')
    marker = json.loads(marker_path.read_bytes())
    assert marker['checkpoint_sha256'] == sha256(raw).hexdigest()
    assert marker['review_required'] is True
    service = AssistantService(app)

    def forbidden_scope(*args, **kwargs):
        raise AssertionError('Archived Scope must not be reconstructed for history.')

    monkeypatch.setattr(Plan, 'from_dict', forbidden_scope)
    report = service.get(identity)
    assert report.status == 'paused'
    assert report.review_required is True
    assert (report.completed, report.total) == (1, 2)
    assert report.results[0]['libraries'][0]['name'] == 'Synthetic checkpoint library'
    assert service.jobs() == (report,)
    assert 'review' in report.message.lower()
    review = service.saved_plan_for_review(identity)
    assert review['steps'] == [{'tool': 'library.backup', 'arguments': {'name': 'snapshot'}}]
    assert 'scope' not in review
    assert (app.root / name).read_bytes() == raw


@pytest.mark.parametrize('operation', ['run', 'resume'])
def test_same_path_restore_requires_new_review_before_execution(self_backup, tmp_path, monkeypatch, operation):
    original, source, digest, identity, name, raw = self_backup
    old_root = original.root
    old_root.rename(tmp_path / 'preserved-live')
    restored = LibraryApp(restore_backup(source, old_root, digest))
    service = AssistantService(restored)
    assert json.loads(raw)['data_dir'] == str(restored.root)

    def forbidden_execute(*args, **kwargs):
        raise AssertionError('A restored job must not execute any tool.')

    monkeypatch.setattr(service, '_execute', forbidden_execute)
    with pytest.raises(PermissionError, match='new review'):
        getattr(service, operation)(identity)
    assert service.get(identity).status == 'paused'
    assert (restored.root / name).read_bytes() == raw


def test_restored_history_survives_another_backup(self_backup, tmp_path):
    app, identity = _restored(self_backup, tmp_path)
    name = 'state/assistant/' + identity + '.restore-review.json'
    marker = (app.root / name).read_bytes()
    destination = tmp_path / 'second-backup'
    digest = create_backup(app, destination)
    target = restore_backup(destination, tmp_path / 'second-restore', digest)
    assert (target / name).read_bytes() == marker
    assert AssistantService(LibraryApp(target)).get(identity).completed == 1


def test_restored_gate_detects_changed_checkpoint(self_backup, tmp_path):
    app, identity = _restored(self_backup, tmp_path)
    service = AssistantService(app)
    path = service.store.path(identity)
    value = json.loads(path.read_bytes())
    value['message'] = 'A different valid checkpoint'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='checkpoint changed'):
        service.get(identity)
    with pytest.raises(ValueError, match='checkpoint changed'):
        service.resume(identity)
    destination = tmp_path / 'invalid-backup'
    with pytest.raises(RestoreError, match='does not match its checkpoint'):
        create_backup(app, destination)
    assert not destination.exists()


@pytest.mark.parametrize('contents', [
    b'{"schema":"carr-assistant-restore-review/1","id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","checkpoint_sha256":"' + b'0' * 64 + b'","review_required":false}',
    b'{"schema":"carr-assistant-restore-review/1","id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","checkpoint_sha256":"' + b'0' * 64 + b'","review_required":true,"extra":1}',
    b'{"review_required":true,"review_required":false}',
])
def test_review_marker_is_exact_typed_data(contents):
    with pytest.raises(ValueError):
        validate_state_bytes('state/assistant/' + 'a' * 32 + '.restore-review.json', contents)


def test_orphan_assistant_control_is_not_published(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    directory = app.root / 'state' / 'assistant'
    directory.mkdir(parents=True)
    (directory / ('a' * 32 + '.control.json')).write_bytes(b'{"command":"run"}')
    destination = tmp_path / 'invalid-backup'
    with pytest.raises(RestoreError, match='no matching job'):
        create_backup(app, destination)
    assert not destination.exists()


def test_completed_job_history_is_preserved_after_restore(tmp_path):
    app = LibraryApp(tmp_path / 'live')
    service = AssistantService(app)
    identity = service.submit(Plan(Scope(), (Step('library.list'),)))
    assert service.run(identity).status == 'complete'
    destination = tmp_path / 'backup'
    digest = create_backup(app, destination)
    target = restore_backup(destination, tmp_path / 'restored', digest)
    restored = AssistantService(LibraryApp(target))
    report = restored.get(identity)
    assert report.status == 'paused'
    assert report.completed == report.total == 1
    assert report.results == ({'libraries': [], 'artifacts': []},)
    with pytest.raises(PermissionError, match='new review'):
        restored.run(identity)

