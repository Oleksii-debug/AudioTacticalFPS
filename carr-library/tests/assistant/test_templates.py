"""Real private template files, fresh authorization, and passive common backup."""
from hashlib import sha256
import json
import os

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import templates as module
from carr_library.assistant.templates import TemplateStore, validate_templates
from carr_library.durable_state import encode
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import restore_backup


@pytest.fixture
def setup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    return app, service, TemplateStore(service.store.directory)


def test_templates_preserve_unicode_exact_text_and_are_never_jobs(setup):
    app, service, store = setup
    raw = 'Створи бібліотеку "Архів"\r\nЗнайди "залізниця"\n'
    assert store.list() == () and not store.path.exists()
    store.save('Український огляд', raw)
    assert TemplateStore(store.directory).load('український огляд') == raw
    assert store.list() == ({'name': 'Український огляд', 'instruction': raw},)
    assert service.jobs() == () and app.workspaces() == ()
    before = store.path.read_bytes()
    with pytest.raises(FileExistsError):
        store.save('УКРАЇНСЬКИЙ ОГЛЯД', 'Different instructions')
    assert store.path.read_bytes() == before
    store.delete('Український огляд', expected_instruction=raw)
    assert store.list() == () and service.jobs() == ()


@pytest.mark.parametrize('name', ['', ' ', ' leading', 'trailing ', 'a\nb', 'a\tb',
                                  'a\x00b', 'folder/name', 'folder\\name', 'x' * 121])
def test_invalid_names_do_not_change_saved_templates(setup, name):
    _, _, store = setup
    store.save('Keep', 'List libraries')
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(name, 'List books')
    assert store.path.read_bytes() == before


@pytest.mark.parametrize('instruction', ['', ' \r\n', 'a\x00b', 'я' * (module.MAX_INSTRUCTION_BYTES // 2 + 1)])
def test_invalid_or_oversize_instruction_never_enters_store(setup, instruction):
    _, _, store = setup
    with pytest.raises(ValueError):
        store.save('Invalid', instruction)
    assert not store.path.exists()


@pytest.mark.parametrize('change', ['schema', 'extra', 'list', 'authority', 'duplicate', 'count', 'total'])
def test_strict_inert_schema_rejects_damaged_or_oversize_state(change):
    value = {'schema': module.SCHEMA, 'templates': [{'name': 'Saved', 'instruction': 'List libraries'}]}
    if change == 'schema':
        value['schema'] = 'carr-assistant-job/1'
    elif change == 'extra':
        value['credential_ref'] = 'not-template-data'
    elif change == 'list':
        value['templates'] = {}
    elif change == 'authority':
        value['templates'][0]['scope'] = {'allow_full_backup': True}
    elif change == 'duplicate':
        value['templates'].append({'name': 'SAVED', 'instruction': 'List books'})
    elif change == 'count':
        value['templates'] = [{'name': str(i), 'instruction': 'List libraries'} for i in range(101)]
    else:
        value['templates'] = [{'name': str(i), 'instruction': 'x' * module.MAX_INSTRUCTION_BYTES} for i in range(17)]
    with pytest.raises(ValueError):
        validate_templates(value)


def test_duplicate_json_keys_and_oversize_file_are_not_loaded(setup):
    _, _, store = setup
    store.path.write_text('{"schema":"' + module.SCHEMA + '","templates":[],"templates":[]}', encoding='utf-8')
    with pytest.raises(ValueError, match='Duplicate'):
        store.list()
    store.path.write_bytes(b' ' * (module.MAX_BYTES + 1))
    with pytest.raises(ValueError, match='allowance'):
        store.list()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_linked_template_file_is_rejected_without_touching_target(setup, tmp_path, kind):
    _, _, store = setup
    target = tmp_path / 'unrelated.json'
    raw = encode({'schema': module.SCHEMA, 'templates': []})
    target.write_bytes(raw)
    if kind == 'symlink':
        store.path.symlink_to(target)
    else:
        os.link(target, store.path)
    with pytest.raises(ValueError):
        store.save('Unexpected', 'List libraries')
    assert target.read_bytes() == raw


def test_writer_failure_keeps_previous_bytes_and_cleans_recognized_temporary(setup, monkeypatch):
    _, _, store = setup
    store.save('Keep', 'List libraries')
    before = store.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Synthetic publication failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='publication'):
        store.save('New', 'List books')
    assert store.path.read_bytes() == before
    assert not list(store.directory.glob('.templates.json.*.tmp'))


def test_conflicting_writer_and_stale_delete_keep_existing_instruction(setup):
    _, _, store = setup
    store.save('Keep', 'List libraries')
    before = store.path.read_bytes()
    with FileLock(store.directory / 'templates.lock'):
        with pytest.raises(RuntimeError, match='another operation'):
            store.save('Conflict', 'List books')
    with pytest.raises(ValueError, match='changed'):
        store.delete('Keep', expected_instruction='Previously reviewed text')
    assert store.path.read_bytes() == before


def test_replaced_file_during_read_does_not_return_a_mixed_snapshot(setup, monkeypatch):
    _, _, store = setup
    store.save('Keep', 'List libraries')
    actual = module.read_json
    def changed(path):
        result = actual(path)
        module.atomic_json(path, {'schema': module.SCHEMA, 'templates': []})
        return result
    monkeypatch.setattr(module, 'read_json', changed)
    with pytest.raises(ValueError, match='changed while reading'):
        store.list()


def test_loaded_json_template_needs_current_scope_and_explicit_submit(setup):
    app, service, store = setup
    first = app.create_workspace('Original scope')
    second = app.create_workspace('Current scope')
    old = Plan(Scope(workspace_id=first), (Step('library.catalog'),))
    store.save('Old scoped plan', old.to_json())
    loaded = store.load('Old scoped plan')
    assert service.jobs() == ()
    with pytest.raises(PermissionError, match='permissions'):
        service.preview(loaded, Scope(workspace_id=second))
    assert service.jobs() == ()
    value = old.to_dict()
    del value['scope']
    store.save('Reusable plan', json.dumps(value))
    plan = service.preview(store.load('Reusable plan'), Scope(workspace_id=second))
    assert plan.scope.workspace_id == second and service.jobs() == ()
    identity = service.submit(plan)
    assert service.resume(identity).status == 'complete'
    assert service.store.load(identity)['workspace'] == second


@pytest.mark.parametrize('empty', [False, True])
def test_common_backup_after_save_delete_restores_templates_without_execution(setup, tmp_path, monkeypatch, empty):
    app, service, store = setup
    workspace = app.create_workspace('Retained library')
    imported = app.add_text(workspace, 'Original', 'Exact source bytes.\r\nНезмінний оригінал.')
    raw = '{"schema":"carr-assistant-plan/1","scope":{"output_root":"Z:/missing"},"steps":[]}'
    store.save('Inert unfinished JSON', raw)
    if empty:
        store.delete('Inert unfinished JSON', expected_instruction=raw)
    before = store.path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('Backup and restoration must never plan or execute templates.')
    for name in ('preview', 'submit', 'run', 'resume'):
        monkeypatch.setattr(AssistantService, name, forbidden)
    destination = tmp_path / 'backup'
    checksum = app.backup(destination)
    manifest = json.loads((destination / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    assert any(item['filename'] == 'state/assistant/templates.json' for item in manifest['sidecars'])
    assert not any(item['filename'].endswith('.lock') for item in manifest['sidecars'])
    restored = LibraryApp(restore_backup(destination, tmp_path / 'restored', checksum))
    recovered_service = AssistantService(restored)
    recovered = TemplateStore(recovered_service.store.directory)
    assert recovered.path.read_bytes() == before and recovered.list() == store.list()
    assert recovered_service.jobs() == service.jobs() == ()
    assert restored.assets.resolve(imported['asset']).read_bytes() == app.assets.resolve(imported['asset']).read_bytes()


def test_resigned_template_authority_tampering_fails_before_restore_publication(setup, tmp_path):
    app, _, store = setup
    store.save('Saved', 'List libraries')
    backup = tmp_path / 'backup'
    app.backup(backup)
    path = backup / 'state/assistant/templates.json'
    value = json.loads(path.read_bytes())
    value['templates'][0]['scope'] = {'allow_full_backup': True}
    raw = encode(value)
    path.write_bytes(raw)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['filename'] == 'state/assistant/templates.json')
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    manifest_raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(manifest_raw)
    target = tmp_path / 'rejected'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(backup, target, sha256(manifest_raw).hexdigest())
    assert not target.exists()
