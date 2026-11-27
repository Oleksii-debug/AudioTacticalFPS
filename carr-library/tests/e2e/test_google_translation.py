"""Real local ledger/export checks with synthetic Google responses; no network."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from carr_library.providers import (BudgetLedger, DirectTransport, JobApproval,
    MAX_TRANSLATION_RESPONSE, ProfileStore, ProviderError, ProviderGateway,
    ProviderProfile, ProviderRequest)
from carr_library.translation import decode_google_translation
from carr_library.ui.api_connections import ConnectionsController
from carr_library.ui.translation_dialog import translation_request


class Vault:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def put(self, reference, binding, secret):
        self.values[reference, binding] = secret

    def get(self, reference, binding):
        self.reads += 1
        return self.values[reference, binding]

    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.output = json.dumps({'data': {'translations': [{
            'translatedText': 'Привіт, світе!\nДругий рядок.',
            'detectedSourceLanguage': 'en', 'model': 'nmt'}]}}, ensure_ascii=False).encode()

    def post(self, profile, route, payload, headers, **kwargs):
        self.calls.append((profile.endpoint, route, json.loads(payload), dict(headers)))
        return self.output


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'private'
    root.mkdir()
    vault, transport = Vault(), Transport()
    controller = ConnectionsController(root, vault=vault, transport=transport)
    profile = controller.store.save(ProviderProfile('google', 'My translation account',
        'google-translate-v2', 'https://translation.googleapis.com', 'nmt', ('translation',)),
        secret='synthetic-not-real-google-key')
    request = translation_request('Hello, world!\nSecond line.', '', 'uk')
    ledger = BudgetLedger(root / 'test-ledger.sqlite')
    gateway = ProviderGateway(ledger, vault=vault, transport=transport)
    approval = JobApproval('translate-job', profile.fingerprint, (request.fingerprint(profile),),
        budget_micros=100, mode='APPROVED_REMOTE')
    return root, vault, transport, controller, profile, request, ledger, gateway, approval


def perform(setup, *, request=None, approval=None, operation='translate-operation', cancelled=lambda: False):
    _root, _vault, _transport, _controller, profile, original, _ledger, gateway, approved = setup
    return gateway.perform(profile, request or original, approval=approval or approved,
        operation_id=operation, estimated_micros=100, cancelled=cancelled)


def test_official_json_request_and_key_header_preserve_exact_unicode(setup):
    root, vault, transport, _controller, profile, request, ledger, _gateway, approval = setup
    source = '  Україна e\u0301 — &amp; <b>буква</b>\r\n👩🏽\u200d💻  '
    request = replace(request, text=source, source_language='uk', target_language='en')
    transport.output = b'{"data":{"translations":[{"translatedText":"Synthetic English translation","model":"nmt"}]}}'
    perform(setup, request=request, approval=replace(approval, request_hashes=(request.fingerprint(profile),)))
    endpoint, route, body, headers = transport.calls[0]
    assert endpoint + route == 'https://translation.googleapis.com/language/translate/v2'
    assert body == {'q': [source], 'source': 'uk', 'target': 'en', 'format': 'text', 'model': 'nmt'}
    assert headers == {'X-Goog-Api-Key': 'synthetic-not-real-google-key'}
    assert ledger.report('translate-job')[0]['state'] == 'complete'
    assert 'synthetic-not-real' not in (root / 'api-connections.json').read_text()
    with sqlite3.connect(ledger.path) as connection:
        dump = '\n'.join(connection.iterdump())
    assert source not in dump
    assert 'synthetic-not-real' not in dump
    assert vault.reads == 1


def test_auto_detection_omits_source_and_records_detected_language(setup):
    output = perform(setup)
    assert 'source' not in setup[2].calls[0][2]
    result = decode_google_translation(output, setup[5])
    assert result.detected_source_language == 'en'
    assert result.text == 'Привіт, світе!\nДругий рядок.'


def test_reported_language_cannot_silently_override_explicit_source(setup):
    request = replace(setup[5], source_language='uk')
    approval = replace(setup[8], request_hashes=(request.fingerprint(setup[4]),))
    with pytest.raises(ProviderError, match='uncertain charge'):
        perform(setup, request=request, approval=approval)
    assert setup[6].report('translate-job')[0]['state'] == 'uncertain'


@pytest.mark.parametrize('field,value', [('text', 'Other exact text'),
    ('target_language', 'pl'), ('source_language', 'en')])
def test_approval_cannot_be_reused_for_different_text_or_language(setup, field, value):
    with pytest.raises(ProviderError, match='outside the approved'):
        perform(setup, request=replace(setup[5], **{field: value}))
    assert setup[1].reads == 0
    assert setup[2].calls == []
    assert setup[6].report('translate-job') == []


def test_local_only_and_cancel_do_not_read_keys_or_send(setup):
    with pytest.raises(ProviderError, match='LOCAL ONLY'):
        perform(setup, approval=replace(setup[8], mode='LOCAL_ONLY'))
    with pytest.raises(ProviderError, match='cancelled before'):
        perform(setup, cancelled=lambda: True)
    assert setup[1].reads == 0
    assert setup[2].calls == []
    assert setup[6].report('translate-job') == []


def test_unconnected_exported_profile_cannot_call_remote(setup):
    root, vault, transport, controller, profile, request, _ledger, _gateway, _approval = setup
    restored = ProviderProfile.from_dict(json.loads(json.dumps(controller.store.export_profiles()))['profiles'][0])
    with pytest.raises(ProviderError, match='Reconnect this profile'):
        controller.run(restored, request, root.parent / 'translation.json', approved=True, estimate_micros=100)
    assert vault.reads == 0
    assert transport.calls == []


@pytest.mark.parametrize('output', [
    b'{"data":{"translations":[]}}',
    b'{"data":{"translations":[{"translatedText":"ok"},{"translatedText":"other"}]}}',
    b'{"data":{"translations":[{"translatedText":"ok","translatedText":"changed","detectedSourceLanguage":"en"}]}}',
    b'{"data":{"translations":[{"translatedText":"ok"}]}}',
    b'{"data":{"translations":[{"translatedText":null,"detectedSourceLanguage":"en"}]}}',
    b'{"data":{"translations":[{"translatedText":"ok","detectedSourceLanguage":"../en"}]}}',
    b'{"data":{"translations":[{"translatedText":"ok","detectedSourceLanguage":"en","model":"other-model"}]}}',
    b'{"data":{"translations":[{"translatedText":"ok","detectedSourceLanguage":"en","tool_calls":[{}]}]}}',
    b'{"data":{"translations":[{"translatedText":"\\ud800","detectedSourceLanguage":"en"}]}}',
    b'{"data":{"translations":[{"translatedText":"a\\u0000b","detectedSourceLanguage":"en"}]}}',
    b'{"data":{"translations":[{"translatedText":NaN,"detectedSourceLanguage":"en"}]}}',
    b'<!doctype html><html>synthetic-server-body</html>',
    b'\xff',
    b'[' * 2000 + b']' * 2000,
    b'x' * (MAX_TRANSLATION_RESPONSE + 1),
], ids=('empty-translations', 'multiple-translations', 'duplicate-text',
        'missing-detected-language', 'null-text', 'invalid-language', 'wrong-model',
        'unexpected-tools', 'unpaired-surrogate', 'nul-text', 'nonfinite-number',
        'html-response', 'invalid-utf8', 'excessive-nesting', 'oversized-response'))
def test_malformed_response_is_uncertain_and_cannot_be_automatically_rebilled(setup, output):
    setup[2].output = output
    with pytest.raises(ProviderError, match='uncertain charge') as raised:
        perform(setup)
    assert 'synthetic-server-body' not in str(raised.value)
    assert raised.value.__cause__ is None
    assert setup[6].report('translate-job')[0]['state'] == 'uncertain'
    with pytest.raises(ProviderError, match='already recorded'):
        perform(setup)
    assert len(setup[2].calls) == 1


@pytest.mark.parametrize('endpoint', ['http://translation.googleapis.com',
    'https://translation.googleapis.com.evil.example', 'https://translation.googleapis.com:444',
    'https://translation.googleapis.com/v3', 'https://key@translation.googleapis.com',
    'https://translation.googleapis.com?key=synthetic'])
def test_google_key_destination_cannot_be_repointed(setup, endpoint):
    with pytest.raises(ProviderError):
        replace(setup[4], endpoint=endpoint)


def test_google_adapter_cannot_be_used_as_another_capability_or_advanced_auth(setup):
    for changes in ({'capabilities': ('llm',)}, {'model': 'custom-model'}, {'local': True}):
        with pytest.raises(ProviderError):
            replace(setup[4], **changes)
    with pytest.raises(ProviderError):
        ProviderProfile('voice', 'Voice', 'elevenlabs', 'https://api.elevenlabs.io/v1',
                        'test-model', ('translation',))


@pytest.mark.parametrize('source,target', [('auto', 'uk'), ('en', 'auto'),
    ('en', ''), ('en', 'uk<script>'), ('en\n', 'uk')])
def test_request_rejects_invalid_language_codes_before_network(source, target):
    with pytest.raises(ProviderError):
        ProviderRequest('translation', 'text', source_language=source, target_language=target)


def test_excerpt_limit_uses_unicode_characters_and_does_not_truncate():
    text = '😀' * 5000
    assert translation_request(text, '', 'uk').text == text
    with pytest.raises(ProviderError, match='5000'):
        translation_request(text + 'x', '', 'uk')
    with pytest.raises(ProviderError):
        ProviderRequest('llm', 'test', target_language='uk')


def test_old_nontranslation_request_fingerprints_remain_compatible():
    profile = ProviderProfile('local', 'Local', 'openai-compatible',
        'http://127.0.0.1:8080/v1', 'model', ('llm',), local=True)
    request = ProviderRequest('llm', 'Same saved task')
    original = {'profile': profile.fingerprint, 'request': {
        'capability': 'llm', 'text': 'Same saved task', 'voice': None, 'duration_seconds': None}}
    raw = json.dumps(original, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    assert request.fingerprint(profile) == sha256(raw).hexdigest()


def test_translation_export_is_separate_source_bound_and_secret_free(setup):
    root, _vault, transport, controller, profile, request, *_rest = setup
    original = root / 'original.txt'
    original.write_bytes(request.text.encode())
    target = root.parent / 'translation.json'
    receipt = controller.run(profile, request, target, approved=True, estimate_micros=100)
    document = json.loads(target.read_bytes())
    assert document['kind'] == 'derived_translation'
    assert document['source']['text'] == request.text
    assert document['source']['sha256'] == sha256(original.read_bytes()).hexdigest()
    assert original.read_bytes() == request.text.encode()
    assert document['translation']['text'] == receipt['translation_text']
    assert document['translation']['language'] == 'uk'
    assert document['operation']['request_sha256'] == request.fingerprint(profile)
    assert document['operation']['billed_cost'] == 'unknown'
    assert 'synthetic-not-real' not in target.read_text()
    assert 'credential_ref' not in target.read_text()
    with pytest.raises(FileExistsError):
        controller.run(profile, request, target, approved=True, estimate_micros=100)
    assert len(transport.calls) == 1


def test_output_preflight_blocks_network_and_ledger_charge(setup):
    root, vault, transport, controller, profile, request, *_rest = setup
    for destination in (root / 'original.txt', root.parent / 'CON.txt'):
        with pytest.raises(ValueError):
            controller.run(profile, request, destination, approved=True, estimate_micros=100)
    assert transport.calls == []
    assert vault.reads == 0


def test_post_commit_cleanup_failure_returns_success_without_rebilling(setup, monkeypatch):
    root, _vault, transport, controller, profile, request, *_rest = setup
    target = root.parent / 'translation.json'
    monkeypatch.setattr('carr_library.ui.api_connections.os.unlink',
                        Mock(side_effect=OSError('synthetic staging cleanup failure')))
    receipt = controller.run(profile, request, target, approved=True, estimate_micros=100)
    assert json.loads(target.read_bytes())['translation']['text'] == receipt['translation_text']
    assert Path(receipt['cleanup_pending']).read_bytes() == target.read_bytes()
    assert len(transport.calls) == 1
    with pytest.raises(ValueError, match='without hard links'):
        controller.run(profile, request, target, approved=True, estimate_micros=100)
    assert len(transport.calls) == 1


def test_failed_publication_retains_complete_result_and_does_not_retry(setup, monkeypatch):
    root, _vault, transport, controller, profile, request, *_rest = setup
    target = root.parent / 'translation.json'
    monkeypatch.setattr('carr_library.ui.api_connections.os.link',
                        Mock(side_effect=OSError('synthetic output publication failure')))
    with pytest.raises(ProviderError, match='Recover the complete result.*do not repeat the paid request'):
        controller.run(profile, request, target, approved=True, estimate_micros=100)
    assert not target.exists()
    staged = tuple(root.parent.glob('.provider-result-*'))
    assert len(staged) == 1
    assert json.loads(staged[0].read_bytes())['source']['text'] == request.text
    assert len(transport.calls) == 1


@pytest.mark.parametrize('declared,blocks', [
    (str(MAX_TRANSLATION_RESPONSE + 1), []),
    (None, [b'x' * (MAX_TRANSLATION_RESPONSE + 1)]),
])
def test_transport_applies_translation_budget_and_json_accept(setup, monkeypatch, declared, blocks):
    response = Mock(status=200)
    response.getheader.return_value = declared
    response.read.side_effect = blocks + [b'']
    connection = Mock()
    connection.getresponse.return_value = response
    context = Mock()
    monkeypatch.setattr('carr_library.providers.socket.getaddrinfo',
        lambda *args, **kwargs: [(2, 1, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr('carr_library.providers.http.client.HTTPConnection', lambda *args, **kwargs: connection)
    monkeypatch.setattr('carr_library.providers.ssl.SSLContext', lambda *args: context)
    with pytest.raises(ProviderError, match='response exceeds'):
        DirectTransport().post(setup[4], '/language/translate/v2', b'{}',
                               {'X-Goog-Api-Key': 'synthetic-key'})
    sent = connection.request.call_args.kwargs['headers']
    assert sent['Accept'] == 'application/json'
    assert sent['X-Goog-Api-Key'] == 'synthetic-key'
    assert connection.request.call_args.args == ('POST', '/language/translate/v2')
    if declared is not None:
        response.read.assert_not_called()
    connection.close.assert_called_once()
