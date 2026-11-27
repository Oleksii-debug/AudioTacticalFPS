"""Real filesystem continuation; no native device or accessibility claim."""
from copy import deepcopy
from pathlib import Path
import os

import pytest

from carr_library.ingest import batch_scan as scan


def text(path, value=b'Original continued source.'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def pages(paths, **options):
    cursor = None
    for _ in range(100):
        page = scan.scan_paths_page(paths, cursor=cursor, **options)
        yield page
        if page.cursor is None:
            return
        cursor = page.cursor
    pytest.fail('A bounded finite source set did not finish scanning.')


def test_actual_5001_sources_continue_without_losing_or_repeating_a_path(tmp_path):
    folder = tmp_path / 'selected'
    folder.mkdir()
    expected = set()
    for index in range(5001):
        expected.add(str(text(folder / f'{index:05}.txt')))
    result = tuple(pages([folder], timeout=600))
    assert len(result) >= 2 and result[0].cursor is not None
    rows = [row for page in result for row in page.rows]
    assert len(rows) == 5001
    assert {row['source'] for row in rows} == expected
    assert all(row['manifest']['size'] == len(b'Original continued source.') for row in rows)
    assert all(row['state'] in ('queued', 'duplicate') for row in rows)
    assert all(sum(row['state'] in ('queued', 'duplicate') for row in page.rows) <= 5000
               for page in result)


def test_actual_20001_directory_entries_continue_in_bounded_pages(tmp_path, monkeypatch):
    folder = tmp_path / 'selected'
    folder.mkdir()
    for index in range(20001):
        (folder / f'{index:05}.unsupported').touch()
    supported = text(folder / 'retained.txt')
    calls = []
    real = scan.planned_snapshot

    def capture(path):
        calls.append(Path(path))
        return real(path)

    monkeypatch.setattr(scan, 'planned_snapshot', capture)
    result = tuple(pages([folder], timeout=600))
    assert len(result) >= 2 and result[0].cursor is not None
    assert result[0].cursor['stack'][0]['count'] <= 20000
    assert result[-1].cursor is None
    assert calls == [supported]
    assert sum(row['state'] == 'queued' for page in result for row in page.rows) == 1
    assert all(len(page.rows) <= scan.MAX_REJECTIONS + 2 for page in result)


@pytest.mark.parametrize('recursive', [False, True])
def test_nested_explicit_roots_resume_without_duplicate_path_admission(tmp_path, recursive):
    folder = tmp_path / 'selected'
    a = text(folder / 'a.txt', b'Root')
    nested = folder / 'nested'
    b = text(nested / 'b.txt', b'Nested')
    c = text(nested / 'deeper' / 'c.txt', b'Deeper')
    rows = [row for page in pages([folder, nested, a], recursive=recursive,
                                  max_files=1, max_entries=2)
            for row in page.rows if row['manifest']]
    expected = {str(a), str(b), str(c)} if recursive else {str(a), str(b)}
    assert {row['source'] for row in rows} == expected
    assert len(rows) == len(expected)


def test_continuation_does_not_reopen_previously_admitted_source_bytes(tmp_path, monkeypatch):
    folder = tmp_path / 'selected'
    for index in range(3):
        text(folder / f'{index}.txt', str(index).encode())
    first = scan.scan_paths_page([folder], max_files=1)
    first_source = first.rows[0]['source']
    real = scan.planned_snapshot
    opened = []

    def capture(path):
        assert str(path) != first_source
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(scan, 'planned_snapshot', capture)
    following = scan.scan_paths_page([folder], cursor=first.cursor)
    assert len(opened) == 2 and following.cursor is None


@pytest.mark.parametrize('damage', ['different-roots', 'escape', 'prefix', 'directory-changed'])
def test_stale_or_foreign_cursor_fails_before_another_source_read(tmp_path, monkeypatch, damage):
    folder = tmp_path / 'selected'
    for index in range(3):
        text(folder / f'{index}.txt')
    first = scan.scan_paths_page([folder], max_files=1)
    cursor, roots = deepcopy(first.cursor), [folder]
    if damage == 'different-roots':
        roots = [tmp_path]
    elif damage == 'escape':
        cursor['stack'][0]['path'] = str(tmp_path)
    elif damage == 'prefix':
        cursor['stack'][0]['prefix_sha256'] = '0' * 64
    else:
        text(folder / 'added.txt')
        info = folder.stat()
        os.utime(folder, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    monkeypatch.setattr(scan, 'planned_snapshot',
                        lambda *_: pytest.fail('An invalid cursor reached another source.'))
    with pytest.raises(ValueError, match='scan|cursor|directory|root'):
        scan.scan_paths_page(roots, cursor=cursor)


def test_cancel_before_continuation_does_not_open_a_directory(tmp_path, monkeypatch):
    folder = tmp_path / 'selected'
    text(folder / 'a.txt')
    text(folder / 'b.txt')
    first = scan.scan_paths_page([folder], max_files=1)
    monkeypatch.setattr(scan.os, 'scandir',
                        lambda *_: pytest.fail('A pre-cancelled scan opened a directory.'))
    with pytest.raises(InterruptedError):
        scan.scan_paths_page([folder], cursor=first.cursor, cancelled=lambda: True)


def test_cancellation_inside_prefix_replay_keeps_the_saved_cursor_unchanged(tmp_path, monkeypatch):
    folder = tmp_path / 'selected'
    for index in range(8):
        text(folder / f'{index}.txt')
    first = scan.scan_paths_page([folder], max_files=5)
    saved = deepcopy(first.cursor)
    count = 0

    def cancelled():
        nonlocal count
        count += 1
        return count >= 5

    monkeypatch.setattr(scan, 'planned_snapshot',
                        lambda *_: pytest.fail('Cancellation did not stop before a new source.'))
    with pytest.raises(InterruptedError):
        scan.scan_paths_page([folder], cursor=first.cursor, cancelled=cancelled)
    assert first.cursor == saved


def test_prefix_replay_has_a_real_time_budget(tmp_path, monkeypatch):
    folder = tmp_path / 'selected'
    for index in range(3):
        text(folder / f'{index}.txt')
    first = scan.scan_paths_page([folder], max_files=1)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(scan.time, 'monotonic', lambda: next(ticks, 2.0))
    with pytest.raises(TimeoutError, match='Reopening'):
        scan.scan_paths_page([folder], cursor=first.cursor, timeout=1)


def test_reparse_source_is_rejected_without_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    folder = tmp_path / 'selected'
    source = text(folder / 'a.txt')
    actual = Path.lstat

    def lstat(path, *args, **kwargs):
        value = actual(path, *args, **kwargs)
        if path == source:
            return SimpleNamespace(**{key: getattr(value, key) for key in (
                'st_mode', 'st_dev', 'st_ino', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')},
                st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(scan, 'planned_snapshot',
                        lambda *_: pytest.fail('A reparse source reached intake.'))
    page = scan.scan_paths_page([folder])
    assert page.rows[0]['state'] == 'rejected' and page.cursor is None


def test_existing_catalog_and_within_page_duplicate_binding_is_preserved(tmp_path):
    from hashlib import sha256
    folder = tmp_path / 'selected'
    text(folder / 'a.txt', b'Existing')
    text(folder / 'b.txt', b'New')
    text(folder / 'c.txt', b'New')
    page = scan.scan_paths_page([folder], existing_assets=(sha256(b'Existing').hexdigest(),))
    existing = next(row for row in page.rows if row['file'] == 'a.txt')
    assert existing['duplicate_of'] == 'existing:' + sha256(b'Existing').hexdigest()
    new = [row for row in page.rows if row['file'] != 'a.txt']
    assert sorted(row['state'] for row in new) == ['duplicate', 'queued']
    duplicate = next(row for row in new if row['state'] == 'duplicate')
    original = next(row for row in new if row['state'] == 'queued')
    assert duplicate['duplicate_of'] == original['source']
