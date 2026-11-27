"""Explicit CLI queue/schedule flow on real existing assistant jobs."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import cli
from carr_library.assistant.scheduling import ScheduleManager


@pytest.fixture
def scheduled_cli(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    plan = Plan(Scope(allow_create=True),
                (Step('library.create', {'name': 'CLI once'}), Step('library.list')))
    identity = service.submit(plan)
    clock = [datetime(2026, 11, 27, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(ScheduleManager, '_now', lambda self: clock[0])
    return app, service, plan, identity, clock


def invoke(app, capsys, *arguments):
    code = cli.main(['schedule', '--data-dir', str(app.root), *arguments])
    output = capsys.readouterr()
    return code, None if not output.out.strip() else json.loads(output.out), output.err


def test_cli_queue_preserves_exact_reviewed_plan_without_running(tmp_path, capsys):
    root = tmp_path / 'library'
    instructions = tmp_path / 'plan.json'
    instructions.write_text(json.dumps({'schema': 'carr-assistant-plan/1',
        'steps': [{'tool': 'library.create', 'arguments': {'name': 'Queued only'}}]}), encoding='utf-8')
    status = cli.main(['--data-dir', str(root), '--instructions', str(instructions), '--allow-create', '--queue'])
    result = json.loads(capsys.readouterr().out)
    app = LibraryApp(root)
    saved = AssistantService(app).store.load(result['id'])
    assert status == 0 and result['status'] == saved['status'] == 'queued'
    assert result['execution_started'] is False and saved['plan'] == result['plan']
    assert saved['plan_sha256'] == result['plan_sha256'] and app.workspaces() == ()


def test_cli_create_list_and_early_foreground_exit_never_dispatch(scheduled_cli, capsys):
    app, service, plan, identity, clock = scheduled_cli
    due = clock[0] + timedelta(seconds=10)
    code, view, _ = invoke(app, capsys, 'create', '--job', identity, '--plan-sha256', plan.digest,
                           '--at', due.isoformat(), '--lateness-seconds', '10')
    assert code == 0 and view['state'] == 'pending' and app.workspaces() == ()
    code, rows, _ = invoke(app, capsys, 'list')
    assert code == 0 and rows[0]['plan'] == plan.to_dict() and len(rows) == 1
    saved = service.store.path(identity).read_bytes()
    code, report, _ = invoke(app, capsys, 'run', '--job', identity,
        '--schedule-sha256', view['schedule_sha256'], '--run')
    assert code == 3 and report['status'] == 'not_due'
    assert service.store.path(identity).read_bytes() == saved and app.workspaces() == ()


def test_cli_explicit_due_run_uses_same_checkpoint_once(scheduled_cli, capsys):
    app, service, plan, identity, clock = scheduled_cli
    view = ScheduleManager(service).create(identity, clock[0] + timedelta(seconds=1),
        expected_plan_sha256=plan.digest, lateness_seconds=60)
    clock[0] += timedelta(seconds=2)
    code, report, _ = invoke(app, capsys, 'run', '--job', identity,
                             '--schedule-sha256', view.schedule_sha256, '--run')
    assert code == 0 and report['status'] == 'complete'
    assert [row.name for row in app.workspaces()] == ['CLI once']
    assert service.store.load(identity)['schedule']['state'] == 'complete'
    code, report, error = invoke(app, capsys, 'run', '--job', identity,
                                 '--schedule-sha256', view.schedule_sha256, '--run')
    assert code == 1 and report is None and 'pending schedule changed' in error
    assert len(app.workspaces()) == 1


def test_cli_missing_explicit_arm_and_changed_pins_never_run(scheduled_cli, capsys):
    app, service, plan, identity, clock = scheduled_cli
    view = ScheduleManager(service).create(identity, clock[0], expected_plan_sha256=plan.digest)
    with pytest.raises(SystemExit, match='--run'):
        cli.main(['schedule', '--data-dir', str(app.root), 'run', '--job', identity,
                  '--schedule-sha256', view.schedule_sha256])
    capsys.readouterr()
    code, result, _ = invoke(app, capsys, 'run', '--job', identity,
                             '--schedule-sha256', '0' * 64, '--run')
    assert code == 1 and result is None and app.workspaces() == ()


def test_cli_expired_and_disabled_slots_require_review(scheduled_cli, capsys):
    app, service, plan, identity, clock = scheduled_cli
    view = ScheduleManager(service).create(identity, clock[0], expected_plan_sha256=plan.digest,
                                            lateness_seconds=1)
    clock[0] += timedelta(seconds=2)
    code, result, _ = invoke(app, capsys, 'run', '--job', identity,
                             '--schedule-sha256', view.schedule_sha256, '--run')
    assert code == 1 and result['status'] == 'attention' and app.workspaces() == ()
    view = ScheduleManager(service).get(identity)
    code, disabled, _ = invoke(app, capsys, 'disable', '--job', identity,
                               '--schedule-sha256', view.schedule_sha256)
    assert code == 0 and disabled['state'] == 'disabled'


@pytest.mark.parametrize('wait', ['nan', 'inf', '-1', '86401'], ids=['nan', 'infinite', 'negative', 'too-long'])
def test_cli_refuses_unbounded_foreground_wait_before_opening_state(tmp_path, wait):
    missing = tmp_path / 'does-not-exist'
    with pytest.raises(SystemExit, match='foreground wait'):
        cli.main(['schedule', '--data-dir', str(missing), 'run', '--job', 'a' * 32,
                  '--schedule-sha256', 'b' * 64, '--run', '--wait-seconds', wait])
    assert not missing.exists()


def test_cli_queue_and_run_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(['--data-dir', str(tmp_path / 'library'), '--instructions', 'selected.json',
                  '--queue', '--run'])


def test_cli_sigint_during_real_schedule_returns130_and_retains_checkpoint(
        scheduled_cli, capsys, monkeypatch):
    from carr_library.assistant import schedule_cli
    app, service, plan, identity, clock = scheduled_cli
    view = ScheduleManager(service).create(identity, clock[0], expected_plan_sha256=plan.digest)
    handlers = []
    original_signal = schedule_cli.signal.signal
    def capture_signal(number, handler):
        handlers.append(handler)
        return original_signal(number, handler)
    monkeypatch.setattr(schedule_cli.signal, 'signal', capture_signal)
    def interrupted(self, step, scope, record, checkpoint, persist, cancelled, progress, index):
        handlers[0](schedule_cli.signal.SIGINT, None)
        assert cancelled()
        raise InterruptedError('Explicit signal before the first actual operation.')
    monkeypatch.setattr(AssistantService, '_execute', interrupted)
    code, result, _ = invoke(app, capsys, 'run', '--job', identity,
                             '--schedule-sha256', view.schedule_sha256, '--run')
    assert code == 130 and result['status'] == 'paused'
    saved = service.store.load(identity)
    assert saved['schedule']['state'] == 'attention' and app.workspaces() == ()
