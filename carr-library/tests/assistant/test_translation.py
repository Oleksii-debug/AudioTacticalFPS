"""Synthetic TLS-adapter, authorization, resume and original-integrity checks."""
from decimal import Decimal
from hashlib import sha256
import http.client
import json
import ssl
from time import time
import pytest
from carr_library.assistant.profiles import ProviderProfile, ProfileStore
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
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
        def __init__(self, host, port, **kwargs):
            assert host == 'translation.googleapis.com'
            assert port in (None, 443)
            assert isinstance(kwargs.get('context'), ssl.SSLContext)
            self.position = 0

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
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
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
    from uuid import UUID
    service, p, _ = ready(tmp_path, connection)
    approval, cost = translation_approval(p, 'Selected source.', 'en', 'uk', '1')
    identity = service.submit(p, 'Selected source.', 'en', 'uk', authorization=approval, per_call_cost=cost)
    assert not service.gateway.ledger._path(approval.id).exists()
    saved, = service.list()
    assert UUID(identity).version == 4
    assert saved['id'] == identity and saved['status'] == 'queued'
    assert service.resume(identity)['status'] == 'complete'


def bound_ready(tmp_path, connection, *, first=1, last=3, raw=None):
    """Use the real retained-library reader and gateway; only HTTPS is synthetic."""
    from types import SimpleNamespace
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Translation source')
    topic = app.create_topic(workspace, 'Selected research')
    if raw is None:
        raw = ('Witness evidence. Українська e\u0301 🙂.\r\n' * 160
               + '\fSecond source section. Žltý text.\r\n'
               + 'Second retained paragraph. ' * 180 + '\fFinal original section.')
    imported = app.add_text(workspace, 'Synthetic source book', raw)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    retained = read_source_range(desk, first, last)
    vault = Vault()
    directory = app.root / 'state' / 'translation'
    gateway = Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE')
    service = TranslationService(directory, gateway=gateway, library=app)
    p = profile()
    approval = Authorization('language_test', 'language_test', ((p.id, p.endpoint),),
                             ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, imported=imported,
        source=retained, document=retained.reading_document(), raw=raw, service=service,
        profile=p, vault=vault)


def submit_bound(case):
    approval, cost = translation_approval(case.profile, case.source.text, 'en', 'uk', '2', source_bound=True)
    identity = case.service.submit(case.profile, case.source.text, 'en', 'uk',
        title=case.source.title, source_document=case.document, topic=case.topic,
        authorization=approval, per_call_cost=cost)
    return identity, approval


@pytest.mark.parametrize('first,last', [(1, 3), (2, 2)], ids=['whole-document', 'source-range'])
def test_bound_document_resume_export_preserves_exact_original_and_canonical_lineage(
        tmp_path, connection, first, last):
    case = bound_ready(tmp_path, connection, first=first, last=last)
    original = case.app.assets.resolve(case.imported['asset'])
    before = original.read_bytes()
    assert len(connection['calls']) == 1  # Reading and preview stay local.
    preview = case.service.preview(case.profile, case.source.text, 'en', 'uk')
    assert preview['characters'] == len(case.source.text)
    assert preview['calls'] == len(split_text(case.source.text))
    identity, _ = submit_bound(case)
    stop = []
    paused = case.service.resume(identity, cancelled=lambda: bool(stop),
                                 progress=lambda *args: stop.append(True))
    assert paused['completed'] == 1 and paused['status'] == 'paused'
    first_receipt = case.service.jobs.load(identity)['receipts'][0]
    calls = len(connection['calls'])
    reopened = TranslationService(case.service.directory, gateway=case.service.gateway, library=case.app)
    completed = reopened.resume(identity)
    assert completed['status'] == 'complete'
    assert len(connection['calls']) - calls == completed['total'] - 1
    assert reopened.jobs.load(identity)['receipts'][0] == first_receipt
    target = tmp_path / 'source-translation'
    result = reopened.export(identity, target)
    assert result['status'] == 'complete'
    assert original.read_bytes() == before == case.raw.encode('utf-8')
    assert (target / 'original.txt').read_bytes() == case.source.text.encode('utf-8')
    reading = ReadingDocument.load(target / 'translation.reading.json')
    assert reading.source_text == case.source.text
    assert reading.source_anchor == case.document.source_anchor
    assert reading.text != reading.source_text
    assert reading.source_span(0, len(reading.text)).exact is False
    assert reading.undo() and reading.text == case.source.text
    for left, right in zip(case.source.positions, case.source.positions[1:]):
        assert not reading.source_span(left['end'], right['start']).exact
    report = read_json(target / 'translation-provenance.json')
    assert report['quotation_authority'] is False
    assert report['source_anchor'] == case.document.source_anchor
    assert [row['ordinal'] for row in report['source_anchor']['positions']] == list(range(first, last + 1))
    for name, digest in report['files'].items():
        assert sha256((target / name).read_bytes()).hexdigest() == digest
    calls = len(connection['calls'])
    assert reopened.resume(identity, cancelled=lambda: True)['status'] == 'complete'
    assert len(connection['calls']) == calls


def revoke_source(case, change):
    if change == 'workspace':
        case.app.archive_workspace(case.workspace)
    elif change == 'topic':
        with case.app.topic_transaction(case.workspace) as tree:
            tree.archive(case.topic)
    elif change == 'revision':
        case.app.update_metadata(case.workspace, case.imported['edition'], title='New source revision')
    else:
        original = case.app.assets.resolve(case.imported['asset'])
        raw = original.read_bytes()
        original.write_bytes(b'X' + raw[1:])


@pytest.mark.parametrize('change', ['workspace', 'topic', 'revision', 'original'])
def test_bound_source_changed_before_submission_never_creates_or_sends_job(tmp_path, connection, change):
    case = bound_ready(tmp_path, connection)
    revoke_source(case, change)
    calls = len(connection['calls'])
    with pytest.raises((ValueError, PermissionError)):
        submit_bound(case)
    assert not case.service.jobs.identities()
    assert len(connection['calls']) == calls


@pytest.mark.parametrize('change', ['workspace', 'topic', 'revision', 'original'])
def test_bound_source_change_between_chunks_retains_paid_prefix_without_next_request(
        tmp_path, connection, change):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    def accepted(done, total, phase):
        assert done == 1 and total > 1
        revoke_source(case, change)
    with pytest.raises((ValueError, PermissionError)):
        case.service.resume(identity, progress=accepted)
    saved = case.service.jobs.load(identity)
    assert saved['status'] == 'attention' and len(saved['receipts']) == 1
    assert len(connection['calls']) == 2  # Discovery plus exactly the accepted first chunk.
    before = saved['receipts']
    with pytest.raises((ValueError, PermissionError)):
        case.service.resume(identity)
    assert case.service.jobs.load(identity)['receipts'] == before
    assert len(connection['calls']) == 2


@pytest.mark.parametrize('change', ['workspace', 'topic', 'revision', 'original'])
def test_completed_source_job_requires_current_authorization_before_export(tmp_path, connection, change):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    assert case.service.resume(identity)['status'] == 'complete'
    before = case.service.jobs.path(identity).read_bytes()
    revoke_source(case, change)
    target = tmp_path / 'denied'
    with pytest.raises((ValueError, PermissionError)):
        case.service.export(identity, target)
    assert not target.exists()
    assert case.service.jobs.path(identity).read_bytes() == before
    assert len(connection['calls']) == 2


def test_source_revocation_during_response_retains_uncertain_charge_without_retry(
        tmp_path, connection, monkeypatch):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, approval = submit_bound(case)
    connection_type = http.client.HTTPSConnection
    original_getresponse = connection_type.getresponse
    def revoked_response(instance):
        case.app.archive_workspace(case.workspace)
        return original_getresponse(instance)
    monkeypatch.setattr(connection_type, 'getresponse', revoked_response)
    with pytest.raises(ProviderError):
        case.service.resume(identity)
    saved = case.service.jobs.load(identity)
    assert saved['status'] == 'attention' and saved['receipts'] == []
    usage = case.service.gateway.ledger.get(approval.id)
    assert usage['operations'][identity + '_0']['status'] == 'uncertain'
    case.app.restore_workspace(case.workspace)
    with pytest.raises(ProviderError, match='already been attempted'):
        case.service.resume(identity)
    assert len(connection['calls']) == 2


def test_new_source_jobs_cannot_attach_edits_or_change_bound_identity(tmp_path, connection):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    original = case.source.text
    case.document.set_text('A modified reading copy.')
    with pytest.raises(ValueError, match='unedited'):
        submit_bound(case)
    case.document.undo()
    identity, approval = submit_bound(case)
    before = case.service.jobs.path(identity).read_bytes()
    _, cost = translation_approval(case.profile, original, 'en', 'uk', '2')
    with pytest.raises(FileExistsError, match='already exists'):
        case.service.submit(case.profile, original, 'en', 'uk', title=case.source.title,
            source_document=case.document, topic=case.topic, authorization=approval, per_call_cost=cost)
    assert case.service.jobs.path(identity).read_bytes() == before
    saved = case.service.jobs.load(identity)
    saved['source_binding']['topic'] = None
    case.service.jobs.save(saved)
    with pytest.raises(ValueError, match='saved job identity'):
        case.service.resume(identity)
    assert len(connection['calls']) == 1


def test_bound_job_needs_library_context_and_unknown_or_downgraded_formats_fail_closed(
        tmp_path, connection):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    unbound_service = TranslationService(case.service.directory, gateway=case.service.gateway)
    with pytest.raises(PermissionError, match='from its library'):
        unbound_service.resume(identity)
    saved = case.service.jobs.load(identity)
    saved['kind'] = 'translation/1'
    case.service.jobs.save(saved)
    with pytest.raises(ValueError, match='legacy job format'):
        case.service.resume(identity)
    saved['kind'] = 'translation/999'
    case.service.jobs.save(saved)
    with pytest.raises(ValueError, match='binding'):
        case.service.export(identity, tmp_path / 'invalid')
    assert len(connection['calls']) == 1


@pytest.mark.parametrize('same_path', [False, True], ids=['new-directory', 'recreated-original-directory'])
@pytest.mark.parametrize('complete', [False, True], ids=['paused', 'complete'])
def test_real_backup_preserves_source_job_but_never_rebinds_remote_approval(tmp_path, connection, complete, same_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup

    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    case.service.resume(identity, cancelled=lambda: bool(stop),
                        progress=(lambda *args: None) if complete else lambda *args: stop.append(True))
    saved = case.service.jobs.load(identity)
    archive = tmp_path / 'backup'
    digest = case.app.backup(archive)
    destination = tmp_path / 'restored'
    if same_path:
        destination = case.app.root
        destination.rename(tmp_path / 'previous-library')
    restored_root = restore_backup(archive, destination, digest)
    restored = LibraryApp(restored_root)
    vault = Vault()
    directory = restored.root / 'state' / 'translation'
    gateway = Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE')
    service = TranslationService(directory, gateway=gateway, library=restored)
    assert service.jobs.load(identity) == saved
    calls = len(connection['calls'])
    if complete:
        # Completed local work is usable without renewing or transmitting approval.
        assert service.resume(identity, cancelled=lambda: True)['status'] == 'complete'
        service.export(identity, tmp_path / 'restored-output')
        reading = ReadingDocument.load(tmp_path / 'restored-output' / 'translation.reading.json')
        assert reading.source_anchor == case.document.source_anchor
    else:
        with pytest.raises(PermissionError, match='moved or restored'):
            service.resume(identity)
        assert service.jobs.load(identity)['receipts'] == saved['receipts']
    assert len(connection['calls']) == calls and not vault.calls


@pytest.mark.parametrize('change', ['workspace', 'original'], ids=['late-archival', 'late-original-mutation'])
def test_source_export_rechecks_after_verification_and_does_not_write_private_library(
        tmp_path, connection, monkeypatch, change):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    case.service.resume(identity)
    for target in (case.app.root / 'export', case.app.assets_dir / 'export'):
        with pytest.raises(ValueError, match='outside'):
            case.service.export(identity, target)
        assert not target.exists()
    original_context = case.service._source_context
    def late_change(*args, **kwargs):
        result = original_context(*args, **kwargs)
        revoke_source(case, change)
        return result
    monkeypatch.setattr(case.service, '_source_context', late_change)
    with pytest.raises(ValueError):
        case.service.export(identity, tmp_path / 'late-export')
    assert not (tmp_path / 'late-export').exists()
    assert case.service.jobs.load(identity)['status'] == 'complete'
    assert len(connection['calls']) == 2


def test_source_export_original_mutation_during_staging_never_publishes_completion(
        tmp_path, connection, monkeypatch):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    case.service.resume(identity)
    original_save = ReadingDocument.save
    def mutate_after_save(document, path):
        result = original_save(document, path)
        revoke_source(case, 'original')
        return result
    monkeypatch.setattr(ReadingDocument, 'save', mutate_after_save)
    target = tmp_path / 'changed-during-export'
    with pytest.raises(ValueError):
        case.service.export(identity, target)
    assert (target / 'translation.reading.json').exists()
    assert not (target / 'translation-provenance.json').exists()
    assert case.service.jobs.load(identity)['status'] == 'complete'
    assert len(connection['calls']) == 2


def test_source_export_serializes_archival_and_cancellation_never_claims_complete(
        tmp_path, connection, monkeypatch):
    from carr_library.ingest.writer_guard import WriterBusy
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    case.service.resume(identity)
    original_save = ReadingDocument.save
    observed = []
    def checked_save(document, path):
        with pytest.raises(WriterBusy):
            case.app.archive_workspace(case.workspace)
        observed.append(True)
        return original_save(document, path)
    monkeypatch.setattr(ReadingDocument, 'save', checked_save)
    case.service.export(identity, tmp_path / 'protected')
    assert observed == [True]
    with case.app.read(case.workspace):
        pass
    stop = []
    def cancelling_save(document, path):
        result = original_save(document, path)
        stop.append(True)
        return result
    monkeypatch.setattr(ReadingDocument, 'save', cancelling_save)
    with pytest.raises(InterruptedError):
        case.service.export(identity, tmp_path / 'cancelled', cancelled=lambda: bool(stop))
    assert not (tmp_path / 'cancelled' / 'translation-provenance.json').exists()
    assert case.service.jobs.load(identity)['status'] == 'complete'
    assert len(connection['calls']) == 2


def test_last_accepted_source_chunk_is_completed_despite_late_cancel(tmp_path, connection):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    stop = []
    completed = case.service.resume(identity, cancelled=lambda: bool(stop),
                                    progress=lambda *args: stop.append(True))
    assert completed['status'] == 'complete' and completed['completed'] == completed['total'] == 1
    assert case.service.jobs.load(identity)['status'] == 'complete'


@pytest.fixture
def translation_window(monkeypatch, tmp_path):
    """Exercise real native callbacks with explicit controls; not an NVDA claim."""
    import itertools
    import sys
    from threading import Event
    from types import SimpleNamespace
    from carr_library.ui import translation_dialog as ui

    controls, messages, menu_handlers, runs, errors, gateways = [], [], {}, [], [], []
    state = {'scenario': None, 'approval': 1, 'during_approval': None, 'range': '1-3',
             'during_range': None, 'deferred': False, 'pending': [], 'focused': None}

    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', '')
            self.name = ''
            self.title = kwargs.get('title', '')
            self.value = kwargs.get('value', '')
            self.items = kwargs.get('choices', [])
            self.selection = -1
            self.enabled, self.editable, self.destroyed = True, True, False
            self.handlers = {}
            controls.append(self)
        def SetName(self, value): self.name = value
        def SetSizer(self, value): pass
        def Add(self, *args, **kwargs): pass
        def SetSelection(self, value): self.selection = value
        def GetSelection(self): return self.selection
        def SetEditable(self, value): self.editable = value
        def Enable(self, value=True): self.enabled = value
        def Disable(self): self.enabled = False
        def GetValue(self): return self.value
        def ChangeValue(self, value):
            assert not self.destroyed, 'Late callback touched a destroyed control'
            self.value = value
        def SetFocus(self):
            assert not self.destroyed, 'Late callback focused a destroyed control'
            state['focused'] = self.name
        def Bind(self, event, callback, **kwargs): self.handlers[event] = callback
        def EndModal(self, value): self.result = value
        def ShowModal(self):
            if self.title == 'Translation — selected text derivatives':
                state['scenario']()
            return 10
        def Destroy(self):
            self.destroyed = True
            for control in controls:
                if control.parent is self:
                    control.destroyed = True

    class ChoiceDialog(Control):
        def __init__(self, parent, message, title, rows):
            super().__init__(parent, title=title, choices=rows)
            self.selection = 0

    class DirectoryDialog(Control):
        def GetPath(self): return str(tmp_path)

    class RangeDialog(Control):
        def __init__(self, parent, message, title, value):
            super().__init__(parent, title=title, value=state['range'])
        def ShowModal(self):
            if state['during_range']:
                state['during_range']()
            return 10
        def __enter__(self): return self
        def __exit__(self, *args): self.Destroy()

    def message_box(message, title, *args):
        messages.append((message, title))
        if state['during_approval']:
            state['during_approval']()
        return state['approval']

    identities = itertools.count(100)
    wx = SimpleNamespace(Dialog=Control, TextCtrl=Control, StaticText=Control, Choice=Control,
        Button=Control, BoxSizer=Control, SingleChoiceDialog=ChoiceDialog, DirDialog=DirectoryDialog,
        TextEntryDialog=RangeDialog, DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=3,
        HORIZONTAL=4, TE_MULTILINE=8, TE_READONLY=16, EXPAND=32, ALL=64,
        YES_NO=128, NO_DEFAULT=256, YES=1, NO=0, ID_OK=10, ID_CLOSE=11,
        EVT_BUTTON='button', EVT_CLOSE='close', EVT_MENU='menu',
        MessageBox=message_box, CallAfter=lambda function, *args: function(*args),
        NewIdRef=lambda: next(identities))
    monkeypatch.setitem(sys.modules, 'wx', wx)

    def find(prefix):
        return next(control for control in controls if control.name.startswith(prefix))
    def click(label):
        button = next(control for control in controls if control.label.replace('&', '') == label)
        assert button.enabled, label
        button.handlers['button'](None)

    def frame_for(case):
        frame = SimpleNamespace(library=case.app, active=(case.workspace, case.topic),
            working=False, busy=False, _closing=False, cancel_event=Event(), operation_progress=None,
            choice=Control(), report=lambda message: errors.append(message))
        rows = {row.edition_id: row for row in case.app.browse(case.workspace)}
        panel = SimpleNamespace(workspace=case.workspace, ready=True, rows=rows, books=Control(),
            _selection=lambda: (None, None, case.imported['edition']))
        frame.book = SimpleNamespace(GetPage=lambda index: panel)
        frame.menu_bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabel=lambda index: '&Tools',
            GetMenu=lambda index: SimpleNamespace(Append=lambda *args: None))
        frame.Bind = lambda event, callback, **kwargs: menu_handlers.update({kwargs['id']: callback})
        def run_job(title, operation, done=None, failed=None):
            runs.append(title)
            frame.cancel_event.clear()
            def execute():
                try:
                    result = operation()
                except Exception as exc:
                    if failed:
                        failed(exc)
                    else:
                        raise
                else:
                    if done:
                        done(result)
            if state['deferred']:
                state['pending'].append(execute)
            else:
                execute()
        frame.run_job = run_job
        return frame

    def bind(case):
        frame = frame_for(case)
        ProfileStore(case.app.root / 'state' / 'providers').save(case.profile)
        def gateway(*args, **kwargs):
            gateways.append(True)
            return case.service.gateway
        monkeypatch.setattr(ui, 'Gateway', gateway)
        monkeypatch.setattr(ui, 'TranslationService', lambda *args, **kwargs: case.service)
        return frame

    return SimpleNamespace(ui=ui, state=state, controls=controls, messages=messages,
        menu_handlers=menu_handlers, runs=runs, errors=errors, gateways=gateways,
        find=find, click=click, bind=bind)


@pytest.mark.parametrize('approval', ['accepted', 'declined', 'revoked', 'account-revoked'],
                         ids=['approved', 'no-transfer', 'late-source-revocation', 'late-account-revocation'])
def test_native_source_translation_callbacks_require_preview_consent_and_keep_lineage(
        tmp_path, connection, translation_window, approval):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    window = translation_window
    frame = window.bind(case)
    def guard():
        with case.app.read(case.workspace):
            pass
    def scenario():
        selected = window.find('Selected text')
        assert selected.value == case.source.text and not selected.editable
        assert 'positions 3-3' in window.find('Source selection').value
        window.find('Maximum approved').ChangeValue('2')
        window.find('Source language').ChangeValue('en')
        window.click('Translate / resume')
        assert 'Preview' in window.find('Preview, supported').value
        assert len(connection['calls']) == 1 and not case.service.jobs.identities()
        window.click('Preview')
        assert 'No request sent' in window.find('Preview, supported').value
        if approval == 'declined':
            window.state['approval'] = 0
        elif approval == 'revoked':
            window.state['during_approval'] = lambda: case.app.archive_workspace(case.workspace)
        elif approval == 'account-revoked':
            window.state['during_approval'] = lambda: ProfileStore(
                case.app.root / 'state' / 'providers').remove(case.profile.id)
        window.click('Translate / resume')
        assert len(window.messages) == 1
        assert f'exactly {len(case.source.text)}' in window.messages[0][0]
        if approval == 'accepted':
            job, = case.service.list()
            assert job['status'] == 'complete' and job['source_binding']['anchor'] == case.document.source_anchor
            window.click('Export')
            target, = tmp_path.glob('Translation-*')
            exported = ReadingDocument.load(target / 'translation.reading.json')
            assert exported.source_anchor == case.document.source_anchor
            assert exported.source_text == case.source.text and exported.text != case.source.text
            window.click('New translation')
            assert selected.value == case.source.text and not selected.editable
        else:
            assert not case.service.jobs.identities() and len(connection['calls']) == 1
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame, source_loader=lambda cancelled: case.source,
                               source_guard=guard, source_topic=case.topic)
    assert not window.errors


def test_native_saved_source_job_resumes_without_new_transfer_or_mutable_source_controls(
        tmp_path, connection, translation_window):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    case.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    window = translation_window
    frame = window.bind(case)
    def scenario():
        window.click('Saved jobs')
        selected = window.find('Selected text')
        assert selected.value == case.source.text and not selected.editable
        window.click('Translate / resume')
        assert case.service.jobs.load(identity)['status'] == 'complete'
        assert not window.messages  # Original reviewed approval remains exact.
        window.click('New translation')
        assert selected.value == '' and selected.editable
        assert 'no retained-library locator' in window.find('Source selection').value
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    assert not window.errors


def test_closed_translation_window_cannot_start_delayed_source_load(
        tmp_path, connection, translation_window):
    case = bound_ready(tmp_path, connection)
    window = translation_window
    frame = window.bind(case)
    window.state['deferred'] = True
    window.state['scenario'] = lambda: window.click('Close')
    loaded = []
    window.ui.show_translation(frame, source_loader=lambda cancelled: loaded.append(True))
    for execute in window.state['pending']:
        execute()
    assert not loaded and len(connection['calls']) == 1


@pytest.mark.parametrize('revoked', [False, True], ids=['selected-range', 'revoked-in-picker'])
def test_book_translation_menu_reuses_authorized_reader_and_exact_source_range(
        tmp_path, connection, translation_window, monkeypatch, revoked):
    case = bound_ready(tmp_path, connection)
    window = translation_window
    frame = window.bind(case)
    window.state['range'] = '2-2'
    if revoked:
        window.state['during_range'] = lambda: case.app.archive_workspace(case.workspace)
    observed = []
    def opened(parent, *, source_loader, source_guard, source_topic):
        source_guard()
        source = source_loader(lambda: False)
        assert source_topic == case.topic
        observed.append(source)
    monkeypatch.setattr(window.ui, 'show_translation', opened)
    window.ui.install_translation_actions(frame)
    window.menu_handlers[101](None)
    if revoked:
        assert not observed and 'archived' in window.errors[-1]
    else:
        source, = observed
        assert source.text == case.raw.split('\f')[1]
        assert [row['ordinal'] for row in source.positions] == [2]
        assert source.asset_sha256 == case.imported['asset']
        assert not window.errors
    assert len(connection['calls']) == 1


@pytest.mark.parametrize('value', ['0', '-1', '4', '3-2', '1-4', '1-2-3', 'chapter 2', ''])
def test_translation_source_range_rejects_invalid_or_inferred_positions(value):
    from carr_library.ui.translation_dialog import source_range
    with pytest.raises(ValueError):
        source_range(value, 3)


def test_large_translation_output_stops_at_reading_copy_budget_without_rebilling_prefix(tmp_path, connection):
    raw = 'Bound source paragraph. ' * 4000 + '\fSecond section.\fFinal section.'
    case = bound_ready(tmp_path, connection, raw=raw)
    identity, _ = submit_bound(case)
    connection['custom'] = {'data': {'translations': [{'translatedText': 'T' * 190000}]}}
    with pytest.raises(ValueError, match='2,000,000-character'):
        case.service.resume(identity)
    saved = case.service.jobs.load(identity)
    assert saved['status'] == 'attention'
    assert len(saved['receipts']) == 11 < len(saved['chunks'])
    assert sum(len(row['text']) for row in saved['receipts']) == 2_090_000
    calls = len(connection['calls'])
    with pytest.raises(ValueError, match='Accepted chunks were retained'):
        case.service.resume(identity)
    assert len(connection['calls']) == calls == 12
    assert not (tmp_path / 'excess-output').exists()


@pytest.mark.parametrize('cancelled', [False, True], ids=['source-deadline', 'source-cancel'])
def test_source_reader_timeout_retains_its_actual_cancel_or_failure_state(
        tmp_path, connection, monkeypatch, cancelled):
    from carr_library.reader.reading_desk import ReadingDesk
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    def interrupted_page(*args):
        if cancelled:
            stop.append(True)
        raise TimeoutError('Synthetic retained-source read deadline')
    monkeypatch.setattr(ReadingDesk, 'page', interrupted_page)
    if cancelled:
        assert case.service.resume(identity, cancelled=lambda: bool(stop))['status'] == 'paused'
    else:
        with pytest.raises(TimeoutError, match='read deadline'):
            case.service.resume(identity)
    saved = case.service.jobs.load(identity)
    assert saved['status'] == ('paused' if cancelled else 'attention')
    assert saved['receipts'] == [] and len(connection['calls']) == 1


@pytest.mark.parametrize('bound', [False, True], ids=['selected-text', 'library-source'])
def test_translation_text_and_self_hash_cannot_override_the_accepted_decoded_output(
        tmp_path, connection, bound):
    if bound:
        case = bound_ready(tmp_path, connection, first=3, last=3)
        service = case.service
        identity, approval = submit_bound(case)
    else:
        service, p, _ = ready(tmp_path, connection)
        value = 'Previously accepted original.'
        approval, cost = translation_approval(p, value, 'en', 'uk', '1')
        identity = service.submit(p, value, 'en', 'uk', authorization=approval, per_call_cost=cost)
    service.resume(identity)
    saved = service.jobs.load(identity)
    saved['receipts'][0]['text'] = 'A different forged provider translation.'
    saved['receipts'][0]['translation_sha256'] = sha256(saved['receipts'][0]['text'].encode('utf-8')).hexdigest()
    service.jobs.save(saved)
    usage_before = service.gateway.ledger._path(approval.id).read_bytes()
    calls = len(connection['calls'])
    with pytest.raises(ValueError, match='decoded-output'):
        service.resume(identity)
    target = tmp_path / 'forged'
    with pytest.raises(ValueError, match='decoded-output'):
        service.export(identity, target)
    assert not target.exists() and len(connection['calls']) == calls
    assert service.gateway.ledger._path(approval.id).read_bytes() == usage_before


def test_decoded_translation_digest_and_legacy_missing_proof_are_explicit(tmp_path, connection):
    from carr_library.assistant.state import atomic_json
    service, p, _ = ready(tmp_path, connection)
    value = 'Synthetic original.'
    approval, cost = translation_approval(p, value, 'en', 'uk', '1')
    identity = service.submit(p, value, 'en', 'uk', authorization=approval, per_call_cost=cost)
    connection['custom'] = {'data': {'translations': [{'translatedText': 'A &amp; B'}]}}
    service.resume(identity)
    usage = service.gateway.ledger.get(approval.id)
    recorded = usage['operations'][identity + '_0']['usage']
    assert recorded['translation_sha256'] == sha256(b'A & B').hexdigest()
    service.export(identity, tmp_path / 'bound-text')
    report = read_json(tmp_path / 'bound-text' / 'translation-provenance.json')
    assert report['decoded_text_binding'] == 'usage-ledger-sha256'
    del recorded['translation_sha256']  # An actual old v1 receipt lacked this field.
    atomic_json(service.gateway.ledger._path(approval.id), usage)
    calls = len(connection['calls'])
    service.export(identity, tmp_path / 'legacy-text')
    report = read_json(tmp_path / 'legacy-text' / 'translation-provenance.json')
    assert report['decoded_text_binding'] == 'unrecorded-legacy'
    assert (tmp_path / 'legacy-text' / 'translated.txt').read_bytes() == b'A & B'
    assert len(connection['calls']) == calls


def test_source_translation_requires_decoded_output_proof_even_after_restore(tmp_path, connection):
    from carr_library.assistant.state import atomic_json
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, approval = submit_bound(case)
    case.service.resume(identity)
    usage = case.service.gateway.ledger.get(approval.id)
    del usage['operations'][identity + '_0']['usage']['translation_sha256']
    atomic_json(case.service.gateway.ledger._path(approval.id), usage)
    with pytest.raises(ValueError, match='decoded-output'):
        case.service.export(identity, tmp_path / 'missing-proof')
    assert not (tmp_path / 'missing-proof').exists()


def test_source_join_separators_count_toward_the_reading_copy_limit(tmp_path, connection):
    case = bound_ready(tmp_path, connection, first=1, last=1, raw=('S' + ' ' * 4499) * 20)
    identity, _ = submit_bound(case)
    connection['custom'] = {'data': {'translations': [{'translatedText': 'T' * 99000}]}}
    with pytest.raises(ValueError, match='2,000,000-character'):
        case.service.resume(identity)
    saved = case.service.jobs.load(identity)
    assert len(saved['receipts']) == len(saved['chunks']) == 20
    assert sum(len(row['text']) for row in saved['receipts']) == 1_980_000
    assert saved['status'] == 'attention'
    calls = len(connection['calls'])
    with pytest.raises(ValueError, match='2,000,000-character'):
        case.service.resume(identity)
    assert len(connection['calls']) == calls == 21


def test_maximum_source_serialization_keeps_checkpoint_and_single_edit_within_existing_bounds(
        tmp_path, connection):
    from carr_library.assistant.models import canonical
    from carr_library.assistant.state import MAX_CHECKPOINT_BYTES
    from carr_library.speech.reading import MAX_PROJECT_BYTES
    # Intake rejects binary controls: use an admitted four-byte UTF-8 source,
    # then the worst six-byte JSON escape for the translated reading edit.
    # No model or paid translation is used for actual serialization checks.
    case = bound_ready(tmp_path, connection, first=1, last=1, raw='\U0001f642' * 2_000_000)
    identity, _ = submit_bound(case)
    saved = case.service.jobs.load(identity)
    assert len((canonical(saved) + '\n').encode('utf-8')) <= MAX_CHECKPOINT_BYTES
    assert case.service.jobs.path(identity).stat().st_size <= MAX_CHECKPOINT_BYTES
    document = case.source.reading_document()
    document.replace(0, len(document.text), '\x02' * 2_000_000,
                     label='Machine translation derivative; not a source quotation')
    path = tmp_path / 'bounded.reading.json'
    document.save(path)
    assert path.stat().st_size <= MAX_PROJECT_BYTES
    reopened = ReadingDocument.load(path)
    assert reopened.source_text == case.source.text and len(reopened.text) == 2_000_000
    assert len(connection['calls']) == 1  # Earlier language discovery only.


@pytest.mark.parametrize('started', [False, True], ids=['before-first-request', 'accepted-receipt'])
@pytest.mark.parametrize('change_approval', [False, True], ids=['keep-source-approval', 'rewrite-as-excerpt'])
def test_source_job_cannot_downgrade_to_legacy_to_bypass_archival(
        tmp_path, connection, change_approval, started):
    from uuid import UUID
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, approval = submit_bound(case)
    assert UUID(identity).version == 5
    if started:
        case.service.resume(identity)
    else:
        assert not case.service.gateway.ledger._path(approval.id).exists()
    case.app.archive_workspace(case.workspace)
    saved = case.service.jobs.load(identity)
    saved['kind'] = 'translation/1'
    del saved['source_binding']
    if change_approval:
        saved['authorization']['data_classes'] = ['selected-excerpt']
    case.service.jobs.save(saved)
    calls = len(connection['calls'])
    with pytest.raises(ValueError, match='legacy job format'):
        case.service.export(identity, tmp_path / 'downgraded')
    with pytest.raises(ValueError, match='legacy job format'):
        case.service.resume(identity)
    assert len(connection['calls']) == calls and not (tmp_path / 'downgraded').exists()
    if not started:
        assert not case.service.gateway.ledger._path(approval.id).exists()


def change_saved_account(case, mode):
    from dataclasses import replace
    store = ProfileStore(case.app.root / 'state' / 'providers')
    if mode == 'missing':
        store.remove(case.profile.id)
    elif mode == 'disabled':
        store.save(replace(case.profile, enabled=False))
    else:
        store.save(replace(case.profile, name='Changed account',
                           credential_ref='carr/provider/google_test/' + 'b' * 32))


@pytest.mark.parametrize('mode', ['missing', 'changed'])
def test_native_saved_resume_requires_the_exact_visible_account_before_gateway(
        tmp_path, connection, translation_window, mode):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    case.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    saved = case.service.jobs.load(identity)
    window = translation_window
    frame = window.bind(case)
    change_saved_account(case, mode)
    calls = len(connection['calls'])
    def scenario():
        window.find('Optional price').ChangeValue('99')
        window.click('Saved jobs')
        account = window.find('Google Basic v2')
        assert account.GetSelection() == -1
        assert window.find('Optional price').GetValue() == ''
        status = window.find('Preview, supported')
        for value in (identity, case.profile.id, case.profile.name, 'en to uk', '2 USD'):
            assert value in status.GetValue()
        account.SetFocus()
        window.click('Translate / resume')
        assert 'Visible settings differ' in status.GetValue()
        assert window.state['focused'] == status.name
        assert case.service.jobs.load(identity) == saved
        assert len(connection['calls']) == calls and not window.gateways
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    assert not window.errors


@pytest.mark.parametrize('mode', ['missing', 'disabled', 'changed'])
def test_native_saved_resume_rereads_catalog_after_dialog_and_worker_queue(
        tmp_path, connection, translation_window, mode):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    case.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    saved = case.service.jobs.load(identity)
    window = translation_window
    frame = window.bind(case)
    calls = len(connection['calls'])
    def scenario():
        window.click('Saved jobs')
        account = window.find('Google Basic v2')
        assert account.GetSelection() == 0
        window.state['deferred'] = True
        account.SetFocus()
        window.click('Translate / resume')
        change_saved_account(case, mode)
        pending, = window.state['pending']
        window.state['pending'].clear()
        pending()
        status = window.find('Preview, supported')
        assert 'unavailable, disabled or changed' in status.GetValue()
        assert window.state['focused'] == status.name
        assert case.service.jobs.load(identity) == saved
        assert len(connection['calls']) == calls and not window.gateways
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    assert not window.errors


@pytest.mark.parametrize('wrong_account', [False, True], ids=['exact-account', 'other-visible-account'])
def test_native_saved_resume_matches_visible_account_and_numeric_budget(
        tmp_path, connection, translation_window, wrong_account):
    case = bound_ready(tmp_path, connection)
    identity, _ = submit_bound(case)
    stop = []
    case.service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))
    saved = case.service.jobs.load(identity)
    window = translation_window
    frame = window.bind(case)
    ProfileStore(case.app.root / 'state' / 'providers').save(profile(
        id='other_google', name='Another account', credential_ref='carr/provider/other_google/' + 'b' * 32))
    calls = len(connection['calls'])
    def scenario():
        window.click('Saved jobs')
        account = window.find('Google Basic v2')
        assert account.GetSelection() == 0
        account.SetSelection(1 if wrong_account else 0)
        window.find('Maximum approved').ChangeValue('2.00')
        account.SetFocus()
        window.click('Translate / resume')
        if wrong_account:
            status = window.find('Preview, supported')
            assert 'Visible settings differ' in status.GetValue()
            assert window.state['focused'] == status.name
            assert case.service.jobs.load(identity) == saved
            assert len(connection['calls']) == calls and not window.gateways
        else:
            assert case.service.jobs.load(identity)['status'] == 'complete'
            assert len(connection['calls']) - calls == len(saved['chunks']) - len(saved['receipts'])
            assert window.gateways == [True]
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    assert not window.errors


@pytest.mark.parametrize('mode', ['missing', 'changed'])
def test_native_completed_export_keeps_original_lineage_without_current_account(
        tmp_path, connection, translation_window, mode):
    case = bound_ready(tmp_path, connection, first=3, last=3)
    identity, _ = submit_bound(case)
    case.service.resume(identity)
    window = translation_window
    frame = window.bind(case)
    change_saved_account(case, mode)
    calls, secrets = len(connection['calls']), len(case.vault.calls)
    def scenario():
        window.click('Saved jobs')
        assert window.find('Google Basic v2').GetSelection() == -1
        window.click('Export')
        target, = tmp_path.glob('Translation-*')
        reading = ReadingDocument.load(target / 'translation.reading.json')
        assert reading.source_text == case.source.text
        assert reading.source_anchor == case.document.source_anchor
        assert read_json(target / 'translation-provenance.json')['job_id'] == identity
        assert len(connection['calls']) == calls and len(case.vault.calls) == secrets
        assert not window.gateways
        window.click('Close')
    window.state['scenario'] = scenario
    window.ui.show_translation(frame)
    assert not window.errors
