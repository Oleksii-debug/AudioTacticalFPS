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

    if os.name == 'nt':
        pytest.skip('POSIX-only filename fixture')
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'draft?.json').write_bytes(b'x')
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


@pytest.mark.parametrize('operation', ('snapshot', 'restore'))
def test_state_inventory_rejects_unreadable_subtree(tmp_path, monkeypatch, operation):
    import os
    from pathlib import Path
    from carr_library.state_backup import restore_state

    root = tmp_path / 'source'
    blocked = root / 'drafts'
    blocked.mkdir(parents=True)
    (blocked / 'unsaved.json').write_bytes(b'x')
    (root / 'settings.json').write_bytes(b'x')
    target = tmp_path / 'target'
    scandir = os.scandir

    def denied(path):
        if Path(path) == blocked:
            raise PermissionError('synthetic unreadable state subtree')
        return scandir(path)

    # Inject at actual enumeration, not at the manifest validator. The same
    # fixture works when CI runs with administrative filesystem permissions.
    monkeypatch.setattr(os, 'scandir', denied)
    with pytest.raises(PermissionError, match='unreadable state subtree'):
        if operation == 'snapshot':
            snapshot_state(root, target)
        else:
            # A malicious/incomplete manifest omits the unreadable subtree.
            restore_state(root, target, [entry('settings.json')])
    assert not target.exists()
    assert (blocked / 'unsaved.json').read_bytes() == b'x'


def test_state_restore_rejects_unlisted_file_before_scanning_later_subtree(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from carr_library.state_backup import restore_state

    root = tmp_path / 'source'
    later = root / 'later'
    later.mkdir(parents=True)
    (root / 'unlisted.json').write_bytes(b'x')
    (later / 'settings.json').write_bytes(b'x')
    scandir = os.scandir

    def observe(path):
        if Path(path) == later:
            raise AssertionError('Unexpected scan after unlisted file was found')
        return scandir(path)

    monkeypatch.setattr(os, 'scandir', observe)
    with pytest.raises(ValueError, match='missing or unlisted'):
        restore_state(root, tmp_path / 'target', [entry('later/settings.json')])
    assert not (tmp_path / 'target').exists()


@pytest.mark.parametrize('failure', (PermissionError, NotADirectoryError))
def test_state_root_lookup_error_is_not_absence(tmp_path, monkeypatch, failure):
    from pathlib import Path

    root = tmp_path / 'source'
    root.mkdir()
    (root / 'draft.json').write_bytes(b'x')
    target = tmp_path / 'stage'
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == root:
            raise failure('state lookup failed')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(failure, match='state lookup failed'):
        snapshot_state(root, target)
    assert not target.exists()


def test_state_root_missing_is_optional_only_when_requested(tmp_path):
    root, target = tmp_path / 'missing', tmp_path / 'stage'
    assert snapshot_state(root, target) == []
    with pytest.raises(FileNotFoundError):
        snapshot_state(root, target, missing_ok=False)
    assert not target.exists()
