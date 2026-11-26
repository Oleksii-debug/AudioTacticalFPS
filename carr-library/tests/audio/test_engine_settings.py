"""Only explicit local model/executable configuration reaches the shared registry."""
import json
import os
from pathlib import Path

import pytest

from carr_library.audio.engine_settings import (
    EngineSettings, configured_registry, load_settings, save_settings, validate_settings_bytes,
)
from carr_library.ui.audio_studio import StudioController


def test_settings_roundtrip_and_studio_share_one_registry(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    library.mkdir()
    binary = tmp_path / 'ffmpeg-test-placeholder'
    binary.write_bytes(b'Not executed')
    settings = EngineSettings(ffmpeg=str(binary))
    save_settings(library, settings)
    assert load_settings(library) == settings
    calls, registry = [], object()
    def factory(**kwargs):
        calls.append(kwargs)
        return registry
    monkeypatch.setattr('carr_library.audio.engines.default_registry', factory)
    assert configured_registry(library) is registry
    assert StudioController(private_root=library)._registry() is registry
    assert calls == [dict(ffmpeg=str(binary), piper_executable=None, piper_manifest=None, dordiai_manifest=None)] * 2


@pytest.mark.parametrize('bad', [{'api_key': 'synthetic'}, {'helper': '/tmp/program'}, {'piper_executable': '/tmp/piper'},
                               {'ffmpeg': '//server/share/program'}, {'ffmpeg': 'relative-program'}, {'version': True}])
def test_unknown_secret_paths_and_incomplete_models_are_rejected(bad):
    value = EngineSettings().to_dict()
    value.update(bad)
    with pytest.raises(ValueError):
        validate_settings_bytes(json.dumps(value).encode())


def test_linked_setting_file_and_untrusted_linked_executable_preserve_previous(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    saved = save_settings(library, EngineSettings())
    before = saved.read_bytes()
    binary = tmp_path / 'executable'
    binary.write_bytes(b'Not executed')
    os.link(binary, tmp_path / 'alias')
    with pytest.raises(ValueError, match='without links'):
        save_settings(library, EngineSettings(ffmpeg=str(binary)))
    assert saved.read_bytes() == before
    os.link(saved, tmp_path / 'settings-alias')
    with pytest.raises(ValueError, match='without links'):
        save_settings(library, EngineSettings())


def test_restored_configuration_validates_without_applying_missing_executables():
    settings = EngineSettings(ffmpeg='/a/missing/installed/ffmpeg')
    assert validate_settings_bytes(json.dumps(settings.to_dict()).encode()) == settings


def test_optional_flite_discovery_failure_does_not_disable_other_local_engines(tmp_path, monkeypatch):
    from carr_library.audio import engines
    binary = tmp_path / 'ffmpeg-placeholder'
    binary.write_bytes(b'Not executed')
    monkeypatch.setattr(engines.shutil, 'which', lambda name: None)
    def unavailable(*args):
        raise engines.EngineUnavailable('No libflite in this build')
    monkeypatch.setattr(engines, 'FliteEngine', unavailable)
    registry = engines.default_registry(ffmpeg=binary)
    assert any(row['engine'] == 'flite' and not row['available'] for row in registry.notices)
    assert 'flite' not in registry.engines
