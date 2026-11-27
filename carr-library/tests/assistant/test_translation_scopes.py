"""Structured selections use real catalog bytes and synthetic provider replies."""
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json
import socket
from threading import Event

import pytest

from carr_library import providers
from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.state import atomic_json
from carr_library.assistant.translation import TranslationService, language_approval
from carr_library.assistant.translation_selection import (
    DocumentSelection, blocks, capture_selection, validate_selection)
from carr_library.assistant.translation_scope import TranslationScopeService
from carr_library.assistant.translation_scope_state import (
    ScopeApproval, scope_approval, scope_budgets, validate_scope_plan)
from carr_library.assistant.translation_state import (
    snapshot_guard, state_paths, validate_state_tree)
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.providers import BudgetLedger, ProfileStore, ProviderError, ProviderGateway, ProviderProfile

PARENT = 'projects/123456789/locations/us-central1'
ENDPOINT = 'https://translation.googleapis.com/v3/' + PARENT
GLOSSARY = PARENT + '/glossaries/scoped-terms'
TOKEN = 'SYNTHETIC_SCOPED_TRANSLATION_ONLY'


class Vault:
    def __init__(self):
        self.values = {}
    def put(self, reference, binding, value):
        self.values[reference, binding] = value
    def get(self, reference, binding):
        return self.values[reference, binding]
    def delete(self, reference, binding):
        self.values.pop((reference, binding), None)


class Transport:
    def __init__(self):
        self.calls = []
        self.fail = False
    def get(self, profile, route, headers, **kwargs):
        if profile.provider == 'google-translation-v3':
            return json.dumps({'languages': [
                {'languageCode': 'en', 'displayName': 'English', 'supportSource': True},
                {'languageCode': 'uk', 'displayName': 'Ukrainian', 'supportTarget': True}]}).encode()
        return json.dumps({'data': {'languages': [
            {'language': 'en', 'name': 'English'}, {'language': 'uk', 'name': 'Ukrainian'}]}}).encode()
    def post(self, profile, route, raw, headers, **kwargs):
        value = json.loads(raw)
        source = value['contents'][0] if profile.provider == 'google-translation-v3' else value['q']
        self.calls.append(source)
        if self.fail:
            raise RuntimeError('Synthetic uncertain response ' + TOKEN)
        if profile.provider == 'google-translation-v3':
            row = {'translatedText': 'ПЕРЕКЛАД ' + source, 'model': value['model']}
            if 'glossaryConfig' in value:
                row['glossaryConfig'] = value['glossaryConfig']
                return json.dumps({'glossaryTranslations': [row]}, ensure_ascii=False).encode()
            return json.dumps({'translations': [row]}, ensure_ascii=False).encode()
        return json.dumps({'data': {'translations': [
            {'translatedText': 'ПЕРЕКЛАД ' + source, 'detectedSourceLanguage': 'en', 'model': 'nmt'}]}},
            ensure_ascii=False).encode()


def configured(tmp_path, monkeypatch, kind='advanced'):
    monkeypatch.setattr(providers, 'time', lambda: 1_800_000_000.0)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('No real paid-service network in scoped CI'))
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scoped translation')
    vault, transport = Vault(), Transport()
    profiles = ProfileStore(app.root / 'api-connections.json', vault)
    if kind == 'advanced':
        profile = profiles.save_google_token(ProviderProfile('scoped', 'Synthetic v3', 'google-translation-v3',
            ENDPOINT, 'general/nmt', ('translation',)), TOKEN, 1_800_001_800.0)
    else:
        profile = profiles.save(ProviderProfile('scoped', 'Synthetic v2', 'google-translation-v2',
            providers.GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',)), secret=TOKEN)
    plain = TranslationService(app.root, profiles=profiles,
        gateway=ProviderGateway(BudgetLedger(app.root / 'api-operations.sqlite'), vault=vault, transport=transport))
    plain.refresh_languages(profile, approval=language_approval(profile))
    service = TranslationScopeService(app, service=plain)
    book = app.add_text(workspace, 'Source <script>title</script>',
        'First paragraph. Україна 🙂\r\n\f\fSecond paragraph &amp; <script>alert(1)</script>.\n',
        authors=('Synthetic Author',), language='en')
    assert book['state'] == 'complete'
    selection = capture_selection(app, workspace, [DocumentSelection(book['edition'])])
    return app, workspace, book, service, profile, transport, selection


@pytest.fixture
def ready(tmp_path, monkeypatch):
    return configured(tmp_path, monkeypatch)


def approved(service, profile, selection, *, glossary=GLOSSARY, budget=2_000_003, rate='20'):
    preview = service.preview(profile, selection, 'en', 'uk', budget, glossary=glossary, rate=rate)
    permit = scope_approval(profile, selection, 'en', 'uk', budget,
        language_catalog_sha256=preview['language_catalog_sha256'], glossary=glossary, rate=rate)
    identity = service.submit(profile, selection, 'en', 'uk', budget, approval=permit,
                              glossary=glossary, rate=rate)
    return identity, preview


def test_document_selection_retains_empty_sections_unicode_locators_and_source_order(ready):
    _, _, _, _, _, transport, selection = ready
    sections = selection['items'][0]['sections']
    assert [row['ordinal'] for row in sections] == [1, 2, 3]
    assert sections[1]['source_text'] == ''
    assert all(row['locator']['kind'] == 'logical_segment' and row['locator']['printed'] is None for row in sections)
    assert all(row['source_text_verified'] and row['headings'] == [] for row in sections)
    assert len(tuple(blocks(selection))) == 3 and not transport.calls


def test_actual_epub_chapter_keeps_declared_headings_and_excludes_unselected_chapter(ready, tmp_path):
    from tests.reader.test_ebook_reader import book
    app, workspace, _, _, _, _, _ = ready
    path = tmp_path / 'chapters.epub'
    book(path)
    imported = app.import_file(workspace, path, title='Actual synthetic EPUB')
    assert imported['state'] == 'complete'
    selection = capture_selection(app, workspace,
        [DocumentSelection(imported['edition'], chapter=2)], mode='chapter')
    item = selection['items'][0]
    assert item['chapter'] == 2 and [s['ordinal'] for s in item['sections']] == [2]
    section = item['sections'][0]
    assert section['heading_basis'] == 'epub_chapter' and section['headings']
    assert 'Archive context' in section['source_text'] and 'Railway research' not in section['source_text']
    assert not section['source_text_verified'] and section['locator']['printed'] is None
    assert ''.join(unit['text'] for unit in blocks(selection)) == section['source_text']


@pytest.mark.parametrize('kind', ['basic', 'advanced'])
def test_scope_reuses_both_existing_google_job_versions(tmp_path, monkeypatch, kind):
    _, _, _, service, profile, transport, selection = configured(tmp_path, monkeypatch, kind)
    identity, preview = approved(service, profile, selection, glossary=GLOSSARY if kind == 'advanced' else '')
    assert service.resume(identity)['status'] == 'complete'
    plan = service.plans.load(identity)
    assert sum(job['budget_micros'] for job in plan['jobs'] if job is not None) == preview['budget_micros']
    expected = 'carr-translation-job/2' if kind == 'advanced' else 'carr-translation-job/1'
    assert {service.service.jobs.load(job['job_id'])['schema'] for job in plan['jobs'] if job is not None} == {expected}
    assert len(transport.calls) == preview['calls'] == 2


def test_batch_preview_has_exact_per_document_and_block_prices_without_network(ready):
    app, workspace, _, service, profile, transport, selection = ready
    other = app.add_text(workspace, 'Second book', 'Another explicit document.', language='en')
    chosen = capture_selection(app, workspace, [
        DocumentSelection(selection['items'][0]['edition']), DocumentSelection(other['edition'])], mode='batch')
    preview = service.preview(profile, chosen, 'en', 'uk', 2_000_003, glossary=GLOSSARY, rate='20')
    assert len(preview['items']) == 2 and preview['items'][0]['blocks'] == 3
    assert sum(row['budget_micros'] for row in preview['items']) == 2_000_003
    assert sum(row['budget_micros'] for row in preview['blocks']) == 2_000_003
    assert preview['estimated_micros'] == sum(row['estimated_micros'] for row in preview['items'])
    assert preview['items'][1]['estimated_micros'] == len('Another explicit document.') * 20
    assert not transport.calls and not service.plans.identities()


@pytest.mark.parametrize('mode,chapter', [('chapter', None), ('document', 1), ('chapter', 1)])
def test_no_inferred_chapter_from_an_unstructured_text_page(ready, mode, chapter):
    app, workspace, book, _, _, _, _ = ready
    with pytest.raises(ValueError):
        capture_selection(app, workspace, [DocumentSelection(book['edition'], chapter=chapter)], mode=mode)


def test_overlapping_batch_documents_are_rejected_before_any_approval(ready):
    app, workspace, book, _, _, transport, _ = ready
    with pytest.raises(ValueError, match='Overlapping'):
        capture_selection(app, workspace, [DocumentSelection(book['edition']), DocumentSelection(book['edition'])], mode='batch')
    assert not transport.calls


def test_foreign_workspace_is_refused_before_original_access(ready, monkeypatch):
    app, _, book, _, _, _, _ = ready
    other = app.create_workspace('Other authorized workspace')
    monkeypatch.setattr(app, '_retained_source', lambda *a, **k: pytest.fail('Foreign original was read'))
    with pytest.raises(ValueError):
        capture_selection(app, other, [DocumentSelection(book['edition'])])


def test_cancelled_capture_never_opens_an_original(ready, monkeypatch):
    app, workspace, book, _, _, _, _ = ready
    monkeypatch.setattr(app, '_retained_source', lambda *a, **k: pytest.fail('Cancelled source was read'))
    with pytest.raises(InterruptedError):
        capture_selection(app, workspace, [DocumentSelection(book['edition'])], cancelled=lambda: True)


def test_character_preflight_refuses_before_materializing_source_pages(ready, monkeypatch):
    import carr_library.assistant.translation_selection as module
    app, workspace, book, _, _, _, _ = ready
    monkeypatch.setattr(module, 'MAX_SOURCE_CHARACTERS', 10)
    monkeypatch.setattr(module.ReadingDesk, 'page', lambda *a, **k: pytest.fail('Oversized source was allocated'))
    with pytest.raises(ValueError, match='two million'):
        capture_selection(app, workspace, [DocumentSelection(book['edition'])])


@pytest.mark.parametrize('damage', ['digest', 'order', 'locator', 'heading', 'missing', 'extra', 'overlap', 'negative-heading'])
def test_typed_selection_rejects_source_or_structure_forgery(ready, damage):
    selection = json.loads(canonical(ready[-1]))
    item = selection['items'][0]
    section = item['sections'][0]
    if damage == 'digest':
        section['source_text'] += 'changed'
    elif damage == 'order':
        item['sections'].reverse()
    elif damage == 'locator':
        section['locator']['printed'] = 'invented'
    elif damage == 'heading':
        section['heading_basis'] = 'invented'
    elif damage == 'missing':
        item['sections'].pop()
    elif damage == 'extra':
        selection['grant_remote_permission'] = True
    elif damage == 'overlap':
        selection['mode'] = 'batch'
        selection['items'].append(json.loads(canonical(item)))
    else:
        section['headings'] = [{'title': 'Invented heading', 'offset': -1, 'level': 1}]
    with pytest.raises(ValueError):
        validate_selection(selection)


@pytest.mark.parametrize('change', ['text', 'budget', 'language', 'glossary', 'rate'])
def test_reviewed_manifest_cannot_expand_transfer_or_price_authority(ready, change):
    _, _, _, service, profile, transport, selection = ready
    preview = service.preview(profile, selection, 'en', 'uk', 2_000_003, glossary=GLOSSARY, rate='20')
    permit = scope_approval(profile, selection, 'en', 'uk', 2_000_003,
        language_catalog_sha256=preview['language_catalog_sha256'], glossary=GLOSSARY, rate='20')
    chosen = json.loads(canonical(selection))
    budget, source, glossary, rate = 2_000_003, 'en', GLOSSARY, '20'
    if change == 'text':
        chosen['items'][0]['sections'][0]['source_text'] += ' unapproved'
    elif change == 'budget':
        budget += 1
    elif change == 'language':
        source = 'auto'
    elif change == 'glossary':
        glossary = ''
    else:
        rate = '21'
    with pytest.raises((PermissionError, ValueError)):
        service.submit(profile, chosen, source, 'uk', budget, approval=permit, glossary=glossary, rate=rate)
    assert not transport.calls and not service.plans.identities()


def test_preview_cost_above_an_item_reservation_blocks_approval(ready):
    _, _, _, service, profile, transport, selection = ready
    with pytest.raises(ValueError, match='estimate exceeds'):
        service.preview(profile, selection, 'en', 'uk', 1, rate='20')
    assert not transport.calls


def test_saved_scope_pause_restart_and_complete_resume_never_rebill_accepted_children(ready):
    _, _, _, service, profile, transport, selection = ready
    identity, preview = approved(service, profile, selection)
    stop = Event()
    assert service.resume(identity, cancelled=stop.is_set, progress=lambda *args: stop.set())['status'] == 'paused'
    assert len(transport.calls) == 1
    reopened = TranslationScopeService(service.library, service=service.service)
    assert reopened.resume(identity)['status'] == 'complete'
    assert len(transport.calls) == preview['calls'] == 2
    assert reopened.resume(identity)['status'] == 'complete' and len(transport.calls) == 2
    plan = reopened.plans.load(identity)
    assert sum(row['estimate_micros'] for job in plan['jobs'] if job is not None
               for row in service.service.gateway.ledger.report(job['job_id'])) == 2_000_003


def test_uncertain_paid_child_blocks_every_later_item_and_never_retries(ready):
    _, _, _, service, profile, transport, selection = ready
    identity, _ = approved(service, profile, selection)
    transport.fail = True
    with pytest.raises(ProviderError) as caught:
        service.resume(identity)
    assert TOKEN not in str(caught.value) and len(transport.calls) == 1
    assert service.plans.load(identity)['status'] == 'attention'
    transport.fail = False
    with pytest.raises(ProviderError, match='not retried'):
        service.resume(identity)
    assert len(transport.calls) == 1


def test_partial_child_preparation_is_durable_but_cannot_recreate_missing_jobs(ready, monkeypatch):
    _, _, _, service, profile, transport, selection = ready
    original = service.service.submit
    calls = []
    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise OSError('Synthetic child publication failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(service.service, 'submit', fail_second)
    with pytest.raises(OSError):
        approved(service, profile, selection)
    identities = service.plans.identities()
    assert len(identities) == 1 and service.plans.load(identities[0])['status'] == 'preparing'
    validate_state_tree(None, service.root)
    with pytest.raises(PermissionError, match='missing job'):
        service.resume(identities[0])
    assert len(calls) == 2 and not transport.calls


def test_removed_approved_child_is_not_recreated_or_rebilled(ready):
    _, _, _, service, profile, transport, selection = ready
    identity, _ = approved(service, profile, selection)
    plan = service.plans.load(identity)
    service.service.jobs.path(plan['jobs'][0]['job_id']).unlink()
    with pytest.raises(ValueError, match='missing'):
        service.resume(identity)
    with pytest.raises(ValueError, match='missing'):
        validate_state_tree(None, service.root)
    assert not transport.calls


@pytest.mark.parametrize('change', ['title', 'budget', 'profile', 'source'])
def test_child_rebinding_is_rejected_by_typed_backup_and_resume(ready, change):
    _, _, _, service, profile, transport, selection = ready
    identity, _ = approved(service, profile, selection)
    plan = service.plans.load(identity)
    child = service.service.jobs.load(plan['jobs'][0]['job_id'])
    if change == 'title':
        child['title'] = 'Another plan title'
    elif change == 'budget':
        child['approval']['budget_micros'] += 1
    elif change == 'profile':
        child['profile']['name'] = 'Rebound account'
    else:
        child['source_text'] += 'rebound'
    atomic_json(service.service.jobs.path(child['id']), child)
    with pytest.raises((ValueError, ProviderError)):
        validate_state_tree(None, service.root)
    with pytest.raises((ValueError, ProviderError)):
        service.resume(identity)
    assert not transport.calls


def test_archived_scope_revokes_saved_remote_transfer_before_send(ready):
    app, workspace, _, service, profile, transport, selection = ready
    identity, _ = approved(service, profile, selection)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError):
        service.resume(identity)
    assert not transport.calls


def test_original_drift_invalidates_review_before_any_provider_request(ready):
    app, _, book, service, profile, transport, selection = ready
    path = app.assets.resolve(book['asset'])
    path.write_bytes(b'Changed retained original')
    with pytest.raises(ValueError):
        service.preview(profile, selection, 'en', 'uk', 2_000_003)
    assert not transport.calls


def test_complete_derivative_preserves_bilingual_blocks_headings_and_safe_html(ready, tmp_path):
    app, _, book, service, profile, transport, selection = ready
    original = app.assets.resolve(book['asset']).read_bytes()
    identity, _ = approved(service, profile, selection)
    assert service.resume(identity)['status'] == 'complete'
    folder = tmp_path / 'structured-export'
    result = service.export(identity, folder)
    assert result['status'] == 'complete'
    assert json.loads((folder / 'original-selected.json').read_text('utf-8')) == selection
    structure = json.loads((folder / 'translation-structure.json').read_text('utf-8'))
    assert [s['ordinal'] for s in structure['documents'][0]['sections']] == [1, 2, 3]
    assert structure['documents'][0]['sections'][1]['translated_text'] == ''
    assert all(not s['quotation_authority'] for s in structure['documents'][0]['sections'])
    markup = (folder / 'bilingual.html').read_text('utf-8')
    assert '<script>' not in markup and '&lt;script&gt;' in markup
    report = json.loads((folder / 'translation-provenance.json').read_text('utf-8'))
    assert report['source_is_immutable'] and not report['quotation_authority']
    assert report['source_layout_preserved'] is False and report['glossary'] == GLOSSARY
    for name, digest in report['files'].items():
        assert sha256((folder / name).read_bytes()).hexdigest() == digest
    assert all(row['decoded_receipt_integrity'] == 'decoded-ledger-bound'
               for block in report['blocks'] for row in block['receipts'])
    assert app.assets.resolve(book['asset']).read_bytes() == original
    with pytest.raises(FileExistsError):
        service.export(identity, folder)
    assert len(transport.calls) == 2


def test_destination_is_rejected_before_reading_derived_source(ready, monkeypatch):
    _, _, _, service, profile, _, selection = ready
    identity, _ = approved(service, profile, selection)
    monkeypatch.setattr(service.plans, 'load', lambda *a: pytest.fail('Unapproved export read source'))
    with pytest.raises(ValueError):
        service.export(identity, service.root / 'forbidden-export')


def test_incomplete_or_cancelled_export_has_no_complete_marker(ready, tmp_path):
    _, _, _, service, profile, _, selection = ready
    identity, _ = approved(service, profile, selection)
    target = tmp_path / 'not-complete'
    with pytest.raises(ValueError, match='Finish all'):
        service.export(identity, target)
    assert not target.exists()
    with pytest.raises(InterruptedError):
        service.export(identity, target, cancelled=lambda: True)
    assert not target.exists()


def test_typed_backup_restores_passive_structure_without_rebinding_remote_permission(ready, tmp_path):
    app, _, _, service, profile, transport, selection = ready
    complete, _ = approved(service, profile, selection)
    service.resume(complete)
    pending, _ = approved(service, profile, selection)
    original = {path.name: path.read_bytes() for path in state_paths(app.root)}
    backup = tmp_path / 'complete-backup'
    receipt = create_backup(app, backup)
    verify_backup(backup, receipt)
    destination = tmp_path / 'restored'
    restore_backup(backup, destination, receipt)
    assert original == {path.name: path.read_bytes() for path in state_paths(destination)}
    restored = TranslationScopeService(LibraryApp(destination))
    output = tmp_path / 'restored-derivative'
    restored.export(complete, output)
    report = json.loads((output / 'translation-provenance.json').read_text('utf-8'))
    assert all(row['decoded_receipt_integrity'] == 'archived-unverified'
               for block in report['blocks'] for row in block['receipts'])
    with pytest.raises(PermissionError, match='restored'):
        restored.resume(pending)
    assert len(transport.calls) == 2
    assert not (destination / 'api-connections.json').exists()
    assert all(TOKEN.encode() not in path.read_bytes() for path in backup.rglob('*') if path.is_file())


def test_scope_and_child_native_locks_share_existing_snapshot_protocol(ready):
    _, _, _, service, profile, _, selection = ready
    identity, _ = approved(service, profile, selection)
    child = next(job for job in service.plans.load(identity)['jobs'] if job is not None)
    before = {path.name: path.read_bytes() for path in state_paths(service.root)}
    with service.plans.lock(identity), service.service.jobs.lock(child['job_id']):
        with exclusive_writer(service.root), snapshot_guard(service.root):
            validate_state_tree(None, service.root)
    assert before == {path.name: path.read_bytes() for path in state_paths(service.root)}
