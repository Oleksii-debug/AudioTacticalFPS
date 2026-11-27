"""Fixed Windows helper identity is separate from strict private-file trust."""
from pathlib import Path
import os
import stat
from types import SimpleNamespace

import pytest

from carr_library.security import _files
from carr_library.security import standard_windows as standard


class SystemDirectory:
    def __init__(self, path, size=None):
        self.path = str(path)
        self.size = size
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, buffer, capacity):
        self.calls.append(capacity)
        buffer.value = self.path
        return len(self.path) if self.size is None else self.size


def system_directory(monkeypatch, root, *, size=None):
    lookup = SystemDirectory(root, size)
    def load(name, *, use_last_error):
        assert name == 'kernel32' and use_last_error is True
        return SimpleNamespace(GetSystemDirectoryW=lookup)
    monkeypatch.setattr(standard.ctypes, 'WinDLL', load, raising=False)
    return lookup


def fixed_helper(tmp_path):
    system = tmp_path / 'System32'
    helper = system / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b'inert synthetic system helper; never executed')
    return system, helper


@pytest.mark.parametrize('links', [1, 2, 3])
def test_fixed_os_helper_accepts_servicing_hardlinks_without_environment_selection(tmp_path, monkeypatch, links):
    root, helper = fixed_helper(tmp_path)
    for index in range(links - 1):
        os.link(helper, tmp_path / ('servicing-link-' + str(index)))
    decoy = tmp_path / 'untrusted-environment'
    decoy.mkdir()
    (decoy / 'powershell.exe').write_bytes(b'not selected')
    monkeypatch.setenv('SystemRoot', str(decoy))
    monkeypatch.setenv('WINDIR', str(decoy))
    monkeypatch.setenv('PATH', str(decoy))
    lookup = system_directory(monkeypatch, root)
    assert standard.WindowsStandardHost._powershell() == helper
    assert helper.stat().st_nlink == links
    assert lookup.calls == [32768]
    assert lookup.argtypes == [standard.ctypes.c_wchar_p, standard.ctypes.c_uint32]
    assert lookup.restype is standard.ctypes.c_uint32


def test_hardlinked_private_files_remain_refused_before_any_content_read(tmp_path, monkeypatch):
    source = tmp_path / 'private-state.bin'
    source.write_bytes(b'private state')
    os.link(source, tmp_path / 'foreign-link.bin')
    def forbidden(*args, **kwargs):
        raise AssertionError('A hardlinked private file reached its content reader.')
    monkeypatch.setattr(_files, 'verified_reader', forbidden)
    monkeypatch.setattr(standard, 'verified_reader', forbidden)
    for read in (_files.file_info, _files.read_bytes, standard._file_digest):
        with pytest.raises(_files.TrustError, match='unlinked local file'):
            read(source)
    assert source.read_bytes() == b'private state'


@pytest.mark.parametrize('mode,attributes,links', [
    (stat.S_IFDIR | 0o700, 0, 1),
    (stat.S_IFLNK | 0o700, 0, 1),
    (stat.S_IFREG | 0o700, 0x400, 1),
    (stat.S_IFREG | 0o700, 0, 0),
])
def test_system_helper_rejects_nonregular_or_redirected_leaf(tmp_path, monkeypatch, mode, attributes, links):
    root, helper = fixed_helper(tmp_path)
    system_directory(monkeypatch, root)
    original = Path.lstat
    def metadata(path, *args, **kwargs):
        if path == helper:
            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_nlink=links)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(_files.TrustError, match='fixed Windows system helper'):
        standard.WindowsStandardHost._powershell()


@pytest.mark.parametrize('size', [0, 32768, 32769])
def test_unavailable_or_truncated_system_directory_never_reaches_a_file(tmp_path, monkeypatch, size):
    system_directory(monkeypatch, tmp_path, size=size)
    def forbidden(*args, **kwargs):
        raise AssertionError('An invalid API result reached a filesystem lookup.')
    monkeypatch.setattr(standard, 'directory', forbidden)
    with pytest.raises(_files.TrustError, match='system directory is unavailable'):
        standard.WindowsStandardHost._powershell()


def test_system_api_failure_does_not_fall_back_to_environment_or_path(tmp_path, monkeypatch):
    root, helper = fixed_helper(tmp_path)
    monkeypatch.setenv('SystemRoot', str(root))
    monkeypatch.setenv('PATH', str(helper.parent))
    def fail(*args, **kwargs):
        raise OSError('synthetic system API unavailable')
    monkeypatch.setattr(standard.ctypes, 'WinDLL', fail, raising=False)
    with pytest.raises(OSError, match='synthetic system API unavailable'):
        standard.WindowsStandardHost._powershell()


def test_redirected_system_helper_parent_is_refused(tmp_path, monkeypatch):
    root, helper = fixed_helper(tmp_path)
    system_directory(monkeypatch, root)
    redirected = root / 'WindowsPowerShell'
    original = Path.lstat
    def metadata(path, *args, **kwargs):
        if path == redirected:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400, st_nlink=1)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', metadata)
    with pytest.raises(_files.TrustError, match='Directory is linked, redirected'):
        standard.WindowsStandardHost._powershell()
