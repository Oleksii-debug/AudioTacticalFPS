"""Portable state manifests must be valid before publication, not only restore."""
from hashlib import sha256

import pytest

from carr_library.state_backup import snapshot_state, validate_state


def entry(name):
    return dict(filename=name, size_bytes=1, sha256=sha256(b'x').hexdigest())


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'),
    ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('folder', 'folder/item.json'),
    ('folder/item.json', 'folder'),
])
def test_portable_manifest_rejects_collisions(names):
    with pytest.raises(ValueError, match='Duplicate|collide'):
        validate_state([entry(name) for name in names])


def test_portable_manifest_allows_distinct_siblings():
    validate_state([entry('Folder/one.json'), entry('Folder/two.json'),
                    entry('Other/one.json')])


@pytest.mark.parametrize('name', [
    '.', 'draft?.json', 'draft*.json', 'draft".json',
    'draft<.json', 'draft>.json', 'folder/part|one.json',
    'COM\u00b9.json', 'folder/LPT\u00b2.json', 'com\u00b3.txt',
    'CONIN$', 'conout$.json', 'AUX .json',
])
def test_portable_manifest_rejects_non_windows_names(name):
    with pytest.raises(ValueError, match='Invalid'):
        validate_state([entry(name)])


def test_portable_manifest_keeps_ordinary_unicode_names():
    validate_state([entry('knihy/\u0413\u043e\u043b\u043e\u0441.json'),
                    entry('folder/COM10.json'), entry('folder/.draft.json')])


def test_portable_snapshot_roundtrip_unicode(tmp_path):
    from carr_library.state_backup import restore_state

    root = tmp_path / 'source'
    names = ('knihy/\u0413\u043e\u043b\u043e\u0441.json', 'folder/COM10.json',
             'folder/.draft.json')
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode('utf-8'))
    stage, restored = tmp_path / 'stage', tmp_path / 'restored'
    entries = snapshot_state(root, stage)
    restore_state(stage, restored, entries)
    assert {item['filename'] for item in entries} == set(names)
    for name in names:
        assert (restored / name).read_bytes() == (root / name).read_bytes()
