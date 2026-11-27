"""Review/row identity and worker-settlement policies; native controls have a separate gate."""
from types import SimpleNamespace
from threading import Event, Thread

import pytest

from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.book_batch import BookBatchService, select_books
from carr_library.ui.book_batch_dialog import BookWorkspace, BookBatchDialog, review_text
from tests.audio.test_book_plans import library, make_plan


def workspace(library):
    return BookWorkspace(BookBatchService(BookAudioService(library[0], library[1])))


def test_displayed_identity_survives_reorder_and_removal_never_retargets(library, tmp_path):
    state = workspace(library)
    a, b = make_plan(library), make_plan(library, item=library[3])
    state.add(a)
    state.add(b)
    displayed = (a.id, b.id)
    view = BookBatchDialog.__new__(BookBatchDialog)
    view.state, view.displayed = state, displayed
    view.selected = SimpleNamespace(GetSelection=lambda: 0)
    state.move(a.id, 1)
    assert view.selection() == a.id and state.index(a.id) == 1
    state.remove(a.id)
    with pytest.raises(ValueError, match='changed'):
        view.selection()
    assert [row['plan'].id for row in state.rows] == [b.id]


def test_prepared_review_is_invalidated_by_order_or_destination(library, tmp_path):
    state = workspace(library)
    a, b = make_plan(library), make_plan(library, item=library[3])
    state.add(a)
    state.add(b)
    output = tmp_path / 'output'
    output.mkdir()
    state.output(output)
    captured = state.snapshot()
    _, batch, plans, outputs = captured
    review = state.service.review(batch, outputs, plans=plans)
    state.accept_review(captured, review)
    assert state.reviewed is review
    state.move(a.id, 1)
    assert state.reviewed is None and state.batch is None
    with pytest.raises(ValueError, match='changed'):
        state.accept_review(captured, review)
    assert str(output) in review_text(batch, plans, review)


def test_saved_batch_does_not_restore_prior_output_permission(library, tmp_path):
    state = workspace(library)
    state.add(make_plan(library))
    output = tmp_path / 'output'
    output.mkdir()
    state.output(output)
    snapshot = state.snapshot()
    _, batch, plans, outputs = snapshot
    reviewed = state.service.review(batch, outputs, plans=plans)
    state.service.prepare(batch, outputs, plans=plans, approval=reviewed.fingerprint)
    state.load(batch.id)
    assert state.batch.id == batch.id and state.rows[0]['output'] is None and state.reviewed is None
    with pytest.raises(ValueError, match='output'):
        state.snapshot()


def shell():
    view = BookBatchDialog.__new__(BookBatchDialog)
    library = object()
    view.library, view.scope = library, ('workspace', None)
    view.frame = SimpleNamespace(library=library, active=view.scope, _closing=False,
        cancel_event=Event(), working=False, busy=False)
    view.alive, view.busy, view.closing = True, False, False
    view.cancel, view.action, view.operation_token = Event(), 'pause', 0
    view.messages, view.destroyed_count = [], 0
    view.report_box = SimpleNamespace(ChangeValue=view.messages.append, SetFocus=lambda: None)
    view.controls = lambda: None
    view.books = SimpleNamespace(close=lambda: None)
    def destroy():
        view.destroyed_count += 1
    view.dialog = SimpleNamespace(Destroy=destroy)
    return view


def test_close_waits_for_actual_worker_and_ignores_late_progress():
    view = shell()
    entered, release = Event(), Event()
    callbacks, done = [], []
    def run_job(label, operation, done=None, failed=None):
        def worker():
            entered.set()
            release.wait(5)
            try:
                value = operation()
                callbacks.append(lambda: done(value))
            except Exception as error:
                callbacks.append(lambda error=error: failed(error))
        thread = Thread(target=worker)
        thread.start()
        view.thread = thread
        return True
    view.frame.run_job = run_job
    view.begin('held work', lambda: 'result', done.append)
    assert entered.wait(2)
    veto = []
    event = SimpleNamespace(CanVeto=lambda: True, Veto=lambda: veto.append(True))
    view.close(event)
    assert veto and view.busy and view.alive and view.destroyed_count == 0
    release.set()
    view.thread.join(5)
    assert not view.thread.is_alive() and len(callbacks) == 1
    callbacks.pop()()
    assert not view.alive and view.destroyed_count == 1 and not done
    previous = tuple(view.messages)
    view._progress(view.operation_token, 'late result')
    assert tuple(view.messages) == previous


def test_failed_submission_resets_busy_and_complete_result_survives_stop():
    view = shell()
    view.frame.run_job = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('submit refused'))
    with pytest.raises(RuntimeError, match='submit refused'):
        view.begin('work', lambda: None, lambda result: None)
    assert not view.busy
    captured, completed = [], []
    view.frame.run_job = lambda label, work, done, failed: captured.append(done)
    view.begin('published', lambda: None, completed.append, accept_after_stop=True)
    view.cancel.set()
    captured.pop()({'status': 'complete', 'destination': 'accepted'})
    assert completed == [{'status': 'complete', 'destination': 'accepted'}]


def test_late_progress_from_previous_operation_or_changed_library_is_ignored():
    view = shell()
    view.busy, view.operation_token = True, 4
    view._progress(3, 'old')
    assert not view.messages
    view._progress(4, 'current')
    assert view.messages == ['current']
    view.frame.library = object()
    view._progress(4, 'foreign context')
    assert view.messages == ['current']
