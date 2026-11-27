"""Real local schedules: canonical snapshots/imports, no background dispatcher."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup
from carr_library import local_schedules as schedules
from carr_library.local_schedule_model import validate_record
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits, ResourceRequest


class Clock:
    def __init__(self):
        self.value = datetime.now(timezone.utc)

    def __call__(self):
        return self.value

    def at(self, value):
        from carr_library.local_schedule_model import utc
        self.value = utc(value)

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scheduled library')
    app.add_text(workspace, 'Original', 'An unchanged railway original.')
    watch = tmp_path / 'selected'
    watch.mkdir()
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    clock = Clock()
    store = schedules.ScheduleStore(app, clock=clock)
    return app, workspace, watch, repository, clock, store


def rule(world, kind='watch', **options):
    app, workspace, watch, repository, clock, store = world
    return store.create(kind, 'Явне локальне правило', repository if kind == 'backup' else watch,
        first_due=clock(), interval_seconds=60, lateness_seconds=300,
        **({'workspace': workspace} if kind == 'watch' else {}), **options)


def run_once(store, view, **options):
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    outcome = session.tick(**options)
    return session, outcome


def next_due(clock, store, identity):
    clock.at(store.get(identity).record['next_due'])


def test_save_and_new_sessions_never_execute_or_auto_arm(world):
    app, workspace, watch, repository, clock, store = world
    (watch / 'new.txt').write_text('Scheduled incoming original.', encoding='utf-8')
    view = rule(world)
    before = tuple(app.browse(workspace))
    first = schedules.ScheduleSession(store)
    second = schedules.ScheduleSession(schedules.ScheduleStore(app, clock=clock))
    clock.advance(120)
    assert first.armed == second.armed == ()
    assert first.tick() is second.tick() is None
    assert tuple(app.browse(workspace)) == before
    assert not (app.root / 'import-batches').exists()
    assert store.get(view.id).record['last'] is None
    first.close()
    with pytest.raises(RuntimeError, match='closed'):
        first.arm(view.id, expected=view.sha256)


def test_actual_watched_folder_import_dedup_changed_bytes_and_no_empty_batches(world):
    app, workspace, watch, repository, clock, store = world
    source = watch / 'новий.txt'
    source.write_text('A first scheduled source.', encoding='utf-8')
    (watch / 'unsupported.bin').write_bytes(b'left alone')
    view = rule(world)
    session, first = run_once(store, view)
    assert first['status'] == 'complete' and first['recorded']
    batch_id = first['result']['batch_id']
    actual = app.import_batch(workspace, batch_id)
    assert actual['counts']['complete'] == 1
    assert actual['rows'][0]['result']['isolated'] and actual['rows'][0]['result']['verified']
    original_bytes = source.read_bytes()
    batch_files = sorted((app.root / 'import-batches').glob('*.json'))
    next_due(clock, store, view.id)
    idle = session.tick()
    assert idle['status'] == 'complete' and idle['result']['batch_id'] is None
    assert sorted((app.root / 'import-batches').glob('*.json')) == batch_files
    assert source.read_bytes() == original_bytes
    source.write_text('A changed scheduled source.', encoding='utf-8')
    next_due(clock, store, view.id)
    changed = session.tick()
    assert changed['result']['batch_id'] != batch_id and changed['result']['counts']['complete'] == 1
    assert len(app.browse(workspace)) == 3
    source.unlink()
    next_due(clock, store, view.id)
    removed = session.tick()
    assert removed['result']['batch_id'] is None and len(app.browse(workspace)) == 3
    session.close()


def test_actual_two_successive_backups_restore_independently_and_inactive(world, tmp_path):
    app, workspace, watch, repository, clock, store = world
    view = rule(world, 'backup')
    session, first = run_once(store, view)
    assert first['status'] == 'complete' and first['recorded']
    first_token = first['result']['snapshot_sha256']
    app.add_text(workspace, 'Second', 'A later library revision.')
    next_due(clock, store, view.id)
    second = session.tick()
    second_token = second['result']['snapshot_sha256']
    assert second_token != first_token and second['result']['parent'] == first_token
    assert second['result']['reused_chunks'] > 0
    session.close()
    for token, count, name in ((first_token, 1, 'first'), (second_token, 2, 'second')):
        target = tmp_path / name
        restored = incremental_backup.restore_snapshot(repository, token, target)
        assert restored['restored']
        recovered = LibraryApp(target)
        assert len(recovered.browse(workspace)) == count
        recovered_store = schedules.ScheduleStore(recovered)
        saved = recovered_store.get(view.id)
        unarmed = schedules.ScheduleSession(recovered_store)
        assert unarmed.armed == () and unarmed.tick() is None
        with pytest.raises(PermissionError):
            unarmed.arm(saved.id, expected=saved.sha256)
        assert saved.record['last']['status'] == 'dispatching'
        unarmed.close()


def test_changed_current_vault_head_requires_new_explicit_token_review(world):
    app, _, _, repository, clock, store = world
    view = rule(world, 'backup')
    outside = incremental_backup.create_snapshot(app, repository)
    session = schedules.ScheduleSession(store)
    with pytest.raises(PermissionError, match='advanced'):
        session.arm(view.id, expected=view.sha256)
    assert session.armed == ()
    reviewed = store.acknowledge(view.id, expected=view.sha256, next_due=clock(),
                                parent=outside['snapshot_sha256'])
    session.arm(view.id, expected=reviewed.sha256)
    assert session.tick()['result']['parent'] == outside['snapshot_sha256']
    session.close()


@pytest.mark.parametrize('kind', ['backup', 'watch'])
def test_exact_source_root_or_target_replacement_refuses_before_dispatch(world, tmp_path, kind):
    app, _, watch, repository, clock, store = world
    view = rule(world, kind)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    target = repository if kind == 'backup' else watch
    target.rename(tmp_path / ('old-' + kind))
    target.mkdir()
    if kind == 'watch':
        (target / 'unapproved.txt').write_text('Do not import these replacement bytes.', encoding='utf-8')
    before = store.get(view.id).record
    with pytest.raises(PermissionError):
        session.tick()
    assert store.get(view.id).record == before and session.armed == ()
    assert not (app.root / 'import-batches').exists()


def test_same_path_library_replacement_rejects_stale_store_and_session(world, tmp_path):
    app, workspace, watch, _, _, store = world
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    old = tmp_path / 'old-library'
    app.root.rename(old)
    replacement = LibraryApp(app.root)
    (watch / 'new.txt').write_text('Never approved for the replacement library.', encoding='utf-8')
    before = (old / 'state' / 'local-schedules' / (view.id + '.json')).read_bytes()
    with pytest.raises(PermissionError):
        session.tick()
    assert (old / 'state' / 'local-schedules' / (view.id + '.json')).read_bytes() == before
    assert not (replacement.root / 'import-batches').exists()


def test_same_byte_policy_restore_invalidates_live_ticket_identity(world):
    app, _, watch, _, _, store = world
    (watch / 'new.txt').write_text('Must not start under a restored live ticket.', encoding='utf-8')
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    path = store.directory / (view.id + '.json')
    raw = path.read_bytes()
    replacement = path.with_name('.pending-abcdefgh')
    replacement.write_bytes(raw)
    before = path.stat().st_ino
    os.replace(replacement, path)
    assert path.stat().st_ino != before and sha256(path.read_bytes()).hexdigest() == view.sha256
    with pytest.raises(PermissionError, match='restored'):
        session.tick()
    assert not (app.root / 'import-batches').exists()


def test_expired_window_is_attention_without_catchup(world, monkeypatch):
    _, _, _, _, clock, store = world
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    monkeypatch.setattr(session, '_watch', lambda *a: pytest.fail('Expired work cannot run.'))
    clock.advance(301)
    outcome = session.tick()
    assert outcome['status'] == 'attention' and 'expired' in outcome['message']
    assert session.armed == () and store.get(view.id).record['last']['started'] is None


def test_utc_rollback_disarms_without_running_a_later_tick(world):
    _, _, _, _, clock, store = world
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    clock.advance(1)
    assert session.has_due()
    clock.advance(-2)
    with pytest.raises(InterruptedError, match='backwards'):
        session.has_due()
    assert session.armed == ()
    clock.advance(10)
    assert session.tick() is None


def test_close_and_disarm_cancel_only_their_live_ticket(world):
    _, _, _, _, _, store = world
    view = rule(world)
    first, second = schedules.ScheduleSession(store), schedules.ScheduleSession(store)
    first.arm(view.id, expected=view.sha256)
    second.arm(view.id, expected=view.sha256)
    first.close()
    assert first.armed == () and second.armed == (view.id,)
    assert second.tick()['status'] == 'complete'
    second.close()


def test_actual_native_catalog_lock_denies_competing_dispatch(world):
    _, _, _, _, _, store = world
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    with store.locked():
        with pytest.raises(RuntimeError, match='already running'):
            session.tick()
    assert store.get(view.id).record['sequence'] == 0 and session.armed == ()


def test_waiting_for_shared_admission_stops_before_any_source_scan(world, monkeypatch):
    app, _, watch, _, _, store = world
    (watch / 'new.txt').write_text('Still untouched.', encoding='utf-8')
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    observed = Event()
    original = ResourceBroker._reason
    def reason(self, request, volumes, active, limits):
        result = original(self, request, volumes, active, limits)
        if active and result is not None:
            observed.set()
        return result
    monkeypatch.setattr(ResourceBroker, '_reason', reason)
    from carr_library.ingest import batch_scan
    monkeypatch.setattr(batch_scan, 'scan_paths', lambda *a, **kw: pytest.fail('Scan began before admission.'))
    outcomes = []
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='fixture-held'):
        thread = Thread(target=lambda: outcomes.append(session.tick()), daemon=True)
        thread.start()
        assert observed.wait(5), 'The actual broker did not observe the held CPU slot.'
        session.disarm()
        thread.join(5)
        assert not thread.is_alive()
        assert outcomes and outcomes[0]['status'] in ('attention', 'cancelled')
        assert not (app.root / 'import-batches').exists()
    assert not broker.snapshot()['leases']


def test_observer_failure_cannot_reverse_actual_watched_import(world):
    app, workspace, watch, _, _, store = world
    (watch / 'new.txt').write_text('Observer-independent import.', encoding='utf-8')
    view = rule(world)
    def observer(*args):
        raise RuntimeError('Detached UI observer')
    session, outcome = run_once(store, view, progress=observer)
    assert outcome['status'] == 'complete' and outcome['recorded']
    assert len(app.browse(workspace)) == 2
    session.close()


def test_partial_cancel_keeps_exact_existing_batch_and_requires_review(world):
    app, workspace, watch, _, clock, store = world
    for number in range(2):
        (watch / (str(number) + '.txt')).write_text('Distinct watched source ' + str(number), encoding='utf-8')
    view = rule(world)
    stopped = Event()
    session, outcome = run_once(store, view, cancelled=stopped.is_set,
        progress=lambda done, total, phase: stopped.set() if done == 1 else None)
    assert outcome['status'] == 'attention' and outcome['result']['state'] == 'paused'
    identity = outcome['result']['batch_id']
    batch = app.import_batch(workspace, identity)
    assert batch['counts']['complete'] == 1 and batch['counts']['queued'] == 1
    assert len(app.browse(workspace)) == 2 and session.armed == ()
    saved = store.get(view.id)
    with pytest.raises(PermissionError, match='unresolved'):
        schedules.ScheduleSession(store).arm(view.id, expected=saved.sha256)
    completed = app.run_import_batch(workspace, identity)
    assert completed['state'] == 'complete' and len(app.browse(workspace)) == 3
    reviewed = store.acknowledge(view.id, expected=saved.sha256, next_due=clock())
    resumed, idle = run_once(store, reviewed)
    assert idle['result']['batch_id'] is None
    assert len(list((app.root / 'import-batches').glob('*.json'))) == 1
    resumed.close()


@pytest.mark.parametrize('fault', ['backup-returned', 'batch-planned'])
def test_actual_process_death_never_replays_unsettled_dispatch(world, tmp_path, fault):
    app, workspace, watch, repository, clock, store = world
    prior = incremental_backup.create_snapshot(app, repository)
    if fault == 'batch-planned':
        (watch / 'new.txt').write_text('Source not imported by the crashed planner.', encoding='utf-8')
        view = rule(world)
    else:
        view = rule(world, 'backup', parent=prior['snapshot_sha256'])
    report = tmp_path / 'fault-result.json'
    child = r'''
import json, os, sys
from pathlib import Path
from carr_library import local_schedules as module
assert Path(module.__file__).resolve() == Path(sys.argv[4]).resolve()
from carr_library.application import LibraryApp
app = LibraryApp(sys.argv[1])
store = module.ScheduleStore(app)
session = module.ScheduleSession(store)
session.arm(sys.argv[2], expected=sys.argv[3])
def die(value):
    with Path(sys.argv[5]).open('wb') as out:
        out.write(json.dumps(value).encode('utf-8')); out.flush(); os.fsync(out.fileno())
    os._exit(73)
if sys.argv[6] == 'backup-returned':
    from carr_library import incremental_backup
    actual = incremental_backup.create_snapshot
    def stop_after_return(*args, **kwargs):
        result = actual(*args, **kwargs)
        die(result)
    incremental_backup.create_snapshot = stop_after_return
else:
    from carr_library.ingest.batches import ImportBatchStore
    actual = ImportBatchStore.plan
    def stop_after_plan(self, *args, **kwargs):
        result = actual(self, *args, **kwargs)
        die({'batch_id': result['batch_id']})
    ImportBatchStore.plan = stop_after_plan
session.tick()
raise AssertionError('Actual fault boundary was not reached')
'''
    process = subprocess.run([sys.executable, '-B', '-c', child, str(app.root), view.id,
        view.sha256, str(Path(schedules.__file__).resolve()), str(report), fault],
        capture_output=True, timeout=90, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert process.returncode == 73, process.stderr.decode('utf-8', 'replace')
    returned = json.loads(report.read_text(encoding='utf-8'))
    saved = store.get(view.id)
    assert saved.record['last']['status'] == 'dispatching'
    reopened = schedules.ScheduleSession(schedules.ScheduleStore(app))
    with pytest.raises(PermissionError, match='unresolved'):
        reopened.arm(view.id, expected=saved.sha256)
    assert reopened.tick() is None
    if fault == 'backup-returned':
        assert incremental_backup.verify_snapshot(repository, returned['snapshot_sha256'])['payloads_verified']
        target = tmp_path / 'prior-restored'
        incremental_backup.restore_snapshot(repository, prior['snapshot_sha256'], target)
        assert len(LibraryApp(target).browse(workspace)) == 1
        assert len(incremental_backup.list_snapshots(repository)) == 2
    else:
        batch = app.import_batch(workspace, returned['batch_id'])
        assert batch['counts']['queued'] == 1 and len(app.browse(workspace)) == 1
        assert len(list((app.root / 'import-batches').glob('*.json'))) == 1


def test_returned_backup_receipt_survives_later_schedule_write_failure(world, monkeypatch):
    _, _, _, repository, _, store = world
    view = rule(world, 'backup')
    actual = store._write
    def fail_completion(value, **kwargs):
        if value['last'] and value['last']['status'] == 'complete':
            raise OSError('Injected schedule publication failure after actual backup return.')
        return actual(value, **kwargs)
    monkeypatch.setattr(store, '_write', fail_completion)
    session, outcome = run_once(store, view)
    assert outcome['status'] == 'complete' and not outcome['recorded'] and session.armed == ()
    assert incremental_backup.verify_snapshot(repository, outcome['result']['snapshot_sha256'])['payloads_verified']
    assert store.get(view.id).record['last']['status'] == 'dispatching'


def test_actual_visible_dispatch_fsync_failure_does_not_execute(world, monkeypatch):
    _, _, _, _, _, store = world
    view = rule(world)
    from carr_library import durable_state
    actual = durable_state.sync_directory
    def fail(path):
        if Path(path) == store.directory:
            raise OSError('Injected directory synchronization failure')
        return actual(path)
    monkeypatch.setattr(durable_state, 'sync_directory', fail)
    session, outcome = run_once(store, view)
    assert outcome['status'] == 'attention' and outcome['result'] is None
    assert store.get(view.id).record['last']['status'] == 'dispatching'
    assert session.armed == ()


def test_same_byte_competing_policy_creation_is_a_conflict(world, monkeypatch):
    _, _, _, _, _, store = world
    actual = schedules.atomic_bytes
    created = []
    def competitor(path, raw, **kwargs):
        if not kwargs.get('replace'):
            Path(path).write_bytes(raw)
            created.append(Path(path))
        return actual(path, raw, **kwargs)
    monkeypatch.setattr(schedules, 'atomic_bytes', competitor)
    with pytest.raises(FileExistsError):
        rule(world)
    assert len(created) == 1 and created[0].is_file()
    assert len(store.list()) == 1


@pytest.mark.parametrize('entry', ['UNKNOWN.json', 'A' * 32 + '.json', 'extra', 'foreign.lock'])
def test_unknown_aliased_or_nested_schedule_namespace_fails_before_work(world, entry):
    _, _, _, _, _, store = world
    view = rule(world)
    path = store.directory / entry
    if entry == 'extra':
        path.mkdir()
    else:
        path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        store.get(view.id)


def test_hardlinked_policy_and_hardlinked_watched_input_are_rejected(world):
    app, _, watch, _, _, store = world
    view = rule(world)
    path = store.directory / (view.id + '.json')
    alias = store.root.parent / 'policy-alias.json'
    os.link(path, alias)
    with pytest.raises(ValueError):
        store.get(view.id)
    alias.unlink()
    source = watch / 'source.txt'
    source.write_text('A linked file is not scheduled for import.', encoding='utf-8')
    os.link(source, watch / 'alias.txt')
    session, outcome = run_once(store, view)
    assert outcome['status'] == 'attention' and session.armed == ()
    assert not (app.root / 'import-batches').exists()


def test_current_workspace_archive_denies_dispatch_without_authorizing_a_neighbor(world):
    app, workspace, watch, _, _, store = world
    other = app.create_workspace('Other library')
    (watch / 'new.txt').write_text('Not approved for the other scope.', encoding='utf-8')
    view = rule(world)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        session.tick()
    assert app.browse(other) == () and session.armed == ()


@pytest.mark.parametrize('field,value', [
    ('interval_seconds', True), ('interval_seconds', 59), ('interval_seconds', 604801),
    ('lateness_seconds', 0), ('recursive', 1), ('root_identity', [0, 0]),
    ('target', 'https://example.test/remote'), ('created', '2026-01-01'),
])
def test_saved_policy_validation_is_strict_and_passive(world, field, value):
    _, _, _, _, _, store = world
    view = rule(world)
    record = view.record
    record[field] = value
    with pytest.raises(ValueError):
        validate_record(record)


def test_rule_count_limit_and_disabled_policy_never_auto_arms(world):
    _, _, _, _, _, store = world
    views = [rule(world) for _ in range(32)]
    with pytest.raises(ValueError, match='32'):
        rule(world)
    disabled = store.disable(views[0].id, expected=views[0].sha256)
    with pytest.raises(PermissionError):
        schedules.ScheduleSession(store).arm(disabled.id, expected=disabled.sha256)
    assert len(store.list()) == 32


@pytest.mark.parametrize('kind', ['backup', 'watch'])
def test_replacement_inside_canonical_service_checkpoint_stops_unapproved_target(world, tmp_path, monkeypatch, kind):
    app, workspace, watch, repository, _, store = world
    target = repository if kind == 'backup' else watch
    if kind == 'watch':
        (watch / 'original.txt').write_text('Reviewed source bytes.', encoding='utf-8')
    view = rule(world, kind)
    session = schedules.ScheduleSession(store)
    session.arm(view.id, expected=view.sha256)
    moved = tmp_path / ('moved-' + kind)
    entered = []
    if kind == 'backup':
        actual = incremental_backup.create_snapshot
        def switched(*args, **kwargs):
            entered.append(True)
            target.rename(moved)
            incremental_backup.initialize_repository(target)
            return actual(*args, **kwargs)
        monkeypatch.setattr(incremental_backup, 'create_snapshot', switched)
    else:
        from carr_library.ingest import batch_scan
        actual = batch_scan.scan_paths
        def switched(*args, **kwargs):
            entered.append(True)
            target.rename(moved)
            target.mkdir()
            (target / 'unapproved.txt').write_text('New directory identity, no import approval.', encoding='utf-8')
            return actual(*args, **kwargs)
        monkeypatch.setattr(batch_scan, 'scan_paths', switched)
    outcome = session.tick()
    assert entered == [True] and outcome['status'] == 'attention'
    assert outcome['result'] is None and session.armed == ()
    assert store.get(view.id).record['last']['status'] == 'attention'
    assert len(app.browse(workspace)) == 1 and not (app.root / 'import-batches').exists()
    if kind == 'backup':
        assert incremental_backup.list_snapshots(target) == []
        assert incremental_backup.list_snapshots(moved) == []
        assert not tuple((target / 'chunks').glob('*/*'))
    else:
        assert (moved / 'original.txt').read_text(encoding='utf-8') == 'Reviewed source bytes.'
        assert (target / 'unapproved.txt').read_text(encoding='utf-8').startswith('New directory')


def test_invalid_returned_snapshot_contract_cannot_be_classified_complete(world, monkeypatch):
    _, _, _, repository, _, store = world
    view = rule(world, 'backup')
    actual, tokens = incremental_backup.create_snapshot, []
    def invalid(*args, **kwargs):
        result = actual(*args, **kwargs)
        tokens.append(result['snapshot_sha256'])
        return {**result, 'published': False}
    monkeypatch.setattr(incremental_backup, 'create_snapshot', invalid)
    session, outcome = run_once(store, view)
    assert outcome['status'] == 'attention' and outcome['result'] is None and session.armed == ()
    assert store.get(view.id).record['last']['status'] == 'attention'
    assert incremental_backup.verify_snapshot(repository, tokens[0])['payloads_verified']


def test_returned_snapshot_survives_post_release_failure_without_auto_repetition(world, monkeypatch):
    _, _, _, repository, _, store = world
    view = rule(world, 'backup')
    from carr_library.operations.broker import ResourceLease
    actual = ResourceLease.release
    calls = []
    def released_then_failed(self):
        actual(self)
        if self.operation_id.startswith('schedule-backup:'):
            calls.append(self.operation_id)
            raise OSError('Injected reporting failure after actual reservation release.')
    monkeypatch.setattr(ResourceLease, 'release', released_then_failed)
    session, outcome = run_once(store, view)
    assert calls and outcome['status'] == 'complete' and outcome['recorded']
    assert outcome['result']['control_cleanup_pending'] and outcome['message']
    assert session.armed == ()
    assert incremental_backup.verify_snapshot(repository, outcome['result']['snapshot_sha256'])['payloads_verified']


@pytest.mark.parametrize('kind,options', [
    ('backup', {'workspace': 'unreviewed-import-scope'}),
    ('backup', {'recursive': True}),
    ('watch', {'include_generated': True}),
    ('watch', {'parent': 'a' * 64}),
])
def test_cross_operation_options_are_rejected_without_silent_ignoring(world, kind, options):
    _, _, _, _, _, store = world
    with pytest.raises(ValueError):
        rule(world, kind, **options)
    assert store.list() == ()
