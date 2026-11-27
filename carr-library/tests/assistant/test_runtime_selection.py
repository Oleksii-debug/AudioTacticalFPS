"""Real local FFmpeg through the active tool resolver, with an empty PATH.

The handle fixture captures installed local executable bytes and their hashes.
It does not represent a publisher-verified Windows bundle or claim activation
of Windows PE executables on another platform. No voice/model is downloaded.
"""
from hashlib import sha256
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.production import LocalProduction
from carr_library.audio import runtime
from carr_library.audio.engines import default_registry
from carr_library.audio.ffmpeg import wav_info


@pytest.fixture
def captured_local_tools(tmp_path, monkeypatch):
    binaries = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe')}
    if not all(binaries.values()):
        pytest.skip('Actual installed FFmpeg and ffprobe are required for this local runtime boundary.')
    directory = tmp_path / 'selected-runtime'
    directory.mkdir()
    records = {}
    for name, binary in binaries.items():
        path = directory / (name + '.exe')
        shutil.copyfile(binary, path)
        path.chmod(0o700)
        records[path.name] = {'size_bytes': path.stat().st_size,
                              'sha256': sha256(path.read_bytes()).hexdigest()}
    monkeypatch.setattr(runtime, '_active', {'directory': directory, 'executables': records, 'users': 1})
    monkeypatch.setenv('PATH', '')
    return directory


def test_default_assistant_uses_selected_real_tool_without_path(captured_local_tools, tmp_path):
    assert shutil.which('ffmpeg') is None
    registry = default_registry()
    voices = [voice for voice in registry.voices() if voice.engine == 'flite' and voice.available]
    if not voices:
        pytest.skip('The installed FFmpeg needs its actual Flite speech filter.')
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    production = LocalProduction(app.root, registry=registry)
    result = production({'operation_id': 'a' * 32, '_artifact_budget_bytes': 32 * 1024 ** 2,
                         'text': 'The selected local speech tool works without a search path.',
                         'voice_ids': [voices[0].id], 'language': 'en', 'stems': False},
                        output, lambda: False, lambda message: None)
    assert wav_info(result['output']).seconds > 0
    assert Path(result['output']).is_relative_to(output)
    assert runtime.available_tool('ffmpeg') == str(captured_local_tools / 'ffmpeg.exe')


def test_selected_runtime_tamper_cannot_fall_back_to_path(captured_local_tools, tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    (captured_local_tools / 'ffmpeg.exe').write_bytes(b'Changed selected executable')
    production = LocalProduction(app.root, registry=object(),
        renderer=lambda *a, **k: pytest.fail('A changed selected runtime reached rendering.'))
    monkeypatch.setenv('PATH', str(Path(shutil.__file__).parent))
    with pytest.raises(ValueError, match='reviewed bytes'):
        production({'operation_id': 'b' * 32, '_artifact_budget_bytes': 32 * 1024 ** 2,
                    'text': 'Never render with a changed selected runtime.',
                    'voice_ids': ['flite:kal'], 'language': 'en'},
                   output, lambda: False, lambda message: None)
    assert not list(output.iterdir())


def test_missing_local_runtime_fails_actionably_with_empty_path(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, '_active', None)
    monkeypatch.setenv('PATH', '')
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'
    output.mkdir()
    production = LocalProduction(app.root, registry=object())
    with pytest.raises(ValueError, match='reviewed local FFmpeg runtime'):
        production({'operation_id': 'c' * 32, '_artifact_budget_bytes': 32 * 1024 ** 2,
                    'text': 'Unavailable tool.', 'voice_ids': ['flite:kal'], 'language': 'en'},
                   output, lambda: False, lambda message: None)
    assert not list(output.iterdir())
