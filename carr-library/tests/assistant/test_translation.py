"""Synthetic TLS-adapter, authorization, resume and original-integrity checks."""
from decimal import Decimal
from hashlib import sha256
import json
from time import time
import pytest
from carr_library.assistant.profiles import ProviderProfile
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant import providers
from carr_library.assistant.translation import TranslationService, split_text, ENDPOINT
from carr_library.assistant.state import read_json
from carr_library.speech.reading import ReadingDocument
from carr_library.ui.translation_dialog import translation_approval


def profile(**changes):
    values = dict(id='google_test', name='Synthetic account', adapter='google-translation-v2',
                  endpoint=ENDPOINT, model='nmt',
                  capabilities=('translation.text', 'translation.languages'),
                  credential_ref='carr/provider/google_test/' + 'a' * 32, enabled=True)
    values.update(changes)
    return ProviderProfile(**values)


class Vault:
    def __init__(self):
        self.calls = []

    def get(self, identity, reference):
        self.calls.append((identity, reference))
        return 'SYNTHETIC_TEST_SECRET_DO_NOT_SEND'


@pytest.fixture
def connection(monkeypatch):
    state = {'calls': [], 'status': 200, 'custom': None}
    class Connection:
        def __init__(self, target, *, local, deadline, cancelled):
            assert target.hostname == 'translation.googleapis.com'
            assert target.port in (None, 443) and target.scheme == 'https' and not local
            self.deadline, self.cancelled = deadline, cancelled
            self.position = 0

        def check(self):
            providers._check_request(self.deadline, self.cancelled)

        def request(self, method, route, body, headers):
            state['calls'].append({'method': method, 'route': route, 'body': body, 'headers': dict(headers)})
            if state['custom'] is not None:
                data = state['custom']
            elif method == 'GET':
                data = {'data': {'languages': [{'language': 'en', 'name': 'English'},
                                               {'language': 'uk', 'name': 'Ukrainian'}]}}
            else:
                payload = json.loads(body)
                data = {'data': {'translations': [{'translatedText': 'TRANSLATED: ' + payload['q'],
                                                   'detectedSourceLanguage': 'en'}]}}
            self.raw = json.dumps(data).encode('utf-8')
            if state.get('escape_secret'):
                secret = state['escape_secret']
                escaped = ''.join('\\u%04x' % ord(c) for c in secret).encode('ascii')
                self.raw = self.raw.replace(secret.encode('utf-8'), escaped)

        def getresponse(self):
            self.status = state['status']
            return self

        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json; charset=utf-8',
                    'Content-Length': str(len(self.raw))}.get(name, default)

        def read(self, size):
            block = self.raw[self.position:self.position + size]
            self.position += len(block)
            return block

        def close(self):
            pass
    monkeypatch.setattr(providers, '_Connection', Connection)
    return state


def ready(tmp_path, connection):
    vault = Vault()
    gateway = Gateway(tmp_path / 'gateway', vault=vault, mode='APPROVED_REMOTE')
    service = TranslationService(tmp_path / 'translation', gateway=gateway)
    p = profile()
    approval = Authorization('language_test', 'language_test', ((p.id, p.endpoint),),
                             ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return service, p, vault


def test_local_only_blocks_google_before_credentials_or_http(tmp_path, connection):
    vault = Vault()
    with pytest.raises(PermissionError, match='LOCAL ONLY'):
        Gateway(tmp_path, vault=vault).request(profile(), 'translation.text',
            {'q': 'Synthetic test', 'target': 'uk'}, task_id='task', data_class='selected-excerpt')
    assert not vault.calls and not connection['calls']


@pytest.mark.parametrize('change', [
    {'endpoint': 'https://evil.example/language/translate/v2'},
    {'endpoint': ENDPOINT + '?key=secret'},
    {'endpoint': 'https://translation.googleapis.com/v3'},
    {'model': 'advanced-v3'}, {'capabilities': ('llm.chat',)}, {'local': True},
])
def test_google_profile_has_exact_official_authority(change):
    with pytest.raises(ValueError):
        profile(**change)


def test_language_discovery_uses_get_and_header_secret(tmp_path, connection):
    service, p, vault = ready(tmp_path, connection)
    call = connection['calls'][0]
    assert call['method'] == 'GET' and call['body'] is None
    assert call['route'] == '/language/translate/v2/languages?target=en&model=nmt'
    assert call['headers']['X-goog-api-key'] == 'SYNTHETIC_TEST_SECRET_DO_NOT_SEND'
    assert 'SYNTHETIC_TEST_SECRET' not in call['route']
    assert service.languages(p)[1]['language'] == 'uk'
    assert 'SYNTHETIC_TEST_SECRET' not in service._languages_path(p).read_text()
    assert len(vault.calls) == 1


def test_preview_requires_provider_language_inventory(tmp_path):
    with pytest.raises(ValueError, match='fetch the supported'):
        TranslationService(tmp_path).preview(profile(), 'Synthetic source', 'auto', 'uk')


def test_preview_counts_unicode_and_owner_entered_price(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    value = 'Українська мова 🙂\n' * 1000
    result = service.preview(p, value, 'auto', 'uk', rate_per_million='20')
    assert result['characters'] == len(value)
    assert result['calls'] == len(split_text(value))
    assert Decimal(result['estimated_cost']) == Decimal(len(value)) * Decimal(20) / Decimal(1_000_000)
    assert len(connection['calls']) == 1
    with pytest.raises(ValueError, match='not declared supported'):
        service.preview(p, value, 'en', 'xx')


def test_text_api_uses_plaintext_and_secret_is_only_header(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    value = 'Synthetic source <tag> & original.'
    authorization, per_call = translation_approval(p, value, 'auto', 'uk', '1')
    identity = service.submit(p, value, 'auto', 'uk', authorization=authorization, per_call_cost=per_call)
    assert service.resume(identity)['status'] == 'complete'
    call = connection['calls'][-1]
    assert call['method'] == 'POST' and call['route'] == '/language/translate/v2'
    assert json.loads(call['body']) == {'q': value, 'target': 'uk', 'format': 'text', 'model': 'nmt'}
    assert 'SYNTHETIC_TEST_SECRET' not in call['body'].decode()


def test_reject_changed_text_under_prior_approval(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'First approved text', 'en', 'uk', '1')
    with pytest.raises(PermissionError, match='exact selected text'):
        service.submit(p, 'Different text', 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert len(connection['calls']) == 1


def test_resume_reuses_accepted_chunks_without_rebilling(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    source = 'Synthetic long paragraph. ' * 900
    approval, cost = translation_approval(p, source, 'en', 'uk', '2')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    stop = []
    result = service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    assert result['status'] == 'paused' and result['completed'] == 1
    before = len(connection['calls'])
    reopened = TranslationService(service.directory, gateway=service.gateway)
    result = reopened.resume(identity)
    assert result['status'] == 'complete'
    assert len(connection['calls']) - before == result['total'] - 1
    count = len(connection['calls'])
    reopened.resume(identity)
    assert len(connection['calls']) == count


def test_export_preserves_original_and_derivative_authority(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    value = 'Synthetic preserved source.'
    original = tmp_path / 'source.txt'
    original.write_text(value)
    approval, cost = translation_approval(p, value, 'en', 'uk', '1')
    identity = service.submit(p, value, 'en', 'uk', authorization=approval, per_call_cost=cost)
    service.resume(identity)
    output = tmp_path / 'export'
    service.export(identity, output)
    assert original.read_text() == value == (output / 'original.txt').read_text()
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.source_text == value and reading.text != value
    assert reading.source_span(0, len(reading.text)).exact is False
    report = read_json(output / 'translation-provenance.json')
    assert report['quotation_authority'] is False
    for filename, checksum in report['files'].items():
        assert sha256((output / filename).read_bytes()).hexdigest() == checksum
    with pytest.raises(FileExistsError):
        service.export(identity, output)


@pytest.mark.parametrize('source, expected', [
    pytest.param('A' * 4498 + '\n\nB', 'Переклад\n\nПереклад', id='paragraphs'),
    pytest.param('A' * 4499 + ' B', 'Переклад Переклад', id='word-space'),
    pytest.param('A' * 4497 + '\r\n\tB', 'Переклад\r\n\tПереклад', id='split-whitespace'),
    pytest.param('A' * 4499 + ' \u2003B', 'Переклад \u2003Переклад', id='unicode-space'),
    pytest.param('A' * 4499 + ' ' * 4501 + '\tB',
                 'Переклад' + ' ' * 4501 + '\tПереклад', id='whitespace-only-chunk'),
    pytest.param('漢' * 4501, 'ПерекладПереклад', id='no-space-script'),
])
def test_export_preserves_source_separators_between_translation_chunks(tmp_path, connection, source, expected):
    service, p, _ = ready(tmp_path, connection)
    connection['custom'] = {'data': {'translations': [{'translatedText': 'Переклад'}]}}
    approval, cost = translation_approval(p, source, 'en', 'uk', '1')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    saved = service.jobs.load(identity)
    request_count = len(connection['calls'])
    output = tmp_path / 'export'
    service.export(identity, output)
    assert (output / 'translated.txt').read_bytes().decode('utf-8') == expected
    assert (output / 'original.txt').read_bytes().decode('utf-8') == source
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.text == expected and reading.source_text == source
    assert service.jobs.load(identity) == saved
    assert len(connection['calls']) == request_count


def test_resumed_translation_keeps_exact_join_separator_without_changing_receipts(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    source = 'A' * 4498 + '\n\nB'
    approval, cost = translation_approval(p, source, 'en', 'uk', '1')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    connection['custom'] = {'data': {'translations': [{'translatedText': 'First \n'}]}}
    stop = []
    assert service.resume(identity, cancelled=lambda: bool(stop),
                          progress=lambda *args: stop.append(True))['status'] == 'paused'
    connection['custom'] = {'data': {'translations': [{'translatedText': '\tSecond'}]}}
    reopened = TranslationService(service.directory, gateway=service.gateway)
    assert reopened.resume(identity)['status'] == 'complete'
    saved = reopened.jobs.load(identity)
    assert [row['text'] for row in saved['receipts']] == ['First \n', '\tSecond']
    assert len(connection['calls']) == 3  # Discovery and exactly two approved chunk requests.
    output = tmp_path / 'resumed-export'
    reopened.export(identity, output)
    assert (output / 'translated.txt').read_bytes().decode('utf-8') == 'First\n\nSecond'
    assert reopened.jobs.load(identity) == saved
    assert len(connection['calls']) == 3


def test_single_chunk_export_preserves_the_exact_provider_text(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    source = '  Single source paragraph.\n'
    translated = '  Exact provider text.\t\n'
    connection['custom'] = {'data': {'translations': [{'translatedText': translated}]}}
    approval, cost = translation_approval(p, source, 'en', 'uk', '1')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    service.export(identity, tmp_path / 'single-export')
    assert (tmp_path / 'single-export' / 'translated.txt').read_bytes().decode('utf-8') == translated


def test_corrupt_completed_chunk_blocks_resume(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
    identity = service.submit(p, 'Synthetic source', 'en', 'uk', authorization=approval, per_call_cost=cost)
    service.resume(identity)
    job = service.jobs.load(identity)
    job['receipts'][0]['text'] = 'tampered'
    service.jobs.save(job)
    count = len(connection['calls'])
    with pytest.raises(ValueError, match='damaged'):
        service.resume(identity)
    assert len(connection['calls']) == count


@pytest.mark.parametrize('status', [302, 400, 401, 429, 500])
def test_uncertain_charge_is_not_automatically_retried(tmp_path, connection, status):
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
    identity = service.submit(p, 'Synthetic source', 'en', 'uk', authorization=approval, per_call_cost=cost)
    connection['status'] = status
    with pytest.raises(ProviderError):
        service.resume(identity)
    before = len(connection['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(connection['calls']) == before


def test_chunking_preserves_exact_unicode_and_bounds():
    value = '  абв🙂\n' * 3000
    parts = split_text(value)
    assert ''.join(parts) == value
    assert max(map(len, parts)) <= 4500


@pytest.mark.parametrize('encoding', ['html', 'json_unicode'])
@pytest.mark.parametrize('operation', ['translation.text', 'translation.languages'])
def test_encoded_credentials_never_reach_receipts_or_language_cache(tmp_path, connection, encoding, operation):
    service, p, _ = ready(tmp_path, connection)
    secret = 'SYNTHETIC_TEST_SECRET_DO_NOT_SEND'
    encoded = ''.join('&#%d;' % ord(c) for c in secret)
    if operation == 'translation.text':
        data = {'data': {'translations': [{'translatedText': encoded if encoding == 'html' else secret}]}}
    else:
        data = {'data': {'languages': [{'language': 'en', 'name': encoded if encoding == 'html' else secret}]}}
    connection['custom'] = data
    if encoding == 'json_unicode':
        connection['escape_secret'] = secret
    if operation == 'translation.text':
        approval, cost = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
        identity = service.submit(p, 'Synthetic source', 'en', 'uk', authorization=approval, per_call_cost=cost)
        with pytest.raises(ProviderError, match='credential material') as error:
            service.resume(identity)
        assert not service.jobs.load(identity)['receipts']
        ledger = service.gateway.ledger.get(approval.id)
    else:
        before = service._languages_path(p).read_bytes()
        approval = Authorization('second_languages', 'second_languages', ((p.id, p.endpoint),),
                                 ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
        with pytest.raises(ProviderError, match='credential material') as error:
            service.refresh_languages(p, authorization=approval)
        assert service._languages_path(p).read_bytes() == before
        ledger = service.gateway.ledger.get(approval.id)
    assert secret not in str(error.value)
    assert all(row['status'] == 'uncertain' and row['output_sha256'] is None
               for row in ledger['operations'].values())
    for path in (tmp_path / 'translation').rglob('*.json'):
        assert secret not in path.read_text(encoding='utf-8')


def test_cancel_before_transmission_can_resume_exact_request(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
    identity = service.submit(p, 'Synthetic source', 'en', 'uk', authorization=approval, per_call_cost=cost)
    calls = [0]
    def cancelled():
        calls[0] += 1
        return calls[0] >= 2
    result = service.resume(identity, cancelled=cancelled)
    assert result['status'] == 'paused' and result['completed'] == 0
    assert len(connection['calls']) == 1  # Only the prior language discovery.
    ledger = service.gateway.ledger.get(approval.id)
    assert next(iter(ledger['operations'].values()))['status'] == 'not_sent'
    assert service.resume(identity)['status'] == 'complete'
    assert len(connection['calls']) == 2
    assert service.resume(identity)['status'] == 'complete'
    assert len(connection['calls']) == 2


@pytest.mark.parametrize('mutation', ['profile', 'model', 'operation', 'bound', 'authorization', 'request'])
def test_not_sent_retry_cannot_expand_original_authority(tmp_path, mutation):
    from dataclasses import replace
    p = profile()
    approval, _ = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
    gateway = Gateway(tmp_path, vault=Vault(), mode='APPROVED_REMOTE')
    ledger = gateway.ledger
    request_hash = approval.request_hashes[0]
    ledger.reserve(approval, 'same_op', request_hash, Decimal('1'), p, 'translation.text')
    ledger.finish(approval.id, 'same_op', status='not_sent')
    changed_profile = replace(p, name='Changed profile') if mutation == 'profile' else p
    if mutation == 'model':
        # A different valid provider model/profile cannot borrow this receipt.
        changed_profile = ProviderProfile(p.id, p.name, 'openai-compatible',
            'https://api.example.test/v1', 'different-model', credential_ref=p.credential_ref, enabled=True)
    changed_approval = replace(approval, budget='2') if mutation == 'authorization' else approval
    with pytest.raises(PermissionError):
        ledger.reserve(changed_approval, 'same_op', 'f' * 64 if mutation == 'request' else request_hash,
            Decimal('0.5') if mutation == 'bound' else Decimal('1'), changed_profile,
            'translation.languages' if mutation == 'operation' else 'translation.text')
    assert ledger.get(approval.id)['operations']['same_op']['status'] == 'not_sent'


@pytest.mark.parametrize('status', ['reserved', 'uncertain', 'complete'])
def test_sent_or_unresolved_attempts_remain_nonretryable(tmp_path, status):
    p = profile()
    approval, _ = translation_approval(p, 'Synthetic source', 'en', 'uk', '1')
    ledger = Gateway(tmp_path).ledger
    request_hash = approval.request_hashes[0]
    ledger.reserve(approval, 'same_op', request_hash, Decimal('1'), p, 'translation.text')
    if status != 'reserved':
        ledger.finish(approval.id, 'same_op', status=status)
    with pytest.raises(ProviderError, match='already been attempted'):
        ledger.reserve(approval, 'same_op', request_hash, Decimal('1'), p, 'translation.text')


@pytest.mark.parametrize('translated', ['', ' \t\r\n', '&#32;&#10;&#9;', '&nbsp;'])
def test_empty_decoded_provider_output_keeps_uncertain_charge_and_no_receipt(tmp_path, connection, translated):
    service, p, _ = ready(tmp_path, connection)
    connection['custom'] = {'data': {'translations': [{'translatedText': translated}]}}
    approval, cost = translation_approval(p, 'Selected source with content.', 'en', 'uk', '1')
    identity = service.submit(p, 'Selected source with content.', 'en', 'uk',
                              authorization=approval, per_call_cost=cost)
    with pytest.raises(ProviderError):
        service.resume(identity)
    job = service.jobs.load(identity)
    assert job['status'] == 'attention' and not job['receipts']
    usage = service.gateway.ledger.get(approval.id)
    attempted = usage['operations'][identity + '_0']
    assert attempted['status'] == 'uncertain' and attempted['output_sha256'] is None
    count = len(connection['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    with pytest.raises(ValueError):
        service.export(identity, tmp_path / 'empty-export')
    assert len(connection['calls']) == count
    assert not (tmp_path / 'empty-export').exists()


@pytest.mark.parametrize('action', ['export', 'resume'])
@pytest.mark.parametrize('mutation', [
    'source-language', 'target-language', 'model', 'profile-name', 'credential-ref',
    'per-call-cost', 'authorization', 'request', 'response', 'empty-receipt',
    'ledger-profile', 'ledger-response', 'ledger-status', 'missing-ledger',
])
def test_saved_translation_requires_its_original_authority_and_receipts(tmp_path, connection, action, mutation):
    from carr_library.assistant.state import atomic_json
    service, p, _ = ready(tmp_path, connection)
    source = 'Selected immutable source.'
    approval, cost = translation_approval(p, source, 'en', 'uk', '1')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    job = service.jobs.load(identity)
    usage_path = service.gateway.ledger._path(approval.id)
    if mutation == 'source-language':
        job['source_language'] = 'auto'
    elif mutation == 'target-language':
        job['target_language'] = 'pl'
    elif mutation == 'model':
        job['profile']['model'] = 'another-model'
    elif mutation == 'profile-name':
        job['profile']['name'] = 'A different account snapshot'
    elif mutation == 'credential-ref':
        job['profile']['credential_ref'] = 'carr/provider/google_test/' + 'b' * 32
    elif mutation == 'per-call-cost':
        job['per_call_cost'] = '0.25'
    elif mutation == 'authorization':
        job['authorization']['budget'] = '2'
    elif mutation == 'request':
        job['receipts'][0]['request_sha256'] = '0' * 64
    elif mutation == 'response':
        job['receipts'][0]['response_sha256'] = '0' * 64
    elif mutation == 'empty-receipt':
        job['receipts'][0]['text'] = ' \n'
        job['receipts'][0]['translation_sha256'] = sha256(b' \n').hexdigest()
    elif mutation == 'missing-ledger':
        usage_path.unlink()
    else:
        usage = service.gateway.ledger.get(approval.id)
        recorded = usage['operations'][identity + '_0']
        if mutation == 'ledger-profile':
            recorded['profile_sha256'] = '0' * 64
        elif mutation == 'ledger-response':
            recorded['output_sha256'] = '0' * 64
        else:
            recorded['status'] = 'uncertain'
        atomic_json(usage_path, usage)
    service.jobs.save(job)
    before = service.jobs.path(identity).read_bytes()
    count = len(connection['calls'])
    output = tmp_path / 'invalid-export'
    with pytest.raises((ValueError, PermissionError)):
        if action == 'export':
            service.export(identity, output)
        else:
            service.resume(identity)
    assert not output.exists()
    assert len(connection['calls']) == count
    assert service.jobs.path(identity).read_bytes() == before


def test_verified_completed_translation_exports_offline_after_approval_expiry(tmp_path, connection, monkeypatch):
    from carr_library.assistant import providers
    service, p, vault = ready(tmp_path, connection)
    source = 'A completed translation stays available offline.'
    approval, cost = translation_approval(p, source, 'en', 'uk', '1')
    identity = service.submit(p, source, 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert service.resume(identity)['status'] == 'complete'
    receipts = service.jobs.load(identity)['receipts']
    calls, credentials = len(connection['calls']), len(vault.calls)
    monkeypatch.setattr(providers, 'time', lambda: approval.expires_at + 60)
    service.gateway.mode = 'LOCAL_ONLY'
    reopened = TranslationService(service.directory, gateway=service.gateway)
    assert reopened.resume(identity)['status'] == 'complete'
    reopened.export(identity, tmp_path / 'offline-export')
    assert (tmp_path / 'offline-export/original.txt').read_text(encoding='utf-8') == source
    assert reopened.jobs.load(identity)['receipts'] == receipts
    assert len(connection['calls']) == calls and len(vault.calls) == credentials


def test_unstarted_translation_is_readable_without_a_usage_record(tmp_path, connection):
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'Selected source.', 'en', 'uk', '1')
    identity = service.submit(p, 'Selected source.', 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert not service.gateway.ledger._path(approval.id).exists()
    saved, = service.list()
    assert saved['id'] == identity and saved['status'] == 'queued'
    assert service.resume(identity)['status'] == 'complete'
