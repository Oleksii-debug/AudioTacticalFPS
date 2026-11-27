"""Exact partitions reuse ordinary retained-source jobs and accepted receipts.

Real DOCX parser children and real backup/restore are used. The translator is
the existing bounded synthetic HTTP transport; no model quality is claimed.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
from pathlib import Path
import shutil
from uuid import NAMESPACE_URL, uuid5

import pytest
from docx import Document

from carr_library.application import LibraryApp
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService, translation_approval, job_chunks
from carr_library.assistant.translation_documents import (
    SelectedDocument, run_documents, review_documents, prepare_documents)
from carr_library.assistant.translation_layout import (
    capture_layout, review_layout, prepare_layout, export_layout, order_layout_jobs)
from carr_library.assistant.translation_partition import (
    make_partition, partition_ranges, partition_chunk_groups, partition_chunks)
from carr_library.durable_state import atomic_json
from carr_library.library_backup import create_backup, restore_backup

_spec = importlib.util.spec_from_file_location('partition_layout_fixture',
    Path(__file__).with_name('test_translation_layout.py'))
_layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_layout)
ready = _layout.ready
bound = _layout.bound


def small_partition(value='First.\n\nSecond.'):
    return make_partition(value, 'carr-docx-body-layout/1', 'a' * 64,
                          [6, 8, len(value)])


def larger(case, *, count=65):
    source = case.source.with_name('Explicit larger source.docx')
    raw = _layout.docx_bytes(paragraphs=count, rich=False)
    source.write_bytes(raw)
    result = case.app._import_file(case.workspace, source, title='Larger retained source',
        work_id='partition-work', edition_id='partition-edition')
    assert result['state'] == 'complete'
    layout = capture_layout(case.app, case.workspace, 'partition-edition')
    return layout, source, raw


def saved(case, layout):
    review = review_layout(case.service, case.profile, layout, 'en', 'uk', '0')
    return prepare_layout(case.service, case.profile, layout, 'en', 'uk', '0',
                          approved_review=review['review_sha256'])['jobs']


@pytest.mark.parametrize('change', [
    {'boundaries': []}, {'boundaries': [0, 15]}, {'boundaries': [6, 6, 15]},
    {'boundaries': [8, 6, 15]}, {'boundaries': [6, 8, 14]},
    {'boundaries': [6, 8, 16]}, {'boundaries': [True, 8, 15]},
    {'boundaries': [6.0, 8, 15]}, {'boundaries': tuple([6, 8, 15])},
    {'profile': 'unreviewed-layout'}, {'profile': []},
    {'mapping_sha256': 'A' * 64}, {'mapping_sha256': 'a' * 63},
    {'schema': 'carr-translation-partition/2'}, {'extra': 'unexpected'},
])
def test_partition_rejects_ambiguous_or_incomplete_coverage(change):
    value = small_partition()
    value.update(change)
    with pytest.raises(ValueError):
        partition_chunks('First.\n\nSecond.', value)


def test_partition_preserves_exact_source_and_whitespace_without_transfer_chunks():
    value = 'First.\n\nSecond.'
    partition = small_partition(value)
    assert partition_ranges(value, partition) == ((0, 6), (6, 8), (8, 15))
    assert partition_chunk_groups(value, partition) == (('First.',), ('\n\n',), ('Second.',))
    assert ''.join(partition_chunks(value, partition)) == value
    with pytest.raises(ValueError):
        make_partition('x' * 4097, 'carr-docx-body-layout/1', 'a' * 64, range(1, 4098))


def test_partition_cannot_attach_to_pasted_text_or_competing_paragraph_policy(ready):
    partition = small_partition()
    before = list(ready.state['calls'])
    with pytest.raises(ValueError, match='retained source'):
        translation_approval(ready.profile, 'First.\n\nSecond.', 'en', 'uk', '0',
                             source_partition=partition)
    with pytest.raises(ValueError, match='partition policy'):
        ready.service.preview(ready.profile, 'First.\n\nSecond.', 'en', 'uk',
                              preserve_paragraphs=True, source_partition=partition)
    approval, cost = translation_approval(ready.profile, 'First.\n\nSecond.', 'en', 'uk', '0',
                                          source_bound=True, source_partition=partition)
    with pytest.raises(ValueError, match='retained source'):
        ready.service.submit(ready.profile, 'First.\n\nSecond.', 'en', 'uk',
            authorization=approval, per_call_cost=cost, source_partition=partition)
    assert ready.state['calls'] == before
    assert ready.service.jobs.identities() == ()


def test_absent_partition_keeps_original_source_job_uuid_and_chunks(bound):
    layout = capture_layout(bound.app, bound.workspace, bound.edition)
    identities = saved(bound, layout)
    for identity in identities:
        job = bound.service.get(identity)
        assert 'source_partition' not in job
        original = {key: job[key] for key in (
            'source_binding', 'source_sha256', 'authorization', 'profile', 'preserve_paragraphs')}
        assert identity == uuid5(NAMESPACE_URL,
            'urn:carr-library:translation/2:' + canonical(original)).hex
        assert list(job_chunks(job)) == job['chunks']
    assert len(identities) == 6


def test_larger_docx_uses_one_job_with_exact_paragraph_and_separator_receipts(bound, tmp_path):
    layout, source, raw = larger(bound)
    before = len(bound.state['calls'])
    identities = saved(bound, layout)
    assert len(identities) == len(layout.selections) == 1
    assert len(bound.state['calls']) == before
    job = bound.service.get(identities[0])
    assert job['kind'] == 'translation/2' and job['source_partition'] == layout.source_partition
    assert len(job['source_binding']['anchor']['positions']) == 65
    assert sum(bool(chunk.strip()) for chunk in job['chunks']) == 65
    assert len(job['chunks']) == 129 and ''.join(job['chunks']) == job['source_text']
    assert order_layout_jobs(bound.service, layout, identities) == tuple(identities)
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert len(bound.state['calls']) - before == 65
    target = tmp_path / 'Larger translated.docx'
    result = export_layout(bound.service, layout, identities, target)
    assert result['status'] == 'complete' and len(result['jobs']) == 65
    assert len({row['id'] for row in result['jobs']}) == 1
    original, translated = Document(BytesIO(raw)), Document(target)
    # This is the existing LibreTranslate format=text fixture, whose response
    # deliberately contains the literal entity string. It is already plaintext;
    # a second HTML unescape would corrupt the accepted output/receipt binding.
    expected = ['DERIVED: ' + paragraph.text + ' &amp;' for paragraph in original.paragraphs]
    assert [paragraph.text for paragraph in translated.paragraphs] == expected
    completed_job = bound.service.get(identities[0])
    assert [receipt['text'] for chunk, receipt in zip(completed_job['chunks'], completed_job['receipts'])
            if chunk.strip()] == expected
    assert [row['translation_sha256'] for row in result['jobs']] == [
        sha256(value.encode('utf-8')).hexdigest() for value in expected]
    assert all(len(row['receipt_indices']) == 1 for row in result['jobs'])
    assert source.read_bytes() == raw


def test_partition_pause_resumes_only_unaccepted_requests(bound, tmp_path):
    layout, _, _ = larger(bound)
    identities = saved(bound, layout)
    calls = len(bound.state['calls'])
    stopped = False
    def progress(done, total, phase):
        nonlocal stopped
        if done == 1:
            stopped = True
    result = bound.service.resume(identities[0], cancelled=lambda: stopped, progress=progress)
    assert result['status'] == 'paused'
    job = bound.service.get(identities[0])
    assert len(job['receipts']) == 1 and len(bound.state['calls']) - calls == 1
    with pytest.raises(ValueError, match='Finish every'):
        export_layout(bound.service, layout, identities, tmp_path / 'unfinished.docx')
    assert not (tmp_path / 'unfinished.docx').exists()
    accepted = deepcopy(job['receipts'][0])
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert len(bound.state['calls']) - calls == 65
    assert bound.service.get(identities[0])['receipts'][0] == accepted


def test_changed_partition_is_refused_before_request_and_before_output(bound, tmp_path):
    layout, _, _ = larger(bound)
    identities = saved(bound, layout)
    job = bound.service.jobs.load(identities[0])
    job['source_partition']['mapping_sha256'] = '0' * 64
    bound.service.jobs.save(job)
    calls = list(bound.state['calls'])
    with pytest.raises(ValueError, match='saved job identity'):
        bound.service.resume(identities[0])
    with pytest.raises(ValueError):
        export_layout(bound.service, layout, identities, tmp_path / 'forged.docx')
    assert not (tmp_path / 'forged.docx').exists()
    assert bound.state['calls'] == calls


def test_partition_checks_actual_root_again_before_next_transfer(bound, tmp_path):
    layout, _, _ = larger(bound)
    identities = saved(bound, layout)
    original_root = bound.app.root
    moved = tmp_path / 'moved-owned-library'
    calls = len(bound.state['calls'])
    changed = False
    def progress(done, total, phase):
        nonlocal changed
        if done == 1 and not changed:
            original_root.rename(moved)
            original_root.mkdir()
            changed = True
    try:
        with pytest.raises((ValueError, PermissionError, OSError)):
            bound.service.resume(identities[0], progress=progress)
        assert changed and len(bound.state['calls']) - calls == 1
    finally:
        if changed:
            shutil.rmtree(original_root)
            moved.rename(original_root)
    assert len(bound.service.get(identities[0])['receipts']) == 1


def test_full_backup_restores_partition_job_for_offline_layout_export(bound, tmp_path, monkeypatch):
    layout, source, raw = larger(bound)
    identities = saved(bound, layout)
    assert run_documents(bound.service, identities)['status'] == 'complete'
    baseline = tmp_path / 'original-output.docx'
    export_layout(bound.service, layout, identities, baseline)
    expected = baseline.read_bytes()
    receipt = create_backup(bound.app, tmp_path / 'backup')
    source.unlink()
    bound.source.unlink()
    shutil.rmtree(bound.app.root)
    restored = restore_backup(tmp_path / 'backup', tmp_path / 'restored', receipt)
    app = LibraryApp(restored)
    ProfileStore(app.root / 'state/providers').remove(bound.profile.id)
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    def forbidden(*args, **kwargs):
        pytest.fail('Restored completed formatting attempted a provider or credential call.')
    monkeypatch.setattr(service.gateway, 'request', forbidden)
    monkeypatch.setattr(service.gateway.vault, 'get', forbidden)
    fresh = capture_layout(app, bound.workspace, 'partition-edition')
    result = export_layout(service, fresh, identities, tmp_path / 'restored-output.docx')
    assert result['status'] == 'complete'
    assert (tmp_path / 'restored-output.docx').read_bytes() == expected
    assert app.assets.resolve(fresh.asset_sha256).read_bytes() == raw


def test_ordinary_review_hash_does_not_authorize_changed_layout_partitions(bound):
    layout = capture_layout(bound.app, bound.workspace, bound.edition)
    selected = layout.selections[0]
    value = selected.document.source_text
    partition = make_partition(value, 'carr-docx-body-layout/1', 'a' * 64, [len(value)])
    ordinary = review_documents(bound.service, bound.profile, (selected,), 'en', 'uk', '0')
    calls = list(bound.state['calls'])
    with pytest.raises(PermissionError, match='exact document batch'):
        prepare_documents(bound.service, bound.profile, (selected,), 'en', 'uk', '0',
            approved_review=ordinary['review_sha256'], source_partitions=(partition,))
    assert bound.service.jobs.identities() == () and bound.state['calls'] == calls


def test_generic_pasted_job_cannot_acquire_a_source_partition(ready):
    value = 'First.\n\nSecond.'
    approval, cost = translation_approval(ready.profile, value, 'en', 'uk', '0')
    identity = ready.service.submit(ready.profile, value, 'en', 'uk',
                                   authorization=approval, per_call_cost=cost)
    job = ready.service.jobs.load(identity)
    job['source_partition'] = small_partition(value)
    ready.service.jobs.save(job)
    calls = list(ready.state['calls'])
    with pytest.raises(ValueError, match='legacy job format'):
        ready.service.resume(identity)
    assert ready.state['calls'] == calls


def test_prepare_captures_partition_before_guard_can_mutate_callers_mapping(bound, monkeypatch):
    from carr_library.assistant import translation_documents as documents
    layout = capture_layout(bound.app, bound.workspace, bound.edition)
    selected = layout.selections[0]
    value = selected.document.source_text
    partition = make_partition(value, 'carr-docx-body-layout/1', 'a' * 64, [len(value)])
    captured = deepcopy(partition)
    review = review_documents(bound.service, bound.profile, (selected,), 'en', 'uk', '0',
                              source_partitions=(partition,))
    reviewed = False
    actual = documents.review_documents
    def observe(*args, **kwargs):
        nonlocal reviewed
        result = actual(*args, **kwargs)
        reviewed = True
        return result
    monkeypatch.setattr(documents, 'review_documents', observe)
    def guard():
        if reviewed:
            partition['mapping_sha256'] = 'b' * 64
    calls = list(bound.state['calls'])
    result = prepare_documents(bound.service, bound.profile, (selected,), 'en', 'uk', '0',
        approved_review=review['review_sha256'], source_partitions=(partition,), guard=guard)
    assert reviewed and partition != captured
    assert len(result['jobs']) == 1 and bound.state['calls'] == calls
    assert bound.service.get(result['jobs'][0])['source_partition'] == captured
