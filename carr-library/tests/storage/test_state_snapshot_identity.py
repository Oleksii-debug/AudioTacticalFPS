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


@pytest.mark.parametrize('split_roots', (False, True))
def test_typed_capture_counts_ignored_entries_across_roots(tmp_path, monkeypatch, split_roots):
    from carr_library import backup_sidecars
    from carr_library.ingest.restore import RestoreError

    audio = tmp_path / 'state/audio'
    audio.mkdir(parents=True)
    paths = [audio / '.pending-one', audio / '.pending-two']
    if split_roots:
        copies = tmp_path / 'reading-copies'
        copies.mkdir()
        paths.append(copies / '.reading-copy-three.tmp')
    else:
        paths.append(audio / '.pending-three')
    for path in paths:
        path.write_bytes(b'unchanged')
    limit = 6 if split_roots else 5
    monkeypatch.setattr(backup_sidecars, 'MAX_SCAN_ENTRIES', limit, raising=False)
    assert backup_sidecars._capture(tmp_path)[0] == {}
    monkeypatch.setattr(backup_sidecars, 'MAX_SCAN_ENTRIES', limit - 1)
    with pytest.raises(RestoreError, match='scan exceeds its entry budget'):
        backup_sidecars._capture(tmp_path)
    assert all(path.read_bytes() == b'unchanged' for path in paths)


def test_typed_capture_counts_but_does_not_traverse_excluded_cache(tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    from carr_library.ingest.restore import RestoreError

    cache = tmp_path / 'state/studio/cache'
    cache.mkdir(parents=True)
    (cache / '.pending-one').write_bytes(b'cache')
    monkeypatch.setattr(backup_sidecars, 'MAX_SCAN_ENTRIES', 3, raising=False)
    files, directories = backup_sidecars._capture(tmp_path)
    assert files == {} and set(directories) == {'state', 'state/studio'}
    with pytest.raises(RestoreError, match='scan exceeds its entry budget'):
        backup_sidecars._capture(tmp_path, include_generated=True)


def test_typed_capture_streams_and_stops_before_stat_of_excess_entry(tmp_path, monkeypatch):
    import os
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace
    from carr_library import backup_sidecars
    from carr_library.ingest.restore import RestoreError

    audio = tmp_path / 'state/audio'
    audio.mkdir(parents=True)
    (audio / '.pending-one').write_bytes(b'first')
    scandir = os.scandir

    @contextmanager
    def observed(path):
        if Path(path) != audio:
            with scandir(path) as entries:
                yield entries
            return

        def entries():
            yield SimpleNamespace(name='.pending-one')
            # This name deliberately does not exist. The scan bound must
            # reject it before lstat or any attempt to consume a third name.
            yield SimpleNamespace(name='.pending-excess')
            pytest.fail('scan consumed another entry after exceeding its limit')

        yield entries()

    def forbidden(path):
        pytest.fail('typed capture must not materialize a directory listing')

    monkeypatch.setattr(backup_sidecars, 'MAX_SCAN_ENTRIES', 3, raising=False)
    monkeypatch.setattr(os, 'scandir', observed)
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    with pytest.raises(RestoreError, match='scan exceeds its entry budget'):
        backup_sidecars._capture(tmp_path)


def test_typed_capture_observes_cancel_on_final_scan_close(tmp_path, monkeypatch):
    import os
    from contextlib import contextmanager
    from carr_library import backup_sidecars

    (tmp_path / 'state').mkdir()
    scandir = os.scandir
    stopped = False

    @contextmanager
    def observed(path):
        nonlocal stopped
        with scandir(path) as entries:
            yield entries
        stopped = True

    monkeypatch.setattr(os, 'scandir', observed)
    with pytest.raises(InterruptedError, match='cancelled'):
        backup_sidecars._capture(tmp_path, cancelled=lambda: stopped)


def test_typed_capture_scan_budget_prevents_backup_publication(tmp_path, monkeypatch):
    from carr_library import backup_sidecars
    from carr_library.application import LibraryApp
    from carr_library.ingest.restore import RestoreError
    from carr_library.library_backup import create_backup

    app = LibraryApp(tmp_path / 'source')
    audio = app.root / 'state/audio'
    audio.mkdir(parents=True, exist_ok=True)
    retained = audio / '.pending-synthetic'
    retained.write_bytes(b'unchanged')
    monkeypatch.setattr(backup_sidecars, 'MAX_SCAN_ENTRIES', 1, raising=False)
    destination = tmp_path / 'backup'
    with pytest.raises(RestoreError, match='scan exceeds its entry budget'):
        create_backup(app, destination)
    assert not destination.exists()
    assert retained.read_bytes() == b'unchanged'
