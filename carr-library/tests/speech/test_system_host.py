"""A Windows serviced executable may be hard-linked; user inputs may not."""
import os

import pytest

from carr_library.speech import _io


def test_only_fixed_system_powershell_can_use_servicing_hardlinks(tmp_path, monkeypatch):
    root = tmp_path / 'Windows'
    executable = root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic executable identity')
    twin = tmp_path / 'servicing-copy.exe'
    os.link(executable, twin)
    monkeypatch.setattr(_io, 'windows_directory', lambda: root)
    assert _io.system_powershell(executable) == executable
    with pytest.raises(ValueError, match='links'):
        _io.local_file(executable)
    with pytest.raises(ValueError, match='system PowerShell'):
        _io.system_powershell(twin)


def test_system_host_does_not_accept_symlinks(tmp_path, monkeypatch):
    root = tmp_path / 'Windows'
    executable = root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    executable.parent.mkdir(parents=True)
    actual = tmp_path / 'other.exe'
    actual.write_bytes(b'other')
    try:
        executable.symlink_to(actual)
    except OSError:
        pytest.skip('This host does not allow symlink creation.')
    monkeypatch.setattr(_io, 'windows_directory', lambda: root)
    with pytest.raises(ValueError, match='links'):
        _io.system_powershell(executable)
