"""Completed Book PCM inspection retains the original plan and source authority."""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioRuntime
from carr_library.audio import cleanup, process
from carr_library.durable_state import atomic_json
from carr_library.operations._locks import NativeLock
from carr_library.studio.render import _operation_sha256

_spec = importlib.util.spec_from_file_location(
    'silence_book_fixtures', Path(__file__).with_name('test_book_audio.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
book = _fixtures.book


@pytest.fixture
def completed(book):
    e = book
    _fixtures.require_audio()
    plan = _fixtures.plan(e, 'document')
    _, ready = _fixtures.prepared(e, plan)
    rows = e.service.run(plan.id, e.output, approval=ready.fingerprint)
    assert len(rows) == 1 and rows[0]['status'] == 'complete'
    state = e.production.get(plan.job_id(1))
    budget = AudioBudget(threads=1, timeout=30, max_output_bytes=4 * 1024**2,
                         reserve_disk_bytes=0)
    e.production.runtime_factory = lambda root, **options: AudioRuntime(
        root, budget=budget, **options)
    master = Path(state['destination']) / 'master.wav'
    job_path = e.production.jobs_root / state['id'] / 'job.json'
    plan_path = e.service.plans / (plan.id + '.json')
    return SimpleNamespace(e=e, plan=plan, state=state, budget=budget, master=master,
        job_path=job_path, plan_path=plan_path, speech=tuple(e.voices.received))


def inspect(r, **options):
    return r.e.service.inspect_silence(r.plan.id, 1, threshold_dbfs=-60,
        minimum_seconds=.05, budget=r.budget, **options)


def artifacts(r):
    return {row['path']: (sha256(Path(row['path']).read_bytes()).hexdigest(),
                         Path(row['path']).stat().st_mtime_ns)
            for row in r.state['artifacts']}


def locked(path):
    lock = NativeLock(path)
    acquired = lock.acquire()
    if acquired:
        lock.release()
    return not acquired


def test_completed_book_inspects_actual_master_without_render_or_receipt_changes(completed):
    r = completed
    before = artifacts(r)
    job, plan = r.job_path.read_bytes(), r.plan_path.read_bytes()
    source = r.e.app.assets.resolve(r.e.imported['asset']).read_bytes()
    result = inspect(r)
    with wave.open(str(r.master), 'rb') as pcm:
        assert result['audio']['frames'] == pcm.getnframes()
        assert result['audio']['channels'] == pcm.getnchannels()
        assert result['audio']['sample_rate'] == pcm.getframerate()
    assert result['status'] == 'complete' and result['job_id'] == r.state['id']
    assert result['project_fingerprint'] == r.plan.project(1).fingerprint
    assert result['source']['sha256'] == sha256(r.master.read_bytes()).hexdigest()
    assert result['criteria']['channel_policy'] == 'every-channel'
    assert result['criteria']['minimum_frames'] == 1200
    assert result['interval_count'] == result['omitted_count'] == 0
    assert result['intervals'] == []
    assert tuple(r.e.voices.received) == r.speech
    assert (r.job_path.read_bytes(), r.plan_path.read_bytes()) == (job, plan)
    assert artifacts(r) == before
    assert r.e.app.assets.resolve(r.e.imported['asset']).read_bytes() == source
    assert r.e.production.broker.snapshot()['leases'] == []
    assert not list(r.e.production.cache.glob('.carr-silence-*'))


@pytest.mark.parametrize('change', ['pending', 'restored', 'different-format'])
def test_book_denies_ineligible_saved_job_before_inspector_or_external_output(completed, monkeypatch, change):
    r = completed
    state = dict(r.state)
    if change == 'pending':
        state['status'] = 'pending'
    elif change == 'restored':
        state['restore_requires_new_output'] = True
    else:
        state['format'] = 'mp3'
        state['operation_sha256'] = _operation_sha256(state)
    atomic_json(r.job_path, state, replace=True)
    expected = r.job_path.read_bytes()
    before = artifacts(r)
    calls = []
    def forbidden(*args, **options):
        calls.append(True)
        raise AssertionError('An ineligible Book item reached the PCM inspector.')
    monkeypatch.setattr(r.e.production, 'inspect_silence', forbidden)
    with pytest.raises(ValueError, match='exact Book part|immutable reviewed'):
        inspect(r)
    assert not calls
    assert r.job_path.read_bytes() == expected
    assert artifacts(r) == before and tuple(r.e.voices.received) == r.speech


def test_archived_original_book_source_is_refused_before_pcm_inspection(completed, monkeypatch):
    r = completed
    r.e.app.archive_workspace(r.e.workspace)
    calls = []
    def forbidden(*args, **options):
        calls.append(True)
        raise AssertionError('An archived original source reached the PCM inspector.')
    monkeypatch.setattr(r.e.production, 'inspect_silence', forbidden)
    with pytest.raises(ValueError, match='missing or archived'):
        inspect(r)
    assert not calls and tuple(r.e.voices.received) == r.speech


@pytest.mark.parametrize('change', ['archive-source', 'plan', 'job'])
def test_source_plan_or_job_change_during_actual_measurement_cannot_return_report(completed, change):
    r = completed
    before = artifacts(r)
    job, plan = r.job_path.read_bytes(), r.plan_path.read_bytes()
    changed = []
    def progress(message):
        if 'Measuring intervals at or below the threshold' not in message or changed:
            return
        changed.append(True)
        if change == 'archive-source':
            r.e.app.archive_workspace(r.e.workspace)
        elif change == 'plan':
            updated = replace(r.plan, options=replace(
                r.plan.options, name_template='{index:04d} - changed review'))
            atomic_json(r.plan_path, {'plan': updated.to_dict(),
                'fingerprint': updated.fingerprint}, replace=True)
        else:
            updated = dict(r.state, progress='External completed-job metadata change')
            atomic_json(r.job_path, updated, replace=True)
    with pytest.raises(ValueError, match='missing or archived|plan changed|job changed'):
        inspect(r, progress=progress)
    assert changed
    assert artifacts(r) == before and tuple(r.e.voices.received) == r.speech
    if change != 'job':
        assert r.job_path.read_bytes() == job
    else:
        assert r.e.production.get(r.state['id'])['progress'] == 'External completed-job metadata change'
    if change != 'plan':
        assert r.plan_path.read_bytes() == plan
    else:
        assert r.e.service.load(r.plan.id).fingerprint != r.plan.fingerprint
    assert r.e.production.broker.snapshot()['leases'] == []
    assert not list(r.e.production.cache.glob('.carr-silence-*'))


@pytest.mark.parametrize('stop', ['cancel', 'close'])
def test_book_cancel_or_close_during_measurement_keeps_completed_job_unchanged(completed, stop):
    r = completed
    token = threading.Event()
    job = r.job_path.read_bytes()
    before = artifacts(r)
    phases = []
    def progress(message):
        if 'Measuring intervals at or below the threshold' in message:
            phases.append(message)
            if stop == 'cancel':
                token.set()
            else:
                r.e.service.close()
    with pytest.raises((AudioCancelled, InterruptedError)):
        inspect(r, cancelled=token.is_set, progress=progress)
    assert phases and r.job_path.read_bytes() == job
    assert artifacts(r) == before and tuple(r.e.voices.received) == r.speech
    assert r.e.production.broker.snapshot()['leases'] == []
    assert not list(r.e.production.cache.glob('.carr-silence-*'))


def test_book_inspection_uses_the_existing_plan_lock(completed):
    r = completed
    own = NativeLock(r.e.service.plans / ('.book-' + r.plan.id + '.lock'))
    assert own.acquire()
    try:
        with pytest.raises(RuntimeError, match='already being changed'):
            inspect(r)
    finally:
        own.release()
    assert tuple(r.e.voices.received) == r.speech
    assert not r.e.production.broker.snapshot()['leases']


@pytest.mark.parametrize('masked', [False, True], ids=['direct-owner', 'masked-owner'])
def test_book_plan_lock_follows_the_actual_pcm_probe_cleanup_owner(completed, monkeypatch, masked):
    r = completed
    allowed, owners, children = threading.Event(), [], []
    actual_stop = process._stop_owned
    original_factory = r.e.production.runtime_factory
    before = artifacts(r)
    job = r.job_path.read_bytes()

    def manual(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)

    def delayed(child):
        assert actual_stop(child) is True
        args = tuple(map(str, child.args))
        if not children and '-show_entries' in args and Path(args[-1]) == r.master:
            children.append(child)
        if children and child is children[0] and not allowed.is_set():
            raise TimeoutError('Modeled missing final acknowledgement after actual PCM probe exit.')
        return True

    def factory(root, **options):
        runtime = original_factory(root, **options)
        actual_inspect = runtime.inspect_silence
        def measured(*args, **kwargs):
            try:
                return actual_inspect(*args, **kwargs)
            except cleanup.AudioCleanupPending as error:
                if masked:
                    raise OSError('Modeled observation failure retaining its native cause.') from error
                raise
        runtime.inspect_silence = measured
        return runtime

    monkeypatch.setattr(cleanup, 'pending_cleanup', manual)
    monkeypatch.setattr(process, '_stop_owned', delayed)
    monkeypatch.setattr(r.e.production, 'runtime_factory', factory)
    try:
        with pytest.raises(OSError if masked else cleanup.AudioCleanupPending) as caught:
            inspect(r)
        assert children and children[0].poll() == 0
        assert set(cleanup._pending_owners(caught.value)) == set(owners)
        assert owners and all(not owner.close() for owner in owners)
        assert locked(r.e.service.plans / ('.book-' + r.plan.id + '.lock'))
        assert locked(r.e.production.jobs_root / r.state['id'] / '.render.lock')
        assert len(r.e.production.broker.snapshot()['leases']) == 1
        assert r.job_path.read_bytes() == job and artifacts(r) == before
        allowed.set()
        assert all(owner.close() for owner in owners)
        assert not locked(r.e.service.plans / ('.book-' + r.plan.id + '.lock'))
        assert not locked(r.e.production.jobs_root / r.state['id'] / '.render.lock')
        assert not r.e.production.broker.snapshot()['leases']
        assert tuple(r.e.voices.received) == r.speech
    finally:
        allowed.set()
        for owner in owners:
            assert owner.close()
        monkeypatch.setattr(process, '_stop_owned', actual_stop)


@pytest.mark.parametrize('index', [0, 2, True])
def test_book_inspection_requires_an_exact_saved_part_index(completed, index):
    r = completed
    with pytest.raises(ValueError, match='Choose a part'):
        r.e.service.inspect_silence(r.plan.id, index, threshold_dbfs=-60,
            minimum_seconds=.05, budget=r.budget)
    assert not r.e.production.broker.snapshot()['leases']
    assert tuple(r.e.voices.received) == r.speech
