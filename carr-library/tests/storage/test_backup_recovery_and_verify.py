"""Registered recovery/settings bytes and read-only complete verification."""
from hashlib import sha256
import json

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.keyboard_preferences import KeymapStore
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from .backup_compat_fixture import historical_backup
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.command_registry import CommandRegistry, CommandSpec


@pytest.fixture
def recovery_library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    topic = app.create_topic(workspace, 'Evidence')
    book = app.add_text(workspace, 'Book', 'Exact original quotation.')
    hit, = app.search(workspace, 'Exact')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved note')
    row, = app.evidence(workspace, topic)
    draft = EvidenceDrafts(app.root).checkpoint(row, 'Незбережена чернетка нотатки.')
    registry = CommandRegistry((CommandSpec('library.import', 'Import', default_shortcut='Ctrl+I'),))
    KeymapStore(app.root / 'keyboard-shortcuts.json').save({'library.import': 'Ctrl+Shift+K'}, registry)
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    ReadingState(app.root).remember(page.location)
    return app, workspace, topic, evidence, draft


@pytest.mark.parametrize('historical', [None, 3, 4], ids=['current-v5', 'compatible-v3', 'compatible-v4'])
def test_recovery_notes_and_keymap_keep_exact_bindings(recovery_library, tmp_path, historical):
    app, workspace, topic, evidence, draft = recovery_library
    expected = {name: (app.root / name).read_bytes()
                for name in ('evidence-drafts.json', 'keyboard-shortcuts.json', 'reading-state.json')}
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    if historical is not None:
        digest = historical_backup(backup)
    if historical == 4:
        # Preserve the earlier path/sidecars shape as an explicit fixture.
        manifest_path = backup / 'manifest.json'
        manifest = json.loads(manifest_path.read_bytes())
        (backup / 'sidecars').mkdir()
        for entry in manifest['sidecars']:
            name = entry.pop('filename')
            (backup / name).rename(backup / 'sidecars' / name)
            entry['path'] = name
        manifest['version'] = 4
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        digest = sha256(manifest_path.read_bytes()).hexdigest()
    manifest = verify_backup(backup, digest)
    assert manifest['version'] == (historical or 5)
    field = 'filename' if historical == 3 else 'path'
    assert {entry[field] for entry in manifest['sidecars']} == set(expected)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert {name: (restored / name).read_bytes() for name in expected} == expected
    assert KeymapStore(restored / 'keyboard-shortcuts.json').load() == {'library.import': 'Ctrl+Shift+K'}
    reopened = EvidenceDrafts(restored).get(workspace, topic, evidence)
    assert reopened == draft
    row, = LibraryApp(restored).evidence(workspace, topic)
    assert reopened.matches(row)
    assert row.note == 'Saved note'


def test_busy_keymap_uses_its_own_nonblocking_writer_lock(recovery_library, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, *_ = recovery_library
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened database before keymap lock'))
    with FileLock(app.root / 'keyboard-shortcuts.json.lock'):
        with pytest.raises(ValueError, match='Keyboard shortcuts'):
            create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_verify_hashes_sidecars_without_creating_duplicate_copies(recovery_library, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, *_ = recovery_library
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    before = {p.relative_to(backup): p.read_bytes() for p in backup.rglob('*') if p.is_file()}
    copied = library_backup._copy_sidecar
    targets = []
    def inspect(root, relative, target, identity, cancelled):
        targets.append(target)
        assert target is None
        return copied(root, relative, target, identity, cancelled)
    monkeypatch.setattr(library_backup, '_copy_sidecar', inspect)
    manifest = verify_backup(backup, digest)
    assert len(targets) == len(manifest['sidecars']) == 3
    assert {p.relative_to(backup): p.read_bytes() for p in backup.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('name', ['evidence-drafts.json', 'keyboard-shortcuts.json', 'reading-state.json'])
def test_resigned_or_corrupt_registered_state_cannot_be_verified(recovery_library, tmp_path, name):
    app, *_ = recovery_library
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    path = backup / 'sidecars' / name
    path.write_bytes(b'x' * path.stat().st_size)
    with pytest.raises(ValueError):
        verify_backup(backup, digest)
    # A new outer hash cannot turn invalid state syntax into valid recovery.
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['path'] == name)
    entry['sha256'] = sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        verify_backup(backup, sha256(manifest_path.read_bytes()).hexdigest())


@pytest.mark.parametrize('version', [1, 2, 3])
def test_verify_preserves_supported_old_formats(tmp_path, version):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Library')
    book = app.add_text(workspace, 'Source', 'Exact source text.')
    if version == 3:
        ReadingState(app.root).remember(ReadingDesk(app, workspace, book['edition']).page(1).location)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    historical_backup(backup)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['version'] = version
    if version == 1:
        del manifest['schema_fingerprint']
    elif version == 3:
        # The historical single-reading-file shape is still authenticated.
        manifest['reading_state'] = manifest.pop('sidecars')[0]
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    assert verify_backup(backup, digest) == manifest


def _resign_entry(backup, relative, value):
    path = backup / 'sidecars' / relative
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['path'] == relative)
    entry.update(size_bytes=path.stat().st_size, sha256=sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    return sha256(manifest_path.read_bytes()).hexdigest()


def _saved_reading_studio(tmp_path):
    from carr_library.ui.audio_studio import StudioController
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reading handoff')
    book = app.add_text(workspace, 'Original', 'Незмінний оригінал. Exact source.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    store = NarrationStore(app.root)
    project = revise(new_project(page.raw_text, source_location=page.location), 'Saved spoken reading.')
    saved_digest = store.save(project)
    studio = StudioController(private_root=app.root)
    studio.from_reading_project(project, saved_digest=saved_digest)
    path = studio.save_in_library()
    # A later reader edit must not invalidate the previous explicit handoff.
    newer = revise(project, 'Later reader revision independent of Studio.')
    store.save(newer, expected_digest=saved_digest)
    return app, studio, path, newer, page


def test_saved_reading_handoff_keeps_original_and_historical_revision_after_restore(tmp_path):
    from carr_library.ui.audio_studio import StudioController
    app, studio, path, newer, page = _saved_reading_studio(tmp_path)
    before = path.read_bytes()
    original = studio.draft.source_text
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    copied = restored / path.relative_to(app.root)
    reopened = StudioController(private_root=restored).open(copied)
    assert copied.read_bytes() == path.read_bytes() == before
    assert reopened.draft.source_text == original == page.raw_text
    assert reopened.script == 'Saved spoken reading.'
    assert reopened.source_reference == studio.source_reference
    assert reopened.output_dir.is_relative_to(restored)
    assert NarrationStore(restored).load(newer.id)[0] == newer


def test_resigned_saved_reading_reference_requires_exact_historical_source(tmp_path):
    app, _, path, _, _ = _saved_reading_studio(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    relative = path.relative_to(app.root).as_posix()
    payload = json.loads((backup / 'sidecars' / relative).read_bytes())
    payload['source_reference']['source_location']['workspace'] = 'forged-library'
    digest = _resign_entry(backup, relative, payload)
    with pytest.raises(ValueError, match='exact saved source'):
        verify_backup(backup, digest)
    with pytest.raises(ValueError, match='exact saved source'):
        restore_backup(backup, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()
