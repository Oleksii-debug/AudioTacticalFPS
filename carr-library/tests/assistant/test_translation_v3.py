"""Actual shared gateway/jobs with synthetic Google REST responses, never live API."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import ssl
import signal
import sys
from types import SimpleNamespace
from time import time
from uuid import uuid4

import pytest

from carr_library.assistant.profiles import (
    ProviderProfile, ProfileStore, google_oauth_credential, google_oauth_token, save_google_token,
)
from carr_library.assistant.providers import Authorization, Gateway, ProviderError
from carr_library.assistant.state import read_json
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.assistant import translation_cli, providers as provider_transport
from carr_library.application import LibraryApp
from carr_library.ui import translation_dialog as ui
from carr_library.speech.reading import ReadingDocument


PARENT = 'projects/123456789/locations/us-central1'
ENDPOINT = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/reviewed-terms'
TOKEN = 'SYNTHETIC_OAUTH_TOKEN_NO_CREDENTIAL'


def profile(**changes):
    values = dict(id='advanced_test', name='Synthetic Advanced account', adapter='google-translation-v3',
                  endpoint=ENDPOINT, model='general/nmt', capabilities=('translation.text', 'translation.languages'),
                  credential_ref='carr/provider/advanced_test/' + 'a' * 32, enabled=True)
    return ProviderProfile(**{**values, **changes})


class Vault:
    def __init__(self):
        self.records = {profile().credential_ref: google_oauth_credential(TOKEN, time() + 3500, ENDPOINT)}
        self.calls = []

    def get(self, identity, reference):
        self.calls.append(('get', identity, reference))
        return self.records[reference]

    def put(self, identity, value, *, reference=None):
        reference = reference or ('carr/provider/' + identity + '/' + uuid4().hex)
        self.calls.append(('put', identity, reference))
        self.records[reference] = value
        return reference

    def delete(self, identity, reference):
        self.calls.append(('delete', identity, reference))
        del self.records[reference]


@pytest.fixture
def transport(monkeypatch):
    state = {'calls': [], 'status': 200, 'response': None, 'closed': 0}
    class Connection:
        def __init__(self, host, port, **kwargs):
            assert host == 'translation.googleapis.com' and port in (None, 443)
            assert isinstance(kwargs['context'], ssl.SSLContext)
            self.position = 0
            self.deadline, self.cancelled = kwargs['deadline'], kwargs['cancelled']

        def check(self):
            provider_transport._check_request(self.deadline, self.cancelled)

        def request(self, method, route, body, headers):
            state['calls'].append({'method': method, 'route': route, 'body': body, 'headers': dict(headers)})
            if method == 'GET':
                document = {'languages': [
                    {'languageCode': 'en', 'displayName': 'English', 'supportSource': True, 'supportTarget': True},
                    {'languageCode': 'uk', 'displayName': 'Ukrainian', 'supportSource': True, 'supportTarget': True},
                    {'languageCode': 'de', 'displayName': 'German source only', 'supportSource': True},
                    {'languageCode': 'fr', 'displayName': 'French target only', 'supportTarget': True},
                ]}
            else:
                payload = json.loads(body)
                row = {'translatedText': 'Похідний текст &amp; ' + payload['contents'][0], 'model': payload['model']}
                if 'sourceLanguageCode' not in payload:
                    row['detectedLanguageCode'] = 'en'
                document = {'translations': [row]}
                if 'glossaryConfig' in payload:
                    document['glossaryTranslations'] = [{**row, 'translatedText': 'Глосарій ' + row['translatedText'],
                                                        'glossaryConfig': payload['glossaryConfig']}]
            if state['response'] is not None:
                document = state['response'](document) if callable(state['response']) else state['response']
            self.raw = json.dumps(document, ensure_ascii=False).encode('utf-8')
            if state.get('escape_secret'):
                secret = state['escape_secret']
                escaped = ''.join('\\u%04x' % ord(char) for char in secret).encode('ascii')
                self.raw = self.raw.replace(secret.encode('utf-8'), escaped)


        def getresponse(self):
            self.status = state['status']
            return self

        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)

        def read(self, size):
            result = self.raw[self.position:self.position + size]
            self.position += len(result)
            return result

        def close(self):
            state['closed'] += 1
    def configured(target, *, local, deadline, cancelled):
        assert not local
        return Connection(target.hostname, target.port, context=ssl.create_default_context(),
                          deadline=deadline, cancelled=cancelled)

    # This fixture exercises the canonical gateway/ledger with synthetic replies;
    # the separate verified loopback TLS suite exercises the actual transport.
    def no_dns(*args, **kwargs):
        raise AssertionError('Synthetic Advanced v3 tests must not resolve or contact any provider.')

    monkeypatch.setattr(provider_transport.socket, 'getaddrinfo', no_dns)
    monkeypatch.setattr(provider_transport, '_Connection', configured)
    return state


def ready(tmp_path, transport):
    vault = Vault()
    service = TranslationService(tmp_path / 'translation',
        gateway=Gateway(tmp_path / 'translation' / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    p = profile()
    approval = Authorization('discovery', 'discovery', ((p.id, p.endpoint),),
                            ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return service, p, vault


def submit(service, p, value='Unchanged original source.', **kwargs):
    approval, cost = translation_approval(p, value, 'en', 'uk', '1', **kwargs)
    identity = service.submit(p, value, 'en', 'uk', authorization=approval, per_call_cost=cost, **kwargs)
    return identity, approval


@pytest.mark.parametrize('change', [
    {'endpoint': 'https://other.example/v3/' + PARENT},
    {'endpoint': ENDPOINT + '?key=hidden'}, {'endpoint': ENDPOINT + '/:translateText'},
    {'endpoint': ENDPOINT.replace('/123456789/', '/project-name/')},
    {'endpoint': ENDPOINT.replace('/123456789/', '/0/')},
    {'endpoint': ENDPOINT.replace('/us-central1', '/us-central1/../global')},
    {'endpoint': ENDPOINT.replace('https:', 'http:')},
    {'endpoint': ENDPOINT.replace('googleapis.com', 'googleapis.com:8443')},
    {'model': 'general/translation-llm'}, {'model': 'nmt'}, {'local': True}, {'capabilities': ('llm.chat',)},
])
def test_explicit_advanced_identity_rejects_wrong_authority(change):
    with pytest.raises(ValueError):
        profile(**change)


def test_local_only_denies_before_vault_or_network(tmp_path, transport):
    vault = Vault()
    with pytest.raises(PermissionError, match='LOCAL ONLY'):
        Gateway(tmp_path, vault=vault).request(profile(), 'translation.text', {'q': 'Text', 'target': 'uk'},
            task_id='task', data_class='selected-excerpt')
    assert not vault.calls and not transport['calls']


def test_language_inventory_uses_oauth_project_and_direction_flags(tmp_path, transport):
    service, p, vault = ready(tmp_path, transport)
    call, = transport['calls']
    assert call['method'] == 'GET' and call['body'] is None
    assert call['route'].startswith('/v3/' + PARENT + '/supportedLanguages?displayLanguageCode=en&model=projects%2F123456789')
    assert call['headers']['Authorization'] == 'Bearer ' + TOKEN
    assert call['headers']['X-goog-user-project'] == '123456789'
    assert 'X-goog-api-key' not in call['headers'] and TOKEN not in call['route']
    assert service.languages(p)[2] == {'language': 'de', 'name': 'German source only', 'source': True, 'target': False}
    assert service.preview(p, 'Source', 'de', 'fr')['calls'] == 1
    for source, target in [('fr', 'uk'), ('en', 'de'), ('auto', 'de')]:
        with pytest.raises(ValueError, match='not declared supported'):
            service.preview(p, 'Source', source, target)
    assert len(vault.calls) == 1 and len(transport['calls']) == 1


def test_actual_service_writes_source_preserving_glossary_derivative(tmp_path, transport):
    service, p, _ = ready(tmp_path, transport)
    value = 'Literal &amp; original Україна.'
    identity, approval = submit(service, p, value, glossary=GLOSSARY)
    assert service.resume(identity)['status'] == 'complete'
    call = transport['calls'][-1]
    assert call['route'] == '/v3/' + PARENT + ':translateText' and call['method'] == 'POST'
    payload = json.loads(call['body'])
    assert payload == {'contents': [value], 'sourceLanguageCode': 'en', 'targetLanguageCode': 'uk',
                       'mimeType': 'text/plain', 'model': PARENT + '/models/general/nmt',
                       'glossaryConfig': {'glossary': GLOSSARY, 'ignoreCase': False}}
    assert sha256(call['body']).hexdigest() == approval.request_hashes[0]
    output = tmp_path / 'fresh export'
    service.export(identity, output)
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.source_text == value
    assert reading.text.startswith('Глосарій ') and '&amp;' in reading.text
    assert reading.source_span(0, len(reading.text)).exact is False
    receipt = read_json(output / 'translation-provenance.json')
    assert receipt['glossary'] == GLOSSARY and receipt['provider_endpoint'] == ENDPOINT
    assert receipt['chunks'][0]['glossary_config'] == payload['glossaryConfig']
    assert receipt['chunks'][0]['reported_model'] == payload['model']
    assert receipt['quotation_authority'] is False
    assert all(sha256((output / name).read_bytes()).hexdigest() == digest for name, digest in receipt['files'].items())
    with pytest.raises(FileExistsError):
        service.export(identity, output)
    for path in service.directory.rglob('*.json'):
        assert TOKEN not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('kind', ['missing_glossary', 'different_glossary', 'case_insensitive', 'missing_model', 'different_model'])
def test_never_accepts_plain_result_when_glossary_or_model_unverified(tmp_path, transport, kind):
    service, p, _ = ready(tmp_path, transport)
    identity, approval = submit(service, p, glossary=GLOSSARY)
    def wrong(document):
        if kind == 'missing_glossary':
            del document['glossaryTranslations']
        elif kind == 'different_glossary':
            document['glossaryTranslations'][0]['glossaryConfig']['glossary'] += '-other'
        elif kind == 'case_insensitive':
            document['glossaryTranslations'][0]['glossaryConfig']['ignoreCase'] = True
        elif kind == 'missing_model':
            del document['glossaryTranslations'][0]['model']
        else:
            document['glossaryTranslations'][0]['model'] = PARENT.replace('123456789', '987654321') + '/models/general/nmt'
        return document
    transport['response'] = wrong
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert service.jobs.load(identity)['receipts'] == []
    assert next(iter(service.gateway.ledger.get(approval.id)['operations'].values()))['status'] == 'uncertain'
    before = len(transport['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    assert len(transport['calls']) == before


@pytest.mark.parametrize('glossary,source', [
    (GLOSSARY, 'auto'), (GLOSSARY.replace('123456789', '999999999'), 'en'),
    (GLOSSARY.replace('us-central1', 'europe-west1'), 'en'), (GLOSSARY + '/../../other', 'en'),
])
def test_invalid_glossary_fails_before_any_transfer(tmp_path, transport, glossary, source):
    service, p, _ = ready(tmp_path, transport)
    with pytest.raises(ValueError, match='glossary'):
        service.preview(p, 'Source', source, 'uk', glossary=glossary)
    assert len(transport['calls']) == 1


def test_approval_binds_exact_glossary(tmp_path, transport):
    service, p, _ = ready(tmp_path, transport)
    approval, cost = translation_approval(p, 'Source', 'en', 'uk', '1', glossary=GLOSSARY)
    with pytest.raises(PermissionError, match='exact selected text'):
        service.submit(p, 'Source', 'en', 'uk', glossary=GLOSSARY + '-changed', authorization=approval, per_call_cost=cost)
    assert not service.list() and len(transport['calls']) == 1


@pytest.mark.parametrize('record', ['AIzaSyntheticKey', '{"access_token":"hidden"}', '[]'])
def test_untyped_vault_values_are_never_sent_as_v3_bearer(tmp_path, transport, record):
    service, p, vault = ready(tmp_path, transport)
    identity, approval = submit(service, p)
    vault.records[p.credential_ref] = record
    with pytest.raises(ProviderError, match='OAuth token expired or is invalid'):
        service.resume(identity)
    assert len(transport['calls']) == 1
    assert next(iter(service.gateway.ledger.get(approval.id)['operations'].values()))['status'] == 'not_sent'


@pytest.mark.parametrize('expires', [None, True, 0, float('inf'), float('nan'), 10**400, '2026-11-27T12:00:00', 'not-time'])
def test_token_expiry_validation_is_bounded_and_explicit(expires):
    with pytest.raises(ValueError):
        google_oauth_credential(TOKEN, expires, ENDPOINT)


def test_token_record_iso_expiry_and_endpoint_binding():
    stamp = datetime.fromtimestamp(time() + 300, timezone.utc).isoformat()
    record = google_oauth_credential(TOKEN, stamp, ENDPOINT)
    assert google_oauth_token(record, ENDPOINT)[0] == TOKEN
    with pytest.raises(ValueError, match='endpoint'):
        google_oauth_token(record, ENDPOINT.replace('123456789', '234567890'))


def test_expiry_reentry_resumes_same_job_without_rebilling(tmp_path, transport):
    service, p, vault = ready(tmp_path, transport)
    store = ProfileStore(tmp_path / 'profiles'); store.save(p)
    identity, approval = submit(service, p, 'Long approved source. ' * 550, glossary=GLOSSARY)
    stop = []
    assert service.resume(identity, cancelled=lambda: bool(stop), progress=lambda *args: stop.append(True))['completed'] == 1
    record = json.loads(vault.records[p.credential_ref]); record['expires_at'] = time() - 1
    vault.records[p.credential_ref] = json.dumps(record)
    before = len(transport['calls'])
    with pytest.raises(ProviderError, match='expired'):
        service.resume(identity)
    assert len(transport['calls']) == before
    renewed = save_google_token(store, vault, p, TOKEN + '_RENEWED', time() + 3500, current=p)
    assert renewed == p and store.get(p.id) == p
    reopened = TranslationService(service.directory, gateway=service.gateway)
    result = reopened.resume(identity)
    assert result['status'] == 'complete'
    assert len(transport['calls']) - before == result['total'] - 1
    assert transport['calls'][-1]['headers']['Authorization'] == 'Bearer ' + TOKEN + '_RENEWED'
    count = len(transport['calls']); reopened.resume(identity)
    assert len(transport['calls']) == count
    operations = service.gateway.ledger.get(approval.id)['operations']
    assert all(row['status'] == 'complete' for row in operations.values())
    assert sum(Decimal(row['reserved_cost']) for row in operations.values()) <= Decimal('1')


def test_profile_export_contains_neither_token_nor_vault_reference(tmp_path):
    store = ProfileStore(tmp_path / 'profiles'); vault = Vault()
    p = profile(credential_ref=None, enabled=False)
    saved = save_google_token(store, vault, p, TOKEN, time() + 3500)
    assert saved.credential_ref and TOKEN not in store.path(p.id).read_text()
    document = json.loads(store.export_json())
    assert document['profiles'][0]['credential_ref'] is None
    assert document['profiles'][0]['enabled'] is False
    assert TOKEN not in json.dumps(document)


def test_completed_glossary_receipt_cannot_be_rebound(tmp_path, transport):
    service, p, _ = ready(tmp_path, transport)
    identity, _ = submit(service, p, glossary=GLOSSARY)
    service.resume(identity)
    job = service.jobs.load(identity)
    job['receipts'][0]['glossary_config']['glossary'] += '-changed'
    service.jobs.save(job)
    with pytest.raises(ValueError, match='provenance'):
        service.export(identity, tmp_path / 'unpublished')
    assert not (tmp_path / 'unpublished').exists()


@pytest.mark.parametrize('mutation', ['model', 'glossary', 'target', 'endpoint'])
def test_saved_request_cannot_silently_expand_original_approval(tmp_path, transport, mutation):
    service, p, _ = ready(tmp_path, transport)
    identity, _ = submit(service, p, glossary=GLOSSARY)
    job = service.jobs.load(identity)
    if mutation == 'model':
        job['profile']['model'] = 'general/translation-llm'
    elif mutation == 'glossary':
        job['glossary'] += '-changed'
    elif mutation == 'target':
        job['target_language'] = 'fr'
    else:
        job['profile']['endpoint'] = ENDPOINT.replace('us-central1', 'global')
    service.jobs.save(job)
    with pytest.raises((ValueError, PermissionError)):
        service.resume(identity)
    assert len(transport['calls']) == 1


def test_cli_requires_exact_review_then_uses_existing_jobs_and_gateway(tmp_path, transport, monkeypatch, capsys):
    app = LibraryApp(tmp_path / 'library')
    ProfileStore(app.root / 'state' / 'providers').save(profile())
    vault = Vault()
    monkeypatch.setattr(translation_cli, 'Gateway', lambda directory, **kwargs: Gateway(directory, vault=vault, **kwargs))
    base = ['--data-dir', str(app.root)]
    assert translation_cli.main(base + ['profiles']) == 0
    profiles = json.loads(capsys.readouterr().out)
    assert profiles[0]['adapter'] == 'google-translation-v3' and 'credential_ref' not in profiles[0]
    assert not transport['calls']
    assert translation_cli.main(base + ['languages', '--profile', profile().id]) == 0
    assert json.loads(capsys.readouterr().out) == [] and not transport['calls']
    assert translation_cli.main(base + ['languages', '--profile', profile().id, '--approve-remote']) == 0
    assert json.loads(capsys.readouterr().out)[2]['target'] is False
    original = tmp_path / 'selected.txt'; original.write_text('Exact reviewed source.', encoding='utf-8')
    args = ['--profile', profile().id, '--input', str(original), '--source', 'en', '--target', 'uk',
            '--budget', '1', '--glossary', GLOSSARY]
    assert translation_cli.main(base + ['preview'] + args) == 0
    review = json.loads(capsys.readouterr().out)
    assert review['glossary'] == GLOSSARY and review['endpoint'] == ENDPOINT
    submit_args = base + ['submit'] + args + ['--review-sha256', review['review_sha256']]
    assert translation_cli.main(submit_args) == 1
    assert 'Explicitly approve' in capsys.readouterr().err
    assert translation_cli.main(submit_args + ['--approve-remote']) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert len(transport['calls']) == 1 and submitted['status'] == 'queued'
    previous = signal.getsignal(signal.SIGINT)
    assert translation_cli.main(base + ['resume', submitted['id']]) == 0
    assert signal.getsignal(signal.SIGINT) is previous
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    destination = tmp_path / 'cli export'
    assert translation_cli.main(base + ['export', submitted['id'], '--output', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert (destination / 'original.txt').read_text() == original.read_text()
    before = len(transport['calls'])
    assert translation_cli.main(base + ['jobs']) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved[0]['glossary'] == GLOSSARY and 'source_text' not in saved[0]
    assert len(transport['calls']) == before


@pytest.mark.parametrize('mutation', ['text', 'glossary', 'target', 'budget', 'profile'])
def test_cli_rejects_stale_preview_before_job_publication(tmp_path, transport, monkeypatch, capsys, mutation):
    app = LibraryApp(tmp_path / 'library')
    p = profile(); store = ProfileStore(app.root / 'state' / 'providers'); store.save(p)
    vault = Vault()
    monkeypatch.setattr(translation_cli, 'Gateway', lambda directory, **kwargs: Gateway(directory, vault=vault, **kwargs))
    base = ['--data-dir', str(app.root)]
    translation_cli.main(base + ['languages', '--profile', p.id, '--approve-remote']); capsys.readouterr()
    source = tmp_path / 'source.txt'; source.write_text('Original source', encoding='utf-8')
    args = ['--profile', p.id, '--input', str(source), '--source', 'en', '--target', 'uk', '--budget', '1', '--glossary', GLOSSARY]
    assert translation_cli.main(base + ['preview'] + args) == 0
    review = json.loads(capsys.readouterr().out)
    if mutation == 'text':
        source.write_text('Changed source', encoding='utf-8')
    elif mutation == 'profile':
        store.save(replace(p, name='Changed selected account'))
    else:
        option = '--' + mutation
        args[args.index(option) + 1] = {'glossary': GLOSSARY + '-other', 'target': 'fr', 'budget': '2'}[mutation]
    assert translation_cli.main(base + ['submit'] + args + ['--approve-remote', '--review-sha256', review['review_sha256']]) == 1
    assert 'current preview hash' in capsys.readouterr().err
    assert not TranslationService(app.root / 'state' / 'translation').list()
    assert len(transport['calls']) == 1


def test_stale_account_editor_cannot_replace_newer_credential(tmp_path):
    p = profile(); store = ProfileStore(tmp_path / 'profiles'); store.save(p)
    vault = Vault(); before = dict(vault.records)
    store.save(replace(p, endpoint=ENDPOINT.replace('123456789', '234567890')))
    with pytest.raises(ValueError, match='account changed'):
        save_google_token(store, vault, p, TOKEN + '_NEW', time() + 300, current=p)
    assert vault.records == before and not vault.calls


@pytest.fixture
def translation_handlers(tmp_path, transport, monkeypatch):
    """Actual handler closures; widget/scheduler stand-ins are not native proof."""
    state = {'widgets': [], 'queue': [], 'approvals': [], 'scenario': None}
    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
            self.value, self.name, self.label, self.choices = value, '', label, choices
            self.enabled, self.selection, self.bindings, self.focused = True, -1, {}, False
            state['widgets'].append(self)
        def SetName(self, name): self.name = name
        def SetSelection(self, value): self.selection = value
        def GetSelection(self): return self.selection
        def GetValue(self): return self.value
        def ChangeValue(self, value): self.value = value
        def Enable(self, value=True): self.enabled = value
        def Disable(self): self.enabled = False
        def SetFocus(self): self.focused = True
        def Bind(self, event, callback): self.bindings[event] = callback
        def SetSizer(self, sizer): pass
        def EndModal(self, result): pass
        def Destroy(self): pass
        def ShowModal(self):
            state['scenario']()
            return 1
    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate((
        'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
        'TE_MULTILINE', 'TE_READONLY', 'YES_NO', 'NO_DEFAULT', 'YES', 'ID_OK', 'ID_CLOSE', 'EVT_BUTTON', 'EVT_CLOSE'))})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: callback(*args)
    def approve(message, *args):
        state['approvals'].append(message)
        return wx.YES
    wx.MessageBox = approve
    monkeypatch.setitem(sys.modules, 'wx', wx)
    app = LibraryApp(tmp_path / 'library')
    p = profile(); ProfileStore(app.root / 'state' / 'providers').save(p)
    vault = Vault()
    monkeypatch.setattr(ui, 'Gateway', lambda directory, **kwargs: Gateway(directory, vault=vault, **kwargs))
    frame = SimpleNamespace(library=app, working=False, _closing=False, cancel_event=__import__('threading').Event())
    def run_job(title, callback, done, failed):
        state['queue'].append((callback, done, failed))
        return True
    frame.run_job = run_job
    def find(name): return next(widget for widget in state['widgets'] if widget.name == name)
    def press(name): find(name).bindings[wx.EVT_BUTTON](None)
    def complete():
        callback, done, failed = state['queue'].pop(0)
        try: result = callback()
        except Exception as error: failed(error)
        else: done(result)
    def scenario(callback):
        state['scenario'] = callback
        ui.show_translation(frame)
    return SimpleNamespace(state=state, frame=frame, find=find, press=press, complete=complete, scenario=scenario,
                           service=TranslationService(app.root / 'state' / 'translation'), p=p, vault=vault)


def prepare_handler(e):
    e.find('Source language code; auto detects').ChangeValue('en')
    e.find('Target language code').ChangeValue('uk')
    e.find('Advanced v3 glossary resource; blank for none').ChangeValue(GLOSSARY)
    e.find('Selected text; originals remain unchanged').ChangeValue('Handler reviewed original.')
    e.find('Maximum approved budget in USD; no default price').ChangeValue('1')
    e.press('Fetch languages'); e.complete()


def test_actual_native_handlers_queue_reviewed_glossary_and_focus_readable_completion(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e)
        assert GLOSSARY not in e.state['approvals'][0]  # Discovery sends no glossary or text.
        e.press('Preview')
        assert GLOSSARY in e.find('Preview, supported languages and job status').value
        e.press('Translate / resume')
        assert len(transport['calls']) == 1 and len(e.state['queue']) == 1
        assert not e.find('Advanced v3 glossary resource; blank for none').enabled
        assert GLOSSARY in e.state['approvals'][-1] and ENDPOINT in e.state['approvals'][-1]
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'complete' and job['glossary'] == GLOSSARY
        assert e.find('Preview, supported languages and job status').focused
        assert e.find('Selected text; originals remain unchanged').value == 'Handler reviewed original.'
    e.scenario(scenario)


@pytest.mark.parametrize('field,value', [
    ('Advanced v3 glossary resource; blank for none', GLOSSARY + '-changed'),
    ('Selected text; originals remain unchanged', 'Changed after preview'),
    ('Target language code', 'en'), ('Maximum approved budget in USD; no default price', '2'),
    ('Optional price per million characters in USD; your current contract rate', '999'),
])
def test_actual_handlers_reject_changed_review_without_call(translation_handlers, transport, field, value):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview')
        e.find(field).ChangeValue(value); e.press('Translate / resume')
        assert 'Preview this exact' in e.find('Preview, supported languages and job status').value
        assert not e.state['queue'] and not e.service.list()
        assert len(transport['calls']) == 1
    e.scenario(scenario)


def test_handler_close_before_queued_request_preserves_paused_job_without_transmission(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume'); e.press('Close')
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'paused' and not job['receipts']
        assert len(transport['calls']) == 1
    e.scenario(scenario)


def test_handler_root_change_before_worker_does_not_send_old_library_text(translation_handlers, transport, tmp_path):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume')
        e.frame.library = LibraryApp(tmp_path / 'different library')
        e.complete()
        assert 'library changed' in e.find('Preview, supported languages and job status').value
        job, = e.service.list()
        assert job['status'] == 'queued' and not job['receipts']
        assert len(transport['calls']) == 1
        e.press('Translate / resume')
        assert not e.state['queue'] and len(e.service.list()) == 1
    e.scenario(scenario)


@pytest.mark.parametrize('source, expected', [
    pytest.param('A' * 4498 + '\n\nB', 'Похідний &amp;\n\nПохідний &amp;', id='paragraphs'),
    pytest.param('A' * 4499 + ' ' * 4501 + '\tB',
                 'Похідний &amp;' + ' ' * 4501 + '\tПохідний &amp;', id='whitespace-only-chunk'),
    pytest.param('漢' * 4501, 'Похідний &amp;Похідний &amp;', id='no-space-script'),
])
def test_advanced_export_keeps_canonical_chunk_joins_and_raw_receipts(tmp_path, transport, source, expected):
    service, p, _ = ready(tmp_path, transport)
    transport['response'] = {'translations': [{'translatedText': 'Похідний &amp;',
                                              'model': PARENT + '/models/general/nmt'}]}
    identity, _ = submit(service, p, source)
    assert service.resume(identity)['status'] == 'complete'
    saved = service.jobs.path(identity).read_bytes()
    before = len(transport['calls'])
    service.export(identity, tmp_path / 'joined-export')
    output = tmp_path / 'joined-export'
    assert (output / 'translated.txt').read_bytes().decode('utf-8') == expected
    assert (output / 'original.txt').read_bytes().decode('utf-8') == source
    reading = ReadingDocument.load(output / 'translation.reading.json')
    assert reading.text == expected and reading.source_text == source
    assert service.jobs.path(identity).read_bytes() == saved
    assert len(transport['calls']) == before


@pytest.mark.parametrize('translated', ['', ' \t\r\n', '\u2003\u00a0'])
def test_advanced_empty_output_never_publishes_or_rebills(tmp_path, transport, translated):
    service, p, _ = ready(tmp_path, transport)
    transport['response'] = {'translations': [{'translatedText': translated,
                                              'model': PARENT + '/models/general/nmt'}]}
    identity, approval = submit(service, p)
    with pytest.raises(ProviderError):
        service.resume(identity)
    job = service.jobs.load(identity)
    assert job['status'] == 'attention' and not job['receipts']
    row = service.gateway.ledger.get(approval.id)['operations'][identity + '_0']
    assert row['status'] == 'uncertain' and row['output_sha256'] is None
    before = len(transport['calls'])
    with pytest.raises(ProviderError, match='already been attempted'):
        service.resume(identity)
    with pytest.raises(ValueError):
        service.export(identity, tmp_path / 'empty-export')
    assert len(transport['calls']) == before
    assert not (tmp_path / 'empty-export').exists()


@pytest.mark.parametrize('encoding', ['html', 'json_unicode'])
@pytest.mark.parametrize('operation', ['translation.text', 'translation.languages'])
def test_advanced_decoded_credentials_never_reach_local_publication(tmp_path, transport, encoding, operation):
    service, p, _ = ready(tmp_path, transport)
    encoded = ''.join('&#%d;' % ord(char) for char in TOKEN) if encoding == 'html' else TOKEN
    if operation == 'translation.text':
        transport['response'] = {'translations': [{'translatedText': encoded,
                                                  'model': PARENT + '/models/general/nmt'}]}
    else:
        transport['response'] = {'languages': [{'languageCode': 'uk', 'displayName': encoded,
                                               'supportTarget': True}]}
    if encoding == 'json_unicode':
        transport['escape_secret'] = TOKEN
    if operation == 'translation.text':
        identity, approval = submit(service, p)
        with pytest.raises(ProviderError, match='credential material') as failure:
            service.resume(identity)
        assert not service.jobs.load(identity)['receipts']
    else:
        before = service._languages_path(p).read_bytes()
        approval = Authorization('renew_discovery', 'renew_discovery', ((p.id, p.endpoint),),
            ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
        with pytest.raises(ProviderError, match='credential material') as failure:
            service.refresh_languages(p, authorization=approval)
        assert service._languages_path(p).read_bytes() == before
    assert TOKEN not in str(failure.value)
    assert all(row['status'] == 'uncertain' and row['output_sha256'] is None
               for row in service.gateway.ledger.get(approval.id)['operations'].values())
    for path in service.directory.rglob('*.json'):
        assert TOKEN not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('action', ['resume', 'export'])
@pytest.mark.parametrize('mutation', ['source', 'target', 'profile', 'credential', 'cost',
                                     'authorization', 'request', 'response', 'model', 'glossary'])
def test_advanced_completed_receipts_keep_canonical_authority(tmp_path, transport, action, mutation):
    service, p, _ = ready(tmp_path, transport)
    identity, approval = submit(service, p, glossary=GLOSSARY)
    assert service.resume(identity)['status'] == 'complete'
    job = service.jobs.load(identity)
    if mutation == 'source':
        job['source_language'] = 'de'
    elif mutation == 'target':
        job['target_language'] = 'fr'
    elif mutation == 'profile':
        job['profile']['name'] = 'Different account snapshot'
    elif mutation == 'credential':
        job['profile']['credential_ref'] = 'carr/provider/' + p.id + '/' + 'b' * 32
    elif mutation == 'cost':
        job['per_call_cost'] = '0.5'
    elif mutation == 'authorization':
        job['authorization']['budget'] = '2'
    elif mutation == 'request':
        job['receipts'][0]['request_sha256'] = '0' * 64
    elif mutation == 'response':
        job['receipts'][0]['response_sha256'] = '0' * 64
    elif mutation == 'model':
        job['receipts'][0]['reported_model'] = PARENT.replace('123456789', '987654321') + '/models/general/nmt'
    else:
        job['receipts'][0]['glossary_config']['glossary'] += '-different'
    service.jobs.save(job)
    saved = service.jobs.path(identity).read_bytes()
    before = len(transport['calls'])
    destination = tmp_path / 'changed-export'
    with pytest.raises((ValueError, PermissionError)):
        if action == 'resume':
            service.resume(identity)
        else:
            service.export(identity, destination)
    assert not destination.exists()
    assert len(transport['calls']) == before
    assert service.jobs.path(identity).read_bytes() == saved
    assert service.gateway.ledger.get(approval.id)['operations'][identity + '_0']['status'] == 'complete'


@pytest.mark.parametrize('detected', [False, 0, [], {}])
def test_advanced_rejects_nonstring_detected_language_before_receipt(tmp_path, transport, detected):
    service, p, _ = ready(tmp_path, transport)
    identity, approval = submit(service, p)
    transport['response'] = {'translations': [{'translatedText': 'Valid text',
        'model': PARENT + '/models/general/nmt', 'detectedLanguageCode': detected}]}
    with pytest.raises(ProviderError, match='detected translation language'):
        service.resume(identity)
    assert not service.jobs.load(identity)['receipts']
    row = service.gateway.ledger.get(approval.id)['operations'][identity + '_0']
    assert row['status'] == 'uncertain' and row['output_sha256'] is None
