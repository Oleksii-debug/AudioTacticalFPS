"""Use the existing sealed-tool verifier at the shared studio factories."""
from pathlib import Path
import os
import shutil
from types import SimpleNamespace

import pytest

from carr_library import audio_runtime as bundled_runtime
from carr_library import runtime_settings
from carr_library.speech import VoiceRegistry


def tools(directory):
    directory.mkdir()
    result = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = directory / (name + '.exe')
        path.write_bytes(b'Synthetic path marker; never executed.')
        result[name] = str(path)
    return result


@pytest.mark.parametrize('configured', [(), ('ffmpeg',), ('ffprobe',), ('ffmpeg', 'ffprobe')])
def test_studio_selects_verified_bundle_tools_and_preserves_explicit_settings(
        tmp_path, monkeypatch, configured):
    bundled = tools(tmp_path / 'sealed bundle tools')
    selected = tools(tmp_path / 'explicit tools')
    state = tmp_path / 'state'
    settings = runtime_settings.load_settings(state)
    for name in configured:
        settings[name] = selected[name]
    runtime_settings.save_settings(state, settings)
    before = runtime_settings.settings_path(state).read_bytes()
    original_path = os.environ.get('PATH')
    monkeypatch.setattr(bundled_runtime, 'bundled_audio_tools', lambda: bundled)
    monkeypatch.setattr(shutil, 'which', lambda *_: pytest.fail('Sealed tools must bypass PATH.'))
    runtime = runtime_settings.audio_runtime(tmp_path / 'output', state_root=state)
    for name in ('ffmpeg', 'ffprobe'):
        expected = selected[name] if name in configured else bundled[name]
        assert getattr(runtime, name) == str(Path(expected).resolve())
    assert runtime_settings.settings_path(state).read_bytes() == before
    assert os.environ.get('PATH') == original_path


def test_voice_discovery_receives_the_same_verified_ffmpeg(tmp_path, monkeypatch):
    bundled = tools(tmp_path / 'sealed bundle tools')
    calls = []
    marker = SimpleNamespace()
    monkeypatch.setattr(bundled_runtime, 'bundled_audio_tools', lambda: bundled)
    def discover(cls, **kwargs):
        calls.append(kwargs)
        return marker
    monkeypatch.setattr(VoiceRegistry, 'discover', classmethod(discover))
    stop = lambda: False
    assert runtime_settings.voice_registry(tmp_path / 'state', cancelled=stop) is marker
    assert calls == [{'ffmpeg': bundled['ffmpeg'], 'cancelled': stop}]


@pytest.mark.parametrize('operation', ['audio', 'voices'])
def test_invalid_sealed_runtime_never_falls_back_to_an_unverified_tool(
        tmp_path, monkeypatch, operation):
    def invalid():
        raise ValueError('Audio runtime checksum failed.')
    monkeypatch.setattr(bundled_runtime, 'bundled_audio_tools', invalid)
    monkeypatch.setattr(shutil, 'which', lambda *_: pytest.fail('Invalid bundle reached PATH fallback.'))
    monkeypatch.setattr(VoiceRegistry, 'discover', lambda **_: pytest.fail('Invalid bundle reached voices.'))
    with pytest.raises(ValueError, match='checksum'):
        if operation == 'audio':
            runtime_settings.audio_runtime(tmp_path / 'output', state_root=tmp_path / 'state')
        else:
            runtime_settings.voice_registry(tmp_path / 'state')


def test_installation_without_bundled_audio_keeps_existing_local_discovery(tmp_path, monkeypatch):
    selected = tools(tmp_path / 'installed tools')
    calls = []
    monkeypatch.setattr(bundled_runtime, 'bundled_audio_tools', lambda: None)
    def which(name):
        calls.append(name)
        return selected[name]
    monkeypatch.setattr(shutil, 'which', which)
    runtime = runtime_settings.audio_runtime(tmp_path / 'output', state_root=tmp_path / 'state')
    assert calls == ['ffmpeg', 'ffprobe']
    assert runtime.ffmpeg == str(Path(selected['ffmpeg']).resolve())
    assert runtime.ffprobe == str(Path(selected['ffprobe']).resolve())
