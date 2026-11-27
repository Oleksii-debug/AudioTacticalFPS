from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import os
import stat
from zipfile import ZipFile

import pytest

from carr_library.ingest import batch_scan


def write(path, data=b'An original text.\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_sorted_folder_plan_uses_existing_intake_without_writing(tmp_path):
    write(tmp_path / 'z.txt', 'Український текст.\n'.encode())
    write(tmp_path / 'nested' / 'a.pdf', b'%PDF-1.7\n')
    write(tmp_path / 'unsupported.bin', b'Synthetic unsupported source.')
    # Structural DOCX intake fixture; preflight does not extract document text.
    with ZipFile(tmp_path / 'b.docx', 'w') as archive:
        for name in ('[Content_Types].xml', '_rels/.rels', 'word/document.xml'):
            archive.writestr(name, b'synthetic')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows = batch_scan.scan_paths([tmp_path])
    assert [r['file'] for r in rows] == ['b.docx', 'nested/a.pdf', 'unsupported.bin', 'z.txt']
    assert [r['state'] for r in rows] == ['queued', 'queued', 'rejected', 'queued']
    for row in rows:
        assert set(row) == {'source', 'file', 'title', 'state', 'message', 'manifest', 'duplicate_of'}
        assert Path(row['source']).is_absolute()
        assert row['title'] == Path(row['source']).stem
        if row['manifest']:
            data = before[row['source']]
            assert set(row['manifest']) == {'sha256', 'mime', 'size'}
            assert row['manifest']['sha256'] == sha256(data).hexdigest()
            assert row['manifest']['size'] == len(data)
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('policy', [
    {'supported': ()}, {'supported': frozenset()}, {'supported': frozenset({'.WAV'})},
    {'supported': frozenset({'wav'})}, {'file_manifest': False},
    {'max_files': True}, {'max_files': 0}, {'max_files': batch_scan.MAX_FILES + 1},
    {'skip_unsupported': 'yes'}, {'cancelled': None},
])
def test_invalid_scan_policy_is_rejected_before_any_directory_or_file_read(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(batch_scan.os, 'scandir', lambda *a, **k: pytest.fail('Invalid scan opened a folder.'))
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Invalid scan read a source.'))
    with pytest.raises(ValueError):
        batch_scan.scan_paths([tmp_path], **policy)


@pytest.mark.parametrize('manifest', [None, {'sha256': 'x', 'mime': 'audio/wav', 'size': 3},
    {'sha256': 'a' * 64, 'mime': 'audio/wav', 'size': True},
    {'sha256': 'a' * 64, 'mime': 'audio/wav', 'size': 4}])
def test_custom_policy_cannot_publish_an_invalid_manifest(tmp_path, manifest):
    source = write(tmp_path / 'audio.wav', b'123')
    row, = batch_scan.scan_paths([source], supported=frozenset({'.wav'}),
                                 file_manifest=lambda path: manifest)
    assert row['state'] == 'rejected' and row['manifest'] is None
    assert 'invalid source manifest' in row['message']
    assert source.read_bytes() == b'123'


def test_exact_duplicates_are_scoped_and_keep_original_paths(tmp_path):
    first = write(tmp_path / 'a.txt', b'same source')
    second = write(tmp_path / 'b.txt', b'same source')
    third = write(tmp_path / 'c.txt', b'already in this library')
    existing = sha256(third.read_bytes()).hexdigest()
    rows = batch_scan.scan_paths([second, third, first], existing_assets=[existing])
    assert [r['state'] for r in rows] == ['queued', 'duplicate', 'duplicate']
    assert rows[1]['duplicate_of'] == str(first)
    assert rows[2]['duplicate_of'] == 'existing:' + existing
    assert batch_scan.scan_paths([third])[0]['state'] == 'queued'
    assert batch_scan.scan_paths([third, second, first], existing_assets=[existing]) == rows


def test_overlapping_selection_scans_each_path_once(tmp_path):
    path = write(tmp_path / 'a.txt')
    assert len(batch_scan.scan_paths([tmp_path, path, tmp_path])) == 1


def test_nonrecursive_skips_nested_folder_without_opening_file(tmp_path, monkeypatch):
    write(tmp_path / 'a.txt')
    write(tmp_path / 'nested' / 'b.txt')
    real = batch_scan.snapshot
    opened = []

    def capture(root, relative):
        opened.append(relative)
        return real(root, relative)

    monkeypatch.setattr(batch_scan, 'snapshot', capture)
    rows = batch_scan.scan_paths([tmp_path], recursive=False)
    assert opened == ['a.txt']
    assert rows[1]['state'] == 'rejected' and 'recursive' in rows[1]['message']


def test_root_symlink_rejected_before_resolve_or_snapshot(tmp_path, monkeypatch):
    root = tmp_path / 'real'
    write(root / 'a.txt')
    link = tmp_path / 'selected'
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip('Symlinks unavailable on this host.')

    def forbidden(*args, **kwargs):
        raise AssertionError('Rejected root must never be resolved or opened.')

    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(batch_scan, 'snapshot', forbidden)
    row, = batch_scan.scan_paths([link])
    assert row['state'] == 'rejected'
    assert row['source'] == str(link)


@pytest.mark.parametrize('selection', ['leaf', 'ancestor'])
def test_symlink_leaf_or_ancestor_cannot_hide_behind_parent_resolution(tmp_path, monkeypatch, selection):
    root = tmp_path / 'real'
    source = write(root / 'a.txt')
    link = tmp_path / ('linked.txt' if selection == 'leaf' else 'linked')
    try:
        link.symlink_to(source if selection == 'leaf' else root,
                        target_is_directory=selection != 'leaf')
    except OSError:
        pytest.skip('Symlinks unavailable on this host.')
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Linked source was opened.'))
    row, = batch_scan.scan_paths([link if selection == 'leaf' else link / 'a.txt'])
    assert row['state'] == 'rejected'


def test_hardlinks_rejected_without_opening_or_deleting(tmp_path, monkeypatch):
    first = write(tmp_path / 'a.txt')
    second = tmp_path / 'b.txt'
    os.link(first, second)
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Hardlink was opened.'))
    rows = batch_scan.scan_paths([tmp_path])
    assert [r['state'] for r in rows] == ['rejected', 'rejected']
    assert first.read_bytes() == second.read_bytes() == b'An original text.\n'
    assert first.stat().st_nlink == 2


def test_windows_reparse_root_is_rejected_before_listing(tmp_path, monkeypatch):
    selected = tmp_path / 'selected'
    selected.mkdir()
    real = Path.lstat

    def lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if path == selected:
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            fields['st_file_attributes'] = stat.FILE_ATTRIBUTE_REPARSE_POINT
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(batch_scan.os, 'scandir', lambda *a, **k: pytest.fail('Reparse directory was opened.'))
    row, = batch_scan.scan_paths([selected])
    assert row['state'] == 'rejected'


@pytest.mark.parametrize('change', ['replace', 'hardlink'])
def test_source_changed_after_intake_is_rejected(tmp_path, monkeypatch, change):
    source = write(tmp_path / 'a.txt')
    real = batch_scan.snapshot

    def changed(root, relative):
        asset = real(root, relative)
        if change == 'replace':
            replacement = write(tmp_path / 'replacement.txt', b'Changed source bytes.')
            os.replace(replacement, source)
        else:
            os.link(source, tmp_path / 'external.txt')
        return asset

    monkeypatch.setattr(batch_scan, 'snapshot', changed)
    row, = batch_scan.scan_paths([source])
    assert row['state'] == 'rejected' and row['manifest'] is None
    assert 'changed' in row['message'].lower()
    assert source.exists()


@pytest.mark.parametrize('name,data', [('fake.pdf', b'Not PDF'), ('empty.txt', b''),
                                       ('binary.txt', b'\x00'), ('macro.docx', b'MZfake')])
def test_existing_intake_rejections_are_reported(tmp_path, name, data):
    source = write(tmp_path / name, data)
    row, = batch_scan.scan_paths([source])
    assert row['state'] == 'rejected' and row['manifest'] is None
    assert source.read_bytes() == data


def test_cancel_before_scan_opens_nothing(tmp_path, monkeypatch):
    source = write(tmp_path / 'a.txt')
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Cancelled source was opened.'))
    with pytest.raises(InterruptedError):
        batch_scan.scan_paths([source], cancelled=lambda: True)


def test_cancel_after_first_snapshot_never_opens_next_file(tmp_path, monkeypatch):
    write(tmp_path / 'a.txt')
    write(tmp_path / 'b.txt')
    stopped, opened = [], []
    real = batch_scan.snapshot

    def capture(root, relative):
        opened.append(relative)
        asset = real(root, relative)
        stopped.append(True)
        return asset

    monkeypatch.setattr(batch_scan, 'snapshot', capture)
    with pytest.raises(InterruptedError):
        batch_scan.scan_paths([tmp_path], cancelled=lambda: bool(stopped))
    assert opened == ['a.txt']


def test_supported_file_bound_stops_before_next_open(tmp_path, monkeypatch):
    for name in ('a', 'b', 'c', 'd'):
        write(tmp_path / (name + '.txt'), name.encode())
    monkeypatch.setattr(batch_scan, 'MAX_FILES', 2)
    real, opened = batch_scan.snapshot, []

    def capture(root, relative):
        opened.append(relative)
        return real(root, relative)

    monkeypatch.setattr(batch_scan, 'snapshot', capture)
    rows = batch_scan.scan_paths([tmp_path])
    assert opened == ['a.txt', 'b.txt']
    assert [r['state'] for r in rows] == ['queued', 'queued', 'rejected']
    assert 'limit' in rows[-1]['message']


def test_directory_listing_is_bounded_before_processing_any_partial_prefix(tmp_path, monkeypatch):
    for name in ('c', 'a', 'b'):
        write(tmp_path / (name + '.txt'))
    monkeypatch.setattr(batch_scan, 'MAX_SCAN_ENTRIES', 2)
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Oversized listing was partly imported.'))
    row, = batch_scan.scan_paths([tmp_path])
    assert row['state'] == 'rejected' and 'entry limit' in row['message']


def test_rejection_rows_are_bounded_and_counted(tmp_path, monkeypatch):
    for n in range(8):
        write(tmp_path / f'{n}.unsupported')
    monkeypatch.setattr(batch_scan, 'MAX_REJECTIONS', 2)
    rows = batch_scan.scan_paths([tmp_path])
    assert len(rows) == 3
    assert '6 additional' in rows[-1]['message'] and '8 total' in rows[-1]['message']
    assert all(r['state'] == 'rejected' and r['manifest'] is None for r in rows)


def test_scan_limit_remains_visible_after_rejection_details_are_capped(tmp_path, monkeypatch):
    first = write(tmp_path / 'a.unsupported')
    second = write(tmp_path / 'b.unsupported')
    folder = tmp_path / 'z'
    for name in ('a', 'b', 'c'):
        write(folder / (name + '.txt'))
    monkeypatch.setattr(batch_scan, 'MAX_REJECTIONS', 1)
    monkeypatch.setattr(batch_scan, 'MAX_SCAN_ENTRIES', 2)
    rows = batch_scan.scan_paths([folder, second, first])
    assert len(rows) == 3
    assert 'entry limit' in rows[1]['message']
    assert 'remaining selections were not scanned' in rows[1]['message']
    assert '1 additional' in rows[2]['message'] and '3 total' in rows[2]['message']


def test_empty_selection_and_folder(tmp_path):
    assert batch_scan.scan_paths([]) == ()
    assert batch_scan.scan_paths([tmp_path]) == ()


def test_planned_snapshot_preserves_exact_immutable_bytes(tmp_path):
    source = write(tmp_path / 'a.txt', 'Original український text.\n'.encode())
    original = source.read_bytes()
    asset = batch_scan.planned_snapshot(source)
    source.write_bytes(b'Later source change.')
    assert asset.data == original
    assert asset.manifest() == {'sha256': sha256(original).hexdigest(),
                                'mime': 'text/plain', 'size': len(original)}


def test_planned_snapshot_rejects_new_hardlink_before_read(tmp_path, monkeypatch):
    source = write(tmp_path / 'a.txt')
    assert batch_scan.scan_paths([source])[0]['state'] == 'queued'
    os.link(source, tmp_path / 'linked.txt')
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Hard-linked source was read.'))
    with pytest.raises(ValueError, match='Hard-linked'):
        batch_scan.planned_snapshot(source)


def test_planned_snapshot_rejects_parent_link_replacement_before_read(tmp_path, monkeypatch):
    parent = tmp_path / 'selected'
    source = write(parent / 'a.txt')
    assert batch_scan.scan_paths([source])[0]['state'] == 'queued'
    moved = tmp_path / 'moved'
    parent.rename(moved)
    try:
        parent.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip('Symlinks unavailable on this host.')
    monkeypatch.setattr(batch_scan, 'snapshot', lambda *a, **k: pytest.fail('Source behind linked parent was read.'))
    with pytest.raises(ValueError, match='source folder'):
        batch_scan.planned_snapshot(source)


@pytest.mark.parametrize('assets', [('not a digest',), ('A' * 64,), (42,)])
def test_existing_assets_must_be_explicit_sha256_digests(assets):
    with pytest.raises(ValueError, match='SHA256'):
        batch_scan.scan_paths([], existing_assets=assets)


@pytest.mark.parametrize('recursive,max_depth', [(False,64),(True,1),(True,2),(True,64)])
def test_explicit_nested_root(tmp_path, monkeypatch, recursive, max_depth):
    nested = tmp_path / 'nested'
    deeper = nested / 'deeper'
    deeper.mkdir(parents=True)
    for path in (tmp_path / 'a.txt', nested / 'b.txt', deeper / 'c.txt'):
        path.write_text(path.name, encoding='utf-8')
    opened = []
    def snapshot(root, name):
        path = root / name
        opened.append(path)
        raw = path.read_bytes()
        return SimpleNamespace(manifest=lambda: {'sha256': sha256(raw).hexdigest(), 'mime': 'text/plain', 'size': len(raw)})
    monkeypatch.setattr(batch_scan, 'snapshot', snapshot)
    monkeypatch.setattr(batch_scan, 'MAX_DEPTH', max_depth)
    rows = batch_scan.scan_paths([nested, tmp_path], recursive=recursive)
    expected = [tmp_path / 'a.txt', nested / 'b.txt']
    if recursive and max_depth > 1:
        expected.append(deeper / 'c.txt')
        assert [row['state'] for row in rows] == ['queued', 'queued', 'queued']
    else:
        assert [row['state'] for row in rows] == ['queued', 'queued', 'rejected']
        assert rows[-1]['source'] == str(deeper)
    assert opened == expected
    assert len({row['source'] for row in rows}) == len(rows)
