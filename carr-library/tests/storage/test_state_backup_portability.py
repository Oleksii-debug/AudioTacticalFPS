"""Portable state manifests must be valid before publication, not only restore."""
from hashlib import sha256
import errno
import os
from pathlib import Path

import pytest

from carr_library.state_backup import restore_state, snapshot_state, validate_state


def entry(name):
    return dict(filename=name, size_bytes=1, sha256=sha256(b'x').hexdigest())


def _refused_restore_without_destination(stage, destination, entries, message, monkeypatch):
    before = {path.relative_to(stage).as_posix(): path.read_bytes()
              for path in stage.rglob('*') if path.is_file()}
    mkdir, attempted = Path.mkdir, []

    def observe(path, *args, **kwargs):
        if path == destination or destination in path.parents:
            attempted.append(path)
        return mkdir(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'mkdir', observe)
        with pytest.raises(ValueError, match=message):
            restore_state(stage, destination, entries)
    assert attempted == []
    assert not destination.exists()
    assert {path.relative_to(stage).as_posix(): path.read_bytes()
            for path in stage.rglob('*') if path.is_file()} == before


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
def test_snapshot_refuses_unrestorable_portable_names(tmp_path, names, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'stage'
    source.mkdir()
    target.mkdir()
    created, aliases = [], []
    for name in names:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with file.open('xb') as stream:
                stream.write(b'x')
        except FileExistsError:
            # Observe the native alias rather than overwrite its first file.
            assert os.name == 'nt'
            same = [previous for previous in created if previous.samefile(file)]
            assert len(same) == 1
            aliases.append((same[0], file))
        else:
            created.append(file)
    actual = {path.relative_to(source).as_posix(): path.read_bytes()
              for path in source.rglob('*') if path.is_file()}
    if set(names) <= set(actual):
        # Retain the original snapshot refusal when both real spellings exist.
        assert aliases == []
        with pytest.raises(ValueError, match='Duplicate|collide'):
            snapshot_state(source, target)
    else:
        # Windows may prevent the requested pair before the product sees it.
        # Bind that fact to the actual file/directory identity, then snapshot
        # the canonical native inventory and exercise the real restore boundary.
        assert os.name == 'nt'
        first = source / names[0].split('/')[0]
        second = source / names[1].split('/')[0]
        assert first.samefile(second)
        assert (first.stat().st_dev, first.stat().st_ino) == (
            second.stat().st_dev, second.stat().st_ino)
        canonical = {names[0].split('/')[0] +
                     ('/' + name.split('/', 1)[1] if '/' in name else '')
                     for name in names}
        assert set(actual) == canonical
        assert len(aliases) == (0 if '/' in names[0] else 1)
        entries = snapshot_state(source, target)
        assert entries == [entry(name) for name in sorted(actual)]
        assert {path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob('*') if path.is_file()} == actual
        requested = []
        for name in names:
            raw = (source / name).read_bytes()
            requested.append(dict(filename=name, size_bytes=len(raw),
                                  sha256=sha256(raw).hexdigest()))
        _refused_restore_without_destination(
            target, tmp_path / 'restored', requested, 'Duplicate|collide', monkeypatch)
        assert {path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob('*') if path.is_file()} == actual
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


def test_snapshot_rejects_nonportable_real_source(tmp_path, monkeypatch):
    root, stage = tmp_path / 'source', tmp_path / 'stage'
    root.mkdir()
    invalid = root / 'draft?.json'
    try:
        with invalid.open('xb') as stream:
            stream.write(b'x')
    except OSError as error:
        # Use the actual Windows invalid-name refusal, never a platform skip.
        assert os.name == 'nt'
        assert error.errno == errno.EINVAL or getattr(error, 'winerror', None) == 123
        assert list(root.iterdir()) == []
        ordinary = root / 'draft.json'
        ordinary.write_bytes(b'x')
        assert snapshot_state(root, stage) == [entry(ordinary.name)]
        assert (stage / ordinary.name).read_bytes() == b'x'
        raw = (stage / ordinary.name).read_bytes()
        requested = [dict(filename=invalid.name, size_bytes=len(raw),
                          sha256=sha256(raw).hexdigest())]
        _refused_restore_without_destination(
            stage, tmp_path / 'restored', requested, 'Invalid', monkeypatch)
        assert ordinary.read_bytes() == b'x'
        assert [path.name for path in root.iterdir()] == [ordinary.name]
    else:
        # A POSIX filesystem supplies the original real invalid portable name.
        with pytest.raises(ValueError, match='Invalid'):
            snapshot_state(root, stage)
        assert not stage.exists()
        assert invalid.read_bytes() == b'x'

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
