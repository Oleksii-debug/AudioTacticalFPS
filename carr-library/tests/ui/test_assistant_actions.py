"""Actual worker/service acceptance without claiming native key dispatch."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.ui.assistant_actions import AssistantActions


@pytest.fixture
def host(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reviewed task scope')
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = inputs / 'source.txt'
    source.write_text('Exact railway source. Україна.', encoding='utf-8')
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    frame = SimpleNamespace(library=app, active=(workspace, None), _closing=False)
    return frame, inputs, outputs, source


def preview_import(host):
    frame, inputs, outputs, source = host
    actions = AssistantActions(frame)
    actions.preview('Import "' + str(source) + '"', Scope(frame.active[0], (inputs,), outputs))
    return actions


def test_preview_has_no_side_effect_and_real_worker_imports_only_after_run(host):
    frame, _, _, _ = host
    actions = preview_import(host)
    assert not actions.service.jobs() and not frame.library.browse(frame.active[0])
    generation, operation = actions.prepare_run()
    with ThreadPoolExecutor(max_workers=1) as executor:
        report = executor.submit(operation).result(timeout=5)
    assert actions.finish(generation) and not actions.busy
    assert report.status == 'complete' and len(frame.library.browse(frame.active[0])) == 1


@pytest.mark.parametrize('change', ['parent_close', 'destroy', 'library', 'active', 'database', 'root', 'edit'])
def test_pending_worker_cannot_run_after_parent_or_approval_changes(host, tmp_path, change):
    frame, _, _, _ = host
    original = frame.library
    workspace = frame.active[0]
    original_root, original_database = original.root, original.database
    actions = preview_import(host)
    generation, operation = actions.prepare_run()
    if change == 'parent_close':
        frame._closing = True
    elif change == 'destroy':
        actions.close()
    elif change == 'library':
        frame.library = LibraryApp(tmp_path / 'other')
    elif change == 'active':
        frame.active = (original.create_workspace('Other'), None)
    elif change == 'database':
        original.database = tmp_path / 'different.sqlite'
    elif change == 'root':
        original.root = tmp_path / 'different'
    else:
        actions.invalidate()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(InterruptedError, match='before execution'):
                executor.submit(operation).result(timeout=5)
        assert not actions.finish(generation)
        assert not actions.busy
    finally:
        original.root, original.database = original_root, original_database
    assert not original.browse(workspace)


@pytest.mark.parametrize('change', ['close', 'scope', 'library'])
def test_running_worker_cancels_before_actual_create_commit(host, tmp_path, change):
    frame, _, _, _ = host
    original = frame.library
    actions = AssistantActions(frame)
    actions.preview('Create library "Cancelled operation"', Scope(allow_create=True))
    entered, release = Event(), Event()
    def progress(*_):
        entered.set()
        assert release.wait(5)
    generation, operation = actions.prepare_run(progress=progress)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(operation)
        assert entered.wait(5)
        if change == 'close':
            actions.close()
        elif change == 'scope':
            frame.active = None
        else:
            frame.library = LibraryApp(tmp_path / 'other')
        release.set()
        report = future.result(timeout=5)
    assert report.status == 'paused' and report.completed == 0
    assert [row.name for row in original.workspaces()] == ['Reviewed task scope']
    assert not actions.finish(generation)


def test_saved_job_displays_exact_plan_before_restart_approval(host):
    frame, inputs, outputs, source = host
    service = AssistantService(frame.library)
    plan = service.preview('Import "' + str(source) + '"', Scope(frame.active[0], (inputs,), outputs))
    identity = service.submit(plan)
    actions = AssistantActions(frame)
    with pytest.raises(PermissionError, match='Preview'):
        actions.prepare_run()
    reviewed = actions.review_job(identity)
    assert reviewed.to_dict() == plan.to_dict() and actions.approved_digest == plan.digest
    generation, operation = actions.prepare_run()
    with ThreadPoolExecutor(max_workers=1) as executor:
        report = executor.submit(operation).result(timeout=5)
    assert actions.finish(generation) and report.status == 'complete'


def test_changed_plan_after_preview_cannot_execute(host):
    actions = preview_import(host)
    actions.plan.steps[0].arguments['path'] = '/not-approved/source.txt'
    with pytest.raises(PermissionError, match='Preview'):
        actions.prepare_run()
    assert not actions.service.jobs()


def test_edited_pending_generation_releases_busy_but_old_completion_cannot_own_new_task(host):
    frame, _, _, _ = host
    actions = preview_import(host)
    old, operation = actions.prepare_run()
    actions.invalidate()
    with pytest.raises(InterruptedError):
        operation()
    assert not actions.finish(old) and not actions.busy
    actions.preview('List books', Scope(frame.active[0]))
    current, second = actions.prepare_run()
    assert not actions.finish(old) and actions.busy
    assert second().status == 'complete'
    assert actions.finish(current)


def test_nested_chooser_result_cannot_accept_plan_from_changed_parent(host):
    frame, _, _, _ = host
    actions = AssistantActions(frame)
    candidate = actions.service.preview('List books', Scope(frame.active[0]))
    frame.active = None
    with pytest.raises(ValueError, match='parent library'):
        actions.accept(candidate)
    assert actions.plan is None


def test_completed_search_late_pause_yields_historical_report(host):
    frame, _, _, _ = host
    frame.library.add_text(frame.active[0], 'Text', 'Exact railway quotation.')
    actions = AssistantActions(frame)
    actions.preview('Search "railway"', Scope(frame.active[0]))
    generation, operation = actions.prepare_run()
    with ThreadPoolExecutor(max_workers=1) as executor:
        report = executor.submit(operation).result(timeout=5)
    actions.cancel_event.set()
    assert actions.finish(generation)
    historical = actions.service.get(report.id)
    assert historical.results[0]['results'][0]['verified'] is False


@pytest.mark.parametrize('change', ['close', 'scope', 'library'])
def test_running_search_cancel_is_paused_before_results_are_accepted(host, tmp_path, change):
    frame, _, _, _ = host
    original = frame.library
    original.add_text(frame.active[0], 'Text', 'Exact railway quotation.')
    actions = AssistantActions(frame)
    actions.preview('Search "railway"', Scope(frame.active[0]))
    entered, release = Event(), Event()
    execute = actions.service._execute

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return execute(*args, **kwargs)

    actions.service._execute = held
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation, future = actions.start(executor)
        assert entered.wait(5)
        if change == 'close':
            actions.close()
        elif change == 'scope':
            frame.active = None
        else:
            frame.library = LibraryApp(tmp_path / 'replacement')
        release.set()
        report = future.result(timeout=5)
    assert report.status == 'paused' and report.completed == 0
    assert not report.results and not actions.finish(generation)
    assert actions.service.get(report.id).status == 'paused'


def test_search_timeout_without_cancellation_remains_visible_failure(host, monkeypatch):
    frame, _, _, _ = host
    actions = AssistantActions(frame)
    actions.preview('Search "railway"', Scope(frame.active[0]))

    def timed_out(*args, **kwargs):
        raise TimeoutError('Controlled search deadline exceeded')

    monkeypatch.setattr(actions.service._search, 'search', timed_out)
    generation, operation = actions.prepare_run()
    report = operation()
    assert report.status == 'failed' and report.completed == 0
    assert 'deadline exceeded' in report.message
    assert not actions.cancel_event.is_set() and actions.finish(generation)


def test_completion_from_destroyed_parent_is_not_deliverable(host):
    frame, _, _, _ = host
    actions = AssistantActions(frame)
    actions.preview('List books', Scope(frame.active[0]))
    generation, operation = actions.prepare_run()
    with ThreadPoolExecutor(max_workers=1) as executor:
        report = executor.submit(operation).result(timeout=5)
    assert report.status == 'complete'
    actions.close()
    assert not actions.finish(generation) and actions.cancel_event.is_set()


def test_actual_executor_rejection_restores_idle_controls_and_allows_retry(host):
    actions = preview_import(host)
    transitions = []
    executor = ThreadPoolExecutor(max_workers=1)
    executor.shutdown(wait=True)
    with pytest.raises(RuntimeError, match='shutdown'):
        actions.start(executor, set_running=transitions.append)
    assert transitions == [True, False] and not actions.busy
    with ThreadPoolExecutor(max_workers=1) as replacement:
        generation, future = actions.start(replacement, set_running=transitions.append)
        report = future.result(timeout=5)
    assert report.status == 'complete' and actions.finish(generation)


def test_incomplete_host_identity_is_unavailable_to_late_callbacks(host):
    frame, _, _, _ = host
    actions = preview_import(host)
    original = frame.library.assets
    frame.library.assets = None
    try:
        assert not actions.current()
        with pytest.raises(ValueError, match='parent library'):
            actions.ensure()
    finally:
        frame.library.assets = original
