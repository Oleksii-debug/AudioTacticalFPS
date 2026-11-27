"""Exact Google wire contract and real local budgets; no external requests."""
from dataclasses import replace
import json
import socket
from unittest.mock import Mock

import pytest

from carr_library import providers
from carr_library.providers import (BudgetLedger, DirectTransport, GOOGLE_TRANSLATE_ENDPOINT,
    JobApproval, ProfileStore, ProviderError, ProviderGateway, ProviderProfile, ProviderRequest,
    translation_result)


class MemoryVault:
    def __init__(self):
        self.values = {}
    def put(self, ref, binding, secret):
        self.values[ref, binding] = secret
    def get(self, ref, binding):
        return self.values[ref, binding]
    def delete(self, ref, binding):
        self.values.pop((ref, binding), None)


class ControlledTransport:
    def __init__(self):
        self.calls = []
        self.response = b'{"data":{"translations":[{"translatedText":"Synthetic derivative","model":"nmt"}]}}'
        self.failure = None
    def post(self, profile, route, payload, headers, **kwargs):
        self.calls.append(('POST', profile, route, json.loads(payload), dict(headers)))
        if self.failure:
            raise self.failure
        return self.response
    def get(self, profile, route, parameters, headers, **kwargs):
        self.calls.append(('GET', profile, route, dict(parameters), dict(headers)))
        return b'{"data":{"languages":[{"language":"en","name":"English"},{"language":"uk","name":"Ukrainian"}]}}'


@pytest.fixture
def gateway(tmp_path):
    vault, transport = MemoryVault(), ControlledTransport()
    store = ProfileStore(tmp_path/'api-connections.json', vault)
    profile = store.save(ProviderProfile('translation', 'Translation', 'google-translate',
        GOOGLE_TRANSLATE_ENDPOINT, 'nmt', ('translation', 'translation_languages')),
        secret='synthetic-key-not-a-real-credential')
    ledger = BudgetLedger(tmp_path/'api-operations.sqlite')
    return ProviderGateway(ledger, vault=vault, transport=transport), profile, transport, ledger


def approval(profile, request, **changes):
    return JobApproval(**{'job_id':'reviewed', 'profile_fingerprint':profile.fingerprint,
        'request_hashes':(request.fingerprint(profile),), 'budget_micros':100,
        'mode':'APPROVED_REMOTE', **changes})


@pytest.mark.parametrize('source', [None, 'en'])
def test_exact_text_and_language_options_have_one_documented_post(gateway, source):
    host, profile, transport, ledger = gateway
    request = ProviderRequest('translation', 'Exact 😀\r\n[execute] <tag> & literal',
                              source_language=source, target_language='uk')
    output = host.perform(profile, request, approval=approval(profile, request),
                          operation_id='chunk-0', estimated_micros=100)
    method, selected, route, body, headers = transport.calls[0]
    expected = {'q':request.text, 'target':'uk', 'format':'text', 'model':'nmt'}
    if source is not None:
        expected['source'] = source
    assert (method, selected, route, body) == ('POST', profile, '', expected)
    assert headers == {'x-goog-api-key':'synthetic-key-not-a-real-credential'}
    assert translation_result(output, 'translation')['translatedText'] == 'Synthetic derivative'
    assert ledger.report('reviewed')[0]['state'] == 'complete'


def test_language_inventory_uses_get_and_zero_estimate_with_exact_approval(gateway):
    host, profile, transport, ledger = gateway
    request = ProviderRequest('translation_languages', '', target_language='en')
    output = host.perform(profile, request, approval=approval(profile, request, budget_micros=0),
                          operation_id='languages', estimated_micros=0)
    assert transport.calls[0][0:4] == ('GET', profile, '/languages', {'target':'en', 'model':'nmt'})
    assert [row['language'] for row in translation_result(output, request.capability)] == ['en', 'uk']


@pytest.mark.parametrize('change', ['text', 'target', 'source', 'profile', 'local-only'])
def test_changed_approval_never_reads_key_or_sends_request(gateway, change, monkeypatch):
    host, profile, transport, ledger = gateway
    request = ProviderRequest('translation', 'Reviewed exact text', target_language='uk')
    approved = approval(profile, request)
    if change == 'text': request = replace(request, text='Other text')
    elif change == 'target': request = replace(request, target_language='sk')
    elif change == 'source': request = replace(request, source_language='en')
    elif change == 'profile': profile = replace(profile, name='Other account')
    else: approved = replace(approved, mode='LOCAL_ONLY')
    monkeypatch.setattr(host.vault, 'get', lambda *args: pytest.fail('Unapproved key access'))
    with pytest.raises(ProviderError):
        host.perform(profile, request, approval=approved, operation_id='denied', estimated_micros=1)
    assert transport.calls == [] and ledger.report('reviewed') == []


@pytest.mark.parametrize('response', [b'{"data":{"translations":[]}}',
    b'{"data":{"translations":[{"translatedText":"x","model":"other"}]}}',
    b'{"data":{"translations":[{"translatedText":"x","execute":"shell"}]}}',
    b'{"data":{"translations":[{"translatedText":"x","translatedText":"y"}]}}'])
def test_invalid_response_is_durable_uncertain_and_never_retried(gateway, response):
    host, profile, transport, ledger = gateway
    request = ProviderRequest('translation', 'Reviewed text', target_language='uk')
    transport.response = response
    for _ in range(2):
        with pytest.raises(ProviderError):
            host.perform(profile, request, approval=approval(profile, request),
                         operation_id='uncertain', estimated_micros=50)
    assert len(transport.calls) == 1 and ledger.report('reviewed')[0]['state'] == 'uncertain'


@pytest.mark.parametrize('changes', [dict(endpoint='https://translation.googleapis.com.evil.example/language/translate/v2'),
    dict(endpoint=GOOGLE_TRANSLATE_ENDPOINT+'?key=secret'), dict(model='other'), dict(local=True),
    dict(capabilities=('llm',))])
def test_google_profile_cannot_route_a_key_to_another_host_or_model(gateway, changes):
    with pytest.raises(ProviderError):
        replace(gateway[1], **changes)


def test_real_transport_builds_get_query_with_key_only_in_header(gateway, monkeypatch):
    profile = gateway[1]
    response = Mock(status=200)
    response.getheader.return_value = None
    response.read.side_effect = [b'{"data":{"languages":[{"language":"en","name":"English"}]}}', b'']
    connection = Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(providers.socket, 'getaddrinfo', lambda *args, **kw:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(providers.http.client, 'HTTPConnection', lambda *args, **kw: connection)
    monkeypatch.setattr(providers.ssl, 'SSLContext', lambda *args: Mock())
    DirectTransport().get(profile, '/languages', {'target':'en', 'model':'nmt'}, {'x-goog-api-key':'synthetic-key'})
    args, kwargs = connection.request.call_args
    assert args == ('GET', '/language/translate/v2/languages?target=en&model=nmt')
    assert kwargs['body'] is None and kwargs['headers']['x-goog-api-key'] == 'synthetic-key'
    assert kwargs['headers']['Accept'] == 'application/json'
    assert 'synthetic-key' not in args[1]
    connection.close.assert_called_once()


def test_default_requests_retain_exact_pretranslation_fingerprints():
    # Captured from 618c847 before adding the two optional request fields.
    cases = [
        (ProviderProfile('writer','Writer','openai-compatible','https://example.com/v1','chosen',('llm',)),
         ProviderRequest('llm','Text 😀\r\nSecond line'), '585918eb9548a3dea655b4ee2683f16d85875f4e51142d3816410da9ae92936b'),
        (ProviderProfile('voice','Voice','elevenlabs','https://api.elevenlabs.io/v1','chosen',('tts',)),
         ProviderRequest('tts','Text','voice-id'), '07d2ab65ff8817f2cbdd0e9c3938475897923dcfb6236ca47417841a3c56d34b'),
        (ProviderProfile('music','Music','elevenlabs','https://api.elevenlabs.io/v1','chosen',('music',)),
         ProviderRequest('music','Original instrumental',duration_seconds=10), 'ceb198e3ea82a02873cba0f1078cf96eb6bb8ba09bb7dc2a7044d3d68cd270d7'),
    ]
    for profile, request, expected in cases:
        assert request.fingerprint(profile) == expected
