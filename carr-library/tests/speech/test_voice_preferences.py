"""Saved preferences resolve only current, exactly declared local voices."""
from copy import deepcopy
import json

import pytest

from carr_library import runtime_settings as settings
from carr_library.durable_state import state_lock
from carr_library.speech import Voice, VoiceRegistry


def empty():
    return {'favorites': [], 'defaults': {}, 'profiles': {}}


def preferences():
    return {'favorites': ['local:uk'], 'defaults': {'uk': 'local:uk'},
            'profiles': {'Тихе читання': {'voice_id': 'local:uk', 'language': 'uk',
                                         'rate': -2, 'volume': 55}}}


@pytest.fixture
def voices():
    class Provider:
        name = 'Synthetic declaration provider, no acoustic claims'
        def voices(self):
            return [Voice('local:uk', 'Український', 'Fixture', 'uk'),
                    Voice('local:sk', 'Slovenský', 'Fixture', 'sk'),
                    Voice('local:us', 'English US', 'Fixture', 'en_US'),
                    Voice('local:gb', 'English UK', 'Fixture', 'en-GB'),
                    Voice('local:fixed', 'Fixed controls', 'Fixture', 'uk', ())]
    registry = VoiceRegistry([Provider()])
    yield registry.voices()
    registry.close()


def test_legacy_load_does_not_rewrite_bytes_or_probe_optional_engines(tmp_path, monkeypatch):
    path = settings.settings_path(tmp_path)
    path.parent.mkdir(parents=True)
    legacy = {'schema': 1, 'ffmpeg': '/not-installed/ffmpeg', 'ffprobe': None,
              'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    raw = (json.dumps(legacy, indent=3) + '\n').encode()
    path.write_bytes(raw)
    monkeypatch.setattr(settings, 'local_path', lambda *_a, **_k: pytest.fail('Passive preferences probed an engine.'))
    assert settings.load_voice_preferences(tmp_path) == empty()
    assert path.read_bytes() == raw


def test_reload_persists_exact_ids_profiles_and_external_declarations(tmp_path):
    value = settings.load_settings(tmp_path)
    settings.save_settings(tmp_path, value)
    path = settings.settings_path(tmp_path)
    source = json.loads(path.read_text())
    source['ffmpeg'] = '/uninstalled-after-relocation/ffmpeg'
    path.write_text(json.dumps(source))
    selected = preferences()
    saved = settings.save_voice_preferences(tmp_path, selected, expected=empty())
    selected['profiles']['Тихе читання']['volume'] = 99
    assert saved == settings.load_voice_preferences(tmp_path) == preferences()
    restored = json.loads(path.read_text())
    assert restored['ffmpeg'] == source['ffmpeg']
    assert {key: val for key, val in restored.items() if key != 'voice_preferences'} == {
        key: val for key, val in source.items() if key != 'voice_preferences'}


def test_two_windows_refuse_stale_preference_save_without_overwriting(tmp_path):
    first = settings.load_voice_preferences(tmp_path)
    second = settings.load_voice_preferences(tmp_path)
    settings.save_voice_preferences(tmp_path, preferences(), expected=first)
    before = settings.settings_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match='another window'):
        settings.save_voice_preferences(tmp_path, empty(), expected=second)
    assert settings.settings_path(tmp_path).read_bytes() == before


def test_engine_window_cannot_overwrite_a_later_voice_preference_change(tmp_path):
    engine_window = settings.load_settings(tmp_path)
    settings.save_settings(tmp_path, engine_window)
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    before = settings.settings_path(tmp_path).read_bytes()
    with pytest.raises(ValueError, match='Voice preferences changed'):
        settings.save_settings(tmp_path, engine_window)
    assert settings.settings_path(tmp_path).read_bytes() == before
    # An old API caller with no new field changes engines without erasing it.
    engine_window.pop('voice_preferences')
    settings.save_settings(tmp_path, engine_window)
    assert settings.load_voice_preferences(tmp_path) == preferences()


def test_shared_settings_lock_refuses_overlapping_changes(tmp_path):
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    path = settings.settings_path(tmp_path)
    before = path.read_bytes()
    with state_lock(path.parent):
        with pytest.raises(RuntimeError, match='already being changed'):
            settings.save_voice_preferences(tmp_path, empty(), expected=preferences())
    assert path.read_bytes() == before


def test_explicit_voice_beats_default_but_not_exact_language_guard(voices):
    saved = preferences()
    assert settings.resolve_voice_preference(voices, saved, language='uk')['voice_id'] == 'local:uk'
    assert settings.resolve_voice_preference(voices, saved, language='uk', voice_id='local:fixed') == {
        'voice_id': 'local:fixed', 'language': 'uk', 'rate': 0, 'volume': 100}
    with pytest.raises(ValueError, match='exact language'):
        settings.resolve_voice_preference(voices, saved, language='sk', voice_id='local:uk')


def test_profile_controls_are_not_applied_to_a_different_voice_or_language(voices):
    selected = settings.resolve_voice_preference(voices, preferences(), language='uk', profile='Тихе читання')
    assert selected == preferences()['profiles']['Тихе читання']
    with pytest.raises(ValueError, match='different voice'):
        settings.resolve_voice_preference(voices, preferences(), language='uk',
                                          voice_id='local:fixed', profile='Тихе читання')
    with pytest.raises(ValueError, match='different voice'):
        settings.resolve_voice_preference(voices, preferences(), language='sk', profile='Тихе читання')


@pytest.mark.parametrize('language', ['en', 'en-GB', 'en-Latn-US'])
def test_no_region_or_script_fallback_to_similarly_named_voice(voices, language):
    saved = empty()
    saved['defaults'][language] = 'local:us'
    with pytest.raises(ValueError, match='no language fallback'):
        settings.resolve_voice_preference(voices, saved, language=language)


def test_adapter_underscore_tag_and_request_case_use_one_exact_language(voices):
    value = empty()
    value['defaults']['EN-us'] = 'local:us'
    assert settings.resolve_voice_preference(voices, value, language='en-US')['voice_id'] == 'local:us'


def test_removed_voice_and_unsupported_controls_do_not_pick_another_voice(voices):
    saved = preferences()
    with pytest.raises(ValueError, match='no substitute'):
        settings.resolve_voice_preference(voices[1:], saved, language='uk')
    saved['profiles']['Тихе читання']['voice_id'] = 'local:fixed'
    with pytest.raises(ValueError, match='does not support'):
        settings.resolve_voice_preference(voices, saved, language='uk', profile='Тихе читання')
    assert saved['defaults']['uk'] == 'local:uk'


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(secret='not-a-supported-setting'),
    lambda value: value['favorites'].append('local:uk'),
    lambda value: value['favorites'].append('bad\nvoice'),
    lambda value: value['defaults'].update({'en-US': 'a', 'EN-us': 'b'}),
    lambda value: value['defaults'].update({'en_US': 'local:us'}),
    lambda value: value['profiles']['Тихе читання'].update(rate=True),
    lambda value: value['profiles']['Тихе читання'].update(rate=11),
    lambda value: value['profiles']['Тихе читання'].update(volume=-1),
    lambda value: value['profiles']['Тихе читання'].update(command='never execute'),
    lambda value: value['profiles'].update({'тихе ЧИТАННЯ': deepcopy(value['profiles']['Тихе читання'])}),
])
def test_invalid_preferences_keep_prior_bytes_and_do_not_echo_values(tmp_path, mutate):
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    before = settings.settings_path(tmp_path).read_bytes()
    value = preferences()
    mutate(value)
    with pytest.raises(ValueError):
        settings.save_voice_preferences(tmp_path, value, expected=preferences())
    assert settings.settings_path(tmp_path).read_bytes() == before


def test_owner_guard_is_rechecked_after_latest_settings_read_before_commit(tmp_path, monkeypatch):
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    path = settings.settings_path(tmp_path)
    before = path.read_bytes()
    admitted = [True]
    original = settings._read_settings
    def read(root):
        value = original(root)
        admitted[0] = False
        return value
    def guard():
        if not admitted[0]:
            raise InterruptedError('The owning reader changed.')
    monkeypatch.setattr(settings, '_read_settings', read)
    with pytest.raises(InterruptedError, match='owning reader'):
        settings.save_voice_preferences(tmp_path, empty(), expected=preferences(), guard=guard)
    assert path.read_bytes() == before
