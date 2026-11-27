"""Real isolated substitutions behind the existing narration controller."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from threading import Event, get_ident

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui import narration_editor as module


@pytest.fixture
def host(tmp_path):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Regex reading')
    imported = app.add_text(workspace, 'Original source', 'Original 😀: 12 kg.\r\nKeep the source.')
    page = ReadingDesk(app, workspace, imported['edition']).page(1)
    store = NarrationStore(app.root)
    project = new_project(page.raw_text, source_location=page.location)
    saved = store.save(project)
    controller = module.NarrationController(store, project, expected_digest=saved)
    job = module.RegexPreviewJob(controller)
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield app, workspace, imported, controller, job, executor
        job.close()


def compute(host, pattern=r'(\d+) kg', replacement=r'\1 kilograms'):
    *_, job, executor = host
    request, future = job.start(executor, pattern, replacement)
    future.result(timeout=10)
    return request, future, job.complete(request, future)


def test_explicit_preview_apply_undo_save_and_backup_keep_exact_citation(host, tmp_path):
    app, workspace, imported, controller, job, executor = host
    selected, = app.search(workspace, 'Original')
    original_bytes = app.assets.resolve(imported['asset']).read_bytes()
    original_project = controller.project
    request, future, preview = compute(host)
    assert controller.project is original_project and not controller.dirty
    assert '12 kilograms' in preview.text
    job.apply(preview)
    assert len(controller.project.revisions) == 2
    assert controller.project.original_text == original_project.original_text
    assert controller.project.source_location == original_project.source_location
    controller.undo()
    assert controller.pending_text == original_project.current_text
    controller.redo()
    assert controller.pending_text == preview.text
    controller.save()
    saved, _ = controller.store.load(controller.project.id)
    assert saved.current_text == preview.text and saved.source_location == original_project.source_location
    assert app.copy(workspace, selected.ref, selected.revision, 'quote') == selected.quote
    assert app.assets.resolve(imported['asset']).read_bytes() == original_bytes
    backup = tmp_path/'backup'
    checksum = app.backup(backup)
    restored = restore_backup(backup, tmp_path/'restored', checksum)
    assert NarrationStore(restored).load(saved.id)[0] == saved


def test_substitution_runs_outside_the_callers_thread(host, monkeypatch):
    actual = module.regex_preview
    entered = []
    def observed(*args, **kwargs):
        entered.append(get_ident())
        return actual(*args, **kwargs)
    monkeypatch.setattr(module, 'regex_preview', observed)
    compute(host)
    assert entered and entered[0] != get_ident()


@pytest.mark.parametrize('change', ['pending-text','project','root','closed','guard'])
def test_changed_editing_context_cannot_accept_completed_result(host, change):
    *_, controller, job, executor = host
    request, future = job.start(executor, 'Original', 'Changed')
    future.result(timeout=10)
    old = controller.project
    if change == 'pending-text':
        controller.pending_text = 'A newer user edit.'
    elif change == 'project':
        controller.project = replace(old, title='Different revision context')
    elif change == 'root':
        controller.store.root.rename(controller.store.root.with_name('original-root'))
        controller.store.root.mkdir()
    elif change == 'closed':
        job.close()
    else:
        controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source scope changed'))
    assert job.complete(request, future) is None
    assert controller.project.original_text == old.original_text
    assert not job.busy


def test_old_completion_and_old_preview_cannot_replace_new_operation(host):
    *_, controller, job, executor = host
    first_request, first_future, first = compute(host)
    second_request, second_future, second = compute(host, 'Original', 'Current')
    assert job.complete(first_request, first_future) is None
    with pytest.raises(ValueError, match='inspect'):
        job.apply(first)
    job.apply(second)
    assert controller.pending_text.startswith('Current') and '12 kg' in controller.pending_text


def test_other_future_cannot_supply_the_result_for_a_known_request(host):
    *_, job, executor = host
    request, future = job.start(executor, 'Original', 'Derived')
    actual = future.result(timeout=10)
    forged = Future()
    forged.set_result(replace(actual, text='Unowned future data'))
    with pytest.raises(ValueError, match='future'):
        job.complete(request, forged)
    assert job.complete(request, future) is actual


def test_cancel_after_worker_done_withdraws_unpublished_preview(host):
    *_, controller, job, executor = host
    request, future = job.start(executor, 'Original', 'Changed')
    future.result(timeout=10)
    job.cancel()
    assert job.complete(request, future) is None and not controller.dirty


def test_reentrant_guard_change_during_apply_keeps_newer_user_text(host):
    *_, controller, job, executor = host
    *_, preview = compute(host)
    original = controller.project
    def changed():
        controller.pending_text = 'Newer typed words.'
    controller.guard = changed
    with pytest.raises(ValueError, match='changed'):
        job.apply(preview)
    assert controller.pending_text == 'Newer typed words.' and controller.project is original


def test_submit_rejection_keeps_job_idle_and_allows_a_fresh_attempt(host):
    *_, controller, job, executor = host
    class Rejected:
        def submit(self, *args, **kwargs):
            raise RuntimeError('executor unavailable')
    with pytest.raises(RuntimeError, match='executor unavailable'):
        job.start(Rejected(), '.', 'x')
    assert not job.busy and not controller.dirty
    *_, proposal = compute(host)
    assert proposal.match_count == 1


def test_close_before_queued_job_starts_never_computes_or_mutates(host, monkeypatch):
    *_, controller, job, executor = host
    entered, release = Event(), Event()
    def held():
        entered.set()
        assert release.wait(5)
    blocker = executor.submit(held)
    assert entered.wait(5)
    try:
        request, future = job.start(executor, 'Original', 'Changed')
        job.close()
    finally:
        release.set()
    blocker.result(timeout=5)
    with pytest.raises(InterruptedError):
        future.result(timeout=10)
    assert not job.busy and not controller.dirty
    assert job.complete(request, future) is None


def test_cancel_between_preview_and_explicit_apply_keeps_history(host):
    *_, controller, job, executor = host
    *_, preview = compute(host)
    old = controller.project
    job.cancel()
    with pytest.raises(ValueError, match='inspect'):
        job.apply(preview)
    assert controller.project is old and not controller.dirty
