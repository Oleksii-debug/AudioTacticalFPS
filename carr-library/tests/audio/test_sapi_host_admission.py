"""Exact serviced PowerShell host admission; synthetic paths never execute.

The system-directory API alone is doubled here. Real Windows-directory
observations are separately required by windows_native_sapi_smoke.py.
"""
import ctypes
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from carr_library.audio import engines as api
from carr_library.audio.model_manifest import ModelManifestError


def hosts(tmp_path, monkeypatch):
    root = tmp_path / 'Windows'
    result = {}
    for architecture, folder in [('64', 'System32'), ('32', 'SysWOW64')]:
        path = root / folder / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'synthetic host; never executed')
        result[architecture] = path
    monkeypatch.setattr(api, '_windows_directory', lambda: root)
    monkeypatch.setattr(api, '_run', lambda *args, **kwargs: pytest.fail('A synthetic host was executed.'))
    return root, result


@pytest.mark.parametrize('architecture', ['32', '64'])
def test_serviced_hardlink_is_allowed_only_at_selected_system_host(tmp_path, monkeypatch, architecture):
    root, selected = hosts(tmp_path, monkeypatch)
    host = selected[architecture]
    servicing = root / 'servicing-copy.exe'
    os.link(host, servicing)
    assert host.lstat().st_nlink == 2
    assert api._system_powershell(host, architecture=architecture) == host
    engine_id = 'sapi5-32' if architecture == '32' else 'sapi5'
    assert api.SAPI5Engine(host, engine_id=engine_id).binary == host
    with pytest.raises(api.EngineUnavailable, match='exact installed'):
        api._system_powershell(servicing, architecture=architecture)
    assert host.read_bytes() == servicing.read_bytes()
    assert host.lstat().st_ino == servicing.lstat().st_ino


@pytest.mark.parametrize('architecture', ['32', '64'])
@pytest.mark.parametrize('kind', ['foreign-copy', 'foreign-hardlink', 'wrong-architecture', 'parent-spelling'])
def test_foreign_or_wrong_architecture_path_is_never_a_system_host(tmp_path, monkeypatch, architecture, kind):
    _, selected = hosts(tmp_path, monkeypatch)
    host = selected[architecture]
    if kind == 'wrong-architecture':
        candidate = selected['64' if architecture == '32' else '32']
    elif kind == 'parent-spelling':
        candidate = host.parent / '..' / 'v1.0' / host.name
    else:
        candidate = tmp_path / 'powershell.exe'
        if kind == 'foreign-hardlink':
            os.link(host, candidate)
        else:
            candidate.write_bytes(host.read_bytes())
    with pytest.raises(api.EngineUnavailable, match='exact installed'):
        api.SAPI5Engine(candidate, engine_id='sapi5-32' if architecture == '32' else 'sapi5')


@pytest.mark.parametrize('kind', ['symlink', 'reparse', 'ancestor-reparse'])
def test_linked_system_host_is_rejected_before_execution(tmp_path, monkeypatch, kind):
    _, selected = hosts(tmp_path, monkeypatch)
    host = selected['32']
    affected = host.parent if kind == 'ancestor-reparse' else host
    original = Path.lstat
    def observed(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == affected:
            return SimpleNamespace(
                st_mode=(stat.S_IFLNK | 0o777) if kind == 'symlink' else info.st_mode,
                st_file_attributes=0 if kind == 'symlink' else 0x400)
        return info
    # A stat-boundary double avoids requiring Windows Developer Mode for a
    # symlink fixture. It exercises the real local_path ancestor checks.
    monkeypatch.setattr(Path, 'lstat', observed)
    with pytest.raises(ModelManifestError, match='Linked|Reparse'):
        api.SAPI5Engine(host, engine_id='sapi5-32')


@pytest.mark.parametrize('kind', ['missing', 'directory'])
def test_unavailable_system_host_is_refused(tmp_path, monkeypatch, kind):
    _, selected = hosts(tmp_path, monkeypatch)
    host = selected['64']
    host.unlink()
    if kind == 'directory':
        host.mkdir()
    with pytest.raises((FileNotFoundError, api.EngineUnavailable)):
        api.SAPI5Engine()


@pytest.mark.parametrize('architecture', [None, True, 64, 'x86', ''])
def test_architecture_is_typed_before_windows_lookup(monkeypatch, architecture):
    monkeypatch.setattr(api, '_windows_directory', lambda: pytest.fail('Invalid architecture reached Windows.'))
    with pytest.raises(ValueError, match='architecture'):
        api._system_powershell(architecture=architecture)


def test_default_and_lazy_selection_ignore_spoofed_systemroot(tmp_path, monkeypatch):
    _, selected = hosts(tmp_path, monkeypatch)
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'foreign'))
    assert api.SAPI5Engine().binary == selected['64']
    lazy = api._LazySAPI32Engine()
    assert lazy.voices() == () and lazy._adapter is None
    exact = 'sapi5-32:' + 'a' * 24
    assert lazy._selected(exact).binary == selected['32']
    assert lazy._selected(exact).id == 'sapi5-32'
    # Explicit catalogue discovery uses the same actual-root contract.
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='nt', environ={'SystemRoot': 'foreign'}))
    monkeypatch.setattr(api, 'NativeSAPI5Engine', lambda: SimpleNamespace(id='sapi5'))
    monkeypatch.setattr(api.shutil, 'which', lambda name: None)
    registry = api.default_registry(include_powershell=True)
    assert registry.engines['sapi5-32'].binary == selected['32']


def test_host_path_is_checked_again_before_each_request(tmp_path, monkeypatch):
    root, selected = hosts(tmp_path, monkeypatch)
    engine = api.SAPI5Engine()
    foreign = tmp_path / 'same-bytes.exe'
    foreign.write_bytes(selected['64'].read_bytes())
    engine.binary = foreign
    with pytest.raises(api.EngineUnavailable, match='exact installed'):
        engine._call({'operation': 'voices'})
    engine.binary = selected['64']
    monkeypatch.setattr(api, '_windows_directory', lambda: root / 'changed-root')
    with pytest.raises(api.EngineUnavailable, match='exact installed'):
        engine._call({'operation': 'voices'})


def test_cancelled_request_never_resolves_a_host_or_launches(tmp_path, monkeypatch):
    hosts(tmp_path, monkeypatch)
    engine = api.SAPI5Engine()
    monkeypatch.setattr(api, '_system_powershell', lambda *args, **kwargs: pytest.fail('Cancelled host lookup.'))
    with pytest.raises(InterruptedError):
        engine._call({'operation': 'voices'}, cancelled=lambda: True)


def native_api(monkeypatch, value, *, returned=None):
    calls = []
    def read(buffer, capacity):
        calls.append(('read', capacity))
        buffer.value = value
        return len(value) if returned is None else returned
    def load(name, **options):
        calls.append(('load', name, options))
        return SimpleNamespace(GetSystemWindowsDirectoryW=read)
    # Keep ctypes' shared namespace untouched: other current file helpers use it.
    monkeypatch.setattr(api, 'ctypes', SimpleNamespace(
        WinDLL=load, POINTER=ctypes.POINTER, c_wchar=ctypes.c_wchar,
        c_uint=ctypes.c_uint, create_unicode_buffer=ctypes.create_unicode_buffer))
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='nt', environ={'SystemRoot': 'foreign'}))
    return calls, read


def test_windows_directory_uses_fixed_native_api(tmp_path, monkeypatch):
    root = tmp_path / 'Actual-Windows'
    root.mkdir()
    calls, read = native_api(monkeypatch, str(root))
    assert api._windows_directory() == root
    assert calls == [('load', 'kernel32', {'use_last_error': True}), ('read', 32768)]
    assert read.argtypes == [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
    assert read.restype is ctypes.c_uint


@pytest.mark.parametrize('kind', ['zero', 'truncated', 'relative', 'missing', 'not-directory', 'empty'])
def test_invalid_native_windows_directory_is_refused(tmp_path, monkeypatch, kind):
    root = tmp_path / 'Actual-Windows'
    if kind == 'not-directory':
        root.write_bytes(b'not a directory')
    elif kind != 'missing':
        root.mkdir()
    value = 'relative-Windows' if kind == 'relative' else '' if kind == 'empty' else str(root)
    returned = 0 if kind == 'zero' else 32768 if kind == 'truncated' else 1 if kind == 'empty' else None
    native_api(monkeypatch, value, returned=returned)
    with pytest.raises((api.EngineUnavailable, FileNotFoundError)):
        api._windows_directory()


def test_nonwindows_never_resolves_environment_host(monkeypatch):
    monkeypatch.setattr(api, 'os', SimpleNamespace(name='posix', environ={'SystemRoot': 'foreign'}))
    monkeypatch.setattr(api, 'ctypes', SimpleNamespace(
        WinDLL=lambda *args, **kwargs: pytest.fail('Non-Windows host lookup.')))
    with pytest.raises(api.EngineUnavailable, match='requires Windows'):
        api.SAPI5Engine()


@pytest.mark.parametrize('engine_id', [None, 'sapi4', 'custom'])
def test_unknown_sapi_identity_is_rejected_before_host_lookup(monkeypatch, engine_id):
    monkeypatch.setattr(api, '_windows_directory', lambda: pytest.fail('Unrecognized engine reached Windows.'))
    with pytest.raises(ValueError, match='engine identity'):
        api.SAPI5Engine(engine_id=engine_id)
