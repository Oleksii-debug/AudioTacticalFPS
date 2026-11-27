"""Reader dispatch boundaries using real source/state and a controlled UI queue.

These checks deliberately hold worker completion and UI delivery apart: a reader
can be cancelled, closed, or lose its library scope between those two events.
The native keyboard probe separately exercises wx accelerator dispatch.
"""
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.reading_desk import ReaderController, ReadingDialog


class _Control:
    def __init__(self, value='', *, enabled=True):
        self.value, self.enabled = value, enabled
        self.focused = False
        self.destroyed = False

    def _alive(self):
        assert not self.destroyed, 'A late callback touched a destroyed reader control.'

    def ChangeValue(self, value):
        self._alive()
        self.value = value

    def SetInsertionPoint(self, value):
        self._alive()

    def SetFocus(self):
        self._alive()
        self.focused = True

    def Enable(self, enabled=True):
        self._alive()
        self.enabled = enabled

    def Disable(self):
        self.Enable(False)

    def IsEnabled(self):
        self._alive()
        return self.enabled


class _Executor:
    """Real Future callbacks with deterministic worker completion, no wx thread."""

    def __init__(self):
        self.jobs = []
        self.stopped = False

    def submit(self, operation):
        assert not self.stopped
        future = Future()
        self.jobs.append((future, operation))
        return future

    def complete(self, index=0):
        future, operation = self.jobs[index]
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = operation()
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def shutdown(self, *, wait, cancel_futures):
        assert wait is False and cancel_futures is True
        self.stopped = True
        for future, _ in self.jobs:
            future.cancel()


class _NoPrompts:
    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            pytest.fail('A disabled reader command opened a prompt: ' + name)
        return unexpected


@pytest.fixture
def reader(tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Command boundary source')
    raw = 'First exact source. Україна.\fSecond exact source. Slovenská citácia.'
    book = library.add_text(workspace, 'Dispatch source', raw)
    state = ReadingState(library.root)
    cancelled = Event()
    desk = ReadingDesk(library, workspace, book['edition'], cancelled=cancelled.is_set)
    controller = ReaderController(desk, state, cancelled=cancelled.is_set, defer_display=True)

    dialog = ReadingDialog.__new__(ReadingDialog)
    dialog.wx_queue = []
    dialog.wx = SimpleNamespace(CallAfter=dialog.wx_queue.append, ID_CANCEL=5101)
    dialog.dialog = SimpleNamespace(IsModal=lambda: False)
    dialog.closed = dialog.busy = dialog._destroy_requested = False
    dialog.parent = SimpleNamespace()
    dialog._audio_dialog = dialog._appearance = None
    dialog.cancelled, dialog.controller = cancelled, controller
    dialog.executor = _Executor()
    dialog.text, dialog.location, dialog.status = _Control(), _Control(), _Control()
    dialog.buttons = [_Control(), _Control()]
    dialog.cancel = _Control(enabled=False)
    dialog.dialogs = _NoPrompts()
    dialog._page(controller.start())
    dialog.source = SimpleNamespace(library=library, workspace=workspace,
                                    edition=book['edition'], state=state, raw=raw)
    yield dialog
    dialog.close()


def _assert_first_page_unchanged(reader):
    assert reader.text.value == reader.source.raw.split('\f')[0]
    assert reader.controller.current.ordinal == 1
    assert [location.index for location in reader.controller.history] == [1]
    assert reader.source.state.resume(reader.source.workspace, reader.source.edition).index == 1
    assert reader.source.library._selections == {}


def _deliver(reader):
    while reader.wx_queue:
        reader.wx_queue.pop(0)()


@pytest.mark.parametrize('command', [
    lambda reader: reader.navigate(1),
    lambda reader: reader.go_first(),
    lambda reader: reader.history(-1),
    lambda reader: reader.go_to(),
    lambda reader: reader.where(),
    lambda reader: reader.add_bookmark(),
    lambda reader: reader.manage_bookmarks(),
    lambda reader: reader.help(),
], ids=['next', 'first', 'back', 'go-to', 'where', 'add-bookmark', 'manage-bookmarks', 'help'])
def test_busy_command_cannot_overlap_work_or_undo_pending_cancellation(reader, command):
    reader.navigate(1)
    assert reader.busy and len(reader.executor.jobs) == 1
    assert all(not button.enabled for button in reader.buttons)
    assert reader.cancel.enabled
    reader.cancelled.set()

    command(reader)

    assert reader.cancelled.is_set(), 'A rejected command must not clear the current cancellation.'
    assert len(reader.executor.jobs) == 1
    assert reader.busy
    _assert_first_page_unchanged(reader)
    reader.executor.complete()
    _deliver(reader)
    assert not reader.busy and not reader.cancel.enabled
    assert all(button.enabled for button in reader.buttons)
    assert 'cancelled' in reader.status.value.lower()
    _assert_first_page_unchanged(reader)


def test_worker_completion_alone_never_advances_display_resume_or_history(reader):
    reader.navigate(1)
    reader.executor.complete()
    assert len(reader.wx_queue) == 1
    _assert_first_page_unchanged(reader)

    _deliver(reader)

    assert not reader.busy
    assert reader.text.value == reader.source.raw.split('\f')[1]
    assert reader.controller.current.ordinal == 2
    assert reader.source.state.resume(reader.source.workspace, reader.source.edition).index == 2
    assert [location.index for location in reader.controller.history] == [1, 2]


def test_cancel_after_worker_completion_rejects_queued_page_without_persisting_it(reader):
    reader.navigate(1)
    reader.executor.complete()
    reader.cancelled.set()

    _deliver(reader)

    _assert_first_page_unchanged(reader)
    assert not reader.busy
    assert 'cancelled' in reader.status.value.lower()


def test_changed_scope_after_worker_completion_rejects_queued_page(reader):
    reader.navigate(1)
    reader.executor.complete()

    def changed_scope():
        raise ValueError('Active library changed before source display.')

    reader.controller.guard = changed_scope
    _deliver(reader)

    _assert_first_page_unchanged(reader)
    assert 'library changed' in reader.status.value
    assert not reader.busy


def test_close_after_worker_completion_never_touches_destroyed_native_controls(reader):
    reader.navigate(1)
    reader.executor.complete()
    assert reader.wx_queue
    reader.close()
    for control in [reader.text, reader.location, reader.status, reader.cancel, *reader.buttons]:
        control.destroyed = True

    _deliver(reader)

    _assert_first_page_unchanged(reader)
    assert reader.cancelled.is_set() and reader.executor.stopped


def test_closed_reader_cannot_submit_another_navigation(reader):
    reader.close()

    reader.navigate(1)
    reader.go_first()
    reader.history(-1)
    reader.where()
    reader.manage_bookmarks()

    assert reader.executor.jobs == []
    _assert_first_page_unchanged(reader)


def test_close_cancels_unstarted_work_without_late_ui_delivery(reader):
    reader.navigate(1)
    reader.close()
    reader.executor.complete()

    assert reader.wx_queue == []
    _assert_first_page_unchanged(reader)


def test_submission_failure_restores_controls_without_false_navigation(reader):
    def failed(_operation):
        raise RuntimeError('Synthetic executor unavailable.')

    reader.executor.submit = failed
    reader.navigate(1)

    assert not reader.busy and not reader.cancel.enabled
    assert all(button.enabled for button in reader.buttons)
    assert 'executor unavailable' in reader.status.value
    _assert_first_page_unchanged(reader)
