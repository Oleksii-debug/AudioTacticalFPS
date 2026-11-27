"""Actual common format4 snapshots and commit-aware assistant backup recovery."""
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.backup import accepts, ignored, restored_bytes, validate
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.library_backup import restore_backup


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic assistant backup')
    app.add_text(workspace, 'Original', 'Exact quotation for a complete local backup.')
    topic = app.create_topic(workspace, 'Source evidence')
    hit, = app.search(workspace, 'Exact quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate synthetic note.')
    output = tmp_path / 'output'
    output.mkdir()
    service = AssistantService(app)
    scope = Scope(workspace, output_root=output, allow_full_backup=True, max_bytes=8 * 1024 ** 2)
    plan = service.preview('Backup "complete"', scope)
    identity = service.submit(plan)
    return app, workspace, topic, output, service, plan, identity


def test_real_backup_restore_preserves_sources_and_inert_plan(prepared, tmp_path):
    app, workspace, topic, output, service, plan, identity = prepared
    report = service.run(identity)
    assert report.status == 'complete', report
    backup = output / 'complete'
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    name = 'state/assistant/' + identity + '.json'
    assert name in {entry['filename'] for entry in manifest['sidecars']}
    original_backup = (backup / name).read_bytes()
    source_record = service.store.path(identity).read_bytes()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', report.results[0]['manifest_sha256']))
    recovered = AssistantService(restored)
    record = recovered.store.load(identity)
    assert record['requires_reapproval'] is True
    assert record['plan'] == plan.to_dict() and record['data_dir'] == str(app.root)
    assert (backup / name).read_bytes() == original_backup
    assert service.store.path(identity).read_bytes() == source_record
    assert restored.evidence(workspace, topic)[0].anchor.quote == 'Exact quotation'
    assert recovered.get(identity).status == 'attention'
    with pytest.raises(PermissionError, match='review and submit'):
        recovered.resume(identity, approved_plan_sha256=plan.digest)
    # A second backup of the restored inert job remains a valid common snapshot.
    assert restored.backup(tmp_path / 'second-backup')


def test_restore_marks_reapproval_even_when_saved_root_string_would_match(prepared):
    _, _, _, _, service, _, identity = prepared
    name = 'state/assistant/' + identity + '.json'
    raw = service.store.path(identity).read_bytes()
    changed = validate(name, restored_bytes(name, raw))
    assert changed['data_dir'] == str(service.app.root) and changed['requires_reapproval'] is True


def test_complete_backup_ignores_only_exact_producer_temporaries(prepared, tmp_path):
    app, _, _, _, service, _, identity = prepared
    service.pause(identity)
    with service.store.lock(identity):
        temporary = service.store.directory / ('.' + identity + '.json.' + 'a' * 32 + '.tmp')
        temporary.write_bytes(b'Incomplete bytes are never a checkpoint.')
        snapshot = tmp_path / 'snapshot'
        digest = app.backup(snapshot)
    manifest = json.loads((snapshot / 'manifest.json').read_bytes())
    names = {entry['filename'] for entry in manifest['sidecars']}
    assert names == {'state/assistant/' + identity + '.json',
                     'state/assistant/' + identity + '.control.json'}
    restored = LibraryApp(restore_backup(snapshot, tmp_path / 'restore-controls', digest))
    recovered = AssistantService(restored)
    assert recovered.store.control(identity) == 'pause'
    (service.store.directory / 'arbitrary-command.json').write_text('{"shell":"no"}')
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_rehashed_malformed_job_cannot_enter_restored_library(prepared, tmp_path):
    _, _, _, output, service, _, identity = prepared
    report = service.run(identity)
    assert report.status == 'complete', report
    folder = output / 'complete'
    name = 'state/assistant/' + identity + '.json'
    raw = b'{"schema":"carr-assistant-job/1","command":"shell"}'
    (folder / name).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_bytes = canonical(manifest).encode('utf-8')
    (folder / 'manifest.json').write_bytes(manifest_bytes)
    with pytest.raises(ValueError, match='assistant|sidecar'):
        restore_backup(folder, tmp_path / 'rejected', sha256(manifest_bytes).hexdigest())
    assert not (tmp_path / 'rejected').exists()


def test_budget_and_cancellation_reach_the_actual_backup_producer(prepared, monkeypatch):
    app, _, _, output, service, plan, identity = prepared
    original = app.backup
    stop = Event()
    calls = []
    def produce(destination, **kwargs):
        calls.append(kwargs)
        stop.set()
        return original(destination, **kwargs)
    monkeypatch.setattr(app, 'backup', produce)
    report = service.run(identity, cancelled=stop.is_set)
    assert report.status == 'paused' and not list(output.iterdir())
    assert calls[0]['max_bytes'] == plan.scope.max_bytes
    assert callable(calls[0]['cancelled'])


def test_crash_after_atomic_publication_reconciles_only_exact_own_backup(prepared, monkeypatch):
    _, _, _, output, service, _, identity = prepared
    from carr_library import file_publication
    original = file_publication.publish_directory_no_replace
    def published_then_interrupted(stage, destination):
        original(stage, destination)
        raise OSError('Synthetic interruption after the commit boundary')
    monkeypatch.setattr(file_publication, 'publish_directory_no_replace', published_then_interrupted)
    first = service.run(identity)
    assert first.status == 'failed' and (output / 'complete' / 'manifest.json').is_file()
    before = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob('*') if p.is_file()}
    monkeypatch.setattr(file_publication, 'publish_directory_no_replace',
                        lambda *a: pytest.fail('A committed backup must not be published again.'))
    resumed = service.resume(identity)
    assert resumed.status == 'complete', resumed
    assert {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob('*') if p.is_file()} == before


def test_late_cancellation_keeps_committed_backup_receipt(prepared, monkeypatch):
    _, _, _, output, service, _, identity = prepared
    from carr_library import file_publication
    original = file_publication.publish_directory_no_replace
    stop = Event()
    def commit(stage, destination):
        original(stage, destination)
        stop.set()
    monkeypatch.setattr(file_publication, 'publish_directory_no_replace', commit)
    result = service.run(identity, cancelled=stop.is_set)
    assert result.status == 'complete' and result.results[0]['backup_receipt']['files'] >= 4
    assert Path(result.results[0]['directory']) == output / 'complete'
    assert service.store.load(identity)['steps'][0]['status'] == 'complete'


def test_directory_sync_error_retains_published_receipt_and_visible_warning(prepared, monkeypatch):
    _, _, _, output, service, _, identity = prepared
    import carr_library.library_backup as backend
    original = backend._sync_directory
    def sync(path):
        if Path(path) == output and (output / 'complete').exists():
            raise OSError('Synthetic late sync failure')
        return original(path)
    monkeypatch.setattr(backend, '_sync_directory', sync)
    result = service.run(identity)
    assert result.status == 'complete', result
    assert result.results[0]['warnings'] and (output / 'complete' / 'manifest.json').is_file()


@pytest.mark.parametrize('name', [
    'state/assistant/123.json', 'state/assistant/' + 'a' * 32 + '.tmp',
    'state/assistant/.' + 'a' * 32 + '.json.untrusted.tmp',
    'state/assistant/' + 'a' * 32 + '/nested.json',
])
def test_unknown_assistant_files_are_neither_validated_nor_silently_omitted(name):
    assert not accepts(name) and not ignored(name)
    with pytest.raises(ValueError):
        validate(name, b'{}')
