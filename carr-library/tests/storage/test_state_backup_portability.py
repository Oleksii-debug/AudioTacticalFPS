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


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'),
    ('A/one.json', 'a/two.json'),
])
def test_snapshot_refuses_unrestorable_portable_names(tmp_path, names):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    for name in names:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        if file.exists():
            pytest.skip('Filesystem does not support case-distinct names.')
        file.write_bytes(b'x')
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*.json')}
    actual.update(p.relative_to(source).as_posix() for p in source.rglob('*.JSON'))
    if not set(names) <= actual:
        pytest.skip('Filesystem aliases the requested directory spellings.')
    with pytest.raises(ValueError, match='Duplicate|collide'):
        snapshot_state(source, target)
    assert all((source / name).read_bytes() == b'x' for name in names)
