"""Foreground schedule authority, actual jobs, native contention and recovery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.models import canonical
from carr_library.assistant.scheduling import ScheduleManager, ScheduleSession
from carr_library.assistant.schedule_model import fingerprint
from carr_library.assistant.validation import validate_job
from carr_library.library_backup import create_backup, restore_backup, verify_backup


@pytest.fixture
def scheduled(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    output = tmp_path / 'output'; output.mkdir()
    service = AssistantService(app)
    plan = Plan(Scope(output_root=output, allow_create=True), (
        Step('library.create', {'name': 'Exactly once'}),
        Step('library.list'),
    ))
    identity = service.submit(plan)
    clock = [datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)]
    manager = ScheduleManager(service, clock=lambda: clock[0])
    due = clock[0] + timedelta(minutes=5)
    view = manager.create(identity, due, expected_plan_sha256=plan.digest, lateness_seconds=60)
    return app, service, identity, clock, manager, view, output


def test_saving_listing_arming_and_future_ticks_do_not_execute(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    raw = service.store.path(identity).read_bytes()
    assert manager.list() == (view,) and app.workspaces() == ()
    session = ScheduleSession(manager)
    assert session.tick() is None
    assert session.arm({identity: view.schedule_sha256}) == (identity,)
    assert session.tick() is None and app.workspaces() == ()
    assert service.store.path(identity).read_bytes() == raw
    session.close()
    clock[0] += timedelta(minutes=5)
    assert session.tick() is None and app.workspaces() == ()
    assert ScheduleSession(manager).tick() is None


def test_one_explicit_due_slot_uses_current_job_and_verifies_completion_once(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager)
    session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    phases = []
    result = session.tick(progress=lambda *row: phases.append(row))
    assert result.id == identity and result.status == 'complete' and result.completed == 2
    assert [row.name for row in app.workspaces()] == ['Exactly once']
    saved = service.store.load(identity)
    assert saved['schedule']['state'] == 'complete'
    assert saved['schedule']['started'] == clock[0].isoformat()
    assert manager.get(identity).state == 'complete'
    assert phases[-1] == (2, 2, 'complete')
    assert session.tick() is None and session.armed == ()
    with pytest.raises(PermissionError):
        ScheduleSession(manager).arm({identity: view.schedule_sha256})
    assert service.resume(identity).status == 'complete'
    assert len(app.workspaces()) == 1


def test_expired_window_is_attention_without_a_delayed_side_effect(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=6)
    result = session.tick()
    assert result.status == 'attention' and 'expired' in result.message
    assert app.workspaces() == () and session.tick() is None
    saved = service.store.load(identity)
    assert saved['status'] == 'queued' and saved['schedule']['started'] is None
    assert saved['schedule']['state'] == 'attention'
    with pytest.raises(PermissionError):
        ScheduleSession(manager).arm({identity: fingerprint(saved['schedule'])})
    # Only a new explicit time review may replace this never-started slot.
    new = manager.create(identity, clock[0] + timedelta(minutes=1),
        expected_plan_sha256=saved['plan_sha256'], expected_schedule_sha256=fingerprint(saved['schedule']))
    assert new.state == 'pending' and app.workspaces() == ()


def test_changed_plan_and_schedule_cannot_reuse_an_armed_pin(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    saved = service.store.load(identity)
    saved['plan']['steps'][0]['arguments']['name'] = 'Not the reviewed plan'
    saved['plan_sha256'] = sha256(canonical(saved['plan']).encode('utf-8')).hexdigest()
    saved['schedule']['plan_sha256'] = saved['plan_sha256']
    service.store.save(saved)
    clock[0] += timedelta(minutes=5)
    report = session.tick()
    assert report.status == 'attention' and app.workspaces() == ()
    assert session.armed == () and session.tick() is None


def test_ordinary_explicit_run_disables_pending_slot_without_another_executor(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    assert service.run(identity).status == 'complete'
    saved = service.store.load(identity)
    assert saved['schedule']['state'] == 'disabled'
    clock[0] += timedelta(minutes=5)
    assert session.tick().status == 'attention'
    assert len(app.workspaces()) == 1


def test_clock_rollback_disarms_without_rewriting_the_pending_job(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    assert session.tick() is None
    raw = service.store.path(identity).read_bytes()
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(RuntimeError, match='clock moved backwards'):
        session.tick()
    assert session.armed == () and app.workspaces() == ()
    assert service.store.path(identity).read_bytes() == raw


def test_two_live_sessions_share_the_actual_nonreentrant_job_lock(scheduled, monkeypatch):
    app, service, identity, clock, manager, view, _ = scheduled
    first, second = ScheduleSession(manager), ScheduleSession(manager)
    for session in (first, second):
        session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    entered, release = Event(), Event()
    original = service._execute
    calls = []
    def execute(step, *args, **kwargs):
        calls.append(step.tool)
        if step.tool == 'library.create':
            entered.set()
            assert release.wait(5), 'Test worker was not released.'
        return original(step, *args, **kwargs)
    monkeypatch.setattr(service, '_execute', execute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.tick)
        try:
            assert entered.wait(5), 'Scheduled operation never entered the actual executor.'
            report = second.tick()
            assert report.status == 'attention'
            assert app.workspaces() == () and calls == ['library.create']
            assert second.tick() is None
        finally:
            release.set()
        assert future.result(timeout=5).status == 'complete'
    assert calls == ['library.create', 'library.list'] and len(app.workspaces()) == 1


def test_disarming_an_inflight_tick_cancels_through_existing_checkpoint(scheduled, monkeypatch):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    entered, release = Event(), Event()
    original = service._execute
    def execute(step, scope, record, checkpoint, persist, cancelled, progress, index):
        entered.set()
        assert release.wait(5)
        if cancelled():
            raise InterruptedError('Stopped at the existing tool cancellation boundary.')
        return original(step, scope, record, checkpoint, persist, cancelled, progress, index)
    monkeypatch.setattr(service, '_execute', execute)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(session.tick)
        try:
            assert entered.wait(5)
            session.disarm()
        finally:
            release.set()
        report = future.result(timeout=5)
    assert report.status == 'paused' and app.workspaces() == ()
    saved = service.store.load(identity)
    assert saved['schedule']['state'] == 'attention' and saved['schedule']['started'] is not None
    assert session.tick() is None


def test_crash_after_action_before_completion_never_automatically_replays(scheduled, monkeypatch):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    original = service.store.save
    class SimulatedCrash(BaseException):
        pass
    def save(value):
        if value['steps'][0]['status'] == 'complete':
            raise SimulatedCrash('Process lost after actual library creation.')
        return original(value)
    monkeypatch.setattr(service.store, 'save', save)
    with pytest.raises(SimulatedCrash):
        session.tick()
    assert len(app.workspaces()) == 1 and session.tick() is None
    saved = service.store.load(identity)
    assert saved['status'] == 'running' and saved['schedule']['state'] == 'dispatching'
    fresh = ScheduleSession(manager)
    assert manager.get(identity).state == 'attention'
    with pytest.raises(PermissionError):
        fresh.arm({identity: fingerprint(saved['schedule'])})
    monkeypatch.setattr(service.store, 'save', original)
    assert service.resume(identity).status == 'complete'
    assert len(app.workspaces()) == 1
    assert service.store.load(identity)['schedule']['state'] == 'disabled'


def test_failed_claim_publication_never_dispatches_or_implicitly_retries(scheduled, monkeypatch):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    original = service.store.save
    def save(value):
        if value.get('schedule', {}).get('state') == 'dispatching':
            raise OSError('Injected claim publication failure.')
        return original(value)
    monkeypatch.setattr(service.store, 'save', save)
    assert session.tick().status == 'attention'
    assert app.workspaces() == () and session.tick() is None
    assert service.store.load(identity)['schedule']['state'] == 'pending'


def test_full_backup_of_scheduled_job_preserves_bytes_but_never_arms_restore(scheduled, tmp_path):
    app, service, identity, clock, manager, view, _ = scheduled
    raw = service.store.path(identity).read_bytes()
    target = tmp_path / 'snapshot'; digest = create_backup(app, target)
    verify_backup(target, digest)
    root = restore_backup(target, tmp_path / 'restored', digest)
    restored_service = AssistantService(LibraryApp(root))
    restored = ScheduleManager(restored_service, clock=lambda: clock[0])
    assert restored_service.store.path(identity).read_bytes() == raw
    assert restored.get(identity).state == 'attention'
    with pytest.raises(PermissionError):
        ScheduleSession(restored).arm({identity: view.schedule_sha256})
    assert ScheduleSession(restored).tick() is None
    second = tmp_path / 'second'; verify_backup(second, create_backup(LibraryApp(root), second))


def test_self_backup_can_borrow_only_its_existing_same_owner_job_lock(tmp_path):
    app = LibraryApp(tmp_path / 'library'); app.create_workspace('Backup source')
    output = tmp_path / 'selected-output'; output.mkdir()
    service = AssistantService(app)
    plan = Plan(Scope(output_root=output, allow_full_backup=True), (Step('library.backup', {'name': 'saved'}),))
    identity = service.submit(plan)
    clock = [datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)]
    manager = ScheduleManager(service, clock=lambda: clock[0])
    view = manager.create(identity, clock[0], expected_plan_sha256=plan.digest)
    session = ScheduleSession(manager); session.arm({identity: view.schedule_sha256})
    result = session.tick()
    assert result.status == 'complete', result.message
    digest = result.results[0]['manifest_sha256']
    verify_backup(output / 'saved', digest)
    relative = service.store.path(identity).relative_to(app.root)
    captured = json.loads((output / 'saved' / 'sidecars' / relative).read_bytes())
    assert captured['schedule']['state'] == 'dispatching' and captured['status'] == 'running'
    assert service.store.load(identity)['schedule']['state'] == 'complete'


@pytest.mark.parametrize('field,value', [
    ('state', 'armed'), ('state', True), ('root_identity', [1, 0]),
    ('root_identity', [True, 4]), ('plan_sha256', '0' * 64),
    ('due', '2026-11-27T12:05:00'), ('expires', '2026-11-28T12:06:01+00:00'),
    ('started', '2026-11-27T12:05:00+00:00'), ('message', '\0'),
], ids=['persisted-arming', 'state-bool', 'zero-inode', 'bool-inode', 'changed-plan',
        'naive-time', 'late-budget', 'pending-started', 'nul-message'])
def test_schedule_schema_is_data_only_and_fail_closed(scheduled, field, value):
    _, service, identity, *_ = scheduled
    record = service.store.load(identity); record['schedule'][field] = value
    with pytest.raises((ValueError, TypeError)):
        validate_job(record)


def test_schedule_definition_cannot_add_executable_or_network_controls(scheduled):
    _, service, identity, *_ = scheduled
    record = service.store.load(identity)
    record['schedule']['command'] = 'Do not execute data as a command.'
    with pytest.raises(ValueError):
        validate_job(record)
    with pytest.raises(ValueError):
        Step('schedule.arm', {'id': identity})


def test_completed_schedule_reverification_refuses_changed_actual_artifact_without_replaying(
        scheduled, monkeypatch):
    app, service, identity, clock, manager, view, output = scheduled
    artifact = output / 'completed.txt'
    actual = service._execute
    def produce(step, *args, **kwargs):
        result = actual(step, *args, **kwargs)
        if step.tool == 'library.list':
            artifact.write_text('original completed bytes', encoding='utf-8')
            result['artifacts'] = [str(artifact)]
        return result
    monkeypatch.setattr(service, '_execute', produce)
    session = ScheduleSession(manager)
    session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    assert session.tick().status == 'complete'
    before = service.store.path(identity).read_bytes()
    artifact.write_text('changed completed bytes!', encoding='utf-8')
    def no_dispatch(*args, **kwargs):
        raise AssertionError('A completed scheduled operation cannot be dispatched during verification.')
    monkeypatch.setattr(service, '_execute', no_dispatch)
    with pytest.raises(ValueError, match='completed artifact changed'):
        service.resume(identity)
    assert service.store.path(identity).read_bytes() == before
    assert len(app.workspaces()) == 1 and artifact.read_text('utf-8') == 'changed completed bytes!'


def test_cancelled_completed_schedule_verification_preserves_exact_historical_receipt(scheduled):
    app, service, identity, clock, manager, view, _ = scheduled
    session = ScheduleSession(manager)
    session.arm({identity: view.schedule_sha256})
    clock[0] += timedelta(minutes=5)
    assert session.tick().status == 'complete'
    before = service.store.path(identity).read_bytes()
    with pytest.raises(InterruptedError, match='saved result is retained'):
        service.resume(identity, cancelled=lambda: True)
    assert service.store.path(identity).read_bytes() == before and len(app.workspaces()) == 1
