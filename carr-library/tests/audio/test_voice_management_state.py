"""Current settings CAS, passive restore, and exact source authority regressions."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy

import pytest

from carr_library.durable_state import atomic_json
from carr_library.runtime_settings import load_settings, save_settings, save_voice_preferences, load_voice_preferences
from carr_library.audio.voice_settings import (VoiceSettingsStore, VoiceManagementCommitted,
    validate_management_binding, verify_voice_settings_bindings)
from carr_library.audio.voice_manifest import LocalModelManifest, held_file
from carr_library.audio.voice_restore import prepare_restored_state

_fixture = runpy.run_path(str(Path(__file__).with_name('voice_management_fixtures.py')))
installed = _fixture['installed']
configure = _fixture['configure']


def inert_settings(tmp_path, *, managed=False):
    state = tmp_path / 'library/state'
    state.mkdir(parents=True)
    value = load_settings(state)
    runtime = tmp_path / 'not-installed-runtime'
    row = {'enabled': True, 'runtime_directory': str(runtime)}
    if managed:
        refs = runtime / 'models/voices'
        row['management'] = {'schema': 1, 'revision': 4,
            'manifest': str(runtime / 'manifest.json'), 'sha256': 'a' * 64, 'restored': False,
            'ownership': {'schema': 'carr-voice-ownership/1', **{
                name: {'path': str(path), 'device': 7, 'inode': index}
                for index, (name, path) in enumerate([
                    ('runtime', runtime), ('references', refs),
                    ('packages', refs / 'packages'), ('lock', refs / 'packages/.voice-packages.lock')], 1)}}}
    value['dordiai'] = row
    path = state / 'audio/engines.json'
    path.parent.mkdir()
    atomic_json(path, value, replace=False)
    return state, path, value


@pytest.mark.parametrize('replacement', [None, {'enabled': False}, {'enabled': True, 'runtime_directory': '/changed-runtime'}])
def test_explicit_engine_cas_refuses_stale_unmanaged_parent_before_any_write(tmp_path, replacement):
    state, path, previous = inert_settings(tmp_path)
    concurrent = deepcopy(previous)
    concurrent['dordiai'] = replacement
    save_settings(state, concurrent)
    actual = path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        save_settings(state, previous, expected_dordiai=deepcopy(previous['dordiai']))
    assert path.read_bytes() == actual


def test_explicit_engine_cas_also_refuses_a_management_binding_added_later(tmp_path):
    state, path, previous = inert_settings(tmp_path)
    stale = deepcopy(previous)
    _, _, bound = inert_settings(tmp_path / 'second', managed=True)
    current = deepcopy(previous)
    current['dordiai']['management'] = bound['dordiai']['management']
    atomic_json(path, current, replace=True)
    actual = path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        save_settings(state, stale, expected_dordiai=stale['dordiai'])
    assert path.read_bytes() == actual


@pytest.mark.parametrize('action', ['reset', 'disconnect', 'switch-runtime'])
def test_generic_engine_changes_preserve_dormant_binding_instead_of_erasing_revocation(tmp_path, action):
    state, path, previous = inert_settings(tmp_path, managed=True)
    proposed = deepcopy(previous)
    if action == 'reset':
        proposed['dordiai'] = None
    elif action == 'disconnect':
        proposed['dordiai']['enabled'] = False
    else:
        proposed['dordiai'] = {'enabled': True, 'runtime_directory': str(tmp_path / 'other-runtime')}
    save_settings(state, proposed, expected_dordiai=deepcopy(previous['dordiai']))
    current = load_settings(state)['dordiai']
    expected = {**previous['dordiai']['management'], 'restored': True}
    assert current['management'] == expected
    assert action == 'switch-runtime' or current['enabled'] is False


def test_unreviewed_generic_settings_cannot_create_or_edit_managed_authority(tmp_path):
    state, path, previous = inert_settings(tmp_path, managed=True)
    proposed = deepcopy(previous)
    proposed['dordiai']['management']['sha256'] = 'b' * 64
    before = path.read_bytes()
    with pytest.raises(ValueError, match='authority'):
        save_settings(state, proposed, expected_dordiai=previous['dordiai'])
    assert path.read_bytes() == before


def test_management_registration_preserves_latest_unrelated_voice_preferences(installed):
    library, selected, manager, calls = installed
    prefs = {'favorites': ['fixture:voice'], 'defaults': {}, 'profiles': {}}
    save_voice_preferences(library / 'state', prefs, expected=load_voice_preferences(library / 'state'))
    before = manager.store.read()
    result = manager.register_manifest(selected, expected_revision=before['revision'])
    assert result['revision'] == before['revision'] + 1
    assert load_voice_preferences(library / 'state') == prefs
    assert load_settings(library / 'state')['dordiai']['management']['sha256'] == sha256(selected.read_bytes()).hexdigest()


def test_cancelled_registration_preserves_all_current_settings_and_manifest(installed):
    library, selected, manager, calls = installed
    before = manager.store.path.read_bytes(), selected.read_bytes(), len(calls)
    with pytest.raises(InterruptedError):
        manager.register_manifest(selected, expected_revision=manager.store.read()['revision'],
                                  cancelled=lambda: True)
    assert (manager.store.path.read_bytes(), selected.read_bytes(), len(calls)) == before


def test_stale_registration_revision_refuses_before_owner_child(installed):
    library, selected, manager, calls = installed
    count, before = len(calls), manager.store.path.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        manager.register_manifest(selected, expected_revision=0)
    assert len(calls) == count and manager.store.path.read_bytes() == before


@pytest.mark.parametrize('field, value', [
    ('sha256', None), ('sha256', 'A' * 64), ('revision', True),
    ('restored', 0), ('manifest', 'https://invalid.test/manifest.json'),
    ('manifest', r'C:\reviewed\manifest.json:stream'),
])
def test_passive_binding_decoder_refuses_untyped_or_external_authority(tmp_path, field, value):
    _, _, settings = inert_settings(tmp_path, managed=True)
    binding = deepcopy(settings['dordiai']['management'])
    binding[field] = value
    with pytest.raises(ValueError):
        validate_management_binding(binding)


def test_passive_restore_never_opens_external_runtime_and_disables_old_authority(tmp_path):
    state, path, settings = inert_settings(tmp_path, managed=True)
    assert not Path(settings['dordiai']['runtime_directory']).exists()
    assert prepare_restored_state(state) is True
    after = load_settings(state)['dordiai']
    assert after['enabled'] is False and after['management']['restored'] is True
    assert {k: v for k, v in after['management'].items() if k != 'restored'} == {
        k: v for k, v in settings['dordiai']['management'].items() if k != 'restored'}
    assert not Path(settings['dordiai']['runtime_directory']).exists()


def test_unmanaged_restore_keeps_exact_existing_settings_bytes(tmp_path):
    state, path, settings = inert_settings(tmp_path)
    before = path.read_bytes()
    assert prepare_restored_state(state) is False
    assert path.read_bytes() == before


def test_real_state_snapshot_restore_keeps_source_bytes_and_makes_binding_dormant(tmp_path):
    from carr_library.state_backup import snapshot_state, restore_state
    state, path, settings = inert_settings(tmp_path, managed=True)
    original = path.read_bytes()
    snapshot = tmp_path / 'snapshot'
    entries = snapshot_state(state, snapshot)
    target = tmp_path / 'restored-state'
    restore_state(snapshot, target, entries)
    assert path.read_bytes() == original
    assert (snapshot / 'audio/engines.json').read_bytes() == original
    restored = load_settings(target)['dordiai']
    assert restored['enabled'] is False and restored['management']['restored'] is True


def test_complete_backup_restores_only_dormant_declarations_without_external_assets(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'real-library')
    workspace = app.create_workspace('Voice restore fixture')
    app.add_text(workspace, 'Exact source', 'Unchanged source text retained through backup.')
    state, path, settings = inert_settings(tmp_path / 'fixture', managed=True)
    destination = app.root / 'state/audio'
    destination.mkdir(parents=True, exist_ok=True)
    atomic_json(destination / 'engines.json', settings, replace=False)
    original = (destination / 'engines.json').read_bytes()
    archive = tmp_path / 'backup'
    digest = create_backup(app, archive)
    restored_root = restore_backup(archive, tmp_path / 'restored-library', digest)
    assert (destination / 'engines.json').read_bytes() == original
    restored = load_settings(restored_root / 'state')['dordiai']
    assert restored['enabled'] is False and restored['management']['restored'] is True
    assert not Path(settings['dordiai']['runtime_directory']).exists()
    assert not any(path.name in {'model.bin', '.voice-packages.lock'} for path in archive.rglob('*'))


def test_real_same_byte_source_substitution_is_refused_by_held_source_boundary(tmp_path):
    source = tmp_path / 'source.zip'
    source.write_bytes(b'exact reviewed bytes')
    original = source.read_bytes()
    moved = tmp_path / 'held-original.zip'
    swapped = False
    try:
        with pytest.raises((ValueError, OSError)):
            with held_file(source, expected_sha256=sha256(original).hexdigest(), maximum=1024):
                try:
                    source.rename(moved)
                except OSError as error:
                    assert getattr(error, 'winerror', None) in {5, 32, 33}
                    raise
                swapped = True
                source.write_bytes(original)
    finally:
        if swapped:
            assert moved.read_bytes() == original and source.read_bytes() == original
    assert source.read_bytes() == original


@pytest.mark.parametrize('late', ['scope', 'interrupt', 'exit'])
def test_actual_settings_commit_survives_late_scope_or_interruption(installed, monkeypatch, late):
    from carr_library.audio import voice_settings
    library, selected, manager, calls = installed
    previous = manager.store.read()
    real = voice_settings.atomic_json
    fault = {'scope': ValueError('synthetic late root scope'), 'interrupt': KeyboardInterrupt(),
             'exit': SystemExit(71)}[late]
    wrote = []
    def publish(path, value, **kwargs):
        result = real(path, value, **kwargs)
        if Path(path) == manager.store.path:
            wrote.append(deepcopy(value))
            monkeypatch.setattr(manager.store, 'check', lambda: (_ for _ in ()).throw(fault))
        return result
    monkeypatch.setattr(voice_settings, 'atomic_json', publish)
    expected_type = VoiceManagementCommitted if late == 'scope' else type(fault)
    with pytest.raises(expected_type) as caught:
        manager.store.register_manifest(selected, expected_revision=previous['revision'])
    receipt = caught.value.voice_management_receipt
    assert wrote and receipt['revision'] == previous['revision'] + 1
    assert receipt['runtime_configuration'] == wrote[-1]['dordiai']
    assert json.loads(manager.store.path.read_bytes())['dordiai'] == wrote[-1]['dordiai']
    if late != 'scope':
        assert caught.value is fault
