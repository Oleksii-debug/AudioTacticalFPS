from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
from threading import Thread
from time import time

import pytest

from carr_library.assistant import (
    Authorization, CredentialUnavailable, Gateway, ProfileStore, ProviderError,
    ProviderProfile, WindowsCredentialVault,
)
from carr_library.assistant.credentials import validate_reference
from carr_library.assistant.models import Scope, canonical
from carr_library.assistant.providers import UsageLedger, _request
from carr_library.assistant.llm import propose_local_plan


class Vault:
    def __init__(self):
        self.calls = []

    def get(self, identity, reference):
        validate_reference(reference, identity)
        self.calls.append(identity)
        return 'synthetic-secret-' + identity


@pytest.fixture
def server():
    requests = []
    settings = {'status': 200, 'type': 'application/json',
                'body': {'choices': [{'message': {'content': 'Local response'}}],
                         'usage': {'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}}}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            requests.append((self.path, dict(self.headers), json.loads(data)))
            self.send_response(settings['status'])
            self.send_header('Content-Type', settings['type'])
            raw = settings['body'] if isinstance(settings['body'], bytes) else json.dumps(settings['body']).encode()
            self.send_header('Content-Length', str(len(raw)))
            if settings['status'] == 302:
                self.send_header('Location', 'http://127.0.0.1:9/secret-destination')
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(httpd.server_port) + '/v1', requests, settings
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)


def remote(**changes):
    data = dict(id='account_one', name='First account', adapter='openai-compatible',
                endpoint='https://api.example.test/v1', model='chosen-model', enabled=True,
                credential_ref='carr/provider/account_one/' + 'a' * 32)
    data.update(changes)
    return ProviderProfile(**data)


def auth(profile, **changes):
    data = dict(id='approval_one', task_id='project-one', profiles=((profile.id, profile.endpoint),),
                operations=('llm.chat',), data_classes=('selected-script',), budget='1.00',
                max_calls=2, expires_at=time() + 60)
    data.update(changes)
    return Authorization(**data)


PAYLOAD = {'messages': [{'role': 'user', 'content': 'Synthetic test only'}]}


def test_local_only_denies_before_any_vault_or_connection(tmp_path, monkeypatch):
    vault = Vault()
    gateway = Gateway(tmp_path / 'providers', vault=vault)
    calls = []
    monkeypatch.setattr(http.client, 'HTTPSConnection', lambda *a, **k: calls.append(a))
    with pytest.raises(PermissionError, match='LOCAL ONLY'):
        gateway.request(remote(), 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script')
    assert not vault.calls and not calls
    assert not list((tmp_path / 'providers').rglob('*.json'))


@pytest.mark.parametrize('change', [
    {'task_id': 'different-task'}, {'profiles': (('different-account', 'https://api.example.test/v1'),)},
    {'profiles': (('account_one', 'https://wrong.example/v1'),)}, {'operations': ('music.compose',)},
    {'data_classes': ('user-instruction',)}, {'expires_at': 0}, {'request_hashes': ('0' * 64,)},
])
def test_remote_envelope_rejects_wrong_scope_before_credentials(tmp_path, change):
    profile = remote()
    vault = Vault()
    gateway = Gateway(tmp_path, vault=vault, mode='HYBRID')
    with pytest.raises(PermissionError):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
                        authorization=auth(profile, **change), max_cost='0.20')
    assert not vault.calls


def test_unknown_cost_rejected_and_budget_retains_uncertain_reservation(tmp_path):
    profile = remote()
    gateway = Gateway(tmp_path, vault=Vault(), mode='APPROVED_REMOTE')
    approval = auth(profile)
    with pytest.raises(PermissionError, match='cost is unknown'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
                        authorization=approval)
    ledger = gateway.ledger
    ledger.reserve(approval, 'op1', 'a' * 64, Decimal('0.80'), profile, 'llm.chat')
    ledger.finish(approval.id, 'op1', status='uncertain')
    with pytest.raises(PermissionError, match='budget'):
        ledger.reserve(approval, 'op2', 'b' * 64, Decimal('0.30'), profile, 'llm.chat')
    with pytest.raises(ProviderError, match='already been attempted'):
        ledger.reserve(approval, 'op1', 'a' * 64, Decimal('0.80'), profile, 'llm.chat')
    with pytest.raises(PermissionError, match='different data'):
        ledger.reserve(approval, 'op1', 'f' * 64, Decimal('0.10'), profile, 'llm.chat')


def test_actual_loopback_chat_and_separate_account_routing(tmp_path, server):
    address, requests, settings = server
    vault = Vault()
    gateway = Gateway(tmp_path, vault=vault)
    for identity in ('account_one', 'account_two'):
        profile = ProviderProfile(identity, identity, 'openai-compatible', address, 'local-model',
                                  local=True, enabled=True,
                                  credential_ref='carr/provider/' + identity + '/' + 'a' * 32)
        result = gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project', data_class='selected-script')
        assert result['text'] == 'Local response' and result['local'] is True
        assert result['usage']['total_tokens'] == 10
    assert vault.calls == ['account_one', 'account_two']
    assert requests[0][0] == '/v1/chat/completions'
    assert requests[0][1]['Authorization'] == 'Bearer synthetic-secret-account_one'
    assert requests[1][1]['Authorization'] == 'Bearer synthetic-secret-account_two'
    assert requests[0][2]['model'] == 'local-model'
    assert not list(tmp_path.rglob('*.json'))


def test_redirect_never_forwarded_and_error_is_redacted(tmp_path, server):
    address, requests, settings = server
    settings['status'] = 302
    settings['body'] = {'error': 'synthetic-secret-account_one'}
    profile = ProviderProfile('account_one', 'Local', 'openai-compatible', address, 'local-model',
                              local=True, enabled=True,
                              credential_ref='carr/provider/account_one/' + 'a' * 32)
    with pytest.raises(ProviderError, match='redirect') as error:
        Gateway(tmp_path, vault=Vault()).request(profile, 'llm.chat', PAYLOAD,
            task_id='project', data_class='selected-script')
    assert 'synthetic-secret' not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize('address', [
    'http://api.example.test/v1', 'https://user:password@example.com/v1',
    'https://example.com/v1?api_key=secret', 'https://example.com/#token',
    'https://127.0.0.1/v1', 'https://localhost/v1', 'https://example.com/%2Fescape',
])
def test_invalid_remote_endpoint_cannot_save_credentials(address):
    with pytest.raises(ValueError):
        remote(endpoint=address)


def test_untrusted_response_tool_call_and_wrong_content_type_are_rejected(tmp_path, server):
    address, requests, settings = server
    profile = ProviderProfile('local', 'Local', 'openai-compatible', address, 'local-model', local=True, enabled=True)
    gateway = Gateway(tmp_path)
    settings['body'] = {'choices': [{'message': {'content': 'done', 'tool_calls': [{'function': {'name': 'shell'}}]}}]}
    with pytest.raises(ProviderError, match='unsolicited tool'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='p', data_class='selected-script')
    settings['type'] = 'text/html'
    with pytest.raises(ProviderError, match='content type'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='p', data_class='selected-script')


def test_response_bounds_and_pretransmission_cancel(tmp_path, server):
    address, requests, settings = server
    profile = ProviderProfile('local', 'Local', 'openai-compatible', address, 'local-model', local=True, enabled=True)
    gateway = Gateway(tmp_path)
    with pytest.raises(InterruptedError):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='p', data_class='selected-script', cancelled=lambda: True)
    assert not requests
    with pytest.raises(ProviderError, match='byte budget'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='p', data_class='selected-script', max_response_bytes=5)
    assert len(requests) == 1


def test_profiles_export_never_contains_secret_references_or_enabled_state(tmp_path):
    store = ProfileStore(tmp_path / 'profiles')
    store.save(remote())
    exported = store.export_json()
    assert 'carr/provider' not in exported
    assert json.loads(exported)['profiles'][0]['enabled'] is False
    imported = ProfileStore(tmp_path / 'second')
    row, = imported.import_json(exported)
    assert row.credential_ref is None and row.enabled is False
    with pytest.raises(FileExistsError):
        imported.import_json(exported)
    bad = json.loads(exported)
    bad['profiles'][0]['api_key'] = 'do-not-persist'
    with pytest.raises(TypeError):
        imported.import_json(json.dumps(bad))
    assert all('do-not-persist' not in p.read_text() for p in tmp_path.rglob('*.json'))


def test_credential_references_cannot_cross_accounts():
    with pytest.raises(PermissionError):
        remote(credential_ref='carr/provider/account_two/' + 'a' * 32)
    with pytest.raises(PermissionError):
        validate_reference('carr/provider/account_one/' + 'a' * 32, 'account_two')


def test_windows_vault_has_no_plaintext_fallback(tmp_path):
    vault = WindowsCredentialVault()
    if os.name == 'nt':
        # Actual Windows user vault integration; only a fresh synthetic credential.
        reference = vault.put('synthetic_test', 'synthetic-not-a-user-secret')
        try:
            assert vault.get('synthetic_test', reference) == 'synthetic-not-a-user-secret'
        finally:
            vault.delete('synthetic_test', reference)
        with pytest.raises(CredentialUnavailable):
            vault.get('synthetic_test', reference)
    else:
        with pytest.raises(CredentialUnavailable):
            vault.put('synthetic_test', 'synthetic-not-a-user-secret')
    assert not list(tmp_path.iterdir())


def test_actual_local_planner_output_cannot_grant_tool_or_scope_permissions(tmp_path, server):
    address, requests, settings = server
    profile = ProviderProfile('local', 'Local', 'openai-compatible', address, 'local-model', local=True, enabled=True)
    gateway = Gateway(tmp_path)
    settings['body'] = {'choices': [{'message': {'content': json.dumps({
        'schema': 'carr-assistant-plan/1', 'steps': [{'tool': 'shell', 'arguments': {'command': 'steal'}}]})}}]}
    with pytest.raises(ValueError, match='not allowed'):
        propose_local_plan(gateway, profile, 'Read the document', Scope())
    settings['body'] = {'choices': [{'message': {'content': json.dumps({
        'schema': 'carr-assistant-plan/1', 'steps': [{'tool': 'library.list', 'arguments': {}}]})}}]}
    plan = propose_local_plan(gateway, profile, 'List libraries', Scope())
    assert plan.steps[0].tool == 'library.list'


def test_elevenlabs_templates_are_typed_and_locked_to_official_host():
    base = dict(id='eleven', name='Eleven', adapter='elevenlabs', endpoint='https://api.elevenlabs.io/v1',
                model='chosen-model', capabilities=('speech.synthesize', 'music.compose', 'sfx.generate'),
                voice='voice_123')
    profile = ProviderProfile(**base)
    route, body, kind = _request(profile, 'speech.synthesize', {'text': 'Hello'})
    assert route == '/text-to-speech/voice_123?output_format=mp3_44100_128' and body['text'] == 'Hello'
    route, body, kind = _request(profile, 'music.compose', {'prompt': 'Original suspense', 'duration_ms': 30000})
    assert route.startswith('/music?') and body['force_instrumental'] is True
    route, body, kind = _request(profile, 'sfx.generate', {'text': 'A wooden door', 'duration_seconds': 2})
    assert route.startswith('/sound-generation?')
    with pytest.raises(ValueError):
        ProviderProfile(**{**base, 'endpoint': 'https://evil.example/v1'})
    with pytest.raises(ValueError):
        _request(profile, 'speech.synthesize', {'text': 'Hello', 'url': 'https://example.com/private'})


def test_untrusted_provider_response_cannot_echo_a_credential(tmp_path, server):
    address, requests, settings = server
    profile = ProviderProfile('account_one', 'Local', 'openai-compatible', address, 'local-model',
                              local=True, enabled=True,
                              credential_ref='carr/provider/account_one/' + 'a' * 32)
    settings['body'] = {'choices': [{'message': {'content': 'synthetic-secret-account_one'}}]}
    with pytest.raises(ProviderError, match='credential material') as error:
        Gateway(tmp_path, vault=Vault()).request(profile, 'llm.chat', PAYLOAD,
            task_id='project', data_class='selected-script')
    assert 'synthetic-secret-account_one' not in str(error.value)
    assert not list(tmp_path.rglob('*.json'))


def test_native_header_encoding_error_does_not_expose_secret(tmp_path, server):
    address, requests, settings = server
    class UnicodeVault(Vault):
        def get(self, *args):
            return 'секрет-not-a-real-key'
    profile = ProviderProfile('account_one', 'Local', 'openai-compatible', address, 'local-model',
                              local=True, enabled=True,
                              credential_ref='carr/provider/account_one/' + 'a' * 32)
    with pytest.raises(ProviderError) as error:
        Gateway(tmp_path, vault=UnicodeVault()).request(profile, 'llm.chat', PAYLOAD,
            task_id='project', data_class='selected-script')
    assert 'секрет' not in str(error.value) and 'not-a-real-key' not in str(error.value)
    assert not requests


def test_authorized_remote_route_budget_and_unsafe_retry_with_local_mock(tmp_path, server, monkeypatch):
    """Real local HTTP response, mocked TLS transport; no external API call."""
    address, requests, settings = server
    from urllib.parse import urlsplit
    local = urlsplit(address)
    targets = []
    def mock_tls(host, port, **options):
        targets.append((host, port))
        return http.client.HTTPConnection(local.hostname, local.port, timeout=options['timeout'])
    monkeypatch.setattr(http.client, 'HTTPSConnection', mock_tls)
    profile = remote()
    approval = auth(profile, budget='0.50', max_calls=3)
    gateway = Gateway(tmp_path, vault=Vault(), mode='APPROVED_REMOTE')
    result = gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
        authorization=approval, max_cost='0.20', operation_id='successful')
    assert result['text'] == 'Local response'
    assert targets == [('api.example.test', None)]
    audit = gateway.ledger.get(approval.id)
    entry = audit['operations']['successful']
    assert entry['status'] == 'complete' and entry['actual_cost'] is None
    assert entry['reserved_cost'] == '0.20' and entry['usage']['total_tokens'] == 10
    assert 'Synthetic test only' not in json.dumps(audit)
    assert 'synthetic-secret' not in json.dumps(audit)
    settings['status'] = 503
    settings['body'] = {'error': 'synthetic-secret-account_one'}
    with pytest.raises(ProviderError) as error:
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
            authorization=approval, max_cost='0.20', operation_id='uncertain')
    assert 'synthetic-secret' not in str(error.value)
    assert gateway.ledger.get(approval.id)['operations']['uncertain']['status'] == 'uncertain'
    with pytest.raises(ProviderError, match='already been attempted'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
            authorization=approval, max_cost='0.20', operation_id='uncertain')
    with pytest.raises(PermissionError, match='budget'):
        gateway.request(profile, 'llm.chat', PAYLOAD, task_id='project-one', data_class='selected-script',
            authorization=approval, max_cost='0.20', operation_id='over-budget')
    assert len(requests) == 2
