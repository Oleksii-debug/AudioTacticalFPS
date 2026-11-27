"""Real retained sources/jobs/exports with synthetic bounded local HTTP responses.

test_translation_local.py is the shared test transport companion. No actual
translator/model quality or original-layout qualification is implied.
"""
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService, translation_chunks
from carr_library.assistant.translation_documents import (
    paragraph_ranges, capture_document, review_documents, prepare_documents,
    run_documents, export_documents, TranslationBatchPreparedError,
    TranslationBatchRunError, TranslationBatchExportedError, _selections)
from carr_library.speech.reading import ReadingDocument

_spec = importlib.util.spec_from_file_location('translation_local_test_support',
    Path(__file__).with_name('test_translation_local.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
ready = _fixture.ready


@pytest.fixture
def bound(tmp_path, ready):
    from types import SimpleNamespace
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit translation workspace')
    originals = []
    selected = []
    for title, raw in [
        ('First source', 'Original <one> 🙂\r\n\r\nOriginal second paragraph.'),
        ('Second source', 'Other original.\n\nOther final paragraph.'),
    ]:
        entry = app.add_text(workspace, title, raw)
        originals.append((app.assets.resolve(entry['asset']), raw.encode('utf-8')))
        selected.append(capture_document(app, workspace, entry['edition']))
    profile = ready.profile
    ProfileStore(app.root / 'state/providers').save(profile)
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    service.refresh_languages(profile, authorization=_fixture.discovery(profile))
    return SimpleNamespace(app=app, workspace=workspace, service=service, profile=profile,
        selected=tuple(selected), originals=originals, state=ready.state)


def prepare(case):
    review = review_documents(case.service, case.profile, case.selected, 'en', 'uk', '0')
    result = prepare_documents(case.service, case.profile, case.selected, 'en', 'uk', '0',
                               approved_review=review['review_sha256'])
    return result['jobs']


@pytest.mark.parametrize('value', [
    'First\n\nSecond', '  First\r\n \r\nSecond\r\n\r\n',
    'First\n\n\n\n', '\n\n Українська 🙂\r\rAnother.\n\n  ',
])
def test_paragraph_chunks_preserve_exact_unicode_and_all_source_separators(value):
    ranges = paragraph_ranges(value)
    assert ''.join(value[first:last] for first, last in ranges) == value
    assert all(value[first:last].strip() for first, last in ranges)
    assert ''.join(translation_chunks(value, preserve_paragraphs=True)) == value


def test_paragraph_limit_is_inclusive_and_rejects_more_before_job_creation():
    value = 'Selected\n\n' * 2048
    assert len(paragraph_ranges(value)) == 2048
    with pytest.raises(ValueError, match='fewer paragraphs'):
        paragraph_ranges(value + 'Another\n\n')
    with pytest.raises(ValueError):
        translation_chunks('Text', preserve_paragraphs='yes')


def test_batch_review_and_prepare_never_contact_provider_or_mutate_originals(bound):
    before = len(bound.state['calls'])
    review = review_documents(bound.service, bound.profile, bound.selected, 'en', 'uk', '0')
    assert review['provider_calls_started'] is False and review['calls'] == 4
    assert all(Decimal(row['allocated_budget']) == 0 for row in review['documents'])
    result = prepare_documents(bound.service, bound.profile, bound.selected, 'en', 'uk', '0',
                               approved_review=review['review_sha256'])
    assert len(result['jobs']) == 2 and len(bound.state['calls']) == before
    for identity in result['jobs']:
        saved = bound.service.get(identity)
        assert saved['kind'] == 'translation/2' and saved['preserve_paragraphs'] is True
        assert saved['status'] == 'queued' and not saved['receipts']
    for path, original in bound.originals:
        assert path.read_bytes() == original


@pytest.mark.parametrize('change', ['target', 'source', 'range', 'profile'])
def test_old_batch_review_never_authorizes_different_selection_or_profile(bound, change):
    review = review_documents(bound.service, bound.profile, bound.selected, 'en', 'uk', '0')
    selected, p, source, target = bound.selected, bound.profile, 'en', 'uk'
    if change == 'target':
        target = 'en'
    elif change == 'source':
        source = 'auto'
    elif change == 'range':
        selected = selected[:1]
    else:
        p = replace(p, name='Changed explicit profile')
    calls = len(bound.state['calls'])
    with pytest.raises((ValueError, PermissionError)):
        prepare_documents(bound.service, p, selected, source, target, '0',
                          approved_review=review['review_sha256'])
    assert not bound.service.jobs.identities() and len(bound.state['calls']) == calls


def test_duplicate_and_modified_reading_copies_are_not_batch_sources(bound):
    with pytest.raises(ValueError, match='twice'):
        _selections([bound.selected[0], bound.selected[0]])
    bound.selected[0].document.set_text('Edited reading copy.')
    with pytest.raises(ValueError, match='unedited'):
        _selections(bound.selected)


def test_actual_job_resume_and_structured_export_keep_original_lineage(bound, tmp_path):
    identities = prepare(bound)
    assert run_documents(bound.service, identities)['status'] == 'complete'
    output = tmp_path / 'exports'
    output.mkdir()
    receipt = export_documents(bound.service, identities, output)
    assert receipt['status'] == 'complete' and len(receipt['exports']) == 2
    for identity, item, selected in zip(identities, receipt['exports'], bound.selected):
        directory = Path(item['path'])
        original = selected.document.source_text
        assert (directory / 'original.txt').read_bytes() == original.encode()
        mapping = json.loads((directory / 'translation-structure.json').read_text())
        assert mapping['job_id'] == identity and mapping['quotation_authority'] is False
        assert ''.join(row['original'] for row in mapping['paragraphs']) == original
        assert len(mapping['paragraphs']) == 2
        assert all(row['source_positions'] for row in mapping['paragraphs'])
        html = (directory / 'bilingual.html').read_text()
        assert 'Original <one>' not in html
        if '<one>' in original:
            assert '&lt;one&gt;' in html
        report = json.loads((directory / 'translation-provenance.json').read_text())
        assert report['source_anchor'] == selected.document.source_anchor
        for name, digest in report['files'].items():
            assert sha256((directory / name).read_bytes()).hexdigest() == digest
        reading = ReadingDocument.load(directory / 'translation.reading.json')
        assert reading.source_anchor == selected.document.source_anchor
        assert reading.undo() and reading.text == original
    assert all(path.read_bytes() == raw for path, raw in bound.originals)


def test_source_revocation_during_preparation_returns_already_saved_jobs(bound, monkeypatch):
    review = review_documents(bound.service, bound.profile, bound.selected, 'en', 'uk', '0')
    submit = bound.service.submit
    saved = []
    def first(*args, **kwargs):
        identity = submit(*args, **kwargs)
        saved.append(identity)
        if len(saved) == 1:
            bound.app.archive_workspace(bound.workspace)
        return identity
    monkeypatch.setattr(bound.service, 'submit', first)
    calls = len(bound.state['calls'])
    with pytest.raises(TranslationBatchPreparedError) as raised:
        prepare_documents(bound.service, bound.profile, bound.selected, 'en', 'uk', '0',
                          approved_review=review['review_sha256'])
    assert raised.value.receipt['jobs'] == saved and len(saved) == 1
    assert raised.value.receipt['provider_calls_started'] is False
    assert bound.service.get(saved[0])['status'] == 'queued'
    assert len(bound.state['calls']) == calls


def test_cancel_after_first_document_preserves_completion_and_resumes_remaining_once(bound):
    identities = prepare(bound)
    stopped = Event()
    before = len(bound.state['calls'])
    def progress(done, total, phase):
        if bound.service.jobs.load(identities[0])['status'] == 'complete':
            stopped.set()
    with pytest.raises(TranslationBatchRunError) as raised:
        run_documents(bound.service, identities, cancelled=stopped.is_set, progress=progress)
    assert raised.value.receipt['status'] == 'paused'
    assert bound.service.get(identities[0])['status'] == 'complete'
    assert bound.service.get(identities[1])['status'] == 'queued'
    assert len(bound.state['calls']) - before == 2
    before = len(bound.state['calls'])
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert len(bound.state['calls']) - before == 2


def test_changed_current_profile_blocks_new_calls_but_completed_work_stays_offline(bound, tmp_path):
    identities = prepare(bound)
    assert run_documents(bound.service, identities[:1])['status'] == 'complete'
    ProfileStore(bound.app.root / 'state/providers').remove(bound.profile.id)
    before = len(bound.state['calls'])
    assert run_documents(bound.service, identities[:1])['status'] == 'complete'
    with pytest.raises((OSError, ValueError)):
        run_documents(bound.service, identities[1:])
    parent = tmp_path / 'offline-export'
    parent.mkdir()
    assert export_documents(bound.service, identities[:1], parent)['status'] == 'complete'
    assert len(bound.state['calls']) == before


def test_partial_batch_export_returns_real_committed_directories_and_never_deletes_them(bound, tmp_path, monkeypatch):
    identities = prepare(bound)
    run_documents(bound.service, identities)
    original = bound.service.export
    emitted = []
    def interrupted(identity, path, **kwargs):
        if emitted:
            raise OSError('Synthetic second publication failure')
        result = original(identity, path, **kwargs)
        emitted.append(result)
        return result
    monkeypatch.setattr(bound.service, 'export', interrupted)
    parent = tmp_path / 'partial'
    parent.mkdir()
    with pytest.raises(TranslationBatchExportedError) as raised:
        export_documents(bound.service, identities, parent)
    receipt = raised.value.receipt
    assert receipt['status'] == 'partially-published'
    assert receipt['exports'][0]['path'] == emitted[0]['path']
    assert Path(emitted[0]['path']).is_dir() and len(list(parent.iterdir())) == 1
    assert all(bound.service.get(identity)['status'] == 'complete' for identity in identities)


def test_local_structured_state_uses_passive_complete_backup_and_restores_without_server(bound, tmp_path):
    from carr_library.library_backup import create_backup, restore_backup
    from carr_library.backup_producers import _translation_languages, _translation_job
    identities = prepare(bound)
    run_documents(bound.service, identities)
    for identity in identities:
        _translation_job(bound.service.get(identity), identity)
    cached = json.loads(bound.service._languages_path(bound.profile).read_text())
    _translation_languages(cached, bound.profile.id)
    calls = len(bound.state['calls'])
    backup = tmp_path / 'backup'
    digest = create_backup(bound.app, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    service = TranslationService(restored.root / 'state/translation', library=restored)
    assert [service.get(identity)['status'] for identity in identities] == ['complete', 'complete']
    # Existing completed derivatives can be exported after fresh retained-source
    # validation; this does not grant another local/provider request.
    exported = service.export(identities[0], tmp_path / 'verified-restored-export')
    assert exported['status'] == 'complete'
    assert len(bound.state['calls']) == calls


def test_restored_pending_batch_needs_fresh_source_approval_before_any_provider_call(bound, tmp_path):
    from carr_library.library_backup import create_backup, restore_backup
    identities = prepare(bound)
    backup = tmp_path / 'pending-backup'
    digest = create_backup(bound.app, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored-pending', digest))
    service = TranslationService(restored.root / 'state/translation', library=restored)
    calls = len(bound.state['calls'])
    with pytest.raises(PermissionError, match='moved or restored'):
        service.resume(identities[0])
    assert not service.jobs.load(identities[0])['receipts']
    assert len(bound.state['calls']) == calls


def test_cli_document_capture_enforces_remaining_batch_budget_during_reads(bound, tmp_path, monkeypatch):
    from carr_library.assistant import translation_cli
    from carr_library.assistant import translation_documents as documents
    selected = tmp_path / 'selected-ranges.json'
    selected.write_text(json.dumps([
        {'workspace': bound.workspace, 'edition': item.document.source_anchor['edition']}
        for item in bound.selected]))
    capture = documents.capture_document
    observed = []
    def bounded(*args, **kwargs):
        observed.append(kwargs['max_characters'])
        return capture(*args, **kwargs)
    monkeypatch.setattr(documents, 'capture_document', bounded)
    result = translation_cli._document_selections(bound.app, selected, cancelled=lambda: False)
    assert len(result) == 2
    assert observed == [documents.MAX_CHARACTERS,
                        documents.MAX_CHARACTERS - len(result[0].document.source_text)]


def test_direct_local_resume_rereads_current_catalog_before_any_request(bound):
    identities = prepare(bound)
    store = ProfileStore(bound.app.root / 'state/providers')
    store.save(replace(bound.profile, enabled=False))
    before = len(bound.state['calls'])
    with pytest.raises(PermissionError, match='profile changed'):
        bound.service.resume(identities[0])
    assert not bound.service.get(identities[0])['receipts']
    assert len(bound.state['calls']) == before
