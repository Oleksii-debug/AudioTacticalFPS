"""Primary Studio masters use the existing publication snapshot and native owner."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import threading
import wave

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioRuntime
from carr_library.audio import cleanup, process, silence
from carr_library.audio.projects import atomic_json
from carr_library.operations import ResourceBroker
from carr_library.operations._locks import NativeLock
from carr_library.studio.publishing_source import DirectorPublicationSource, LibraryPublicationService

_spec = importlib.util.spec_from_file_location(
    'silence_primary_fixtures', Path(__file__).with_name('test_publishing_primary.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
primary_completed = _fixtures.primary_completed
primary = _fixtures.primary


@pytest.fixture
def completed(primary):
    source, state, project = primary
    budget = AudioBudget(threads=1, timeout=30, max_output_bytes=16 * 1024**2,
                         reserve_disk_bytes=0)
    source.runtime_factory = lambda root, **options: AudioRuntime(
        root, budget=budget, **options)
    return source, state, project, budget


def inspect(value, **options):
    source, state, _, budget = value
    return source.inspect_silence(state['id'], threshold_dbfs=-60,
        minimum_seconds=.1, budget=budget, **options)


def retained_files(value):
    source, state, _, _ = value
    return {str(path): (sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for root in (source.folder / 'cache', Path(state['output_dir']))
            for path in root.rglob('*') if path.is_file()}


def locked(source):
    lock = NativeLock(source.folder / '.render.lock')
    acquired = lock.acquire()
    if acquired:
        lock.release()
    return not acquired


def test_primary_completed_master_uses_same_snapshot_and_lease_without_rendering(completed, monkeypatch):
    source, state, project, budget = completed
    job_file = source.folder / 'jobs' / (state['id'] + '.json')
    original, files = job_file.read_bytes(), retained_files(completed)
    snapshots, runtimes = [], []
    snapshot = source.publication_snapshot
    factory = source.runtime_factory
    broker = ResourceBroker.for_app(source.private_root)

    def verified(*args, **kwargs):
        assert locked(source)
        rows = broker.snapshot()['leases']
        assert len(rows) == 1 and rows[0]['request']['cpu_slots'] == budget.threads
        assert Path(rows[0]['request']['disk_path']).samefile(source.folder / 'cache')
        snapshots.append(True)
        return snapshot(*args, **kwargs)

    def admitted(*args, **kwargs):
        assert locked(source) and len(broker.snapshot()['leases']) == 1
        runtime = factory(*args, **kwargs)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(source, 'publication_snapshot', verified)
    monkeypatch.setattr(source, 'runtime_factory', admitted)
    monkeypatch.setattr(_fixtures.ProductionDirector, 'render',
        lambda *a, **k: pytest.fail('Inspection must not invoke the primary renderer.'))
    report = inspect(completed, broker=broker)
    master = Path(state['output_dir']) / 'master.wav'
    with wave.open(str(master), 'rb') as pcm:
        assert report['audio']['frames'] == pcm.getnframes()
        assert report['audio']['sample_rate'] == pcm.getframerate()
        assert report['audio']['channels'] == pcm.getnchannels()
    assert report['status'] == 'complete' and report['source_family'] == 'director'
    assert report['job_id'] == state['id'] and report['project_fingerprint'] == project.fingerprint
    assert report['source']['sha256'] == sha256(master.read_bytes()).hexdigest()
    assert report['interval_count'] >= 1
    assert any(row['duration_seconds'] >= .15 for row in report['intervals'])
    assert snapshots == [True, True] and len(runtimes) == 1
    assert job_file.read_bytes() == original and retained_files(completed) == files
    assert not locked(source) and broker.snapshot()['leases'] == []
    assert not list((source.folder / 'cache').glob('.carr-inspect-*'))


@pytest.mark.parametrize('change', ['restored', 'invalid-restored', 'pending', 'missing-master'])
def test_primary_ineligible_job_cannot_resolve_former_outputs_or_build_runtime(completed, monkeypatch, change):
    source, state, _, _ = completed
    changed = dict(state)
    if change == 'restored':
        changed['restore_requires_new_output'] = True
    elif change == 'invalid-restored':
        changed['restore_requires_new_output'] = 'true'
    elif change == 'pending':
        changed['state'] = 'pending'
    else:
        changed['outputs'] = [row for row in state['outputs'] if row['file'] != 'master.wav']
    job_file = source.folder / 'jobs' / (state['id'] + '.json')
    atomic_json(job_file, changed)
    expected = job_file.read_bytes()
    monkeypatch.setattr(source, 'publication_snapshot',
        lambda *a, **k: pytest.fail('Ineligible job reached external artifact verification.'))
    monkeypatch.setattr(source, 'runtime_factory',
        lambda *a, **k: pytest.fail('Ineligible job reached runtime construction.'))
    with pytest.raises(ValueError):
        inspect(completed)
    assert job_file.read_bytes() == expected
    assert not list((source.folder / 'cache').glob('.carr-inspect-*'))


@pytest.mark.parametrize('change', ['script', 'frames', 'reading-copy'])
def test_primary_rehashed_timeline_cannot_bypass_original_publication_semantics(completed, monkeypatch, change):
    source, state, _, _ = completed
    def mutation(value):
        if change == 'script':
            value['script_sha256'] = '0' * 64
        elif change == 'frames':
            value['frames'] += 1
        else:
            value['timings'][0]['text'] = 'Unreviewed reading copy'
    _fixtures.rewrite_timeline(source, state, mutation)
    job_file = source.folder / 'jobs' / (state['id'] + '.json')
    expected = job_file.read_bytes()
    monkeypatch.setattr(source, 'runtime_factory',
        lambda *a, **k: pytest.fail('Invalid original-source graph reached decoding.'))
    with pytest.raises(ValueError):
        inspect(completed)
    assert job_file.read_bytes() == expected
    assert not ResourceBroker.for_app(source.private_root).snapshot()['leases']


@pytest.mark.parametrize('change', ['job', 'master'])
def test_primary_change_after_actual_scan_cannot_return_report_or_rewrite_journal(completed, monkeypatch, change):
    source, state, _, _ = completed
    job_file = source.folder / 'jobs' / (state['id'] + '.json')
    original = job_file.read_bytes()
    master = Path(state['output_dir']) / 'master.wav'
    before = master.read_bytes()
    actual_factory = source.runtime_factory
    observed = []

    def factory(root, **options):
        runtime = actual_factory(root, **options)
        actual = runtime.inspect_silence
        def changed(*args, **kwargs):
            report = actual(*args, **kwargs)
            observed.append(report['pcm_sha256'])
            if change == 'job':
                atomic_json(job_file, dict(state, message='External metadata change after measurement.'))
            else:
                with master.open('r+b') as stream:
                    stream.seek(-2, os.SEEK_END)
                    stream.write(b'\x01\x00')
            return report
        runtime.inspect_silence = changed
        return runtime

    monkeypatch.setattr(source, 'runtime_factory', factory)
    with pytest.raises((ValueError, PermissionError)):
        inspect(completed)
    assert observed and not locked(source)
    if change == 'job':
        assert source.get(state['id'])['message'] == 'External metadata change after measurement.'
        assert master.read_bytes() == before
    else:
        assert job_file.read_bytes() == original
        if os.name == 'nt':
            assert master.read_bytes() == before
    assert not ResourceBroker.for_app(source.private_root).snapshot()['leases']
    assert not list((source.folder / 'cache').glob('.carr-inspect-*'))


def test_primary_cancel_after_decode_preserves_original_job_and_outputs(completed):
    source, state, _, _ = completed
    stopped = threading.Event()
    job_file = source.folder / 'jobs' / (state['id'] + '.json')
    original, files = job_file.read_bytes(), retained_files(completed)
    phases = []
    def progress(message):
        if 'Measuring intervals at or below the threshold' in message:
            phases.append(message)
            stopped.set()
    with pytest.raises(AudioCancelled):
        inspect(completed, cancelled=stopped.is_set, progress=progress)
    assert phases and job_file.read_bytes() == original
    assert retained_files(completed) == files
    assert not ResourceBroker.for_app(source.private_root).snapshot()['leases']


@pytest.mark.parametrize('masked', [False, True], ids=['direct-owner', 'masked-owner'])
def test_primary_existing_render_lock_and_lease_follow_pcm_probe_owner(completed, monkeypatch, masked):
    source, state, _, _ = completed
    master = Path(state['output_dir']) / 'master.wav'
    broker = ResourceBroker.for_app(source.private_root)
    allowed, owners, children = threading.Event(), [], []
    actual_stop, actual_factory = process._stop_owned, source.runtime_factory
    stages = []
    files = retained_files(completed)

    def manual(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)

    def delayed(child):
        assert actual_stop(child) is True
        args = tuple(map(str, child.args))
        if not children and '-show_entries' in args and Path(args[-1]) == master:
            children.append(child)
        if children and child is children[0] and not allowed.is_set():
            raise TimeoutError('Modeled missing final acknowledgement after actual primary PCM probe.')
        return True

    def factory(root, **options):
        stages.append(Path(root))
        runtime = actual_factory(root, **options)
        actual = runtime.inspect_silence
        def measured(*args, **kwargs):
            try:
                return actual(*args, **kwargs)
            except cleanup.AudioCleanupPending as error:
                if masked:
                    raise OSError('Modeled wrapper preserving its native cause.') from error
                raise
        runtime.inspect_silence = measured
        return runtime

    monkeypatch.setattr(cleanup, 'pending_cleanup', manual)
    monkeypatch.setattr(process, '_stop_owned', delayed)
    monkeypatch.setattr(source, 'runtime_factory', factory)
    try:
        with pytest.raises(OSError if masked else cleanup.AudioCleanupPending) as caught:
            inspect(completed, broker=broker)
        assert children and children[0].poll() == 0
        assert set(cleanup._pending_owners(caught.value)) == set(owners)
        assert owners and all(not owner.close() for owner in owners)
        assert locked(source) and len(broker.snapshot()['leases']) == 1
        assert len(stages) == 1 and stages[0].is_dir()
        with pytest.raises(RuntimeError, match='active render or publication'):
            inspect(completed, broker=broker)
        allowed.set()
        assert all(owner.close() for owner in owners)
        assert not stages[0].exists() and not locked(source)
        assert not broker.snapshot()['leases'] and retained_files(completed) == files
        assert source.get(state['id']) == state
    finally:
        allowed.set()
        for owner in owners:
            assert owner.close()
        monkeypatch.setattr(process, '_stop_owned', actual_stop)


def test_library_router_uses_exact_director_family_and_current_shared_broker(completed, monkeypatch):
    source, state, _, budget = completed
    router = LibraryPublicationService(source.private_root, budget=budget)
    seen = []
    actual = DirectorPublicationSource.inspect_silence
    def routed(instance, job_id, **options):
        assert instance.private_root == source.private_root
        assert instance.project_id == source.project_id and job_id == state['id']
        assert options['broker'] is router.production.broker
        assert options['budget'] is budget
        instance.runtime_factory = source.runtime_factory
        seen.append(job_id)
        return actual(instance, job_id, **options)
    monkeypatch.setattr(DirectorPublicationSource, 'inspect_silence', routed)
    try:
        report = router.inspect_silence('director:' + source.project_id + ':' + state['id'],
            threshold_dbfs=-60, minimum_seconds=.1)
        assert report['source_family'] == 'director' and seen == [state['id']]
        assert report['job_id'] == state['id']
        assert report['selected_source_id'] == 'director:' + source.project_id + ':' + state['id']
        for malformed in (state['id'], 'director:' + state['id'], 'unknown:' + state['id']):
            with pytest.raises(ValueError, match='Unknown inspection'):
                router.inspect_silence(malformed, threshold_dbfs=-60, minimum_seconds=.1)
        router.close()
        with pytest.raises(RuntimeError, match='closed'):
            router.inspect_silence('director:' + source.project_id + ':' + state['id'],
                threshold_dbfs=-60, minimum_seconds=.1)
        assert seen == [state['id']]
    finally:
        router.close()


@pytest.mark.parametrize('wrong', [False, True], ids=['exact-local-id', 'wrong-local-id'])
def test_scene_picker_identity_is_added_without_replacing_measurement_job_id(completed, monkeypatch, wrong):
    source, state, _, budget = completed
    router = LibraryPublicationService(source.private_root, budget=budget)
    report = {'schema': 'carr-silence-inspection/1', 'status': 'complete',
              'job_id': 'another-job' if wrong else state['id']}
    calls = []
    def scene(job_id, **options):
        calls.append((job_id, options))
        return report
    monkeypatch.setattr(router.production, 'inspect_silence', scene)
    try:
        selection = 'scene:' + state['id']
        if wrong:
            with pytest.raises(ValueError, match='another selected source'):
                router.inspect_silence(selection, threshold_dbfs=-60, minimum_seconds=.1)
        else:
            result = router.inspect_silence(selection, threshold_dbfs=-60, minimum_seconds=.1)
            assert result['job_id'] == state['id'] and result['selected_source_id'] == selection
            assert result is not report and 'selected_source_id' not in report
        assert len(calls) == 1 and calls[0][0] == state['id']
        assert calls[0][1]['budget'] is budget
    finally:
        router.close()
