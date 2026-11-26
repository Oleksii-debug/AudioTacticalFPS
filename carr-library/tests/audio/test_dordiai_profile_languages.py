"""Per-profile declarations never inherit global language resources."""
from hashlib import sha256
import json

import pytest

from carr_library.audio.engines import DordiaiVoicesEngine, EngineUnavailable, VoiceRegistry
from carr_library.audio.model_manifest import ModelManifestError


def installed(tmp_path, profiles, *, resources=('en', 'uk', 'sk', 'en-US'), declarations=None):
    root = tmp_path / 'runtime'
    (root / 'python').mkdir(parents=True)
    (root / 'models' / 'voices').mkdir(parents=True)
    (root / 'python' / 'python.exe').write_bytes(b'Synthetic file; never executed.')
    (root / 'nvda_worker.py').write_text('raise AssertionError("This fixture must not execute")\n')
    (root / 'models' / 'voices' / 'catalog.json').write_text(json.dumps(
        {'voices': profiles, 'languages': {code: {} for code in resources}}), encoding='utf-8')
    settings = {'protocol': 'nvda-worker-jsonl-pcm24k-v1'}
    if declarations is not None:
        settings['profile_languages'] = declarations
    payload = dict(schema_version=1, engine='dordiai-voices', model_id='synthetic-contract',
        revision='a' * 40, license='Synthetic fixture', license_accepted=True,
        files={p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
               for p in root.rglob('*') if p.is_file()}, settings=settings)
    path = root / 'installation.json'
    path.write_text(json.dumps(payload), encoding='utf-8')
    return DordiaiVoicesEngine(path)


def test_explicit_profile_languages_do_not_expand_to_global_catalogue(tmp_path, monkeypatch):
    engine = installed(tmp_path, {'uk_only': {'name': 'Synthetic Ukrainian', 'languages': ['uk']},
                                  'english': {'name': 'Synthetic English', 'languages': ['en']}})
    assert {voice.id: voice.languages for voice in engine.voices()} == {
        'dordiai-voices:uk_only': ('uk',), 'dordiai-voices:english': ('en',)}
    started = []
    monkeypatch.setattr(engine, '_stream', lambda *args: started.append(args))
    with pytest.raises(EngineUnavailable, match='exact language'):
        engine.synthesize('Not a supported request.', tmp_path / 'output.wav',
                          voice='dordiai-voices:uk_only', language='en')
    assert not started and not (tmp_path / 'output.wav').exists()
    assert engine.cache_identity('dordiai-voices:uk_only')['languages'] == ['uk']


@pytest.mark.parametrize('declaration', [None, [], 'uk', ['uk', 'UK'], ['unknown'], [True], ['sk']])
def test_unknown_empty_malformed_or_missing_resource_profile_is_unavailable(tmp_path, declaration):
    profile = {'name': 'Synthetic profile'}
    if declaration is not None:
        profile['languages'] = declaration
    engine = installed(tmp_path, {'voice': profile}, resources=('en', 'uk'))
    voice, = engine.voices()
    assert not voice.available and voice.languages == () and voice.reason
    registry = VoiceRegistry([engine])
    report, = registry.capabilities()
    assert report['available'] is False and report['voices'] == 0 and report['reason']
    with pytest.raises(EngineUnavailable):
        registry.cache_identity(voice.id)
    with pytest.raises(EngineUnavailable):
        registry.synthesize('Synthetic request.', tmp_path / 'blocked.wav', voice=voice.id, language='en')
    assert not (tmp_path / 'blocked.wav').exists()


def test_manifest_can_explicitly_project_reviewed_package_declarations_for_legacy_catalog(tmp_path):
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic legacy entry'}},
                       declarations={'voice': ['UK', 'en_US']})
    voice, = engine.voices()
    assert voice.available and voice.languages == ('uk', 'en-us')
    assert engine.cache_identity(voice.id)['language_contract'] == 'explicit-profile-languages-v1'


def test_global_nonlanguage_mode_never_expands_voice_language_support(tmp_path):
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic', 'languages': ['uk']}},
                       resources=('uk', 'mixed', 'automatic'))
    voice, = engine.voices()
    assert voice.available and voice.languages == ('uk',)


def test_conflicting_profile_and_manifest_declarations_remain_unavailable(tmp_path):
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic', 'languages': ['uk']}},
                       declarations={'voice': ['en']})
    voice, = engine.voices()
    assert not voice.available and 'disagree' in voice.reason


def test_declaration_cannot_create_a_voice_absent_from_the_installed_catalogue(tmp_path):
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic'}}, declarations={'invented': ['en']})
    with pytest.raises(ModelManifestError, match='installed'):
        engine.voices()


@pytest.mark.parametrize('requested', ['en', 'en-GB', 'uk'])
def test_region_specific_declaration_does_not_allow_base_or_nearby_language(tmp_path, requested):
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic region', 'languages': ['en-US']}})
    engine.voices()
    with pytest.raises(EngineUnavailable, match='No substitute'):
        engine.synthesize('Synthetic request.', tmp_path / 'not-produced.wav',
                          voice='dordiai-voices:voice', language=requested)


def test_qualified_language_reaches_worker_without_base_language_rewriting(tmp_path, monkeypatch):
    import wave
    engine = installed(tmp_path, {'voice': {'name': 'Synthetic region', 'languages': ['en-US']}})
    requests = []
    def stream(command, request, output, cancelled):
        requests.append(request)
        with wave.open(str(output), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b'\x01\x00' * 24)
    monkeypatch.setattr(engine, '_stream', stream)
    engine.synthesize('Synthetic contract check.', tmp_path / 'fixture.wav',
                      voice='dordiai-voices:voice', language='en_US')
    assert requests[0]['language'] == 'en-us'


def test_unavailable_profile_does_not_make_engine_available_when_all_others_disabled(tmp_path):
    engine = installed(tmp_path, {'legacy': {'name': 'Legacy without a declaration'},
        'disabled': {'name': 'Disabled', 'disabled': True, 'languages': ['en']}})
    rows = engine.voices()
    assert len(rows) == 1 and not rows[0].available
    assert VoiceRegistry([engine]).capabilities()[0]['available'] is False


def test_studio_excludes_unavailable_profile_and_does_not_select_a_replacement(tmp_path):
    from types import SimpleNamespace
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController
    engine = installed(tmp_path, {'unknown_first': {'name': 'Unqualified first profile'},
        'declared': {'name': 'Explicit declaration', 'languages': ['uk']}})
    registry = VoiceRegistry([engine])
    controller = StudioController(private_root=tmp_path / 'private', registry=registry)
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.controller = controller
    dialog.voice_rows = []
    labels, selections = [], []
    dialog.voice = SimpleNamespace(SetItems=labels.extend, SetSelection=selections.append)
    dialog.wx = SimpleNamespace(NOT_FOUND=-1)
    dialog._message = lambda *args, **kwargs: None
    dialog._run = lambda label, action, done: done(action())
    dialog.reload_voices()
    assert [row['id'] for row in dialog.voice_rows] == ['dordiai-voices:declared']
    assert len(labels) == 1 and 'Explicit declaration' in labels[0]
    assert selections == [-1] and controller.voice == 'sapi5:default'
    assert registry.capabilities()[0]['voices'] == 1
