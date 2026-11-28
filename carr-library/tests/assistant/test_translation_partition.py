"""Exact layout partitions use the existing retained-source approval and receipts.

The current synthetic Google transport is reused; no paid API or model quality
is claimed. All original selected-text and translation/1 records remain valid.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from carr_library.assistant.library_translation import LibrarySelection, capture_library_sources
from carr_library.assistant.models import canonical
from carr_library.assistant.translation import (
    TranslationService, translation_approval, job_chunks, split_text)
from carr_library.assistant.translation_partition import (
    make_partition, partition_ranges, partition_chunk_groups, partition_chunks)

_spec = importlib.util.spec_from_file_location('partition_current_gateway',
    Path(__file__).with_name('test_library_translation.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
setup = _base.setup


def small_partition():
    return make_partition('First.\n\nSecond.', 'carr-docx-body-layout/1', 'a' * 64, [6, 8, 15])


def source(case):
    return capture_library_sources(case.app, case.workspace, (LibrarySelection(case.book['edition']),))


def partitioned(case):
    captured = source(case)
    positions = captured.binding['sources'][0]['anchor']['positions']
    boundaries = sorted({point for row in positions for point in (row['start'], row['end'])
                         if point > 0})
    partition = make_partition(captured.text, 'carr-docx-body-layout/1', 'a' * 64, boundaries)
    approval, cost = translation_approval(case.profile, captured.text, 'en', 'uk', '1',
        source_binding=captured.binding, source_partition=partition)
    identity = case.service.submit(case.profile, captured.text, 'en', 'uk',
        source_binding=captured.binding, source_partition=partition,
        authorization=approval, per_call_cost=cost)
    return captured, partition, identity


@pytest.mark.parametrize('change', [
    {'boundaries': []}, {'boundaries': [0, 15]}, {'boundaries': [6, 6, 15]},
    {'boundaries': [8, 6, 15]}, {'boundaries': [6, 8, 14]},
    {'boundaries': [6, 8, 16]}, {'boundaries': [True, 8, 15]},
    {'boundaries': [6.0, 8, 15]}, {'boundaries': (6, 8, 15)},
    {'profile': 'unreviewed-layout'}, {'profile': []},
    {'mapping_sha256': 'A' * 64}, {'mapping_sha256': 'a' * 63},
    {'schema': 'carr-translation-partition/2'}, {'extra': 'unexpected'},
])
def test_partition_rejects_ambiguous_or_incomplete_coverage(change):
    partition = small_partition()
    partition.update(change)
    with pytest.raises(ValueError):
        partition_chunks('First.\n\nSecond.', partition)


def test_partition_preserves_every_code_point_and_whitespace_separator():
    value = 'First.\n\nSecond.'
    partition = small_partition()
    assert partition_ranges(value, partition) == ((0, 6), (6, 8), (8, 15))
    assert partition_chunk_groups(value, partition) == (('First.',), ('\n\n',), ('Second.',))
    assert ''.join(partition_chunks(value, partition)) == value
    with pytest.raises(ValueError):
        make_partition('x' * 4097, 'carr-docx-body-layout/1', 'a' * 64, range(1, 4098))


@pytest.mark.parametrize('method', ['approval', 'preview', 'submit'])
def test_layout_partition_cannot_authorize_unbound_pasted_text(setup, method):
    case = setup
    value, partition = 'First.\n\nSecond.', small_partition()
    calls, vault = list(case.calls), list(case.vault_calls)
    with pytest.raises(ValueError, match='retained library source'):
        if method == 'approval':
            translation_approval(case.profile, value, 'en', 'uk', '1', source_partition=partition)
        elif method == 'preview':
            case.service.preview(case.profile, value, 'en', 'uk', source_partition=partition)
        else:
            approval, cost = translation_approval(case.profile, value, 'en', 'uk', '1')
            case.service.submit(case.profile, value, 'en', 'uk', authorization=approval,
                                per_call_cost=cost, source_partition=partition)
    assert case.calls == calls and case.vault_calls == vault
    assert case.service.jobs.identities() == ()


def test_absent_partition_retains_original_source_uuid_chunks_and_approval(setup):
    case, captured = setup, source(setup)
    approval, cost = translation_approval(case.profile, captured.text, 'en', 'uk', '1',
                                          source_binding=captured.binding)
    identity = case.service.submit(case.profile, captured.text, 'en', 'uk',
        source_binding=captured.binding, authorization=approval, per_call_cost=cost)
    job = case.service.get(identity)
    payload = {key: job[key] for key in (
        'source_binding', 'source_sha256', 'authorization', 'execution_origin')}
    payload['profile_sha256'] = sha256(canonical(job['profile']).encode('utf-8')).hexdigest()
    assert identity == uuid5(NAMESPACE_URL,
        'urn:carr-library:translation-source/1:' + canonical(payload)).hex
    assert 'source_partition' not in job
    assert job['chunks'] == list(split_text(captured.text)) == list(job_chunks(job))
    assert approval.task_id == approval.id + ':source:' + captured.binding['sha256']
    assert case.service.preview(case.profile, captured.text, 'en', 'uk',
        source_binding=captured.binding)['preservation'] == 'plain text derivative; document formatting is not retained'


def test_partition_reuses_one_job_and_retains_accepted_receipts_across_pause(setup):
    case = setup
    captured, partition, identity = partitioned(case)
    job = case.service.get(identity)
    assert job['kind'] == 'translation/1' and job['source_partition'] == partition
    assert ''.join(job['chunks']) == captured.text
    assert len(job['chunks']) == 5 and sum(bool(chunk.strip()) for chunk in job['chunks']) == 3
    before = len(case.calls)
    stop = []
    paused = case.service.resume(identity, cancelled=lambda: bool(stop),
        progress=lambda done, total, phase: stop.append(done))
    assert paused['status'] == 'paused' and paused['completed'] == 1
    accepted = deepcopy(case.service.get(identity)['receipts'])
    assert len(case.calls) == before + 1
    complete = case.service.resume(identity)
    assert complete['status'] == 'complete' and complete['completed'] == 5
    current = case.service.get(identity)
    assert current['receipts'][:1] == accepted
    assert [row['text'] for chunk, row in zip(current['chunks'], current['receipts'])
            if not chunk.strip()] == ['\n\n', '\n\n']
    assert len(case.calls) == before + 3
    assert case.service.resume(identity) == complete and len(case.calls) == before + 3
    assert case.app.assets.resolve(case.book['asset']).read_bytes() == case.original.encode('utf-8')


@pytest.mark.parametrize('damage', ['mapping', 'boundaries', 'null', 'removed'])
def test_changed_saved_partition_is_refused_before_credentials_or_next_request(setup, damage):
    case = setup
    _, _, identity = partitioned(case)
    job = case.service.jobs.load(identity)
    if damage == 'mapping':
        job['source_partition']['mapping_sha256'] = 'b' * 64
    elif damage == 'boundaries':
        job['source_partition']['boundaries'] = [len(job['source_text'])]
    elif damage == 'null':
        job['source_partition'] = None
    else:
        job.pop('source_partition')
    case.service.jobs.save(job)
    calls, vault = list(case.calls), list(case.vault_calls)
    with pytest.raises((ValueError, PermissionError)):
        case.service.resume(identity)
    assert case.calls == calls and case.vault_calls == vault
    assert case.service.jobs.load(identity) == job


def test_plain_authority_does_not_authorize_same_text_with_a_new_layout(setup):
    case, captured = setup, source(setup)
    partition = make_partition(captured.text, 'carr-docx-body-layout/1', 'a' * 64, [len(captured.text)])
    approval, cost = translation_approval(case.profile, captured.text, 'en', 'uk', '1',
                                          source_binding=captured.binding)
    calls = list(case.calls)
    with pytest.raises(PermissionError):
        case.service.submit(case.profile, captured.text, 'en', 'uk',
            source_binding=captured.binding, source_partition=partition,
            authorization=approval, per_call_cost=cost)
    assert not case.service.jobs.identities() and case.calls == calls


def test_partition_is_detached_before_a_live_guard_mutates_the_caller_mapping(setup):
    case, captured = setup, source(setup)
    partition = make_partition(captured.text, 'carr-docx-body-layout/1', 'a' * 64, [len(captured.text)])
    expected = deepcopy(partition)
    approval, cost = translation_approval(case.profile, captured.text, 'en', 'uk', '1',
        source_binding=captured.binding, source_partition=partition)
    touched = []
    def guard():
        touched.append(True)
        partition['mapping_sha256'] = 'b' * 64
    case.service.scope_guard = guard
    identity = case.service.submit(case.profile, captured.text, 'en', 'uk',
        source_binding=captured.binding, source_partition=partition,
        authorization=approval, per_call_cost=cost)
    case.service.scope_guard = lambda: None
    assert touched and partition != expected
    assert case.service.get(identity)['source_partition'] == expected
    assert len(case.calls) == 1


def test_plain_job_cannot_acquire_formatted_export_authority_by_adding_a_field(setup):
    case = setup
    value = 'First.\n\nSecond.'
    approval, cost = translation_approval(case.profile, value, 'en', 'uk', '1')
    identity = case.service.submit(case.profile, value, 'en', 'uk',
                                  authorization=approval, per_call_cost=cost)
    job = case.service.jobs.load(identity)
    job['source_partition'] = make_partition(value, 'carr-docx-body-layout/1', 'a' * 64, [len(value)])
    case.service.jobs.save(job)
    calls = list(case.calls)
    with pytest.raises(ValueError, match='legacy unbound'):
        case.service.resume(identity)
    assert case.calls == calls
