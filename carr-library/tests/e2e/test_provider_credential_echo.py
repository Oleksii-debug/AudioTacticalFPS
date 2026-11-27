"""Credential reflection must never become a saved provider result.

All credentials are synthetic. Most cases exercise the real durable ledger
through an explicit transport double; the two local controls use actual HTTP
on an ephemeral loopback port and never consult a vault or external service.
"""
from dataclasses import asdict
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from threading import Thread
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

    def post(self, profile, route, payload, headers, **kwargs):
        self.calls += 1
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
def test_actual_local_http_without_credentials_remains_available(tmp_path, monkeypatch, kind):
    calls = []
    response = json.dumps(
        {'choices': [{'message': {'content': 'Synthetic local answer &#65;.'}}]}
        if kind == 'llm' else {'translatedText': 'Synthetic local translation &#65;.'}).encode()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers['Content-Length'])
            calls.append((self.path, dict(self.headers), self.rfile.read(size)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    def no_vault(*args, **kwargs):
        raise AssertionError('The selected local request must not consult a credential vault.')

    monkeypatch.setattr(api, 'WindowsCredentialVault', no_vault)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = 'http://127.0.0.1:' + str(server.server_port)
        profile = api.ProviderProfile('local', 'Selected local service',
            'openai-compatible' if kind == 'llm' else api.LOCAL_TRANSLATION_PROVIDER,
            origin + '/v1' if kind == 'llm' else origin, 'reviewed-local-runtime',
            ('llm',) if kind == 'llm' else ('translation',), local=True)
        request = (api.ProviderRequest('llm', 'Synthetic local input.') if kind == 'llm'
            else api.LocalTranslationRequest(text='Synthetic local input.', source='en', target='uk'))
        approval = api.JobApproval('local-job', profile.fingerprint, (request.fingerprint(profile),),
            mode='LOCAL_ONLY', allow_loopback=True)
        ledger = api.BudgetLedger(tmp_path / 'local.sqlite')
        assert api.ProviderGateway(ledger).perform(profile, request, approval=approval,
            operation_id='local-request', estimated_micros=0) == response
        assert len(calls) == 1
        assert calls[0][0] == ('/v1/chat/completions' if kind == 'llm' else '/translate')
        assert not {'authorization', 'xi-api-key', 'x-goog-api-key'} & {key.lower() for key in calls[0][1]}
        assert ledger.report('local-job')[0]['state'] == 'complete'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
