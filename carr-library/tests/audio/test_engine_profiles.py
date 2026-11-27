"""Local registrations/admission; synthetic assets never execute as engines."""
import hashlib
import json
import os
import shutil
from unittest.mock import Mock

from filelock import FileLock
import pytest

from carr_library.audio import engines
from carr_library.audio.model_manifest import LocalModelManifest, ModelManifestError
from carr_library.runtime_settings import EngineProfileStore, configured_voice_registry, profile_reference


def installation(tmp_path, name='pack', *, engine='piper'):
    root = tmp_path / name
    root.mkdir()
    if engine == 'piper':
        payloads = {'piper.exe': b'synthetic executable, never run', 'voice.onnx': b'synthetic model',
                    'voice.json': b'{}'}
        settings = dict(executable='piper.exe', model='voice.onnx', config='voice.json',
                        language='uk', name='Synthetic local voice')
    else:
        payloads = {'engine.bin': b'synthetic installed bytes', 'model.bin': b'synthetic model'}
        settings = {}
    for path, data in payloads.items():
        (root / path).write_bytes(data)
    value = dict(schema_version=1, engine=engine, model_id=name, revision='a' * 40,
                 license='Synthetic test assets only', license_accepted=True, trusted_weights=True,
                 files={name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()},
                 settings=settings)
    path = root / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def store(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    return EngineProfileStore(root)


def test_registration_is_persistent_and_does_not_execute_or_import_assets(tmp_path, monkeypatch):
    selected = installation(tmp_path)
    registry = store(tmp_path)
    process = Mock(side_effect=AssertionError('No executable may run during registration'))
    monkeypatch.setattr(engines.subprocess, 'Popen', process)
    profile = registry.register(selected, name='Український голос')
    reopened = EngineProfileStore(registry.library_root)
    assert reopened.snapshot().profiles == (profile,)
    assert profile.enabled and profile.manifest_sha256 == hashlib.sha256(selected.read_bytes()).hexdigest()
    reviewed = reopened.admit(profile.id, engine='piper')
    assert reviewed.manifest.path == selected and reviewed.files == 3
    assert reviewed.bytes == sum(path.stat().st_size for path in selected.parent.iterdir() if path != selected)
    process.assert_not_called()


def test_registering_the_same_reviewed_installation_preserves_profile_identity(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    first = registry.register(selected, name='First name')
    second = registry.register(selected, name='Explicit renamed profile')
    assert first.id == second.id
    assert registry.snapshot().profiles == (second,)


def test_content_change_is_rejected_even_when_size_and_mtime_are_restored(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    profile = registry.register(selected)
    asset = selected.parent / 'voice.onnx'
    before = asset.stat()
    original = asset.read_bytes()
    asset.write_bytes(b'x' * len(original))
    os.utime(asset, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ModelManifestError, match='Checksum'):
        registry.admit(profile.id)
    assert registry.snapshot().profiles == (profile,)


def test_captured_manifest_hash_cannot_be_silently_replaced_at_registration(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    reviewed = registry.inspect(selected)
    value = json.loads(selected.read_bytes())
    value['revision'] = 'b' * 40
    selected.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ModelManifestError, match='changed'):
        registry.register(selected, expected_sha256=reviewed.manifest.manifest_sha256)
    assert not registry.path.exists()


def test_explicit_new_manifest_revision_gets_a_new_id_and_disables_the_old_one(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    old = registry.register(selected, name='Old revision')
    value = json.loads(selected.read_bytes())
    value['revision'] = 'b' * 40
    selected.write_text(json.dumps(value), encoding='utf-8')
    new = registry.register(selected, name='Reviewed new revision')
    assert old.id != new.id
    rows = {row.id: row for row in registry.snapshot().profiles}
    assert not rows[old.id].enabled and rows[new.id].enabled
    with pytest.raises(ModelManifestError, match='disabled'):
        registry.admit(old.id)


@pytest.mark.parametrize('change', ['unlisted', 'license', 'trust', 'missing-executable'])
def test_unreviewed_or_incomplete_installations_never_create_registration(tmp_path, change):
    registry, selected = store(tmp_path), installation(tmp_path)
    value = json.loads(selected.read_bytes())
    if change == 'unlisted':
        (selected.parent / 'unlisted.py').write_text('raise RuntimeError()', encoding='utf-8')
    elif change == 'license':
        value['license_accepted'] = False
    elif change == 'trust':
        value['trusted_weights'] = False
    else:
        del value['settings']['executable']
    selected.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises((ValueError, FileNotFoundError)):
        registry.register(selected)
    assert not registry.path.exists()


def test_portable_project_cannot_register_its_own_executable_installation(tmp_path):
    registry = store(tmp_path)
    selected = installation(registry.library_root)
    with pytest.raises(ModelManifestError, match='separate'):
        registry.register(selected)
    assert not registry.path.exists()


def test_legacy_paths_remain_inert_and_exact_bytes_are_preserved_on_explicit_registration(tmp_path, monkeypatch):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.path.parent.mkdir(parents=True)
    legacy = dict(schema=1, ffmpeg='/do/not/execute', ffprobe=None,
                  piper=[{'executable': '/unknown/private/helper'}], voice_helpers=[],
                  music_models=[], fluidsynth=None)
    raw = (json.dumps(legacy, indent=3) + '\n\n').encode('utf-8')
    registry.path.write_bytes(raw)
    process = Mock(side_effect=AssertionError('Legacy paths must remain inert'))
    monkeypatch.setattr(engines.subprocess, 'Popen', process)
    snapshot = registry.snapshot()
    assert snapshot.legacy and snapshot.profiles == ()
    profile = registry.register(selected)
    assert registry.snapshot().profiles == (profile,)
    archived = registry.path.parent / 'legacy' / ('engines-schema1-' + hashlib.sha256(raw).hexdigest() + '.json')
    assert archived.read_bytes() == raw
    process.assert_not_called()


def test_disconnected_installation_remains_listed_and_can_be_disabled_or_removed(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    profile = registry.register(selected)
    shutil.rmtree(selected.parent)
    assert registry.snapshot().profiles == (profile,)
    registry.set_enabled(profile.id, False)
    assert not registry.snapshot().profile(profile.id).enabled
    with pytest.raises(ModelManifestError, match='disabled'):
        registry.admit(profile.id)
    registry.remove(profile.id)
    assert registry.snapshot().profiles == ()


def test_disabling_and_removing_never_delete_installed_assets(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    profile = registry.register(selected)
    before = {path.name: path.read_bytes() for path in selected.parent.iterdir()}
    registry.set_enabled(profile.id, False)
    registry.set_enabled(profile.id, True)
    registry.remove(profile.id)
    assert before == {path.name: path.read_bytes() for path in selected.parent.iterdir()}


def test_cancellation_during_inventory_verification_preserves_previous_registry(tmp_path, monkeypatch):
    registry, selected = store(tmp_path), installation(tmp_path)
    original = LocalModelManifest.verify
    cancelled = False
    def verified(manifest, **options):
        nonlocal cancelled
        result = original(manifest, **options)
        cancelled = True
        return result
    monkeypatch.setattr(LocalModelManifest, 'verify', verified)
    with pytest.raises(InterruptedError):
        registry.register(selected, cancelled=lambda: cancelled)
    assert not registry.path.exists()


def test_concurrent_settings_writer_is_not_overridden(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.path.parent.mkdir(parents=True)
    with FileLock(str(registry.path.with_name('engines.json.lock')), timeout=0):
        with pytest.raises(RuntimeError, match='another operation'):
            registry.register(selected)
    assert not registry.path.exists()


def test_new_registration_cannot_overwrite_a_duplicate_display_name(tmp_path):
    registry = store(tmp_path)
    first = registry.register(installation(tmp_path, 'one'), name='Голос')
    before = registry.path.read_bytes()
    with pytest.raises(ValueError, match='distinct'):
        registry.register(installation(tmp_path, 'two'), name='ГОЛОС')
    assert registry.path.read_bytes() == before
    assert registry.snapshot().profiles == (first,)


@pytest.mark.parametrize('change', ['schema-bool', 'engine-type', 'enabled-type', 'digest', 'identity',
                                   'relative-path', 'network-path', 'unknown-field', 'duplicate-name'])
def test_registry_schema_rejects_malformed_or_unscoped_records(tmp_path, change):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.register(selected)
    value = json.loads(registry.path.read_bytes())
    row = value['profiles'][0]
    if change == 'schema-bool':
        value['schema'] = True
    elif change == 'engine-type':
        row['engine'] = []
    elif change == 'enabled-type':
        row['enabled'] = 1
    elif change == 'digest':
        row['manifest_sha256'] = 'G' * 64
    elif change == 'identity':
        row['id'] = '../external'
    elif change == 'relative-path':
        row['manifest_path'] = '../external/manifest.json'
    elif change == 'network-path':
        row['manifest_path'] = '//server/share/manifest.json'
    elif change == 'unknown-field':
        row['command'] = 'unreviewed arbitrary command'
    else:
        value['profiles'].append(dict(row, id='f' * 32))
    registry.path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        registry.snapshot()


def test_copied_registry_at_another_library_root_is_not_active(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    profile = registry.register(selected)
    destination = tmp_path / 'other-library'
    destination.mkdir()
    restored = EngineProfileStore(destination)
    restored.path.parent.mkdir(parents=True)
    shutil.copyfile(registry.path, restored.path)
    with pytest.raises(ValueError, match='another location'):
        restored.admit(profile.id)


def test_captured_store_rejects_replaced_library_directory(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.register(selected)
    moved = tmp_path / 'old-library'
    registry.library_root.rename(moved)
    registry.library_root.mkdir()
    with pytest.raises(ValueError, match='directory changed'):
        registry.snapshot()
    assert (moved / 'state/audio/engines.json').exists()


def test_music_reference_is_a_registered_id_and_cannot_be_a_project_path(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path, engine='fluidsynth')
    profile = registry.register(selected)
    reference = profile_reference(profile)
    assert registry.music_manifest(reference, 'fluidsynth').manifest_sha256 == profile.manifest_sha256
    for value in ('manifest.json', 'models/manifest.json', str(selected), '../manifest.json'):
        with pytest.raises(ModelManifestError, match='saved local music profile'):
            registry.music_manifest(value, 'fluidsynth')
    with pytest.raises(ModelManifestError, match='another engine'):
        registry.music_manifest(reference, 'musicgen')
    registry.set_enabled(profile.id, False)
    with pytest.raises(ModelManifestError, match='disabled'):
        registry.music_manifest(reference, 'fluidsynth')


def test_registered_speech_ids_coexist_and_revalidate_before_every_call(tmp_path, monkeypatch):
    registry = store(tmp_path)
    first = registry.register(installation(tmp_path, 'one'))
    second = registry.register(installation(tmp_path, 'two'))
    calls = []
    class SyntheticPiper:
        def __init__(self, executable, manifest):
            self.manifest = LocalModelManifest.load(manifest, engine='piper')
        def voices(self, *, cancelled):
            return (engines.Voice('piper:voice', 'Synthetic', ('uk',), 'piper'),)
        def cache_identity(self, voice, *, cancelled):
            calls.append(('identity', voice))
            return self.manifest.cache_identity()
        def synthesize(self, text, output, *, voice, cancelled, **settings):
            calls.append(('speech', voice, text))
            return output
    monkeypatch.setattr(engines, 'PiperEngine', SyntheticPiper)
    monkeypatch.setattr(engines, 'default_registry', lambda **options: engines.VoiceRegistry())
    voices = configured_voice_registry(registry.library_root)
    rows = voices.voices()
    assert len(rows) == 2 and rows[0].id != rows[1].id
    chosen = next(row for row in rows if row.engine == 'local-' + first.id)
    assert voices.cache_identity(chosen.id)['local_profile_id'] == first.id
    voices.synthesize('Текст', tmp_path / 'unused.wav', voice=chosen.id, language='uk')
    assert calls == [('identity', 'piper:voice'), ('speech', 'piper:voice', 'Текст')]
    registry.set_enabled(first.id, False)
    before = list(calls)
    with pytest.raises(ModelManifestError, match='disabled'):
        voices.synthesize('No execution', tmp_path / 'unused.wav', voice=chosen.id, language='uk')
    assert calls == before
    other = next(row for row in rows if row.engine == 'local-' + second.id)
    registry.remove(second.id)
    with pytest.raises(ValueError, match='no longer exists'):
        voices.cache_identity(other.id)


def test_invalid_local_registry_does_not_disable_installed_system_voices(tmp_path, monkeypatch):
    registry = store(tmp_path)
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text('{"schema": true}', encoding='utf-8')
    base = engines.VoiceRegistry()
    monkeypatch.setattr(engines, 'default_registry', lambda **options: base)
    actual = configured_voice_registry(registry.library_root)
    assert actual is base and actual.notices[-1]['engine'] == 'local-profiles'


def test_machine_engine_registrations_are_excluded_from_complete_backup(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Profile exclusion fixture')
    registry = EngineProfileStore(app.root)
    profile = registry.register(installation(tmp_path))
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    verify_backup(backup, digest)
    restored = tmp_path / 'restored'
    restore_backup(backup, restored, digest)
    assert not (restored / 'state/audio/engines.json').exists()
    assert EngineProfileStore(restored).snapshot().profiles == ()
    assert registry.snapshot().profile(profile.id).enabled

def test_write_budget_rejection_retains_the_last_readable_registry(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    registry = store(tmp_path)
    first = registry.register(installation(tmp_path, 'one'))
    before = registry.path.read_bytes()
    # The current snapshot still fits; adding another valid row does not.
    monkeypatch.setattr(runtime_settings, 'MAX_SETTINGS_BYTES', len(before) + 8)
    with pytest.raises(ValueError, match='byte budget'):
        registry.register(installation(tmp_path, 'two'))
    assert registry.path.read_bytes() == before
    assert registry.snapshot().profiles == (first,)


def test_profile_labels_use_normalized_unicode_before_duplicate_check(tmp_path):
    registry = store(tmp_path)
    profile = registry.register(installation(tmp_path, 'one'), name='  Cafe\u0301  ')
    assert profile.name == 'Café'
    before = registry.path.read_bytes()
    with pytest.raises(ValueError, match='distinct'):
        registry.register(installation(tmp_path, 'two'), name='CAFÉ')
    assert registry.path.read_bytes() == before

def test_production_resolves_optional_music_before_any_engine_execution(tmp_path, monkeypatch):
    from carr_library.audio import production
    from types import SimpleNamespace
    plan = production.parse_script(
        '[music engine=fluidsynth model_manifest=profiles/' + 'a' * 32 + '.json]\nNarrator: Text.')
    execution = Mock(side_effect=AssertionError('Disabled profile must not execute an engine'))
    engine = SimpleNamespace(synthesize=execution, cache_identity=lambda voice: {'fixture': voice})
    monkeypatch.setattr(production.audio, 'ffmpeg_identity', lambda *args, **options: {'fixture': True})
    resolver = Mock(side_effect=ModelManifestError('The selected profile is disabled.'))
    with pytest.raises(ModelManifestError, match='disabled'):
        production.render_project(plan, tmp_path / 'project', engine=engine,
                                  ffmpeg=tmp_path / 'never-run', music_manifest_resolver=resolver)
    assert resolver.call_args.args == ('profiles/' + 'a' * 32 + '.json', 'fluidsynth')
    assert callable(resolver.call_args.kwargs['cancelled'])
    execution.assert_not_called()


def test_existing_hardlinked_settings_lock_cannot_touch_another_file(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.path.parent.mkdir(parents=True)
    other = tmp_path / 'unrelated.bin'
    before = b'Unrelated bytes must not be truncated by opening a lock.'
    other.write_bytes(before)
    os.link(other, registry.path.with_name('engines.json.lock'))
    with pytest.raises(ValueError, match='hard-link alias'):
        registry.register(selected)
    assert other.read_bytes() == before and not registry.path.exists()


def test_existing_directory_settings_lock_is_rejected_before_publication(tmp_path):
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.path.parent.mkdir(parents=True)
    registry.path.with_name('engines.json.lock').mkdir()
    with pytest.raises(ValueError, match='regular'):
        registry.register(selected)
    assert not registry.path.exists()


@pytest.mark.parametrize('after_move', [False, True], ids=['before-move', 'after-move-directory-sync'])
def test_legacy_archive_failure_preserves_source_and_retries_without_partial_final(tmp_path, monkeypatch, after_move):
    from carr_library.audio import jobs
    registry, selected = store(tmp_path), installation(tmp_path)
    registry.path.parent.mkdir(parents=True)
    raw = (json.dumps(dict(schema=1, ffmpeg=None, ffprobe=None, piper=[],
                          voice_helpers=[], music_models=[], fluidsynth=None), indent=3) + '\n').encode('utf-8')
    registry.path.write_bytes(raw)
    legacy = registry.path.parent / 'legacy'
    target = legacy / ('engines-schema1-' + hashlib.sha256(raw).hexdigest() + '.json')
    with monkeypatch.context() as patch:
        if after_move:
            original = jobs._sync_directory
            def fail_after_move(directory):
                if directory == legacy:
                    raise OSError('Synthetic legacy directory sync failure.')
                return original(directory)
            patch.setattr(jobs, '_sync_directory', fail_after_move)
        else:
            patch.setattr(jobs, 'publish_noreplace',
                          Mock(side_effect=OSError('Synthetic legacy stage publication failure.')))
        with pytest.raises(OSError, match='Synthetic legacy'):
            registry.register(selected)
    assert registry.path.read_bytes() == raw
    assert not list(legacy.glob('.publish-*'))
    if after_move:
        assert target.read_bytes() == raw
    else:
        assert not target.exists()
    profile = registry.register(selected)
    assert target.read_bytes() == raw
    assert registry.snapshot().profiles == (profile,)
