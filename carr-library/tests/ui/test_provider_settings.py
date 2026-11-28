"""Settings logic and concrete consent; all remote transports are explicit fakes."""
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from carr_library.assistant.providers import HTTPResult, ProviderError
from carr_library.ui.provider_settings import (ProviderSettingsModel, prepare_connection_test,
    profile_label, reviewed_cost, run_reviewed_connection_test)


class Vault:
    available = True
    def __init__(self):
        self.values, self.reads = {}, []
    def store(self, profile_id, endpoint, secret):
        reference = 'wincred:' + uuid4().hex
        self.values[reference] = (profile_id, endpoint, secret)
        return reference
    def resolve(self, reference, profile_id, endpoint):
        self.reads.append(reference)
        saved_id, saved_endpoint, secret = self.values[reference]
        assert (saved_id, saved_endpoint) == (profile_id, endpoint)
        return secret
    def delete(self, reference):
        self.values.pop(reference, None)


class Transport:
    def __init__(self):
        self.requests = []
    def request(self, profile, path, payload, headers, **kwargs):
        self.requests.append((profile, path, json.loads(payload)))
        return HTTPResult('application/json', b'{"choices":[{"message":{"content":"connected"}}]}')


def save(model, *, current=None, secret='', name='Selected account', local=False, endpoint=None):
    return model.save(current=current, name=name, mode='local-chat' if local else 'remote-chat',
        endpoint_text=endpoint or ('http://127.0.0.1:8080/v1' if local else 'https://api.example.com/v1'),
        model='explicit-model', secret=secret)


def test_settings_create_edit_remove_and_never_read_existing_key(tmp_path):
    vault = Vault()
    model = ProviderSettingsModel(tmp_path / 'library', vault=vault)
    first = save(model, secret='synthetic-setting-key')
    second = save(model, name=first.name, secret='another-synthetic-key')
    assert first.id != second.id and profile_label(first) != profile_label(second)
    updated = save(model, current=first, name='Renamed')
    assert updated.credential_ref == first.credential_ref
    assert vault.reads == []
    assert 'synthetic-setting-key' not in model.store.path.read_text()
    with pytest.raises(ValueError, match='changed'):
        save(model, current=first, name='Stale change')
    with pytest.raises(ValueError, match='changed'):
        model.remove(first)
    model.remove(updated)
    assert model.store.list_profiles() == (second,)
    assert updated.credential_ref not in vault.values and vault.reads == []


def test_endpoint_edit_disconnects_old_key_without_resolving_it(tmp_path):
    vault = Vault()
    model = ProviderSettingsModel(tmp_path / 'library', vault=vault)
    first = save(model, secret='synthetic-setting-key')
    changed = save(model, current=first, endpoint='https://other.example.com/v1')
    assert changed.credential_ref is None and vault.reads == []


@pytest.mark.parametrize('value', ['', 'unknown', 'NaN', 'Infinity', '-1', '1e9999999',
                                    '0.0000001', '1000001', '1; send', '  '])
def test_unknown_or_invalid_budget_cannot_authorize_test(value):
    with pytest.raises(ValueError):
        reviewed_cost(value)


def test_budget_conversion_is_exact():
    assert reviewed_cost('0.000001') == 1
    assert reviewed_cost('0.12') == 120000
    assert reviewed_cost('0') == 0


def test_test_plan_binds_profile_data_budget_and_single_call(tmp_path):
    vault, transport = Vault(), Transport()
    model = ProviderSettingsModel(tmp_path / 'library', vault=vault)
    profile = save(model, secret='synthetic-setting-key')
    with pytest.raises(ValueError, match='known'):
        prepare_connection_test(profile)
    test = prepare_connection_test(profile, cost_text='0.001')
    assert test.text in test.review and profile.endpoint in test.review
    assert 'USD 0.001' in test.review and 'synthetic-setting-key' not in test.review
    with pytest.raises(ValueError, match='Review'):
        run_reviewed_connection_test(model.store, test, approved_fingerprint='wrong',
                                     vault=vault, _transport=transport)
    assert transport.requests == []
    result = run_reviewed_connection_test(model.store, test, approved_fingerprint=test.fingerprint,
                                          vault=vault, _transport=transport)
    assert result['text'] == 'connected'
    assert transport.requests[0][2]['messages'] == [{'role': 'user', 'content': test.text}]
    assert transport.requests[0][2]['max_tokens'] == 64
    with pytest.raises(ProviderError, match='already attempted'):
        run_reviewed_connection_test(model.store, test, approved_fingerprint=test.fingerprint,
                                     vault=vault, _transport=transport)
    assert len(transport.requests) == 1


def test_changed_profile_invalidates_prepared_test_before_secret_lookup(tmp_path):
    vault, transport = Vault(), Transport()
    model = ProviderSettingsModel(tmp_path / 'library', vault=vault)
    profile = save(model, secret='synthetic-setting-key')
    test = prepare_connection_test(profile, cost_text='0.001')
    save(model, current=profile, name='Edited after review')
    with pytest.raises(ValueError, match='changed after review'):
        run_reviewed_connection_test(model.store, test, approved_fingerprint=test.fingerprint,
                                     vault=vault, _transport=transport)
    assert not transport.requests and not vault.reads


def test_local_profile_uses_zero_budget_and_no_key(tmp_path):
    vault, transport = Vault(), Transport()
    model = ProviderSettingsModel(tmp_path / 'library', vault=vault)
    profile = save(model, local=True)
    test = prepare_connection_test(profile)
    assert test.reserved_microunits == 0
    run_reviewed_connection_test(model.store, test, approved_fingerprint=test.fingerprint,
                                 vault=vault, _transport=transport)
    assert transport.requests[0][0].local and not vault.reads
