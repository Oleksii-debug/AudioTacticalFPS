"""Actual format4 snapshots preserve both keyboard generations and user drafts."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.keyboard_preferences import KeymapStore
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import restore_backup
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry


@pytest.fixture
def complete_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Keyboard and recovery source')
    topic = app.create_topic(workspace, 'Research')
    imported = app.add_text(workspace, 'Exact source', 'Exact original source. Український текст.')
    hit, = app.search(workspace, 'Exact')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved baseline')
    row, = app.evidence(workspace, topic)
    draft = EvidenceDrafts(app.root).checkpoint(row, 'Uncommitted personal thought. Нотатка.')
    page = ReadingDesk(app, workspace, imported['edition']).page(1)
    ReadingState(app.root).remember(page.location)
    project = new_project(page.text, title='Separate reading copy', source_location=page.location)
    NarrationStore(app.root).save(project)
    KeybindingStore(app.root).save((Command('File/Save current changes', 'Save', 'Ctrl+S'),),
                                  {'File/Save current changes': 'Ctrl+Alt+S'})
    KeymapStore(app.root / 'keyboard-shortcuts.json').save(
        {'file.save': 'Ctrl+Alt+S', 'reader.reading_copy': 'Ctrl+Alt+Y'},
        CommandRegistry(builtin_commands()))
    return app, workspace, topic, draft, project, imported


@pytest.mark.parametrize('stale', [False, True])
def test_format4_restores_exact_profiles_reading_copy_and_uncommitted_draft(complete_library, tmp_path, stale):
    app, workspace, topic, draft, project, imported = complete_library
    if stale:
        with app.evidence_transaction(workspace) as evidence:
            evidence.edit_note(topic, draft.evidence, 'A later saved baseline')
    folder = tmp_path / 'backup'
    digest = app.backup(folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4
    names = {entry['filename'] for entry in manifest['sidecars']}
    assert {'pending-imports/manifest.json', 'pending-imports/imports.sqlite'} <= names
    # The import producer builds its own snapshot envelope; these direct-copy
    # producers must still preserve their exact original bytes and filenames.
    names = {name for name in names if not name.startswith('pending-imports/')}
    assert names == {'keybindings.json', 'keyboard-shortcuts.json', 'evidence-drafts.json',
                     'reading-state.json', f'narration-projects/{project.id}.json'}
    original = {name: (app.root / name).read_bytes() for name in names}
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    assert all((restored.root / name).read_bytes() == raw for name, raw in original.items())
    assert all((app.root / name).read_bytes() == raw for name, raw in original.items())
    recovered = EvidenceDrafts(restored.root).get(*draft.key)
    current, = restored.evidence(workspace, topic)
    assert recovered == draft and recovered.matches(current) is (not stale)
    assert current.note == ('A later saved baseline' if stale else 'Saved baseline')
    assert NarrationStore(restored.root).load(project.id)[0] == project
    assert restored.assets.resolve(imported['asset']).read_bytes() == app.assets.resolve(imported['asset']).read_bytes()
    assert EvidenceDrafts(restored.root).discard(recovered)
    assert restored.evidence(workspace, topic)[0].note == current.note


@pytest.mark.parametrize('name', ['keyboard-shortcuts.json', 'evidence-drafts.json'])
@pytest.mark.parametrize('direction', ['backup', 'restore'])
def test_invalid_registered_sidecars_fail_whole_snapshot_even_with_new_hash(complete_library, tmp_path, name, direction):
    app, *_ = complete_library
    folder = tmp_path / 'backup'
    if direction == 'backup':
        (app.root / name).write_bytes(b'{"version":true,"private":"synthetic"}')
        with pytest.raises(ValueError, match='sidecar'):
            app.backup(folder)
        assert not folder.exists()
    else:
        app.backup(folder)
        raw = b'{"version":true,"private":"synthetic"}'
        (folder / name).write_bytes(raw)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        entry = next(row for row in manifest['sidecars'] if row['filename'] == name)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        (folder / 'manifest.json').write_bytes(manifest_bytes)
        with pytest.raises(ValueError, match='sidecar'):
            restore_backup(folder, tmp_path / 'rejected', sha256(manifest_bytes).hexdigest())
        assert not (tmp_path / 'rejected').exists()
