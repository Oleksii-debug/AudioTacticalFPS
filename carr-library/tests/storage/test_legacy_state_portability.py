"""Canonical portable-name fixes applied to the existing historical read adapter."""
from hashlib import sha256
import json

import pytest

from carr_library.backup_legacy_state import validate_state, verify_state
from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from .backup_compat_fixture import create_historical_backup


def entry(name, data=b'x'):
    return {'filename': name, 'size_bytes': len(data), 'sha256': sha256(data).hexdigest()}


@pytest.mark.parametrize('name', [
    '.', 'draft?.json', 'draft*.json', 'draft".json', 'draft<.json',
    'draft>.json', 'folder/part|one.json', 'COM\u00b9.json',
    'folder/LPT\u00b2.json', 'com\u00b3.txt', 'CONIN$', 'conout$.json',
    'AUX .json', 'folder//part.json', 'folder\\part.json',
])
def test_invalid_legacy_names_are_refused_before_any_filesystem_read(tmp_path, name):
    target = tmp_path / 'new-destination'
    with pytest.raises(ValueError, match='Invalid portable legacy'):
        verify_state(tmp_path / 'absent-source', target, [entry(name)])
    assert not target.exists()


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'),
    ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('caf\u00e9.json', 'cafe\u0301.json'),
    ('folder', 'folder/item.json'),
    ('folder/item.json', 'folder'),
])
def test_legacy_aliases_and_file_directory_conflicts_precede_destination_creation(tmp_path, names):
    target = tmp_path / 'new-destination'
    with pytest.raises(ValueError, match='Conflicting'):
        verify_state(tmp_path / 'absent-source', target, [entry(name) for name in names])
    assert not target.exists()


def test_ordinary_unicode_and_exact_shared_ancestors_remain_valid():
    validate_state([entry('Folder/one.json'), entry('Folder/two.json'),
                    entry('Other/one.json'), entry('knihy/Голос.json'),
                    entry('folder2/COM10.json'), entry('folder2/.draft.json')])


def test_actual_historical_unicode_copy_keeps_exact_original_and_streamed_bytes(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    values = {'knihy/Голос.json': 'Точний незмінний оригінал'.encode('utf-8'),
              'Folder/one.json': b'one', 'Folder/two.json': b'two',
              'folder2/COM10.json': b'ordinary device-like name',
              'folder2/.draft.json': b''}
    for name, data in values.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    entries = [entry(name, data) for name, data in values.items()]
    verify_state(source, target, entries)
    verify_state(target, None, entries)
    assert {name: (source / name).read_bytes() for name in values} == values
    assert {name: (target / name).read_bytes() for name in values} == values


@pytest.mark.parametrize('names', [
    ('draft?.json',),
    ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('folder', 'folder/item.json'),
])
def test_full_legacy_restore_dispatch_rejects_portability_before_missing_payload_or_publish(tmp_path, names):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Original library')
    original = app.add_text(workspace, 'Original', 'Unchanged verified source.')
    backup = tmp_path / 'backup'
    create_historical_backup(app, backup)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['version'] == 2
    manifest.update(version=3, state=[entry(name) for name in names],
                    generated_audio_included=False)
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    manifest_path.write_bytes(raw)
    database_before = app.database.read_bytes()
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='Invalid portable legacy|Conflicting legacy'):
        restore_backup(backup, destination, sha256(raw).hexdigest())
    assert not destination.exists()
    assert manifest_path.read_bytes() == raw
    assert app.database.read_bytes() == database_before
    assert app.search(workspace, 'Unchanged')[0].edition == original['edition']
