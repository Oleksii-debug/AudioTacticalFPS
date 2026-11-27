"""A snapshot must notice changed bytes even when file timestamps do not move."""
import pytest

from carr_library import state_backup


def test_same_size_rewrite_with_unchanged_metadata_invalidates_snapshot(tmp_path, monkeypatch):
    source, target = tmp_path / 'state', tmp_path / 'snapshot'
    source.mkdir()
    target.mkdir()
    first = source / 'a.json'
    first.write_bytes(b'old')
    (source / 'b.json').write_bytes(b'b' * (2 * 1024 * 1024))
    # Windows creation time does not track writes; coarse/restored last-write
    # times are not a content identity. Retain real device, inode and byte size.
    monkeypatch.setattr(state_backup, '_stamp',
                        lambda info: (info.st_dev, info.st_ino, info.st_size, 0, 0))
    changed = False
    def concurrent_writer():
        nonlocal changed
        if not changed and (target / 'a.json').exists() and (target / 'b.json').exists():
            first.write_bytes(b'new')
            changed = True
        return False
    with pytest.raises(ValueError, match='changed|snapshot|backup'):
        state_backup.snapshot_state(source, target, cancelled=concurrent_writer)
    assert changed
    assert (target / 'a.json').read_bytes() == b'old'
    assert first.read_bytes() == b'new'
