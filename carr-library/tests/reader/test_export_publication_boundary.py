"""Fault injection at the create-only publication boundary, with real files."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.research import export_file


@pytest.mark.parametrize('binary', [False, True])
def test_create_only_cleanup_after_publication_is_truthful(tmp_path, monkeypatch, binary):
    target = tmp_path / 'result'
    private = tmp_path / 'private'
    private.mkdir()
    payload = 'Точна цитата\r\nExact original.'
    original_unlink = os.unlink
    failed = False

    def fail_once_after_publication(path, *args, **kwargs):
        nonlocal failed
        if (Path(path).name.startswith('.research-export-')
                and target.exists() and not failed):
            failed = True
            assert target.read_bytes() == payload.encode()
            raise PermissionError('injected transient staging cleanup failure')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', fail_once_after_publication)
    writer = export_file.write_binary_export if binary else export_file.write_export
    writer(target, payload.encode() if binary else payload, private_root=private,
           require_absent=True)
    assert failed is (os.name != 'nt')
    assert target.read_bytes() == payload.encode()
    assert target.stat().st_nlink == 1
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_process_exit_immediately_after_create_only_publication(tmp_path, binary):
    target = tmp_path / 'result'
    private = tmp_path / 'private'
    private.mkdir()
    payload = 'Точна цитата\r\nExact original.'
    code = '''
import os, sys
from carr_library.research import export_file
operation = 'rename' if os.name == 'nt' else 'link'
actual = getattr(os, operation)
def publish_then_exit(*args, **kwargs):
    actual(*args, **kwargs)
    os._exit(74)
setattr(os, operation, publish_then_exit)
writer = export_file.write_binary_export if sys.argv[3] == '1' else export_file.write_export
payload = sys.argv[4]
writer(sys.argv[1], payload.encode() if sys.argv[3] == '1' else payload,
       private_root=sys.argv[2], require_absent=True)
'''
    # The child must execute the same source/installed publisher as this test.
    environment = dict(os.environ, PYTHONPATH=str(Path(export_file.__file__).parents[2]))
    child = subprocess.run([sys.executable, '-B', '-c', code, str(target), str(private),
                            str(int(binary)), payload], capture_output=True, timeout=30,
                           env=environment)
    assert child.returncode == 74, child.stderr
    assert target.read_bytes() == payload.encode()
    residue = list(tmp_path.glob('.research-export-*'))
    if os.name == 'nt':
        # The single native no-replace rename must leave no second link,
        # even when the process exits before returning from publication.
        assert not residue
        assert target.stat().st_nlink == 1
        export_file.write_export(target, 'confirmed replacement', private_root=private)
        assert target.read_bytes() == b'confirmed replacement'
    else:
        # Existing POSIX hard-link fallback is fail-closed after this crash;
        # this branch does NOT qualify POSIX crash recovery.
        assert len(residue) == 1 and residue[0].samefile(target)
        assert target.stat().st_nlink == 2
        with pytest.raises(ValueError, match='hard links'):
            export_file.write_export(target, 'confirmed replacement', private_root=private)
        assert target.read_bytes() == payload.encode()


@pytest.mark.parametrize('binary', [False, True])
@pytest.mark.parametrize('explicit_policy', [False, True])
def test_competing_file_at_actual_publication_is_preserved(tmp_path, monkeypatch, binary, explicit_policy):
    target = tmp_path / 'result'
    private = tmp_path / 'private'
    private.mkdir()
    operation = 'rename' if os.name == 'nt' else 'link'
    actual = getattr(os, operation)

    def compete_then_publish(source, destination, **kwargs):
        target.write_bytes(b'Independent work')
        actual(source, destination, **kwargs)

    monkeypatch.setattr(os, operation, compete_then_publish)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(FileExistsError):
        writer(target, b'new' if binary else 'new', private_root=private,
               **({'require_absent': True} if explicit_policy else {}))
    assert target.read_bytes() == b'Independent work'
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
@pytest.mark.parametrize('when', ['after_approval', 'after_staging'])
def test_equal_metadata_content_change_requires_fresh_approval(tmp_path, monkeypatch, binary, when):
    target = tmp_path / 'result'
    private = tmp_path / 'private'
    private.mkdir()
    target.write_bytes(b'Owner version A')
    original_info = target.stat()
    original_state = export_file._file_state(original_info)
    state = export_file._file_state

    def frozen_visible_metadata(info):
        # This models Windows metadata equivalence explicitly. The destination
        # remains the same real ordinary file and receives different real bytes.
        if (info.st_dev, info.st_ino) == (original_info.st_dev, original_info.st_ino):
            return original_state
        return state(info)

    monkeypatch.setattr(export_file, '_file_state', frozen_visible_metadata)
    approval = export_file.capture_export_destination(target, private_root=private)

    def change_existing_file():
        target.write_bytes(b'Owner version B')
        os.utime(target, ns=(original_info.st_atime_ns, original_info.st_mtime_ns))
        assert target.stat().st_size == original_info.st_size
        assert export_file._file_state(target.stat()) == original_state

    if when == 'after_approval':
        change_existing_file()
    else:
        sync = os.fsync
        def change_after_real_staging(fd):
            sync(fd)
            change_existing_file()
        monkeypatch.setattr(os, 'fsync', change_after_real_staging)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new' if binary else 'new', private_root=private, approval=approval)
    assert target.read_bytes() == b'Owner version B'
    assert not list(tmp_path.glob('.research-export-*'))


def test_cancel_during_existing_content_snapshot_preserves_file(tmp_path):
    target = tmp_path / 'result'
    original = b'A' * (3 * 1024 * 1024)
    target.write_bytes(original)
    checks = 0

    def cancel_during_snapshot():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(TimeoutError, match='destination was not changed'):
        export_file.write_export(target, 'new', private_root=tmp_path / 'private',
                                 cancelled=cancel_during_snapshot)
    assert checks == 3
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))

@pytest.mark.parametrize('binary', [False, True])
def test_create_only_export_rejects_occupied_file_before_reading_its_contents(tmp_path, monkeypatch, binary):
    from carr_library.research import export_file
    library = tmp_path / 'private-library'
    library.mkdir()
    target = tmp_path / 'occupied-export'
    original = b'Existing user bytes must not be read or replaced by create-only export.'
    target.write_bytes(original)

    def forbidden(*args, **kwargs):
        raise AssertionError('Create-only export must reject an occupied destination before hashing it.')

    monkeypatch.setattr(export_file, '_content_state', forbidden)
    writer = export_file.write_binary_export if binary else export_file.write_export
    payload = b'New diagnostics' if binary else 'New diagnostics'
    with pytest.raises(FileExistsError):
        writer(target, payload, private_root=library, require_absent=True)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))
