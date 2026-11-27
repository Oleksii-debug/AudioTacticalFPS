"""Synthetic Google Basic v2 boundary checks. No sockets, keys or paid requests."""
from dataclasses import asdict, replace
from contextlib import closing
from hashlib import sha256
import html
import json
import socket
import ssl
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from carr_library import providers
from carr_library.providers import (
    BudgetLedger, DirectTransport, GoogleTranslationRequest, JobApproval, ProfileStore,
    ProviderError, ProviderGateway, ProviderProfile, ProviderRequest,
    GOOGLE_TRANSLATION_ENDPOINT, decode_translation_response)

SECRET = 'synthetic-translation-test-key-never-real'


class Vault:
    def __init__(self):
        self.values, self.reads = {}, []
        self.on_get = lambda: None

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        self.reads.append((reference, binding))
        self.on_get()
        return self.values[reference, binding]

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls, self.error, self.custom = [], None, None

    def get(self, profile, route, headers, **kwargs):
        self.calls.append(('GET', profile, route, None, dict(headers)))
        if self.error:
            raise self.error
        return self.custom or json.dumps({'data': {'languages': [
            {'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}}).encode()

    def post(self, profile, route, body, headers, **kwargs):
        self.calls.append(('POST', profile, route, body, dict(headers)))
        if self.error:
            raise self.error
        return self.custom or json.dumps({'data': {'translations': [
            {'translatedText': 'Synthetic translated text', 'detectedSourceLanguage': 'en', 'model': 'nmt'}]}}).encode()


def profile(**changes):
    values = dict(id='translation-account', name='Synthetic translation account',
                  provider='google-translation-v2', endpoint=GOOGLE_TRANSLATION_ENDPOINT,
                  model='nmt', capabilities=('translation',))
    values.update(changes)
    return ProviderProfile(**values)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('Unexpected real DNS lookup'))
    vault, transport = Vault(), Transport()
    store = ProfileStore(tmp_path / 'api-connections.json', vault)
    selected = store.save(profile(), secret=SECRET)
    ledger = BudgetLedger(tmp_path / 'api-operations.sqlite')
    gateway = ProviderGateway(ledger, vault=vault, transport=transport)
    return selected, vault, transport, ledger, gateway


def approval(selected, request, **changes):
    values = dict(job_id='a' * 32, profile_fingerprint=selected.fingerprint,
                  request_hashes=(request.fingerprint(selected),), budget_micros=50,
                  max_calls=1, mode='APPROVED_REMOTE')
    values.update(changes)
    return JobApproval(**values)


@pytest.mark.parametrize('change', [
    {'endpoint': 'https://translation.googleapis.com.evil.test/language/translate/v2'},
    {'endpoint': GOOGLE_TRANSLATION_ENDPOINT + '?key=secret'},
    {'endpoint': GOOGLE_TRANSLATION_ENDPOINT + '/'},
    {'endpoint': 'https://translation.googleapis.com/v3'},
    {'endpoint': 'http://translation.googleapis.com/language/translate/v2'},
    {'endpoint': 'https://translation.googleapis.com:444/language/translate/v2'},
    {'model': 'advanced-v3'}, {'model': 'base'}, {'local': True}, {'capabilities': ('llm',)},
], ids=['wrong-host', 'query-key', 'trailing-route', 'advanced-route', 'http', 'port',
        'advanced-model', 'fallback-model', 'loopback', 'capability'])
def test_profile_pins_official_v2_authority(change):
    with pytest.raises(ProviderError):
        profile(**change)


def test_default_local_only_rejects_before_vault_or_transport(setup):
    selected, vault, transport, ledger, gateway = setup
    request = GoogleTranslationRequest('Synthetic source', 'en', 'uk')
    with pytest.raises(ProviderError, match='LOCAL ONLY'):
        gateway.perform(selected, request, approval=approval(selected, request, mode='LOCAL_ONLY'),
                        operation_id='first', estimated_micros=50)
    assert not vault.reads and not transport.calls and not ledger.report('a' * 32)


def test_language_query_sends_no_selected_text_and_key_is_header_only(setup, tmp_path):
    selected, vault, transport, ledger, gateway = setup
    request = GoogleTranslationRequest(operation='languages')
    raw = gateway.perform(selected, request, approval=approval(selected, request, budget_micros=0),
                          operation_id='languages', estimated_micros=0)
    method, actual_profile, route, body, headers = transport.calls[0]
    assert method == 'GET' and body is None
    assert route == '/languages?target=en&model=nmt'
    assert actual_profile.endpoint == GOOGLE_TRANSLATION_ENDPOINT
    assert headers == {'X-goog-api-key': SECRET}
    assert SECRET not in route
    assert decode_translation_response(request, raw)['languages'][1]['language'] == 'uk'
    assert SECRET not in (tmp_path / 'api-connections.json').read_text()
    assert len(vault.reads) == 1
    assert ledger.report('a' * 32)[0]['state'] == 'complete'
    assert ledger.report('a' * 32)[0]['decoded_translation_sha256'] is None


def test_translation_uses_exact_plaintext_pair_and_model(setup):
    selected, _, transport, ledger, gateway = setup
    request = GoogleTranslationRequest('Text <tag> & literal.', 'en', 'uk')
    raw = gateway.perform(selected, request, approval=approval(selected, request),
                          operation_id='first', estimated_micros=50)
    method, _, route, body, headers = transport.calls[0]
    assert method == 'POST' and route == ''
    assert json.loads(body) == {'q': request.text, 'source': 'en', 'target': 'uk', 'format': 'text', 'model': 'nmt'}
    assert SECRET not in body.decode()
    assert headers == {'X-goog-api-key': SECRET}
    assert ledger.report('a' * 32)[0]['output_sha256'] == sha256(raw).hexdigest()


@pytest.mark.parametrize('change', [
    {'text': 'Changed data'}, {'source': 'auto'}, {'target': 'sl'},
], ids=['text', 'source', 'target'])
def test_changed_request_cannot_borrow_prior_approval(setup, change):
    selected, vault, transport, ledger, gateway = setup
    request = GoogleTranslationRequest('Original approved text', 'en', 'uk')
    with pytest.raises(ProviderError, match='outside the approved'):
        gateway.perform(selected, replace(request, **change), approval=approval(selected, request),
                        operation_id='first', estimated_micros=50)
    assert not vault.reads and not transport.calls and not ledger.report('a' * 32)


@pytest.mark.parametrize('case', ['blank', 'nul', 'large', 'surrogate', 'same-pair', 'invalid-source', 'discovery-text'])
def test_request_rejects_unbounded_or_ambiguous_inputs(case):
    values = dict(text='source', source='en', target='uk')
    values.update({
        'blank': {'text': '   '}, 'nul': {'text': 'source\0'}, 'large': {'text': 'x' * 5001},
        'surrogate': {'text': '\ud800'}, 'same-pair': {'target': 'en'},
        'invalid-source': {'source': 'en?key=x'},
        'discovery-text': {'operation': 'languages', 'source': 'auto', 'target': 'en'},
    }[case])
    with pytest.raises(ProviderError):
        GoogleTranslationRequest(**values)


@pytest.mark.parametrize('encoding', ['plain', 'json-unicode', 'html', 'nested-html', 'deep-html'])
@pytest.mark.parametrize('operation', ['text', 'languages'])
def test_reflected_credentials_never_become_results_or_receipts(setup, encoding, operation):
    selected, _, transport, ledger, gateway = setup
    encoded = SECRET
    if encoding in ('html', 'nested-html', 'deep-html'):
        encoded = ''.join('&#' + str(ord(char)) + ';' for char in SECRET)
        if encoding == 'nested-html':
            encoded = html.escape(encoded)
        elif encoding == 'deep-html':
            for _ in range(7):
                encoded = html.escape(encoded)
    if operation == 'languages':
        request = GoogleTranslationRequest(operation='languages')
        data = {'data': {'languages': [{'language': 'en', 'name': encoded}]}}
    else:
        request = GoogleTranslationRequest('Synthetic source', 'en', 'uk')
        data = {'data': {'translations': [{'translatedText': encoded}]}}
    raw = json.dumps(data).encode()
    if encoding == 'json-unicode':
        raw = raw.replace(SECRET.encode(), ''.join('\\u%04x' % ord(c) for c in SECRET).encode())
    transport.custom = raw
    with pytest.raises(ProviderError) as caught:
        gateway.perform(selected, request, approval=approval(selected, request),
                        operation_id='first', estimated_micros=50)
    assert SECRET not in str(caught.value) and caught.value.__cause__ is None
    row = ledger.report('a' * 32)[0]
    assert row['state'] == 'uncertain' and row['output_sha256'] is None
    assert row['decoded_translation_sha256'] is None
    with pytest.raises(ProviderError, match='already recorded'):
        gateway.perform(selected, request, approval=approval(selected, request),
                        operation_id='first', estimated_micros=50)
    assert len(transport.calls) == 1


@pytest.mark.parametrize('data', [
    {'data': {'translations': []}},
    {'data': {'translations': [{'translatedText': 'x', 'model': 'advanced-v3'}]}},
    {'data': {'translations': [{'translatedText': 'x', 'tool_calls': [{'execute': 'shell'}]}]}},
    {'data': {'translations': [{'translatedText': ''}]}},
    {'data': {'translations': [{'translatedText': '&#32;'}]}},
    {'error': {'message': 'secret response body'}},
], ids=['missing-output', 'wrong-model', 'commands', 'empty-output', 'encoded-empty-output', 'provider-error'])
def test_malformed_responses_are_uncertain_and_not_displayed(setup, data):
    selected, _, transport, ledger, gateway = setup
    transport.custom = json.dumps(data).encode()
    request = GoogleTranslationRequest('Synthetic source', 'en', 'uk')
    with pytest.raises(ProviderError) as caught:
        gateway.perform(selected, request, approval=approval(selected, request),
                        operation_id='first', estimated_micros=50)
    assert 'secret response body' not in str(caught.value)
    assert ledger.report('a' * 32)[0]['state'] == 'uncertain'


def test_known_unsent_cancellation_reuses_only_exact_original_reservation(setup):
    selected, vault, transport, ledger, gateway = setup
    request = GoogleTranslationRequest('Synthetic source', 'en', 'uk')
    permit = approval(selected, request)
    stop = Event()
    vault.on_get = stop.set
    with pytest.raises(ProviderError, match='No request was sent'):
        gateway.perform(selected, request, approval=permit, operation_id='first',
                        estimated_micros=50, cancelled=stop.is_set)
    assert not transport.calls and ledger.report(permit.job_id)[0]['state'] == 'cancelled_before_send'
    for changed in (replace(permit, budget_micros=100), replace(permit, max_calls=2)):
        with pytest.raises(ProviderError):
            ledger.reserve('first', changed, request.fingerprint(selected), 50)
    with pytest.raises(ProviderError):
        ledger.reserve('first', permit, request.fingerprint(selected), 49)
    stop.clear()
    vault.on_get = lambda: None
    gateway.perform(selected, request, approval=permit, operation_id='first',
                    estimated_micros=50, cancelled=stop.is_set)
    assert len(transport.calls) == 1 and ledger.report(permit.job_id)[0]['state'] == 'complete'
    with pytest.raises(ProviderError, match='already recorded'):
        gateway.perform(selected, request, approval=permit, operation_id='first', estimated_micros=50)
    assert len(transport.calls) == 1


def test_existing_provider_request_hash_is_backward_compatible():
    selected = ProviderProfile('local', 'Local', 'openai-compatible',
                               'http://127.0.0.1:8080/v1', 'model', ('llm',), local=True)
    request = ProviderRequest('llm', 'Previously approved exact text')
    original = {'profile': selected.fingerprint, 'request': {
        'capability': 'llm', 'text': request.text, 'voice': None, 'duration_seconds': None}}
    raw = json.dumps(original, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    assert request.fingerprint(selected) == sha256(raw).hexdigest()
    assert set(asdict(request)) == {'capability', 'text', 'voice', 'duration_seconds'}


def test_direct_language_transport_keeps_tls_and_exact_get_authority(monkeypatch):
    selected = profile()
    response = Mock(status=200)
    raw = b'{"data":{"languages":[{"language":"en","name":"English"}]}}'
    response.getheader.return_value = str(len(raw))
    response.read.side_effect = [raw, b'']
    connection = Mock()
    connection.sock = Mock()
    plain_socket = connection.sock
    connection.getresponse.return_value = response
    monkeypatch.setattr(providers.socket, 'getaddrinfo', lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('142.250.190.78', 443))])
    monkeypatch.setattr(providers.http.client, 'HTTPConnection', Mock(return_value=connection))
    contexts = []
    original = ssl.SSLContext
    def context(protocol):
        actual = original(protocol)
        actual.wrap_socket = Mock(return_value=Mock())
        contexts.append(actual)
        return actual
    monkeypatch.setattr(providers, 'ssl', SimpleNamespace(PROTOCOL_TLS_CLIENT=ssl.PROTOCOL_TLS_CLIENT,
                                                       SSLContext=context))
    result = DirectTransport().get(selected, '/languages?target=en&model=nmt',
                                   {'X-goog-api-key': SECRET})
    assert result == raw
    args, kwargs = connection.request.call_args
    assert args == ('GET', '/language/translate/v2/languages?target=en&model=nmt')
    assert kwargs['body'] is None and kwargs['headers']['Host'] == 'translation.googleapis.com'
    assert kwargs['headers']['Accept'] == 'application/json'
    contexts[0].wrap_socket.assert_called_once_with(plain_socket, server_hostname='translation.googleapis.com')
    assert contexts[0].check_hostname and contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].keylog_filename is None
    connection.close.assert_called_once()


@pytest.mark.parametrize('route', ['/languages?target=en&model=nmt&key=secret', '/v3', '/languages/../x'])
def test_direct_get_route_is_not_general_url_capability(route):
    with pytest.raises(ProviderError, match='route'):
        DirectTransport().get(profile(), route, {})


@pytest.mark.parametrize('detected', [None, 'en'], ids=['explicit-source', 'detected-source'])
def test_gateway_binds_decoded_translation_and_language_in_live_ledger(setup, detected):
    selected, _, transport, ledger, gateway = setup
    request = GoogleTranslationRequest('Synthetic source', 'en', 'uk')
    row = {'translatedText': 'A &amp; B &#x1F642;', 'model': 'nmt'}
    if detected is not None:
        row['detectedSourceLanguage'] = detected
    transport.custom = json.dumps({'data': {'translations': [row]}}).encode()
    raw = gateway.perform(selected, request, approval=approval(selected, request),
                          operation_id='decoded', estimated_micros=50)
    record = ledger.report('a' * 32)[0]
    expected = sha256(json.dumps({'text': 'A & B 🙂', 'detected_source': detected},
                                ensure_ascii=False, sort_keys=True,
                                separators=(',', ':')).encode('utf-8')).hexdigest()
    assert record['state'] == 'complete'
    assert record['output_sha256'] == sha256(raw).hexdigest()
    assert record['decoded_translation_sha256'] == expected != record['output_sha256']
    with closing(sqlite3.connect(ledger.path)) as database, database:
        dump = '\n'.join(database.iterdump())
    assert SECRET not in dump and 'A & B' not in dump and request.text not in dump


def test_existing_wire_only_ledger_migration_is_concurrent_and_never_backfills_proof(tmp_path):
    path = tmp_path / 'old-operations.sqlite'
    original = ('accepted', 'old-job', 'a' * 64, 7, 'complete', 'b' * 64)
    with closing(sqlite3.connect(path)) as database, database:
        database.executescript(
            'CREATE TABLE approval (id TEXT PRIMARY KEY, digest TEXT NOT NULL);'
            'CREATE TABLE operation (id TEXT PRIMARY KEY, job TEXT NOT NULL, request_hash TEXT NOT NULL,'
            'estimate_micros INTEGER NOT NULL, state TEXT NOT NULL, output_sha256 TEXT);')
        database.execute('INSERT INTO approval VALUES (?, ?)', ('old-job', 'c' * 64))
        database.execute('INSERT INTO operation VALUES (?, ?, ?, ?, ?, ?)', original)

    with ThreadPoolExecutor(max_workers=2) as executor:
        opened = list(executor.map(BudgetLedger, [path, path]))
    for ledger in opened + [BudgetLedger(path)]:
        rows = ledger.report('old-job')
        assert len(rows) == 1
        assert tuple(rows[0][key] for key in (
            'id', 'job', 'request_hash', 'estimate_micros', 'state', 'output_sha256')) == original
        assert rows[0]['decoded_translation_sha256'] is None
    with closing(sqlite3.connect(path)) as database, database:
        columns = [row[1] for row in database.execute('PRAGMA table_info(operation)')]
        assert columns.count('decoded_translation_sha256') == 1
        assert database.execute('SELECT * FROM approval').fetchall() == [('old-job', 'c' * 64)]
