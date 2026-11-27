"""Restore both published V3 manifest shapes without weakening source binding."""
from hashlib import sha256
import json
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import FILENAME, ReadingState, snapshot_locations


def manifest(folder):
    return json.loads((folder / 'manifest.json').read_bytes())


def legacy_manifest(folder):
    """Build an exact published V3 fixture from today's verified V4 producer."""
    value = manifest(folder)
    assert value['version'] == 4
    assert value.pop('generated_audio_included') is False
    # Explicitly remove the newer captured-import component from this
    # synthetic historical fixture; current V4 backups retain that history.
    value['sidecars'] = [entry for entry in value['sidecars']
                         if not entry['filename'].startswith('pending-imports/')]
    if (folder / 'pending-imports').exists():
        shutil.rmtree(folder / 'pending-imports')
    value['version'] = 3
    return value


def seal(folder, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def saved_place(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Історія')
    book = app.add_text(workspace, 'Original', 'One segment.\fДругий розділ.\r\n')
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Café — точне місце')
    return app, page, bookmark


@pytest.mark.parametrize('shape', ['reading_state', 'sidecars'])
def test_v3_shapes_restore_exact_same_saved_source(saved_place, tmp_path, shape):
    app, page, bookmark = saved_place
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = legacy_manifest(folder)
    assert value['version'] == 3 and set(value['sidecars'][0]) == {'filename', 'size_bytes', 'sha256'}
    if shape == 'reading_state':
        value['reading_state'], = value.pop('sidecars')
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', seal(folder, value)))
    state = ReadingState(restored.root)
    where = page.location
    assert state.path.read_bytes() == (app.root / FILENAME).read_bytes()
    assert state.resume(where.workspace, where.edition) == where
    assert state.bookmarks(where.workspace, where.edition) == (bookmark,)
    assert ReadingDesk(restored, where.workspace, where.edition).validate(where) == page


def test_earlier_v3_explicit_absence_does_not_create_reading_state(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Empty reader')
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = legacy_manifest(folder)
    assert value.pop('sidecars') == []
    value.update(version=3, reading_state=None)
    restored = restore_backup(folder, tmp_path / 'restored', seal(folder, value))
    assert not (restored / FILENAME).exists()


@pytest.mark.parametrize('damage', ['both_shapes', 'neither_shape', 'foreign_filename', 'false_absence', 'source_digest'])
def test_legacy_shape_does_not_hide_extra_files_or_forge_provenance(saved_place, tmp_path, damage):
    app, _, _ = saved_place
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    value = legacy_manifest(folder)
    entry, = value.pop('sidecars')
    value['reading_state'] = entry
    if damage == 'both_shapes':
        value['sidecars'] = [entry]
    elif damage == 'neither_shape':
        del value['reading_state']
    elif damage == 'foreign_filename':
        entry['filename'] = 'keybindings.json'
    elif damage == 'false_absence':
        value['reading_state'] = None
    else:
        reading = json.loads((folder / FILENAME).read_bytes())
        reading['positions'][0]['location']['text_sha256'] = '0' * 64
        raw = json.dumps(reading).encode()
        (folder / FILENAME).write_bytes(raw)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError):
        restore_backup(folder, destination, seal(folder, value))
    assert not destination.exists()


def test_both_reading_snapshot_api_names_preserve_identical_bytes(saved_place):
    app, page, _ = saved_place
    state = ReadingState(app.root)
    before = state.path.read_bytes()
    assert state.snapshot() == state.snapshot_bytes() == before
    assert snapshot_locations(before) == (page.location,)
    positions, bookmarks, identity, raw = state._read_validated()
    assert positions and bookmarks and identity and raw == before
    assert state.path.read_bytes() == before
