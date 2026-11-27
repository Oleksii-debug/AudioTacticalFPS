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
