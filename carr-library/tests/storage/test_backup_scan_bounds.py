"""Streaming inventories retain the existing typed backup and namespace limits."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import backup_compat, backup_sidecars, state_backup
from carr_library.application import LibraryApp
from carr_library.ingest.restore import RestoreError
from carr_library.library_backup import create_backup, restore_backup


def _capture(kind, root, target, *, cancelled=lambda: False):
    if kind == 'native':
        return state_backup.snapshot_state(root, target, cancelled=cancelled)
    return backup_sidecars._walk(root, exclude_temporary=True, cancelled=cancelled)


@pytest.mark.parametrize('split_roots', [False, True])
def test_native_capture_counts_ignored_names_across_directories(tmp_path, monkeypatch, split_roots):
    root = tmp_path / 'state'
    audio = root / 'audio'
    audio.mkdir(parents=True)
    paths = [audio / '.pending-one', audio / '.pending-two']
    if split_roots:
        speech = root / 'speech'
        speech.mkdir()
        paths.append(speech / '.pending-three')
    else:
        paths.append(audio / '.pending-three')
    for path in paths:
        path.write_bytes(b'unchanged')
    limit = 6 if split_roots else 5
    monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', limit, raising=False)
    assert state_backup.snapshot_state(root, tmp_path / 'stage') == []
    monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', limit - 1)
    with pytest.raises(ValueError, match='scan exceeds its entry budget'):
        state_backup.snapshot_state(root, tmp_path / 'other-stage')
    assert all(path.read_bytes() == b'unchanged' for path in paths)
    assert not (tmp_path / 'other-stage').exists()


def test_native_capture_counts_cache_root_without_descending_when_excluded(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    cache = root / 'studio/cache'
    cache.mkdir(parents=True)
    payload = cache / 'chunk.wav'
    payload.write_bytes(b'generated cache remains untouched')
    scanned = []
    original = os.scandir

    def observed(path):
        if not isinstance(path, int):
            scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, 'scandir', observed)
    monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', 3, raising=False)
    assert state_backup.snapshot_state(root, tmp_path / 'without-cache') == []
    assert cache not in scanned
    with pytest.raises(ValueError, match='scan exceeds its entry budget'):
        state_backup.snapshot_state(root, tmp_path / 'with-cache', include_generated=True)
    assert cache in scanned
    assert payload.read_bytes() == b'generated cache remains untouched'


@pytest.mark.parametrize('kind', ['native', 'legacy'])
def test_capture_stops_before_stat_of_excess_name_and_closes_iterator(tmp_path, monkeypatch, kind):
    root = tmp_path / 'state'
    root.mkdir()
    count = 1 if kind == 'native' else 3
    names = ['.pending-' + str(number) for number in range(count)]
    for name in names:
        (root / name).write_bytes(b'owned temporary bytes')
    if kind == 'native':
        monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', 2, raising=False)
    else:
        monkeypatch.setattr(backup_sidecars, 'MAX_FILES', 1)
    consumed, closed = [], []
    original = os.scandir

    @contextmanager
    def observed(path):
        if isinstance(path, int) or Path(path) != root:
            with original(path) as entries:
                yield entries
            return
        def entries():
            for name in [*names, '.pending-does-not-exist']:
                consumed.append(name)
                yield SimpleNamespace(name=name)
            pytest.fail('Capture consumed another name after its entry budget.')
        try:
            yield entries()
        finally:
            closed.append(True)

    monkeypatch.setattr(os, 'scandir', observed)
    monkeypatch.setattr(Path, 'iterdir', lambda *a, **k: pytest.fail('Eager directory listing.'))
    expected = 'scan exceeds its entry budget' if kind == 'native' else 'tree resource limit'
    error_type = ValueError if kind == 'native' else RestoreError
    with pytest.raises(error_type, match=expected):
        _capture(kind, root, tmp_path / 'stage')
    assert consumed == [*names, '.pending-does-not-exist'] and closed == [True]
    assert all((root / name).read_bytes() == b'owned temporary bytes' for name in names)
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('kind', ['native', 'legacy'])
def test_capture_closes_iterator_when_cancelled_before_next_stat(tmp_path, monkeypatch, kind):
    root = tmp_path / 'state'
    root.mkdir()
    temporary = root / '.pending-one'
    temporary.write_bytes(b'unchanged')
    stopped, closed, consumed = False, [], []
    original = os.scandir

    @contextmanager
    def observed(path):
        if isinstance(path, int) or Path(path) != root:
            with original(path) as entries:
                yield entries
            return
        def entries():
            nonlocal stopped
            consumed.append('.pending-one')
            yield SimpleNamespace(name='.pending-one')
            stopped = True
            consumed.append('.pending-does-not-exist')
            yield SimpleNamespace(name='.pending-does-not-exist')
            pytest.fail('Capture continued after cancellation.')
        try:
            yield entries()
        finally:
            closed.append(True)

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        _capture(kind, root, tmp_path / 'stage', cancelled=lambda: stopped)
    assert consumed == ['.pending-one', '.pending-does-not-exist'] and closed == [True]
    assert temporary.read_bytes() == b'unchanged'
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('kind', ['native', 'legacy'])
def test_capture_checks_cancellation_after_final_iterator_close(tmp_path, monkeypatch, kind):
    root = tmp_path / 'state'
    root.mkdir()
    stopped = False
    original = os.scandir

    @contextmanager
    def observed(path):
        nonlocal stopped
        with original(path) as entries:
            yield entries
        stopped = True

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        _capture(kind, root, tmp_path / 'stage', cancelled=lambda: stopped)
    assert stopped and not (tmp_path / 'stage').exists()


def test_native_scan_budget_blocks_full_backup_publication(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Bounded state snapshot')
    directory = app.root / 'state/audio'
    directory.mkdir(parents=True)
    temporary = directory / '.pending-synthetic'
    temporary.write_bytes(b'unchanged')
    monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', 1, raising=False)
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='scan exceeds its entry budget'):
        create_backup(app, destination)
    assert not destination.exists()
    assert temporary.read_bytes() == b'unchanged'


def test_legacy_excluded_cache_is_not_enumerated_and_project_still_roundtrips(tmp_path, monkeypatch):
    from carr_library.audio.projects import ProjectStore, parse_script

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Legacy project')
    store = ProjectStore(app.root)
    saved = store.create(parse_script('Narrator: Retained project text.', ['local-voice'],
                                     project_id='reviewed-project'))
    cache = store.path(saved.id) / 'cache'
    retained = cache / ('a' * 64 + '.wav')
    retained.write_bytes(b'Optional generated cache is not source authority.')
    original = os.scandir

    def forbidden_cache(path):
        if not isinstance(path, int) and Path(path) == cache:
            raise PermissionError('Excluded generated cache was enumerated.')
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'scandir', forbidden_cache)
        checksum = create_backup(app, tmp_path / 'backup', include_generated=False)
    manifest = json.loads((tmp_path / 'backup/manifest.json').read_text(encoding='utf-8'))
    assert manifest['version'] == 5
    assert all('/cache/' not in entry['path'] for entry in manifest['sidecars']['files'])
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert ProjectStore(restored).load(saved.id) == saved
    assert retained.read_bytes() == b'Optional generated cache is not source authority.'


def test_legacy_retained_file_limit_still_applies(tmp_path, monkeypatch):
    root = tmp_path / 'studio'
    root.mkdir()
    for name in ('first.json', 'second.json'):
        (root / name).write_bytes(b'{}')
    monkeypatch.setattr(backup_sidecars, 'MAX_FILES', 1)
    with pytest.raises(RestoreError, match='file count limit'):
        backup_sidecars._walk(root)


def test_legacy_directory_depth_limit_still_applies(tmp_path):
    root = tmp_path / 'studio'
    root.mkdir()
    root.joinpath(*('level' + str(number) for number in range(8))).mkdir(parents=True)
    with pytest.raises(RestoreError, match='tree resource limit'):
        backup_sidecars._walk(root)


def _flat_namespace(root, *, state=False):
    root.mkdir()
    (root / 'library.sqlite').write_bytes(b'synthetic database namespace marker')
    (root / 'manifest.json').write_bytes(b'{}')
    (root / 'originals').mkdir()
    if state:
        (root / 'state').mkdir()
    return backup_compat.Inventory((), frozenset({'state'} if state else ()))


def test_flat_namespace_rejects_first_extra_before_stat_and_closes_iterator(tmp_path, monkeypatch):
    root = tmp_path / 'flat'
    inventory = _flat_namespace(root)
    consumed, closed = [], []
    original = os.scandir

    @contextmanager
    def observed(path):
        if isinstance(path, int) or Path(path) != root:
            with original(path) as entries:
                yield entries
            return
        def entries():
            consumed.append('not-declared-and-not-present')
            yield SimpleNamespace(name='not-declared-and-not-present')
            pytest.fail('Namespace consumed another member after detecting an extra name.')
        try:
            yield entries()
        finally:
            closed.append(True)

    monkeypatch.setattr(os, 'scandir', observed)
    monkeypatch.setattr(Path, 'iterdir', lambda *a, **k: pytest.fail('Eager directory listing.'))
    with pytest.raises(RestoreError, match='unexpected sidecar entry'):
        backup_compat.check_namespace(root, inventory)
    assert consumed == ['not-declared-and-not-present'] and closed == [True]


def test_flat_namespace_checks_cancellation_after_last_iterator_close(tmp_path, monkeypatch):
    root = tmp_path / 'flat'
    inventory = _flat_namespace(root)
    stopped = False
    original = os.scandir

    @contextmanager
    def observed(path):
        nonlocal stopped
        with original(path) as entries:
            yield entries
        stopped = True

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_compat.check_namespace(root, inventory, cancelled=lambda: stopped)
    assert stopped


def test_flat_namespace_preserves_explicit_empty_state_root(tmp_path):
    root = tmp_path / 'flat'
    inventory = _flat_namespace(root, state=True)
    backup_compat.check_namespace(root, inventory)
    extra = root / 'state/unlisted.json'
    extra.write_bytes(b'unchanged')
    with pytest.raises(RestoreError, match='unexpected sidecar entry'):
        backup_compat.check_namespace(root, inventory)
    assert extra.read_bytes() == b'unchanged'


def _state_entry(name, raw):
    return {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def test_native_restore_rejects_first_unlisted_file_before_stat_and_closes(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir()
    (root / 'settings.json').write_bytes(b'{}')
    entries = [_state_entry('settings.json', b'{}')]
    consumed, closed = [], []
    original = os.scandir

    @contextmanager
    def observed(path):
        if isinstance(path, int) or Path(path) != root:
            with original(path) as children:
                yield children
            return
        def children():
            consumed.append('unlisted-does-not-exist')
            yield SimpleNamespace(name='unlisted-does-not-exist', is_dir=lambda **kw: False)
            pytest.fail('Native restore consumed another unlisted name.')
        try:
            yield children()
        finally:
            closed.append(True)

    monkeypatch.setattr(os, 'scandir', observed)
    monkeypatch.setattr(Path, 'iterdir', lambda *a, **k: pytest.fail('Eager directory listing.'))
    with pytest.raises(ValueError, match='missing or unlisted'):
        state_backup.restore_state(root, tmp_path / 'restored', entries)
    assert consumed == ['unlisted-does-not-exist'] and closed == [True]
    assert not (tmp_path / 'restored').exists()
    assert (root / 'settings.json').read_bytes() == b'{}'


def test_native_restore_cancellation_closes_before_next_file_stat(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir()
    (root / 'settings.json').write_bytes(b'{}')
    entries = [_state_entry('settings.json', b'{}')]
    stopped, closed, consumed = False, [], []
    original = os.scandir

    @contextmanager
    def observed(path):
        if isinstance(path, int) or Path(path) != root:
            with original(path) as children:
                yield children
            return
        def children():
            nonlocal stopped
            consumed.append('settings.json')
            yield SimpleNamespace(name='settings.json', is_dir=lambda **kw: False)
            stopped = True
            consumed.append('unlisted-does-not-exist')
            yield SimpleNamespace(name='unlisted-does-not-exist', is_dir=lambda **kw: False)
            pytest.fail('Native restore continued after cancellation.')
        try:
            yield children()
        finally:
            closed.append(True)

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        state_backup.restore_state(root, tmp_path / 'restored', entries, cancelled=lambda: stopped)
    assert consumed == ['settings.json', 'unlisted-does-not-exist'] and closed == [True]
    assert not (tmp_path / 'restored').exists()
    assert (root / 'settings.json').read_bytes() == b'{}'


def test_native_restore_checks_cancel_after_last_iterator_close(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir()
    stopped = False
    original = os.scandir

    @contextmanager
    def observed(path):
        nonlocal stopped
        with original(path) as children:
            yield children
        stopped = True

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        state_backup.restore_state(root, tmp_path / 'restored', [], cancelled=lambda: stopped)
    assert stopped and not (tmp_path / 'restored').exists()


def test_native_restore_preserves_declared_file_capacity_and_bounds_extra_empty_dirs(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    root.mkdir()
    entries = []
    for name, raw in (('one/draft.json', b'first'), ('two/draft.json', b'second')):
        path = root / name
        path.parent.mkdir()
        path.write_bytes(raw)
        entries.append(_state_entry(name, raw))
    (root / 'empty').mkdir()
    monkeypatch.setattr(state_backup, 'MAX_STATE_FILES', 2)
    monkeypatch.setattr(state_backup, 'MAX_STATE_SCAN_ENTRIES', 1, raising=False)
    # Both declared files and their parents fit independently of the additional
    # empty-directory allowance; released manifests do not record empty dirs.
    state_backup.restore_state(root, tmp_path / 'restored', entries)
    for entry in entries:
        assert (tmp_path / 'restored' / entry['filename']).read_bytes() == (root / entry['filename']).read_bytes()
    assert not (tmp_path / 'restored/empty').exists()
    (root / 'another-empty').mkdir()
    with pytest.raises(ValueError, match='restore scan exceeds its entry budget'):
        state_backup.restore_state(root, tmp_path / 'rejected', entries)
    assert not (tmp_path / 'rejected').exists()
    assert (root / 'one/draft.json').read_bytes() == b'first'
    assert (root / 'two/draft.json').read_bytes() == b'second'
