"""Advanced v3 protocol/authentication checks with synthetic responses only."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from carr_library import providers
from carr_library.providers import (
    BudgetLedger, DirectTransport, GoogleAdvancedTranslationRequest, GoogleTranslationRequest,
    JobApproval, ProfileStore, ProviderError, ProviderGateway, ProviderProfile,
    GOOGLE_TRANSLATION_ENDPOINT, decode_translation_response, google_oauth_credential,
    google_oauth_token, google_v3_parent, translation_request)

PARENT = 'projects/123456789/locations/us-central1'
ENDPOINT = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/reviewed-terms'
TOKEN = 'SYNTHETIC_OAUTH_TOKEN_NEVER_REAL'
MODEL = PARENT + '/models/general/nmt'


class Vault:
    def __init__(self):
        self.values, self.reads, self.writes = {}, [], []
        self.on_get = lambda: None
    def put(self, reference, binding, value):
        self.writes.append((reference, binding))
        self.values[reference, binding] = value
    def get(self, reference, binding):
        self.reads.append((reference, binding))
        self.on_get()
        return self.values[reference, binding]
    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls, self.custom = [], None
    def get(self, profile, route, headers, **options):
        self.calls.append(('GET', route, None, dict(headers), options))
        return self.custom if self.custom is not None else json.dumps({'languages': [
            {'languageCode': 'en', 'displayName': 'English', 'supportSource': True},
            {'languageCode': 'uk', 'displayName': 'Українська', 'supportTarget': True}]}).encode()
    def post(self, profile, route, raw, headers, **options):
        self.calls.append(('POST', route, raw, dict(headers), options))
        if self.custom is not None:
            return self.custom
        request = json.loads(raw)
        row = {'translatedText': 'Похідний текст &amp; ', 'model': request['model']}
        response = {'translations': [row]}
        if 'glossaryConfig' in request:
            response['glossaryTranslations'] = [dict(row, glossaryConfig=request['glossaryConfig'])]
        return json.dumps(response, ensure_ascii=False).encode()


def profile(**changes):
    values = dict(id='google-v3', name='Synthetic IAM account', provider='google-translation-v3',
                  endpoint=ENDPOINT, model='general/nmt', capabilities=('translation',))
    values.update(changes)
    return ProviderProfile(**values)


def approval(selected, request, **changes):
    values = dict(job_id='a' * 32, profile_fingerprint=selected.fingerprint,
                  request_hashes=(request.fingerprint(selected),), budget_micros=50,
                  max_calls=1, mode='APPROVED_REMOTE')
    values.update(changes)
    return JobApproval(**values)


@pytest.fixture
def clock(monkeypatch):
    value = [1_800_000_000.0]
    monkeypatch.setattr(providers, 'time', lambda: value[0])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: pytest.fail('Unexpected real DNS'))
    return value


@pytest.fixture
def ready(tmp_path, clock):
    vault, transport = Vault(), Transport()
    store = ProfileStore(tmp_path / 'api-connections.json', vault)
    selected = store.save_google_token(profile(), TOKEN, clock[0] + 1800)
    ledger = BudgetLedger(tmp_path / 'api-operations.sqlite')
    gateway = ProviderGateway(ledger, vault=vault, transport=transport)
    return selected, store, vault, transport, ledger, gateway


@pytest.mark.parametrize('change', [
    {'endpoint': ENDPOINT.replace('translation.googleapis.com', 'translation.googleapis.com.evil.test')},
    {'endpoint': ENDPOINT.replace('https:', 'http:')},
    {'endpoint': ENDPOINT + '/'},
    {'endpoint': ENDPOINT + '?key=secret'},
    {'endpoint': ENDPOINT.replace('123456789', 'named-project')},
    {'endpoint': ENDPOINT.replace('123456789', '0123456789')},
    {'endpoint': ENDPOINT.replace('123456789', '0')},
    {'endpoint': ENDPOINT.replace('googleapis.com', 'googleapis.com:444')},
    {'endpoint': ENDPOINT.replace('us-central1', '../global')},
    {'model': 'general/translation-llm'}, {'capabilities': ('llm',)}, {'local': True},
], ids=['host', 'http', 'slash', 'query', 'named-project', 'leading-zero', 'zero',
        'port', 'path', 'uninstalled-model', 'capability', 'local'])
def test_profile_pins_explicit_google_project_location_and_nmt(change):
    with pytest.raises(ProviderError):
        profile(**change)


@pytest.mark.parametrize('case', ['api-key', 'json-file', 'whitespace', 'expired', 'no-offset',
                                  'nan', 'infinite', 'boolean-expiry', 'too-far', 'oversized'])
def test_credentials_are_explicit_bounded_and_unexpired(clock, case):
    token, expiry = TOKEN, clock[0] + 1800
    if case == 'api-key':
        token = 'AIza' + 'A' * 35
    elif case == 'json-file':
        token = '{"private_key":"not-a-token"}'
    elif case == 'whitespace':
        token += '\r\n'
    elif case == 'expired':
        expiry = clock[0]
    elif case == 'no-offset':
        expiry = '2027-01-15T12:00:00'
    elif case == 'nan':
        expiry = float('nan')
    elif case == 'infinite':
        expiry = float('inf')
    elif case == 'boolean-expiry':
        expiry = True
    elif case == 'too-far':
        expiry = clock[0] + 43201
    else:
        token *= 200
    with pytest.raises(ProviderError):
        google_oauth_credential(token, expiry, ENDPOINT)


def test_expiry_offset_and_exact_endpoint_record(clock):
    expiry = clock[0] + 1800
    stamp = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
    record = google_oauth_credential(TOKEN, stamp, ENDPOINT)
    assert google_oauth_token(record, ENDPOINT) == (TOKEN, expiry)
    with pytest.raises(ProviderError):
        google_oauth_token(record, ENDPOINT.replace('us-central1', 'global'))
    duplicated = record[:-1] + ',"expires_at":' + str(expiry) + '}'
    with pytest.raises(ProviderError):
        google_oauth_token(duplicated, ENDPOINT)


def test_v3_store_rejects_raw_key_without_mutating_profiles(tmp_path, clock):
    store = ProfileStore(tmp_path / 'api-connections.json', Vault())
    with pytest.raises(ProviderError):
        store.save(profile(), secret='AIza' + 'A' * 35)
    assert not store.path.exists() and not store.vault.values


def test_basic_v2_request_and_profile_hash_contract_stays_exact():
    selected = ProviderProfile('basic', 'Basic', 'google-translation-v2',
                               GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',))
    request = translation_request(selected, 'Previously approved text', 'en', 'uk')
    assert type(request) is GoogleTranslationRequest
    assert set(asdict(selected)) == {'id', 'name', 'provider', 'endpoint', 'model',
                                    'capabilities', 'credential_ref', 'local'}
    expected = {'profile': selected.fingerprint, 'request': {
        'text': 'Previously approved text', 'source': 'en', 'target': 'uk', 'operation': 'text'}}
    raw = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    assert request.fingerprint(selected) == sha256(raw).hexdigest()
    with pytest.raises(ProviderError):
        translation_request(selected, 'Text', 'en', 'uk', glossary=GLOSSARY)


def test_same_account_token_renewal_preserves_exact_approval_reference(ready, clock, monkeypatch):
    selected, store, vault, _, _, _ = ready
    original = store.path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Same-account renewal must not rewrite profile JSON.')
    monkeypatch.setattr(providers, '_atomic_json', forbidden)
    renewed = store.save_google_token(selected, TOKEN + '_NEW', clock[0] + 3600,
                                     renew_same_account=True)
    assert renewed == selected and renewed.fingerprint == selected.fingerprint
    assert store.path.read_bytes() == original
    assert google_oauth_token(vault.get(selected.credential_ref, selected.credential_binding),
                              selected.endpoint)[0] == TOKEN + '_NEW'


def test_changed_account_or_profile_requires_new_authority(ready, clock):
    selected, store, vault, _, _, _ = ready
    before = len(vault.writes)
    with pytest.raises(ProviderError, match='exact unchanged'):
        store.save_google_token(replace(selected, name='Changed'), TOKEN, clock[0] + 1800,
                                renew_same_account=True)
    assert len(vault.writes) == before
    replacement = store.save_google_token(selected, TOKEN + '_OTHER', clock[0] + 1800)
    assert replacement.credential_ref != selected.credential_ref
    assert replacement.fingerprint != selected.fingerprint
    assert (selected.credential_ref, selected.credential_binding) not in vault.values


def test_language_query_has_no_source_text_and_preserves_directional_support(ready):
    selected, store, vault, transport, ledger, gateway = ready
    request = translation_request(selected, operation='languages')
    permit = approval(selected, request, budget_micros=0)
    raw = gateway.perform(selected, request, approval=permit, operation_id='languages', estimated_micros=0)
    method, route, body, headers, options = transport.calls[0]
    assert method == 'GET' and body is None
    assert route == '/supportedLanguages?' + urlencode({'displayLanguageCode': 'en', 'model': MODEL})
    assert headers == {'Authorization': 'Bearer ' + TOKEN, 'X-goog-user-project': '123456789'}
    assert 'credential_expires_at' in options and 'expires_at' not in headers
    assert TOKEN not in route and TOKEN not in store.path.read_text(encoding='utf-8')
    assert 'carr-google-oauth' not in store.path.read_text(encoding='utf-8')
    rows = decode_translation_response(request, raw, profile=selected)['languages']
    assert rows == [{'language': 'en', 'name': 'English', 'source': True, 'target': False},
                    {'language': 'uk', 'name': 'Українська', 'source': False, 'target': True}]
    assert ledger.report(permit.job_id)[0]['state'] == 'complete'
    assert len(vault.reads) == 1


def test_text_uses_exact_model_glossary_and_literal_entities(ready):
    selected, _, _, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source &amp; <tag>', 'en', 'uk', glossary=GLOSSARY)
    raw = gateway.perform(selected, request, approval=approval(selected, request),
                          operation_id='text', estimated_micros=50)
    method, route, body, headers, _ = transport.calls[0]
    assert method == 'POST' and route == ':translateText'
    assert json.loads(body) == {'contents': [request.text], 'sourceLanguageCode': 'en',
        'targetLanguageCode': 'uk', 'mimeType': 'text/plain', 'model': MODEL,
        'glossaryConfig': {'glossary': GLOSSARY, 'ignoreCase': False}}
    result = decode_translation_response(request, raw, profile=selected)
    assert result == {'text': 'Похідний текст &amp; ', 'detected_source': None,
                      'reported_model': MODEL, 'glossary': GLOSSARY}
    assert 'X-goog-api-key' not in headers and TOKEN not in body.decode()
    assert ledger.report('a' * 32)[0]['output_sha256'] == sha256(raw).hexdigest()


@pytest.mark.parametrize('case', ['local-only', 'different-glossary', 'different-region', 'basic-request'])
def test_changed_or_wrong_version_requests_stop_before_credentials_and_ledger(ready, case):
    selected, _, vault, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source', 'en', 'uk', glossary=GLOSSARY)
    permit = approval(selected, request)
    if case == 'local-only':
        permit = replace(permit, mode='LOCAL_ONLY')
    elif case == 'different-glossary':
        request = replace(request, glossary=GLOSSARY + '-changed')
    elif case == 'different-region':
        selected = replace(selected, endpoint=ENDPOINT.replace('us-central1', 'europe-west1'))
    else:
        request = GoogleTranslationRequest('Source', 'en', 'uk')
        permit = approval(selected, request)
    with pytest.raises(ProviderError):
        gateway.perform(selected, request, approval=permit, operation_id='text', estimated_micros=50)
    assert not vault.reads and not transport.calls and not ledger.report(permit.job_id)


@pytest.mark.parametrize('case', ['auto-source', 'global', 'other-project', 'other-region', 'traversal'])
def test_glossary_cannot_borrow_a_different_scope(case):
    selected, glossary, source = profile(), GLOSSARY, 'en'
    if case == 'auto-source':
        source = 'auto'
    elif case == 'global':
        selected = replace(selected, endpoint=ENDPOINT.replace('us-central1', 'global'))
        glossary = GLOSSARY.replace('us-central1', 'global')
    elif case == 'other-project':
        glossary = glossary.replace('123456789', '987654321')
    elif case == 'other-region':
        glossary = glossary.replace('us-central1', 'europe-west1')
    else:
        glossary += '/../other'
    with pytest.raises(ProviderError):
        translation_request(selected, 'Source', source, 'uk', glossary=glossary)


@pytest.mark.parametrize('case', ['missing-glossary', 'wrong-model', 'wrong-glossary', 'ignore-case',
                                  'commands', 'boolean-detection', 'basic-response', 'duplicate'])
def test_unverified_result_is_uncertain_and_never_retried(ready, case):
    selected, _, _, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source', 'en', 'uk', glossary=GLOSSARY)
    row = {'translatedText': 'Derivative', 'model': MODEL,
           'glossaryConfig': {'glossary': GLOSSARY, 'ignoreCase': False}}
    value = {'glossaryTranslations': [row]}
    if case == 'missing-glossary':
        value = {'translations': [{'translatedText': 'Unrequested fallback', 'model': MODEL}]}
    elif case == 'wrong-model':
        row['model'] = MODEL.replace('123456789', '987654321')
    elif case == 'wrong-glossary':
        row['glossaryConfig']['glossary'] += '-other'
    elif case == 'ignore-case':
        row['glossaryConfig']['ignoreCase'] = True
    elif case == 'commands':
        row['tool_calls'] = [{'execute': 'never'}]
    elif case == 'boolean-detection':
        row['detectedLanguageCode'] = False
    elif case == 'basic-response':
        value = {'data': {'translations': [{'translatedText': 'Unrequested v2 fallback'}]}}
    raw = json.dumps(value).encode()
    if case == 'duplicate':
        raw = raw.replace(b'"model":', b'"model":"duplicate","model":', 1)
    transport.custom = raw
    permit = approval(selected, request)
    with pytest.raises(ProviderError):
        gateway.perform(selected, request, approval=permit, operation_id='text', estimated_micros=50)
    assert ledger.report(permit.job_id)[0]['state'] == 'uncertain'
    with pytest.raises(ProviderError, match='already recorded'):
        gateway.perform(selected, request, approval=permit, operation_id='text', estimated_micros=50)
    assert len(transport.calls) == 1


@pytest.mark.parametrize('encoding', ['plain', 'json-escaped', 'html', 'unused-base-row'])
def test_reflected_oauth_material_is_never_accepted(ready, encoding):
    selected, _, _, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source', 'en', 'uk', glossary=GLOSSARY)
    value = TOKEN if encoding != 'html' else ''.join('&#%d;' % ord(char) for char in TOKEN)
    result = {'glossaryTranslations': [{'translatedText': value, 'model': MODEL,
               'glossaryConfig': {'glossary': GLOSSARY}}]}
    if encoding == 'unused-base-row':
        result['glossaryTranslations'][0]['translatedText'] = 'Safe selected text'
        result['translations'] = [{'translatedText': TOKEN, 'model': MODEL}]
    raw = json.dumps(result).encode()
    if encoding == 'json-escaped':
        raw = raw.replace(TOKEN.encode(), ''.join('\\u%04x' % ord(char) for char in TOKEN).encode())
    transport.custom = raw
    with pytest.raises(ProviderError) as caught:
        gateway.perform(selected, request, approval=approval(selected, request), operation_id='text',
                        estimated_micros=50)
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None
    assert ledger.report('a' * 32)[0]['state'] == 'uncertain'


def test_expired_before_send_is_unsent_and_exact_renewal_can_resume(ready, clock):
    selected, store, _, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source', 'en', 'uk')
    permit = approval(selected, request)
    clock[0] += 1800
    with pytest.raises(ProviderError, match='No request was sent'):
        gateway.perform(selected, request, approval=permit, operation_id='text', estimated_micros=50)
    assert not transport.calls and ledger.report(permit.job_id)[0]['state'] == 'cancelled_before_send'
    store.save_google_token(selected, TOKEN + '_NEW', clock[0] + 1800, renew_same_account=True)
    gateway.perform(selected, request, approval=permit, operation_id='text', estimated_micros=50)
    assert len(transport.calls) == 1 and ledger.report(permit.job_id)[0]['state'] == 'complete'


def test_expiry_after_vault_read_is_rechecked_before_transport(ready, clock):
    selected, _, _, transport, ledger, gateway = ready
    request = translation_request(selected, 'Source', 'en', 'uk')
    checks = []
    def cancelled():
        checks.append(True)
        if len(checks) == 2:
            clock[0] += 1801
        return False
    with pytest.raises(ProviderError, match='No request was sent'):
        gateway.perform(selected, request, approval=approval(selected, request), operation_id='text',
                        estimated_micros=50, cancelled=cancelled)
    assert len(checks) == 2 and not transport.calls
    assert ledger.report('a' * 32)[0]['state'] == 'cancelled_before_send'


@pytest.mark.parametrize('route', [':translateText?key=token', '/supportedLanguages?displayLanguageCode=en',
                                  '/supportedLanguages/../other'])
def test_direct_transport_cannot_be_a_general_url_route(clock, route):
    with pytest.raises(ProviderError, match='route'):
        DirectTransport().get(profile(), route, {}, credential_expires_at=clock[0] + 100)


@pytest.mark.parametrize('expires_during_response', [False, True], ids=['ordinary', 'expiry-after-send'])
def test_native_transport_boundary_keeps_tls_and_accepts_already_sent_response(clock, monkeypatch, expires_during_response):
    selected = profile()
    route = '/supportedLanguages?' + urlencode({'displayLanguageCode': 'en', 'model': MODEL})
    raw = b'{"languages":[{"languageCode":"uk","supportTarget":true}]}'
    response = Mock(status=200)
    response.getheader.side_effect = lambda name, default=None: {
        'Content-Length': str(len(raw)), 'Content-Type': 'application/json; charset=utf-8'}.get(name, default)
    response.read.side_effect = [raw, b'']
    connection = Mock()
    connection.sock = Mock()
    plain_socket = connection.sock
    connection.getresponse.return_value = response
    if expires_during_response:
        connection.request.side_effect = lambda *args, **kwargs: clock.__setitem__(0, clock[0] + 200)
    monkeypatch.setattr(providers.socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('142.250.190.78', 443))])
    monkeypatch.setattr(providers.http.client, 'HTTPConnection', Mock(return_value=connection))
    contexts, original = [], ssl.SSLContext
    def context(protocol):
        actual = original(protocol)
        actual.wrap_socket = Mock(return_value=Mock())
        contexts.append(actual)
        return actual
    monkeypatch.setattr(providers, 'ssl', SimpleNamespace(PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
                                                       SSLContext=context))
    result = DirectTransport().get(selected, route, {'Authorization': 'Bearer ' + TOKEN},
                                   credential_expires_at=clock[0] + 100)
    assert result == raw
    arguments, options = connection.request.call_args
    assert arguments == ('GET', '/v3/' + PARENT + route)
    assert options['body'] is None and options['headers']['Host'] == 'translation.googleapis.com'
    assert 'expires_at' not in options['headers']
    contexts[0].wrap_socket.assert_called_once_with(plain_socket, server_hostname='translation.googleapis.com')
    assert contexts[0].check_hostname and contexts[0].verify_mode == ssl.CERT_REQUIRED
    connection.close.assert_called_once()
