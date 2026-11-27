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
def test_competing_file_at_actual_publication_is_preserved(tmp_path, monkeypatch, binary):
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
               require_absent=True)
    assert target.read_bytes() == b'Independent work'
    assert not list(tmp_path.glob('.research-export-*'))
