"""Canonical state-backup names remain portable when retained by the current reader."""
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_legacy_state import validate_state
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from .backup_compat_fixture import create_historical_backup


def entry(name, raw=b'x'):
    return {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'),
    ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('folder', 'folder/item.json'),
    ('folder/item.json', 'folder'),
    ('caf\u00e9.json', 'cafe\u0301.json'),
])
def test_historical_manifest_rejects_portable_path_collisions(names):
    with pytest.raises(ValueError, match='Conflicting'):
        validate_state([entry(name) for name in names])


@pytest.mark.parametrize('name', [
    '.', 'draft?.json', 'draft*.json', 'draft".json', 'draft<.json',
    'draft>.json', 'folder/part|one.json', 'folder\\part.json',
    'COM\u00b9.json', 'folder/LPT\u00b2.json', 'com\u00b3.txt',
    'CONIN$', 'conout$.json', 'AUX .json', 'folder/NUL.txt',
    'folder/ending. ', '../escaped', '/absolute', 'C:/absolute',
])
def test_historical_manifest_rejects_nonportable_names_before_filesystem_use(name):
    with pytest.raises(ValueError, match='Invalid'):
        validate_state([entry(name)])


def test_historical_manifest_keeps_unicode_siblings_and_exact_byte_budgets():
    entries = [entry('knihy/\u0413\u043e\u043b\u043e\u0441.json'),
               entry('Folder/one.json'), entry('Folder/two.json'),
               entry('Other/COM10.json'), entry('Other/.draft.json')]
    validate_state(entries, max_files=5, max_bytes=5)
    with pytest.raises(ValueError, match='budget'):
        validate_state(entries, max_bytes=4)


def seal(folder, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest(), raw


def historical_fixture(tmp_path, payloads):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Canonical snapshot')
    source = tmp_path / 'canonical-backup'
    create_historical_backup(app, source)
    manifest = json.loads((source / 'manifest.json').read_bytes())
    assert manifest['version'] == 2
    state = source / 'state'
    state.mkdir()
    for name, raw in payloads.items():
        target = state / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    manifest.update(version=3, state=[entry(name, raw) for name, raw in payloads.items()],
                    generated_audio_included=False)
    digest, raw = seal(source, manifest)
    return source, manifest, digest, raw


def test_portable_historical_state_restores_and_rebacks_exact_inert_bytes(tmp_path):
    payloads = {'knihy/\u0413\u043e\u043b\u043e\u0441.json': b'{"saved":"source"}\n',
                'Folder/COM10.json': b'{"saved":"another"}\r\n'}
    source, _, digest, manifest_raw = historical_fixture(tmp_path, payloads)
    before = {p.relative_to(source).as_posix(): p.read_bytes()
              for p in source.rglob('*') if p.is_file()}
    verify_backup(source, digest)
    restored = restore_backup(source, tmp_path / 'restored', digest)
    retained = restored / 'state' / 'historical-backups' / digest
    assert (retained / 'manifest.json').read_bytes() == manifest_raw
    for name, raw in payloads.items():
        assert (retained / 'files' / sha256(name.encode('utf-8')).hexdigest()).read_bytes() == raw
    assert not (restored / 'state' / 'knihy').exists()
    backup_again = tmp_path / 'backup-again'
    new_digest = create_backup(LibraryApp(restored), backup_again)
    verify_backup(backup_again, new_digest)
    second = restore_backup(backup_again, tmp_path / 'second', new_digest)
    again = second / 'state' / 'historical-backups' / digest
    assert {p.relative_to(again).as_posix(): p.read_bytes()
            for p in again.rglob('*') if p.is_file()} == {
                p.relative_to(retained).as_posix(): p.read_bytes()
                for p in retained.rglob('*') if p.is_file()}
    assert {p.relative_to(source).as_posix(): p.read_bytes()
            for p in source.rglob('*') if p.is_file()} == before


def test_invalid_historical_manifest_is_rejected_without_restore_publication(tmp_path):
    source, manifest, _, _ = historical_fixture(tmp_path, {})
    manifest['state'] = [entry('A/one.json'), entry('a/two.json')]
    digest, raw = seal(source, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='Conflicting'):
        verify_backup(source, digest)
    with pytest.raises(ValueError, match='Conflicting'):
        restore_backup(source, destination, digest)
    assert not destination.exists()
    assert (source / 'manifest.json').read_bytes() == raw
    assert tuple((source / 'state').iterdir()) == ()
