"""File-level export protection, including ordinary confirmed replacement."""
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from carr_library.research import export_file
from carr_library.research.export_file import write_export


@pytest.fixture
def private(tmp_path):
    root = tmp_path / 'private library'
    root.mkdir()
    (root / 'originals').mkdir()
    (root / 'library.sqlite').write_bytes(b'private database')
    (root / 'originals' / 'source.txt').write_bytes(b'exact original')
    return root


def _symlink(source, link, *, directory=False):
    try:
        link.symlink_to(source, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'This environment cannot create symlinks: {exc}')


def test_export_preserves_exact_unicode_and_mixed_line_endings(tmp_path, private):
    destination = tmp_path / 'Дослідження with spaces.md'
    content = 'Цитата: Україна.\r\nNote: café.\nOriginal\rEnding.'

    write_export(destination, content, private_root=private)

    assert destination.read_bytes() == content.encode('utf-8')
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_confirmed_overwrite_replaces_ordinary_file(tmp_path, private):
    destination = tmp_path / 'research.md'
    destination.write_bytes(b'previous export')

    write_export(destination, 'New export\n', private_root=private)

    assert destination.read_bytes() == b'New export\n'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existing', [False, True])
def test_cancel_after_temporary_write_preserves_destination(tmp_path, private, monkeypatch, existing):
    destination = tmp_path / 'research.md'
    if existing:
        destination.write_bytes(b'previous export')
    cancelled = False
    original_fsync = export_file.os.fsync

    def cancel_after_flush(descriptor):
        nonlocal cancelled
        original_fsync(descriptor)
        cancelled = True

    monkeypatch.setattr(export_file.os, 'fsync', cancel_after_flush)
    with pytest.raises(InterruptedError, match='destination was not changed'):
        write_export(destination, 'A newly rendered bibliography', private_root=private,
                     cancelled=lambda: cancelled)
    assert destination.read_bytes() == b'previous export' if existing else not destination.exists()
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('relative', ['library.sqlite', 'originals/source.txt', 'new-export.md'])
def test_export_never_writes_inside_private_library(private, relative):
    destination = private / relative
    previous = destination.read_bytes() if destination.exists() else None

    with pytest.raises(ValueError, match='outside the private library'):
        write_export(destination, 'overwrite', private_root=private)

    assert (destination.read_bytes() if destination.exists() else None) == previous
    assert not tuple(private.rglob('.research-export-*'))


def test_normalized_parent_route_into_private_library_is_rejected(tmp_path, private):
    other = tmp_path / 'exports'
    other.mkdir()
    destination = other / '..' / private.name / 'library.sqlite'

    with pytest.raises(ValueError, match='outside the private library'):
        write_export(destination, 'overwrite', private_root=private)

    assert (private / 'library.sqlite').read_bytes() == b'private database'


def test_directory_is_not_an_export_target(tmp_path, private):
    destination = tmp_path / 'exports'
    destination.mkdir()

    with pytest.raises(ValueError, match='regular file'):
        write_export(destination, 'overwrite', private_root=private)

    assert destination.is_dir()
    assert not tuple(destination.iterdir())


@pytest.mark.parametrize('source_is_private', [False, True])
def test_file_symlink_is_never_followed(tmp_path, private, source_is_private):
    source = private / 'library.sqlite' if source_is_private else tmp_path / 'original.txt'
    if not source_is_private:
        source.write_bytes(b'outside original')
    previous = source.read_bytes()
    destination = tmp_path / 'export.md'
    _symlink(source, destination)

    with pytest.raises(ValueError, match='regular file'):
        write_export(destination, 'overwrite', private_root=private)

    assert source.read_bytes() == previous
    assert destination.is_symlink()


@pytest.mark.parametrize('source_is_private', [False, True])
def test_symlink_ancestor_is_rejected(tmp_path, private, source_is_private):
    actual = private if source_is_private else tmp_path / 'actual exports'
    if not source_is_private:
        actual.mkdir()
    alias = tmp_path / 'alias'
    _symlink(actual, alias, directory=True)

    with pytest.raises(ValueError, match='symbolic links'):
        write_export(alias / 'new.md', 'overwrite', private_root=private)

    assert not (actual / 'new.md').exists()


@pytest.mark.parametrize('source_is_private', [False, True])
def test_hardlink_target_is_rejected(tmp_path, private, source_is_private):
    source = private / 'originals' / 'source.txt' if source_is_private else tmp_path / 'source.txt'
    if not source_is_private:
        source.write_bytes(b'outside original')
    previous = source.read_bytes()
    destination = tmp_path / 'export.md'
    try:
        os.link(source, destination)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f'This environment cannot create hardlinks: {exc}')

    with pytest.raises(ValueError, match='hard links'):
        write_export(destination, 'overwrite', private_root=private)

    assert source.read_bytes() == destination.read_bytes() == previous
    assert source.samefile(destination)


@pytest.mark.parametrize('as_parent', [False, True])
def test_windows_reparse_attributes_rejected_without_following(
        monkeypatch, tmp_path, private, as_parent):
    folder = tmp_path / 'exports'
    folder.mkdir()
    destination = folder / 'research.md'
    destination.write_bytes(b'previous export')
    guarded = folder if as_parent else destination
    original_lstat = Path.lstat

    def reparse_lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == guarded:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, 'lstat', reparse_lstat)

    with pytest.raises(ValueError, match='reparse points'):
        write_export(destination, 'overwrite', private_root=private)

    assert destination.read_bytes() == b'previous export'
    assert not tuple(folder.glob('.research-export-*'))


@pytest.mark.parametrize('operation', ['fsync', 'replace'])
def test_failed_export_preserves_existing_file_and_cleans_temporary(
        monkeypatch, tmp_path, private, operation):
    destination = tmp_path / 'research.md'
    destination.write_bytes(b'previous export')

    def fail(*args, **kwargs):
        raise OSError('simulated disk failure')

    monkeypatch.setattr(export_file.os, operation, fail)

    with pytest.raises(OSError, match='simulated disk failure'):
        write_export(destination, 'new export', private_root=private)

    assert destination.read_bytes() == b'previous export'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existed', [False, True])
def test_destination_changed_during_export_requires_new_confirmation(
        monkeypatch, tmp_path, private, existed):
    destination = tmp_path / 'research.md'
    if existed:
        destination.write_bytes(b'previous export')

    def another_writer(_fd):
        destination.write_bytes(b'new content from another operation')

    monkeypatch.setattr(export_file.os, 'fsync', another_writer)

    with pytest.raises(ValueError, match='destination changed'):
        write_export(destination, 'this export', private_root=private)

    assert destination.read_bytes() == b'new content from another operation'
    assert not tuple(tmp_path.glob('.research-export-*'))


def test_invalid_unicode_fails_before_modifying_target(tmp_path, private):
    destination = tmp_path / 'research.md'
    destination.write_bytes(b'previous export')

    with pytest.raises(UnicodeEncodeError):
        write_export(destination, '\ud800', private_root=private)

    assert destination.read_bytes() == b'previous export'
    assert not tuple(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('name', [
    'NUL', 'con.txt', 'PRN.md', 'AUX.csv', 'COM1.txt', 'lpt9.md',
    'COM\u00b9.txt', 'LPT\u00b2.md', 'CONIN$', 'CONOUT$',
    'research.md:hidden', 'research.md::$DATA',
    'research?.md', 'research*.md', 'research|.md', 'research<.md',
    'research>.md', 'research".md',
    'research\x00.md', 'research\n.md', 'research\x7f.md', 'research\u0085.md',
    'research.md.', 'research.md ',
])
def test_windows_unsafe_filename_rejected_before_any_file_write(tmp_path, private, name):
    before = set(tmp_path.iterdir())

    with pytest.raises(ValueError, match='export filename'):
        write_export(tmp_path / name, 'new export', private_root=private)

    assert set(tmp_path.iterdir()) == before
    assert (private / 'library.sqlite').read_bytes() == b'private database'


@pytest.mark.parametrize('empty_path', ['', '.', '..'])
def test_empty_filename_rejected(empty_path, private):
    with pytest.raises(ValueError, match='export filename'):
        write_export(empty_path, 'new export', private_root=private)


def test_directory_path_with_trailing_separator_rejected(tmp_path, private):
    destination = tmp_path / 'exports'
    destination.mkdir()

    with pytest.raises(ValueError, match='export filename'):
        write_export(str(destination) + os.sep, 'new export', private_root=private)

    assert not tuple(destination.iterdir())


@pytest.mark.parametrize('name', ['COM10.md', 'NUL-source.txt', '\u0414\u043e\u0441\u043b\u0456\u0434\u0436\u0435\u043d\u043d\u044f.ris', 'notes and sources.md'])
def test_ordinary_names_remain_exportable(tmp_path, private, name):
    destination = tmp_path / name
    write_export(destination, 'exact export', private_root=private)
    assert destination.read_bytes() == b'exact export'
