"""Version-bound approval through the real current provider and pipe transport.

Owner metadata and PCM are explicit synthetic fixtures. State CAS, complete
manifest reads, OS leases, subprocesses, shared registry and output publication
are the production implementations. This is not owner API or acoustic proof.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.audio.voice_lifecycle import VoiceProfileLifecycle
from carr_library.audio.voice_manifest import LocalModelManifest
from carr_library.audio.voice_ownership import capture_voice_ownership, hold_voice_ownership
from carr_library.integrations import dordiai_voices as api
from carr_library.integrations.dordiai_speech import DordiaiSpeechProvider
from carr_library.runtime_settings import load_settings, save_settings
from carr_library.speech.runtime import VoiceRegistry
from .test_dordiai_voices import ACTOR


def _metadata_fixture():
    path = Path(__file__).resolve().parents[1] / 'audio' / 'voice_management_fixtures.py'
    spec = importlib.util.spec_from_file_location('_managed_speech_metadata_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def managed(tmp_path, monkeypatch):
    fixture = _metadata_fixture()
    library, manifest_path, manager, owner_calls = fixture.installed.__wrapped__(tmp_path, monkeypatch)
    root, requests_path = manifest_path.parent, tmp_path / 'speech-requests.jsonl'
    worker = ACTOR.replace("Path(__file__).with_name('requests.jsonl')",
                           'Path(' + repr(str(requests_path)) + ')')
    additions = {
        'nvda_worker.py': worker.encode('utf-8'),
        'windows_runtime.py': b'def reset_inherited_dll_directory(): return {}\ndef configure_worker_import_paths(): return {}\n',
        'language_catalog.py': b"""
def canonical_code(value): return value.lower().replace('_','-')
def coverage(models=None,selected_only=True):
 return [{'runtime_code':code,'settings_label_uk':code,'resources_present':True,'status':'resources-present-unverified'} for code in ('uk','sk')]
def compatible_voice_ids(models,language,allowed_voice_ids): return tuple(allowed_voice_ids)
""",
        'wide_languages.py': b'def registered(code): return None\n',
        'voice_policy.py': (fixture.POLICY + '\ndef filter_voice_catalog(values):\n return {key:value for key,value in values.items() if is_voice_allowed(key)}\n').encode('utf-8'),
    }
    document = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, data in additions.items():
        (root / name).write_bytes(data)
        document['files'][name] = sha256(data).hexdigest()
    fixture.write_json(manifest_path, document)
    settings = load_settings(library / 'state')
    previous = deepcopy(settings['dordiai'])
    settings['dordiai']['expected_worker_sha256'] = sha256(additions['nvda_worker.py']).hexdigest()
    save_settings(library / 'state', settings, expected_dordiai=previous)
    assert manager.store.read()['restored'] is True
    manager.register_manifest(manifest_path, expected_revision=manager.store.read()['revision'])
    fixture.configure(manager, backend='holos')
    package_path = tmp_path / 'voice-1.dordiai-voice'
    document = fixture.package(package_path, backend='holos')
    manager.apply(manager.inspect(package_path), rights_approved=True, supported_languages=['uk'])
    real_process, children, providers = api._JsonLinesProcess, [], []
    def selected_interpreter(command, cwd, **kwargs):
        # Only the inert, manifest-bound fixture interpreter is substituted.
        # All production framing, owned processes and cleanup stay active.
        assert Path(command[0]) == root / 'python/python.exe'
        assert command[1:4] == ['-I', '-B', '-u']
        child = real_process([sys.executable, *command[1:]], cwd, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(api, '_JsonLinesProcess', selected_interpreter)
    def provider():
        row = manager.store.read()['runtime_configuration']
        value = DordiaiSpeechProvider(root, python_executable=row['python_executable'],
            expected_worker_sha256=row['expected_worker_sha256'],
            configuration=row, state_root=library / 'state')
        providers.append(value)
        return value
    value = SimpleNamespace(library=library, root=root, manifest=manifest_path,
        manager=manager, document=document, fixture=fixture, provider=provider,
        children=children, requests=requests_path, owner_calls=owner_calls)
    try:
        yield value
    finally:
        try:
            for selected in reversed(providers):
                selected.close()
        finally:
            for child in children:
                child.close()


def chosen(provider):
    voices = provider.voices()
    return next(voice for voice in voices if provider._tokens[voice.id] == ('fixture', 'uk'))


def log(case):
    return [json.loads(line) for line in case.requests.read_text(encoding='utf-8').splitlines()] if case.requests.exists() else []


def test_managed_capabilities_and_public_identity_bind_active_version_document_references_and_approval(managed):
    from carr_library.audio.voice_packages import _hash
    provider = managed.provider()
    voice = chosen(provider)
    approved = provider._token_authority[voice.id]
    assert approved[0] == '1.0.0'
    assert approved[1] == _hash(managed.document)
    assert approved[2] == _hash({row['role']: row['sha256'] for row in managed.document['files'].values()})
    assert len(approved[3]) == 64
    caps = provider.adapter.capabilities()
    assert caps.managed_authority_sha256 == approved[3]
    assert caps.managed_asset_versions == (('fixture', *approved[:3]),)
    assert all(language != 'sk' for raw, language in provider._tokens.values() if raw == 'fixture')
    assert provider.fingerprint(voice.id) == provider.fingerprint(voice.id)
    assert all(child.process.poll() is not None and child._cleanup_done for child in managed.children)
    with hold_voice_ownership(capture_voice_ownership(managed.root), timeout=0):
        pass


def test_actual_managed_pcm_uses_existing_registry_and_releases_only_after_child_death(managed, tmp_path):
    provider = managed.provider()
    registry = VoiceRegistry([provider])
    voice = chosen(provider)
    result = registry.synthesize('Synthetic managed reference.', voice.id, tmp_path / 'managed.wav')
    assert result.frames == 6 and result.sample_rate == 24000
    assert [entry['mode'] for entry in log(managed)] == ['reference', 'next', 'next']
    assert log(managed)[0]['profile'] == 'fixture'
    assert provider.adapter._worker is None
    assert all(child.process.poll() is not None and child._cleanup_done for child in managed.children)
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    with hold_voice_ownership(capture_voice_ownership(managed.root), timeout=0):
        pass


def test_actual_activation_rejects_old_queued_selection_and_changes_new_public_id(managed, tmp_path):
    provider = managed.provider()
    registry = VoiceRegistry([provider])
    original = chosen(provider)
    fingerprint = provider.fingerprint(original.id)
    cache_identity = registry.fingerprint(original.id)
    saved = registry.synthesize('First reviewed version.', original.id, tmp_path / 'first.wav')
    before = saved.path.read_bytes()
    path = tmp_path / 'voice-2.dordiai-voice'
    managed.fixture.package(path, version='2.0.0', backend='holos')
    managed.manager.apply(managed.manager.inspect(path), rights_approved=True, supported_languages=['uk'])
    calls = log(managed)
    with pytest.raises(ValueError, match='configuration changed'):
        provider.fingerprint(original.id)
    with pytest.raises(ValueError):
        registry.synthesize('Stale saved plan.', original.id, tmp_path / 'stale.wav')
    assert not (tmp_path / 'stale.wav').exists() and log(managed) == calls
    assert saved.path.read_bytes() == before
    fresh = managed.provider()
    current = chosen(fresh)
    assert current.id != original.id and fresh._token_authority[current.id][0] == '2.0.0'
    assert fresh.fingerprint(current.id) != fingerprint
    current_registry = VoiceRegistry([fresh])
    assert current_registry.fingerprint(current.id) != cache_identity
    assert current_registry.synthesize('Explicit new selection.', current.id, tmp_path / 'second.wav').frames == 6


@pytest.mark.parametrize('delete', [False, True])
def test_actual_revoke_or_delete_never_falls_back_or_reuses_a_saved_plan(managed, tmp_path, delete):
    provider = managed.provider()
    voice = chosen(provider)
    previous = provider.fingerprint(voice.id)
    lifecycle = VoiceProfileLifecycle(managed.library, manager=managed.manager, broker=managed.manager.broker)
    review = lifecycle.review_removal('fixture', delete_files=delete)
    lifecycle.remove(review, confirmation=review.confirmation)
    with pytest.raises(ValueError, match='configuration changed'):
        provider.fingerprint(voice.id)
    fresh = managed.provider()
    assert all(fresh._tokens[item.id][0] != 'fixture' for item in fresh.voices())
    with pytest.raises(ValueError):
        fresh.fingerprint(voice.id)
    with pytest.raises(ValueError, match='lacks a current version-bound approval'):
        list(fresh.adapter.stream_pcm('No legacy fallback.', 'fixture', 'uk'))
    assert not log(managed)
    directory = managed.root / 'models/voices/packages/fixture'
    assert directory.exists() is (not delete)
    assert len(previous) == 64


@pytest.mark.parametrize('field', ['missing', 'document_sha256', 'references'])
def test_explicit_reregistration_does_not_invent_missing_per_profile_rights(managed, field):
    value = json.loads(managed.manifest.read_text(encoding='utf-8'))
    review = value['settings']['profile_reviews']['fixture']
    if field == 'missing':
        del value['settings']['profile_reviews']['fixture']
    else:
        review[field] = {'document_sha256': '0' * 64, 'references': {'holos_profile': '0' * 64}}[field]
    managed.fixture.write_json(managed.manifest, value)
    managed.manager.register_manifest(managed.manifest, expected_revision=managed.manager.store.read()['revision'])
    provider = managed.provider()
    assert all(provider._tokens[item.id][0] != 'fixture' for item in provider.voices())
    with pytest.raises(ValueError, match='lacks a current version-bound approval'):
        list(provider.adapter.stream_pcm('No automatic rights.', 'fixture', 'uk'))
    assert not log(managed)


@pytest.mark.parametrize('kind', ['manifest', 'index', 'reference', 'worker'])
def test_changed_exact_asset_refuses_before_any_pcm_or_new_public_handle(managed, kind):
    provider = managed.provider()
    voice = chosen(provider)
    path = {'manifest': managed.manifest,
            'index': managed.root / 'models/voices/packages/voice-packages.json',
            'reference': managed.root / 'models/voices/packages/fixture/1.0.0/profile.dat',
            'worker': managed.root / 'nvda_worker.py'}[kind]
    path.write_bytes(path.read_bytes() + b' ')
    before_children = len(managed.children)
    with pytest.raises(ValueError):
        provider.fingerprint(voice.id)
    assert len(managed.children) == before_children and not log(managed)
    assert provider.adapter._worker is None
    with hold_voice_ownership(capture_voice_ownership(managed.root), timeout=0):
        pass


@pytest.mark.parametrize('kind', ['index', 'document', 'settings'])
def test_current_managed_stream_rechecks_authority_after_each_actual_pcm_yield(managed, kind):
    provider = managed.provider()
    chosen(provider)
    stream = provider.adapter.stream_pcm('Current approved version.', 'fixture', 'uk')
    assert next(stream).frames == 3
    child = provider.adapter._worker
    if kind == 'settings':
        settings = load_settings(managed.library / 'state')
        previous = deepcopy(settings['dordiai'])
        settings['dordiai']['enabled'] = False
        save_settings(managed.library / 'state', settings, expected_dordiai=previous)
    else:
        path = managed.root / 'models/voices/packages' / (
            'voice-packages.json' if kind == 'index' else 'fixture/1.0.0/voice-package.json')
        path.write_bytes(path.read_bytes() + b' ')
    try:
        with pytest.raises(ValueError):
            next(stream)
        assert child.process.poll() is not None and child._cleanup_done
        assert provider.adapter._worker is None
        assert [entry['mode'] for entry in log(managed)].count('reference') == 1
        assert all(entry['mode'] != 'next' for entry in log(managed))
    finally:
        stream.close()


def test_restored_binding_stays_dormant_until_explicit_current_registration(managed):
    from carr_library.durable_state import atomic_json
    from carr_library.runtime_settings import settings_path
    selected = settings_path(managed.library / 'state')
    data = json.loads(selected.read_text(encoding='utf-8'))
    data['dordiai']['management']['restored'] = True
    atomic_json(selected, data, replace=True)
    before_children = len(managed.children)
    with pytest.raises(ValueError, match='restored voices require explicit'):
        managed.provider()
    assert len(managed.children) == before_children
    managed.manager.register_manifest(managed.manifest, expected_revision=managed.manager.store.read()['revision'])
    assert chosen(managed.provider()).id.startswith('dordiai-voices:')


def test_managed_lock_digest_uses_only_current_held_descriptor_during_full_fingerprint(managed, monkeypatch):
    provider = managed.provider()
    voice = chosen(provider)
    lock = managed.root / 'models/voices/packages/.voice-packages.lock'
    original = Path.open
    def deny_second_handle(path, *args, **kwargs):
        if path == lock:
            pytest.fail('A managed Windows byte lock cannot be hashed through another file handle.')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', deny_second_handle)
    assert len(provider.fingerprint(voice.id)) == 64
    assert all(child.process.poll() is not None and child._cleanup_done for child in managed.children)


def test_direct_adapter_cannot_adopt_a_library_review_without_its_binding(managed):
    row = managed.manager.store.read()['runtime_configuration']
    with api.DordiaiVoicesAdapter(managed.root, python_executable=row['python_executable'],
            expected_worker_sha256=row['expected_worker_sha256']) as adapter:
        assert 'fixture' not in {voice.id for voice in adapter.voices()}
        with pytest.raises(ValueError, match='lacks a current version-bound approval'):
            list(adapter.stream_pcm('No implicit library permission.', 'fixture', 'uk'))
    assert not log(managed)
