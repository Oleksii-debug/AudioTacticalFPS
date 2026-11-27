"""Checked backups preserve real reader/editor state without source retargeting."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.ocr_languages import load_presets, store_preset
from carr_library.extract.page_ocr import Config
from carr_library.ingest.writer_guard import WriterBusy
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.storage.revisions import Storage


def manifest_digest(folder, manifest=None):
    path = folder / 'manifest.json'
    if manifest is not None:
        path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    return sha256(path.read_bytes()).hexdigest()


def file_bytes(folder):
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in folder.rglob('*') if path.is_file()}


@pytest.fixture
def reading_library(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    a = app.create_workspace('Active reading')
    b = app.create_workspace('Archived reading')
    source = 'Незмінний текст. Čítanie e\u0301. Exact original source.'
    pages = []
    for workspace, author in ((a, 'Автор'), (b, 'Autorka')):
        app.add_text(workspace, 'Same title', source, authors=(author,),
                     work_id='same-work', edition_id='same-edition')
        pages.append(ReadingDesk(app, workspace, 'same-edition').page(1))
    state = ReadingState(app.root)
    bookmarks = []
    for page in pages:
        state.remember(page.location)
        bookmarks.append(state.add_bookmark(page.location, 'Мій початок — úvod 📚'))
    project = new_project(pages[0].text, title='Reading copy', source_location=pages[0].location)
    store = NarrationStore(app.root)
    store.save(project)
    draft = revise(project, 'An explicitly edited reading copy.', label='Unsaved recovery draft')
    store.save(draft, draft=True)
    manual = new_project('An independent script.', title='Manual text')
    store.save(manual)
    KeybindingStore(app.root).save((Command('Library/Backup', 'Back up libraries', 'Ctrl+F8'),),
                                 {'Library/Backup': 'Ctrl+Shift+F8'})
    store_preset(app.root, 'Українська, English та slovenčina',
                 Config(languages='ukr+eng+slk', orientation=True))
    # A valid older reading position must survive both metadata revision and
    # library archival; a backup must not silently promote it to a new revision.
    revision = app.update_metadata(a, 'same-edition', title='New catalog title',
                                   authors=('Автор',),
                                   expected_metadata=app.edition_metadata(a, 'same-edition'))
    assert revision != pages[0].location.revision
    app.archive_workspace(b)
    return app, a, b, pages, bookmarks, project, draft, manual


def test_complete_backup_preserves_reader_projects_drafts_and_shortcuts(reading_library, tmp_path):
    app, a, b, pages, bookmarks, project, draft, manual = reading_library
    folder = tmp_path / 'Резервна копія'
    source_files = file_bytes(app.root)
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['version'] == 4 and manifest['generated_audio_included'] is False
    reader_entries = [entry for entry in manifest['sidecars']
                      if not entry['filename'].startswith('pending-imports/')]
    assert any(entry['filename'] == 'pending-imports/manifest.json' for entry in manifest['sidecars'])
    names = {entry['filename'] for entry in reader_entries}
    assert names == {'reading-state.json', 'keybindings.json', 'ocr-language-presets.json',
                     f'narration-projects/{project.id}.json',
                     f'narration-projects/{project.id}.draft.json',
                     f'narration-projects/{manual.id}.json'}
    for entry in reader_entries:
        raw = (folder / entry['filename']).read_bytes()
        assert raw == source_files[entry['filename']]
        assert len(raw) == entry['size_bytes']
        assert sha256(raw).hexdigest() == entry['sha256']
    backup_files = file_bytes(folder)
    restored_path = restore_backup(folder, tmp_path / 'Відновлена бібліотека', digest)
    restored = LibraryApp(restored_path)
    state = ReadingState(restored_path)
    for page, bookmark in zip(pages, bookmarks):
        loc = page.location
        assert state.resume(loc.workspace, loc.edition) == loc
        assert state.bookmarks(loc.workspace, loc.edition) == (bookmark,)
    assert NarrationStore(restored_path).load(project.id)[0] == project
    assert NarrationStore(restored_path).load(project.id, draft=True)[0] == draft
    assert NarrationStore(restored_path).load(manual.id)[0] == manual
    assert KeybindingStore(restored_path).load() == {'Library/Backup': 'Ctrl+Shift+F8'}
    assert load_presets(restored_path) == load_presets(app.root)
    assert next(w for w in restored.workspaces(include_archived=True) if w.id == b).archived
    with pytest.raises(ValueError, match='no longer active'):
        ReadingDesk(restored, a, 'same-edition', revision=pages[0].location.revision)
    current = ReadingDesk(restored, a, 'same-edition').page(1)
    assert current.text == pages[0].text and current.location != pages[0].location
    assert file_bytes(folder) == backup_files
    assert all((restored_path / name).stat().st_nlink == 1 for name in names)
    assert not (restored_path / INCOMPLETE).exists()
    assert all((app.root / name).read_bytes() == source_files[name] for name in names)


@pytest.mark.parametrize('target', ['reading-state', 'narration', 'draft', 'keybindings', 'ocr'])
def test_changed_sidecar_cannot_restore_even_when_still_valid_json(reading_library, tmp_path, target):
    app, _, _, _, _, project, _, _ = reading_library
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    paths = {'reading-state': folder / 'reading-state.json',
             'narration': folder / 'narration-projects' / f'{project.id}.json',
             'draft': folder / 'narration-projects' / f'{project.id}.draft.json',
             'keybindings': folder / 'keybindings.json',
             'ocr': folder / 'ocr-language-presets.json'}
    path = paths[target]
    # Appending whitespace keeps JSON semantically valid but breaks its manifest
    # binding. Never silently discard a damaged sidecar and restore the rest.
    path.write_bytes(path.read_bytes() + b' ')
    destination = tmp_path / 'must-not-appear'
    with pytest.raises(ValueError, match='sidecar'):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


@pytest.mark.parametrize('field,value', [
    ('workspace', 'missing-workspace'), ('edition', 'missing-edition'),
    ('revision', 'missing-revision'), ('asset_sha256', '0' * 64),
    ('text_sha256', '0' * 64), ('index', 2), ('kind', 'physical_page'),
])
def test_resigned_bookmark_must_match_exact_historical_source(
        reading_library, tmp_path, field, value):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    path = folder / 'reading-state.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    state['positions'][0]['location'][field] = value
    raw = json.dumps(state, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    entry = next(row for row in manifest['sidecars'] if row['filename'] == path.name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    digest = manifest_digest(folder, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='exact saved source'):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


@pytest.mark.parametrize('name', ['reading-state.json', 'keybindings.json', 'ocr-language-presets.json'])
@pytest.mark.parametrize('damage', ['truncated', 'symlink', 'hardlink', 'directory'])
def test_invalid_live_sidecar_aborts_backup_without_changing_library(
        reading_library, tmp_path, name, damage):
    app, *_ = reading_library
    path = app.root / name
    original = path.read_bytes()
    if damage == 'truncated':
        path.write_bytes(original[:7])
    else:
        path.unlink()
        if damage == 'directory':
            path.mkdir()
        else:
            external = tmp_path / 'external-state.json'
            external.write_bytes(original)
            if damage == 'symlink':
                path.symlink_to(external)
            else:
                os.link(external, path)
    before = app.database.read_bytes()
    folder = tmp_path / 'rejected-backup'
    with pytest.raises((OSError, ValueError)):
        create_backup(app, folder)
    assert not folder.exists()
    assert app.database.read_bytes() == before
    if damage == 'truncated':
        assert path.read_bytes() == original[:7]
    elif damage != 'directory':
        assert external.read_bytes() == original


@pytest.mark.parametrize('change', [
    'unknown', 'traversal', 'absolute', 'windows-path', 'duplicate', 'bool-size',
    'wrong-shape', 'extra-root', 'missing-file', 'extra-narration-file',
])
def test_sidecar_manifest_and_namespace_are_strict_before_database_open(
        reading_library, tmp_path, monkeypatch, change):
    app, *_ = reading_library
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    entry = manifest['sidecars'][0]
    if change in ('unknown', 'traversal', 'absolute', 'windows-path'):
        entry['filename'] = {'unknown': 'credentials.json', 'traversal': '../outside.json',
                             'absolute': '/outside.json', 'windows-path': 'C:\\outside.json'}[change]
    elif change == 'duplicate':
        manifest['sidecars'].append(dict(entry))
    elif change == 'bool-size':
        entry['size_bytes'] = True
    elif change == 'wrong-shape':
        manifest['sidecars'] = {}
    elif change == 'extra-root':
        (folder / 'credentials.json').write_text('not a backup input')
    elif change == 'missing-file':
        (folder / 'reading-state.json').unlink()
    else:
        (folder / 'narration-projects' / 'unlisted.json').write_text('{}')
    digest = manifest_digest(folder, manifest)

    def forbid_open(*args, **kwargs):
        pytest.fail('Invalid sidecar manifest reached writable database access')

    monkeypatch.setattr('carr_library.library_backup.Storage', forbid_open)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, digest)
    assert not destination.exists()


def test_database_and_reader_snapshot_share_real_writer_lock(reading_library, tmp_path, monkeypatch):
    app, _, _, pages, *_ = reading_library
    original = Storage.backup
    attempted = []

    def backup_and_try_reader_write(storage, path):
        original(storage, path)
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(ReadingState(app.root).remember, pages[0].location)
            with pytest.raises(WriterBusy):
                future.result(timeout=5)
        attempted.append(True)

    monkeypatch.setattr(Storage, 'backup', backup_and_try_reader_write)
    destination = tmp_path / 'consistent-backup'
    create_backup(app, destination)
    assert attempted == [True]
    assert (destination / 'reading-state.json').read_bytes() == (app.root / 'reading-state.json').read_bytes()


@pytest.mark.parametrize('mode', ['backup', 'restore'])
def test_interrupted_sidecar_publication_never_looks_complete(
        reading_library, tmp_path, monkeypatch, mode):
    app, *_ = reading_library
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup) if mode == 'restore' else None
    destination = tmp_path / 'interrupted'
    original_link = os.link

    def fail_sidecar(source, target, *args, **kwargs):
        if str(target) == str(destination / 'reading-state.json'):
            raise OSError('Simulated sidecar publication interruption')
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(os, 'link', fail_sidecar)
    with pytest.raises(OSError, match='publication interruption'):
        if mode == 'backup':
            create_backup(app, destination)
        else:
            restore_backup(backup, destination, digest)
    if mode == 'backup':
        assert not (destination / 'manifest.json').exists()
    else:
        assert (destination / INCOMPLETE).is_file()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(destination)
    with pytest.raises(FileExistsError):
        create_backup(app, destination)


@pytest.mark.parametrize('version', [1, 2])
def test_legacy_backups_remain_usable_without_inventing_reading_state(tmp_path, version):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Legacy research')
    app.add_text(workspace, 'Legacy book', 'Exact legacy text.')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['version'] == 4 and manifest['sidecars']
    assert all(entry['filename'].startswith('pending-imports/') for entry in manifest['sidecars'])
    assert manifest['generated_audio_included'] is False
    # Earlier formats did not carry pending journals or captured sources.
    shutil.rmtree(folder / 'pending-imports')
    manifest['version'] = version
    del manifest['sidecars']
    del manifest['generated_audio_included']
    if version == 1:
        del manifest['schema_fingerprint']
    restored = restore_backup(folder, tmp_path / 'restored', manifest_digest(folder, manifest))
    assert len(LibraryApp(restored).search(workspace, 'legacy')) == 1
    assert not (restored / 'reading-state.json').exists()
    assert not (restored / 'narration-projects').exists()
    assert not (restored / 'keybindings.json').exists()
    assert not (restored / 'ocr-language-presets.json').exists()
