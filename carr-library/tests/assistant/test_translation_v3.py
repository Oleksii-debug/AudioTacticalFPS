"""Actual shared gateway/jobs with synthetic Google REST responses, never live API."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import http.client
import json
import ssl
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
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
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
    state = {'widgets': [], 'queue': [], 'approvals': [], 'scenario': None, 'gateways': []}
    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
            self.parent, self.identity, self.destroyed = parent, len(state['widgets']) + 1, False
            self.value, self.name, self.label, self.choices = value, '', label, choices
            self.enabled, self.selection, self.bindings, self.focused = True, -1, {}, False
            state['widgets'].append(self)
        def SetName(self, name): self.name = name
        def GetId(self): return self.identity
        def SetSelection(self, value): self.selection = value
        def SetEditable(self, value):
            assert not self.destroyed, 'Late callback edited a destroyed native control'
            self.editable = value
        def GetSelection(self): return self.selection
        def GetValue(self): return self.value
        def ChangeValue(self, value):
            assert not self.destroyed, 'Late callback wrote a destroyed native control'
            self.value = value
        def Enable(self, value=True):
            assert not self.destroyed, 'Late callback enabled a destroyed native control'
            self.enabled = value
        def Disable(self): self.enabled = False
        def SetFocus(self):
            assert not self.destroyed, 'Late callback focused a destroyed native control'
            self.focused = True
        def Bind(self, event, callback): self.bindings[event] = callback
        def SetSizer(self, sizer): pass
        def EndModal(self, result): pass
        def Destroy(self):
            assert not self.destroyed, 'A deleted native dialog was destroyed again'
            callback = self.bindings.get(wx.EVT_WINDOW_DESTROY)
            if callback:
                callback(SimpleNamespace(GetId=lambda: self.identity, Skip=lambda: None))
            self.destroyed = True
            for widget in state['widgets']:
                if widget.parent is self:
                    widget.destroyed = True
        def ShowModal(self):
            state['scenario']()
            return 1
    class ChoiceDialog(Widget):
        def __init__(self, parent, message, title, rows):
            super().__init__(parent, choices=rows)
            self.selection = 0
        def ShowModal(self): return wx.ID_OK
    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate((
        'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
        'TE_MULTILINE', 'TE_READONLY', 'YES_NO', 'NO_DEFAULT', 'YES', 'ID_OK', 'ID_CLOSE', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY'))})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    wx.SingleChoiceDialog = ChoiceDialog
    wx.CallAfter = lambda callback, *args: callback(*args)
    def approve(message, *args):
        state['approvals'].append(message)
        return wx.YES
    wx.MessageBox = approve
    monkeypatch.setitem(sys.modules, 'wx', wx)
    app = LibraryApp(tmp_path / 'library')
    p = profile(); ProfileStore(app.root / 'state' / 'providers').save(p)
    vault = Vault()
    def gateway(directory, **kwargs):
        state['gateways'].append(directory)
        return Gateway(directory, vault=vault, **kwargs)
    monkeypatch.setattr(ui, 'Gateway', gateway)
    frame = SimpleNamespace(library=app, working=False, _closing=False, cancel_event=__import__('threading').Event())
    def destroy_parent():
        for widget in state['widgets']:
            if widget.parent is frame and not widget.destroyed:
                widget.Destroy()
    frame.Destroy = destroy_parent
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
                           service=TranslationService(app.root / 'state' / 'translation'), p=p, vault=vault, wx=wx)


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


def test_handler_close_before_queued_request_creates_no_job_or_transmission(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume'); e.press('Close')
        e.complete()
        assert not e.service.list()
        assert len(transport['calls']) == 1
    e.scenario(scenario)


def test_handler_root_change_before_worker_does_not_send_old_library_text(translation_handlers, transport, tmp_path):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume')
        e.frame.library = LibraryApp(tmp_path / 'different library')
        e.complete()
        message = e.find('Preview, supported languages and job status').value
        assert 'library' in message and 'changed' in message
        assert not e.service.list()
        assert len(transport['calls']) == 1
        e.press('Translate / resume')
        assert not e.state['queue'] and not e.service.list()
    e.scenario(scenario)


def test_direct_parent_destroy_before_queued_transfer_creates_no_job_or_request(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume')
        assert len(e.state['queue']) == 1
        before = len(e.state['gateways']), len(e.vault.calls)
        e.frame.Destroy()  # Native destruction can bypass the Close handler.
        e.complete()  # Deliver while ShowModal has not yet unwound its finally.
        assert not e.service.list() and len(transport['calls']) == 1
        assert (len(e.state['gateways']), len(e.vault.calls)) == before
    e.scenario(scenario)


def test_direct_parent_destroy_during_response_retains_uncertain_charge_without_late_ui(
        translation_handlers, transport, monkeypatch):
    e = translation_handlers
    actual = http.client.HTTPSConnection.getresponse
    def response(connection):
        if transport['calls'][-1]['method'] == 'POST':
            e.frame.Destroy()
        return actual(connection)
    monkeypatch.setattr(http.client.HTTPSConnection, 'getresponse', response)
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume')
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'paused' and not job['receipts']
        operations = e.service.gateway.ledger.get(job['authorization']['id'])['operations']
        assert operations[job['id'] + '_0']['status'] == 'uncertain'
        usage_path = e.service.gateway.ledger._path(job['authorization']['id'])
        before = usage_path.read_bytes()
        service = TranslationService(e.service.directory,
            gateway=Gateway(e.service.directory / 'provider', vault=e.vault, mode='APPROVED_REMOTE'))
        with pytest.raises(ProviderError, match='already been attempted'):
            service.resume(job['id'])
        assert e.service.jobs.load(job['id'])['receipts'] == []
        assert usage_path.read_bytes() == before and len(transport['calls']) == 2
    e.scenario(scenario)


def test_direct_parent_destroy_after_commit_keeps_completed_result_without_late_ui(
        translation_handlers, transport, monkeypatch):
    e = translation_handlers
    actual = TranslationService.resume
    def completed(service, *args, **kwargs):
        result = actual(service, *args, **kwargs)
        assert result['status'] == 'complete'
        e.frame.Destroy()
        return result
    monkeypatch.setattr(TranslationService, 'resume', completed)
    def scenario():
        prepare_handler(e); e.press('Preview'); e.press('Translate / resume')
        e.complete()
        job, = e.service.list()
        assert job['status'] == 'complete' and len(job['receipts']) == 1
        assert len(transport['calls']) == 2
    e.scenario(scenario)


def test_descendant_destroy_notification_does_not_cancel_live_translation(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e)
        dialog = e.find('Translation workspace')
        child = e.find('Selected text; originals remain unchanged')
        observed = []
        dialog.bindings[e.wx.EVT_WINDOW_DESTROY](SimpleNamespace(
            GetId=child.GetId, Skip=lambda: observed.append(True)))
        assert observed == [True]
        e.press('Preview'); e.press('Translate / resume'); e.complete()
        job, = e.service.list()
        assert job['status'] == 'complete' and len(transport['calls']) == 2
    e.scenario(scenario)


def source_case(tmp_path, transport, *, first=1, last=2):
    """Actual retained reader and source authority with only HTTPS synthesized."""
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range

    app = LibraryApp(tmp_path / 'bound library')
    workspace = app.create_workspace('Advanced translation')
    topic = app.create_topic(workspace, 'Reviewed source')
    raw = 'Exact original Україна &amp; 🙂. ' * 200 + '\fSecond retained section.'
    imported = app.add_text(workspace, 'Synthetic Advanced source', raw)
    retained = read_source_range(ReadingDesk(app, workspace, imported['edition'], topic=topic), first, last)
    directory = app.root / 'state' / 'translation'
    vault = Vault()
    service = TranslationService(directory, library=app,
        gateway=Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    p = profile()
    approval = Authorization('source_languages', 'source_languages', ((p.id, p.endpoint),),
                            ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, imported=imported,
                           source=retained, document=retained.reading_document(), raw=raw,
                           service=service, p=p, vault=vault)


def submit_source(case):
    approval, cost = translation_approval(case.p, case.source.text, 'en', 'uk', '1',
                                          source_bound=True, glossary=GLOSSARY)
    identity = case.service.submit(case.p, case.source.text, 'en', 'uk',
        source_document=case.document, topic=case.topic, title=case.source.title,
        glossary=GLOSSARY, authorization=approval, per_call_cost=cost)
    return identity, approval


@pytest.mark.parametrize('first,last', [(1, 2), (2, 2)], ids=['whole-source', 'source-range'])
def test_advanced_source_derivative_retains_original_anchor_and_decoded_usage(tmp_path, transport, first, last):
    from uuid import UUID
    case = source_case(tmp_path, transport, first=first, last=last)
    original = case.app.assets.resolve(case.imported['asset'])
    before = original.read_bytes()
    identity, approval = submit_source(case)
    assert UUID(identity).version == 5
    assert case.service.resume(identity)['status'] == 'complete'
    job = case.service.get(identity)
    assert job['kind'] == 'translation/2' and job['authorization']['data_classes'] == ['selected-script']
    operations = case.service.gateway.ledger.get(approval.id)['operations']
    for index, row in enumerate(job['receipts']):
        if job['chunks'][index].strip():
            assert operations[identity + '_' + str(index)]['usage']['translation_sha256'] == row['translation_sha256']
            assert row['reported_model'] == PARENT + '/models/general/nmt'
            assert row['glossary_config'] == {'glossary': GLOSSARY, 'ignoreCase': False}
    output = tmp_path / 'bound output'
    case.service.export(identity, output)
    reading = ReadingDocument.load(output / 'translation.reading.json')
    report = read_json(output / 'translation-provenance.json')
    assert original.read_bytes() == before == case.raw.encode('utf-8')
    assert (output / 'original.txt').read_bytes() == case.source.text.encode('utf-8')
    assert reading.source_text == case.source.text and reading.source_anchor == case.document.source_anchor
    assert not reading.source_span(0, len(reading.text)).exact
    assert reading.undo() and reading.text == case.source.text
    assert report['decoded_text_binding'] == 'usage-ledger-sha256'
    assert report['source_anchor'] == case.document.source_anchor and report['quotation_authority'] is False
    assert report['glossary'] == GLOSSARY and report['provider_endpoint'] == ENDPOINT


@pytest.mark.parametrize('change', ['archival', 'original'])
def test_advanced_source_revocation_between_chunks_keeps_receipt_without_next_transfer(tmp_path, transport, change):
    case = source_case(tmp_path, transport)
    identity, approval = submit_source(case)
    def accepted(done, total, phase):
        assert done == 1 and total > 1
        if change == 'archival':
            case.app.archive_workspace(case.workspace)
        else:
            original = case.app.assets.resolve(case.imported['asset'])
            raw = original.read_bytes()
            original.write_bytes(b'X' + raw[1:])
    with pytest.raises((PermissionError, ValueError)):
        case.service.resume(identity, progress=accepted)
    saved = case.service.jobs.load(identity)
    usage = case.service.gateway.ledger._path(approval.id).read_bytes()
    assert saved['status'] == 'attention' and len(saved['receipts']) == 1
    assert len(transport['calls']) == 2  # Discovery plus the accepted first chunk.
    with pytest.raises((PermissionError, ValueError)):
        case.service.resume(identity)
    with pytest.raises((PermissionError, ValueError)):
        case.service.export(identity, tmp_path / 'revoked')
    assert not (tmp_path / 'revoked').exists()
    assert case.service.jobs.load(identity)['receipts'] == saved['receipts']
    assert case.service.gateway.ledger._path(approval.id).read_bytes() == usage
    assert len(transport['calls']) == 2


@pytest.mark.parametrize('bound', [False, True], ids=['pasted', 'retained-source'])
@pytest.mark.parametrize('mutation', ['text-and-self-hash', 'missing-decoded-proof'])
def test_advanced_receipts_always_require_the_accepted_decoded_text(tmp_path, transport, bound, mutation):
    from carr_library.assistant.state import atomic_json
    if bound:
        case = source_case(tmp_path, transport, first=2, last=2)
        service = case.service
        identity, approval = submit_source(case)
    else:
        service, p, _ = ready(tmp_path, transport)
        identity, approval = submit(service, p, glossary=GLOSSARY)
    service.resume(identity)
    saved = service.jobs.load(identity)
    if mutation == 'text-and-self-hash':
        saved['receipts'][0]['text'] = 'Forged output with the original raw response receipt.'
        saved['receipts'][0]['translation_sha256'] = sha256(saved['receipts'][0]['text'].encode()).hexdigest()
        service.jobs.save(saved)
    else:
        usage = service.gateway.ledger.get(approval.id)
        del usage['operations'][identity + '_0']['usage']['translation_sha256']
        atomic_json(service.gateway.ledger._path(approval.id), usage)
    before = service.gateway.ledger._path(approval.id).read_bytes()
    calls = len(transport['calls'])
    with pytest.raises(ValueError, match='decoded-output'):
        service.resume(identity)
    with pytest.raises(ValueError, match='decoded-output'):
        service.export(identity, tmp_path / 'forged')
    assert not (tmp_path / 'forged').exists() and len(transport['calls']) == calls
    assert service.gateway.ledger._path(approval.id).read_bytes() == before


@pytest.mark.parametrize('started', [False, True], ids=['before-first-transfer', 'accepted-receipt'])
def test_advanced_source_glossary_cannot_expand_or_replace_saved_authority(tmp_path, transport, started):
    case = source_case(tmp_path, transport)
    identity, _ = submit_source(case)
    if started:
        stopped = []
        case.service.resume(identity, cancelled=lambda: bool(stopped), progress=lambda *args: stopped.append(True))
    saved = case.service.jobs.load(identity)
    saved['glossary'] += '-unapproved'
    case.service.jobs.save(saved)
    calls = len(transport['calls'])
    with pytest.raises(ValueError, match='authority'):
        case.service.resume(identity)
    with pytest.raises(ValueError, match='authority'):
        case.service.export(identity, tmp_path / 'changed-glossary')
    assert not (tmp_path / 'changed-glossary').exists() and len(transport['calls']) == calls


@pytest.mark.parametrize('complete', [False, True], ids=['pending-approval', 'completed-offline'])
def test_advanced_same_path_restore_never_revives_remote_approval(tmp_path, transport, complete):
    from carr_library.library_backup import restore_backup
    case = source_case(tmp_path, transport)
    identity, _ = submit_source(case)
    stopped = []
    case.service.resume(identity, cancelled=lambda: bool(stopped),
        progress=(lambda *args: None) if complete else lambda *args: stopped.append(True))
    saved = case.service.get(identity)
    backup = tmp_path / 'archive'
    digest = case.app.backup(backup)
    destination = case.app.root
    destination.rename(tmp_path / 'old library')
    restored = LibraryApp(restore_backup(backup, destination, digest))
    directory = restored.root / 'state' / 'translation'
    vault = Vault()
    service = TranslationService(directory, library=restored,
        gateway=Gateway(directory / 'provider', vault=vault, mode='APPROVED_REMOTE'))
    assert service.get(identity) == saved
    calls = len(transport['calls'])
    if complete:
        assert service.resume(identity, cancelled=lambda: True)['status'] == 'complete'
        output = tmp_path / 'restored output'
        service.export(identity, output)
        reading = ReadingDocument.load(output / 'translation.reading.json')
        assert reading.source_anchor == case.document.source_anchor
        assert read_json(output / 'translation-provenance.json')['decoded_text_binding'] == 'usage-ledger-sha256'
    else:
        with pytest.raises(PermissionError, match='moved or restored'):
            service.resume(identity)
        assert service.jobs.load(identity)['receipts'] == saved['receipts']
    assert len(transport['calls']) == calls and not vault.calls


@pytest.mark.parametrize('change', ['missing', 'disabled', 'changed'])
def test_language_discovery_rereads_account_before_gateway_and_credentials(translation_handlers, transport, change):
    e = translation_handlers
    def scenario():
        e.press('Fetch languages')
        store = ProfileStore(e.frame.library.root / 'state' / 'providers')
        if change == 'missing':
            store.remove(e.p.id)
        else:
            store.save(replace(e.p, **({'enabled': False} if change == 'disabled' else {'name': 'Changed account'})))
        e.complete()
        assert 'unavailable, disabled or changed' in e.find('Preview, supported languages and job status').value
        assert not e.state['gateways'] and not e.vault.calls and not transport['calls']
        assert not e.service.list()
    e.scenario(scenario)


def test_saved_advanced_glossary_is_displayed_and_must_match_before_resume(translation_handlers, transport):
    e = translation_handlers
    def scenario():
        prepare_handler(e)
        approval, cost = translation_approval(e.p, 'Saved original.', 'en', 'uk', '1', glossary=GLOSSARY)
        identity = e.service.submit(e.p, 'Saved original.', 'en', 'uk',
                                   glossary=GLOSSARY, authorization=approval, per_call_cost=cost)
        before = e.service.jobs.path(identity).read_bytes()
        e.press('Saved jobs'); e.complete()
        assert e.find('Advanced v3 glossary resource; blank for none').value == GLOSSARY
        assert 'google-translation-v3' in e.find('Preview, supported languages and job status').value
        e.find('Advanced v3 glossary resource; blank for none').ChangeValue(GLOSSARY + '-changed')
        gateway_count, vault_count = len(e.state['gateways']), len(e.vault.calls)
        e.press('Translate / resume'); e.complete()
        assert 'Visible settings differ' in e.find('Preview, supported languages and job status').value
        assert e.service.jobs.path(identity).read_bytes() == before
        assert len(e.state['gateways']) == gateway_count and len(e.vault.calls) == vault_count
        assert len(transport['calls']) == 1
    e.scenario(scenario)


@pytest.mark.parametrize('changed_authority', [False, True], ids=['same-profile-renewal', 'new-project-requires-review'])
def test_provider_editor_keeps_full_profile_on_token_renewal_without_contacting_google(
        tmp_path, transport, monkeypatch, changed_authority):
    from carr_library import assistant
    from carr_library.ui import assistant_dialog
    app = LibraryApp(tmp_path / 'account editor')
    store = ProfileStore(app.root / 'state' / 'providers')
    p = profile(rights_note='Owner-reviewed account terms remain part of the profile.')
    store.save(p)
    vault = Vault()
    monkeypatch.setattr(assistant, 'WindowsCredentialVault', lambda: vault)
    widgets = []

    class Control:
        def __init__(self, parent=None, *args, value='', label='', choices=(), title='', **kwargs):
            self.parent, self.value, self.label, self.title = parent, value, label, title
            self.name, self.handlers, self.selection = '', {}, -1
            self.choices, self.string_selection = list(choices), ''
            widgets.append(self)
        def SetName(self, value): self.name = value
        def SetItems(self, value): self.choices = value
        def SetSelection(self, value): self.selection = value
        def GetSelection(self): return self.selection
        def SetStringSelection(self, value): self.string_selection = value
        def GetStringSelection(self): return self.string_selection
        def SetValue(self, value): self.value = value
        def GetValue(self): return self.value
        def ChangeValue(self, value): self.value = value
        def Enable(self, value=True): self.enabled = value
        def SetFocus(self): self.focused = True
        def Bind(self, event, handler, **kwargs): self.handlers[event] = handler
        def Add(self, *args, **kwargs): pass
        def SetSizer(self, sizer): pass
        def CreateButtonSizer(self, style): return Control(self)
        def SetEscapeId(self, value): pass
        def Destroy(self): pass
        def EndModal(self, result): pass
        def ShowModal(self):
            if self.title == 'Edit connection':
                by_name('New credential; API key for Basic v2, OAuth access token for Advanced v3; blank keeps it').ChangeValue(TOKEN + '_RENEWED')
                by_name('Advanced v3 token expiry with UTC offset; for example 2026-11-27T15:30:00Z').ChangeValue(
                    datetime.fromtimestamp(time() + 1800, timezone.utc).isoformat())
                if changed_authority:
                    by_name('Official endpoint or explicitly chosen loopback service').ChangeValue(
                        ENDPOINT.replace('123456789', '234567890'))
                return wx.ID_OK
            by_name('Provider accounts').SetSelection(0)
            next(widget for widget in widgets if widget.label == '&Edit').handlers[wx.EVT_BUTTON](None)
            return wx.ID_CLOSE

    def by_name(name): return next(widget for widget in widgets if widget.name == name)
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND',
             'TE_PASSWORD', 'TE_READONLY', 'TE_MULTILINE', 'OK', 'CANCEL', 'CLOSE', 'ID_OK', 'ID_CLOSE',
             'EVT_BUTTON', 'EVT_CHOICE')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('Dialog', 'StaticText', 'TextCtrl', 'Choice', 'ListBox', 'Button', 'CheckBox', 'BoxSizer'):
        setattr(wx, name, Control)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    assistant_dialog.show_provider_settings(SimpleNamespace(library=app))
    saved = store.get(p.id)
    assert saved.rights_note == p.rights_note and saved.name == p.name
    assert not transport['calls']
    if changed_authority:
        assert saved.endpoint != p.endpoint and not saved.enabled
        assert saved.credential_ref != p.credential_ref and p.credential_ref not in vault.records
        assert vault.calls.count(('delete', p.id, p.credential_ref)) == 1
    else:
        assert saved.to_dict() == p.to_dict()
        assert vault.calls == [('put', p.id, p.credential_ref)]
    assert google_oauth_token(vault.records[saved.credential_ref], saved.endpoint)[0] == TOKEN + '_RENEWED'
    assert TOKEN not in store.path(p.id).read_text(encoding='utf-8')
    assert 'No provider was contacted' in by_name('Connection configuration status').GetValue()


@pytest.mark.parametrize('glossary', [None, GLOSSARY], ids=['plain-nmt', 'exact-glossary'])
def test_pasted_advanced_limit_retains_accepted_prefix_before_another_paid_call(tmp_path, transport, glossary):
    from carr_library.assistant.state import MAX_CHECKPOINT_BYTES
    service, p, _ = ready(tmp_path, transport)
    identity, approval = submit(service, p, 'Pasted original sentence. ' * 4000, glossary=glossary)
    def large_result(document):
        key = 'glossaryTranslations' if glossary else 'translations'
        row = document[key][0]
        row['translatedText'] = 'T' * 190000
        return {key: [row]}
    transport['response'] = large_result
    with pytest.raises(ValueError, match='2,000,000-character'):
        service.resume(identity)
    saved = service.get(identity)
    assert saved['kind'] == 'translation/1' and saved['status'] == 'attention'
    assert len(saved['receipts']) == 11 < len(saved['chunks'])
    assert sum(len(row['text']) for row in saved['receipts']) == 2_090_000
    assert service.jobs.path(identity).stat().st_size <= MAX_CHECKPOINT_BYTES
    operations = service.gateway.ledger.get(approval.id)['operations']
    assert len(operations) == 11 and all(row['status'] == 'complete' for row in operations.values())
    before = service.jobs.path(identity).read_bytes()
    reopened = TranslationService(service.directory, gateway=service.gateway)
    with pytest.raises(ValueError, match='Accepted chunks were retained'):
        reopened.resume(identity)
    with pytest.raises(ValueError):
        reopened.export(identity, tmp_path / 'oversized')
    assert not (tmp_path / 'oversized').exists()
    assert service.jobs.path(identity).read_bytes() == before
    assert len(transport['calls']) == 12


@pytest.mark.parametrize('source_character', ['\U0001f642', '\x02'], ids=['four-byte-utf8', 'six-byte-json'])
def test_maximum_pasted_advanced_source_reserves_existing_serialization_limits(tmp_path, transport, source_character):
    from carr_library.assistant.state import MAX_CHECKPOINT_BYTES
    from carr_library.speech.reading import MAX_PROJECT_BYTES
    service, p, _ = ready(tmp_path, transport)
    original = source_character * 2_000_000
    identity, _ = submit(service, p, original, glossary=GLOSSARY)
    assert service.jobs.path(identity).stat().st_size <= MAX_CHECKPOINT_BYTES
    reading = ReadingDocument(original, title='Translation')
    reading.replace(0, len(reading.text), '\x03' * 2_000_000,
                    label='Machine translation derivative; not a source quotation')
    target = tmp_path / 'maximum-pasted.reading.json'
    reading.save(target)
    assert target.stat().st_size <= MAX_PROJECT_BYTES
    restored = ReadingDocument.load(target)
    assert restored.source_text == original and restored.source_anchor is None
    assert len(restored.text) == 2_000_000 and restored.can_undo
    assert restored.undo() and restored.text == original
    assert len(transport['calls']) == 1  # Discovery only; no billable request.

