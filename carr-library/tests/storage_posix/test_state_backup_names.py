"""Actual POSIX names that Windows filesystems cannot create.

These unchanged cases complement the portable manifest and restore contracts in
storage/test_state_backup_portability.py and remain required by Linux CI.
"""
import pytest

from carr_library.state_backup import snapshot_state


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



def test_snapshot_rejects_nonportable_real_source(tmp_path):
    import os

    if os.name == 'nt':
        pytest.skip('POSIX-only filename fixture')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'draft?.json').write_bytes(b'x')
    with pytest.raises(ValueError, match='Invalid'):
        snapshot_state(root, tmp_path / 'stage')
    assert not (tmp_path / 'stage').exists()

