"""Current keyboard, evidence drafts and reading/Studio references survive backup."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.keybindings import Command, KeybindingStore
from carr_library.keyboard_preferences import KeymapStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.progress import ProgressPreferences, save_preferences, load_preferences
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import StudioController
from carr_library.ui.command_registry import CommandRegistry, CommandSpec


def test_both_keymap_generations_and_uncommitted_evidence_roundtrip(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Sources')
    topic = app.create_topic(workspace, 'Evidence')
    app.add_text(workspace, 'Original', 'Exact original source text.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
    evidence, = app.evidence(workspace, topic)
    draft = EvidenceDrafts(app.root).checkpoint(evidence, 'Незбережена думка.\r\nSlovenská poznámka.')
    KeybindingStore(app.root).save((Command('Library/Backup', 'Backup', 'Ctrl+F8'),),
                                 {'Library/Backup': 'Ctrl+Shift+F8'})
    registry = CommandRegistry((CommandSpec('library.search', 'Search', default_shortcut='Ctrl+F'),))
    KeymapStore(app.root / 'keyboard-shortcuts.json').save({'library.search': 'Ctrl+Shift+F'}, registry)
    progress = ProgressPreferences(mode='beep', interval_seconds=12, muted=True)
    save_preferences(app.root, progress)
    for name in ('providers.json', 'api-keys.json'):
        (app.root / name).write_text('SYNTHETIC PRIVATE VALUE — NOT AN APPROVED SIDECAR', encoding='utf-8')
    names = ('keybindings.json', 'keyboard-shortcuts.json', 'evidence-drafts.json', 'progress-preferences.json')
    original = {name: (app.root / name).read_bytes() for name in names}
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 3
    assert {row['filename'] for row in manifest['sidecars']} == set(names)
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert {name: (restored.root / name).read_bytes() for name in names} == original
    assert KeymapStore(restored.root / 'keyboard-shortcuts.json').load() == {'library.search': 'Ctrl+Shift+F'}
    assert load_preferences(restored.root) == progress
    recovered = EvidenceDrafts(restored.root).get(*draft.key)
    assert recovered == draft and recovered.matches(restored.evidence(workspace, topic)[0])
    assert restored.evidence(workspace, topic)[0].note == 'Saved baseline'
    assert not (restored.root / 'api-keys.json').exists()


@pytest.mark.parametrize('name', ['keyboard-shortcuts.json', 'evidence-drafts.json', 'progress-preferences.json'])
def test_malformed_new_productivity_state_is_not_silently_omitted(tmp_path, name):
    app = LibraryApp(tmp_path / 'library')
    (app.root / name).write_text('{"unrecognized":true}', encoding='utf-8')
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('forge_source', [False, True])
def test_managed_reading_studio_handoff_retains_exact_source_scope(tmp_path, forge_source):
    app = LibraryApp(tmp_path / 'library')
    workspace, other = app.create_workspace('Source'), app.create_workspace('Other')
    book = app.add_text(workspace, 'Original', 'Exact original.\r\nНезмінне джерело.')
    app.add_text(other, 'Same words, another source', 'Exact original.\r\nНезмінне джерело.',
                 edition_id=book['edition'])
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    reading = revise(new_project(page.raw_text, source_location=page.location), 'Explicit spoken derivative.')
    store = NarrationStore(app.root)
    saved_digest = store.save(reading)
    studio = StudioController(private_root=app.root)
    studio.from_reading_project(reading, saved_digest=saved_digest)
    project_path = studio.save_in_library()
    before = project_path.read_bytes()
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    if forge_source:
        relative = project_path.relative_to(app.root).as_posix()
        saved = folder / 'sidecars' / relative
        payload = json.loads(saved.read_bytes())
        payload['source_reference']['source_location']['workspace'] = other
        raw = json.dumps(payload, ensure_ascii=False).encode()
        saved.write_bytes(raw)
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        entry = next(row for row in manifest['sidecars'] if row['path'] == relative)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        manifest_path.write_bytes(json.dumps(manifest).encode())
        digest = sha256(manifest_path.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match='location disagrees'):
            restore_backup(folder, tmp_path / 'restored', digest)
        assert not (tmp_path / 'restored').exists()
    else:
        root = restore_backup(folder, tmp_path / 'restored', digest)
        assert (root / project_path.relative_to(app.root)).read_bytes() == before
        recovered = StudioController(private_root=root)
        recovered.open(root / project_path.relative_to(app.root))
        assert recovered.draft.source_text == page.raw_text
        assert recovered.script == reading.current_text
        assert recovered.source_reference['source_location']['workspace'] == workspace
        assert recovered.output_dir.is_relative_to(root)
