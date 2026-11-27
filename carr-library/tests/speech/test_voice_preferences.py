"""Actual persisted voice choices, exact language matching and backup recovery."""
from dataclasses import replace
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.speech.runtime import Voice
from carr_library.speech.voice_preferences import (VoicePreferences, VoicePreferenceStore,
    language_key, voice_language, MAX_FAVORITES)


VOICES = (Voice('fixture:uk', 'Українська', 'Synthetic identity', 'uk-UA'),
          Voice('fixture:en', 'English', 'Synthetic identity', 'en-US'),
          Voice('fixture:gb', 'English UK', 'Synthetic identity', 'en-GB'))


def test_language_defaults_are_exact_and_never_guess_base_or_dialect():
    preferences = VoicePreferences().with_default('uk_UA', 'fixture:uk', VOICES)
    assert preferences.preferred('UK-ua', VOICES) == VOICES[0]
    assert preferences.preferred('uk', VOICES) is None
    assert language_key('zh_Hant_TW') == 'zh-hant-tw'
    for language, voice in [('uk', 'fixture:uk'), ('en-US', 'fixture:gb'), ('sk', 'missing')]:
        with pytest.raises(ValueError, match='currently installed'):
            preferences.with_default(language, voice, VOICES)


@pytest.mark.parametrize('language', ['und', '', 'uk language', '../en', 'x', 'en-123456789', 'en\n', 'e' * 9])
def test_invalid_or_unspecified_languages_do_not_become_defaults(language):
    with pytest.raises(ValueError): language_key(language)
    assert voice_language(replace(VOICES[0], language=language or 'und')) is None


@pytest.mark.parametrize('change', ['missing', 'language'])
def test_missing_or_changed_default_never_falls_back_to_another_voice(change):
    preferences = VoicePreferences().with_default('en-US', 'fixture:en', VOICES)
    current = (VOICES[2],) if change == 'missing' else (replace(VOICES[1], language='en-GB'), VOICES[2])
    with pytest.raises(ValueError, match='no fallback'):
        preferences.preferred('en-US', current)
    assert preferences.with_default('en-US', None, current).preferred('en-US', current) is None


def test_favorites_are_explicit_bounded_and_unavailable_entries_can_be_removed():
    preferences = VoicePreferences().with_favorite('fixture:uk', True, VOICES)
    assert preferences.with_favorite('fixture:uk', True, VOICES) == preferences
    assert preferences.with_favorite('fixture:uk', False, ()).favorites == ()
    with pytest.raises(ValueError, match='currently installed'):
        preferences.with_favorite('missing', True, VOICES)
    with pytest.raises(ValueError, match='Too many'):
        VoicePreferences(favorites=tuple('fixture:' + str(index) for index in range(MAX_FAVORITES + 1)))


def test_real_save_reopen_optimistic_conflict_and_separate_libraries(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    first, token = store.load()
    assert first == VoicePreferences() and token is None and not (root / 'state').exists()
    updated = first.with_default('uk-UA', 'fixture:uk', VOICES).with_favorite('fixture:uk', True, VOICES)
    saved, new_token = store.save(updated, expected_token=token)
    assert VoicePreferenceStore(root).load() == (saved, new_token)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='Another window'):
        store.save(first, expected_token=token)
    assert store.path.read_bytes() == before
    other = tmp_path / 'other'; other.mkdir()
    assert VoicePreferenceStore(other).load() == (VoicePreferences(), None)


@pytest.mark.parametrize('kind', ['unknown_schema', 'unknown_key', 'duplicate_key', 'bad_language', 'duplicate_favorite'])
def test_malformed_saved_preferences_are_preserved_instead_of_reset(tmp_path, kind):
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root); store.path.parent.mkdir(parents=True)
    value = VoicePreferences().to_dict()
    if kind == 'unknown_schema': value['schema'] = 'future-schema'
    elif kind == 'unknown_key': value['execute'] = 'not executable authority'
    elif kind == 'bad_language': value['defaults'] = {'EN_us': 'fixture:en'}
    elif kind == 'duplicate_favorite': value['favorites'] = ['fixture:en'] * 2
    raw = json.dumps(value).encode()
    if kind == 'duplicate_key': raw = b'{"schema":"carr-voice-preferences/1","schema":"carr-voice-preferences/1","defaults":{},"favorites":[]}'
    store.path.write_bytes(raw)
    with pytest.raises(ValueError): store.load()
    with pytest.raises(ValueError): store.save(VoicePreferences(), expected_token=None)
    assert store.path.read_bytes() == raw


def test_changed_library_directory_is_not_an_approved_write_target(tmp_path):
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    root.rename(tmp_path / 'old-library'); root.mkdir()
    with pytest.raises(ValueError, match='directory changed'):
        store.save(VoicePreferences(), expected_token=None)
    assert not list(root.iterdir())


def test_actual_backup_delete_restore_keeps_choices_without_engine_activation(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'бібліотека')
    app.create_workspace('Voice preferences recovery')
    store = VoicePreferenceStore(app.root)
    preferences = VoicePreferences().with_default('uk-UA', 'fixture:uk', VOICES).with_favorite('fixture:en', True, VOICES)
    _, token = store.save(preferences, expected_token=None)
    backup = tmp_path / 'backup'; checksum = app.backup(backup)
    shutil.rmtree(app.root)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, expected_manifest_sha256=checksum)
    from carr_library.speech.runtime import VoiceRegistry
    monkeypatch.setattr(VoiceRegistry, 'discover', lambda **kwargs: (_ for _ in ()).throw(AssertionError('No engine activation')))
    recovered, recovered_token = VoicePreferenceStore(restored).load()
    assert recovered == preferences and recovered_token == token
    with pytest.raises(ValueError, match='unavailable'):
        recovered.preferred('uk-UA', ())
    assert recovered.preferred('uk-UA', VOICES) == VOICES[0]


def test_installed_voice_input_is_consumed_only_within_its_bound():
    from carr_library.speech.voice_preferences import installed_voices
    consumed = []
    def unbounded():
        index = 0
        while True:
            consumed.append(index)
            yield replace(VOICES[0], id='fixture:' + str(index))
            index += 1
    with pytest.raises(ValueError, match='bounded'):
        installed_voices(unbounded())
    assert consumed == list(range(10001))


def test_backup_value_validation_is_passive_and_keeps_unknown_identities(monkeypatch):
    from carr_library.speech import voice_preferences as module
    value = VoicePreferences((('uk-ua', 'not-currently-installed'),), ('another-absent-voice',)).to_dict()
    def forbidden(*args, **kwargs):
        raise AssertionError('Passive preferences must not open files or select an engine.')
    for name in ('local_path', 'regular', 'read_json', 'atomic_json', 'state_lock'):
        monkeypatch.setattr(module, name, forbidden)
    assert module.validate_voice_preferences(value) == value
    with pytest.raises(ValueError, match='Unsupported'):
        module.validate_voice_preferences({**value, 'provider_path': '/not-an-authority'})


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_acknowledged_save_survives_late_lock_exit_failure_with_checked_notice(tmp_path, monkeypatch, failure):
    from contextlib import contextmanager
    from carr_library.speech import voice_preferences as module
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    selected = VoicePreferences().with_default('uk-UA', VOICES[0].id, VOICES)
    actual_lock = module.state_lock
    @contextmanager
    def interrupted_exit(*args, **kwargs):
        with actual_lock(*args, **kwargs):
            yield
        raise failure('injected after atomic publication and native lock release')
    monkeypatch.setattr(module, 'state_lock', interrupted_exit)
    result = store.save(selected, expected_token=None)
    assert result == VoicePreferenceStore(root).load()
    assert result[0] == selected
    assert 'saved and checked' in store.warning and 'state lock' in store.warning
    monkeypatch.setattr(module, 'state_lock', actual_lock)
    store.save(VoicePreferences(), expected_token=result[1])
    assert store.warning == ''


def test_late_lock_failure_cannot_adopt_a_foreign_saved_value(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from carr_library.speech import voice_preferences as module
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    selected = VoicePreferences().with_default('uk-UA', VOICES[0].id, VOICES)
    foreign = VoicePreferences().with_favorite(VOICES[1].id, True, VOICES)
    actual_lock = module.state_lock
    @contextmanager
    def changed_after_exit(*args, **kwargs):
        with actual_lock(*args, **kwargs):
            yield
        module.atomic_json(store.path, foreign.to_dict(), replace=True)
        raise OSError('actual committed value changed before completion could be checked')
    monkeypatch.setattr(module, 'state_lock', changed_after_exit)
    with pytest.raises(OSError, match='actual committed value changed'):
        store.save(selected, expected_token=None)
    assert VoicePreferenceStore(root).load()[0] == foreign
    assert not store.warning


@pytest.mark.parametrize('boundary', ['directory-sync', 'equal-state-before-write'])
def test_writer_error_reports_exact_present_state_without_claiming_own_publication(tmp_path, monkeypatch, boundary):
    from carr_library import durable_state
    from carr_library.speech import voice_preferences as module
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    selected = VoicePreferences().with_default('uk-UA', VOICES[0].id, VOICES)
    if boundary == 'directory-sync':
        def interrupted_sync(*args, **kwargs):
            raise OSError('actual writer reached publication before directory durability failed')
        monkeypatch.setattr(durable_state, 'sync_directory', interrupted_sync)
    else:
        actual_writer = module.atomic_json
        def foreign_equal_before_write(path, value, **kwargs):
            # A competing actor writes equal bytes before this attempted writer
            # fails. Their equality must never become our publication authority.
            actual_writer(path, value, **kwargs)
            raise OSError('selected writer never acknowledged its own write')
        monkeypatch.setattr(module, 'atomic_json', foreign_equal_before_write)
    actual, token = store.save(selected, expected_token=None)
    assert (actual, token) == VoicePreferenceStore(root).load()
    assert actual == selected
    assert 'present and checked' in store.warning
    assert 'completion could not be confirmed' in store.warning
    assert 'were saved' not in store.warning


def test_failed_writer_without_exact_present_value_still_raises(tmp_path, monkeypatch):
    from carr_library.speech import voice_preferences as module
    root = tmp_path / 'library'; root.mkdir()
    store = VoicePreferenceStore(root)
    def failed(*args, **kwargs):
        raise PermissionError('destination publication was refused')
    monkeypatch.setattr(module, 'atomic_json', failed)
    with pytest.raises(PermissionError, match='publication was refused'):
        store.save(VoicePreferences().with_favorite(VOICES[0].id, True, VOICES), expected_token=None)
    assert not store.path.exists() and not store.warning
