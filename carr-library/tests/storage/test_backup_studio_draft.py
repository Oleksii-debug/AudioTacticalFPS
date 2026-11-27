"""The actual editable Studio draft is inert canonical backup state."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.render import ProductionService
from carr_library.ui.studio_dialog import StudioController


def resign(folder, manifest):
    raw = encode(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def update_entry(folder, manifest, relative):
    path = folder / relative
    manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != relative]
    if path.exists():
        raw = path.read_bytes()
        manifest['sidecars'].append({'filename': relative, 'size_bytes': len(raw),
                                     'sha256': sha256(raw).hexdigest()})


@pytest.fixture
def studio(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Studio draft recovery')
    controller = StudioController(app)
    first = controller.save('First saved text.', 'First title', ())
    current = controller.save('Second saved text.', 'Second title', ())
    assert current.id == first.id and current.revision == 2
    yield app, controller, first, current
    controller.service.close()


@pytest.mark.parametrize('draft_text,draft_title', [
    ('', ''), ('[This direction is intentionally incomplete', 'Unparsed title'),
    ('Literal draft whose path-like words remain inert.', 'Long editable title ' * 50),
])
def test_actual_invalid_editable_draft_and_revision_history_roundtrip_without_runtime(
        studio, tmp_path, monkeypatch, draft_text, draft_title):
    app, controller, first, current = studio
    voices = ('C:/a literal unselected voice path/engine.exe', 'unknown:voice')
    controller.autosave(draft_text, draft_title, voices)
    relative = controller.draft.relative_to(app.root)
    expected = {path.relative_to(app.root): path.read_bytes()
                for path in (app.root / 'state/studio').rglob('*.json')}
    assert read_json(controller.draft)['project_id'] == current.id
    def forbidden(*args, **kwargs):
        pytest.fail('Passive Studio draft restore constructed a producer or ran a process.')
    with monkeypatch.context() as patch:
        patch.setattr(ProductionService, '__init__', forbidden)
        patch.setattr(subprocess, 'Popen', forbidden)
        folder, target = tmp_path / 'backup', tmp_path / 'restored'
        digest = create_backup(app, folder)
        restore_backup(folder, target, digest)
    assert {name: (target / name).read_bytes() for name in expected} == expected
    assert read_json(target / relative) == {
        'schema': 1, 'text': draft_text, 'title': draft_title,
        'voice_ids': list(voices), 'project_id': current.id}
    assert not (target / 'state/studio/.local-origin.json').exists()
    assert len(tuple((target / 'state/studio/projects' / current.id / 'revisions').glob('*.json'))) == 1


def test_first_real_save_keeps_its_preparse_null_project_draft(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    controller = StudioController(app)
    try:
        project = controller.save('Actual first saved text.', 'First title', ())
        assert read_json(controller.draft)['project_id'] is None
        folder, target = tmp_path / 'backup', tmp_path / 'restored'
        digest = create_backup(app, folder)
        restore_backup(folder, target, digest)
        assert read_json(target / 'state/studio/draft.json')['project_id'] is None
        restored_project = read_json(target / 'state/studio/projects' / project.id / 'project.json')
        # Compare the complete canonical JSON wire value; dataclass tuples
        # become JSON arrays without changing any retained field or value.
        assert encode(restored_project) == encode(project.to_dict())
    finally:
        controller.service.close()


@pytest.mark.parametrize('operation', ['snapshot', 'restore'])
@pytest.mark.parametrize('damage', ['unknown-field', 'old-malformed', 'schema-bool', 'text',
                                    'title', 'voices', 'voice-item', 'project-path',
                                    'missing-project', 'wrong-project', 'invented-revision'])
def test_malformed_or_dangling_studio_draft_remains_rejected(
        studio, tmp_path, operation, damage):
    app, controller, first, current = studio
    relative = 'state/studio/draft.json'
    folder = tmp_path / 'backup'
    if operation == 'restore':
        create_backup(app, folder)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        root = folder
    else:
        root = app.root
    path = root / relative
    value = json.loads(path.read_bytes())
    if damage == 'unknown-field':
        value['output_permission'] = str(tmp_path / 'unselected-output')
    elif damage == 'old-malformed':
        value = {'hello': 'not a producer draft'}
    elif damage == 'schema-bool':
        value['schema'] = True
    elif damage == 'text':
        value['text'] = ['not text']
    elif damage == 'title':
        value['title'] = None
    elif damage == 'voices':
        value['voice_ids'] = 'not a list'
    elif damage == 'voice-item':
        value['voice_ids'] = [False]
    elif damage == 'project-path':
        value['project_id'] = '../outside'
    elif damage == 'invented-revision':
        value['revision'] = current.revision
    elif damage == 'wrong-project':
        value['project_id'] = 'missing-but-well-formed-project'
    else:
        project_relative = 'state/studio/projects/' + current.id + '/project.json'
        (root / project_relative).unlink()
        if operation == 'restore':
            update_entry(root, manifest, project_relative)
    path.write_bytes(encode(value))
    if operation == 'restore':
        update_entry(root, manifest, relative)
        target = tmp_path / 'rejected'
        with pytest.raises(ValueError):
            restore_backup(folder, target, resign(folder, manifest))
        assert not target.exists()
    else:
        with pytest.raises(ValueError):
            create_backup(app, folder)
        assert not folder.exists()
