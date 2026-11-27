"""The studio and CLI agree on explicitly selected and sealed local tools."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library import audio_runtime
from carr_library.__main__ import main
from carr_library.audio import engines, ffmpeg
from carr_library.ui.audio_studio import StudioController


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


def test_explicit_selection_is_not_replaced_by_a_bundle_or_path(tmp_path, monkeypatch):
    selected = executable(tmp_path, 'chosen.exe')
    monkeypatch.setattr(audio_runtime, 'bundled_audio_tools',
                        lambda: pytest.fail('Explicit selection must precede automatic discovery.'))
    assert ffmpeg.select_audio_tool('ffmpeg', selected) == selected
    for invalid in (Path('relative.exe'), tmp_path / 'missing.exe'):
        with pytest.raises(ValueError, match='absolute path'):
            ffmpeg.select_audio_tool('ffmpeg', invalid)


def test_explicit_cli_path_is_used_for_voice_discovery(monkeypatch, capsys):
    selected = Path(sys.executable).resolve()
    seen = []
    def registry(**kwargs):
        seen.append(kwargs['ffmpeg'])
        return SimpleNamespace(voices=lambda: (), capabilities=lambda: [])
    monkeypatch.setattr(engines, 'default_registry', registry)
    assert main(['--audio-voices', '--ffmpeg', str(selected)]) == 0
    assert seen == [selected]
    assert json.loads(capsys.readouterr().out)['voices'] == []


def test_relative_cli_selection_is_rejected_before_voice_discovery(monkeypatch, capsys):
    monkeypatch.setattr(engines, 'default_registry', lambda **kw: pytest.fail('Invalid tool reached registry.'))
    assert main(['--audio-voices', '--ffmpeg', './ffmpeg']) == 1
    assert 'absolute path' in capsys.readouterr().err


def test_studio_refreshes_its_own_voice_registry_after_explicit_tool_change(tmp_path, monkeypatch):
    first = executable(tmp_path, 'first.exe')
    second = executable(tmp_path, 'second.exe')
    seen = []
    def registry(**kwargs):
        seen.append(Path(kwargs['ffmpeg']))
        return SimpleNamespace(voices=lambda: (), capabilities=lambda: [])
    monkeypatch.setattr(engines, 'default_registry', registry)
    controller = StudioController(private_root=tmp_path / 'library', ffmpeg=first)
    assert controller.voices() == ()
    controller.configure(ffmpeg=second)
    assert controller.voices() == ()
    assert seen == [first, second]
    assert controller.effective_ffmpeg() == second


def test_studio_reuses_saved_tool_before_automatic_bundle_discovery(tmp_path, monkeypatch):
    from carr_library.audio.engine_settings import EngineSettings, save_settings
    selected = executable(tmp_path, 'saved.exe')
    root = tmp_path / 'library'
    save_settings(root, EngineSettings(ffmpeg=str(selected)))
    monkeypatch.setattr(audio_runtime, 'bundled_audio_tools',
                        lambda: pytest.fail('Saved explicit selection must precede automatic discovery.'))
    assert StudioController(private_root=root).effective_ffmpeg() == selected


def test_explicit_studio_tool_can_replace_a_missing_saved_tool(tmp_path, monkeypatch):
    from carr_library.audio.engine_settings import EngineSettings, save_settings
    previous = executable(tmp_path, 'previous.exe')
    selected = executable(tmp_path, 'replacement.exe')
    root = tmp_path / 'library'
    save_settings(root, EngineSettings(ffmpeg=str(previous)))
    previous.unlink()
    seen = []
    def registry(**kwargs):
        seen.append(Path(kwargs['ffmpeg']))
        return SimpleNamespace(voices=lambda: (), capabilities=lambda: [])
    monkeypatch.setattr(engines, 'default_registry', registry)
    controller = StudioController(private_root=root, ffmpeg=selected)
    assert controller.voices() == ()
    assert seen == [selected]
