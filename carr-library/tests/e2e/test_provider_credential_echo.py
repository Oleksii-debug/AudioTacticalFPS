"""Credential reflection must never become a saved provider result.

All credentials are synthetic. The real durable ledger is exercised through
an explicit transport double. The local controls prove credential-free routing
and admission without sockets; the separate allowlisted loopback suite retains
its existing actual HTTP coverage.
"""
from contextlib import closing
import json
import sqlite3
from time import time
from types import SimpleNamespace

import pytest

from carr_library import providers as api
from carr_library.ui.api_connections import ConnectionsController


SECRET = 'synthetic-key-material-DO-NOT-SAVE'


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        return self.values[reference, binding]

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.response = b''
        self.calls = 0
        self.requests = []

    def post(self, profile, route, payload, headers, **kwargs):
        self.calls += 1
        self.requests.append((route, dict(headers), payload))
        return self.response


def task(root, kind):
    root.mkdir(parents=True, exist_ok=True)
    vault = MemoryVault()
    if kind in ('v2', 'v3'):
        advanced = kind == 'v3'
        provider = 'google-translation-v3' if advanced else 'google-translation-v2'
        endpoint = ('https://translation.googleapis.com/v3/projects/123/locations/global'
                    if advanced else api.GOOGLE_TRANSLATION_ENDPOINT)
        profile = api.ProviderProfile('account', 'Synthetic translation', provider, endpoint,
            'general/nmt' if advanced else 'nmt', ('translation',))
        request = (api.GoogleAdvancedTranslationRequest if advanced else api.GoogleTranslationRequest)(
            text='Selected synthetic text.', source='en', target='uk')
        credential = api.google_oauth_credential(SECRET, time() + 600, endpoint) if advanced else SECRET
    else:
        audio = kind != 'llm'
        profile = api.ProviderProfile('account', 'Synthetic account',
            'elevenlabs' if audio else 'openai-compatible',
            'https://api.elevenlabs.io/v1' if audio else 'https://example.com/v1',
            'synthetic-model', (kind,))
        request = api.ProviderRequest(kind, 'Selected synthetic text.',
            voice='synthetic-voice' if kind == 'tts' else None,
            duration_seconds=3 if kind in ('music', 'sound_effects') else None)
        credential = SECRET
    profile = api.ProfileStore(root / 'api-connections.json', vault).save(profile, secret=credential)
    approval = api.JobApproval('job', profile.fingerprint, (request.fingerprint(profile),),
        budget_micros=20, max_calls=2, mode='APPROVED_REMOTE')
    ledger = api.BudgetLedger(root / 'ledger.sqlite')
    transport = Transport()
    gateway = api.ProviderGateway(ledger, vault=vault, transport=transport)
    return SimpleNamespace(root=root, profile=profile, request=request, approval=approval,
        ledger=ledger, vault=vault, transport=transport, gateway=gateway)


def invoke(value):
    return value.gateway.perform(value.profile, value.request, approval=value.approval,
        operation_id='attempt', estimated_micros=10)


def encoded(document, encoding):
    raw = json.dumps(document, ensure_ascii=False)
    if encoding == 'unicode':
        raw = raw.replace(SECRET, ''.join('\\u%04x' % ord(char) for char in SECRET))
    elif encoding == 'html':
        raw = raw.replace(SECRET, ''.join('&#%d;' % ord(char) for char in SECRET))
    return raw.encode('utf-8')


def refused(value):
    with pytest.raises(api.ProviderError) as error:
        invoke(value)
    assert SECRET not in str(error.value) and error.value.__cause__ is None
    rows = value.ledger.report('job')
    assert len(rows) == 1 and rows[0]['state'] == 'uncertain'
    assert rows[0]['output_sha256'] is None and rows[0]['decoded_translation_sha256'] is None
    assert SECRET not in json.dumps(rows)
    with pytest.raises(api.ProviderError, match='already recorded'):
        invoke(value)
    assert value.transport.calls == 1


@pytest.mark.parametrize('encoding', ['raw', 'unicode', 'html'])
@pytest.mark.parametrize('where', ['content', 'metadata'])
def test_llm_reflection_is_rejected_before_any_completed_receipt(tmp_path, encoding, where):
    value = task(tmp_path, 'llm')
    body = {'choices': [{'message': {'content': SECRET if where == 'content' else 'Ordinary answer.'}}]}
    if where == 'metadata':
        body['metadata'] = {'nested': [{'source': SECRET}]}
    value.transport.response = encoded(body, encoding)
    refused(value)


@pytest.mark.parametrize('kind', ['tts', 'music', 'sound_effects'])
def test_binary_audio_credential_reflection_cannot_complete(tmp_path, kind):
    value = task(tmp_path, kind)
    value.transport.response = b'ID3synthetic-audio\0' + SECRET.encode() + b'\0end'
    refused(value)


@pytest.mark.parametrize('kind', ['v2', 'v3'])
@pytest.mark.parametrize('encoding', ['raw', 'unicode', 'html'])
def test_translation_reflection_remains_fail_closed(tmp_path, kind, encoding):
    value = task(tmp_path, kind)
    if kind == 'v2':
        body = {'data': {'translations': [{'translatedText': SECRET, 'detectedSourceLanguage': 'en'}]}}
    else:
        body = {'translations': [{'translatedText': SECRET,
            'model': 'projects/123/locations/global/models/general/nmt', 'detectedLanguageCode': 'en'}]}
    value.transport.response = encoded(body, encoding)
    refused(value)


@pytest.mark.parametrize('kind', ['llm', 'tts'])
def test_native_task_controller_never_publishes_reflected_credentials(tmp_path, kind):
    value = task(tmp_path / 'private', kind)
    value.transport.response = (encoded({'choices': [{'message': {'content': SECRET}}]}, 'unicode')
        if kind == 'llm' else b'ID3' + SECRET.encode())
    controller = ConnectionsController(value.root, vault=value.vault, transport=value.transport)
    target = tmp_path / ('result.txt' if kind == 'llm' else 'result.mp3')
    with pytest.raises(api.ProviderError) as error:
        controller.run(value.profile, value.request, target, approved=True, estimate_micros=10)
    assert SECRET not in str(error.value)
    assert not target.exists() and not list(tmp_path.glob('.provider-result-*'))
    with closing(sqlite3.connect(value.root / 'api-operations.sqlite')) as db:
        assert db.execute('SELECT state,output_sha256 FROM operation').fetchall() == [('uncertain', None)]
    assert value.transport.calls == 1


@pytest.mark.parametrize('kind', ['llm', 'translation'])
def test_local_requests_without_credentials_remain_available(tmp_path, monkeypatch, kind):
    response = json.dumps(
        {'choices': [{'message': {'content': 'Synthetic local answer &#65;.'}}]}
        if kind == 'llm' else {'translatedText': 'Synthetic local translation &#65;.'}).encode()

    def no_vault(*args, **kwargs):
        raise AssertionError('The selected local request must not consult a credential vault.')

    monkeypatch.setattr(api, 'WindowsCredentialVault', no_vault)
    origin = 'http://127.0.0.1:54321'
    profile = api.ProviderProfile('local', 'Selected local service',
        'openai-compatible' if kind == 'llm' else api.LOCAL_TRANSLATION_PROVIDER,
        origin + '/v1' if kind == 'llm' else origin, 'reviewed-local-runtime',
        ('llm',) if kind == 'llm' else ('translation',), local=True)
    request = (api.ProviderRequest('llm', 'Synthetic local input.') if kind == 'llm'
        else api.LocalTranslationRequest(text='Synthetic local input.', source='en', target='uk'))
    approval = api.JobApproval('local-job', profile.fingerprint, (request.fingerprint(profile),),
        mode='LOCAL_ONLY', allow_loopback=True)
    ledger = api.BudgetLedger(tmp_path / 'local.sqlite')
    transport = Transport()
    transport.response = response
    assert api.ProviderGateway(ledger, transport=transport).perform(
        profile, request, approval=approval, operation_id='local-request', estimated_micros=0) == response
    assert transport.calls == 1
    route, headers, payload = transport.requests[0]
    assert route == ('/chat/completions' if kind == 'llm' else '/translate')
    assert headers == {}
    assert json.loads(payload)['model' if kind == 'llm' else 'source'] == (
        'reviewed-local-runtime' if kind == 'llm' else 'en')
    assert ledger.report('local-job')[0]['state'] == 'complete'


@pytest.mark.parametrize('encoding', ['unicode', 'html'])
@pytest.mark.parametrize('where', ['metadata', 'content'])
def test_duplicate_json_cannot_hide_an_escaped_credential(tmp_path, encoding, where):
    value = task(tmp_path, 'llm')
    if where == 'metadata':
        raw = '{"choices":[{"message":{"content":"Ordinary answer."}}],"metadata":"' + SECRET + '","metadata":"ordinary"}'
    else:
        raw = '{"choices":[{"message":{"content":"' + SECRET + '","content":"Ordinary answer."}}]}'
    replacement = (''.join('\\u%04x' % ord(char) for char in SECRET) if encoding == 'unicode'
        else ''.join('&#%d;' % ord(char) for char in SECRET))
    value.transport.response = raw.replace(SECRET, replacement).encode('utf-8')
    assert SECRET.encode() not in value.transport.response
    refused(value)


@pytest.mark.parametrize('kind', ['v2', 'v3'])
@pytest.mark.parametrize('encoding', ['unicode', 'html'])
def test_unknown_google_metadata_cannot_escape_as_completed_raw_bytes(tmp_path, kind, encoding):
    value = task(tmp_path, kind)
    if kind == 'v2':
        body = {'data': {'translations': [{'translatedText': 'Ordinary translation.'}]}}
    else:
        body = {'translations': [{'translatedText': 'Ordinary translation.',
            'model': 'projects/123/locations/global/models/general/nmt'}]}
    body['metadata'] = {'discarded': [SECRET]}
    value.transport.response = encoded(body, encoding)
    assert SECRET.encode() not in value.transport.response
    refused(value)
