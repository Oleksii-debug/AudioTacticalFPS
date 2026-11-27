"""Cross-schema admission and restore contracts for the single ASR facade.

All runtimes/models below are synthetic identities. Real child execution tests
file orchestration only; no inference, model quality or assistive-technology
qualification is implied.
"""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_sidecars import portable_name
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.state_backup import snapshot_state, restore_state
from carr_library.transcription import TranscriptionService, V5_PROTOCOL, RECEIPTS_PROTOCOL
from carr_library.integrations.transcription_scope import check_scope, local_origin
from carr_library.integrations.transcription_values import fingerprint

_spec = importlib.util.spec_from_file_location(
    'unified_asr_fixture', Path(__file__).with_name('fixture_runtime.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


@pytest.fixture
def prepared(tmp_path):
    runtime = _fixture.make_runtime(tmp_path / 'runtime')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit first workspace')
    broker = ResourceBroker.for_app(app.root)
    broker.configure(ResourceLimits(min_free_disk_bytes=0))
    service = TranscriptionService(app, workspace, broker=broker)
    profile = _fixture.register(service, runtime)
    source = tmp_path / 'selected.wav'
    source.write_bytes(b'Synthetic file transport, never decoded as speech.')
    return app, workspace, service, profile, runtime, source


def job_file(service, identity):
    return service.root / 'jobs' / identity / 'job.json'


def starts(runtime):
    path = runtime.worker.with_name('fixture-starts.log')
    return len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0


def test_one_facade_keeps_both_saved_schemas_and_read_only_history_separate(prepared):
    app, workspace, service, profile, runtime, source = prepared
    legacy = service.submit([source])
    canonical = service.create_job(profile['id'], [source])
    assert [row['id'] for row in service.list()] == [legacy]
    assert [row['id'] for row in service.list_jobs()] == [canonical['id']]
    assert service.get(legacy)['protocol'] == RECEIPTS_PROTOCOL
    assert service.get(canonical['id'])['schema'] == 'resonance-transcription-job/1'
    assert not (service.root / 'engine.json').exists()
    assert starts(runtime) == 0


def test_collision_between_schemas_is_refused_without_selecting_a_protocol(prepared):
    app, workspace, service, profile, runtime, source = prepared
    canonical = service.create_job(profile['id'], [source])
    legacy_path = service._path(canonical['id'])
    legacy_path.mkdir()
    (legacy_path / 'job.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Ambiguous'):
        service.get(canonical['id'])
    assert starts(runtime) == 0


def test_global_historical_job_needs_explicit_workspace_review_before_execution(prepared):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    (job_file(service, job['id']).parent / 'workspace.json').unlink()
    before = job_file(service, job['id']).read_bytes()
    origin_before = (service.root / '.local-origin.json').read_bytes()
    assert service.get(job['id'])['execution_review_required'] is True
    assert job_file(service, job['id']).read_bytes() == before
    with pytest.raises(PermissionError, match='bind'):
        service.run(job['id'])
    with pytest.raises(ValueError, match='review'):
        service.bind_canonical_job(job['id'])
    assert starts(runtime) == 0
    bound = service.bind_canonical_job(job['id'], reviewed=True)
    assert bound['status'] == 'pending'
    assert (service.root / '.local-origin.json').read_bytes() == origin_before
    assert starts(runtime) == 0
    completed = service.run(job['id'])
    assert completed['status'] == 'complete' and starts(runtime) == 1


def test_another_workspace_cannot_adopt_a_job_already_bound_elsewhere(prepared):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    other_workspace = app.create_workspace('Separate workspace')
    other = TranscriptionService(app, other_workspace)
    assert other.list_jobs() == ()
    for operation in (other.get, other.run,
                      lambda identity: other.bind_canonical_job(identity, reviewed=True)):
        with pytest.raises(PermissionError, match='another workspace'):
            operation(job['id'])
    assert starts(runtime) == 0


@pytest.mark.parametrize('change', ['settings', 'profile'])
def test_valid_rewritten_plan_cannot_inherit_previous_execution_review(prepared, change):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    state = read_json(job_file(service, job['id']))
    if change == 'settings':
        state['settings']['beam_size'] = 2
    else:
        # A changed but syntactically valid runtime identity must not receive
        # the workspace approval previously given to a different plan.
        state['profile_id'] = 'a' * 32
        state['profile_fingerprint'] = 'a' * 64
    state['plan_fingerprint'] = service._local_protocol()._plan(state)
    atomic_json(job_file(service, job['id']), state, replace=True)
    assert service.get(job['id'])['execution_review_required'] is True
    with pytest.raises(PermissionError, match='bind'):
        check_scope(service, job_file(service, job['id']).parent, state, execution=True)
    with pytest.raises(PermissionError, match='bind'):
        service.run(job['id'])
    assert starts(runtime) == 0


@pytest.mark.parametrize('name,maximum', [('.local-origin.json', 4096), ('workspace.json', 65536)])
def test_authority_json_has_an_exact_byte_budget_after_initial_stat(prepared, monkeypatch, name, maximum):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    from carr_library.integrations import transcription_scope as scope
    original = scope._bounded_json
    target = service.root / name if name.startswith('.') else job_file(service, job['id']).parent / name
    before = target.read_bytes()
    def grow(path, bound):
        if Path(path) == target:
            assert bound == maximum
            target.write_bytes(b' ' * (maximum + 1))
        return original(path, bound)
    monkeypatch.setattr(scope, '_bounded_json', grow)
    with pytest.raises((RestoreError, ValueError)):
        check_scope(service, job_file(service, job['id']).parent, job, execution=True)
    assert starts(runtime) == 0
    target.write_bytes(before)


@pytest.mark.parametrize('same_path', [False, True])
def test_standalone_state_restore_revokes_trust_and_workspace_authority(prepared, tmp_path, same_path):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    snapshot = tmp_path / 'state-copy'
    entries = snapshot_state(app.root / 'state', snapshot)
    assert all('.local-origin.json' not in row['filename'] for row in entries)
    target = app.root / 'state' if same_path else tmp_path / 'restored-state'
    if same_path:
        shutil.rmtree(target)
    restore_state(snapshot, target, entries)
    saved_profile = read_json(target / 'transcription/profiles' / (profile['id'] + '.json'))
    binding = read_json(target / 'transcription/jobs' / job['id'] / 'workspace.json')
    assert saved_profile['trusted'] is False and saved_profile['restored'] is True
    assert binding['review_required'] is True and binding['origin'] is None
    assert not (target / 'transcription/.local-origin.json').exists()
    assert starts(runtime) == 0
    if same_path:
        fresh = TranscriptionService(app, workspace)
        assert fresh.get(job['id'])['execution_review_required'] is True
        with pytest.raises(PermissionError, match='bind'):
            fresh.run(job['id'])
        fresh.bind_canonical_job(job['id'], reviewed=True)
        denied = fresh.run(job['id'])
        assert denied['status'] == 'failed'
        assert 'trust' in denied['error'].lower() and starts(runtime) == 0


@pytest.mark.parametrize('name', [
    'state/transcription/.local-origin.json',
    'state/TRANSCRIPTION/.LOCAL-ORIGIN.JSON',
])
def test_local_execution_authority_cannot_be_injected_as_portable_state(name):
    with pytest.raises((RestoreError, ValueError), match='origin'):
        portable_name(name)


def test_completed_results_survive_revocation_and_removed_external_engine(prepared, tmp_path):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    assert service.run(job['id'])['status'] == 'complete'
    original = service.read_transcript(job['id'])
    service.save_edit(job['id'], job['sources'][0]['id'], 'Separate verified user edit.')
    edited = service.read_transcript(job['id'], edited=True)
    snapshot = tmp_path / 'snapshot'
    entries = snapshot_state(app.root / 'state', snapshot)
    shutil.rmtree(app.root / 'state')
    restore_state(snapshot, app.root / 'state', entries)
    shutil.rmtree(runtime.root)
    fresh = TranscriptionService(app, workspace)
    assert fresh.read_transcript(job['id']) == original
    assert fresh.read_transcript(job['id'], edited=True) == edited
    assert fresh.run(job['id'])['status'] == 'complete'
    destination = tmp_path / 'export'
    destination.mkdir()
    receipt = fresh.export(job['id'], destination)
    assert receipt['status'] == 'complete' and receipt['files']
    with pytest.raises(PermissionError, match='bind'):
        fresh.save_edit(job['id'], job['sources'][0]['id'], 'No implicit restore approval.')


@pytest.mark.parametrize('protocol', [RECEIPTS_PROTOCOL, V5_PROTOCOL])
def test_full_runtime_admission_is_opt_in_and_does_not_convert_legacy_protocol(prepared, protocol):
    app, workspace, service, profile, runtime, source = prepared
    worker = runtime.worker
    if protocol == V5_PROTOCOL:
        worker = runtime.root / 'worker.py'
        worker.write_bytes(runtime.worker.read_bytes())
        for name in ('local_models.py', 'transcript_features.py'):
            (runtime.root / name).write_text('# Synthetic helper identity only.\n', encoding='utf-8')
    config = service.configure(runtime.python, worker, runtime.model.parent, protocol=protocol)
    assert 'runtime_profile_id' not in config
    inspected = service.inspect_runtime(runtime.python, worker, runtime.model, protocol=protocol)
    full = service.register_runtime('Explicit full legacy profile', runtime.python, worker, runtime.model,
        protocol=protocol, owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    bound = service.configure(runtime.python, worker, runtime.model.parent, protocol=protocol,
                              runtime_profile_id=full['id'])
    assert bound['protocol'] == protocol and bound['runtime_profile_id'] == full['id']
    assert service.configuration(verify=True)['runtime_profile_fingerprint'] == full['fingerprint']
    runtime.dependencies['numpy'].write_text('# Changed runtime bytes.\n', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        service.configuration(verify=True)
    assert starts(runtime) == 0


def test_v5_full_profile_binds_both_helper_sources(prepared):
    app, workspace, service, profile, runtime, source = prepared
    worker = runtime.root / 'worker.py'
    worker.write_bytes(runtime.worker.read_bytes())
    for name in ('local_models.py', 'transcript_features.py'):
        (runtime.root / name).write_text('# Synthetic helper.\n', encoding='utf-8')
    inspected = service.inspect_runtime(runtime.python, worker, runtime.model, protocol=V5_PROTOCOL)
    (runtime.root / 'local_models.py').write_text('# Replaced helper.\n', encoding='utf-8')
    with pytest.raises(ValueError, match='exact current'):
        service.register_runtime('Changed helpers', runtime.python, worker, runtime.model,
            protocol=V5_PROTOCOL, owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    assert starts(runtime) == 0


@pytest.mark.parametrize('phase', ['before', 'terminal'])
def test_ui_observer_failure_cannot_reverse_actual_child_completion(prepared, phase):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    events = []
    def unavailable(event):
        events.append(dict(event))
        if phase == 'before' or event.get('status') == 'complete':
            raise RuntimeError('Synthetic missing UI observer')
    result = service.run(job['id'], progress=unavailable)
    assert result['status'] == 'complete' and starts(runtime) == 1
    assert service.verify(job['id'])['status'] == 'complete'
    assert service.read_transcript(job['id'])['text'] == 'Синтетичний протокольний результат.'
    assert events


def test_mutated_library_root_refuses_all_execution_and_profile_mutations(prepared, tmp_path):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    app.root = tmp_path / 'different-library'
    for operation in (
        lambda: service.run(job['id']),
        lambda: service.bind_canonical_job(job['id'], reviewed=True),
        lambda: service.register_runtime('Must not inspect', runtime.python, runtime.worker, runtime.model),
    ):
        with pytest.raises(PermissionError, match='original library'):
            operation()
    assert starts(runtime) == 0


def test_pending_native_cleanup_retains_resource_lease_and_blocks_cache_reads(prepared, monkeypatch):
    """Controlled retained owner, real broker lease; no synthetic PID ownership."""
    import threading
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.integrations import transcription_protocol as protocol
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    entered, finished, pending = Event(), Event(), Event()
    class Owner:
        def __init__(self):
            self.quiescent_event = Event()
            self.close_calls = 0
        def close(self):
            self.close_calls += 1
            entered.set()
            return False
    owner = Owner()
    adapter = service._local_protocol()
    admit, archive = adapter._admit_results, adapter._archive_worker
    def guarded_admit(*args, **kwargs):
        if pending.is_set():
            assert owner.quiescent_event.is_set(), 'Never read raw cache while its native writer remains owned.'
        return admit(*args, **kwargs)
    def guarded_archive(*args, **kwargs):
        assert not pending.is_set() or owner.quiescent_event.is_set()
        return archive(*args, **kwargs)
    def retained_process(*args, **kwargs):
        assert kwargs['owned_windows_job'] is True
        pending.set()
        raise AudioCleanupPending('Synthetic retained owner, not a process failure report.',
                                  cleanup_owner=owner)
    monkeypatch.setattr(adapter, '_admit_results', guarded_admit)
    monkeypatch.setattr(adapter, '_archive_worker', guarded_archive)
    monkeypatch.setattr(protocol, 'run_process', retained_process)
    outcome = {}
    def run():
        try:
            outcome['state'] = service.run(job['id'])
        except BaseException as error:
            outcome['error'] = error
        finally:
            finished.set()
    runner = threading.Thread(target=run)
    runner.start()
    try:
        assert entered.wait(30), 'Runtime verification did not reach the controlled cleanup boundary.'
        assert not finished.is_set()
        assert read_json(job_file(service, job['id']))['status'] == 'running'
        assert ResourceBroker.for_app(app.root).snapshot()['leases']
        owner.quiescent_event.set()
        assert finished.wait(10), 'The quiescent owner did not release the admitted operation.'
    finally:
        owner.quiescent_event.set()
        runner.join(10)
    assert not runner.is_alive() and 'error' not in outcome, outcome
    assert owner.close_calls >= 1 and outcome['state']['status'] == 'failed'
    assert not ResourceBroker.for_app(app.root).snapshot()['leases']
    assert starts(runtime) == 0


@pytest.mark.parametrize('mutation', ['list', 'missing', 'mixed-v5-run'])
def test_malformed_or_mixed_retained_schema_never_selects_an_execution_protocol(prepared, mutation):
    app, workspace, service, profile, runtime, source = prepared
    job = service.create_job(profile['id'], [source])
    value = read_json(job_file(service, job['id']))
    if mutation == 'list':
        value = []
    elif mutation == 'missing':
        del value['sources']
    else:
        value['protocol'] = V5_PROTOCOL
        value['run_context'] = {'worker_run_id': 'a' * 32}
    atomic_json(job_file(service, job['id']), value, replace=True)
    with pytest.raises(ValueError):
        service.get(job['id'])
    with pytest.raises(ValueError):
        service.run(job['id'])
    assert starts(runtime) == 0


def test_full_profile_backup_is_passive_and_restore_revokes_legacy_configuration(prepared, tmp_path):
    from carr_library.library_backup import create_backup, restore_backup
    app, workspace, service, profile, runtime, source = prepared
    inspected = service.inspect_runtime(runtime.python, runtime.worker, runtime.model,
                                        protocol=RECEIPTS_PROTOCOL)
    full = service.register_runtime('Full inventory for an existing receipt protocol',
        runtime.python, runtime.worker, runtime.model, protocol=RECEIPTS_PROTOCOL,
        owner_trusted=True, expected_fingerprint=inspected['fingerprint'])
    service.configure(runtime.python, runtime.worker, runtime.model.parent,
                      runtime_profile_id=full['id'])
    shutil.rmtree(runtime.root)
    backup = tmp_path / 'passive-backup'
    digest = create_backup(app, backup)
    restored_root = restore_backup(backup, tmp_path / 'restored-library', digest)
    restored = LibraryApp(restored_root).transcription(workspace)
    saved = restored.configuration()
    assert saved['review_required'] is True and saved['restored'] is True
    assert saved['runtime_profile_id'] == full['id']
    assert all(row['trusted'] is False for row in restored.list_runtimes())
    # This refusal occurs before looking up the now-absent interpreter/model.
    with pytest.raises(PermissionError, match='explicit review'):
        restored.configuration(verify=True)
