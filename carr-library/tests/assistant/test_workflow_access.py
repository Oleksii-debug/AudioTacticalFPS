"""Existing typed jobs reused as recipes under newly chosen host permissions."""
from dataclasses import replace
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.state import atomic_json
from carr_library.assistant.workflow_access import WorkflowAccess, workflow_name


def test_named_multistep_workflow_saves_without_execution_and_reuses_new_scope(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    first = app.create_workspace('First')
    second = app.create_workspace('Second')
    app.add_text(first, 'Old private source', 'Railway private source.')
    app.add_text(second, 'New chosen source', 'Railway chosen source.')
    service = AssistantService(app)
    access = WorkflowAccess(service)
    plan = Plan(Scope(first), (Step('library.catalog'), Step('library.search', {'query': 'Railway'})))
    original = access.save(plan, '  Огляд джерел  ')
    record = service.store.load(original)
    assert record['status'] == 'queued'
    assert all(step['status'] == 'pending' and step['result'] is None for step in record['steps'])
    assert record['plan']['summary'] == 'Огляд джерел'
    selection, = WorkflowAccess(AssistantService(LibraryApp(app.root))).entries()
    assert selection.name == 'Огляд джерел' and selection.step_count == 2
    recipe = access.recipe(selection)
    assert set(recipe) == {'schema', 'summary', 'steps'}
    reviewed = service.preview(json.dumps(recipe), Scope(second))
    new_job = service.submit(reviewed)
    assert new_job != original
    result = service.run(new_job)
    assert result.status == 'complete' and result.completed == 2
    hits = result.results[1]['results']
    assert len(hits) == 1 and hits[0]['workspace'] == second
    assert service.store.load(original) == record


def test_saved_recipe_review_never_resolves_historical_folders(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    source, output = tmp_path / 'old-input', tmp_path / 'old-output'
    source.mkdir()
    output.mkdir()
    service = AssistantService(app)
    access = WorkflowAccess(service)
    identity = access.save(Plan(Scope(input_roots=(source,), output_root=output),
                               (Step('library.list'),)), 'Portable list')
    source.rmdir()
    output.rmdir()
    def forbidden(*args, **kwargs):
        raise AssertionError('Historical permissions must not access the filesystem.')
    monkeypatch.setattr('carr_library.assistant.models.local_path', forbidden)
    selection, = access.entries()
    assert selection.identity == identity
    assert access.recipe(selection)['steps'] == [{'tool': 'library.list', 'arguments': {}}]
    _, inert = service.review_saved_plan(identity)
    assert inert.scope.input_roots == () and inert.scope.output_root is None


def test_restored_original_stays_blocked_and_recipe_requires_fresh_submission(tmp_path):
    old = LibraryApp(tmp_path / 'old-library')
    service = AssistantService(old)
    identity = WorkflowAccess(service).save(Plan(Scope(), (Step('library.list'),)), 'Portable list')
    restored = LibraryApp(tmp_path / 'restored-library')
    fresh = AssistantService(restored)
    record = service.store.load(identity)
    record['restore_requires_review'] = True
    fresh.store.save(record)
    before = fresh.store.path(identity).read_bytes()
    access = WorkflowAccess(fresh)
    selection, = access.entries()
    assert selection.restored and 'fresh review required' in selection.label
    with pytest.raises(PermissionError, match='fresh review'):
        fresh.resume(identity)
    recipe = access.recipe(selection)
    reviewed = fresh.preview(json.dumps(recipe), Scope())
    new_identity = fresh.submit(reviewed)
    assert new_identity != identity
    assert fresh.run(new_identity).status == 'complete'
    assert fresh.store.path(identity).read_bytes() == before


def test_recipe_never_inherits_input_or_full_backup_grants(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Chosen')
    source = tmp_path / 'inputs'
    output = tmp_path / 'outputs'
    source.mkdir()
    output.mkdir()
    book = source / 'book.txt'
    book.write_text('Synthetic original.', encoding='utf-8')
    service = AssistantService(app)
    access = WorkflowAccess(service)
    old = Plan(Scope(workspace, (source,), output, allow_full_backup=True),
        (Step('library.import', {'path': str(book)}), Step('library.backup', {'name': 'backup'})))
    access.save(old, 'Import and back up')
    selected, = access.entries()
    recipe = json.dumps(access.recipe(selected))
    with pytest.raises(PermissionError, match='outside'):
        service.preview(recipe, Scope(workspace, (), output))
    with pytest.raises(PermissionError, match='all libraries'):
        service.preview(recipe, Scope(workspace, (source,), output))
    accepted = service.preview(recipe, Scope(workspace, (source,), output, allow_full_backup=True))
    assert accepted.scope.allow_full_backup and len(accepted.steps) == 2
    assert not app.browse(workspace)


def test_reused_output_is_still_create_only_and_can_use_a_new_folder(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Chosen')
    imported = app.add_text(workspace, 'Original', 'Exact original reading text.')
    old_output, new_output = tmp_path / 'first', tmp_path / 'second'
    old_output.mkdir()
    new_output.mkdir()
    service = AssistantService(app)
    access = WorkflowAccess(service)
    original = access.save(Plan(Scope(workspace, output_root=old_output),
        (Step('library.extract', {'edition': imported['edition'], 'name': 'reading.txt'}),)), 'Reading copy')
    assert service.run(original).status == 'complete'
    exact = (old_output / 'reading.txt').read_bytes()
    selected, = access.entries()
    recipe = json.dumps(access.recipe(selected))
    with pytest.raises(FileExistsError):
        service.preview(recipe, Scope(workspace, output_root=old_output))
    reviewed = service.preview(recipe, Scope(workspace, output_root=new_output))
    assert service.run(service.submit(reviewed)).status == 'complete'
    assert (new_output / 'reading.txt').read_bytes() == exact
    assert (old_output / 'reading.txt').read_bytes() == exact


def test_changed_saved_recipe_is_not_accepted_from_old_selection(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    access = WorkflowAccess(service)
    identity = access.save(Plan(Scope(), (Step('library.list'),)), 'Original')
    selection, = access.entries()
    record = service.store.load(identity)
    changed = Plan.from_dict(record['plan'])
    changed = replace(changed, summary='Changed')
    record.update(plan=changed.to_dict(), plan_sha256=changed.digest)
    atomic_json(service.store.path(identity), record)
    with pytest.raises(ValueError, match='changed'):
        access.recipe(selection)


@pytest.mark.parametrize('tool', ['shell', 'python', 'network.fetch', 'credential.get', 'library.delete'])
def test_saved_workflow_cannot_introduce_an_unlisted_macro_operation(tmp_path, tool):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    access = WorkflowAccess(service)
    identity = access.save(Plan(Scope(), (Step('library.list'),)), 'Safe')
    record = service.store.load(identity)
    record['plan']['steps'][0]['tool'] = tool
    atomic_json(service.store.path(identity), record)
    with pytest.raises(ValueError):
        access.entries()


@pytest.mark.parametrize('marker', ['true', 1, None, {}])
def test_workflow_catalog_retains_strict_restore_marker(tmp_path, marker):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    access = WorkflowAccess(service)
    identity = access.save(Plan(Scope(), (Step('library.list'),)), 'Safe')
    record = service.store.load(identity)
    record['restore_requires_review'] = marker
    atomic_json(service.store.path(identity), record)
    with pytest.raises(ValueError, match='marker'):
        access.entries()


@pytest.mark.parametrize('value', ['', '  ', 'two\nlines', 'tab\tname', '\x00', 'a' * 201, None, True])
def test_workflow_names_are_short_readable_single_lines(value):
    with pytest.raises(ValueError):
        workflow_name(value)


def test_cancelled_catalog_does_not_read_or_execute_jobs(tmp_path, monkeypatch):
    service = AssistantService(LibraryApp(tmp_path / 'library'))
    access = WorkflowAccess(service)
    access.save(Plan(Scope(), (Step('library.list'),)), 'Saved')
    monkeypatch.setattr(service, 'review_saved_plan', lambda _: pytest.fail('Cancelled review read a job.'))
    with pytest.raises(InterruptedError):
        access.entries(cancelled=lambda: True)


def test_reuse_honors_the_new_callers_step_budget(tmp_path):
    service = AssistantService(LibraryApp(tmp_path / 'library'))
    access = WorkflowAccess(service)
    access.save(Plan(Scope(max_steps=10), (Step('library.list'), Step('library.list'))), 'Two operations')
    saved, = access.entries()
    with pytest.raises(ValueError, match='budget'):
        service.preview(json.dumps(access.recipe(saved)), Scope(max_steps=1))
    assert len(service.store.identities()) == 1
