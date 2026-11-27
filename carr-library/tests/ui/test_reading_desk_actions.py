"""Real-library reader/controller checks with explicit native-dialog doubles."""
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.reading_desk import (
    ReaderController, ReadingActions, _chosen, location_text,
)


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reading library')
    raw = 'Перший сегмент.\r\nPreserved line.\fSecond source segment.\fTretí segment.'
    book = app.add_text(workspace, 'Source book', raw, authors=('Reader test author',))
    desk = ReadingDesk(app, workspace, book['edition'])
    return app, workspace, book, desk, raw


def test_full_source_reading_resume_and_session_history(source):
    app, workspace, book, desk, raw = source
    state = ReadingState(app.root)
    reader = ReaderController(desk, state)
    assert reader.start().raw_text == raw.split('\f')[0]
    assert reader.move(1).ordinal == 2
    assert reader.go(3).raw_text == raw.split('\f')[2]
    assert reader.history_move(-1).ordinal == 2
    assert reader.history_move(-1).ordinal == 1
    assert reader.history_move(1).ordinal == 2
    reopened = ReaderController(ReadingDesk(app, workspace, book['edition']), ReadingState(app.root))
    assert reopened.start().ordinal == 2
    assert 'screen reader cursor' in location_text(reopened.current)
    assert app._selections == {}


def test_history_forward_tail_is_replaced_after_new_navigation(source):
    app, _, _, desk, _ = source
    reader = ReaderController(desk, ReadingState(app.root))
    reader.start()
    reader.go(2)
    reader.go(3)
    reader.history_move(-1)
    reader.go(1)
    with pytest.raises(ValueError, match='no further'):
        reader.history_move(1)
    assert [location.index for location in reader.history] == [1, 2, 1]


@pytest.mark.parametrize('bad', [-1, 0, 4, True, 1.0, '1', None])
def test_invalid_reading_position_does_not_change_resume(source, bad):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    reader = ReaderController(desk, state)
    first = reader.start()
    with pytest.raises(ValueError):
        reader.go(bad)
    assert reader.current == first
    assert state.resume(workspace, book['edition']) == first.location
    assert reader.history == [first.location]


def test_stale_resume_is_not_silently_retargeted(source):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    stale = replace(desk.page(2).location, revision='old-revision')
    state.remember(stale)
    reader = ReaderController(desk, state)
    with pytest.raises(ValueError, match='different'):
        reader.start()
    assert reader.current is None
    assert state.resume(workspace, book['edition']) == stale
    assert reader.go(1).ordinal == 1  # Explicit Start at first source location.
    assert state.resume(workspace, book['edition']) == reader.current.location


def test_failed_resume_save_keeps_reading_available_without_claiming_saved(source, monkeypatch):
    app, workspace, book, desk, raw = source
    state = ReadingState(app.root)
    reader = ReaderController(desk, state)
    reader.start()
    previous = state.resume(workspace, book['edition'])

    def failed(_location, **kwargs):
        raise OSError('Synthetic disk full')
    monkeypatch.setattr(state, 'remember', failed)
    assert reader.go(2).raw_text == raw.split('\f')[1]
    assert 'could not be saved' in reader.save_warning
    assert state.resume(workspace, book['edition']) == previous


def test_late_cancel_before_native_display_never_moves_resume_or_history(source):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    cancelled = Event()
    reader = ReaderController(desk, state, cancelled=cancelled.is_set, defer_display=True)
    first = reader.start()
    assert state.resume(workspace, book['edition']) is None
    reader.accept(first)  # Native _page calls accept only after showing text.
    pending = reader.go(2)
    assert reader.current.ordinal == 1 and len(reader.history) == 1
    cancelled.set()
    with pytest.raises(TimeoutError):
        reader.accept(pending)
    assert reader.current.ordinal == 1 and len(reader.history) == 1
    assert state.resume(workspace, book['edition']).index == 1


def test_scope_change_before_native_display_never_saves_unseen_page(source):
    app, workspace, book, desk, _ = source
    reader = ReaderController(desk, ReadingState(app.root), defer_display=True)
    pending = reader.start()

    def changed():
        raise ValueError('Library changed')
    reader.guard = changed
    with pytest.raises(ValueError, match='Library changed'):
        reader.accept(pending)
    assert reader.current is None and not reader.history
    assert ReadingState(app.root).resume(workspace, book['edition']) is None


def test_archived_workspace_cannot_list_or_modify_existing_bookmark(source):
    app, workspace, _, desk, _ = source
    reader = ReaderController(desk, ReadingState(app.root))
    location = reader.start().location
    bookmark = reader.add_bookmark('Private bookmark', location)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        reader.bookmarks()
    with pytest.raises(ValueError, match='archived'):
        reader.rename_bookmark(bookmark, 'Changed')


def test_bookmark_lifecycle_preserves_source_and_binds_exact_location(source):
    app, workspace, book, desk, raw = source
    reader = ReaderController(desk, ReadingState(app.root))
    second = reader.go(2)
    bookmark = reader.add_bookmark('Review this section', second.location)
    reader.go(1)
    assert reader.open_bookmark(bookmark) == second
    renamed = reader.rename_bookmark(bookmark, 'Seminar reading')
    assert renamed.location == second.location and renamed.label == 'Seminar reading'
    with pytest.raises(ValueError, match='changed'):
        reader.delete_bookmark(bookmark)
    reader.delete_bookmark(renamed)
    assert reader.bookmarks() == ()
    assert [desk.page(index).raw_text for index in range(1, 4)] == raw.split('\f')
    assert app._selections == {}


def test_changed_selection_cannot_save_bookmark_for_different_page(source):
    app, _, _, desk, _ = source
    reader = ReaderController(desk, ReadingState(app.root))
    first = reader.start()
    reader.go(2)
    with pytest.raises(ValueError, match='position changed'):
        reader.add_bookmark('Wrong page', first.location)
    assert reader.bookmarks() == ()


def test_cancelled_or_changed_scope_never_saves_or_changes_history(source):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    cancelled = Event()
    reader = ReaderController(desk, state, cancelled=cancelled.is_set)
    initial = reader.start()
    cancelled.set()
    with pytest.raises(TimeoutError):
        reader.go(2)
    assert reader.current == initial and len(reader.history) == 1
    cancelled.clear()

    def changed():
        raise ValueError('Library changed')
    reader.guard = changed
    with pytest.raises(ValueError, match='Library changed'):
        reader.go(2)
    assert state.resume(workspace, book['edition']) == initial.location


@pytest.mark.parametrize('index', [-1, 2, True, 0.0, None])
def test_native_choice_cannot_use_negative_or_coerced_selection(index):
    with pytest.raises(ValueError, match='Choose'):
        _chosen(index, ('first', 'second'))


class Control:
    def __init__(self):
        self.focused = False
    def SetFocus(self):
        self.focused = True


class Frame:
    def __init__(self, app, workspace):
        self.library, self.active = app, (workspace, None)
        self.working = self.busy = self._closing = False
        self.cancel_event = Event()
        self.choice, self.status, self.books = Control(), Control(), Control()
        rows = {row.edition_id: row for row in app.browse(workspace)}
        self.selected = next(iter(rows), None)
        self.catalog = SimpleNamespace(workspace=workspace, ready=True, rows=rows, books=self.books,
                                       _selection=lambda: (None, None, self.selected))
        self.book = SimpleNamespace(GetPage=lambda _i: self.catalog)
        self.evidence = SimpleNamespace(note='Unsaved private note')
        self.messages, self.jobs, self.pending = [], [], None
        self.before_operation = None

    def report(self, message):
        self.messages.append(message)

    def run_job(self, title, operation, done=None, failed=None):
        self.jobs.append(title)
        self.cancel_event.clear()
        if self.before_operation is not None:
            callback, self.before_operation = self.before_operation, None
            callback()
        try:
            result = operation()
            if done is not None:
                done(result)
        except Exception as exc:
            if failed is None:
                raise
            failed(exc)


class Dialogs:
    def __init__(self, answer=None, path=None, during=None):
        self.answer, self.chosen_path, self.during = answer, path, during
    def choose(self, *args):
        if self.during:
            callback, self.during = self.during, None
            callback()
        return self.answer
    def path(self, *args, **kwargs):
        if self.during:
            callback, self.during = self.during, None
            callback()
        return self.chosen_path

    def approve_export(self, parent, path, *, private_root):
        from carr_library.research.export_file import capture_export_destination
        return capture_export_destination(path, private_root=private_root)


def test_open_selected_book_preserves_unsaved_notes_and_restores_focus(source):
    app, workspace, _, _, raw = source
    frame = Frame(app, workspace)
    opened = []

    class Reader:
        def __init__(self, parent, desk, state, guard):
            self.reader = ReaderController(desk, state, guard)
        def show(self):
            opened.append(self.reader.start().raw_text)
    ReadingActions(frame, reader_factory=Reader).open_book()
    assert opened == [raw.split('\f')[0]]
    assert frame.evidence.note == 'Unsaved private note'
    assert frame.books.focused


def test_stale_scope_cannot_open_previous_library(source):
    app, workspace, _, _, _ = source
    frame = Frame(app, workspace)
    frame.before_operation = lambda: setattr(frame, 'active', ('another', None))
    ReadingActions(frame, reader_factory=lambda *args: pytest.fail('Must not show stale reader')).open_book()
    assert 'changed' in frame.messages[-1]
    assert not frame.books.focused


def test_negative_book_chooser_does_not_open_last_book(source):
    app, workspace, _, _, _ = source
    frame = Frame(app, workspace)
    frame.selected = None
    ReadingActions(frame, dialogs=Dialogs(-1),
                   reader_factory=lambda *args: pytest.fail('No book was selected')).open_book()
    assert 'Choose one item' in frame.messages[-1]


def test_reading_export_import_uses_real_guarded_writer_and_retains_notes(source, tmp_path):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    state.remember(desk.page(2).location)
    item = state.add_bookmark(desk.page(3).location, 'Seminar')
    target = tmp_path / 'reading.json'
    frame = Frame(app, workspace)
    actions = ReadingActions(frame, dialogs=Dialogs(path=target))
    actions.export_state()
    assert target.is_file() and 'Seminar' in target.read_text(encoding='utf-8')
    state.delete_bookmark(workspace, book['edition'], item.id)
    assert not state.bookmarks(workspace, book['edition'])
    actions.import_state()
    assert state.bookmarks(workspace, book['edition'])[0].label == 'Seminar'
    assert frame.evidence.note == 'Unsaved private note'


def test_state_export_does_not_overwrite_private_database(source):
    app, workspace, _, _, _ = source
    before = app.database.read_bytes()
    frame = Frame(app, workspace)
    ReadingActions(frame, dialogs=Dialogs(path=app.database)).export_state()
    assert app.database.read_bytes() == before
    assert 'outside' in frame.messages[-1]


def test_import_rejects_stale_source_before_bookmark_publication(source, tmp_path):
    app, workspace, book, desk, _ = source
    state = ReadingState(app.root)
    item = state.add_bookmark(desk.page(3).location, 'Stale bookmark')
    target = tmp_path / 'stale.json'
    target.write_text(state.export_state(workspace), encoding='utf-8')
    state.delete_bookmark(workspace, book['edition'], item.id)
    app.archive_workspace(workspace)
    frame = Frame.__new__(Frame)
    frame.library, frame.active = app, (workspace, None)
    frame.working = frame.busy = frame._closing = False
    frame.cancel_event, frame.messages, frame.jobs = Event(), [], []
    frame.before_operation = None
    ReadingActions(frame, dialogs=Dialogs(path=target)).import_state()
    assert 'archived' in frame.messages[-1]
    assert state.bookmarks(workspace, book['edition']) == ()


class _DeferredExportFrame(Frame):
    """Use real worker threads while the test owns native callback delivery."""
    def run_job(self, title, operation, done=None, failed=None):
        assert not self.working
        self.working = True
        self.cancel_event.clear()
        self.jobs.append(SimpleNamespace(title=title, operation=operation, done=done,
                                         failed=failed, result=None, error=None))

    def work(self, index):
        from concurrent.futures import ThreadPoolExecutor
        job = self.jobs[index]
        with ThreadPoolExecutor(max_workers=1) as worker:
            try:
                job.result = worker.submit(job.operation).result()
            except Exception as exc:
                job.error = exc

    def complete(self, index):
        job = self.jobs[index]
        self.working = False
        if self._closing:
            return
        try:
            if job.error is not None:
                raise job.error
            if job.done is not None:
                job.done(job.result)
        except Exception as exc:
            if job.failed is None:
                raise
            job.failed(exc)

    def finish(self, index):
        self.work(index)
        self.complete(index)


def _reading_export(source, tmp_path, monkeypatch, *, existing=True):
    import sys
    from unittest.mock import Mock
    app, workspace, _, _, _ = source
    target = tmp_path / 'reading-output.json'
    if existing:
        target.write_bytes(b'Existing independent work')
    wx = SimpleNamespace(YES=1, NO=2, YES_NO=4, NO_DEFAULT=8, ICON_QUESTION=16)
    wx.MessageBox = Mock(return_value=wx.YES)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    frame = _DeferredExportFrame(app, workspace)
    actions = ReadingActions(frame, dialogs=Dialogs(path=target))
    return frame, actions, target, wx


def test_reading_export_captures_and_publishes_in_workers_with_ui_confirmation(
        source, tmp_path, monkeypatch):
    from threading import get_ident
    from carr_library.ui import export_destination, reading_desk
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch)
    main, observed, approvals = get_ident(), [], []
    actual_capture = export_destination.capture_export_destination
    actual_write = reading_desk.write_export

    def capture(*args, **kwargs):
        assert get_ident() != main
        approval = actual_capture(*args, **kwargs)
        approvals.append(approval)
        observed.append('capture')
        return approval

    def publish(*args, **kwargs):
        assert get_ident() != main
        assert kwargs['approval'] is approvals[0]
        observed.append('publish')
        return actual_write(*args, **kwargs)

    def consent(*args):
        assert get_ident() == main
        assert observed == ['capture']
        return wx.YES

    monkeypatch.setattr(export_destination, 'capture_export_destination', capture)
    monkeypatch.setattr(reading_desk, 'write_export', publish)
    wx.MessageBox.side_effect = consent
    actions.export_state()
    assert not observed and len(frame.jobs) == 1
    frame.work(0)
    wx.MessageBox.assert_not_called()
    assert target.read_bytes() == b'Existing independent work'
    frame.complete(0)
    assert len(frame.jobs) == 2 and observed == ['capture']
    frame.finish(1)
    assert observed == ['capture', 'publish']
    assert target.read_text(encoding='utf-8') == ReadingState(frame.library.root).export_state(frame.active[0])
    assert frame.messages[-1] == 'Reading positions and page bookmarks exported.'
    assert frame.evidence.note == 'Unsaved private note'


@pytest.mark.parametrize('stage', ['before_capture', 'after_capture', 'in_confirmation'])
def test_reading_export_cancel_before_publication_keeps_existing_target(
        source, tmp_path, monkeypatch, stage):
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch)
    if stage == 'in_confirmation':
        def consent(*args):
            frame.cancel_event.set()
            return wx.YES
        wx.MessageBox.side_effect = consent
    actions.export_state()
    if stage == 'before_capture':
        frame.cancel_event.set()
    frame.work(0)
    if stage == 'after_capture':
        frame.cancel_event.set()
    frame.complete(0)
    assert len(frame.jobs) == 1 and frame.cancel_event.is_set()
    assert target.read_bytes() == b'Existing independent work'
    assert 'not exported' in frame.messages[-1]


@pytest.mark.parametrize('change', ['library', 'topic', 'close'])
def test_reading_export_stale_capture_never_confirms_or_publishes(
        source, tmp_path, monkeypatch, change):
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch)
    actions.export_state()
    frame.work(0)
    if change == 'library':
        frame.library = object()
    elif change == 'topic':
        frame.active = (frame.active[0], 'another-topic')
    else:
        frame._closing = True
    frame.complete(0)
    wx.MessageBox.assert_not_called()
    assert len(frame.jobs) == 1 and target.read_bytes() == b'Existing independent work'


@pytest.mark.parametrize('existing', [False, True])
def test_reading_export_changed_destination_after_consent_is_preserved(
        source, tmp_path, monkeypatch, existing):
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch, existing=existing)
    actions.export_state()
    frame.finish(0)
    assert len(frame.jobs) == 2
    target.write_bytes(b'Later independent work')
    frame.finish(1)
    assert target.read_bytes() == b'Later independent work'
    assert 'not exported' in frame.messages[-1] and 'changed' in frame.messages[-1]
    assert not list(target.parent.glob('.research-export-*'))


def test_reading_export_declined_consent_does_not_render(source, tmp_path, monkeypatch):
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch)
    wx.MessageBox.return_value = wx.NO
    actions.export_state()
    frame.finish(0)
    assert len(frame.jobs) == 1 and target.read_bytes() == b'Existing independent work'
    assert frame.messages[-1] == 'Reading state export cancelled.'


@pytest.mark.parametrize('late_cancel', [False, True])
def test_reading_export_saved_receipt_and_notice_survive_late_cancel(
        source, tmp_path, monkeypatch, late_cancel):
    from carr_library.research.export_file import ExportReceipt
    from carr_library.ui import reading_desk
    frame, actions, target, wx = _reading_export(source, tmp_path, monkeypatch)
    actual_write = reading_desk.write_export
    warning = 'The file is saved. Temporary-file cleanup is still pending; do not repeat the export.'

    def publish(*args, **kwargs):
        receipt = actual_write(*args, **kwargs)
        if late_cancel:
            frame.cancel_event.set()
        return ExportReceipt(receipt.path, warning)

    monkeypatch.setattr(reading_desk, 'write_export', publish)
    actions.export_state()
    frame.finish(0)
    frame.finish(1)
    assert target.read_text(encoding='utf-8') == ReadingState(frame.library.root).export_state(frame.active[0])
    assert frame.messages[-1] == 'Reading positions and page bookmarks exported. ' + warning
    assert len(frame.jobs) == 2
