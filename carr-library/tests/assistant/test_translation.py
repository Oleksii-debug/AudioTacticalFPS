"""Real library/checkpoints/budgets with an explicit synthetic provider boundary."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import translation as module
from carr_library.assistant.state import atomic_json, read_json
from carr_library.assistant.translation import TranslationService, split_text, validate_backup_file
from carr_library.providers import GOOGLE_TRANSLATE_ENDPOINT, ProfileStore, ProviderError, ProviderProfile
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.reading_desk import ReadingDesk


class Vault:
    def __init__(self): self.values = {}
    def put(self, ref, binding, value): self.values[ref, binding] = value
    def get(self, ref, binding): return self.values[ref, binding]
    def delete(self, ref, binding): self.values.pop((ref, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.after = lambda: None
        self.fail = False
    def get(self, profile, route, parameters, headers, **kwargs):
        self.calls.append(('GET', dict(parameters)))
        return json.dumps({'data':{'languages':[{'language':code,'name':name} for code,name in
            [('en','English'),('uk','Ukrainian'),('sk','Slovak')]]}}).encode()
    def post(self, profile, route, payload, headers, **kwargs):
        body = json.loads(payload)
        self.calls.append(('POST', body))
        if self.fail:
            raise RuntimeError('synthetic-secret-key '+body['q'])
        self.after()
        return json.dumps({'data':{'translations':[{'translatedText':'Derived '+body['q'],
            'model':'nmt','detectedSourceLanguage':'en'}]}}).encode()


@pytest.fixture
def host(tmp_path):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Translation sources')
    vault, transport = Vault(), Transport()
    profile = ProfileStore(app.root/'api-connections.json', vault).save(ProviderProfile(
        'google', 'Synthetic Google account', 'google-translate', GOOGLE_TRANSLATE_ENDPOINT,
        'nmt', ('translation','translation_languages')), secret='synthetic-secret-key')
    service = TranslationService(app, vault=vault, transport=transport)
    assert service.languages(profile.id) == () and transport.calls == []
    service.refresh_languages(profile.id, approved_profile_fingerprint=profile.fingerprint)
    transport.calls.clear()
    return SimpleNamespace(app=app, workspace=workspace, vault=vault, transport=transport,
                           profile=profile, service=service)


def prepared(host, value='Exact original 😀\r\nSecond line.', **kw):
    return host.service.prepare(host.workspace, host.profile.id, value, 'auto', 'uk', **kw)


def approved(service, identity, *, cost=10, budget=10000):
    review = service.review(identity, budget_micros=budget, per_call_micros=cost)
    return service.approve(review, approved_review_sha256=review.digest)


def test_chunks_preserve_every_unicode_and_whitespace_character():
    text = ('😀 e\u0301\r\nSentence.\t' * 700) + '  '
    parts = split_text(text)
    assert ''.join(parts) == text and all(0 < len(part) <= 4500 for part in parts)
    assert len(parts) > 1


def test_prepare_review_and_opening_history_never_send_selected_text(host):
    identity = prepared(host)
    review = host.service.review(identity, budget_micros=20, per_call_micros=10)
    assert review.data['source_text'] == 'Exact original 😀\r\nSecond line.'
    assert review.data['remaining_calls'] == 1 and review.data['quotation_authority'] is False
    assert host.service.jobs(host.workspace)[0]['id'] == identity
    assert host.service.details(identity)['status'] == 'prepared'
    assert host.transport.calls == []
    raw = host.service.store.path(identity).read_bytes()
    assert b'synthetic-secret-key' not in raw and b'url-api-' not in raw
    assert b'APPROVED_REMOTE' not in raw


def test_languages_require_explicit_current_profile_approval(host):
    with pytest.raises(PermissionError):
        host.service.refresh_languages(host.profile.id, approved_profile_fingerprint='0'*64)
    with pytest.raises(ValueError):
        host.service.prepare(host.workspace, host.profile.id, 'Text', 'auto', 'unsupported')
    assert host.transport.calls == []


def test_completed_chunks_are_durable_before_late_cancel_and_resume_needs_new_grant(host):
    value = 'Original 😀\r\n' * 800
    identity = prepared(host, value)
    stop = Event()
    grant = approved(host.service, identity)
    host.transport.after = stop.set
    result = host.service.run(identity, grant=grant, cancelled=stop.is_set)
    assert result['status'] == 'paused' and result['completed'] == 1
    saved = host.service.details(identity)
    assert saved['receipts'][0]['text'] == 'Derived '+saved['plan']['chunks'][0]
    assert saved['pending'] is None
    reopened = TranslationService(host.app, vault=host.vault, transport=host.transport)
    with pytest.raises(PermissionError):
        reopened.run(identity, grant=grant)
    host.transport.after = lambda: None
    result = reopened.run(identity, grant=approved(reopened, identity))
    assert result['status'] == 'complete'
    posts = [row for method,row in host.transport.calls if method == 'POST']
    assert [row['q'] for row in posts] == list(split_text(value))
    assert len(posts) == len(split_text(value))
    assert all('source' not in row and row['format']=='text' and row['model']=='nmt' for row in posts)


def test_copied_checkpoint_cannot_reuse_session_authority(host, tmp_path):
    identity = prepared(host)
    grant = approved(host.service, identity)
    target = tmp_path/'copied-library'
    shutil.copytree(host.app.root, target)
    copied = TranslationService(LibraryApp(target), vault=host.vault, transport=host.transport)
    with pytest.raises(PermissionError):
        copied.run(identity, grant=grant)
    assert host.transport.calls == []
    result = copied.run(identity, grant=approved(copied, identity))
    assert result['status'] == 'complete' and len(host.transport.calls) == 1


def test_uncertain_operation_is_never_repeated_after_error_or_restart(host):
    identity = prepared(host)
    host.transport.fail = True
    with pytest.raises(ProviderError) as error:
        host.service.run(identity, grant=approved(host.service, identity))
    assert 'synthetic-secret-key' not in str(error.value)
    assert error.value.__cause__ is None
    record = host.service.details(identity)
    assert record['status'] == 'attention' and record['pending']['operation_id'] == identity+'_0'
    for service in (host.service, TranslationService(host.app, vault=host.vault, transport=host.transport)):
        with pytest.raises(ProviderError, match='charged'):
            service.review(identity, budget_micros=10000, per_call_micros=10)
    assert len(host.transport.calls) == 1


def test_paid_response_checkpoint_failure_keeps_no_retry_marker(host, monkeypatch):
    identity = prepared(host)
    grant = approved(host.service, identity)
    write = host.service._write
    def fail_accept(value, **kw):
        if value['receipts']:
            raise OSError('Synthetic disk full after the provider response')
        return write(value, **kw)
    monkeypatch.setattr(host.service, '_write', fail_accept)
    with pytest.raises(ProviderError, match='uncertain'):
        host.service.run(identity, grant=grant)
    record = host.service.details(identity)
    assert record['pending'] is not None and not record['receipts']
    with pytest.raises(ProviderError):
        host.service.review(identity, budget_micros=10000, per_call_micros=10)
    assert len(host.transport.calls) == 1


def test_error_recovery_uses_the_loaded_checkpoint_lease_not_a_second_read(host, monkeypatch):
    identity = prepared(host)
    grant = approved(host.service, identity)
    original_load = host.service._load
    replaced = []
    marker = '2026-11-27T00:00:00.000001Z'
    def load(identity):
        value = original_load(identity)
        if host.transport.calls and not replaced:
            other = json.loads(json.dumps(value))
            other['updated'] = marker
            atomic_json(host.service.store.path(identity), other)
            replaced.append(True)
        return value
    monkeypatch.setattr(host.service, '_load', load)
    host.transport.fail = True
    with pytest.raises(ProviderError):
        host.service.run(identity, grant=grant)
    assert read_json(host.service.store.path(identity))['updated'] == marker


def test_exact_current_profile_and_caller_budget_are_required(host):
    identity = prepared(host, 'Sentence. ' * 1000)
    with pytest.raises(PermissionError, match='budget'):
        host.service.review(identity, budget_micros=1, per_call_micros=10)
    review = host.service.review(identity, budget_micros=100, per_call_micros=10)
    host.service.profiles.save(replace(host.profile, name='Changed profile'))
    with pytest.raises(PermissionError, match='profile'):
        host.service.approve(review, approved_review_sha256=review.digest)
    assert host.transport.calls == []


def test_archived_topic_blocks_new_request_even_after_approval(host):
    topic = host.app.create_topic(host.workspace, 'Authorized topic')
    identity = prepared(host, topic=topic)
    grant = approved(host.service, identity)
    with host.app.topic_transaction(host.workspace) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError):
        host.service.run(identity, grant=grant)
    assert host.transport.calls == []


def test_source_validation_observes_cancellation_before_preparing_job(host, monkeypatch):
    imported = host.app.add_text(host.workspace, 'Source', 'Exact source')
    page = ReadingDesk(host.app, host.workspace, imported['edition']).page(1)
    stop = Event()
    original = host.app._retained_source
    def retained(*args, **kwargs):
        stop.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(host.app, '_retained_source', retained)
    with pytest.raises((InterruptedError, TimeoutError)):
        prepared(host, page.raw_text, source_location=page.location, cancelled=stop.is_set)
    assert host.service.store.identities() == () and host.transport.calls == []


@pytest.mark.parametrize('guard', ['bound', 'transport'])
def test_reentrant_cancellation_cannot_keep_a_withdrawn_data_folder(host, tmp_path, guard):
    other = LibraryApp(tmp_path/'replacement')
    def changed():
        host.service.app = other
        return False
    with pytest.raises(PermissionError, match='folder changed'):
        if guard == 'bound':
            host.service._bound(host.workspace, cancelled=changed)
        else:
            host.service._cancelled(changed)
    assert host.transport.calls == []


def test_history_checks_cancellation_before_loading_saved_job(host, monkeypatch):
    prepared(host)
    monkeypatch.setattr(module, 'read_json', lambda *args,**kwargs:pytest.fail('Cancelled job was read'))
    with pytest.raises(InterruptedError):
        host.service.jobs(host.workspace, cancelled=lambda:True)


def test_reading_copy_origin_original_and_translation_survive_owned_save(host, tmp_path):
    raw = 'Immutable source 😀\r\nOriginal words.'
    imported = host.app.add_text(host.workspace, 'Source', raw)
    page = ReadingDesk(host.app, host.workspace, imported['edition']).page(1)
    store = NarrationStore(host.app.root)
    source = revise(new_project(raw, source_location=page.location), 'Explicit saved reading revision')
    source_digest = store.save(source)
    identity = host.service.prepare_reading_copy(host.workspace, host.profile.id, source.id, source_digest,
                                                'auto', 'uk', title='Ukrainian derivative')
    host.service.run(identity, grant=approved(host.service, identity))
    project_id, digest = host.service.save_reading_copy(identity)
    reopened, actual = store.load(project_id)
    assert actual == digest and reopened.original_text == raw and reopened.source_location == page.location
    assert reopened.current_text == 'Derived '+source.current_text
    assert host.service.save_reading_copy(identity) == (project_id, digest)
    assert store.load(source.id)[1] == source_digest
    assert host.app.assets.resolve(imported['asset']).read_bytes() == raw.encode()
    assert host.service.result(identity)[0]['quotation_authority'] is False
    target = tmp_path/'bilingual.txt'
    host.service.export(identity, target, bilingual=True)
    assert 'ORIGINAL\n'+source.current_text in target.read_text()
    assert 'TRANSLATION\nDerived '+source.current_text in target.read_text()
    plain = tmp_path/'translated.txt'
    host.service.export(identity, plain)
    exported = plain.read_text(encoding='utf-8')
    assert 'Google Cloud Translation (NMT)' in exported and 'not quotation evidence' in exported
    assert exported.endswith('Derived '+source.current_text) and identity in exported
    with pytest.raises(FileExistsError):
        host.service.export(identity, target)
    with pytest.raises(ValueError):
        host.service.export(identity, host.app.root/'unsafe.txt')
    changed = revise(reopened, 'Later user edit')
    store.save(changed, expected_digest=digest)
    with pytest.raises(ValueError, match='kept'):
        host.service.save_reading_copy(identity)
    assert store.load(project_id)[0].current_text == 'Later user edit'


def test_source_copy_from_another_library_is_never_adopted(host):
    other = host.app.create_workspace('Other')
    imported = host.app.add_text(other, 'Other source', 'Same visible text')
    page = ReadingDesk(host.app, other, imported['edition']).page(1)
    store = NarrationStore(host.app.root)
    project = new_project(page.raw_text, source_location=page.location)
    digest = store.save(project)
    with pytest.raises(ValueError):
        host.service.prepare_reading_copy(host.workspace, host.profile.id, project.id, digest, 'auto', 'uk')
    assert host.service.store.identities() == () and host.transport.calls == []


@pytest.mark.parametrize('kind', ['authority','plan','chunk','receipt','pending','unknown-field','nonfinite','duplicate'])
def test_tampered_checkpoint_is_rejected_without_rewrite_or_request(host, kind):
    identity = prepared(host)
    path = host.service.store.path(identity)
    value = json.loads(path.read_bytes())
    if kind == 'authority': value['quotation_authority'] = True
    elif kind == 'plan': value['plan']['source_text'] = 'Changed input'
    elif kind == 'chunk': value['plan']['chunks'][0] += 'Changed chunk'
    elif kind == 'receipt': value['receipts'] = [{'text':'Invented'}]
    elif kind == 'pending': value['pending'] = {'index':0}
    elif kind == 'unknown-field': value['approval'] = {'mode':'APPROVED_REMOTE'}
    elif kind == 'nonfinite': value['receipts'] = [float('nan')]
    raw = json.dumps(value).encode()
    if kind == 'duplicate': raw = raw.replace(b'"quotation_authority": false', b'"quotation_authority": false, "quotation_authority": false')
    path.write_bytes(raw)
    with pytest.raises((ValueError, TypeError)):
        host.service.review(identity, budget_micros=100, per_call_micros=10)
    assert path.read_bytes() == raw and host.transport.calls == []


def test_strict_backup_validator_keeps_bytes_and_contains_no_approval(host):
    identity = prepared(host)
    host.service.run(identity, grant=approved(host.service, identity))
    path = host.service.store.path(identity)
    raw = path.read_bytes()
    value = validate_backup_file('state/translations/'+path.name, raw)
    assert value['status'] == 'complete' and value['pending'] is None
    assert raw == path.read_bytes()
    for name in ('../'+path.name, 'state/translations/../'+path.name, identity+'.control.json', 'approval.json'):
        with pytest.raises(ValueError):
            validate_backup_file(name, raw)


def test_live_language_reader_and_backup_share_the_exact_cache_byte_limit(host):
    rows = host.service.refresh_languages(host.profile.id, approved_profile_fingerprint=host.profile.fingerprint)
    path = host.service.store.directory / ('languages-' + host.profile.id + '.json')
    raw = path.read_bytes()
    padded = raw + b' ' * (module.MAX_LANGUAGE_BYTES - len(raw))
    path.write_bytes(padded)
    assert host.service.languages(host.profile.id) == rows
    assert validate_backup_file('state/translations/' + path.name, padded)['languages'] == list(rows)
    oversized = padded + b' '
    path.write_bytes(oversized)
    with pytest.raises(ValueError):
        host.service.languages(host.profile.id)
    with pytest.raises(ValueError):
        validate_backup_file('state/translations/' + path.name, oversized)
    assert path.read_bytes() == oversized
