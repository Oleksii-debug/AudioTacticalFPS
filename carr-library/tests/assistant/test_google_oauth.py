"""Real gateway/job/ledger contracts; only HTTPS and the vault are controlled."""
from dataclasses import replace
from decimal import Decimal
import http.client
import json
import ssl
from time import time
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import pytest

from carr_library.assistant import google_oauth as oauth
from carr_library.assistant import providers
from carr_library.assistant.google_oauth import (
    OAuthRefreshError, credential_summary, parse_refresh_credential,
    refresh_credential, save_google_refresh,
)
from carr_library.assistant.profiles import (
    ProviderProfile, ProfileStore, google_oauth_credential, save_google_token,
)
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.translation import TranslationService, translation_approval

ENDPOINT = 'https://translation.googleapis.com/v3/projects/123456789/locations/global'
CLIENT = '123456789-synthetic.apps.googleusercontent.com'
CLIENT_SECRET = 'SYNTHETIC_DESKTOP_SECRET_123'
REFRESH = 'SYNTHETIC_REFRESH_GRANT_123'
ACCESS = 'SYNTHETIC_ACCESS_TOKEN_123'
SCOPE = 'https://www.googleapis.com/auth/cloud-platform'


def profile(**changes):
    values = dict(id='renewal_account', name='Synthetic renewal account',
        adapter='google-translation-v3', endpoint=ENDPOINT, model='general/nmt',
        capabilities=('translation.text', 'translation.languages'),
        credential_ref='carr/provider/renewal_account/' + 'a' * 32, enabled=True)
    return ProviderProfile(**{**values, **changes})


class Vault:
    def __init__(self):
        self.values = {profile().credential_ref: refresh_credential(CLIENT, CLIENT_SECRET, REFRESH, ENDPOINT)}
        self.calls = []
        self.after_get = lambda: None

    def get(self, identity, reference):
        self.calls.append(('get', identity, reference))
        value = self.values[reference]
        self.after_get()
        return value

    def put(self, identity, value, *, reference=None):
        reference = reference or 'carr/provider/' + identity + '/' + uuid4().hex
        self.calls.append(('put', identity, reference))
        self.values[reference] = value
        return reference

    def delete(self, identity, reference):
        self.calls.append(('delete', identity, reference))
        self.values.pop(reference, None)


@pytest.fixture
def case(tmp_path, monkeypatch):
    state = SimpleNamespace(calls=[], connections=[], now=1000.0,
        token_status=200, api_status=200, token_type='application/json',
        token_length=None, token_raw=None, token_data=None,
        after_token_request=lambda: None, after_token_read=lambda: None)
    monkeypatch.setattr(oauth, 'monotonic', lambda: state.now)

    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host in ('oauth2.googleapis.com', 'translation.googleapis.com')
            assert port in (None, 443) and isinstance(kwargs['context'], ssl.SSLContext)
            assert 0 < kwargs['timeout'] <= 30
            self.host, self.position, self.closed = host, 0, False
            state.connections.append(self)

        def request(self, method, route, body=None, headers=None):
            state.calls.append(dict(host=self.host, method=method, route=route,
                                    body=body, headers=dict(headers)))
            if self.host == 'oauth2.googleapis.com':
                assert method == 'POST' and route == '/token'
                self.status = state.token_status
                self.content_type = state.token_type
                data = state.token_data
                if data is None:
                    data = {'access_token': ACCESS, 'expires_in': 3600,
                            'token_type': 'Bearer', 'scope': SCOPE}
                self.raw = state.token_raw if state.token_raw is not None else json.dumps(data).encode()
                state.after_token_request()
            else:
                self.status, self.content_type = state.api_status, 'application/json'
                if method == 'GET':
                    data = {'languages': [
                        {'languageCode': code, 'displayName': code,
                         'supportSource': True, 'supportTarget': True}
                        for code in ('en', 'uk')]}
                else:
                    payload = json.loads(body)
                    data = {'translations': [{'translatedText': 'Переклад ' + payload['contents'][0],
                                               'model': payload['model']}]}
                self.raw = json.dumps(data).encode()

        def getresponse(self):
            return self

        def getheader(self, name, default=None):
            length = (state.token_length if self.host == 'oauth2.googleapis.com'
                      and state.token_length is not None else str(len(self.raw)))
            return {'Content-Type': self.content_type, 'Content-Length': length}.get(name, default)

        def read(self, size):
            block = self.raw[self.position:self.position + min(size, 97)]
            self.position += len(block)
            if self.host == 'oauth2.googleapis.com':
                state.after_token_read()
            return block

        def close(self):
            self.closed = True

    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    state.vault = Vault()
    state.profile = profile()
    state.gateway = Gateway(tmp_path / 'gateway', vault=state.vault, mode='APPROVED_REMOTE')
    state.directory = tmp_path
    return state


def approval(p, *, operation='translation.text', **changes):
    identity = uuid4().hex
    return Authorization(**dict(id=identity, task_id=identity,
        profiles=((p.id, p.endpoint),), operations=(operation,),
        data_classes=('selected-excerpt',), budget='1', max_calls=2,
        expires_at=time() + 300, **changes))


def request(case, *, grant=None, identity=None, cancelled=lambda: False):
    grant = grant or approval(case.profile)
    operation_id = identity or uuid4().hex
    value = case.gateway.request(case.profile, 'translation.text',
        {'q': 'Selected source Україна.', 'source': 'en', 'target': 'uk'},
        task_id=grant.task_id, data_class='selected-excerpt',
        authorization=grant, max_cost='0.1', operation_id=operation_id,
        cancelled=cancelled)
    return value, grant, operation_id


def token_calls(case):
    return [call for call in case.calls if call['host'] == 'oauth2.googleapis.com']


def api_calls(case):
    return [call for call in case.calls if call['host'] == 'translation.googleapis.com']


def test_actual_gateway_refresh_is_fixed_authority_and_contains_no_document(case):
    result, grant, identity = request(case)
    token, = token_calls(case)
    assert parse_qs(token['body'].decode(), strict_parsing=True) == {
        'grant_type': ['refresh_token'], 'client_id': [CLIENT],
        'client_secret': [CLIENT_SECRET], 'refresh_token': [REFRESH]}
    assert token['route'] == '/token' and 'Authorization' not in token['headers']
    assert token['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    assert b'Selected' not in token['body']
    api, = api_calls(case)
    assert api['headers']['Authorization'] == 'Bearer ' + ACCESS
    assert REFRESH not in json.dumps(api, default=str) and CLIENT_SECRET not in json.dumps(api, default=str)
    assert result['text'].startswith('Переклад')
    row = case.gateway.ledger.get(grant.id)['operations'][identity]
    assert row['status'] == 'complete'
    assert all(connection.closed for connection in case.connections)
    for path in case.directory.rglob('*.json'):
        value = path.read_text()
        assert all(secret not in value for secret in (CLIENT_SECRET, REFRESH, ACCESS))


@pytest.mark.parametrize('mode', ['LOCAL_ONLY', 'INVALID'])
def test_local_only_or_invalid_mode_opens_no_vault_or_token_connection(case, mode):
    case.gateway.mode = mode
    with pytest.raises(PermissionError):
        request(case)
    assert not case.vault.calls and not case.calls


def test_invalid_approval_or_unknown_budget_never_refreshes(case):
    grant = replace(approval(case.profile), profiles=(('different', ENDPOINT),))
    with pytest.raises(PermissionError):
        request(case, grant=grant)
    valid = approval(case.profile)
    with pytest.raises(PermissionError, match='cost is unknown'):
        case.gateway.request(case.profile, 'translation.text', {'q': 'Source', 'target': 'uk'},
            task_id=valid.task_id, data_class='selected-excerpt', authorization=valid)
    assert not case.calls and not case.vault.calls


def test_cancel_after_vault_before_refresh_retains_not_sent(case):
    stopped = []
    case.vault.after_get = lambda: stopped.append(True)
    grant = approval(case.profile)
    identity = uuid4().hex
    with pytest.raises(InterruptedError):
        request(case, grant=grant, identity=identity, cancelled=lambda: bool(stopped))
    assert not case.calls and len(case.vault.calls) == 1
    assert case.gateway.ledger.get(grant.id)['operations'][identity]['status'] == 'not_sent'


@pytest.mark.parametrize('boundary', ['request', 'read'])
def test_cancel_during_refresh_cannot_send_document_or_cache_token(case, boundary):
    stopped = []
    setattr(case, 'after_token_' + boundary, lambda: stopped.append(True))
    grant = approval(case.profile)
    identity = uuid4().hex
    with pytest.raises(InterruptedError):
        request(case, grant=grant, identity=identity, cancelled=lambda: bool(stopped))
    assert len(token_calls(case)) == 1 and not api_calls(case)
    assert not case.gateway._google_tokens._tokens
    assert all(connection.closed for connection in case.connections)
    assert case.gateway.ledger.get(grant.id)['operations'][identity]['status'] == 'not_sent'


def test_approval_expiring_during_refresh_stops_before_document(case, monkeypatch):
    grant = approval(case.profile)
    case.after_token_read = lambda: monkeypatch.setattr(providers, 'time', lambda: grant.expires_at + 1)
    with pytest.raises(PermissionError, match='expired'):
        request(case, grant=grant)
    assert len(token_calls(case)) == 1 and not api_calls(case)


def test_same_total_deadline_covers_token_response(case):
    case.after_token_read = lambda: setattr(case, 'now', case.now + 121)
    with pytest.raises(InterruptedError, match='time budget'):
        request(case)
    assert len(token_calls(case)) == 1 and not api_calls(case)
    assert all(connection.closed for connection in case.connections)


def test_cache_requires_fresh_vault_and_renews_before_monotonic_expiry(case):
    request(case)
    request(case)
    assert len(token_calls(case)) == 1 and len(api_calls(case)) == 2
    assert len(case.vault.calls) == 2
    case.now += 3541
    request(case)
    assert len(token_calls(case)) == 2 and len(api_calls(case)) == 3
    assert all(row[0] == 'get' for row in case.vault.calls)


def test_changed_or_removed_vault_credential_cannot_reuse_cached_access(case):
    request(case)
    case.vault.values[case.profile.credential_ref] = refresh_credential(CLIENT, CLIENT_SECRET, REFRESH + '_NEW', ENDPOINT)
    request(case)
    assert len(token_calls(case)) == 2
    assert parse_qs(token_calls(case)[-1]['body'].decode())['refresh_token'] == [REFRESH + '_NEW']
    case.vault.values.pop(case.profile.credential_ref)
    with pytest.raises(ProviderError):
        request(case)
    assert len(token_calls(case)) == 2 and len(api_calls(case)) == 2


def test_endpoint_mismatch_refuses_even_with_fresh_cached_access(case):
    request(case)
    record = json.loads(case.vault.values[case.profile.credential_ref])
    record['endpoint'] = ENDPOINT.replace('123456789', '234567890')
    case.vault.values[case.profile.credential_ref] = json.dumps(record)
    with pytest.raises(ProviderError, match='endpoint'):
        request(case)
    assert len(token_calls(case)) == len(api_calls(case)) == 1


@pytest.mark.parametrize('status', [301, 302, 400, 401, 403, 429, 500])
def test_refresh_http_failures_are_redacted_not_sent_and_not_retried(case, status):
    case.token_status = status
    case.token_data = {'error': CLIENT_SECRET + REFRESH}
    grant = approval(case.profile)
    identity = uuid4().hex
    with pytest.raises(ProviderError) as raised:
        request(case, grant=grant, identity=identity)
    assert CLIENT_SECRET not in str(raised.value) and REFRESH not in str(raised.value)
    assert len(token_calls(case)) == 1 and not api_calls(case)
    assert case.gateway.ledger.get(grant.id)['operations'][identity]['status'] == 'not_sent'
    assert all(connection.closed for connection in case.connections)


@pytest.mark.parametrize('change', [
    {'expires_in': True}, {'expires_in': 60}, {'expires_in': 43201},
    {'expires_in': '3600'}, {'token_type': 'DPoP'}, {'access_token': 'AIzaSyntheticKey'},
    {'access_token': 'header\r\ninjection'}, {'access_token': REFRESH},
    {'scope': 'https://www.googleapis.com/auth/drive'}, {'scope': ['not a scope']},
    {'refresh_token': 'unexpected_rotated_refresh'}, {'token_uri': 'https://other.invalid/token'},
])
def test_malformed_token_metadata_never_authorizes_translation(case, change):
    case.token_data = {'access_token': ACCESS, 'expires_in': 3600, 'token_type': 'Bearer', **change}
    with pytest.raises(ProviderError):
        request(case)
    assert len(token_calls(case)) == 1 and not api_calls(case)


@pytest.mark.parametrize('kind', ['duplicate', 'oversize', 'truncated', 'content-type', 'empty', 'invalid-json'])
def test_token_stream_bounds_and_duplicate_fields_are_enforced(case, kind):
    if kind == 'duplicate':
        case.token_raw = b'{"access_token":"SYNTHETIC_FIRST","access_token":"SYNTHETIC_SECOND","expires_in":3600,"token_type":"Bearer"}'
    elif kind == 'oversize':
        case.token_raw = b'x' * 32769
    elif kind == 'truncated':
        case.token_length = '999'
    elif kind == 'content-type':
        case.token_type = 'text/html'
    elif kind == 'empty':
        case.token_raw = b''
    else:
        case.token_raw = (CLIENT_SECRET + REFRESH).encode()
    with pytest.raises(ProviderError) as raised:
        request(case)
    assert all(secret not in str(raised.value) for secret in (CLIENT_SECRET, REFRESH))
    assert len(token_calls(case)) == 1 and not api_calls(case)
    assert all(connection.closed for connection in case.connections)


def test_sent_translation_failure_remains_uncertain_without_refresh_or_paid_retry(case):
    case.api_status = 500
    grant = approval(case.profile)
    identity = uuid4().hex
    with pytest.raises(ProviderError, match='Charge state'):
        request(case, grant=grant, identity=identity)
    assert case.gateway.ledger.get(grant.id)['operations'][identity]['status'] == 'uncertain'
    with pytest.raises(ProviderError, match='already been attempted'):
        request(case, grant=grant, identity=identity)
    assert len(token_calls(case)) == len(api_calls(case)) == 1


def test_legacy_manual_access_token_remains_unrenewed(case):
    case.vault.values[case.profile.credential_ref] = google_oauth_credential(ACCESS, time() + 600, ENDPOINT)
    request(case)
    assert not token_calls(case) and len(api_calls(case)) == 1
    expired = json.loads(case.vault.values[case.profile.credential_ref])
    expired['expires_at'] = time() - 1
    case.vault.values[case.profile.credential_ref] = json.dumps(expired)
    with pytest.raises(ProviderError, match='expired'):
        request(case)
    assert not token_calls(case) and len(api_calls(case)) == 1


def test_configuration_and_export_are_local_and_preserve_existing_profile(case):
    store = ProfileStore(case.directory / 'profiles')
    store.save(case.profile)
    before = store.path(case.profile.id).read_bytes()
    saved = save_google_refresh(store, case.vault, case.profile, CLIENT, CLIENT_SECRET, REFRESH, current=case.profile)
    assert saved == case.profile and store.path(saved.id).read_bytes() == before
    assert not case.calls and case.vault.calls[-1][0] == 'put'
    summary = credential_summary(case.vault.values[saved.credential_ref], ENDPOINT)
    assert 'Automatic Google renewal' in summary
    assert all(value not in summary for value in (CLIENT, CLIENT_SECRET, REFRESH))
    exported = store.export_json()
    assert all(value not in exported for value in (CLIENT, CLIENT_SECRET, REFRESH, ACCESS, 'carr/provider/'))


def test_stale_profile_cannot_replace_refresh_grant(case):
    store = ProfileStore(case.directory / 'profiles')
    store.save(replace(case.profile, name='Newer account settings'))
    before = dict(case.vault.values)
    with pytest.raises(ValueError, match='account changed'):
        save_google_refresh(store, case.vault, case.profile, CLIENT, CLIENT_SECRET, REFRESH + '_NEW', current=case.profile)
    assert case.vault.values == before and not case.vault.calls and not case.calls


def test_new_reference_is_rolled_back_when_profile_save_fails(case, monkeypatch):
    store = ProfileStore(case.directory / 'profiles')
    p = profile(credential_ref=None)
    store.save(p)
    before = dict(case.vault.values)
    monkeypatch.setattr(store, 'save', lambda value: (_ for _ in ()).throw(OSError('synthetic save refusal')))
    with pytest.raises(OSError, match='synthetic'):
        save_google_refresh(store, case.vault, p, CLIENT, CLIENT_SECRET, REFRESH, current=p)
    assert case.vault.values == before
    assert [row[0] for row in case.vault.calls] == ['put', 'delete']


@pytest.mark.parametrize('change', [
    {'client_id': 'https://other.invalid/client'}, {'client_secret': 'contains space'},
    {'refresh_token': 'short'}, {'refresh_token': 'x' * 1801},
    {'endpoint': ENDPOINT.replace('googleapis.com', 'other.invalid')},
])
def test_explicit_refresh_configuration_rejects_unreviewed_shapes(change):
    args = dict(client_id=CLIENT, client_secret=CLIENT_SECRET, refresh_token=REFRESH, endpoint=ENDPOINT)
    args.update(change)
    with pytest.raises(ValueError):
        refresh_credential(args['client_id'], args['client_secret'], args['refresh_token'], args['endpoint'])


def test_absent_client_secret_is_not_sent_as_an_empty_parameter(case):
    case.vault.values[case.profile.credential_ref] = refresh_credential(CLIENT, '', REFRESH, ENDPOINT)
    request(case)
    assert 'client_secret' not in parse_qs(token_calls(case)[0]['body'].decode(), keep_blank_values=True)


def test_actual_job_resumes_after_refresh_failure_without_rebilling_completed_prefix(case):
    service = TranslationService(case.directory / 'translation', gateway=case.gateway)
    p = case.profile
    grant = Authorization('language_review', 'language_review', ((p.id, p.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 300)
    service.refresh_languages(p, authorization=grant)
    value = 'Unchanged source paragraph. ' * 550
    approved, cost = translation_approval(p, value, 'en', 'uk', '1')
    identity = service.submit(p, value, 'en', 'uk', authorization=approved, per_call_cost=cost)
    stopped = []
    prefix = service.resume(identity, cancelled=lambda: bool(stopped), progress=lambda *args: stopped.append(True))
    assert prefix['completed'] == 1
    paid_before = len(api_calls(case))
    case.now += 3541
    case.token_status = 400
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert len(api_calls(case)) == paid_before
    assert len(service.jobs.load(identity)['receipts']) == 1
    case.token_status = 200
    result = service.resume(identity)
    assert result['status'] == 'complete'
    assert len(api_calls(case)) - paid_before == result['total'] - 1
    rows = service.gateway.ledger.get(approved.id)['operations']
    assert all(row['status'] == 'complete' for row in rows.values())
    assert sum(Decimal(row['reserved_cost']) for row in rows.values()) <= Decimal('1')
    before = len(case.calls)
    service.resume(identity)
    assert len(case.calls) == before
