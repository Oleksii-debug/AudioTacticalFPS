"""Saved preferences resolve only current, exactly declared local voices."""
from copy import deepcopy
from threading import Event
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
    legacy = {'schema': 1, 'ffmpeg': r'C:\not-installed\ffmpeg.exe', 'ffprobe': None,
              'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
    raw = (json.dumps(legacy, indent=3) + '\n').encode()
    path.write_bytes(raw)
    monkeypatch.setattr(settings, 'voice_registry', lambda *_a, **_k: pytest.fail('Passive preferences probed an engine.'))
    monkeypatch.setattr(settings, 'verify_configured_audio_runtime', lambda *_a, **_k: pytest.fail('Passive preferences inspected assets.'))
    assert settings.load_voice_preferences(tmp_path) == empty()
    assert path.read_bytes() == raw


def test_reload_persists_exact_ids_profiles_and_external_declarations(tmp_path):
    value = settings.load_settings(tmp_path)
    settings.save_settings(tmp_path, value)
    path = settings.settings_path(tmp_path)
    source = json.loads(path.read_text())
    source['ffmpeg'] = r'C:\uninstalled-after-relocation\ffmpeg.exe'
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
    engine_window = {**settings.load_settings(tmp_path), 'voice_preferences': empty()}
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
    with state_lock(path.parent, 'engines'):
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


class Provider:
    """Real registry provider with explicit synthetic metadata, never inference."""
    name = 'Preference metadata fixture'
    def __init__(self, values, identity='a' * 64):
        self.values, self.identity, self.closed, self.enumerated = list(values), identity, 0, 0
        self.on_fingerprint = lambda: None

    def voices(self):
        self.enumerated += 1
        return tuple(self.values)

    def fingerprint(self, _identity):
        self.on_fingerprint()
        return self.identity

    def close(self):
        self.closed += 1


def registry_pair(voices, *, identity='a' * 64):
    borrowed_provider, fresh_provider = Provider(voices), Provider(voices, identity)
    borrowed = VoiceRegistry([borrowed_provider])
    def factory():
        return VoiceRegistry([fresh_provider])
    return borrowed, borrowed_provider, fresh_provider, factory


def test_preferences_never_create_an_active_voice_registration(tmp_path):
    from carr_library.audio.voice_settings import VoiceSettingsStore
    root = tmp_path / 'library'
    settings.save_voice_preferences(root / 'state', preferences(), expected=empty())
    assert not VoiceSettingsStore(root).path.exists()
    assert settings.load_voice_preferences(root / 'state') == preferences()
    assert settings.load_settings(root / 'state')['ffmpeg'] is None


def sealed_settings():
    return {'schema': 1, 'ffmpeg': r'C:\reviewed-audio\bin\ffmpeg.exe',
            'ffprobe': r'C:\reviewed-audio\bin\ffprobe.exe', 'piper': [],
            'voice_helpers': [], 'music_models': [], 'fluidsynth': None,
            'audio_runtime_manifest_sha256': 'b' * 64,
            'restore_requires_revalidation': True}


def test_passive_save_preserves_restored_sealed_settings_without_asset_access(tmp_path, monkeypatch):
    from carr_library.durable_state import atomic_json
    path = settings.settings_path(tmp_path)
    original = sealed_settings()
    atomic_json(path, original)
    monkeypatch.setattr(settings, 'verify_configured_audio_runtime',
                        lambda *_a, **_k: pytest.fail('Preference save inspected an engine.'))
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    actual = settings.load_settings(tmp_path)
    assert {key: value for key, value in actual.items() if key != 'voice_preferences'} == original
    assert settings.load_voice_preferences(tmp_path) == preferences()


def test_restored_preferences_do_not_authorize_discovery_or_selection(tmp_path, voices):
    from carr_library.durable_state import atomic_json
    atomic_json(settings.settings_path(tmp_path), {**sealed_settings(), 'voice_preferences': preferences()})
    borrowed = VoiceRegistry([Provider(voices)])
    try:
        with pytest.raises(ValueError, match='explicit revalidation'):
            settings.review_voice_preference(tmp_path, borrowed, preferences(), language='uk',
                registry_factory=lambda: pytest.fail('A restored preference activated an engine.'))
    finally:
        borrowed.close()


@pytest.mark.parametrize('reason', ['cancel', 'scope'])
def test_last_precommit_guard_preserves_previous_bytes(tmp_path, reason):
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    path, calls = settings.settings_path(tmp_path), []
    before = path.read_bytes()
    stop = Event()
    def guard():
        calls.append(None)
        if len(calls) == 2:
            if reason == 'scope':
                raise ValueError('The active library changed.')
            stop.set()
    with pytest.raises((InterruptedError, ValueError)):
        settings.save_voice_preferences(tmp_path, empty(), expected=preferences(),
                                        cancelled=stop.is_set, guard=guard)
    assert path.read_bytes() == before and len(calls) == 2


def test_late_cancel_does_not_undo_a_committed_preference_save(tmp_path, monkeypatch):
    stop, publish = Event(), settings.atomic_json
    def committed(*args, **kwargs):
        result = publish(*args, **kwargs)
        stop.set()
        return result
    monkeypatch.setattr(settings, 'atomic_json', committed)
    assert settings.save_voice_preferences(tmp_path, preferences(), expected=empty(),
                                           cancelled=stop.is_set) == preferences()
    assert stop.is_set() and settings.load_voice_preferences(tmp_path) == preferences()


def test_review_enumerates_fresh_inventory_and_closes_only_its_owner(tmp_path, voices):
    borrowed, old, fresh, factory = registry_pair(voices)
    try:
        selected = settings.review_voice_preference(tmp_path, borrowed, preferences(),
            language='uk', profile='Тихе читання', registry_factory=factory)
        assert selected == preferences()['profiles']['Тихе читання']
        assert fresh.enumerated == 1 and fresh.closed == 1 and old.closed == 0
        assert borrowed.get('local:uk').id == 'local:uk'
        assert not settings.settings_path(tmp_path).exists()
    finally:
        borrowed.close()
    assert old.closed == 1 and fresh.closed == 1


@pytest.mark.parametrize('change', ['removed', 'identity', 'language', 'controls'])
def test_fresh_inventory_change_refuses_selection_without_substitution(tmp_path, voices, change):
    borrowed, old, fresh, factory = registry_pair(voices)
    if change == 'removed':
        fresh.values = list(voices[1:])
    elif change == 'identity':
        fresh.identity = 'b' * 64
    elif change == 'language':
        fresh.values[0] = Voice('local:uk', 'Changed language', 'Fixture', 'sk')
    else:
        fresh.values[0] = Voice('local:uk', 'Controls removed', 'Fixture', 'uk', ())
    try:
        with pytest.raises(ValueError):
            settings.review_voice_preference(tmp_path, borrowed, preferences(), language='uk',
                profile='Тихе читання', registry_factory=factory)
        assert fresh.closed == 1 and old.closed == 0
        assert not settings.settings_path(tmp_path).exists()
    finally:
        borrowed.close()


@pytest.mark.parametrize('boundary', ['before', 'inventory', 'fingerprint'])
def test_cancelled_review_retains_borrowed_registry(tmp_path, voices, boundary):
    borrowed, old, fresh, factory = registry_pair(voices)
    stop = Event()
    if boundary == 'before':
        stop.set()
    elif boundary == 'fingerprint':
        fresh.on_fingerprint = stop.set
    def discover():
        value = factory()
        if boundary == 'inventory':
            stop.set()
        return value
    try:
        with pytest.raises(InterruptedError):
            settings.review_voice_preference(tmp_path, borrowed, preferences(), language='uk',
                registry_factory=discover, cancelled=stop.is_set)
        assert old.closed == 0 and borrowed.voices() == voices
        assert fresh.closed == (0 if boundary == 'before' else 1)
    finally:
        borrowed.close()


def test_changed_settings_during_inventory_are_not_applied(tmp_path, voices):
    borrowed, old, fresh, factory = registry_pair(voices)
    def discover():
        settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
        return factory()
    try:
        with pytest.raises(ValueError, match='settings changed'):
            settings.review_voice_preference(tmp_path, borrowed, preferences(), language='uk',
                                             registry_factory=discover)
        assert fresh.closed == 1 and old.closed == 0
        assert settings.load_voice_preferences(tmp_path) == preferences()
    finally:
        borrowed.close()


def test_review_refuses_to_take_ownership_of_the_borrowed_registry(tmp_path, voices):
    borrowed, old, fresh, _factory = registry_pair(voices)
    try:
        with pytest.raises(ValueError, match='independently owned'):
            settings.review_voice_preference(tmp_path, borrowed, preferences(), language='uk',
                                             registry_factory=lambda: borrowed)
        assert old.closed == 0 and fresh.closed == 0 and borrowed.voices() == voices
    finally:
        borrowed.close()



def test_unicode_favorites_respect_existing_encoded_json_ceiling(tmp_path):
    settings.save_voice_preferences(tmp_path, preferences(), expected=empty())
    path = settings.settings_path(tmp_path)
    before = path.read_bytes()
    huge = empty()
    huge['favorites'] = ['😀' * 2000 + str(index) for index in range(5000)]
    assert len(huge['favorites']) < 10000
    assert all(len(value) <= 2048 for value in huge['favorites'])
    with pytest.raises(ValueError, match='JSON byte budget'):
        settings.save_voice_preferences(tmp_path, huge, expected=preferences())
    assert path.read_bytes() == before


def test_complete_engine_candidate_shares_the_same_aggregate_budget(tmp_path, monkeypatch):
    settings.save_voice_preferences(tmp_path, empty(), expected=empty())
    before = settings.settings_path(tmp_path).read_bytes()
    value = empty()
    value['favorites'] = ['x' * 200]
    encoded_preferences = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    # A smaller configured test ceiling distinguishes the preferences value from
    # the complete existing engine document without allocating another huge blob.
    monkeypatch.setattr(settings, 'MAX_JSON_BYTES', len(encoded_preferences))
    assert settings.validate_voice_preferences(value) == value
    with pytest.raises(ValueError, match='JSON byte budget'):
        settings.save_voice_preferences(tmp_path, value, expected=empty())
    assert settings.settings_path(tmp_path).read_bytes() == before
