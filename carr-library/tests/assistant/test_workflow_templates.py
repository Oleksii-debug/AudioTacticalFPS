"""Actual scoped plans, filesystem state, import/export and passive recovery."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import templates
from carr_library.assistant.templates import (WorkflowTemplate, WorkflowTemplateStore,
    TemplateChangeCommittedError, combine_instructions, draft_from_plan)
from carr_library.durable_state import encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_backup import snapshot_state, restore_state


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original scope')
    other = app.create_workspace('Fresh chosen scope')
    app.add_text(workspace, 'First source', 'Shared word, first library.')
    app.add_text(other, 'Second source', 'Shared word, second library.')
    inputs, outputs = tmp_path / 'inputs', tmp_path / 'outputs'
    inputs.mkdir()
    outputs.mkdir()
    service = AssistantService(app)
    plan = service.preview('Search "Shared"\nList books', Scope(workspace, (inputs,), outputs))
    return app, workspace, other, inputs, outputs, service, plan


def test_reading_empty_templates_never_creates_a_store_or_job(environment):
    app, _, _, _, _, service, _ = environment
    store = service.workflow_templates()
    assert store.list() == () and not store.root.exists()
    assert service.jobs() == ()


def test_loaded_workflow_requires_fresh_preview_and_separate_actual_run(environment):
    app, workspace, other, inputs, outputs, service, plan = environment
    store = service.workflow_templates()
    saved = store.save('Пошук і каталог', plan)
    instruction = saved.instruction()
    assert set(json.loads(instruction)) == {'schema', 'steps', 'summary'}
    assert 'scope' not in saved.to_dict()['draft']
    assert str(inputs) not in instruction and str(outputs) not in instruction
    assert service.jobs() == ()
    fresh = service.preview(instruction, Scope(other))
    assert fresh.scope.workspace_id == other
    assert service.jobs() == ()
    identity = service.submit(fresh)
    report = service.run(identity)
    assert report.status == 'complete' and report.completed == 2
    assert all(hit['workspace'] == other for hit in report.results[0]['results'])
    assert service.store.load(identity)['plan']['scope']['workspace_id'] == other
    assert workspace != other


def test_saved_and_returned_arguments_do_not_alias_the_input_plan(environment):
    _, _, _, _, _, service, plan = environment
    store = service.workflow_templates()
    saved = store.save('Immutable settings', plan)
    original = saved.fingerprint
    plan.steps[0].arguments['query'] = 'changed source object'
    payload = saved.to_dict()
    payload['draft']['steps'][0]['arguments']['query'] = 'changed exported object'
    assert store.get(saved.id).fingerprint == original
    assert saved.steps[0].arguments['query'] == 'Shared'


def test_two_instances_require_exact_revision_for_save_remove_and_export(environment):
    app, *_, service, plan = environment
    store = service.workflow_templates()
    old = store.save('First name', plan)
    second = WorkflowTemplateStore(app.root)
    current = second.save('Renamed', old.draft, previous=old)
    assert current.id == old.id and current.revision == 2
    for change in (lambda: store.save('Stale', old.draft, previous=old),
                   lambda: store.remove(old), lambda: store.export_json(old)):
        with pytest.raises(ValueError, match='changed after review'):
            change()
    assert store.get(old.id) == current
    store.remove(current)
    assert store.list() == ()
    assert service.jobs() == ()


def test_name_collision_cannot_overwrite_another_template(environment):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    first = store.save('Reviewed batch', plan)
    before = (store.root / (first.id + '.json')).read_bytes()
    with pytest.raises(ValueError, match='distinct name'):
        store.save('REVIEWED BATCH', plan)
    assert (store.root / (first.id + '.json')).read_bytes() == before
    assert len(store.list()) == 1


def test_export_import_creates_new_identity_without_saved_scope(environment, tmp_path):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    first = store.save('First batch', plan)
    second_revision = store.save('First batch', first.draft, previous=first)
    path = tmp_path / 'workflow.json'
    path.write_text(store.export_json(second_revision), encoding='utf-8')
    imported = store.import_file(path, name='Imported batch')
    assert imported.id != first.id and imported.revision == 1
    assert imported.draft == second_revision.draft
    assert service.jobs() == ()
    assert set(json.loads(path.read_text(encoding='utf-8'))) == {
        'schema', 'id', 'name', 'revision', 'draft'}


@pytest.mark.parametrize('change', [
    'scope', 'approval', 'unknown_record', 'shell', 'unknown_argument',
    'boolean_revision', 'control_name', 'wrong_id', 'empty_steps', 'wrong_schema',
])
def test_import_rejects_authority_or_invalid_shape_before_saving(environment, tmp_path, change):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    first = store.save('Existing', plan)
    value = first.to_dict()
    if change == 'scope': value['draft']['scope'] = plan.scope.to_dict()
    elif change == 'approval': value['draft']['approved'] = True
    elif change == 'unknown_record': value['credential'] = 'never-authority'
    elif change == 'shell': value['draft']['steps'][0]['tool'] = 'shell'
    elif change == 'unknown_argument': value['draft']['steps'][0]['arguments']['command'] = 'echo ignored'
    elif change == 'boolean_revision': value['revision'] = True
    elif change == 'control_name': value['name'] = 'name\nsecond line'
    elif change == 'wrong_id': value['id'] = '../outside'
    elif change == 'empty_steps': value['draft']['steps'] = []
    elif change == 'wrong_schema': value['schema'] = 'unknown/99'
    path = tmp_path / 'untrusted.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises((ValueError, TypeError)):
        store.import_file(path, name='Imported')
    assert store.list() == (first,) and service.jobs() == ()


@pytest.mark.parametrize('raw', [
    '{"schema":"one","schema":"two"}',
    '{"schema":NaN}', '{"schema":Infinity}', '{"schema":-Infinity}',
])
def test_import_uses_shared_strict_json_parser(environment, tmp_path, raw):
    _, *_, service, _ = environment
    path = tmp_path / 'untrusted.json'
    path.write_text(raw)
    with pytest.raises(ValueError):
        service.workflow_templates().import_file(path)
    assert not service.workflow_templates().root.exists()


def test_batch_expands_only_selected_typed_steps_in_reviewed_order(environment):
    _, _, other, _, _, service, plan = environment
    store = service.workflow_templates()
    first = store.save('Search', Plan(plan.scope, (plan.steps[0],)))
    second = store.save('Catalog', Plan(plan.scope, (plan.steps[1],)))
    instruction = combine_instructions((first, second, first))
    draft = json.loads(instruction)
    assert [step['tool'] for step in draft['steps']] == [
        'library.search', 'library.catalog', 'library.search']
    assert 'scope' not in draft and service.jobs() == ()
    actual = service.preview(instruction, Scope(other))
    result = service.run(service.submit(actual))
    assert result.status == 'complete' and result.completed == 3
    with pytest.raises(ValueError, match='128 steps'):
        combine_instructions((first,) * 129)


def test_cancellation_after_inventory_refuses_a_real_state_write(environment, monkeypatch):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    saved = store.save('Before cancellation', plan)
    before = (store.root / (saved.id + '.json')).read_bytes()
    stop = Event()
    original = store.list
    def list_then_cancel(**kwargs):
        result = original(**kwargs)
        stop.set()
        return result
    monkeypatch.setattr(store, 'list', list_then_cancel)
    with pytest.raises(InterruptedError):
        store.save('Not published', plan, previous=saved, cancelled=stop.is_set)
    assert (store.root / (saved.id + '.json')).read_bytes() == before
    with pytest.raises(InterruptedError):
        store.remove(saved, cancelled=stop.is_set)
    assert (store.root / (saved.id + '.json')).read_bytes() == before


def test_collection_budget_counts_real_file_bytes_and_record_count(environment, monkeypatch):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    saved = store.save('Real byte accounting', plan)
    path = store.root / (saved.id + '.json')
    raw = path.read_bytes()
    path.write_bytes(raw + b' ' * 2048)
    monkeypatch.setattr(templates, 'MAX_COLLECTION_BYTES', len(raw) + 1024)
    with pytest.raises(ValueError, match='64 MiB'):
        store.list()
    with pytest.raises(ValueError, match='64 MiB'):
        templates.validate_template_collection([{
            'filename': templates.PREFIX + saved.id + '.json', 'size_bytes': path.stat().st_size}])
    path.write_bytes(raw)
    monkeypatch.setattr(templates, 'MAX_COLLECTION_BYTES', 64 * 1024 * 1024)
    monkeypatch.setattr(templates, 'MAX_TEMPLATES', 1)
    with pytest.raises(ValueError, match='256 records'):
        store.save('Second exceeds configured count', plan)
    assert store.list() == (saved,)


def test_real_hard_link_is_refused_before_template_import(environment, tmp_path):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    saved = store.save('Original', plan)
    target = tmp_path / 'linked.json'
    os.link(store.root / (saved.id + '.json'), target)
    with pytest.raises(ValueError, match='without links'):
        store.import_file(target)
    target.unlink()
    assert store.get(saved.id) == saved


@pytest.mark.parametrize('operation', ['save', 'remove'])
def test_post_commit_directory_sync_failure_preserves_exact_change_receipt(
        environment, monkeypatch, operation):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    old = store.save('Original', plan)
    def fail_sync(path):
        raise OSError('synthetic directory sync failure after actual publication')
    if operation == 'save':
        from carr_library import durable_state
        monkeypatch.setattr(durable_state, 'sync_directory', fail_sync)
        change = lambda: store.save('Published successor', old.draft, previous=old)
    else:
        monkeypatch.setattr(templates, 'sync_directory', fail_sync)
        change = lambda: store.remove(old)
    with pytest.raises(TemplateChangeCommittedError) as captured:
        change()
    receipt = captured.value
    assert receipt.durability_confirmed is False
    if operation == 'save':
        assert store.get(old.id) == receipt.template
        assert receipt.template.name == 'Published successor' and receipt.template.revision == 2
    else:
        assert receipt.template == old and store.list() == ()


def test_saved_job_reuse_discards_old_approval_without_opening_old_paths(environment):
    app, workspace, _, inputs, outputs, service, _ = environment
    source = inputs / 'retained.txt'
    source.write_text('Exact retained source.', encoding='utf-8')
    job = service.submit(service.preview('Import "' + str(source) + '"',
                                        Scope(workspace, (inputs,), outputs)))
    record = service.store.load(job)
    record['data_dir'] = str(app.root.parent / 'old-machine')
    service.store.save(record)
    with pytest.raises(PermissionError):
        service.get(job)
    raw = service.store.path(job).read_bytes()
    draft = service.review_instruction(job)
    assert 'scope' not in json.loads(draft)
    assert service.store.path(job).read_bytes() == raw
    with pytest.raises(PermissionError):
        service.preview(draft, Scope(workspace))
    current = service.preview(draft, Scope(workspace, (inputs,), outputs))
    fresh_job = service.submit(current)
    assert fresh_job != job and service.run(fresh_job).status == 'complete'


def test_complete_backup_restores_inert_templates_without_inspecting_literal_paths(
        environment, tmp_path, monkeypatch):
    app, workspace, _, inputs, outputs, service, _ = environment
    source = inputs / 'private-source.txt'
    source.write_text('Literal source.', encoding='utf-8')
    plan = service.preview('Import "' + str(source) + '"', Scope(workspace, (inputs,), outputs))
    saved = service.workflow_templates().save('Pending import settings', plan)
    relative = templates.PREFIX + saved.id + '.json'
    before = (app.root / relative).read_bytes()
    from carr_library.assistant import models
    with monkeypatch.context() as patch:
        patch.setattr(models, 'local_path',
                      lambda *args, **kwargs: pytest.fail('template restore opened plan paths'))
        folder = tmp_path / 'backup'
        checksum = create_backup(app, folder)
        target = restore_backup(folder, tmp_path / 'restored', checksum)
    recovered = AssistantService(LibraryApp(target))
    assert (target / relative).read_bytes() == before
    restored = recovered.workflow_templates().get(saved.id)
    assert restored == saved and recovered.jobs() == ()
    with pytest.raises(PermissionError):
        recovered.preview(restored.instruction(), Scope(workspace))


def test_rehashed_backup_cannot_import_template_scope(environment, tmp_path):
    app, *_, service, plan = environment
    saved = service.workflow_templates().save('Inert', plan)
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    relative = templates.PREFIX + saved.id + '.json'
    value = json.loads((folder / relative).read_bytes())
    value['draft']['scope'] = plan.scope.to_dict()
    raw = encode(value)
    (folder / relative).write_bytes(raw)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['filename'] == relative)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = encode(manifest)
    manifest_path.write_bytes(manifest_raw)
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='workflow|Workflow'):
        restore_backup(folder, target, sha256(manifest_raw).hexdigest())
    assert not target.exists()


def test_standalone_state_route_validates_templates_and_retains_no_locks(environment, tmp_path):
    app, *_, service, plan = environment
    saved = service.workflow_templates().save('Portable settings', plan)
    captured = tmp_path / 'state-copy'
    entries = snapshot_state(app.root / 'state', captured)
    assert all(not item['filename'].endswith('.lock') for item in entries)
    target = tmp_path / 'restored-state'
    restore_state(captured, target, entries)
    relative = 'assistant/templates/' + saved.id + '.json'
    assert read_json(target / relative) == saved.to_dict()
    invalid = read_json(captured / relative)
    invalid['draft']['scope'] = plan.scope.to_dict()
    raw = encode(invalid)
    (captured / relative).write_bytes(raw)
    for entry in entries:
        if entry['filename'] == relative:
            entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='workflow|Workflow'):
        restore_state(captured, tmp_path / 'rejected-state', entries)


def test_create_only_racing_same_bytes_remain_another_publishers_change(environment, monkeypatch):
    _, *_, service, plan = environment
    store = service.workflow_templates()
    real_publish = templates.atomic_json
    competing = []
    def race(path, value, *, replace=False):
        assert not replace and not path.exists()
        real_publish(path, value, replace=False)
        competing.append(path.read_bytes())
        return real_publish(path, value, replace=False)
    monkeypatch.setattr(templates, 'atomic_json', race)
    with pytest.raises(FileExistsError) as captured:
        store.save('Competing exact bytes', plan)
    assert not isinstance(captured.value, TemplateChangeCommittedError)
    remaining = store.list()
    assert len(remaining) == 1
    assert (store.root / (remaining[0].id + '.json')).read_bytes() == competing[0]
