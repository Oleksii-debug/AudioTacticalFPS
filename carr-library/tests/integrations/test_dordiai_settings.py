"""Actual persisted configuration and shared-registry lifecycle, using synthetic PCM."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from carr_library import runtime_settings as settings
from carr_library.integrations import dordiai_speech
from carr_library.speech import SpeechSettings, VoiceRegistry

# Fixture reuse must also work under the installed gate's importlib mode.
_voice_spec = importlib.util.spec_from_file_location('dordiai_voice_fixtures_for_settings',
    Path(__file__).with_name('test_dordiai_voices.py'))
_voice_fixtures = importlib.util.module_from_spec(_voice_spec)
_voice_spec.loader.exec_module(_voice_fixtures)
ACTOR, runtime = _voice_fixtures.ACTOR, _voice_fixtures.runtime


def configured(runtime):
    return {'enabled': True, 'runtime_directory': str(runtime), 'python_executable': str(Path(sys.executable).resolve()),
            'expected_worker_sha256': sha256(ACTOR.encode()).hexdigest()}


def no_platform_voices(monkeypatch):
    monkeypatch.setattr(VoiceRegistry, 'discover', classmethod(lambda cls, **_: cls()))


def test_legacy_settings_are_normalized_without_rewriting_or_mutating_input(tmp_path):
    state = tmp_path / 'state'
    path = settings.settings_path(state)
    legacy = settings.load_settings(state)
    legacy.pop('dordiai')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding='utf8')
    before = path.read_bytes()
    assert settings.load_settings(state)['dordiai'] is None
    assert path.read_bytes() == before
    settings.save_settings(state, legacy)
    assert 'dordiai' not in legacy
    assert settings.load_settings(state)['dordiai'] is None


@pytest.mark.parametrize('declaration', [None, {'enabled': False},
                                      {'enabled': False, 'runtime_directory': '/missing/private/installation'}])
def test_disabled_saved_engine_never_probes_private_paths(tmp_path, monkeypatch, declaration):
    if isinstance(declaration, dict) and 'runtime_directory' in declaration:
        # The disabled path is absent but genuinely absolute on this platform.
        # A POSIX-looking literal is drive-relative on Windows.
        absent = tmp_path / 'missing' / 'private' / 'installation'
        assert not absent.exists()
        declaration = {**declaration, 'runtime_directory': str(absent)}
    no_platform_voices(monkeypatch)
    monkeypatch.setattr(dordiai_speech, 'DordiaiSpeechProvider',
                        lambda *_a, **_k: pytest.fail('Disabled configuration launched a provider.'))
    value = settings.load_settings(tmp_path)
    value['dordiai'] = declaration
    settings.save_settings(tmp_path, value)
    assert settings.load_settings(tmp_path)['dordiai'] == declaration
    registry = settings.voice_registry(tmp_path)
    assert registry.voices() == ()
    registry.close()


@pytest.mark.parametrize('invalid', [True, {}, {'enabled': 1}, {'enabled': True},
    {'enabled': True, 'runtime_directory': '//network/share'},
    {'enabled': True, 'runtime_directory': '\\\\host\\share'},
    {'enabled': True, 'runtime_directory': 'relative/runtime'},
    {'enabled': True, 'runtime_directory': 'https://example.invalid/runtime'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'expected_worker_sha256': 'unreviewed'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'expected_worker_sha256': None},
    {'enabled': False, 'remote_url': 'https://invalid.example'},
    {'enabled': True, 'runtime_directory': '/tmp/local', 'python_executable': ''}])
def test_bad_declaration_preserves_last_saved_configuration(tmp_path, invalid):
    value = settings.load_settings(tmp_path)
    settings.save_settings(tmp_path, value)
    before = settings.settings_path(tmp_path).read_bytes()
    value['dordiai'] = invalid
    with pytest.raises(ValueError):
        settings.save_settings(tmp_path, value)
    assert settings.settings_path(tmp_path).read_bytes() == before


def test_saved_engine_reaches_actual_shared_render_and_releases_worker(tmp_path, runtime, monkeypatch):
    no_platform_voices(monkeypatch)
    state = tmp_path / 'library-state'
    value = settings.load_settings(state)
    value['dordiai'] = configured(runtime)
    settings.save_settings(state, value)
    registry = settings.voice_registry(state)
    provider = registry._providers[0]
    try:
        voice = next(item for item in registry.voices() if item.language == 'uk')
        rendered = registry.synthesize('Saved configuration fixture.', voice.id, tmp_path / 'actual.wav',
                                       settings=SpeechSettings(volume=65, rate=-1, pitch=54))
        assert (rendered.sample_rate, rendered.frames, rendered.channels) == (24000, 6, 1)
        process = provider.adapter._worker.process
        assert process.poll() is None
        request = json.loads((runtime / 'requests.jsonl').read_text().splitlines()[0])
        assert request['controls'] == {'volume': 65, 'pitch': 54, 'timbre': 50}
        assert request['speed'] == 2 ** (-1 / 10)
        assert settings.load_settings(state)['dordiai'] == configured(runtime)
    finally:
        registry.close()
    assert process.poll() is not None
    assert provider.adapter._closed
    assert not registry.voices()
    registry.close()  # Idempotent cleanup of a closed dialog.


def test_failed_later_provider_releases_already_registered_private_engine(tmp_path, runtime, monkeypatch):
    no_platform_voices(monkeypatch)
    owned = []
    original = dordiai_speech.DordiaiSpeechProvider
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        owned.append(value)
        return value
    monkeypatch.setattr(dordiai_speech, 'DordiaiSpeechProvider', capture)
    value = settings.load_settings(tmp_path)
    value['dordiai'] = configured(runtime)
    marker = tmp_path / 'synthetic-piper-runtime'
    marker.write_bytes(b'Synthetic local path marker; never executed.')
    value['piper'] = [{'executable': str(marker), 'models': {}}]
    settings.save_settings(tmp_path, value)
    # Distinguish a broken later configuration from a silent successful registry.
    from carr_library import speech
    def fail(*_args, **_kwargs):
        raise ValueError('Synthetic later-provider configuration failure.')
    monkeypatch.setattr(speech, 'PiperProvider', fail)
    with pytest.raises(ValueError, match='later-provider'):
        settings.voice_registry(tmp_path)
    assert len(owned) == 1 and owned[0].adapter._closed


def test_registry_closes_remaining_workers_after_one_cleanup_error():
    calls = []
    class Provider:
        def __init__(self, name):
            self.name = name
        def voices(self):
            return ()
        def close(self):
            calls.append(self.name)
            if self.name == 'first':
                raise OSError('Synthetic close failure.')
    registry = VoiceRegistry([Provider('first'), Provider('second')])
    registry.close()
    assert calls == ['first', 'second']
    assert len(registry.notices) == 1 and 'first' in registry.notices[0]
    assert [item.name for item in registry._providers] == ['first']
    registry._providers[0].name = 'recovered-first'
    registry.close()
    assert calls == ['first', 'second', 'recovered-first']
    assert registry._providers == [] and registry._cleanup_registered is False
