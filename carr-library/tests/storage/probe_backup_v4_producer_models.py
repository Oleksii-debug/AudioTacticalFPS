"""Explicit acceptance probe using real optional producer models.

Run after installing the owning narration, reading-copy, keymap and studio
modules. This file is intentionally outside default test_ discovery; absence
of a required producer is an error here, never a silently skipped test.
"""
from hashlib import sha256
from importlib import import_module
import json
import pytest

from carr_library.library_backup import create_backup, restore_backup
from test_backup_v4_sidecars import reading, files, manifest, resign


@pytest.fixture
def producer_files(reading):
    # Optional producers are supplied by their owning change, never fake validators.
    narration = import_module('carr_library.reader.narration')
    copies = import_module('carr_library.reader.reading_copy')
    keys = import_module('carr_library.keybindings')
    keyboard = import_module('carr_library.keyboard_preferences')
    commands = import_module('carr_library.ui.command_registry')
    builtins = import_module('carr_library.ui.builtin_keyboard_commands')
    ocr = import_module('carr_library.extract.ocr_languages')
    app, workspace, page, _ = reading
    project = narration.new_project(page.text, title='Narration', source_location=page.location)
    store = narration.NarrationStore(app.root)
    store.save(project)
    draft = narration.revise(project, 'A valid unsaved draft.')
    store.save(draft, draft=True)
    copy = copies.new_copy(workspace, 'Reading copy', page.text, source=page.location)
    path = app.root / 'reading-copies' / (copy.id + '.json')
    path.parent.mkdir()
    path.write_bytes(copies.project_bytes(copy))
    (app.root / 'keybindings.json').write_bytes(keys.encode_preferences({'File/Save': 'Ctrl+Alt+S'}))
    registry = commands.CommandRegistry(builtins.builtin_commands())
    keyboard.KeymapStore(app.root / 'keyboard-shortcuts.json').save(
        {'library.new': 'Ctrl+Alt+N'}, registry)
    raw = json.dumps({'version': 1, 'presets': [{'name': 'Українська', 'languages': 'ukr',
                    'model_policy': 'default', 'orientation': True}]}).encode()
    ocr.validate_presets_bytes(raw)
    (app.root / 'ocr-language-presets.json').write_bytes(raw)
    return app, project, draft, copy


@pytest.mark.parametrize('version', [3, 4])
def test_both_real_editor_and_keymap_formats_roundtrip(producer_files, tmp_path, version):
    app, project, draft, copy = producer_files
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = manifest(folder)
    assert len(value['sidecars']) == 7
    if version == 3:
        value['version'] = 3
        del value['generated_audio_included']
    digest = resign(folder, value)
    before = files(folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    for entry in value['sidecars']:
        name = entry['filename']
        assert (target / name).read_bytes() == (app.root / name).read_bytes()
    assert files(folder) == before


def test_resigned_project_cannot_lie_about_source_text(producer_files, tmp_path):
    app, project, *_ = producer_files
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = manifest(folder)
    name = 'narration-projects/' + project.id + '.json'
    data = json.loads((folder / name).read_bytes())
    data['original_text'] = 'Forged original'
    data['original_sha256'] = sha256(data['original_text'].encode()).hexdigest()
    raw = json.dumps(data).encode()
    (folder / name).write_bytes(raw)
    entry = next(item for item in value['sidecars'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='original|source'):
        restore_backup(folder, tmp_path / 'rejected', resign(folder, value))
    assert not (tmp_path / 'rejected').exists()


def test_source_producer_snapshot_budget_is_respected(producer_files, tmp_path, monkeypatch):
    app, *_ = producer_files
    model = import_module('carr_library.reader.narration')
    monkeypatch.setattr(model, 'MAX_SNAPSHOT_BYTES', 1)
    with pytest.raises(ValueError, match='producer snapshot budget'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.fixture
def studio(reading):
    model = import_module('carr_library.studio.project')
    app, *_ = reading
    (app.root / 'reading-state.json').unlink()  # legacy state shape has no root reader sidecar
    project = model.parse_script('A synthetic audio script.', title='Portable project')
    store = model.ProjectStore(app.root / 'state' / 'studio' / 'projects')
    store.save(project)
    from dataclasses import replace
    revised = replace(project, title='Edited title', revision=2)
    store.save(revised, expected_revision=1)
    return app, project, revised


@pytest.mark.parametrize('version', [3, 4])
def test_real_typed_audio_projects_and_history_restore(studio, tmp_path, version):
    app, project, revised = studio
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = manifest(folder)
    assert len(value['sidecars']) == 2
    if version == 3:
        value['version'] = 3
        value['state'] = [{**entry, 'filename': entry['filename'][len('state/'):]} for entry in value.pop('sidecars')]
    digest = resign(folder, value)
    original = files(folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    from carr_library.studio.project import ProjectStore
    assert ProjectStore(target / 'state' / 'studio' / 'projects').load(project.id) == revised
    assert files(folder) == original
