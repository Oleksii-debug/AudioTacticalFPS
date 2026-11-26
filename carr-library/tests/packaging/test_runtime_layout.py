import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest
from platformdirs.unix import Unix
from platformdirs.windows import Windows

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'packaging'))
from runtime_layout import APP_NAME, resolve_layout


@pytest.fixture
def layout_root(tmp_path, monkeypatch):
    root = tmp_path / 'user data Ж'
    monkeypatch.setenv('XDG_DATA_HOME', str(root))
    with patch('runtime_layout.PlatformDirs', Unix):
        yield root


def test_actual_unix_resolution_no_write(tmp_path, layout_root):
    install = tmp_path / 'Program Files' / 'release 1'
    result = resolve_layout(install)
    assert result.install == install
    assert result.data == layout_root / APP_NAME
    assert not install.exists() and not layout_root.exists()


def test_upgrade_and_existing_data_preserved(tmp_path, layout_root):
    first = resolve_layout(tmp_path / 'release 1')
    first.data.mkdir(parents=True)
    marker = first.data / 'research.sqlite'
    marker.write_bytes(b'synthetic library')
    second = resolve_layout(tmp_path / 'release 2')
    assert second.data == first.data
    assert marker.read_bytes() == b'synthetic library'


@pytest.mark.parametrize('position', ['same', 'inside', 'ancestor'])
def test_overlap_rejected(layout_root, position):
    data = layout_root / APP_NAME
    candidate = {'same': data, 'inside': data / 'app', 'ancestor': layout_root}[position]
    with pytest.raises(ValueError, match='disjoint'):
        resolve_layout(candidate)
    assert not layout_root.exists()


def test_symlink_overlap(tmp_path, layout_root):
    data = layout_root / APP_NAME
    data.mkdir(parents=True)
    link = tmp_path / 'install-link'
    try:
        link.symlink_to(data, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'symlinks not supported: {exc}')
    with pytest.raises(ValueError, match='disjoint'):
        resolve_layout(link)


def test_invalid_path(tmp_path, layout_root):
    with pytest.raises(ValueError, match='absolute'):
        resolve_layout('relative')
    file = tmp_path / 'not directory'
    file.write_text('x', encoding='utf-8')
    with pytest.raises(ValueError, match='directory'):
        resolve_layout(file)


def test_upstream_windows_nonroaming_logic(tmp_path):
    local = tmp_path / 'Local AppData Ж'
    roaming = tmp_path / 'Roaming AppData'
    def folder(name):
        return os.fspath({'CSIDL_LOCAL_APPDATA': local, 'CSIDL_APPDATA': roaming}[name])
    with patch('runtime_layout.PlatformDirs', Windows), patch('platformdirs.windows.get_win_folder', side_effect=folder) as shell:
        result = resolve_layout(tmp_path / 'Program Files' / 'URL')
    assert result.data == local / APP_NAME
    shell.assert_called_once_with('CSIDL_LOCAL_APPDATA')
    assert not local.exists() and not roaming.exists()


@pytest.mark.parametrize('destination', ['install', 'data'])
@pytest.mark.parametrize('linked', [False, True])
def test_regular_file_ancestor(tmp_path, layout_root, monkeypatch, destination, linked):
    blocked = tmp_path / 'existing file Ж'
    blocked.write_bytes(b'preserve me')
    ancestor = blocked
    if linked:
        ancestor = tmp_path / 'file link'
        try:
            ancestor.symlink_to(blocked)
        except OSError as exc:
            pytest.skip(f'symlinks not supported: {exc}')
    impossible = ancestor / 'child' / 'release'
    install = tmp_path / 'program'
    if destination == 'data':
        monkeypatch.setenv('XDG_DATA_HOME', str(impossible))
    else:
        install = impossible
    with pytest.raises(ValueError, match='directory'):
        resolve_layout(install)
    assert blocked.read_bytes() == b'preserve me'
    assert not (tmp_path / 'program').exists()
    assert not layout_root.exists()
