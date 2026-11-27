"""Real large-directory continuation without growing an all-files manifest."""
from base64 import urlsafe_b64encode
import json
import os
from pathlib import Path

import pytest

from carr_library.ingest.batch_scan import scan_paths_page
from carr_library.ingest import batch_scan
from carr_library.ingest.scan_pages import validate_cursor


def write(path, text='Original source.'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def all_pages(paths, **options):
    rows, pages, cursor = [], [], None
    for _ in range(10000):
        page = scan_paths_page(paths, cursor=cursor, **options)
        pages.append(page)
        rows.extend(page.rows)
        cursor = page.next_cursor
        if cursor is None:
            return rows, pages
    raise AssertionError('Scan continuation did not progress.')


def test_actual_5003_files_all_reachable_beyond_old_global_limit(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    for number in range(5003):
        write(root / f'{number:06d}.txt', f'Unique original {number}.')
    # Explicit measured-capacity allowance; the production deadline is unchanged.
    rows, pages = all_pages([root], timeout=300)
    assert len(rows) == 5003
    assert [row['file'] for row in rows] == [f'{i:06d}.txt' for i in range(5003)]
    assert len({row['source'] for row in rows}) == 5003
    assert all(row['state'] == 'queued' for row in rows)
    assert all(page.examined_files <= 5000 and page.scanned_entries <= 20000 for page in pages)
    assert pages[0].next_cursor and pages[-1].complete
    assert (root / '000000.txt').read_text() == 'Unique original 0.'
    assert (root / '005002.txt').read_text() == 'Unique original 5002.'


def test_actual_directory_larger_than_twenty_thousand_entries_is_not_dropped(tmp_path):
    root = tmp_path / 'large'
    root.mkdir()
    for number in range(20001):
        (root / f'{number:06d}.unsupported').touch()
    write(root / 'zz-last.txt', 'Last source remains reachable.')
    rows, pages = all_pages([root])
    assert any(row['file'] == 'zz-last.txt' and row['state'] == 'queued' for row in rows)
    assert sum(page.rejected_entries for page in pages) == 20001
    assert sum(page.examined_files for page in pages) == 1
    assert len(pages) >= 2
    assert all(len(page.rows) <= 102 for page in pages)


def test_nested_and_overlapping_explicit_roots_are_not_repeated(tmp_path):
    root = tmp_path / 'selected'
    for relative in ('a.txt', 'b/a.txt', 'b/c/a.txt', 'd/a.txt'):
        write(root / relative, relative)
    selected = [root, root / 'b', root / 'b/c/a.txt']
    rows, pages = all_pages(selected, file_budget=1, entry_budget=3)
    assert {Path(row['source']).relative_to(root).as_posix() for row in rows} == {
        'a.txt', 'b/a.txt', 'b/c/a.txt', 'd/a.txt'}
    assert len(rows) == len({row['source'] for row in rows}) == 4
    assert all(page.examined_files <= 1 and page.scanned_entries <= 3 for page in pages)


def test_no_recursive_scan_reports_subfolder_without_opening_it(tmp_path):
    root = tmp_path / 'selected'
    write(root / 'a.txt')
    write(root / 'nested/b.txt')
    rows, _ = all_pages([root], recursive=False, entry_budget=1)
    assert [row['file'] for row in rows] == ['a.txt', 'nested']
    assert rows[1]['state'] == 'rejected' and 'recursive' in rows[1]['message']


def test_replaced_directory_refuses_old_position_without_opening_new_files(tmp_path, monkeypatch):
    root = tmp_path / 'selected'
    write(root / 'a.txt', 'One')
    write(root / 'b.txt', 'Two')
    first = scan_paths_page([root], file_budget=1)
    root.rename(tmp_path / 'old')
    write(root / 'a.txt', 'Different one')
    write(root / 'b.txt', 'Different two')
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
                        lambda path: pytest.fail('Replacement root must not be opened.'))
    with pytest.raises(ValueError, match='folder changed'):
        scan_paths_page([root], file_budget=1, cursor=first.next_cursor)


def test_cancelled_page_does_not_consume_saved_continuation(tmp_path, monkeypatch):
    root = tmp_path / 'selected'
    for name in ('a', 'b', 'c'):
        write(root / (name + '.txt'), name)
    first = scan_paths_page([root], file_budget=1)
    original = batch_scan.planned_snapshot
    stopped = False
    def stop_after_read(path):
        nonlocal stopped
        value = original(path)
        stopped = True
        return value
    with monkeypatch.context() as patch:
        patch.setattr(batch_scan, 'planned_snapshot', stop_after_read)
        with pytest.raises(InterruptedError, match='previous saved page'):
            scan_paths_page([root], cursor=first.next_cursor, cancelled=lambda: stopped)
    resumed = scan_paths_page([root], cursor=first.next_cursor)
    assert [row['file'] for row in resumed.rows] == ['b.txt', 'c.txt']


def test_hardlinks_and_reparse_policy_still_fail_closed(tmp_path):
    root = tmp_path / 'selected'
    a = write(root / 'a.txt')
    os.link(a, root / 'b.txt')
    rows, _ = all_pages([root], file_budget=1)
    assert len(rows) == 2 and all(row['state'] == 'rejected' for row in rows)
    assert a.read_text() == 'Original source.'


def test_duplicate_scope_uses_caller_lookup_and_page_local_bytes(tmp_path):
    root = tmp_path / 'selected'
    a = write(root / 'a.txt', 'Existing source')
    write(root / 'b.txt', 'New source')
    write(root / 'c.txt', 'New source')
    from hashlib import sha256
    known = sha256(a.read_bytes()).hexdigest()
    rows, _ = all_pages([root], existing_contains=lambda digest: digest == known)
    assert [row['state'] for row in rows] == ['duplicate', 'queued', 'duplicate']
    assert rows[0]['duplicate_of'] == 'existing:' + known
    assert rows[2]['duplicate_of'] == str(root / 'b.txt')


def test_cursor_pure_validation_never_reads_archived_paths(tmp_path, monkeypatch):
    root = tmp_path / 'selected'
    write(root / 'a.txt', 'One')
    write(root / 'b.txt', 'Two')
    first = scan_paths_page([root], file_budget=1)
    monkeypatch.setattr(Path, 'lstat', lambda *a, **k: pytest.fail('Pure cursor validation touched filesystem.'))
    row = validate_cursor(first.next_cursor, root_count=1)
    assert row['stack'][0]['after'] == 'a.txt'
    row['stack'][0]['relative'] = '../outside'
    forged = urlsafe_b64encode(json.dumps(row).encode()).decode()
    with pytest.raises(ValueError, match='continuation'):
        validate_cursor(forged, root_count=1)


def test_rejected_basename_remains_an_inert_cursor_position(tmp_path):
    root = tmp_path / 'selected'
    write(root / 'a.txt', 'One')
    write(root / 'b.txt', 'Two')
    first = scan_paths_page([root], file_budget=1)
    value = validate_cursor(first.next_cursor)
    # A marker from a rejected POSIX filename is never a path component. The
    # same metadata-only rule is portable on Windows without creating an
    # illegal local filename there.
    value['stack'][0]['after'] = 'a:rejected\\name\n.txt'
    cursor = urlsafe_b64encode(json.dumps(value).encode()).decode()
    resumed = scan_paths_page([root], cursor=cursor)
    assert [row['file'] for row in resumed.rows] == ['b.txt']


def test_continuation_requires_same_explicit_roots_and_recursion(tmp_path):
    root = tmp_path / 'selected'
    write(root / 'a.txt', 'One')
    write(root / 'b.txt', 'Two')
    first = scan_paths_page([root], file_budget=1)
    with pytest.raises(ValueError, match='source selection'):
        scan_paths_page([root], cursor=first.next_cursor, recursive=False)
    with pytest.raises(ValueError, match='source selection'):
        scan_paths_page([tmp_path / 'other'], cursor=first.next_cursor)


@pytest.mark.parametrize('options', [
    {'file_budget': 5001}, {'entry_budget': 20001}, {'file_budget': True},
    {'entry_budget': 0}, {'timeout': float('inf')}, {'cancelled': None},
], ids=['file-bound', 'entry-bound', 'boolean-bound', 'zero-bound', 'infinite-time', 'callback'])
def test_page_guards_remain_explicit_and_before_file_reads(tmp_path, options):
    with pytest.raises(ValueError):
        scan_paths_page([tmp_path], **options)


def test_expired_page_after_real_snapshot_keeps_exact_continuation_retryable(tmp_path, monkeypatch):
    from carr_library.ingest import scan_pages
    root = tmp_path / 'selected'
    for name in ('a', 'b', 'c'):
        write(root / (name + '.txt'), name)
    first = scan_paths_page([root], file_budget=1)
    saved = first.next_cursor
    read = batch_scan.planned_snapshot
    clock, observed = [0.0], []
    def delayed(path):
        value = read(path)
        observed.append(Path(path).name)
        clock[0] = 2.0
        return value
    with monkeypatch.context() as patch:
        patch.setattr(scan_pages, 'monotonic', lambda: clock[0])
        patch.setattr(batch_scan, 'planned_snapshot', delayed)
        with pytest.raises(TimeoutError, match='unchanged continuation'):
            scan_paths_page([root], cursor=saved, timeout=1)
    assert observed == ['b.txt'] and first.next_cursor == saved
    retry = scan_paths_page([root], cursor=saved)
    assert [row['file'] for row in retry.rows] == ['b.txt', 'c.txt']
    assert retry.complete
