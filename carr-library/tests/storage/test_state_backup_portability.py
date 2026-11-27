"""PR141/145 regressions against the composed canonical state producer."""
from hashlib import sha256
import os
from pathlib import Path

import pytest

from carr_library import state_backup


def entry(name):
    return dict(filename=name, size_bytes=1, sha256=sha256(b'x').hexdigest())


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'), ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('folder', 'folder/item.json'), ('folder/item.json', 'folder'),
])
def test_portable_manifest_rejects_file_ancestor_and_unicode_aliases(names):
    with pytest.raises(ValueError, match='Duplicate|collide'):
        state_backup.validate_state([entry(name) for name in names])


def test_portable_manifest_accepts_distinct_siblings():
    state_backup.validate_state([entry('Folder/one.json'), entry('Folder/two.json'), entry('Other/one.json')])


@pytest.mark.parametrize('names', [('voice.json', 'VOICE.JSON'), ('A/one.json', 'a/two.json')])
def test_snapshot_obeys_the_actual_filesystem_names_without_skips(tmp_path, names):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    for number, name in enumerate(names):
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(str(number).encode())
    actual = sorted(path.relative_to(source).as_posix() for path in source.rglob('*') if path.is_file())
    try:
        state_backup.validate_state([entry(name) for name in actual])
    except ValueError:
        with pytest.raises(ValueError, match='Duplicate|collide'):
            state_backup.snapshot_state(source, target)
    else:
        # A case-insensitive filesystem already collapsed or normalized the
        # attempted aliases. Assert its actual inventory round-trips exactly;
        # the pure manifest tests above exercise both collision spellings.
        result = state_backup.snapshot_state(source, target)
        assert [row['filename'] for row in result] == actual
        assert all((target / name).read_bytes() == (source / name).read_bytes() for name in actual)


def test_equal_length_same_timestamp_mutation_is_detected_by_byte_revalidation(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    first = source / 'a.json'
    first.write_bytes(b'original-voice')
    (source / 'z.json').write_bytes(b'x' * (1024 * 1024))
    before = first.stat()
    monkeypatch.setattr(state_backup, '_stamp', lambda value:
        (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns))
    modified = False
    def concurrent_writer():
        nonlocal modified
        if not modified and (target / 'z.json').exists():
            modified = True
            first.write_bytes(b'changed--voice')
            os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns))
        return False
    with pytest.raises(ValueError, match='changed'):
        state_backup.snapshot_state(source, target, cancelled=concurrent_writer)
    assert modified and first.read_bytes() == b'changed--voice'


def test_second_hash_pass_remains_cancellable(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    first = source / 'a.json'
    first.write_bytes(b'original-voice')
    reads = 0
    original_open = Path.open
    def observe(path, mode='r', *args, **kwargs):
        nonlocal reads
        if path == first and mode == 'rb':
            reads += 1
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observe)
    with pytest.raises(InterruptedError, match='cancelled'):
        state_backup.snapshot_state(source, target, cancelled=lambda: reads >= 2)
    assert reads == 2


def test_scoped_selection_is_revalidated_without_reading_unselected_payloads(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    (source / 'bound.json').write_bytes(b'bound')
    outside = source / 'independent.json'
    outside.write_bytes(b'private independent draft')
    original_open = Path.open
    def guarded(path, *args, **kwargs):
        assert path != outside, 'a scoped state copy read an unselected payload'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    result = state_backup.snapshot_state(source, target, include_paths={'bound.json'})
    assert [item['filename'] for item in result] == ['bound.json']
    assert not (target / outside.name).exists()
