"""Provider boundary regressions; synthetic credentials and memory transport only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3
from types import SimpleNamespace
import urllib.error

import pytest

from carr_library import providers
from carr_library.providers import ProviderError, ProviderManager, ProviderProfile


SECRET = 'synthetic-unused-provider-key-742'


class Vault:
    def __init__(self):
        self.keys, self.calls = {}, []

    def put(self, reference, secret):
        self.calls.append(('put', reference))
        self.keys[reference] = secret

    def get(self, reference):
        self.calls.append(('get', reference))
        return self.keys.get(reference)

    def delete(self, reference):
        self.calls.append(('delete', reference))
        self.keys.pop(reference, None)


class Response:
    headers = {'Content-Type': 'application/json'}

    def __init__(self, body=b'{"ok":true}'):
        self.body, self.reads = body, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, count):
        self.reads.append(count)
        return self.body[:count]


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(requests=[], handlers=[], response=Response(), error=None)

    def build(*handlers):
        state.handlers.append(handlers)
        return SimpleNamespace(open=opened)

    def opened(request, timeout):
        state.requests.append((request, timeout))
        if state.error is not None:
            raise state.error
        return state.response

    # No test can construct a real opener, including negative-path failures.
    monkeypatch.setattr(providers.urllib.request, 'build_opener', build)
    return state


@pytest.fixture
def configured(tmp_path, transport):
    vault = Vault()
    manager = ProviderManager(tmp_path / 'settings', vault=vault)
    profile = ProviderProfile('fixture', 'Український профіль', 'llm', 'https://fixture.invalid/v1',
                              'místní-model', enabled=True, request_budget_cents=5, total_budget_cents=10)
    manager.save(profile, key=SECRET)
    vault.calls.clear()
    return manager, profile, vault


def review(manager, payload=None, cost=2):
    payload = {'input': 'Reviewed request text'} if payload is None else payload
    return manager.preview('fixture', payload, maximum_cost_cents=cost)


def send(manager, payload=None, cost=2, approval=None):
    payload = {'input': 'Reviewed request text'} if payload is None else payload
    approval = approval or review(manager, payload, cost)['approval_sha256']
    return manager.request('fixture', payload, approval_sha256=approval, maximum_cost_cents=cost)


def state(manager):
    with sqlite3.connect(manager.database) as db:
        return (db.execute('SELECT spent_cents FROM profiles WHERE id="fixture"').fetchone()[0],
                db.execute('SELECT state,reserved_cents FROM requests ORDER BY updated').fetchall())


def test_local_only_denies_before_any_secret_or_transport_lookup(configured, transport):
    manager, profile, vault = configured
    assert manager.mode == 'LOCAL_ONLY'
    assert review(manager)['mode'] == 'LOCAL_ONLY'
    with pytest.raises(ProviderError, match='LOCAL_ONLY'):
        send(manager)
    assert vault.calls == [] and transport.requests == [] and transport.handlers == []
    assert state(manager) == (0, [])


def test_disabled_provider_in_hybrid_still_denies_before_key_lookup(configured, transport):
    manager, profile, vault = configured
    manager.save(replace(profile, enabled=False))
    manager.set_mode('HYBRID')
    with pytest.raises(ProviderError, match='disabled'):
        send(manager)
    assert vault.calls == [] and transport.requests == []


def test_exact_unicode_payload_is_approved_and_secret_stays_out_of_sqlite(configured, transport, capsys):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    payload = {'input': 'Український текст. Slovenský príbeh. English story.'}
    result = send(manager, payload)
    request, timeout = transport.requests[0]
    assert json.loads(request.data) == payload
    assert request.get_header('Authorization') == 'Bearer ' + SECRET
    assert request.full_url == 'https://fixture.invalid/v1' and timeout == 60
    assert result['sha256'] == sha256(result['body']).hexdigest()
    assert state(manager) == (2, [('complete', 2)])
    assert SECRET.encode() not in manager.database.read_bytes()
    assert payload['input'].encode() not in manager.database.read_bytes()
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    proxy, redirects = transport.handlers[0]
    assert proxy.proxies == {} and isinstance(redirects, providers._NoRedirect)


@pytest.mark.parametrize('change', ['payload', 'endpoint', 'model', 'key'])
def test_changes_after_review_require_new_exact_approval(configured, transport, change):
    manager, profile, vault = configured
    manager.set_mode('HYBRID')
    payload = {'input': 'Reviewed request text'}
    approval = review(manager, payload)['approval_sha256']
    if change == 'payload':
        payload['input'] = 'Not part of the approved request'
    elif change == 'endpoint':
        manager.save(replace(profile, endpoint='https://other.invalid/v1'))
    elif change == 'model':
        manager.save(replace(profile, model='different-model'))
    else:
        manager.save(profile, key='different-synthetic-unused-key')
    vault.calls.clear()
    with pytest.raises(ProviderError, match='exact|changed'):
        send(manager, payload, approval=approval)
    assert vault.calls == [] and transport.requests == [] and state(manager) == (0, [])


def test_payload_callback_mutation_cannot_change_bytes_after_approval(configured, transport, monkeypatch):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    payload = {'messages': [{'content': 'Reviewed content'}]}
    approval = review(manager, payload)['approval_sha256']
    get = vault.get

    def mutate(reference):
        payload['messages'][0]['content'] = 'Mutated after approval'
        return get(reference)

    monkeypatch.setattr(vault, 'get', mutate)
    send(manager, payload, approval=approval)
    assert json.loads(transport.requests[0][0].data)['messages'][0]['content'] == 'Reviewed content'


def test_profile_aba_does_not_approve_one_endpoint_and_send_another(configured, transport, monkeypatch):
    manager, original, vault = configured
    manager.set_mode('HYBRID')
    other = replace(original, endpoint='https://reviewed-other.invalid/v1')
    manager.save(other)
    other_review = review(manager)
    manager.save(original)
    # Model the middle read seeing a concurrent A -> B -> A profile revision.
    monkeypatch.setattr(manager, 'preview', lambda *a, **kw: other_review)
    with pytest.raises(ProviderError, match='exact|changed'):
        send(manager, approval=other_review['approval_sha256'])
    assert vault.calls == [] and transport.requests == []


def test_same_approval_cannot_be_sent_or_reserved_twice(configured, transport):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    send(manager)
    with pytest.raises(ProviderError, match='already attempted'):
        send(manager)
    assert len(transport.requests) == 1 and len(vault.calls) == 1
    assert state(manager) == (2, [('complete', 2)])


def test_concurrent_same_approval_has_one_reservation_and_one_transport(configured, transport):
    manager, _, _ = configured
    manager.set_mode('HYBRID')
    approval = review(manager)['approval_sha256']

    def one():
        try:
            return send(manager, approval=approval)
        except ProviderError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: one(), range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ProviderError) for r in results) == 1
    assert len(transport.requests) == 1 and state(manager) == (2, [('complete', 2)])


def test_total_budget_is_checked_before_key_lookup_or_transport(configured, transport):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    send(manager, {'input': 'first'}, cost=5)
    send(manager, {'input': 'second'}, cost=5)
    vault.calls.clear()
    with pytest.raises(ProviderError, match='remaining total'):
        send(manager, {'input': 'third'}, cost=1)
    assert vault.calls == [] and len(transport.requests) == 2 and state(manager)[0] == 10


@pytest.mark.parametrize('cost', [-1, 6, True, 1.0])
def test_invalid_request_reservation_fails_without_secret_access(configured, transport, cost):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    with pytest.raises(ProviderError, match='budget'):
        review(manager, cost=cost)
    assert vault.calls == [] and transport.requests == [] and state(manager) == (0, [])


@pytest.mark.parametrize('error', [urllib.error.URLError(SECRET), RuntimeError(SECRET),
                                  urllib.error.HTTPError('https://fixture.invalid', 401, SECRET, {}, None)])
def test_transport_errors_never_expose_key_and_never_automatically_retry(configured, transport, error):
    manager, _, _ = configured
    manager.set_mode('HYBRID')
    transport.error = error
    with pytest.raises(ProviderError) as caught:
        send(manager)
    assert SECRET not in str(caught.value)
    assert state(manager) == (2, [('needs_review', 2)])
    with pytest.raises(ProviderError, match='already attempted'):
        send(manager)
    assert len(transport.requests) == 1


def test_redirect_handler_denies_before_forwarding_credentials(configured, transport):
    manager, _, _ = configured
    manager.set_mode('HYBRID')
    send(manager)
    original = transport.requests[0][0]
    redirect = transport.handlers[0][1]
    with pytest.raises(ProviderError, match='redirects'):
        redirect.redirect_request(original, None, 302, 'Found', {}, 'https://unapproved.invalid/')
    assert len(transport.requests) == 1


def test_response_byte_budget_is_bounded_and_failure_receipt_retained(configured, transport):
    manager, profile, _ = configured
    manager.save(replace(profile, max_output_bytes=4))
    manager.set_mode('HYBRID')
    transport.response = Response(b'123456789')
    with pytest.raises(ProviderError, match='response budget'):
        send(manager)
    assert transport.response.reads == [5]
    assert state(manager) == (2, [('needs_review', 2)])


def test_utf8_input_budget_counts_bytes_instead_of_characters(configured, transport):
    manager, profile, vault = configured
    payload = {'input': 'Їжачок'}
    size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    manager.save(replace(profile, max_input_bytes=size))
    assert review(manager, payload)['input_bytes'] == size
    manager.save(replace(profile, max_input_bytes=size - 1))
    with pytest.raises(ProviderError, match='input budget'):
        review(manager, payload)
    assert vault.calls == [] and transport.requests == []


@pytest.mark.parametrize('payload', [
    {'api_key': SECRET}, {'settings': {'api_key': SECRET}},
    {'items': [{'Authorization': SECRET}]}, {'options': {' password ': SECRET}},
])
def test_nested_credential_fields_do_not_enter_request_data(configured, transport, payload):
    manager, _, vault = configured
    with pytest.raises(ProviderError, match='Credentials'):
        review(manager, payload)
    assert vault.calls == [] and transport.requests == []
    assert SECRET.encode() not in manager.database.read_bytes()


@pytest.mark.parametrize('endpoint', [
    'http://remote.invalid/v1', 'file:///private/file', 'https://user:pass@fixture.invalid',
    'https://@fixture.invalid', 'https://fixture.invalid:65536/v1', 'https://fixture.invalid:abc/v1',
    ' https://fixture.invalid/v1', 'https://fixture.invalid/\npath',
    'https://fixture.invalid/\tpath', 'https://fixture.invalid/path\x00',
    'https://fixture.invalid/path?api_key=oops', 'https://fixture.invalid/path#secret',
])
def test_endpoint_rejects_ambiguous_credentials_controls_and_ports(endpoint):
    with pytest.raises(ProviderError):
        ProviderProfile('fixture', 'Provider', 'llm', endpoint, 'model')


@pytest.mark.parametrize('endpoint', [
    'https://fixture.invalid/v1', 'http://localhost:8000/v1',
    'http://127.0.0.1:8080/v1', 'http://[::1]:8000/v1',
])
def test_explicit_https_or_loopback_endpoints_are_valid_without_network(endpoint, transport):
    profile = ProviderProfile('fixture', 'Профіль', 'llm', endpoint, 'model')
    assert profile.endpoint == endpoint and not profile.enabled
    assert transport.requests == []


def test_vault_put_error_cannot_expose_secret_to_settings_dialog(tmp_path, transport):
    class FailingVault(Vault):
        def put(self, reference, secret):
            raise RuntimeError('Backend reported the supplied key: ' + secret)
    manager = ProviderManager(tmp_path / 'settings', vault=FailingVault())
    profile = ProviderProfile('fixture', 'Provider', 'llm', 'https://fixture.invalid', 'model')
    with pytest.raises(ProviderError) as caught:
        manager.save(profile, key=SECRET)
    assert SECRET not in str(caught.value)
    assert manager.profiles() == () and transport.requests == []


def test_vault_get_error_is_redacted_even_if_backend_raises_provider_error(configured, transport, monkeypatch):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    def failed_get(reference):
        raise ProviderError('Unexpected backend failure with ' + SECRET)
    monkeypatch.setattr(vault, 'get', failed_get)
    with pytest.raises(ProviderError) as caught:
        send(manager)
    assert SECRET not in str(caught.value)
    assert transport.requests == [] and state(manager) == (2, [('needs_review', 2)])


def test_key_rotation_failure_keeps_previous_key_and_profile_usable(configured, transport, monkeypatch):
    manager, profile, vault = configured
    previous = dict(vault.keys)
    def failed_connect(*args, **kwargs):
        raise sqlite3.OperationalError('Synthetic settings transaction failure')
    with monkeypatch.context() as patch:
        patch.setattr(providers.sqlite3, 'connect', failed_connect)
        with pytest.raises(Exception, match='settings|transaction'):
            manager.save(replace(profile, model='unsaved-model'), key='synthetic-new-key')
    assert vault.keys == previous
    assert manager.profiles() == (profile,)
    manager.set_mode('HYBRID')
    send(manager)
    assert transport.requests[0][0].get_header('Authorization') == 'Bearer ' + SECRET


def foreign_reference(tmp_path):
    vault = Vault()
    first = ProviderManager(tmp_path / 'first', vault=vault)
    profile = ProviderProfile('fixture', 'Provider', 'llm', 'https://fixture.invalid/v1', 'model',
                              enabled=True, request_budget_cents=5, total_budget_cents=10)
    first.save(profile, key=SECRET)
    reference = next(iter(vault.keys))
    other = ProviderManager(tmp_path / 'second', vault=vault)
    other.save(profile)
    with sqlite3.connect(other.database) as db:
        db.execute('UPDATE profiles SET credential_ref=? WHERE id=?', (reference, profile.id))
    vault.calls.clear()
    return other, profile, vault, reference


def test_copied_settings_cannot_read_another_root_vault_reference(tmp_path, transport):
    manager, _, vault, reference = foreign_reference(tmp_path)
    manager.set_mode('HYBRID')
    with pytest.raises(ProviderError, match='credential|scope|settings'):
        send(manager)
    assert vault.calls == [] and transport.requests == []
    assert vault.keys[reference] == SECRET


def test_replacement_cannot_delete_another_settings_root_credential(tmp_path, transport):
    manager, profile, vault, reference = foreign_reference(tmp_path)
    with pytest.raises(ProviderError, match='credential|scope|settings'):
        manager.save(profile, key='new-synthetic-key')
    assert vault.keys[reference] == SECRET
    assert ('delete', reference) not in vault.calls
    assert transport.requests == []


def test_distinct_settings_roots_never_share_vault_identity(tmp_path, transport):
    vault = Vault()
    a = ProviderManager(tmp_path / 'Українські налаштування', vault=vault)
    b = ProviderManager(tmp_path / 'Slovenské nastavenia', vault=vault)
    profile = ProviderProfile('fixture', 'Shared name', 'tts', 'https://fixture.invalid/v1', 'voice')
    a.save(profile, key='synthetic-key-a')
    b.save(profile, key='synthetic-key-b')
    assert len(vault.keys) == 2
    assert a.credential_namespace != b.credential_namespace
    assert a.profiles() == b.profiles() == (profile,)
    assert transport.requests == []


def test_unicode_endpoint_is_canonical_before_review_and_transport(configured, transport):
    manager, profile, _ = configured
    profile = replace(profile, endpoint='https://приклад.invalid/голос')
    assert profile.endpoint.isascii() and '/%D0' in profile.endpoint
    manager.save(profile)
    manager.set_mode('HYBRID')
    approval = review(manager)
    assert approval['destination'] == profile.endpoint
    send(manager, approval=approval['approval_sha256'])
    assert transport.requests[0][0].full_url == profile.endpoint


def test_revocation_while_vault_prompt_is_open_prevents_transport(configured, transport, monkeypatch):
    manager, _, vault = configured
    manager.set_mode('HYBRID')
    get = vault.get
    def revoked(reference):
        manager.set_mode('LOCAL_ONLY')
        return get(reference)
    monkeypatch.setattr(vault, 'get', revoked)
    with pytest.raises(ProviderError, match='changed|revoked'):
        send(manager)
    assert transport.requests == [] and transport.handlers == []
    assert state(manager) == (2, [('needs_review', 2)])


def test_disable_is_reversible_and_keeps_key_and_reservation_history(configured, transport):
    manager, profile, vault = configured
    manager.set_mode('HYBRID')
    send(manager)
    keys = dict(vault.keys)
    vault.calls.clear()
    disabled = manager.disable(profile.id)
    assert not disabled.enabled and vault.keys == keys and vault.calls == []
    assert state(manager) == (2, [('complete', 2)])
    with pytest.raises(ProviderError, match='disabled'):
        send(manager, {'input': 'second'})
    manager.save(replace(disabled, enabled=True))
    send(manager, {'input': 'second'})
    assert state(manager)[0] == 4 and len(transport.requests) == 2


def test_remove_deletes_only_own_key_and_keeps_request_receipts(configured, transport):
    manager, profile, vault = configured
    manager.set_mode('HYBRID')
    send(manager)
    own_reference = next(iter(vault.keys))
    vault.keys['another-application-key'] = 'unrelated-synthetic-key'
    vault.calls.clear()
    assert manager.remove(profile.id) == {'profile_removed': True, 'credential_removed': True,
                                         'request_receipts_retained': True}
    assert manager.profiles() == ()
    assert vault.keys == {'another-application-key': 'unrelated-synthetic-key'}
    assert vault.calls == [('delete', own_reference)]
    with sqlite3.connect(manager.database) as db:
        assert db.execute('SELECT state,reserved_cents FROM requests').fetchall() == [('complete', 2)]
    assert len(transport.requests) == 1


@pytest.mark.parametrize('operation', ['disable', 'remove'])
def test_profile_removal_or_disable_cannot_touch_foreign_vault_reference(tmp_path, transport, operation):
    manager, profile, vault, reference = foreign_reference(tmp_path)
    with pytest.raises(ProviderError, match='credential|scope'):
        getattr(manager, operation)(profile.id)
    assert vault.calls == [] and vault.keys[reference] == SECRET
    assert manager.profiles() == (profile,)


def test_remove_reports_vault_cleanup_failure_without_false_success_or_secret(configured, transport, monkeypatch):
    manager, profile, vault = configured
    def failed_delete(reference):
        raise RuntimeError('Backend failure containing ' + SECRET)
    monkeypatch.setattr(vault, 'delete', failed_delete)
    result = manager.remove(profile.id)
    assert result['profile_removed'] is True and result['credential_removed'] is False
    assert SECRET not in str(result) and manager.profiles() == ()
    assert transport.requests == []
