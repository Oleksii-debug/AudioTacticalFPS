"""Typed recipes survive a real common4 backup without execution or old scope."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant.templates import TemplateStore, WorkflowTemplate, MAX_TEMPLATES
from carr_library.conversion.batches import DocumentBatches
from carr_library.conversion.planning import SplitOptions
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.render import ProductionService

PREFIX = 'state/workflows/templates/'


def retained(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Only original workspace')
    store = TemplateStore(app.root)
    first = store.save('Named commands', 'assistant',
        {'steps': [{'tool': 'library.create', 'arguments': {'name': {'$parameter': 'name'}}}]},
        {'name': {'kind': 'text', 'label': 'Fresh workspace name', 'example': 'Example'}})
    saved = store.save('Named commands revision2', first.kind, first.definition, first.parameters, previous=first)
    return app, store, saved


def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Backup/restore cannot activate a recipe, process, render or batch.')
    for owner in (AssistantService, DocumentBatches, ProductionService):
        monkeypatch.setattr(owner, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def test_common4_preserves_exact_typed_bytes_and_never_replays_a_macro(tmp_path, monkeypatch):
    app, store, saved = retained(tmp_path)
    name = PREFIX + saved.id + '.json'
    raw = (app.root / name).read_bytes()
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    registry = CommandRegistry((CommandSpec(saved.command_id, saved.name),))
    shortcut_path = app.root / 'keyboard-shortcuts.json'
    KeymapStore(shortcut_path).save({saved.command_id: 'Ctrl+Alt+Shift+F9'}, registry)
    shortcut_bytes = shortcut_path.read_bytes()
    forbid_execution(monkeypatch)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    entry, = [entry for entry in manifest['sidecars'] if entry['filename'] == name]
    assert entry == dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    actual = TemplateStore(restored.root).get(saved.id)
    assert actual == saved and actual.revision == 2
    assert (restored.root / name).read_bytes() == (app.root / name).read_bytes() == raw
    assert len(restored.workspaces()) == len(app.workspaces()) == 1
    assert AssistantService(restored).jobs() == ()
    assert (restored.root / 'keyboard-shortcuts.json').read_bytes() == shortcut_bytes
    assert KeymapStore(restored.root / 'keyboard-shortcuts.json').load() == {
        saved.command_id: 'Ctrl+Alt+Shift+F9'}
    assert 'scope' not in actual.to_dict() and 'approval' not in actual.to_dict()


def test_missing_external_paths_remain_passive_recipe_data_on_restore(tmp_path, monkeypatch):
    app, store, _ = retained(tmp_path)
    source = tmp_path / 'only-explicitly-reauthorized.txt'
    source.write_text('not a backup input', encoding='utf-8')
    saved = store.save('Choose current input again', 'assistant',
        {'steps': [{'tool': 'library.import', 'arguments': {'path': str(source)}}]})
    source.unlink()
    forbid_execution(monkeypatch)
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert TemplateStore(restored.root).get(saved.id) == saved
    assert not source.exists() and not (backup / source.name).exists()


@pytest.mark.parametrize('damage', ['scope', 'tool', 'parameter', 'filename', 'revision', 'unknown-sibling', 'oversize'])
def test_bad_or_unbounded_templates_are_rejected_without_omission(tmp_path, damage):
    app, store, saved = retained(tmp_path)
    path = store.root / (saved.id + '.json')
    value = saved.to_dict()
    if damage == 'scope':
        value['scope'] = {'allow_create': True}
    elif damage == 'tool':
        value['definition']['steps'][0]['tool'] = 'shell'
    elif damage == 'parameter':
        value['parameters']['name']['kind'] = 'python'
    elif damage == 'filename':
        value['id'] = '0' * 32 if saved.id != '0' * 32 else '1' * 32
    elif damage == 'revision':
        value['revision'] = True
    elif damage == 'unknown-sibling':
        path = path.with_suffix('.py')
    raw = encode(value)
    if damage == 'oversize':
        raw += b' ' * (256 * 1024 + 1)
    path.write_bytes(raw)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        create_backup(app, destination)
    assert not destination.exists() and path.read_bytes() == raw


@pytest.mark.parametrize('count', [64, 65])
def test_backup_enforces_the_live_collection_bound(tmp_path, count):
    app, store, saved = retained(tmp_path)
    (store.root / (saved.id + '.json')).unlink()
    for index in range(count):
        current = replace(saved, id=f'{index:032x}', name=f'Workflow {index}')
        (store.root / (current.id + '.json')).write_bytes(encode(current.to_dict()))
    destination = tmp_path / 'backup'
    if count > MAX_TEMPLATES:
        with pytest.raises(ValueError, match='64 records'):
            create_backup(app, destination)
        assert not destination.exists()
    else:
        checksum = create_backup(app, destination)
        restored = LibraryApp(restore_backup(destination, tmp_path / 'restored', checksum))
        assert len(TemplateStore(restored.root).list()) == 64


@pytest.mark.parametrize('resign', [False, True])
def test_backup_manifest_rehash_cannot_authorize_an_unknown_operation(tmp_path, resign):
    app, store, saved = retained(tmp_path)
    backup = tmp_path / 'backup'; checksum = create_backup(app, backup)
    name = PREFIX + saved.id + '.json'
    value = saved.to_dict(); value['definition']['steps'][0]['tool'] = 'process.execute'
    raw = encode(value); (backup / name).write_bytes(raw)
    if resign:
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        entry, = [entry for entry in manifest['sidecars'] if entry['filename'] == name]
        entry.update(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
        encoded = encode(manifest); (backup / 'manifest.json').write_bytes(encoded)
        checksum = sha256(encoded).hexdigest()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(backup, destination, checksum)
    assert not destination.exists() and (backup / name).read_bytes() == raw


def test_actual_template_update_during_snapshot_invalidates_it(tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    app, store, saved = retained(tmp_path)
    name = PREFIX + saved.id + '.json'
    write, changed = backup_sidecars._write, []
    def after_copy(stage, relative, raw):
        write(stage, relative, raw)
        if relative == name:
            changed.append(store.save('Changed during snapshot', saved.kind, saved.definition,
                                      saved.parameters, previous=saved))
    monkeypatch.setattr(backup_sidecars, '_write', after_copy)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='changed during backup'):
        create_backup(app, destination)
    assert not destination.exists() and len(changed) == 1
    assert store.get(saved.id).fingerprint == changed[0].fingerprint
