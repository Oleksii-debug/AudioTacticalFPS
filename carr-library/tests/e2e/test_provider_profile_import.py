"""Explicit inert profile import through the current store and native controller."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import os
from threading import Barrier

import pytest

from carr_library import providers as api
from carr_library.ui.api_connections import ConnectionsController


class NoVault:
    def put(self, *args):
        raise AssertionError('Import must not write a credential.')
    def get(self, *args):
        raise AssertionError('Import must not read a credential.')
    def delete(self, *args):
        raise AssertionError('Import must not delete a credential.')


def profile(kind='remote', identity='imported'):
    reference = 'url-api-' + 'a' * 32
    if kind in ('remote', 'local'):
        return api.ProviderProfile(identity, 'Synthetic ' + kind, 'openai-compatible',
            'https://example.com/v1' if kind == 'remote' else 'http://127.0.0.1:54321/v1',
            'reviewed-model', ('llm',), credential_ref=reference, local=kind == 'local')
    if kind == 'speech':
        return api.ProviderProfile(identity, 'Synthetic speech', 'elevenlabs',
            'https://api.elevenlabs.io/v1', 'reviewed-voice-model', ('tts',), credential_ref=reference)
    if kind == 'local_translation':
        return api.ProviderProfile(identity, 'Synthetic local translation', api.LOCAL_TRANSLATION_PROVIDER,
            'http://127.0.0.1:54321', 'reviewed-local-label', ('translation',), local=True)
    advanced = kind == 'v3'
    return api.ProviderProfile(identity, 'Synthetic translation',
        'google-translation-v3' if advanced else 'google-translation-v2',
        'https://translation.googleapis.com/v3/projects/123/locations/global' if advanced
            else api.GOOGLE_TRANSLATION_ENDPOINT,
        'general/nmt' if advanced else 'nmt', ('translation',), credential_ref=reference)


def document(*profiles):
    return json.dumps({'version': 1, 'profiles': [asdict(item) for item in profiles]},
                      ensure_ascii=False).encode('utf-8')


@pytest.mark.parametrize('kind', ['remote', 'local', 'speech', 'v2', 'v3', 'local_translation'])
def test_import_retains_settings_without_vault_bindings_or_authority(tmp_path, monkeypatch, kind):
    chosen = profile(kind)
    monkeypatch.setattr(api, 'WindowsCredentialVault', lambda: pytest.fail('Native vault opened during import.'))
    monkeypatch.setattr(api.ProviderGateway, 'perform', lambda *a, **k: pytest.fail('Import sent a request.'))
    store = api.ProfileStore(tmp_path / 'api-connections.json', NoVault())
    imported = store.import_profiles(document(chosen))
    assert imported == (replace(chosen, credential_ref=None),)
    assert store.profiles() == imported and imported[0].credential_ref is None
    assert 'url-api-' not in store.path.read_text(encoding='utf-8')
    assert not (tmp_path / 'api-operations.sqlite').exists()
    assert not (tmp_path / 'state' / 'translation').exists()


@pytest.mark.parametrize('damage', ['duplicate_json', 'unknown_top', 'version_bool',
    'unknown_profile', 'embedded_secret', 'unknown_capability', 'bad_endpoint',
    'malformed_ref', 'duplicate_account', 'nonfinite', 'invalid_utf8', 'oversize', 'too_many'])
def test_import_rejects_untrusted_or_unbounded_documents_without_mutation(tmp_path, damage):
    store = api.ProfileStore(tmp_path / 'api-connections.json', NoVault())
    existing = replace(profile(identity='existing'), credential_ref=None)
    store.save(existing)
    before = store.path.read_bytes()
    value = json.loads(document(profile()))
    row = value['profiles'][0]
    if damage == 'duplicate_json':
        raw = b'{"version":1,"version":1,"profiles":[]}'
    elif damage == 'invalid_utf8':
        raw = b'\xff'
    elif damage == 'oversize':
        raw = b' ' * (api.MAX_REQUEST + 1)
    else:
        if damage == 'unknown_top':
            value['approved'] = True
        elif damage == 'version_bool':
            value['version'] = True
        elif damage == 'unknown_profile':
            row['enabled'] = True
        elif damage == 'embedded_secret':
            row['api_key'] = 'synthetic-not-a-real-key'
        elif damage == 'unknown_capability':
            row['capabilities'] = ['shell.execute']
        elif damage == 'bad_endpoint':
            row['endpoint'] = 'https://synthetic-key@example.com/v1'
        elif damage == 'malformed_ref':
            row['credential_ref'] = 'plaintext-not-a-reference'
        elif damage == 'duplicate_account':
            value['profiles'].append(dict(row))
        elif damage == 'nonfinite':
            row['name'] = float('nan')
        elif damage == 'too_many':
            value['profiles'] = [{**row, 'id': 'account_' + str(index)} for index in range(1001)]
        raw = json.dumps(value).encode()
    with pytest.raises((api.ProviderError, FileExistsError)):
        store.import_profiles(raw)
    assert store.path.read_bytes() == before and store.profiles() == (existing,)


def test_conflict_refuses_the_complete_batch_without_overwriting_an_account(tmp_path):
    store = api.ProfileStore(tmp_path / 'api-connections.json', NoVault())
    previous = replace(profile(identity='existing'), credential_ref=None)
    store.save(previous)
    before = store.path.read_bytes()
    with pytest.raises(FileExistsError):
        store.import_profiles(document(profile(identity='new'), profile(identity='existing')))
    assert store.path.read_bytes() == before and store.profiles() == (previous,)


def test_atomic_publication_failure_preserves_prior_accounts(tmp_path, monkeypatch):
    store = api.ProfileStore(tmp_path / 'api-connections.json', NoVault())
    store.save(replace(profile(identity='existing'), credential_ref=None))
    before = store.path.read_bytes()
    original = api.os.replace
    def refuse(source, destination):
        if destination == store.path:
            raise OSError('Synthetic profile publication failure.')
        return original(source, destination)
    monkeypatch.setattr(api.os, 'replace', refuse)
    with pytest.raises(OSError, match='publication failure'):
        store.import_profiles(document(profile()))
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob('.connections-*'))


@pytest.mark.parametrize('duplicate', [False, True])
def test_real_concurrent_imports_use_one_writer_and_never_lose_an_account(tmp_path, duplicate):
    barrier = Barrier(2)
    path = tmp_path / 'api-connections.json'
    def run(identity):
        selected = api.ProfileStore(path, NoVault())
        barrier.wait(timeout=5)
        try:
            selected.import_profiles(document(profile(identity=identity)))
            return 'saved'
        except FileExistsError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, identity) for identity in ('first', 'first' if duplicate else 'second')]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(results) == (['conflict', 'saved'] if duplicate else ['saved', 'saved'])
    assert {row.id for row in api.ProfileStore(path).profiles()} == ({'first'} if duplicate else {'first', 'second'})


def test_empty_export_is_a_noop_without_creating_settings(tmp_path):
    store = api.ProfileStore(tmp_path / 'api-connections.json', NoVault())
    assert store.import_profiles(document()) == ()
    assert not store.path.exists()


def test_native_controller_review_is_read_only_and_import_requires_explicit_approval(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = ConnectionsController(root, vault=NoVault())
    source = tmp_path / 'profiles.json'
    source.write_bytes(document(profile()))
    raw, selected = controller.review_profiles(source)
    assert selected[0].credential_ref is None and not controller.store.path.exists()
    with pytest.raises(api.ProviderError, match='Review'):
        controller.import_profiles(raw)
    assert not controller.store.path.exists()
    assert controller.import_profiles(raw, approved=True) == selected


def test_reviewed_bytes_do_not_change_when_the_external_export_changes_later(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = ConnectionsController(root, vault=NoVault())
    source = tmp_path / 'profiles.json'
    source.write_bytes(document(profile(identity='reviewed')))
    raw, selected = controller.review_profiles(source)
    source.write_bytes(document(profile(identity='later-unreviewed')))
    assert controller.import_profiles(raw, approved=True) == selected
    assert [row.id for row in controller.store.profiles()] == ['reviewed']


def test_import_cannot_follow_a_replaced_library_root(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = ConnectionsController(root, vault=NoVault())
    root.rename(tmp_path / 'previous-library')
    root.mkdir()
    with pytest.raises(api.ProviderError, match='library changed'):
        controller.import_profiles(document(profile()), approved=True)
    assert not (root / 'api-connections.json').exists()
    assert not (tmp_path / 'previous-library' / 'api-connections.json').exists()


def test_native_review_rejects_a_hard_linked_export(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    controller = ConnectionsController(root, vault=NoVault())
    source = tmp_path / 'profiles.json'
    source.write_bytes(document(profile()))
    os.link(source, tmp_path / 'alias.json')
    with pytest.raises(api.ProviderError, match='unlinked'):
        controller.review_profiles(source)
    assert not controller.store.path.exists()


@pytest.mark.parametrize('kind', ['oversize', 'directory'])
def test_native_review_refuses_invalid_inputs_before_any_import(tmp_path, kind):
    root = tmp_path / 'library'
    root.mkdir()
    controller = ConnectionsController(root, vault=NoVault())
    source = tmp_path / 'profiles.json'
    if kind == 'oversize':
        source.write_bytes(b' ' * (api.MAX_REQUEST + 1))
    else:
        source.mkdir()
    with pytest.raises((ValueError, OSError)):
        controller.review_profiles(source)
    assert not controller.store.path.exists()
