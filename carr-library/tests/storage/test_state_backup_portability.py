"""Portable state manifests must be valid before publication, not only restore."""
from hashlib import sha256

import pytest

from carr_library.state_backup import validate_state


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


def test_snapshot_rejects_nonportable_real_source(tmp_path):
    import os
    from carr_library.state_backup import snapshot_state

    if os.name == 'nt':
        pytest.skip('POSIX-only filename fixture')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'draft?.json').write_bytes(b'x')
    with pytest.raises(ValueError, match='Invalid'):
        snapshot_state(root, tmp_path / 'stage')
    assert not (tmp_path / 'stage').exists()
