"""Exact structure on real durable jobs with synthetic gateway responses only."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.translation import TranslationService, translation_approval
from carr_library.assistant.translation_state import validate_translation_job, job_parts
from carr_library.assistant.translation_structure import (
    capture_text, capture_library, source_text, structure_hash, validate_structure,
    parts_and_mask, validate_current_sources)
from tests.assistant.test_translation import ready, text_calls
from tests.assistant.test_translation_advanced import ready as advanced_ready

TEXT = '# Exact heading ##\r\n\r\nOriginal <tag> & text.\n\n' + '```python\nkeep_this(12)\n```\n' + '## Next\nTail.'


def submit(service, profile, document):
    value = source_text(document)
    approval = translation_approval(profile, value, 'en', 'uk', 3_000_000, structure=document)
    return service.submit(profile, value, 'en', 'uk', approval=approval,
                          title='Structured derivative', structure=document)


def test_exact_markdown_boundaries_and_literal_code_survive_real_job_export(ready, tmp_path):
    service, profile, vault, transport = ready
    document = capture_text(TEXT, title='Original title', markdown=True)
    parts, mask = parts_and_mask(TEXT, document)
    assert ''.join(parts) == TEXT and sum(mask) == 4
    assert all('```' not in part and 'keep_this' not in part for part, remote in zip(parts, mask) if remote)
    preview = service.preview(profile, TEXT, 'en', 'uk', structure=document, rate_per_million='20')
    assert preview['calls'] == 4 and preview['structure_sha256'] == structure_hash(document)
    assert not text_calls(transport)
    identity = submit(service, profile, document)
    assert not text_calls(transport)
    result = service.resume(identity)
    assert result['status'] == 'complete' and len(text_calls(transport)) == 4
    job = service.jobs.load(identity)
    assert job['schema'] == 'carr-translation-job/3'
    assert job['source_text'] == TEXT and job['structure'] == document
    assert sum(value for value in job['allocations'] if value is not None) == 3_000_000
    for chunk, remote, receipt in zip(parts, mask, job['receipts']):
        if not remote:
            assert receipt['text'] == chunk and receipt['operation_id'] is None
    destination = tmp_path / 'structured-export'
    service.export(identity, destination)
    assert (destination / 'original.txt').read_bytes() == TEXT.encode('utf-8')
    translated = (destination / 'translated.txt').read_text(encoding='utf-8')
    assert '# ПЕРЕКЛАД: Exact heading ##' in translated
    assert '```python\nkeep_this(12)\n```\n' in translated
    html = (destination / 'structured-bilingual.html').read_text(encoding='utf-8')
    assert '<h1>ПЕРЕКЛАД: Exact heading</h1>' in html and '&lt;tag&gt; &amp;' in html
    manifest = json.loads((destination / 'translation-provenance.json').read_text(encoding='utf-8'))
    assert manifest['schema'] == 'carr-translation-export/3'
    assert manifest['structure_sha256'] == structure_hash(document) and not manifest['quotation_authority']
    assert manifest['files']['structured-translation.json'] == sha256(
        (destination / 'structured-translation.json').read_bytes()).hexdigest()


def test_accepted_structured_chunks_resume_without_repeating_remote_calls(ready):
    service, profile, vault, transport = ready
    document = capture_text(TEXT, markdown=True)
    identity = submit(service, profile, document)
    stop = Event()
    def progress(done, total, phase):
        if text_calls(transport):
            stop.set()
    result = service.resume(identity, cancelled=stop.is_set, progress=progress)
    assert result['status'] == 'paused' and len(text_calls(transport)) == 1
    accepted = deepcopy(service.jobs.load(identity)['receipts'])
    reopened = TranslationService(service.root, gateway=service.gateway, profiles=service.profiles)
    assert reopened.resume(identity)['status'] == 'complete'
    assert len(text_calls(transport)) == 4
    assert reopened.jobs.load(identity)['receipts'][:len(accepted)] == accepted


@pytest.mark.parametrize('alter', ['text', 'heading-level', 'literal-receipt', 'context'])
def test_typed_structure_and_receipts_reject_rehashed_inconsistent_state(ready, alter):
    service, profile, vault, transport = ready
    document = capture_text(TEXT, markdown=True)
    identity = submit(service, profile, document)
    service.resume(identity)
    job = deepcopy(service.jobs.load(identity))
    if alter == 'text':
        job['structure']['documents'][0]['sections'][1]['text'] += 'Changed'
    elif alter == 'heading-level':
        job['structure']['documents'][0]['sections'][1]['level'] = True
    elif alter == 'literal-receipt':
        job['receipts'][0]['text'] = '# CHANGED'
        job['receipts'][0]['translation_sha256'] = sha256(b'# CHANGED').hexdigest()
    else:
        job['structure']['documents'][0]['context'] = {}
    with pytest.raises(ValueError):
        validate_translation_job(job)


def test_library_batch_preserves_selected_order_and_revalidates_original_authority(ready):
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Exact source batch')
    first = app.add_text(workspace, 'First source', 'First unchanged source.')
    second = app.add_text(workspace, 'Second source', 'Second unchanged source.')
    selection = [{'workspace': workspace, 'edition': item['edition'], 'first': 1,
                  'last': None, 'kind': 'document'} for item in (second, first)]
    document = capture_library(app, selection)
    assert [item['title'] for item in document['documents']] == ['Second source', 'First source']
    assert all(item['context'] is not None for item in document['documents'])
    validate_current_sources(app, document)
    identity = submit(service, profile, document)
    app.archive_workspace(workspace)
    with pytest.raises((ValueError, PermissionError), match='archived|missing|available'):
        service.resume(identity)
    assert not text_calls(transport)
    assert service.jobs.load(identity)['status'] == 'attention'


def test_current_metadata_change_requires_recapture_before_structured_submission(ready):
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Exact source metadata')
    book = app.add_text(workspace, 'Original title', 'Unchanged source text.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'first': 1, 'last': None, 'kind': 'document'}])
    app.update_metadata(workspace, book['edition'], title='Changed title')
    with pytest.raises(ValueError, match='revision|metadata|changed'):
        submit(service, profile, document)
    assert service.list() == () and not text_calls(transport)


def test_source_structure_and_literal_budget_are_checked_before_combined_allocation():
    document = capture_text('Text.')
    shared = {'kind': 'literal', 'level': 0, 'text': 'x' * 200000, 'origin': None}
    document['documents'][0]['sections'] = [shared] * 20
    with pytest.raises(ValueError, match='million'):
        validate_structure(document)
    with pytest.raises(ValueError, match='nonempty paragraph'):
        parts_and_mask('```\ncode\n```', capture_text('```\ncode\n```', markdown=True))


def test_fence_like_line_with_info_does_not_resume_translation_inside_code():
    text = '```python\n```still-code\nLiteral body.\n```\nParagraph.'
    document = capture_text(text, markdown=True)
    parts, mask = parts_and_mask(text, document)
    assert [part for part, remote in zip(parts, mask) if remote] == ['Paragraph.']


def test_reviewed_job_digest_is_checked_under_current_job_lock_before_any_request(ready):
    service, profile, vault, transport = ready
    identity = submit(service, profile, capture_text('Unchanged source.'))
    before = service.jobs.load(identity)
    with pytest.raises(PermissionError, match='reviewed translation job'):
        service.resume(identity, expected_job_sha256='0' * 64)
    assert not text_calls(transport) and service.jobs.load(identity) == before
    digest = sha256(canonical(before).encode('utf-8')).hexdigest()
    assert service.resume(identity, expected_job_sha256=digest)['status'] == 'complete'

def test_advanced_structured_job_keeps_glossary_receipts_only_on_remote_sections(advanced_ready):
    from tests.assistant.test_translation_advanced import GLOSSARY, PARENT, text_calls as advanced_calls
    app, service, selected, vault, transport, clock = advanced_ready
    document = capture_text(TEXT, markdown=True)
    value = source_text(document)
    approval = translation_approval(selected, value, 'en', 'uk', 1_000_001,
                                    glossary=GLOSSARY, structure=document)
    identity = service.submit(selected, value, 'en', 'uk', approval=approval,
                              glossary=GLOSSARY, structure=document)
    assert not advanced_calls(transport)
    assert service.resume(identity)['status'] == 'complete'
    job = service.jobs.load(identity)
    parts, mask = job_parts(job)
    assert job['schema'] == 'carr-translation-job/3' and len(advanced_calls(transport)) == sum(mask) == 4
    assert sum(part for part in job['allocations'] if part is not None) == 1_000_001
    for original, remote, receipt in zip(parts, mask, job['receipts']):
        if remote:
            assert receipt['reported_model'] == PARENT + '/models/general/nmt'
            assert receipt['glossary'] == GLOSSARY
        else:
            assert receipt['text'] == original and receipt['operation_id'] is None
            assert receipt['reported_model'] is None and receipt['glossary'] is None
    service.resume(identity)
    assert len(advanced_calls(transport)) == 4


def test_structured_schema_cannot_drop_source_context_after_library_revocation(ready):
    from carr_library.assistant.state import atomic_json
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Bound structured source')
    book = app.add_text(workspace, 'One paragraph', 'Exactly one unchanged paragraph.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}])
    identity = submit(service, profile, document)
    app.archive_workspace(workspace)
    forged = deepcopy(service.jobs.load(identity))
    forged['structure'] = None
    with pytest.raises(ValueError, match='document selection'):
        validate_translation_job(forged)
    atomic_json(service.jobs.path(identity), forged)
    with pytest.raises(ValueError, match='document selection'):
        service.resume(identity)
    assert not text_calls(transport)


@pytest.mark.parametrize('alter', ['title', 'source-kind'])
def test_retained_document_labels_cannot_diverge_before_preview_or_paid_work(ready, alter):
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Exact document labels')
    book = app.add_text(workspace, 'Original title', 'Exact source.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}])
    changed = deepcopy(document)
    if alter == 'title':
        changed['documents'][0]['title'] = 'False source title'
    else:
        changed['documents'][0]['kind'] = 'selected-text'
    with pytest.raises(ValueError, match='title|profile'):
        service.preview(profile, source_text(changed), 'en', 'uk', structure=changed)
    assert not text_calls(transport) and service.list() == ()


def test_cancellation_during_source_revalidation_pauses_and_keeps_paid_receipts(ready, monkeypatch):
    from carr_library.assistant import translation_structure as module
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Cancellable retained source')
    book = app.add_text(workspace, 'Two paragraphs', 'First exact paragraph.\n\nSecond exact paragraph.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}])
    identity = submit(service, profile, document)
    original = module.validate_current_sources
    stopped, validations = Event(), [0]
    def validate(*args, **kwargs):
        validations[0] += 1
        if validations[0] == 2:
            stopped.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'validate_current_sources', validate)
    result = service.resume(identity, cancelled=stopped.is_set)
    saved = service.jobs.load(identity)
    accepted = deepcopy(saved['receipts'])
    assert result['status'] == 'paused' and saved['pending'] is None
    assert len(text_calls(transport)) == 1 and accepted[0]['operation_id'] is not None
    monkeypatch.setattr(module, 'validate_current_sources', original)
    stopped.clear()
    assert service.resume(identity)['status'] == 'complete'
    assert service.jobs.load(identity)['receipts'][:len(accepted)] == accepted
    assert len(text_calls(transport)) == 2


def test_observer_exception_cannot_relabel_or_repeat_durable_structured_output(ready):
    service, profile, vault, transport = ready
    identity = submit(service, profile, capture_text(TEXT, markdown=True))
    def broken_observer(*args):
        raise RuntimeError('Synthetic observer failure after durable receipt.')
    assert service.resume(identity, progress=broken_observer)['status'] == 'complete'
    assert len(text_calls(transport)) == 4
    service.resume(identity)
    assert len(text_calls(transport)) == 4
