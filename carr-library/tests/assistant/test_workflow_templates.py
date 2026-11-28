"""Real typed plans, catalogue CAS and passive full-backup round trips."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import templates
from carr_library.assistant.templates import TemplateStore, WorkflowTemplate, validate_catalog
from carr_library.durable_state import read_json
from carr_library.library_backup import create_backup, restore_backup


def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed scope')
    return app, workspace, AssistantService(app), TemplateStore(app.root)


@pytest.mark.parametrize('kind', templates.KINDS)
def test_named_kind_roundtrip_is_scope_free_and_creates_new_jobs_only_after_submit(tmp_path, kind):
    app, workspace, service, store = environment(tmp_path)
    inputs = tmp_path / 'inputs'; inputs.mkdir()
    outputs = tmp_path / 'outputs'; outputs.mkdir()
    original = Plan(Scope(workspace, (inputs,), outputs, allow_full_backup=True),
                    (Step('library.list'),), 'Reusable safe operation')
    record = WorkflowTemplate.capture('Repeated review', original, kind=kind)
    token = store.save(record, expected_token=None)
    loaded, current = TemplateStore(app.root).get(record.id)
    assert current == token and loaded == record
    raw = read_json(store.path)
    assert set(raw) == {'schema', 'revision', 'templates'}
    assert set(raw['templates'][0]) == {'id', 'name', 'kind', 'summary', 'steps'}
    assert 'scope' not in loaded.proposal()
    assert str(inputs) not in store.path.read_text('utf-8')
    assert str(outputs) not in store.path.read_text('utf-8')
    reviewed = loaded.review(service, Scope(workspace))
    assert reviewed.scope.input_roots == () and reviewed.scope.output_root is None
    assert reviewed.scope.allow_full_backup is False
    assert service.jobs() == ()
    one, two = service.submit(reviewed), service.submit(reviewed)
    assert one != two
    assert service.run(one).status == 'complete'
    assert service.get(two).status == 'queued'
    assert TemplateStore(app.root).get(record.id)[0] == record


def test_template_never_restores_saved_input_or_full_backup_permission(tmp_path):
    app, workspace, service, store = environment(tmp_path)
    inputs = tmp_path / 'inputs'; inputs.mkdir()
    source = inputs / 'source.txt'; source.write_text('Exact source.', encoding='utf-8')
    plan = Plan(Scope(workspace, (inputs,)), (Step('library.import', {'path': str(source)}),))
    record = WorkflowTemplate.capture('Import selected source', plan)
    store.save(record, expected_token=None)
    with pytest.raises(PermissionError, match='outside'):
        record.review(service, Scope(workspace))
    reviewed = record.review(service, Scope(workspace, (inputs,)))
    assert not service.jobs() and not app.browse(workspace)
    identity = service.submit(reviewed)
    assert service.run(identity).status == 'complete'
    assert len(app.browse(workspace)) == 1 and source.read_text('utf-8') == 'Exact source.'
    outputs = tmp_path / 'output'; outputs.mkdir()
    backup = WorkflowTemplate.capture('Complete backup', Plan(
        Scope(output_root=outputs, allow_full_backup=True),
        (Step('library.backup', {'name': 'new-copy'}),)))
    with pytest.raises(PermissionError, match='all libraries'):
        backup.review(service, Scope(output_root=outputs))


def test_template_rechecks_current_workspace_and_replaced_input_before_execution(tmp_path):
    app, workspace, service, store = environment(tmp_path)
    path = tmp_path / 'selected'; path.mkdir()
    source = path / 'original.txt'; source.write_text('Original', encoding='utf-8')
    record = WorkflowTemplate.capture('Current input only', Plan(
        Scope(workspace, (path,)), (Step('library.import', {'path': str(source)}),)))
    source.unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        record.review(service, Scope(workspace, (path,)))
    app.archive_workspace(workspace)
    catalog = WorkflowTemplate.capture('Revoked scope', Plan(Scope(workspace), (Step('library.list'),)))
    with pytest.raises(Exception):
        catalog.review(service, Scope(workspace))
    assert not service.jobs()


def test_two_windows_and_replacement_confirmation_use_exact_catalogue_cas(tmp_path):
    app, workspace, service, store = environment(tmp_path)
    plan = Plan(Scope(workspace), (Step('library.list'),))
    original = WorkflowTemplate.capture('Versioned template', plan)
    first = store.save(original, expected_token=None)
    other = TemplateStore(app.root)
    second = other.save(replace(original, summary='Changed in another window'), expected_token=first)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(original, summary='Stale overwrite'), expected_token=first)
    with pytest.raises(ValueError, match='changed'):
        store.delete(original.id, expected_token=first)
    with pytest.raises(ValueError, match='changed'):
        store.get(original.id, expected_token=first)
    assert store.path.read_bytes() == before
    store.delete(original.id, expected_token=second)
    assert store.list()[0] == ()
    assert read_json(store.path)['revision'] == 3


@pytest.mark.parametrize('field,value', [
    ('scope', {'allow_full_backup': True}), ('approved', True),
    ('job_id', 'a' * 32), ('credential', 'not-a-real-secret'),
    ('results', []), ('status', 'complete'),
])
def test_saved_authority_and_job_fields_are_rejected(tmp_path, field, value):
    app, workspace, service, store = environment(tmp_path)
    record = WorkflowTemplate.capture('Typed data', Plan(Scope(workspace), (Step('library.list'),)))
    raw = record.to_dict(); raw[field] = value
    with pytest.raises(ValueError, match='permissions or job state'):
        WorkflowTemplate.from_dict(raw)
    assert not store.path.exists()


@pytest.mark.parametrize('tool,arguments', [
    ('shell', {'command': 'echo forbidden'}),
    ('python', {'code': 'pass'}),
    ('network.fetch', {'url': 'https://example.invalid'}),
    ('library.list', {'approval': True}),
    ('audio.convert', {'shell': 'invalid'}),
])
def test_macros_accept_only_existing_typed_tool_policy(tmp_path, tool, arguments):
    raw = {'id': '1' * 32, 'name': 'Invalid macro', 'kind': 'keyboard-macro',
           'summary': '', 'steps': [{'tool': tool, 'arguments': arguments}]}
    with pytest.raises((ValueError, PermissionError)):
        WorkflowTemplate.from_dict(raw)


def test_catalogue_rejects_duplicates_limits_and_linked_state(tmp_path, monkeypatch):
    app, workspace, service, store = environment(tmp_path)
    record = WorkflowTemplate.capture('One', Plan(Scope(workspace), (Step('library.list'),)))
    raw = {'schema': templates.SCHEMA, 'revision': 0,
           'templates': [record.to_dict(), replace(record, id='2' * 32, name='one').to_dict()]}
    with pytest.raises(ValueError, match='unique'):
        validate_catalog(raw)
    monkeypatch.setattr(templates, 'MAX_TEMPLATES', 1)
    raw['templates'][1]['name'] = 'Two'
    with pytest.raises(ValueError):
        validate_catalog(raw)
    store.save(record, expected_token=None)
    linked = tmp_path / 'extra-link.json'
    os.link(store.path, linked)
    with pytest.raises(ValueError, match='links'):
        store.list()
    linked.unlink()


def test_passive_backup_restore_does_not_open_saved_paths_or_create_jobs(tmp_path, monkeypatch):
    app, workspace, service, store = environment(tmp_path)
    missing = tmp_path / 'unavailable-source' / 'never-opened.txt'
    record = WorkflowTemplate.capture('Portable references', Plan(Scope(workspace),
        (Step('library.import', {'path': str(missing)}),)))
    store.save(record, expected_token=None)
    from carr_library.assistant import models
    def forbidden(*args, **kwargs):
        raise AssertionError('Passive templates must not resolve saved execution scopes.')
    monkeypatch.setattr(models, 'local_path', forbidden)
    catalogue = store.path.read_bytes()
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert templates.NAME in {row['filename'] for row in manifest['sidecars']}
    assert not any(row['filename'].endswith('.lock') for row in manifest['sidecars'])
    target = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (target / templates.NAME).read_bytes() == catalogue
    assert TemplateStore(target).get(record.id)[0] == record
    assert not tuple((target / 'state/assistant').glob('[0-9a-f]' * 32 + '.json'))


def test_malformed_template_stops_full_backup_without_publishing(tmp_path):
    app, workspace, service, store = environment(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({'schema': templates.SCHEMA, 'revision': 0,
        'templates': [{'id': 'a' * 32, 'name': 'Injected', 'kind': 'workflow',
                      'summary': '', 'steps': [], 'scope': {'allow_create': True}}]}), encoding='utf-8')
    target = tmp_path / 'rejected'
    with pytest.raises(Exception):
        create_backup(app, target)
    assert not target.exists()


@pytest.mark.parametrize('original,alias', [
    ('Café', 'Cafe\u0301'),
    ('Research  notes', 'Research notes'),
    ('Історія', 'ІСТОРІЯ'),
    ('Two\u00a0words', 'Two words'),
])
def test_spoken_equivalent_template_names_cannot_create_ambiguous_entries(tmp_path, original, alias):
    app, workspace, service, store = environment(tmp_path)
    plan = Plan(Scope(workspace), (Step('library.list'),))
    first = WorkflowTemplate.capture(original, plan)
    token = store.save(first, expected_token=None)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='unique'):
        store.save(WorkflowTemplate.capture(alias, plan), expected_token=token)
    assert store.path.read_bytes() == before
    assert store.get(first.id)[0] == first
    assert service.jobs() == ()
