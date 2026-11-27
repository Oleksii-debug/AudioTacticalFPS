"""PR141/145 regressions against the composed canonical state producer."""
from hashlib import sha256
import os
from pathlib import Path

import pytest

from carr_library import state_backup
from carr_library.state_backup import snapshot_state, validate_state


def entry(name):
    return dict(filename=name, size_bytes=1, sha256=sha256(b'x').hexdigest())


@pytest.mark.parametrize('names', [
    ('voice.json', 'VOICE.JSON'), ('A/one.json', 'a/two.json'),
    ('caf\u00e9/one.json', 'cafe\u0301/two.json'),
    ('folder', 'folder/item.json'), ('folder/item.json', 'folder'),
])
def test_portable_manifest_rejects_collisions(names):
    with pytest.raises(ValueError, match='Duplicate|collide'):
        state_backup.validate_state([entry(name) for name in names])


def test_portable_manifest_allows_distinct_siblings():
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
    root = tmp_path / 'source'
    root.mkdir()
    source = root / 'draft?.json'
    if os.name == 'nt':
        # Win32 cannot create this path. The pure manifest must reject the
        # same foreign-source declaration before any restore or publication.
        with pytest.raises(OSError):
            source.write_bytes(b'x')
        with pytest.raises(ValueError, match='Invalid'):
            validate_state([entry('draft?.json')])
        assert not (tmp_path / 'stage').exists()
        return
    source.write_bytes(b'x')
    with pytest.raises(ValueError, match='Invalid'):
        snapshot_state(root, tmp_path / 'stage')
    assert not (tmp_path / 'stage').exists()


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
