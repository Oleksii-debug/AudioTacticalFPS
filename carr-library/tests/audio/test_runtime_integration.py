"""Startup ordering and executable discovery without running synthetic tools."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library import audio_cli, audio_runtime, native_runtime
from carr_library.__main__ import main
from carr_library.audio import ffmpeg


def executable(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text('Synthetic executable marker; never executed.', encoding='utf-8')
    path.chmod(0o755)
    return path


@pytest.mark.parametrize('name', ['ffmpeg', 'ffprobe', 'ffplay', 'espeak-ng'])
def test_absolute_path_search_excludes_current_and_relative_directories(tmp_path, monkeypatch, name):
    current, trusted = tmp_path / 'book folder', tmp_path / 'installed tools'
    executable(current, name + ffmpeg._TOOL_SUFFIX)
    executable(current / 'relative tools', name + ffmpeg._TOOL_SUFFIX)
    selected = executable(trusted, name + ffmpeg._TOOL_SUFFIX)
    monkeypatch.chdir(current)
    monkeypatch.setenv('PATH', os.pathsep.join(['', '.', 'relative tools', str(current), str(trusted)]))
    assert ffmpeg.find_local_tool(name) == selected


def test_current_directory_cannot_supply_an_implicit_fallback(tmp_path, monkeypatch):
    executable(tmp_path, 'ffmpeg' + ffmpeg._TOOL_SUFFIX)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', os.pathsep.join(['', '.', str(tmp_path)]))
    assert ffmpeg.find_local_tool('ffmpeg') is None


def test_windows_native_suffix_never_selects_shell_extensions(tmp_path, monkeypatch):
    current, directory = tmp_path / 'cwd', tmp_path / 'Windows tools'
    current.mkdir()
    executable(directory, 'ffmpeg.cmd')
    executable(directory, 'ffmpeg.bat')
    monkeypatch.chdir(current)
    monkeypatch.setattr(ffmpeg, '_TOOL_SUFFIX', '.exe')
    monkeypatch.setenv('PATHEXT', '.CMD;.BAT;.EXE')
    monkeypatch.setenv('PATH', '"' + str(directory) + '"')
    assert ffmpeg.find_local_tool('ffmpeg') is None
    selected = executable(directory, 'ffmpeg.exe')
    assert ffmpeg.find_local_tool('ffmpeg') == selected


def test_bundle_activation_precedes_audio_adapter_construction(monkeypatch):
    calls = []
    handle = SimpleNamespace(close=lambda: calls.append('native-close'))
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: handle)
    monkeypatch.setattr(audio_runtime, 'activate_bundled_audio_runtime', lambda: calls.append('audio-runtime'))
    monkeypatch.setattr(audio_cli, 'run_audio', lambda args: calls.append('audio-adapters') or 0)
    assert main(['--audio-voices']) == 0
    assert calls == ['audio-runtime', 'audio-adapters', 'native-close']


def test_invalid_runtime_prevents_audio_adapter_execution(monkeypatch, capsys):
    closed = []
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime',
                        lambda: SimpleNamespace(close=lambda: closed.append(True)))
    def invalid():
        raise ValueError('Audio runtime checksum failed.')
    monkeypatch.setattr(audio_runtime, 'activate_bundled_audio_runtime', invalid)
    monkeypatch.setattr(audio_cli, 'run_audio', lambda args: pytest.fail('Unverified runtime reached adapters.'))
    assert main(['--audio-voices']) == 1
    assert 'checksum failed' in capsys.readouterr().err
    assert closed == [True]


def test_explicit_cli_runtime_is_used_for_voice_discovery(monkeypatch, capsys):
    from carr_library.audio import engines
    selected = Path(sys.executable).resolve()
    calls = []
    def registry(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(voices=lambda: (), capabilities=lambda: [])
    monkeypatch.setattr(engines, 'default_registry', registry)
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: None)
    monkeypatch.setattr(audio_runtime, 'activate_bundled_audio_runtime', lambda: None)
    assert main(['--audio-voices', '--ffmpeg', str(selected)]) == 0
    assert calls[0]['ffmpeg'] == selected
    assert json.loads(capsys.readouterr().out)['voices'] == []


def test_relative_cli_runtime_is_rejected_before_voice_discovery(monkeypatch, capsys):
    from carr_library.audio import engines
    monkeypatch.setattr(engines, 'default_registry', lambda **kwargs: pytest.fail('Relative tool reached registry.'))
    monkeypatch.setattr(native_runtime, 'activate_bundled_runtime', lambda: None)
    monkeypatch.setattr(audio_runtime, 'activate_bundled_audio_runtime', lambda: None)
    assert main(['--audio-voices', '--ffmpeg', './ffmpeg']) == 1
    assert 'absolute path' in capsys.readouterr().err
