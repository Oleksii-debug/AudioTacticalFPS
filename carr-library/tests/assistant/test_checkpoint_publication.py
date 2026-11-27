"""Assistant inspection is bounded and cannot reuse unavailable permission paths."""
from copy import deepcopy
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.state import atomic_json
from carr_library.ingest.writer_guard import WriterBusy, exclusive_writer


def test_corrupt_and_disconnected_jobs_do_not_hide_other_jobs(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'external-inputs'
    inputs.mkdir()
    service = AssistantService(app)
    disconnected = service.submit(service.preview('List libraries', Scope(input_roots=(inputs,))))
    corrupt = service.submit(service.preview('List libraries', Scope()))
    healthy = service.submit(service.preview('List libraries', Scope()))
    service.store.path(corrupt).write_bytes(b'{ broken JSON')
    inputs.rmdir()
    reports = {row.id: row for row in service.jobs()}
    assert reports[disconnected].status == reports[corrupt].status == 'attention'
    assert reports[healthy].status == 'queued'
    assert reports[corrupt].total == 0 and 'damaged' in reports[corrupt].message
    assert service.run(healthy).status == 'complete'
    with pytest.raises(FileNotFoundError):
        service.resume(disconnected)
    assert not service.store.path(disconnected).with_suffix('.control.json').exists()


def test_publication_rejects_duplicate_json_in_live_reads(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    raw = service.store.path(identity).read_text(encoding='utf-8').rstrip()
    service.store.path(identity).write_text(raw[:-1] + ',"status":"complete"}', encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        service.resume(identity)
    assert service.jobs()[0].status == 'attention'


def test_reapproval_draft_preserves_step_arguments_without_transferring_permissions(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = inputs / 'source.txt'
    source.write_text('Exact source', encoding='utf-8')
    workspace = app.create_workspace('Reviewed source scope')
    service = AssistantService(app)
    identity = service.submit(service.preview('Import "' + str(source) + '"', Scope(workspace, (inputs,))))
    saved = service.store.load(identity)
    saved['requires_reapproval'] = True
    service.store.save(saved)
    before = service.store.path(identity).read_bytes()
    draft = json.loads(service.review_instruction(identity))
    assert 'scope' not in draft
    assert draft['steps'] == saved['plan']['steps']
    with pytest.raises(PermissionError):
        service.preview(json.dumps(draft), Scope(workspace))
    with pytest.raises(PermissionError, match='review and submit'):
        service.run(identity)
    assert service.store.path(identity).read_bytes() == before


def test_job_and_control_publication_fail_without_partial_changes_while_backup_owns_root(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    identity = service.submit(service.preview('List libraries', Scope()))
    value = service.store.load(identity)
    previous = deepcopy(value)
    raw = service.store.path(identity).read_bytes()
    with exclusive_writer(app.root):
        with pytest.raises(WriterBusy):
            service.store.save(value)
        with pytest.raises(WriterBusy):
            service.store.control(identity, 'pause')
    assert value == previous and service.store.path(identity).read_bytes() == raw
    assert not service.store.path(identity).with_suffix('.control.json').exists()
    service.store.control(identity, 'pause')
    assert service.store.control(identity) == 'pause'


def test_unmanaged_state_directory_is_rejected_before_creation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    other = app.root / 'unmanaged'
    with pytest.raises(ValueError, match='managed'):
        AssistantService(app, state_dir=other)
    assert not other.exists()


def test_job_inventory_stops_at_its_explicit_budget(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    for _ in range(2):
        service.submit(service.preview('List libraries', Scope()))
    monkeypatch.setattr('carr_library.assistant.state.MAX_JOBS', 1)
    with pytest.raises(ValueError, match='Too many'):
        service.jobs()
