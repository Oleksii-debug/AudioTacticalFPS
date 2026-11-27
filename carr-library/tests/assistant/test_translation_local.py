"""Pure local protocol/admission gates; all HTTP is a bounded synthetic transport."""
from dataclasses import asdict, replace
from decimal import Decimal
from hashlib import sha256
import http.client
import json
from time import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProviderProfile, ProfileStore
from carr_library.assistant.providers import (
    Authorization, LocalTranslationAuthorization, Gateway, ProviderError)
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.assistant.translation_local import request, response, validate_languages


def profile(**changes):
    values = dict(id='local_translator', name='Synthetic local translator',
        adapter='libretranslate-local', endpoint='http://127.0.0.1:55123',
        model='declared-synthetic-models', capabilities=('translation.text', 'translation.languages'),
        local=True, enabled=True)
    values.update(changes)
    return ProviderProfile(**values)


def discovery(p):
    identity = uuid4().hex
    return LocalTranslationAuthorization(identity, identity, ((p.id, p.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    state = {'calls': [], 'status': 200, 'raw': None}
    class NoVault:
        def get(self, *args):
            raise AssertionError('A local translator cannot read credentials.')
    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host == '127.0.0.1' and port == 55123
            self.position = 0
        def request(self, method, route, body=None, headers=None):
            state['calls'].append((method, route, body, dict(headers)))
            if state['raw'] is not None:
                self.raw = state['raw']
            elif method == 'GET':
                self.raw = json.dumps([
                    {'code': 'en', 'name': 'English', 'targets': ['uk']},
                    {'code': 'uk', 'name': 'Ukrainian', 'targets': []},
                ]).encode()
            else:
                payload = json.loads(body)
                result = {'translatedText': 'DERIVED: ' + payload['q'] + ' &amp;'}
                if payload['source'] == 'auto':
                    result['detectedLanguage'] = {'language': 'en', 'confidence': 91.5}
                self.raw = json.dumps(result, ensure_ascii=False).encode()
        def getresponse(self):
            self.status = state['status']
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            value = self.raw[self.position:self.position + size]
            self.position += len(value)
            return value
        def close(self):
            pass
    monkeypatch.setattr(http.client, 'HTTPConnection', Connection)
    service = TranslationService(tmp_path / 'translation',
        gateway=Gateway(tmp_path / 'translation/provider', vault=NoVault()))
    p = profile()
    service.refresh_languages(p, authorization=discovery(p))
    return SimpleNamespace(service=service, profile=p, state=state)


@pytest.mark.parametrize('changes', [
    {'endpoint': 'http://localhost:55123'}, {'endpoint': 'https://example.org'},
    {'endpoint': 'http://10.0.0.1:5000'}, {'endpoint': 'http://127.0.0.1:5000/api'},
    {'endpoint': 'http://user:secret@127.0.0.1:5000'}, {'endpoint': 'http://127.0.0.1:5000?key=x'},
    {'local': False}, {'credential_ref': 'carr/provider/local_translator/' + 'a' * 32},
    {'capabilities': ('llm.chat',)}, {'voice': 'somevoice'},
])
def test_local_profile_cannot_change_authority_or_forward_credentials(changes):
    with pytest.raises(ValueError):
        profile(**changes)


def test_local_approval_does_not_change_remote_authorization_digest():
    p = profile()
    local = discovery(p)
    with pytest.raises(ValueError):
        Authorization(**asdict(local))
    remote = Authorization('remote_approval', 'remote_task',
        (('google', 'https://translation.googleapis.com/language/translate/v2'),),
        ('translation.text',), ('selected-excerpt',), '1', 1, time() + 60)
    original = asdict(remote)
    assert set(original) == {'id', 'task_id', 'profiles', 'operations', 'data_classes',
                            'budget', 'max_calls', 'expires_at', 'request_hashes', 'currency'}
    assert canonical(asdict(Authorization(**original))) == canonical(original)
    with pytest.raises(ValueError):
        LocalTranslationAuthorization(**original)


@pytest.mark.parametrize('changes', [
    {'budget': '1'}, {'operations': ('llm.chat',)},
    {'profiles': (('local_translator', 'https://example.org'),)},
])
def test_local_approval_is_zero_cost_translation_only(changes):
    value = asdict(discovery(profile()))
    value.update(changes)
    with pytest.raises(ValueError):
        LocalTranslationAuthorization(**value)


def test_request_mapping_is_exact_and_has_no_model_download_or_key():
    assert request('translation.languages', {'target': 'en'}) == ('/languages', {}, 'application/json')
    route, body, kind = request('translation.text', {'q': 'Text <literal>', 'target': 'uk'})
    assert route == '/translate' and kind == 'application/json'
    assert body == {'q': 'Text <literal>', 'source': 'auto', 'target': 'uk', 'format': 'text'}
    with pytest.raises(ValueError):
        request('translation.text', {'q': 'Text', 'target': 'uk', 'glossary': 'unavailable'})


@pytest.mark.parametrize('raw', [
    b'{"translatedText":"one","translatedText":"two"}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":NaN}}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":true}}',
    b'{"translatedText":"ok"}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":101}}',
    b'{"translatedText":"ok","detectedLanguage":{"language":"../en","confidence":90}}',
])
def test_auto_response_rejects_ambiguous_or_unqualified_detection(raw):
    with pytest.raises(ValueError):
        response(raw, 'translation.text', {'source': 'auto'}, maximum=1000)


@pytest.mark.parametrize('rows', [
    [{'language': 'en', 'name': 'English', 'targets': ['uk']}],
    [{'language': 'en', 'name': 'English', 'targets': [{}]}],
    [{'language': 'en', 'name': 'English', 'targets': ['en', 'en']}],
    [{'language': 'en', 'name': 'English', 'targets': [], 'quality': 'qualified'}],
])
def test_language_inventory_requires_exact_declared_pairs(rows):
    with pytest.raises(ValueError):
        validate_languages(rows)


def test_no_call_without_explicit_local_approval(ready):
    service, p, state = ready.service, ready.profile, ready.state
    calls = len(state['calls'])
    with pytest.raises(PermissionError, match='explicit local'):
        service.gateway.request(p, 'translation.text', {'q': 'Text', 'target': 'uk'},
            task_id='unapproved', data_class='selected-excerpt')
    assert len(state['calls']) == calls


def test_pair_direction_and_zero_cost_are_checked_before_submission(ready):
    service, p, state = ready.service, ready.profile, ready.state
    with pytest.raises(ValueError, match='direction'):
        service.preview(p, 'Text', 'uk', 'en')
    with pytest.raises(ValueError, match='price'):
        service.preview(p, 'Text', 'en', 'uk', rate_per_million='20')
    with pytest.raises(ValueError):
        translation_approval(p, 'Text', 'en', 'uk', '1')
    assert len(state['calls']) == 1


def test_local_job_uses_existing_ledger_preserves_entities_and_exports_offline(ready, tmp_path, monkeypatch):
    service, p, state = ready.service, ready.profile, ready.state
    value = 'Synthetic <source> &amp; 🙂'
    approval, cost = translation_approval(p, value, 'auto', 'uk', '0')
    identity = service.submit(p, value, 'auto', 'uk', authorization=approval, per_call_cost=cost)
    assert service.gateway.mode == 'LOCAL_ONLY'
    assert service.resume(identity)['status'] == 'complete'
    job = service.get(identity)
    assert job['receipts'][0]['text'].endswith('&amp;')
    usage = service.gateway.ledger.get(approval.id)
    operation = usage['operations'][identity + '_0']
    assert Decimal(operation['reserved_cost']) == Decimal('0')
    assert operation['status'] == 'complete'
    assert operation['usage']['decoded_translation_sha256'] == job['receipts'][0]['decoded_translation_sha256']
    assert len(state['calls']) == 2
    _, _, body, headers = state['calls'][-1]
    assert json.loads(body)['format'] == 'text'
    assert not any(key.lower() in ('authorization', 'x-goog-api-key') for key in headers)
    assert b'api_key' not in body
    monkeypatch.setattr(http.client, 'HTTPConnection', lambda *args, **kwargs: pytest.fail('Offline completed work cannot connect.'))
    result = service.export(identity, tmp_path / 'export')
    report = json.loads((tmp_path / 'export/translation-provenance.json').read_text())
    assert result['status'] == 'complete'
    assert report['local_runtime_identity'] == 'not attested by the local API'
    assert report['quotation_authority'] is False
    assert service.resume(identity)['status'] == 'complete'


def test_decoded_text_plus_local_receipt_forgery_still_fails_independent_ledger(ready):
    service, p = ready.service, ready.profile
    approval, cost = translation_approval(p, 'Original', 'en', 'uk', '0')
    identity = service.submit(p, 'Original', 'en', 'uk', authorization=approval, per_call_cost=cost)
    service.resume(identity)
    job = service.jobs.load(identity)
    job['receipts'][0]['text'] = 'Forged'
    job['receipts'][0]['translation_sha256'] = sha256(b'Forged').hexdigest()
    service.jobs.save(job)
    with pytest.raises(ValueError, match='decoded-output'):
        service.get(identity)


@pytest.mark.parametrize('status', [302, 401, 500])
def test_local_error_never_falls_back_or_replays_an_attempt(ready, status):
    service, p, state = ready.service, ready.profile, ready.state
    approval, cost = translation_approval(p, 'Original', 'en', 'uk', '0')
    identity = service.submit(p, 'Original', 'en', 'uk', authorization=approval, per_call_cost=cost)
    state['status'] = status
    with pytest.raises(ProviderError):
        service.resume(identity)
    count = len(state['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(state['calls']) == count


@pytest.mark.parametrize('source', ['en', 'auto'])
@pytest.mark.parametrize('damage', ['missing', 'hash', 'direction'])
def test_paused_local_job_revalidates_declared_pair_before_next_request(ready, source, damage):
    service, p, state = ready.service, ready.profile, ready.state
    value = 'First retained paragraph.\n\nSecond retained paragraph.'
    approval, cost = translation_approval(p, value, source, 'uk', '0', preserve_paragraphs=True)
    identity = service.submit(p, value, source, 'uk', authorization=approval,
                              per_call_cost=cost, preserve_paragraphs=True)
    stop = [False]
    def progress(done, total, phase):
        if done == 1:
            stop[0] = True
    assert service.resume(identity, cancelled=lambda: stop[0], progress=progress)['status'] == 'paused'
    accepted = service.get(identity)['receipts']
    assert len(accepted) == 1
    before_calls = len(state['calls'])
    before_usage = service.gateway.ledger.get(approval.id)
    cache = service._languages_path(p)
    original_cache = cache.read_bytes()
    if damage == 'missing':
        cache.unlink()
    else:
        changed = json.loads(original_cache)
        if damage == 'hash':
            changed['languages_sha256'] = '0' * 64
        else:
            changed['languages'][0]['targets'] = []
            changed['languages_sha256'] = sha256(canonical(changed['languages']).encode('utf-8')).hexdigest()
        cache.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError):
        service.resume(identity)
    assert len(state['calls']) == before_calls
    assert service.gateway.ledger.get(approval.id) == before_usage
    assert service.get(identity)['receipts'] == accepted
    cache.write_bytes(original_cache)
    assert service.resume(identity)['status'] == 'complete'
    assert len(state['calls']) == before_calls + 1
    assert service.get(identity)['receipts'][:1] == accepted


def test_accepted_local_response_uses_pre_request_pair_snapshot(ready, monkeypatch, tmp_path):
    service, p, state = ready.service, ready.profile, ready.state
    approval, cost = translation_approval(p, 'Exact accepted source.', 'auto', 'uk', '0')
    identity = service.submit(p, 'Exact accepted source.', 'auto', 'uk',
                              authorization=approval, per_call_cost=cost)
    request = service.gateway.request
    def changed_after_acceptance(*args, **kwargs):
        result = request(*args, **kwargs)
        service._languages_path(p).unlink()
        return result
    monkeypatch.setattr(service.gateway, 'request', changed_after_acceptance)
    assert service.resume(identity)['status'] == 'complete'
    job = service.get(identity)
    assert len(job['receipts']) == 1 and job['receipts'][0]['detected_source'] == 'en'
    usage = service.gateway.ledger.get(approval.id)['operations'][identity + '_0']
    assert usage['status'] == 'complete'
    assert usage['usage']['decoded_translation_sha256'] == job['receipts'][0]['decoded_translation_sha256']
    assert service.export(identity, tmp_path / 'accepted-offline')['status'] == 'complete'
    assert len(state['calls']) == 2


@pytest.mark.parametrize('sentinel', ['auto', 'AUTO', 'Auto'])
def test_auto_sentinel_is_not_a_local_language(sentinel):
    with pytest.raises(ValueError):
        request('translation.languages', {'target': sentinel})
    with pytest.raises(ValueError):
        request('translation.text', {'q': 'Synthetic text', 'source': 'en', 'target': sentinel})
    with pytest.raises(ValueError):
        validate_languages([{'language': sentinel, 'name': 'Automatic', 'targets': []}])
    with pytest.raises(ValueError):
        validate_languages([{'language': 'en', 'name': 'English', 'targets': [sentinel]},
                            {'language': sentinel, 'name': 'Automatic', 'targets': []}])
    raw = json.dumps({'translatedText': 'Synthetic derivative',
                      'detectedLanguage': {'language': sentinel, 'confidence': 90}}).encode()
    with pytest.raises(ValueError):
        response(raw, 'translation.text', {'source': 'auto'}, maximum=1000)
    if sentinel != 'auto':
        with pytest.raises(ValueError):
            request('translation.text', {'q': 'Synthetic text', 'source': sentinel, 'target': 'uk'})
    else:
        _, body, _ = request('translation.text', {'q': 'Synthetic text', 'source': sentinel, 'target': 'uk'})
        assert body['source'] == 'auto'


@pytest.mark.parametrize('sentinel', ['auto', 'AUTO', 'Auto'])
def test_local_approval_rejects_auto_target_before_provider_call(ready, sentinel):
    service, p, state = ready.service, ready.profile, ready.state
    before = len(state['calls'])
    with pytest.raises(ValueError):
        service.preview(p, 'Synthetic text', 'en', sentinel)
    with pytest.raises(ValueError):
        translation_approval(p, 'Synthetic text', 'en', sentinel, '0')
    assert len(state['calls']) == before


@pytest.mark.parametrize('confidence', [
    '-1', '-0.01', '100.0000001', '1e309', '-1e309', '"90"', 'null',
    '[]', '{}', 'false', '1' + '0' * 400, '-1' + '0' * 400,
    'Infinity', '-Infinity',
])
def test_local_confidence_rejects_out_of_range_or_non_numeric_json(confidence):
    raw = ('{"translatedText":"ok","detectedLanguage":{"language":"en","confidence":'
           + confidence + '}}').encode('ascii')
    with pytest.raises(ValueError):
        response(raw, 'translation.text', {'source': 'auto'}, maximum=1000)


@pytest.mark.parametrize('confidence', [0, 0.0000001, 49.5, 99.999999, 100, 1e-300])
def test_local_confidence_accepts_finite_inclusive_range(confidence):
    raw = json.dumps({'translatedText': 'Дослід &amp; 🙂',
                      'detectedLanguage': {'language': 'uk', 'confidence': confidence}}).encode()
    assert response(raw, 'translation.text', {'source': 'auto'}, maximum=1000) == {
        'text': 'Дослід &amp; 🙂', 'detected_source': 'uk'}


@pytest.mark.parametrize('operation,payload', [
    ('translation.other', {'q': 'text', 'target': 'uk'}),
    ('translation.text', {'q': 'text'}),
    ('translation.text', {'target': 'uk'}),
    ('translation.text', {'q': 'text', 'target': 'uk', 'model': 'other'}),
    ('translation.text', {'q': 'text', 'target': 'uk', 'api_key': 'synthetic'}),
    ('translation.text', {'q': 'text', 'target': 'uk', 'format': 'html'}),
    ('translation.text', {'q': 'text', 'target': '../uk'}),
    ('translation.text', {'q': 'a' * 5001, 'target': 'uk'}),
    ('translation.text', {'q': ' \t\n', 'target': 'uk'}),
    ('translation.languages', {'target': 'uk', 'q': 'private synthetic text'}),
])
def test_local_request_rejects_unapproved_fields_or_invalid_input(operation, payload):
    with pytest.raises(ValueError):
        request(operation, payload)


@pytest.mark.parametrize('output', ['Привіт, світ!', 'A &amp; B <literal>', 'first\nsecond', '🙂' * 1000])
def test_local_explicit_source_response_preserves_exact_output(output):
    raw = json.dumps({'translatedText': output}).encode()
    assert response(raw, 'translation.text', {'source': 'en'}, maximum=1000) == {
        'text': output, 'detected_source': None}


@pytest.mark.parametrize('payload', [
    {'translatedText': 'ok', 'detectedLanguage': {'language': 'en', 'confidence': 100}},
    {'translatedText': None},
    {'translatedText': 'a' * 1001},
    {'translatedText': '\ud800'},
    {'translatedText': 'bad\x00text'},
    {'translatedText': 'ok', 'model': 'unrequested'},
])
def test_local_explicit_source_response_rejects_unapproved_or_invalid_output(payload):
    raw = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        response(raw, 'translation.text', {'source': 'en'}, maximum=1000)
