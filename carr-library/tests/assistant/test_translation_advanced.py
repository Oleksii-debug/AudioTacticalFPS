"""Current translation jobs, backup and exports with synthetic Advanced v3 replies."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import socket
from threading import Event

import pytest

from carr_library import providers
from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.assistant.translation import TranslationService, language_approval, translation_approval, split_text
from carr_library.assistant.translation_state import (
    ADVANCED_CACHE_SCHEMA, ADVANCED_SCHEMA, validate_language_cache, validate_translation_job)
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.providers import BudgetLedger, ProfileStore, ProviderError, ProviderGateway, ProviderProfile
from carr_library.reader.narration_text import NarrationDraft

PARENT = 'projects/123456789/locations/us-central1'
ENDPOINT = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/terms'
TOKEN = 'SYNTHETIC_TEST_OAUTH_NEVER_REAL'


class Vault:
    def __init__(self):
        self.values, self.reads = {}, 0
    def put(self, reference, binding, value):
        self.values[reference, binding] = value
    def get(self, reference, binding):
        self.reads += 1
        return self.values[reference, binding]
    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.fail = False
    def get(self, profile, route, headers, **kwargs):
        self.calls.append(('languages', route))
        return json.dumps({'languages': [
            {'languageCode': 'en', 'displayName': 'English', 'supportSource': True},
            {'languageCode': 'uk', 'displayName': 'Українська', 'supportTarget': True},
            {'languageCode': 'sl', 'displayName': 'Slovenian', 'supportSource': True, 'supportTarget': True}]}).encode()
    def post(self, profile, route, raw, headers, **kwargs):
        body = json.loads(raw)
        self.calls.append(('text', body))
        if self.fail:
            raise RuntimeError('PRIVATE TOKEN AND REQUEST BODY MUST NOT REACH UI')
        row = {'translatedText': 'ПЕРЕКЛАД &amp; ' + body['contents'][0], 'model': body['model']}
        if 'glossaryConfig' in body:
            row['glossaryConfig'] = body['glossaryConfig']
            return json.dumps({'glossaryTranslations': [row]}, ensure_ascii=False).encode()
        return json.dumps({'translations': [row]}, ensure_ascii=False).encode()


@pytest.fixture
def ready(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(providers, 'time', lambda: clock[0])
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('Unexpected real network'))
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Translation verification')
    vault, transport = Vault(), Transport()
    store = ProfileStore(app.root / 'api-connections.json', vault)
    selected = store.save_google_token(ProviderProfile('v3', 'Synthetic IAM account', 'google-translation-v3',
        ENDPOINT, 'general/nmt', ('translation',)), TOKEN, clock[0] + 1800)
    service = TranslationService(app.root, profiles=store,
        gateway=ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'), vault=vault, transport=transport))
    service.refresh_languages(selected, approval=language_approval(selected))
    return app, service, selected, vault, transport, clock


def submit(service, selected, value='Original &amp; source', *, glossary=GLOSSARY):
    permit = translation_approval(selected, value, 'en', 'uk', 1_000_001, glossary=glossary)
    return service.submit(selected, value, 'en', 'uk', approval=permit, glossary=glossary)


def text_calls(transport):
    return [row for row in transport.calls if row[0] == 'text']


def test_new_schema_preserves_immutable_source_and_old_namespace(ready):
    _, service, selected, _, transport, _ = ready
    source = 'Original Україна 🙂\r\n' * 700
    preview = service.preview(selected, source, 'en', 'uk', glossary=GLOSSARY)
    identity = submit(service, selected, source)
    value = service.jobs.load(identity)
    assert value['schema'] == ADVANCED_SCHEMA and value['glossary'] == GLOSSARY
    assert value['source_text'] == source and value['source_sha256'] == sha256(source.encode()).hexdigest()
    assert service.jobs.path(identity).parent == service.root / 'state' / 'translation'
    assert not (service.root / 'state/assistant').exists()
    assert preview['glossary'] == GLOSSARY and preview['provider'] == 'google-translation-v3'
    assert not text_calls(transport)
    assert sum(value['allocations']) == 1_000_001
    cache = json.loads(service._languages_path(selected).read_text(encoding='utf-8'))
    assert cache['schema'] == ADVANCED_CACHE_SCHEMA
    validate_language_cache(cache, profile_id=selected.id)


@pytest.mark.parametrize('source,target', [('uk', 'en'), ('en', 'en'), ('uk', 'sl')],
                         ids=['both-wrong-direction', 'unsupported-target', 'unsupported-source'])
def test_directional_inventory_is_not_expanded_by_preview(ready, source, target):
    _, service, selected, _, transport, _ = ready
    with pytest.raises(ValueError):
        service.preview(selected, 'Original text', source, target)
    assert not text_calls(transport) and not service.list()


def test_cache_cannot_keep_old_fingerprint_but_change_project(ready):
    _, service, selected, _, transport, _ = ready
    path = service._languages_path(selected)
    value = json.loads(path.read_text(encoding='utf-8'))
    value['endpoint'] = value['endpoint'].replace('123456789', '987654321')
    atomic_json(path, value)
    with pytest.raises(ValueError, match='refresh languages'):
        service.preview(selected, 'Original', 'en', 'uk')
    assert not text_calls(transport)


def test_paused_accepted_chunks_survive_exact_token_renewal_without_rebilling(ready):
    _, service, selected, _, transport, clock = ready
    source = 'A source paragraph &amp; 🙂. ' * 1000
    identity = submit(service, selected, source)
    stopped = Event()
    summary = service.resume(identity, cancelled=stopped.is_set, progress=lambda *args: stopped.set())
    assert summary['status'] == 'paused' and summary['completed'] == 1
    accepted = service.jobs.load(identity)['receipts'][0]
    clock[0] += 1801
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert len(text_calls(transport)) == 1
    unsent = service.gateway.ledger.report(identity)
    assert any(row['state'] == 'cancelled_before_send' for row in unsent)
    renewed = service.profiles.save_google_token(selected, TOKEN + '_NEW', clock[0] + 1800,
                                                renew_same_account=True)
    assert renewed.fingerprint == selected.fingerprint
    reopened = TranslationService(service.root, profiles=service.profiles, gateway=service.gateway)
    assert reopened.resume(identity)['status'] == 'complete'
    job = reopened.jobs.load(identity)
    assert job['receipts'][0] == accepted
    assert len(text_calls(transport)) == len(split_text(source))
    assert all(row['reported_model'] == PARENT + '/models/general/nmt'
               and row['glossary'] == GLOSSARY for row in job['receipts'])
    assert sum(row['estimate_micros'] for row in service.gateway.ledger.report(identity)) == 1_000_001
    reopened.resume(identity)
    assert len(text_calls(transport)) == len(split_text(source))


def test_replacing_account_cannot_reuse_a_queued_remote_approval(ready):
    _, service, selected, _, transport, clock = ready
    identity = submit(service, selected)
    changed = service.profiles.save_google_token(selected, TOKEN + '_OTHER', clock[0] + 1800)
    assert changed.fingerprint != selected.fingerprint
    with pytest.raises(PermissionError, match='changed'):
        service.resume(identity)
    assert not text_calls(transport)


def test_uncertain_request_is_attention_and_never_resubmitted(ready):
    _, service, selected, _, transport, _ = ready
    identity = submit(service, selected)
    transport.fail = True
    with pytest.raises(ProviderError) as caught:
        service.resume(identity)
    assert 'PRIVATE' not in str(caught.value)
    assert service.jobs.load(identity)['status'] == 'attention'
    transport.fail = False
    with pytest.raises(ProviderError):
        service.resume(identity)
    assert len(text_calls(transport)) == 1


def test_whitespace_has_no_false_model_or_glossary_receipt(ready):
    _, service, selected, _, transport, _ = ready
    source = ' ' * 4500 + 'Original source\r\n'
    identity = submit(service, selected, source)
    service.resume(identity)
    value = service.jobs.load(identity)
    whitespace = value['receipts'][0]
    assert whitespace['text'] == ' ' * 4500 and whitespace['operation_id'] is None
    assert whitespace['reported_model'] is None and whitespace['glossary'] is None
    assert value['allocations'][0] is None and len(text_calls(transport)) == 1


@pytest.mark.parametrize('change', ['glossary', 'model-receipt', 'glossary-receipt', 'schema',
                                   'profile-version', 'new-field'])
def test_job_versions_cannot_smuggle_or_rebind_accepted_advanced_results(ready, change):
    _, service, selected, _, _, _ = ready
    identity = submit(service, selected)
    service.resume(identity)
    value = service.jobs.load(identity)
    if change == 'glossary':
        value['glossary'] += '-other'
    elif change == 'model-receipt':
        value['receipts'][0]['reported_model'] = 'projects/987654321/locations/us-central1/models/general/nmt'
    elif change == 'glossary-receipt':
        value['receipts'][0]['glossary'] = ''
    elif change == 'schema':
        value['schema'] = 'carr-translation-job/1'
    elif change == 'profile-version':
        value['profile']['provider'] = 'google-translation-v2'
    else:
        value['access_token'] = TOKEN
    with pytest.raises(ValueError):
        validate_translation_job(value)


def test_complete_backup_restore_exports_offline_and_pending_permission_stays_old(ready, tmp_path):
    app, service, selected, vault, transport, _ = ready
    original = 'Original literal &amp; Україна'
    complete = submit(service, selected, original)
    service.resume(complete)
    queued = submit(service, selected, 'Pending separately approved source')
    archived_complete = service.jobs.path(complete).read_bytes()
    archived_queued = service.jobs.path(queued).read_bytes()
    backup = tmp_path / 'backup'
    receipt = create_backup(app, backup)
    before = {path.relative_to(backup): (path.stat().st_mtime_ns, path.read_bytes())
              for path in backup.rglob('*') if path.is_file()}
    verify_backup(backup, receipt)
    assert before == {path.relative_to(backup): (path.stat().st_mtime_ns, path.read_bytes())
                      for path in backup.rglob('*') if path.is_file()}
    target = tmp_path / 'restored'
    restore_backup(backup, target, receipt)
    assert (target / 'state/translation' / (complete + '.json')).read_bytes() == archived_complete
    assert (target / 'state/translation' / (queued + '.json')).read_bytes() == archived_queued
    assert not (target / 'api-connections.json').exists()
    assert not (target / 'api-operations.sqlite').exists()
    restored = TranslationService(target)
    export = tmp_path / 'offline-export'
    restored.export(complete, export)
    assert (export / 'original.txt').read_text(encoding='utf-8') == original
    translated = (export / 'translated.txt').read_text(encoding='utf-8')
    assert translated == 'ПЕРЕКЛАД &amp; ' + original
    provenance = json.loads((export / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert provenance['schema'] == 'carr-translation-export/2' and provenance['glossary'] == GLOSSARY
    assert not provenance['quotation_authority'] and provenance['source_is_immutable']
    draft = NarrationDraft.from_dict(json.loads((export / 'translation.narration.json').read_text(encoding='utf-8')),
                                     source_text=original, source_id='translation-source:' + sha256(original.encode()).hexdigest())
    assert draft.text == translated and draft.undo() and draft.text == original
    with pytest.raises(PermissionError, match='moved/restored'):
        restored.resume(queued)
    with pytest.raises(FileExistsError):
        restored.export(complete, export)
    rebackup = tmp_path / 'rebackup'
    next_receipt = create_backup(LibraryApp(target), rebackup)
    verify_backup(rebackup, next_receipt)
    assert len(text_calls(transport)) == 1
    for path in backup.rglob('*'):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()
    assert TOKEN.encode() not in (export / 'translation-provenance.json').read_bytes()
