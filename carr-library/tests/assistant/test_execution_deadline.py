"""Elapsed budgets guard real local mutations, including a stationary clock tick."""
import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant import service as service_module


@pytest.fixture
def bounded_job(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    clock = [1_000_000_000]
    # Keep the old float-clock path deterministic too: this fixture reproduced
    # the actual Windows failure before the integer-deadline correction.
    monkeypatch.setattr(service_module, 'monotonic', lambda: clock[0] / 1_000_000_000,
                        raising=False)
    monkeypatch.setattr(service_module, 'perf_counter_ns', lambda: clock[0], raising=False)

    def submit(seconds, *, list_after=False):
        steps = (Step('library.create', {'name': 'Bounded library'}),)
        if list_after:
            steps += (Step('library.list'),)
        return service.submit(Plan(Scope(allow_create=True, max_seconds=seconds), steps))

    return app, service, clock, submit


def test_subnanosecond_budget_cannot_authorize_a_same_tick_mutation(bounded_job):
    app, service, _, submit = bounded_job
    identity = submit(1e-12)
    result = service.run(identity)
    assert result.status == 'paused' and result.completed == 0
    assert app.workspaces() == ()
    saved = service.store.load(identity)
    assert saved['workspace'] is None and saved['steps'][0]['status'] == 'pending'


def test_exact_deadline_before_dispatch_expires_inclusively(bounded_job, monkeypatch):
    app, service, clock, submit = bounded_job
    identity = submit(1)
    save = service.store.save

    def settle_running(record):
        save(record)
        if record['status'] == 'running':
            clock[0] = 2_000_000_000

    monkeypatch.setattr(service.store, 'save', settle_running)
    result = service.run(identity)
    assert result.status == 'paused' and result.completed == 0
    assert app.workspaces() == ()


@pytest.mark.parametrize('seconds', [1, 1.0, 1e300])
def test_positive_available_budget_still_completes_a_real_operation(bounded_job, seconds):
    app, service, _, submit = bounded_job
    identity = submit(seconds)
    result = service.run(identity)
    assert result.status == 'complete' and result.completed == 1
    assert [row.name for row in app.workspaces()] == ['Bounded library']
    assert service.get(identity) == result


def test_progress_cannot_spend_the_deadline_then_admit_a_new_mutation(bounded_job):
    app, service, clock, submit = bounded_job
    identity = submit(1)

    def progress(done, total, phase):
        if phase == 'library.create':
            clock[0] = 2_000_000_000

    result = service.run(identity, progress=progress)
    assert result.status == 'paused' and result.completed == 0
    assert app.workspaces() == ()


def test_completed_step_is_retained_and_resumed_without_duplicate_mutation(bounded_job, monkeypatch):
    app, service, clock, submit = bounded_job
    identity = submit(1, list_after=True)
    execute = service._execute

    def complete_then_expire(step, *args, **kwargs):
        result = execute(step, *args, **kwargs)
        if step.tool == 'library.create':
            clock[0] += 1_000_000_000
        return result

    monkeypatch.setattr(service, '_execute', complete_then_expire)
    result = service.run(identity)
    assert result.status == 'paused' and result.completed == 1
    workspace = result.results[0]['workspace']
    assert [row.id for row in app.workspaces()] == [workspace]
    saved = service.store.load(identity)
    assert [step['status'] for step in saved['steps']] == ['complete', 'pending']
    resumed = service.resume(identity)
    assert resumed.status == 'complete' and resumed.completed == 2
    assert [row.id for row in app.workspaces()] == [workspace]
    assert resumed.results[0] == result.results[0]
