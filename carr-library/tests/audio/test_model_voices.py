"""Manifest/real-worker protocol evidence; synthetic fixtures are NOT neural speech."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import model_voices
from carr_library.audio.model_voices import (DordiaiVoicesEngine, EngineUnavailable,
    _DORDIAI_OFFLINE_BOOTSTRAP, _run)
from carr_library.audio.voice_manifest import LocalModelManifest, ModelManifestError, offline_environment
from carr_library.audio.voices import Voice, VoiceRegistry, load_voice_config, supports_language


def manifest(root, *, engine='piper', trusted=True, accepted=True):
    root.mkdir()
    if engine == 'piper':
        assets = {'voice.onnx': b'SYNTHETIC MANIFEST FIXTURE, NOT A REAL MODEL',
                  'voice.onnx.json': b'{"fixture":true}'}
        settings = {'model': 'voice.onnx', 'config': 'voice.onnx.json',
                    'language': 'uk', 'name': 'Synthetic protocol test voice'}
    else:
        assets = {'python/python.exe': b'NONEXECUTABLE PROTOCOL FIXTURE',
                  'nvda_worker.py': b'# synthetic worker placeholder',
                  'models/voices/catalog.json': json.dumps({'voices': {
                      'fixture': {'name': 'Synthetic profile'}, 'disabled': {'name': 'Disabled', 'disabled': True}},
                      'languages': {'uk': 'Ukrainian', 'sk': 'Slovak'}}).encode()}
        settings = {'protocol': 'nvda-worker-jsonl-pcm24k-v1',
                    'profile_languages': {'fixture': {'supported': ['uk'], 'candidates': ['sk']}}}
    for name, data in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    data = {'schema_version': 1, 'engine': engine, 'model_id': 'synthetic-fixture',
            'revision': 'a' * 40, 'license': 'Synthetic fixture only; no model distribution',
            'files': {name: sha256(content).hexdigest() for name, content in assets.items()},
            'settings': settings, 'license_accepted': accepted, 'trusted_weights': trusted,
            'personal_noncommercial': False}
    target = root / 'manifest.json'
    target.write_text(json.dumps(data), encoding='utf-8')
    return target


def config(path):
    return {'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
            'piper': {'executable': str(Path(sys.executable).resolve()), 'manifest': str(path)}}


def test_explicit_config_lists_only_declared_verified_voice_and_fingerprints_assets(tmp_path):
    selected = manifest(tmp_path / 'piper')
    document = tmp_path / 'voice-settings.json'
    document.write_text(json.dumps({'version': 1, 'kind': 'local-voice-engines', 'config': config(selected)}))
    assert load_voice_config(document) == config(selected)
    registry = VoiceRegistry.from_config_file(document)
    voice, = registry.list_voices()
    assert voice.engine == 'piper' and voice.languages == ('uk',)
    assert registry.choose_voice('uk') == voice
    assert registry.voices_for_language('sk') == ()
    with pytest.raises(ValueError, match='No installed'):
        registry.choose_voice('en')
    runtime = registry.runtime_identity()
    assert runtime['model_manifests'][0]['manifest_sha256']
    assert str(tmp_path) not in json.dumps(runtime)
    (selected.parent / 'voice.onnx').write_bytes(b'changed since review')
    with pytest.raises(ModelManifestError, match='Checksum'):
        registry.fingerprint()


def test_optional_dordiai_catalog_is_declared_only_and_requires_trusted_exact_tree(tmp_path):
    selected = manifest(tmp_path / 'dordiai', engine='dordiai-voices')
    registry = VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                              'dordiai_manifest': str(selected)})
    voice, = registry.list_voices()
    assert voice.id == 'dordiai-voices:fixture' and voice.languages == ('uk',)
    assert voice.candidate_languages == ('sk',)
    assert registry.voices_for_language('sk') == ()
    status = next(row for row in registry.engine_status() if row['engine'] == 'dordiai-voices')
    assert status['inference'] == 'not verified by enumeration'
    (selected.parent / 'unreviewed.py').write_text('raise AssertionError("must not run")')
    with pytest.raises(ModelManifestError, match='missing from its reviewed manifest'):
        registry.fingerprint()


def test_dordiai_language_declarations_are_per_profile_and_candidates_never_become_support(tmp_path):
    selected = manifest(tmp_path / 'dordiai', engine='dordiai-voices')
    value = json.loads(selected.read_text())
    catalog_path = selected.parent / 'models/voices/catalog.json'
    catalog = json.loads(catalog_path.read_text())
    catalog['voices'].update(slovak={'name': 'Slovak fixture'}, undeclared={'name': 'Undeclared fixture'})
    data = json.dumps(catalog).encode()
    catalog_path.write_bytes(data)
    value['files']['models/voices/catalog.json'] = sha256(data).hexdigest()
    value['settings']['profile_languages']['slovak'] = {'supported': ['sk'], 'candidates': ['uk']}
    selected.write_text(json.dumps(value))
    registry = VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                              'dordiai_manifest': str(selected)})
    ukrainian, = registry.voices_for_language('uk')
    slovak, = registry.voices_for_language('sk')
    assert ukrainian.id == 'dordiai-voices:fixture' and slovak.id == 'dordiai-voices:slovak'
    assert ukrainian.candidate_languages == ('sk',) and slovak.candidate_languages == ('uk',)
    profiles = next(row for row in registry.engine_status() if row['engine'] == 'dordiai-voices')['profiles']
    unavailable, = [row for row in profiles if row['id'].endswith(':undeclared')]
    assert not unavailable['available'] and unavailable['supported_languages'] == unavailable['candidate_languages'] == []
    with pytest.raises(ValueError, match='does not declare support'):
        registry.synthesize('Candidate is not supported.', tmp_path / 'candidate.wav', voice_id=ukrainian.id, language='sk')
    engine = registry._model_engines['dordiai-voices']
    with pytest.raises(EngineUnavailable, match='candidate languages'):
        engine.synthesize('Direct boundary also rejects.', tmp_path / 'direct.wav', voice=ukrainian.id, language='sk')
    assert not (tmp_path / 'candidate.wav').exists() and not (tmp_path / 'direct.wav').exists()


def test_dordiai_catalogue_global_languages_without_profile_review_produces_no_selectable_voices(tmp_path):
    selected = manifest(tmp_path / 'dordiai', engine='dordiai-voices')
    value = json.loads(selected.read_text())
    del value['settings']['profile_languages']
    selected.write_text(json.dumps(value))
    registry = VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                              'dordiai_manifest': str(selected)})
    assert registry.list_voices() == ()
    status = next(row for row in registry.engine_status() if row['engine'] == 'dordiai-voices')
    assert status['status'] == 'configured' and status['profiles'][0]['supported_languages'] == []


@pytest.mark.parametrize('declaration', [
    {'fixture': {'supported': ['en'], 'candidates': []}},
    {'fixture': {'supported': ['uk'], 'candidates': ['uk']}},
    {'fixture': {'supported': ['uk', 'uk'], 'candidates': []}},
    {'fixture': {'supported': 'uk', 'candidates': []}},
    {'fixture': {'supported': ['uk'], 'candidates': [], 'all_languages': True}},
    {'unknown': {'supported': ['uk'], 'candidates': []}},
])
def test_dordiai_profile_language_review_rejects_undeclared_global_or_ambiguous_support(tmp_path, declaration):
    selected = manifest(tmp_path / 'dordiai', engine='dordiai-voices')
    value = json.loads(selected.read_text())
    value['settings']['profile_languages'] = declaration
    selected.write_text(json.dumps(value))
    with pytest.raises(ModelManifestError):
        DordiaiVoicesEngine(selected).voices()


@pytest.mark.parametrize('trusted,accepted', [(False, True), (True, False)])
def test_unreviewed_dordiai_assets_do_not_advertise_available_voices(tmp_path, trusted, accepted):
    selected = manifest(tmp_path / 'dordiai', engine='dordiai-voices', trusted=trusted, accepted=accepted)
    registry = VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False,
                              'dordiai_manifest': str(selected)})
    assert registry.list_voices() == ()
    assert next(row for row in registry.engine_status() if row['engine'] == 'dordiai-voices')['status'] == 'unavailable'


@pytest.mark.parametrize('bad', [
    {'piper': {'executable': False, 'manifest': '/missing'}},
    {'piper': {'executable': 'relative-program', 'manifest': '/missing'}},
    {'dordiai_manifest': 'relative.json'},
    {'dordiai_manifest': 'https://example.invalid/model.json'},
    {'dordiai_manifest': '//server/private/manifest.json'},
    {'piper': {'executable': '/missing', 'manifest': '/missing', 'download': True}},
])
def test_model_configuration_has_no_implicit_path_or_download_fallback(bad):
    with pytest.raises((ValueError, FileNotFoundError)):
        VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False, **bad})


def test_registry_piper_protocol_preserves_literal_stdin_and_relative_rate(tmp_path, monkeypatch):
    selected = manifest(tmp_path / 'piper')
    registry = VoiceRegistry(config(selected))
    voice, = registry.list_voices()
    observed = []
    def fixture(command, *, data, **kwargs):
        # This fake CLI boundary deliberately writes fixture PCM, not speech.
        observed.append((command, data, kwargs))
        target = Path(command[command.index('--output_file') + 1])
        with wave.open(str(target), 'wb') as writer:
            writer.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            writer.writeframes(b'\x01\x00\xff\xff' * 120)
        return b''
    monkeypatch.setattr(model_voices, '_run', fixture)
    text = 'Український текст; $(not-a-command) : [literal].'
    output = tmp_path / 'fixture.wav'
    result = registry.synthesize(text, output, voice_id=voice.id, language='uk', rate=1.25, volume=.6)
    command, body, options = observed[0]
    assert body == text.encode('utf-8') and text not in ' '.join(map(str, command))
    assert float(command[command.index('--length_scale') + 1]) == pytest.approx(.8)
    assert float(command[command.index('--volume') + 1]) == pytest.approx(.6)
    assert result.frames == 240 and result.engine == 'piper' and result.voice_id == voice.id
    with pytest.raises(FileExistsError):
        registry.synthesize(text, output, voice_id=voice.id, language='uk')
    with pytest.raises(EngineUnavailable, match='bracketed phonemes'):
        registry.synthesize('[[not implicitly authorized phonemes]]', tmp_path / 'phoneme.wav',
                            voice_id=voice.id, language='uk')
    assert len(observed) == 1


def protocol_engine(root):
    engine = object.__new__(DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=root)
    engine.timeout = 5
    return engine


def test_dordiai_real_subprocess_protocol_streams_pcm_and_requests_next(tmp_path):
    worker = tmp_path / 'protocol_fixture.py'
    worker.write_text('''import sys,json,base64
request=json.loads(sys.stdin.readline())
assert request['text']=='Literal protocol fixture'
print(json.dumps({'chunk':base64.b64encode(b'\\x01\\x00'*24).decode()}),flush=True)
assert json.loads(sys.stdin.readline())=={'mode':'next'}
print(json.dumps({'chunk':base64.b64encode(b'\\x02\\x00'*36).decode()}),flush=True)
assert json.loads(sys.stdin.readline())=={'mode':'next'}
print(json.dumps({'finished':True}),flush=True)
''', encoding='utf-8')
    target = tmp_path / 'protocol.wav'
    protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
        {'text': 'Literal protocol fixture'}, target, lambda: False)
    with wave.open(str(target), 'rb') as reader:
        assert reader.getframerate() == 24000 and reader.getnframes() == 60
        assert reader.readframes(60) == b'\x01\x00'*24 + b'\x02\x00'*36


@pytest.mark.parametrize('reply', [{'chunk': 'invalid!'}, {'chunk': 'AA=='}, {'finished': True},
    {'pcm': 'AQI=', 'chunk': 'AQI='}, {'finished': True, 'pcm': 'AQI='}, ['unexpected']])
def test_dordiai_protocol_rejects_invalid_or_empty_pcm(tmp_path, reply):
    worker = tmp_path / 'invalid_fixture.py'
    worker.write_text('import sys;sys.stdin.readline();print(' + repr(json.dumps(reply)) + ',flush=True)\n', encoding='utf-8')
    with pytest.raises((ValueError, EngineUnavailable)):
        protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
            {'text': 'fixture'}, tmp_path / 'rejected.wav', lambda: False)


def test_dordiai_cancellation_stops_waiting_worker(tmp_path):
    worker = tmp_path / 'waiting_fixture.py'
    worker.write_text('import sys,time;sys.stdin.readline();time.sleep(15)', encoding='utf-8')
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
            {'text': 'fixture'}, tmp_path / 'cancelled.wav', lambda: time.monotonic() - began > .15)
    assert time.monotonic() - began < 3


def test_dordiai_bootstrap_denies_python_network_before_external_worker(tmp_path):
    worker = tmp_path / 'network_fixture.py'
    worker.write_text('import socket;socket.create_connection(("127.0.0.1",9))', encoding='utf-8')
    with pytest.raises(EngineUnavailable):
        _run([Path(sys.executable).resolve(), '-I', '-c', _DORDIAI_OFFLINE_BOOTSTRAP, worker])


def test_engine_environment_omits_inherited_secrets_and_proxy_settings(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://private.invalid')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret-never-sent')
    environment = offline_environment()
    assert 'HTTP_PROXY' not in environment and 'OPENAI_API_KEY' not in environment
    assert environment['HF_HUB_OFFLINE'] == '1'


def test_language_selection_uses_declared_locale_or_base_without_arbitrary_fallback():
    registry = VoiceRegistry({'include_sapi': False, 'ffmpeg': False, 'espeak_ng': False})
    english = Voice('fixture:en', 'fixture', 'English', ('en-US',), ('local',))
    generic = Voice('fixture:uk', 'fixture', 'Ukrainian', ('uk',), ('local',))
    exact = Voice('fixture:uk-UA', 'fixture', 'Ukrainian locale', ('uk-UA',), ('local',))
    registry._voices = (english, generic, exact)
    assert registry.choose_voice('uk-UA') == exact
    assert registry.choose_voice('uk') == generic
    assert supports_language(generic, 'uk-UA')
    assert not supports_language(english, 'en-GB')
    with pytest.raises(ValueError, match='No substitute'):
        registry.choose_voice('uk', preferred=english.id)
    with pytest.raises(ValueError, match='No installed'):
        registry.choose_voice('sk')


def test_script_new_roles_use_only_declared_language_defaults_and_keep_explicit_roles():
    from carr_library.audio.projects import parse_script
    english = Voice('fixture:en', 'fixture', 'English', ('en-US',), ('local',))
    ukrainian = Voice('fixture:uk', 'fixture', 'Ukrainian', ('uk',), ('local',))
    result = parse_script('Narrator: Слова.\nFriend: Відповідь.', (english, ukrainian), language='uk')
    assert {role.voice_id for role in result.roles} == {ukrainian.id}
    assert {role.language for role in result.roles} == {'uk'}
    with pytest.raises(ValueError, match='declares the requested language'):
        parse_script('Текст', (english,), language='uk')
    # A saved explicit voice assignment survives editing; availability is
    # separately checked before generation. A string ID asserts no language.
    preserved = parse_script(result.script, (english,), language='sk', previous=result)
    assert preserved.roles == result.roles
    explicit = parse_script('Text', ('explicit-user-selected-id',), language='sk')
    assert explicit.roles[0].voice_id == 'explicit-user-selected-id'
