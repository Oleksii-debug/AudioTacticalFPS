"""Existing Book/Studio operations through one approved assistant checkpoint.

Real PCM uses the existing synthetic-tone fixture plus installed FFmpeg. These
tests establish source, output, consent, resume and ownership contracts only.
"""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path
import shutil
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import book_tools
from carr_library.audio import AudioRuntime
from carr_library.audio.cleanup import AudioCleanupPending, _pending_owners
from carr_library.durable_state import read_json
from carr_library.library_backup import restore_backup
from carr_library.operations._locks import NativeLock
from carr_library.studio.book_audio import BookAudioService
from carr_library.studio.render import ProductionService

# File-bound test helpers retain installed carr_library imports under importlib
# mode. They never add the checkout's src directory to sys.path.
_spec = importlib.util.spec_from_file_location(
    'assistant_book_audio_fixture', Path(__file__).parents[1] / 'studio' / 'test_book_audio.py')
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
book = _fixture.book
_batch_spec = importlib.util.spec_from_file_location(
    'assistant_book_batch_fixture', Path(__file__).parents[1] / 'studio' / 'test_book_batch.py')
_batch_fixture = importlib.util.module_from_spec(_batch_spec)
_batch_spec.loader.exec_module(_batch_fixture)
batch = _batch_fixture.batch


def selected(e, **changes):
    candidate = _fixture.plan(e)
    e.service.save(candidate)
    scope = Scope(e.workspace, (), e.output.parent, max_files=8,
                  max_seconds=120, max_bytes=64 * 1024**2)
    if changes:
        scope = replace(scope, **changes)
    service = AssistantService(e.app, production=e.production.assistant_callback)
    args = {'plan': candidate.id, 'plan_sha256': candidate.fingerprint, 'name': e.output.name}
    return candidate, scope, service, args


def invoke(service, scope, *steps, **kwargs):
    identity = service.submit(Plan(scope, steps))
    return identity, service.run(identity, **kwargs)


def prepare_selected(e, **changes):
    candidate, scope, service, args = selected(e, **changes)
    _, review = invoke(service, scope, Step('book.review', args))
    assert review.status == 'complete', review
    initial = review.results[0]
    _, report = invoke(service, scope, Step('book.prepare',
        {**args, 'review_sha256': initial['review_sha256']}))
    assert report.status == 'complete', report
    return candidate, scope, service, args, report.results[0]


def artifacts(e, candidate):
    result = {}
    for index in range(1, len(candidate.parts) + 1):
        state = e.production.get(candidate.job_id(index))
        for row in state['artifacts']:
            path = Path(row['path'])
            result[str(path)] = (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return result


def test_review_and_prepare_use_saved_source_and_existing_jobs_without_voice_or_media(book, monkeypatch):
    e = book
    candidate, scope, service, args = selected(e)
    def forbidden(*args, **kwargs):
        pytest.fail('Review/Prepare must not discover voices or start a media runtime.')
    monkeypatch.setattr(e.production, '_runtime', forbidden)
    monkeypatch.setattr(e.voices, 'voices', forbidden)
    proposed = service.preview(Plan(scope, (Step('book.review', args),)).to_json(), scope)
    assert proposed.steps[0].tool == 'book.review'
    assert not e.production.jobs()
    review_id, report = invoke(service, scope, Step('book.review', args))
    reviewed, = report.results
    assert report.status == 'complete' and reviewed['prepared'] is False
    assert reviewed['selection_sha256'] == candidate.fingerprint
    assert not e.production.jobs() and e.voices.received == []
    identity, prepared = invoke(service, scope, Step('book.prepare',
        {**args, 'review_sha256': reviewed['review_sha256']}))
    ready, = prepared.results
    assert prepared.status == 'complete' and ready['prepared'] is True
    assert ready['review_sha256'] != reviewed['review_sha256']
    assert len(e.production.jobs()) == len(candidate.parts) == 2
    assert e.voices.received == [] and not tuple(e.output.iterdir())
    checkpoint = service.store.load(identity)['steps'][0]
    assert checkpoint['intent']['book_completed'] is True
    assert checkpoint['intent']['book_operation']['review_sha256'] == reviewed['review_sha256']
    assert service.resume(identity).results == prepared.results
    # Historical review result is no longer current once preparation advances.
    assert service.resume(review_id).status == 'attention'


def test_real_book_render_and_completed_reuse_share_original_pcm_receipts(book):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    original = e.app.assets.resolve(e.imported['asset']).read_bytes()
    identity, report = invoke(service, scope, Step('book.run',
        {**args, 'review_sha256': ready['review_sha256']}))
    assert report.status == 'complete', report
    result, = report.results
    assert result['status'] == 'complete' and len(result['jobs']) == 2
    assert e.voices.received == [candidate.project(i).original_script for i in (1, 2)]
    assert len(e.production.jobs()) == 2 and result['artifacts']
    for index, job in enumerate(result['jobs'], 1):
        assert job['job'] == candidate.job_id(index)
        state = e.production.get(job['job'])
        assert state['status'] == 'complete'
        assert read_json(Path(job['destination']) / 'book-source.json') == candidate.provenance(index)
    accepted = artifacts(e, candidate)
    reopened = AssistantService(LibraryApp(e.app.root), production=e.production.assistant_callback)
    assert reopened.resume(identity).results == report.results
    assert artifacts(e, candidate) == accepted and len(e.voices.received) == 2
    assert e.app.assets.resolve(e.imported['asset']).read_bytes() == original
    assert not e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('kind', ['plan-hash', 'source-revision', 'source-bytes', 'output-parent'])
def test_book_approval_never_adopts_changed_saved_source_or_output(book, kind):
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    step = Step('book.run', {**args, 'review_sha256': ready['review_sha256']})
    identity = service.submit(Plan(scope, (step,)))
    if kind == 'plan-hash':
        state = service.store.load(identity)
        state['plan']['steps'][0]['arguments']['plan_sha256'] = '0' * 64
        service.store.save(state)  # The original approved digest must also reject this.
        with pytest.raises(ValueError, match='approved assistant plan'):
            service.run(identity)
    else:
        if kind == 'source-revision':
            e.app.update_metadata(e.workspace, e.imported['edition'], title='Changed source revision')
        elif kind == 'source-bytes':
            e.app.assets.resolve(e.imported['asset']).write_bytes(b'Unapproved replacement')
        else:
            other = e.output.parent / 'different-output'
            other.mkdir()
            identity = service.submit(Plan(scope, (Step('book.run',
                {**args, 'name': other.name, 'review_sha256': ready['review_sha256']}),)))
        report = service.run(identity)
        assert report.status in ('failed', 'attention') and report.completed == 0
    assert e.voices.received == []
    assert all(row['status'] == 'pending' for row in e.production.jobs())
    assert not tuple(e.output.iterdir())


def test_run_requires_the_new_prepared_review_and_never_prepares_implicitly(book):
    e = book
    candidate, scope, service, args = selected(e)
    _, before = invoke(service, scope, Step('book.review', args))
    old = before.results[0]['review_sha256']
    _, absent = invoke(service, scope, Step('book.run', {**args, 'review_sha256': old}))
    assert absent.status == 'attention' and absent.completed == 0
    assert not e.production.jobs()
    _, prepared = invoke(service, scope, Step('book.prepare', {**args, 'review_sha256': old}))
    assert prepared.status == 'complete'
    _, stale = invoke(service, scope, Step('book.run', {**args, 'review_sha256': old}))
    assert stale.status == 'attention' and stale.completed == 0
    assert len(e.production.jobs()) == 2 and e.voices.received == []
    assert not tuple(e.output.iterdir())


def test_exact_book_intent_is_durable_before_first_job_preparation(book, monkeypatch):
    e = book
    candidate, scope, service, args = selected(e)
    _, report = invoke(service, scope, Step('book.review', args))
    step = Step('book.prepare', {**args, 'review_sha256': report.results[0]['review_sha256']})
    identity = service.submit(Plan(scope, (step,)))
    real = ProductionService.submit
    observed = []
    def submit(production, *values, **kwargs):
        record = service.store.load(identity)
        intent = record['steps'][0]['intent']['book_operation']
        assert intent['tool'] == step.tool and intent['review_sha256'] == step.arguments['review_sha256']
        assert intent['review']['plan_sha256'] == candidate.fingerprint
        assert record['steps'][0]['status'] == 'running'
        observed.append(kwargs['operation_id'])
        return real(production, *values, **kwargs)
    monkeypatch.setattr(ProductionService, 'submit', submit)
    result = service.run(identity)
    assert result.status == 'complete' and observed == [candidate.job_id(1), candidate.job_id(2)]
    assert e.voices.received == []


def test_failed_initial_intent_publication_cannot_prepare_any_book_job(book, monkeypatch):
    e = book
    candidate, scope, service, args = selected(e)
    _, reviewed = invoke(service, scope, Step('book.review', args))
    identity = service.submit(Plan(scope, (Step('book.prepare',
        {**args, 'review_sha256': reviewed.results[0]['review_sha256']}),)))
    real = service.store.save
    attempts = []
    def save(record):
        if record['steps'][0]['intent'].get('book_operation') and not attempts:
            attempts.append(True)
            raise OSError('synthetic failure before publishing first exact intent')
        return real(record)
    monkeypatch.setattr(service.store, 'save', save)
    result = service.run(identity)
    assert result.status == 'failed' and attempts == [True]
    assert not e.production.jobs() and not tuple(e.output.iterdir()) and not e.voices.received


def test_interrupted_prepare_recovers_only_its_existing_deterministic_jobs(book, monkeypatch):
    e = book
    candidate, scope, service, args = selected(e)
    _, reviewed = invoke(service, scope, Step('book.review', args))
    identity = service.submit(Plan(scope, (Step('book.prepare',
        {**args, 'review_sha256': reviewed.results[0]['review_sha256']}),)))
    real, accepted = ProductionService.submit, []
    def stop_after_one(production, *values, **kwargs):
        value = real(production, *values, **kwargs)
        accepted.append(value)
        if len(accepted) == 1:
            raise OSError('synthetic acknowledgement failure after the first durable job')
        return value
    monkeypatch.setattr(ProductionService, 'submit', stop_after_one)
    stopped = service.run(identity)
    assert stopped.status == 'failed' and accepted == [candidate.job_id(1)]
    first = (e.production.jobs_root / candidate.job_id(1) / 'job.json').read_bytes()
    monkeypatch.setattr(ProductionService, 'submit', real)
    done = service.resume(identity)
    assert done.status == 'complete' and done.results[0]['prepared'] is True
    assert (e.production.jobs_root / candidate.job_id(1) / 'job.json').read_bytes() == first
    assert {row['id'] for row in e.production.jobs()} == {candidate.job_id(1), candidate.job_id(2)}
    assert e.voices.received == [] and not tuple(e.output.iterdir())


def test_late_cancel_keeps_completed_book_receipts_and_stops_later_assistant_steps(book, monkeypatch):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    stop = threading.Event()
    actual = BookAudioService.run
    def completed_then_stop(owner, *values, **kwargs):
        value = actual(owner, *values, **kwargs)
        assert all(row['status'] == 'complete' for row in value)
        stop.set()
        return value
    monkeypatch.setattr(BookAudioService, 'run', completed_then_stop)
    identity, report = invoke(service, scope,
        Step('book.run', {**args, 'review_sha256': ready['review_sha256']}),
        Step('audio.effects'), cancelled=stop.is_set)
    assert report.status == 'paused' and report.completed == 1
    assert report.results[0]['status'] == 'complete' and len(e.voices.received) == 2
    assert service.store.load(identity)['steps'][1]['status'] == 'pending'
    before = artifacts(e, candidate)
    stop.clear()
    monkeypatch.setattr(BookAudioService, 'run', actual)
    assert service.resume(identity).status == 'complete'
    assert before == artifacts(e, candidate) and len(e.voices.received) == 2


def test_changed_completed_book_bytes_fail_without_a_second_render(book):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    identity, report = invoke(service, scope, Step('book.run',
        {**args, 'review_sha256': ready['review_sha256']}))
    assert report.status == 'complete'
    master = Path(report.results[0]['jobs'][0]['destination']) / 'master.wav'
    master.write_bytes(b'Externally replaced original master')
    result = service.resume(identity)
    assert result.status in ('failed', 'attention') and len(e.voices.received) == 2
    assert master.read_bytes() == b'Externally replaced original master'


def test_real_book_work_uses_the_approved_runtime_budget_and_shared_broker(book, monkeypatch):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args, ready = prepare_selected(e, max_bytes=16 * 1024**2, max_seconds=90)
    actual, observed = AudioRuntime.inspect, []
    def inspect(runtime, *values, **kwargs):
        assert runtime.budget.max_output_bytes <= scope.max_bytes
        assert runtime.budget.timeout <= scope.max_seconds
        leases = e.production.broker.snapshot()['leases']
        assert len(leases) == 1
        observed.append(runtime.budget)
        return actual(runtime, *values, **kwargs)
    monkeypatch.setattr(AudioRuntime, 'inspect', inspect)
    _, report = invoke(service, scope, Step('book.run',
        {**args, 'review_sha256': ready['review_sha256']}))
    assert report.status == 'complete', report
    assert observed and not e.production.broker.snapshot()['leases']
    assert sum(row['size'] for row in report.results[0]['artifacts']) <= scope.max_bytes


def test_book_part_budget_refuses_work_before_preparation_or_voice_discovery(book):
    e = book
    candidate, scope, service, args = selected(e, max_files=1)
    _, result = invoke(service, scope, Step('book.review', args))
    assert result.status == 'attention' and result.completed == 0
    assert not e.production.jobs() and not e.voices.received


def test_real_ordered_book_batch_routes_use_the_existing_prepared_selection(batch):
    e = batch
    _, ready = _batch_fixture.prepare(e)
    scope = Scope(e.workspace, (), e.outputs[0].parent, max_files=3,
                  max_bytes=64 * 1024**2, max_seconds=180)
    service = AssistantService(e.app, production=e.production.assistant_callback)
    args = {'batch': e.selected.id, 'batch_sha256': e.selected.fingerprint,
            'names': [path.name for path in e.outputs]}
    _, reviewed = invoke(service, scope, Step('book.batch.review', args))
    assert reviewed.status == 'complete' and reviewed.results[0]['review_sha256'] == ready.fingerprint
    _, prepared = invoke(service, scope, Step('book.batch.prepare',
        {**args, 'review_sha256': ready.fingerprint}))
    assert prepared.status == 'complete' and not e.voices.calls
    identity, result = invoke(service, scope, Step('book.batch.run',
        {**args, 'review_sha256': prepared.results[0]['review_sha256']}))
    assert result.status == 'complete', result
    jobs = result.results[0]['jobs']
    assert [(row['plan'], row['index']) for row in jobs] == [
        (e.plans[0].id, 1), (e.plans[0].id, 2), (e.plans[1].id, 1)]
    assert [row[1] for row in e.voices.calls] == ['synthetic-one', 'synthetic-one', 'synthetic-two']
    assert len(e.production.jobs()) == 3
    assert service.resume(identity).results == result.results and len(e.voices.calls) == 3
    assert not e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('masked', [False, True], ids=['direct-owner', 'journal-context'])
def test_assistant_book_pending_owner_retains_same_job_plan_and_render_locks(book, monkeypatch, masked):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    identity = service.submit(Plan(scope, (Step('book.run',
        {**args, 'review_sha256': ready['review_sha256']}),)))
    real = ProductionService._save
    if masked:
        def save(owner, state):
            if state['status'] == 'failed':
                raise OSError('synthetic status save masks the pending owner')
            return real(owner, state)
        monkeypatch.setattr(ProductionService, '_save', save)
    with _fixture._uncertain_book_probe(e, monkeypatch) as (owners, release, observed):
        with pytest.raises((AudioCleanupPending, OSError)) as caught:
            service.run(identity)
        assert observed and observed[0].poll() == 0 and _pending_owners(caught.value)
        assert set(_pending_owners(caught.value)) == set(owners)
        assert len(e.production.broker.snapshot()['leases']) == 1
        staged = Path(observed[0].args[-1])
        assert staged.is_file() and not Path(ready['review']['destinations'][1]).exists()
        first = e.production.get(candidate.job_id(1))
        assert first['status'] == 'complete'
        saved = {row['path']: sha256(Path(row['path']).read_bytes()).hexdigest()
                 for row in first['artifacts']}
        locks = (NativeLock(e.service.plans / ('.book-' + candidate.id + '.lock')),
                 NativeLock(e.production.jobs_root / candidate.job_id(2) / '.render.lock'))
        assert all(lock.acquire() is False for lock in locks)
        with pytest.raises(RuntimeError, match='already running'):
            service.resume(identity)
        release.set()
        assert all(owner.close() for owner in owners)
        assert not e.production.broker.snapshot()['leases']
        for lock in locks:
            assert lock.acquire()
            lock.release()
        monkeypatch.setattr(ProductionService, '_save', real)
        result = service.resume(identity)
        assert result.status == 'complete', result
        assert saved == {path: sha256(Path(path).read_bytes()).hexdigest() for path in saved}
        assert len(e.voices.received) == 3 and not e.production.broker.snapshot()['leases']


def test_real_backup_keeps_book_assistant_reviews_but_revokes_execution(book, tmp_path, monkeypatch):
    e = book
    candidate, scope, service, args, ready = prepare_selected(e)
    run_id = service.submit(Plan(scope, (Step('book.run',
        {**args, 'review_sha256': ready['review_sha256']}),)))
    before = service.store.path(run_id).read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Passive backup/restore must not discover engines or render Book audio.')
    monkeypatch.setattr(ProductionService, '_runtime', forbidden)
    monkeypatch.setattr(e.voices, 'voices', forbidden)
    directory = tmp_path / 'book-backup'
    checksum = e.app.backup(directory)
    restored = LibraryApp(restore_backup(directory, tmp_path / 'restored-book', checksum))
    reader = AssistantService(restored)
    record, inert = reader.review_saved_plan(run_id)
    assert record['restore_requires_review'] is True
    assert record['plan']['steps'][0]['arguments'] == {**args, 'review_sha256': ready['review_sha256']}
    assert inert.scope.output_root is None and inert.scope.input_roots == ()
    with pytest.raises(PermissionError, match='Restored assistant jobs'):
        reader.resume(run_id)
    assert service.store.path(run_id).read_bytes() == before
    assert not e.voices.received and not tuple(e.output.iterdir())
    for index in (1, 2):
        state = read_json(restored.root / 'state' / 'studio' / 'jobs' / candidate.job_id(index) / 'job.json')
        assert state['restore_requires_new_output'] is True
        assert state['destination'] == ready['review']['destinations'][index - 1]


@pytest.mark.parametrize('changes', [
    {'plan': '../escape'}, {'plan_sha256': 'A' * 64}, {'review_sha256': '0' * 63},
    {'name': '../unapproved'}, {'name': r'C:\foreign'}, {'name': '.'},
    {'execute': True},
])
def test_book_route_vocabulary_is_pure_and_bounded(book, changes, monkeypatch):
    from carr_library.assistant.service import _check_arguments
    e = book
    candidate, scope, service, args = selected(e)
    def forbidden(*args, **kwargs):
        pytest.fail('Passive typed argument admission cannot inspect a saved source or runtime.')
    monkeypatch.setattr(book_tools, '_production', forbidden)
    with pytest.raises(ValueError):
        _check_arguments(Step('book.run', {**args, 'review_sha256': '0' * 64, **changes}))


def test_passive_book_preparation_never_grants_missing_voice_execution(book, monkeypatch):
    _fixture.require_audio()
    e = book
    candidate, scope, service, args = selected(e)
    monkeypatch.setattr(e.voices, 'voices', lambda: ())

    # Ordinary Studio submission keeps its existing live voice check.
    with pytest.raises(ValueError, match='voice is no longer available'):
        e.production.submit(candidate.project(1), e.output, stems=False)
    assert not e.production.jobs()

    _, reviewed = invoke(service, scope, Step('book.review', args))
    assert reviewed.status == 'complete', reviewed
    _, prepared = invoke(service, scope, Step('book.prepare',
        {**args, 'review_sha256': reviewed.results[0]['review_sha256']}))
    assert prepared.status == 'complete', prepared
    assert len(e.production.jobs()) == len(candidate.parts) == 2

    # Explicit Run must validate the real current inventory even for a saved,
    # source-bound prepared plan. Its resource lease is not output permission.
    _, result = invoke(service, scope, Step('book.run',
        {**args, 'review_sha256': prepared.results[0]['review_sha256']}))
    assert result.status == 'failed' and result.completed == 0
    assert 'voice is no longer available' in result.message
    first, later = (e.production.get(candidate.job_id(index)) for index in (1, 2))
    assert first['status'] == 'failed' and later['status'] == 'pending'
    assert first['fingerprint'] == candidate.project(1).fingerprint
    assert first['nodes'] == {} and first['artifacts'] == []
    assert e.voices.received == [] and not tuple(e.output.iterdir())
    assert not e.production.broker.snapshot()['leases']
