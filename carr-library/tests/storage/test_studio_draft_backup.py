"""Real backups retain Studio draft baselines without restoring edit authority."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_compat, backup_producers
from carr_library.durable_state import MAX_JSON_BYTES, atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_recovery import _prepared, validate_state_documents
from carr_library.studio.project import ProjectStore, parse_script, validate_studio_draft
from carr_library.ui.studio_dialog import StudioController


NAME = 'state/studio/draft.json'
KEYS = {'fixture.studio-draft': 'Ctrl+F9'}
FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))


def prepared(tmp_path, legacy=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Studio recovery')
    app.add_text(workspace, 'Original', 'Exact immutable source for Studio recovery.')
    KeybindingStore(app.root).save(
        [Command('fixture.studio-draft', 'Synthetic Studio recovery command')], KEYS)
    store = ProjectStore(app.root / 'state' / 'studio' / 'projects')
    service = SimpleNamespace(projects=store)
    controller = StudioController(app, service=service)
    original = controller.save('Narrator: An earlier version.', 'Original title', ('test:voice',))
    newer = replace(original, title='A newer project', revision=2)
    store.save(newer, expected_revision=1, expected_fingerprint=original.fingerprint)
    controller.autosave('Narrator: Unsaved historical text.', original.title, ('test:voice',))
    if legacy:
        value = read_json(controller.draft)
        del value['project_revision'], value['project_fingerprint']
        atomic_json(controller.draft, value, replace=True)
    return app, workspace, controller, original, newer


def snapshot(app, folder, flat):
    if flat:
        return FLAT['_flat_backup'](app, folder)
    digest = create_backup(app, folder)
    return read_json(folder / 'manifest.json'), digest


def reseal(folder, manifest, raw, flat):
    (folder / NAME).write_bytes(raw)
    rows = manifest['sidecars'] if flat else manifest['state']
    wanted = NAME if flat else 'studio/draft.json'
    row, = [entry for entry in rows if entry['filename'] == wanted]
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    payload = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(payload)
    return sha256(payload).hexdigest(), payload


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('legacy', [False, True], ids=['anchored-draft', 'historical-unversioned'])
def test_exact_draft_roundtrip_cannot_overwrite_a_newer_saved_project(tmp_path, monkeypatch, flat, legacy):
    app, workspace, controller, original, newer = prepared(tmp_path, legacy)
    raw = controller.draft.read_bytes()
    project_path = controller.service.projects.directory(original.id) / 'project.json'
    project_raw = project_path.read_bytes()
    keymap_raw = KeybindingStore(app.root).path.read_bytes()
    monkeypatch.setattr(subprocess, 'Popen',
                        lambda *_a, **_k: pytest.fail('Passive Studio recovery launched an engine.'))
    folder = tmp_path / 'backup'
    manifest, digest = snapshot(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5) and manifest['schema_version'] == 6
    assert (folder / NAME).read_bytes() == raw
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert (target / NAME).read_bytes() == raw
    assert (target / project_path.relative_to(app.root)).read_bytes() == project_raw
    assert KeybindingStore(target).load() == KEYS
    assert KeybindingStore(target).path.read_bytes() == keymap_raw
    assert LibraryApp(target).search(workspace, 'immutable')
    store = ProjectStore(target / 'state' / 'studio' / 'projects')
    reopened = StudioController(SimpleNamespace(root=target), service=SimpleNamespace(projects=store))
    draft = read_json(target / NAME)
    notice = reopened.restore_draft_project(draft)
    if legacy:
        assert 'separate project' in notice and reopened.project is None
        created = reopened.save(draft['text'], draft['title'], draft['voice_ids'])
        assert created.id != original.id and created.original_script == draft['text']
    else:
        assert 'historical revision' in notice and reopened.project == original
        with pytest.raises(ValueError, match='Project changed'):
            reopened.save(draft['text'], draft['title'], draft['voice_ids'])
        assert reopened.project == original
    assert store.load(original.id) == newer
    assert (target / project_path.relative_to(app.root)).read_bytes() == project_raw
    assert controller.draft.read_bytes() == (folder / NAME).read_bytes() == raw


@pytest.mark.parametrize('flat', [False, True], ids=['native-format5', 'legacy-flat4'])
@pytest.mark.parametrize('change', ['boolean-revision', 'missing-pair', 'nul-fingerprint', 'unknown-authority', 'malformed-json'])
def test_rehashed_invalid_draft_cannot_enter_native_or_flat_recovery(tmp_path, flat, change):
    app, _, controller, _, _ = prepared(tmp_path)
    folder = tmp_path / 'invalid'
    manifest, _ = snapshot(app, folder, flat)
    value = read_json(folder / NAME)
    if change == 'boolean-revision':
        value['project_revision'] = True
    elif change == 'missing-pair':
        del value['project_fingerprint']
    elif change == 'nul-fingerprint':
        value['project_fingerprint'] = 'a' * 64 + '\x00'
    elif change == 'unknown-authority':
        value['overwrite_current_project'] = True
    raw = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
    if change == 'malformed-json':
        raw = b'{"schema":'
    digest, manifest_raw = reseal(folder, manifest, raw, flat)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, destination, digest)
    assert not destination.exists()
    assert (folder / NAME).read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == manifest_raw


def test_typed_draft_validation_is_passive_and_preserves_both_existing_shapes(tmp_path, monkeypatch):
    _, _, controller, _, _ = prepared(tmp_path)
    anchored = read_json(controller.draft)
    legacy = {key: value for key, value in anchored.items()
              if key not in ('project_revision', 'project_fingerprint')}
    before = deepcopy(anchored), deepcopy(legacy)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Passive draft validation consulted external state.')

    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(ProjectStore, 'load', forbidden)
    for value in (anchored, legacy):
        assert validate_studio_draft(value) is value
        backup_producers.validate(None, NAME, value)
        assert _prepared('studio/draft.json', value) is None
    assert (anchored, legacy) == before


def test_native_draft_limit_is_checked_before_json_materialization(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir()
    def forbidden(*_args, **_kwargs):
        pytest.fail('Oversized typed draft was opened.')
    from carr_library import state_recovery
    monkeypatch.setattr(state_recovery, 'read_json', forbidden)
    with pytest.raises(ValueError, match='JSON budget'):
        validate_state_documents(root, [{'filename': 'studio/draft.json',
            'size_bytes': MAX_JSON_BYTES + 1, 'sha256': 'a' * 64}])
    assert backup_compat.sidecar_limit(NAME) == MAX_JSON_BYTES


def test_multibyte_draft_respects_existing_encoded_budget_without_writing(tmp_path, monkeypatch):
    from carr_library.studio import project
    _, _, controller, _, _ = prepared(tmp_path)
    raw = controller.draft.read_bytes()
    monkeypatch.setattr(project, 'MAX_JSON_BYTES', 512)
    with pytest.raises(ValueError, match='byte budget'):
        controller.autosave('Я' * 200, 'A draft', ('test:voice',))
    assert controller.draft.read_bytes() == raw


def test_unknown_historical_anchor_never_binds_the_current_project(tmp_path):
    _, _, controller, original, newer = prepared(tmp_path)
    draft = read_json(controller.draft)
    draft['project_fingerprint'] = '0' * 64
    atomic_json(controller.draft, draft, replace=True)
    raw = controller.draft.read_bytes()
    with pytest.raises(FileNotFoundError):
        controller.restore_draft_project(draft)
    assert controller.project is None
    assert controller.service.projects.load(original.id) == newer
    assert controller.draft.read_bytes() == raw
