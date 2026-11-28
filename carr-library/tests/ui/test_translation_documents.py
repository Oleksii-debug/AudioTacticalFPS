"""Actual controller admission/lifetime with the existing host work callbacks."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.translation_documents import DocumentTranslationController, selected_range


@pytest.fixture
def control(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Document UI')
    queue, accepted, failed, work = [], [], [], []
    frame = SimpleNamespace(library=library, active=(workspace, None),
        working=False, _closing=False, cancel_event=Event())
    def run_job(title, operation, done, failed):
        queue.append((operation, done, failed))
        return True
    frame.run_job = run_job
    controller = DocumentTranslationController(frame)
    return SimpleNamespace(controller=controller, frame=frame, queue=queue,
        accepted=accepted, failed=failed, work=work, tmp_path=tmp_path)


def execute(case):
    operation, done, failed = case.queue.pop(0)
    try:
        result = operation()
    except Exception as error:
        failed(error)
    else:
        done(result)


def mutation(case, mode):
    if mode == 'closed':
        case.controller.close()
    elif mode == 'closing':
        case.frame._closing = True
    elif mode == 'object':
        case.frame.library = LibraryApp(case.tmp_path / 'other-library')
    elif mode == 'root':
        case.frame.library.root = case.tmp_path / 'other-root'
    elif mode == 'replaced-root':
        root = Path(case.frame.library.root)
        root.rename(case.tmp_path / 'original-library-retained')
        root.mkdir()
        assert case.frame.library is case.controller.library
        assert Path(case.frame.library.root) == case.controller.root
    elif mode == 'workspace':
        case.frame.active = ('different-workspace', None)
    else:
        case.frame.active = (case.frame.active[0], 'different-topic')


@pytest.mark.parametrize('mode', ['closed', 'closing', 'object', 'root', 'replaced-root', 'workspace', 'topic'])
def test_queued_document_work_checks_original_scope_before_any_source_read(control, mode):
    c = control
    c.controller.start('Read selected source',
        lambda: c.work.append('read'), c.accepted.append, c.failed.append)
    mutation(c, mode)
    execute(c)
    assert not c.work and not c.accepted and not c.failed
    assert not c.controller.busy


@pytest.mark.parametrize('mode', ['closed', 'closing', 'object', 'root', 'replaced-root', 'workspace', 'topic'])
def test_late_document_result_never_updates_dead_or_different_native_view(control, mode):
    c = control
    c.controller.start('Completed saved result',
        lambda: c.work.append('saved') or {'status': 'complete'}, c.accepted.append, c.failed.append)
    operation, done, failed = c.queue.pop()
    result = operation()
    mutation(c, mode)
    done(result)
    assert c.work == ['saved'] and not c.accepted and not c.failed


def test_busy_action_does_not_clear_existing_pause_or_schedule_second_worker(control):
    c = control
    c.controller.start('First', lambda: c.work.append('first'), c.accepted.append, c.failed.append)
    c.controller.cancel.set()
    with pytest.raises(ValueError, match='active operation'):
        c.controller.start('Second', lambda: c.work.append('second'), c.accepted.append, c.failed.append)
    assert c.controller.cancel.is_set() and len(c.queue) == 1
    execute(c)
    assert not c.work and not c.accepted
    assert len(c.failed) == 1 and isinstance(c.failed[0], (InterruptedError, TimeoutError))


def test_host_refusal_releases_only_this_window_busy_state(control):
    c = control
    c.frame.run_job = lambda *args, **kwargs: False
    assert c.controller.start('Declined', lambda: c.work.append(True), c.accepted.append, c.failed.append) is False
    assert not c.controller.busy and not c.work


def test_host_exception_preserves_failure_and_releases_own_busy_state(control):
    c = control
    def rejected(*args, **kwargs):
        raise RuntimeError('Synthetic closed host executor')
    c.frame.run_job = rejected
    with pytest.raises(RuntimeError, match='closed host'):
        c.controller.start('Rejected', lambda: c.work.append(True), c.accepted.append, c.failed.append)
    assert not c.controller.busy and not c.work


def test_pending_pause_suppresses_late_success_without_erasing_saved_work(control):
    c = control
    c.controller.start('Saving accepted jobs',
        lambda: c.work.append('durable-job') or ['saved-id'], c.accepted.append, c.failed.append)
    operation, done, failed = c.queue.pop()
    result = operation()
    c.controller.cancel.set()
    done(result)
    assert c.work == ['durable-job'] and not c.accepted and not c.failed
    assert not c.controller.busy


@pytest.mark.parametrize('value,expected', [('*', (1, None)), ('1', (1, 1)), ('2-4', (2, 4)), (' 1 - 3 ', (1, 3))])
def test_native_document_range_has_explicit_bounded_meaning(value, expected):
    assert selected_range(value) == expected


@pytest.mark.parametrize('value', ['', '0', '4-2', '-1', 'chapter 2', '1-2-3', 'all books'])
def test_native_document_range_does_not_infer_chapters_or_whole_library(value):
    with pytest.raises(ValueError):
        selected_range(value)
