"""Canonical147 v3 cases adapted to the single pinned gateway and source-aware CLI.

Only HTTPTransport.request is replaced; actual validation, credentials contracts,
usage ledger and source/job/restore logic run. No live API or quality claim.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import http.client
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
from carr_library.assistant.providers import Authorization, Gateway, ProviderError, HTTPTransport, HTTPResult
from carr_library.assistant.state import read_json
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library import translation_cli
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
    def request(self, profile, route, body, headers, *, method='POST', **kwargs):
        # Preserve the actual shared gateway, request validation, ledger, OAuth
        # handling and response decoder. Only the network transport is synthetic.
        connection = Connection('translation.googleapis.com', 443,
                                context=ssl.create_default_context())
        try:
            connection.request(method, route, body, headers)
            response = connection.getresponse()
            return HTTPResult(response.getheader('Content-Type'),
                              response.read(kwargs.get('maximum_bytes', 1024 * 1024) + 1),
                              response.status)
        finally:
            connection.close()
    monkeypatch.setattr(HTTPTransport, 'request', request)
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




def test_cli_facade_uses_the_existing_source_aware_implementation():
    from carr_library.assistant import translation_cli as compatibility
    assert compatibility.main is translation_cli.main


def test_cli_binds_glossary_and_keeps_all_existing_remote_approval_gates(tmp_path, transport, monkeypatch, capsys):
    app = LibraryApp(tmp_path / 'library')
    p = profile()
    ProfileStore(app.root / 'state/providers').save(p)
    vault = Vault()
    monkeypatch.setattr(translation_cli, 'Gateway',
                        lambda directory, **kwargs: Gateway(directory, vault=vault, **kwargs))
    base = ['--data-dir', str(app.root)]
    assert translation_cli.main(base + ['profiles']) == 0
    profiles = json.loads(capsys.readouterr().out)['profiles']
    assert profiles[0]['adapter'] == 'google-translation-v3' and 'credential_ref' not in profiles[0]
    assert not transport['calls']
    assert translation_cli.main(base + ['languages', '--profile', p.id, '--cached']) == 0
    assert json.loads(capsys.readouterr().out)['languages'] == [] and not transport['calls']
    assert translation_cli.main(base + ['languages', '--profile', p.id]) == 1
    assert '--allow-remote' in capsys.readouterr().err and not transport['calls']
    assert translation_cli.main(base + ['languages', '--profile', p.id, '--allow-remote']) == 0
    assert json.loads(capsys.readouterr().out)['languages'][2]['target'] is False
    original = tmp_path / 'selected.txt'
    original.write_bytes(b'Exact reviewed source.\r\nKeep this original.')
    args = ['--profile', p.id, '--text-file', str(original), '--source', 'en',
            '--target', 'uk', '--budget', '1', '--glossary', GLOSSARY]
    assert translation_cli.main(base + ['preview'] + args) == 0
    review = json.loads(capsys.readouterr().out)
    assert review['glossary'] == GLOSSARY and review['endpoint'] == ENDPOINT
    submit_args = base + ['submit'] + args + ['--approve-preview', review['approval_sha256']]
    assert translation_cli.main(submit_args) == 1
    assert '--allow-remote' in capsys.readouterr().err
    assert translation_cli.main(submit_args + ['--allow-remote']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'complete'
    assert translation_cli.main(base + ['inspect', '--job', result['id']]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected['glossary'] == GLOSSARY and inspected['provider_adapter'] == p.adapter
    assert translation_cli.main(base + ['resume', '--job', result['id'],
                                         '--approve-job', inspected['resume_approval_sha256']]) == 1
    assert '--allow-remote' in capsys.readouterr().err
    before = len(transport['calls'])
    assert translation_cli.main(base + ['resume', '--job', result['id'], '--allow-remote',
                                         '--approve-job', inspected['resume_approval_sha256']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert len(transport['calls']) == before  # Accepted work is never billed again.
    destination = tmp_path / 'cli export'
    assert translation_cli.main(base + ['export', '--job', result['id'], '--output', str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert (destination / 'original.txt').read_bytes() == original.read_bytes()


@pytest.mark.parametrize('mutation', ['text', 'glossary', 'target', 'budget', 'profile'])
def test_cli_rejects_changed_v3_preview_before_submission(tmp_path, transport, monkeypatch, capsys, mutation):
    app = LibraryApp(tmp_path / 'library')
    p = profile()
    store = ProfileStore(app.root / 'state/providers')
    store.save(p)
    vault = Vault()
    monkeypatch.setattr(translation_cli, 'Gateway',
                        lambda directory, **kwargs: Gateway(directory, vault=vault, **kwargs))
    base = ['--data-dir', str(app.root)]
    assert translation_cli.main(base + ['languages', '--profile', p.id, '--allow-remote']) == 0
    capsys.readouterr()
    original = tmp_path / 'source.txt'
    original.write_text('Original source', encoding='utf-8')
    args = ['--profile', p.id, '--text-file', str(original), '--source', 'en',
            '--target', 'uk', '--budget', '1', '--glossary', GLOSSARY]
    assert translation_cli.main(base + ['preview'] + args) == 0
    review = json.loads(capsys.readouterr().out)
    if mutation == 'text':
        original.write_text('Changed source', encoding='utf-8')
    elif mutation == 'profile':
        store.save(replace(p, name='Changed selected account'))
    else:
        args[args.index('--' + mutation) + 1] = {
            'glossary': GLOSSARY + '-other', 'target': 'fr', 'budget': '2'}[mutation]
    assert translation_cli.main(base + ['submit'] + args +
        ['--allow-remote', '--approve-preview', review['approval_sha256']]) == 1
    assert 'exact current preview digest' in capsys.readouterr().err
    assert not TranslationService(app.root / 'state/translation').list()
    assert len(transport['calls']) == 1


def test_stale_account_editor_cannot_replace_newer_credential(tmp_path):
    p = profile()
    store = ProfileStore(tmp_path / 'profiles')
    store.save(p)
    vault = Vault()
    before = dict(vault.records)
    store.save(replace(p, endpoint=ENDPOINT.replace('123456789', '234567890')))
    with pytest.raises(ValueError, match='account changed'):
        save_google_token(store, vault, p, TOKEN + '_NEW', time() + 300, current=p)
    assert vault.records == before and not vault.calls


def _linked(tmp_path, transport):
    from carr_library.assistant.library_translation import LibrarySelection, capture_library_sources
    app = LibraryApp(tmp_path / 'linked-library')
    workspace = app.create_workspace('Advanced source identity')
    original = 'Literal original.\r\nУкраїнський текст &amp;.\fSecond retained position.'
    book = app.add_text(workspace, 'Advanced original', original)
    vault = Vault()
    service = TranslationService(app.root / 'state/translation', library=app, workspace=workspace,
        gateway=Gateway(app.root / 'state/translation/provider', vault=vault, mode='APPROVED_REMOTE'))
    p = profile()
    ProfileStore(app.root / 'state/providers').save(p)
    approval = Authorization('linked_languages', 'linked_languages', ((p.id, p.endpoint),),
                            ('translation.languages',), ('user-instruction',), '0', 1, time() + 60)
    service.refresh_languages(p, authorization=approval)
    selected = capture_library_sources(app, workspace, (LibrarySelection(book['edition']),))
    return app, workspace, book, original, selected, service, p, vault


def test_v3_source_bound_chunks_keep_decoded_ledger_identity_and_offline_restoration(tmp_path, transport):
    from carr_library.assistant.models import canonical
    from carr_library.assistant.providers import translation_receipt_sha256
    from carr_library.library_backup import create_backup, restore_backup
    app, workspace, book, original, selected, service, p, vault = _linked(tmp_path, transport)
    approval, cost = translation_approval(p, selected.text, 'en', 'uk', '1',
                                         source_binding=selected.binding, glossary=GLOSSARY)
    identity = service.submit(p, selected.text, 'en', 'uk', authorization=approval,
        per_call_cost=cost, source_binding=selected.binding, glossary=GLOSSARY)
    assert service.resume(identity)['status'] == 'complete'
    job = service.get(identity)
    assert job['source_binding'] == selected.binding and job['execution_origin']
    usage = service.gateway.ledger.get(approval.id)['operations']
    for index, receipt in enumerate(job['receipts']):
        expected = translation_receipt_sha256(receipt['text'], receipt['detected_source'])
        assert receipt['decoded_translation_sha256'] == expected
        assert usage[identity + '_' + str(index)]['usage']['decoded_translation_sha256'] == expected
        assert receipt['reported_model'] == PARENT + '/models/general/nmt'
        assert receipt['glossary_config'] == {'glossary': GLOSSARY, 'ignoreCase': False}
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    restored_root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = TranslationService(restored_root / 'state/translation',
        library=LibraryApp(restored_root), workspace=workspace)
    recovered = restored.get(identity)
    assert recovered['receipts'] == job['receipts']
    assert recovered['source_binding'] == selected.binding and recovered['glossary'] == GLOSSARY
    assert recovered['profile']['credential_ref'] is None and recovered['profile']['enabled'] is False
    assert recovered['original_profile_sha256'] == sha256(canonical(job['profile']).encode()).hexdigest()
    assert recovered['restore_requires_review'] is True
    before = len(transport['calls']), len(vault.calls)
    with pytest.raises(PermissionError, match='Restored translation'):
        restored.resume(identity)
    export = tmp_path / 'restored-export'
    restored.export(identity, export)
    assert (export / 'original.txt').read_bytes() == selected.text.encode('utf-8')
    assert (len(transport['calls']), len(vault.calls)) == before
    assert app.assets.resolve(book['asset']).read_bytes() == original.encode('utf-8')


def test_v3_rehashed_chunk_text_still_fails_independent_decoded_ledger_binding(tmp_path, transport):
    app, workspace, book, original, selected, service, p, vault = _linked(tmp_path, transport)
    approval, cost = translation_approval(p, selected.text, 'en', 'uk', '1',
                                         source_binding=selected.binding, glossary=GLOSSARY)
    identity = service.submit(p, selected.text, 'en', 'uk', authorization=approval,
        per_call_cost=cost, source_binding=selected.binding, glossary=GLOSSARY)
    service.resume(identity)
    job = service.jobs.load(identity)
    row = job['receipts'][0]
    row['text'] = 'Forged decoded translation'
    row['translation_sha256'] = sha256(row['text'].encode()).hexdigest()
    from carr_library.assistant.providers import translation_receipt_sha256
    row['decoded_translation_sha256'] = translation_receipt_sha256(row['text'], row['detected_source'])
    service.jobs.save(job)
    before = len(transport['calls'])
    with pytest.raises(ValueError, match='decoded translation receipt'):
        service.export(identity, tmp_path / 'must-not-publish')
    assert not (tmp_path / 'must-not-publish').exists()
    assert len(transport['calls']) == before


@pytest.mark.parametrize('mutation', ['model', 'glossary', 'source', 'direction'])
def test_v3_pure_backup_validators_bind_model_glossary_source_and_language_flags(tmp_path, transport, mutation):
    from copy import deepcopy
    from carr_library.assistant.models import canonical
    from carr_library import backup_producers
    app, workspace, book, original, selected, service, p, vault = _linked(tmp_path, transport)
    approval, cost = translation_approval(p, selected.text, 'en', 'uk', '1',
                                         source_binding=selected.binding, glossary=GLOSSARY)
    identity = service.submit(p, selected.text, 'en', 'uk', authorization=approval,
        per_call_cost=cost, source_binding=selected.binding, glossary=GLOSSARY)
    service.resume(identity)
    job = service.get(identity)
    languages = read_json(service._languages_path(p))
    backup_producers._translation_job(deepcopy(job), identity)
    backup_producers._translation_languages(deepcopy(languages), p.id)
    if mutation == 'model':
        job['receipts'][0]['reported_model'] = PARENT + '/models/other'
    elif mutation == 'glossary':
        job['receipts'][0]['glossary_config']['ignoreCase'] = 0
    elif mutation == 'source':
        job['source_binding']['sha256'] = '0' * 64
    else:
        languages['languages'][0]['source'] = 'true'
        languages['languages_sha256'] = sha256(canonical(languages['languages']).encode()).hexdigest()
    with pytest.raises((ValueError, PermissionError)):
        if mutation == 'direction':
            backup_producers._translation_languages(languages, p.id)
        else:
            backup_producers._translation_job(job, identity)
