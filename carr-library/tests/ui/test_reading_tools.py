"""Review-session lifecycle, native offset mapping and one shared keymap."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.keybindings import Command, KeybindingStore, validate_bindings
from carr_library.speech.reading import ReadingDocument, PronunciationRule, export_dictionary
from carr_library.speech.reading_tools import ReadingSnapshot, ReadingMatch, preview_matches
from carr_library.ui.reading_tools import (ReaderEditHost, ReaderToolSession, native_position,
                                           reading_selection, select_reading_range)
from carr_library.ui.keyboard_settings import (VoiceReaderKeyboardAdapter, reader_commands,
                                               voice_reader_commands)


class NativeText:
    """Model the documented Windows CRLF/UTF-16 coordinates, not Python indices."""
    def __init__(self, value, windows):
        self.value, self.windows = value, windows
        self.selection = (0, 0)
        self.shown = None

    def GetValue(self):
        return self.value

    def _units(self, value):
        return len(value.replace('\n', '\r\n').encode('utf-16-le')) // 2 if self.windows else len(value)

    def GetRange(self, start, end):
        if self.windows:
            raw = self.value.replace('\n', '\r\n').encode('utf-16-le')
            return raw[2 * start:2 * end].decode('utf-16-le').replace('\r\n', '\n')
        return self.value[start:end]

    def XYToPosition(self, column, row):
        lines = self.value.split('\n')
        if row >= len(lines):
            return -1
        return self._units('\n'.join(lines[:row]) + ('\n' if row else '')) + column

    def GetSelection(self):
        return self.selection

    def SetSelection(self, start, end):
        self.selection = start, end

    def ShowPosition(self, position):
        self.shown = position


@pytest.mark.parametrize('windows', [False, True])
@pytest.mark.parametrize('value', ['first\nsecond', '😀 Україна\nA', 'a\n\n😀b', 'line\n'])
def test_each_unicode_codepoint_boundary_roundtrips_native_positions(value, windows):
    field = NativeText(value, windows)
    for offset in range(len(value) + 1):
        position = native_position(field, offset, windows=windows)
        assert field.GetRange(0, position) == value[:offset]


def test_native_selection_to_python_indices_and_back():
    field = NativeText('😀 alpha\nβ second', sys.platform == 'win32')
    select_reading_range(field, 2, 10)
    assert reading_selection(field) == (2, 10)
    assert field.GetRange(*field.GetSelection()) == field.GetValue()[2:10]
    assert field.shown == field.GetSelection()[0]


def test_invalid_native_position_does_not_move_selection():
    field = NativeText('a\n😀b', False)
    field.XYToPosition = lambda *args: -1
    original = field.GetSelection()
    with pytest.raises(ValueError, match='Unicode'):
        select_reading_range(field, 2, 3)
    assert field.GetSelection() == original


class BorrowedHost:
    def __init__(self):
        self.document = ReadingDocument('Original A A')
        self.main = threading.get_ident()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = []
        self.running = False
        self.open = True
        self.revoked = False
        self.cancel = threading.Event()
        self.messages = []
        self.selection = (0, 0)
        self.applied = []
        self.host = ReaderEditHost(capture=self.capture, selection=lambda: self.selection,
            select=self.select, verify=self.verify, apply=self.apply, submit=self.submit,
            guard=self.guard, report=lambda message, **kwargs: self.messages.append(message),
            alive=lambda: self.open, busy=lambda: self.running, cancelled=self.cancel.is_set,
            voice=lambda: ('en', 'local:fixture', 'Fixture voice'))

    def guard(self):
        assert threading.get_ident() == self.main, 'A UI source guard ran in a worker.'
        if not self.open or self.revoked:
            raise ValueError('Source scope revoked.')

    def capture(self):
        self.guard()
        return ReadingSnapshot.capture(self.document)

    def verify(self, snapshot):
        self.guard()
        snapshot.check(self.document)

    def select(self, start, end):
        self.guard()
        self.selection = start, end

    def apply(self, snapshot, start, end, replacement, *, label):
        self.verify(snapshot)
        self.applied.append((start, end, replacement))
        return self.document.replace(start, end, replacement, label=label)

    def submit(self, work, done, *, on_error=None):
        self.guard()
        if self.running:
            return False
        self.running = True
        self.cancel.clear()
        future = self.pool.submit(work)
        self.pending.append((future, done, on_error))
        return True

    def finish(self):
        future, done, on_error = self.pending.pop(0)
        try:
            result = future.result(timeout=5)
        except BaseException as exc:
            self.running = False
            if on_error is None:
                raise
            on_error(exc)
        else:
            self.running = False
            done(result)

    def close(self):
        self.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def owner():
    value = BorrowedHost()
    try:
        yield value
    finally:
        value.close()


def operation(owner):
    def prepare(snapshot, cancelled):
        assert threading.get_ident() != owner.main
        assert not cancelled()
        return preview_matches(snapshot, (ReadingMatch(9, 10, 'B'),))
    return prepare


def start_preview(owner, session, work=None):
    return session.start(work or operation(owner),
                         lambda snapshot, value: setattr(session, 'preview', value))


def test_worker_only_prepares_and_explicit_accept_is_one_undo(owner):
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    assert start_preview(owner, session)
    owner.finish()
    assert owner.document.text == 'Original A A' and not owner.applied
    assert session.preview is not None
    assert session.accept()
    assert owner.document.text == 'Original B A'
    assert owner.applied == [(9, 10, 'B')]
    assert owner.document.undo() and owner.document.text == 'Original A A'


@pytest.mark.parametrize('change', ['edit', 'revoke', 'close', 'cancel'])
def test_late_result_never_applies_after_changed_authority_or_cancellation(owner, change):
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    assert start_preview(owner, session)
    owner.pending[0][0].result(timeout=5)
    if change == 'edit':
        owner.document.set_text('Newer reading edit')
    elif change == 'revoke':
        owner.revoked = True
    elif change == 'close':
        session.close()
    else:
        session.cancel()
    expected = owner.document.text
    owner.finish()
    assert session.preview is None and not owner.applied
    assert owner.document.text == expected
    if change != 'close':
        assert owner.messages


def test_closing_tool_cancels_real_worker_without_shutting_borrowed_executor(owner):
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    def worker(snapshot, cancelled):
        entered.set()
        assert release.wait(3)
        if cancelled():
            observed.set()
            raise InterruptedError('Closed tool.')
        pytest.fail('Closed tool did not cancel its worker.')
    try:
        assert start_preview(owner, session, worker)
        assert entered.wait(3)
        session.close()
        release.set()
        owner.finish()
        assert observed.is_set() and not owner.applied
        # Tool closure never owns shutdown of the speech reader's executor.
        assert owner.pool.submit(lambda: 'still-owned-by-reader').result(timeout=3) == 'still-owned-by-reader'
    finally:
        release.set()


def test_scope_is_rechecked_between_preview_and_accept(owner):
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    start_preview(owner, session)
    owner.finish()
    owner.revoked = True
    with pytest.raises(ValueError, match='revoked'):
        session.accept()
    assert not owner.applied and owner.document.text == 'Original A A'


def test_changed_rule_invalidates_previous_preview(owner):
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    start_preview(owner, session)
    owner.finish()
    session.invalidate()
    with pytest.raises(ValueError, match='current preview'):
        session.accept()
    assert not owner.applied


def test_source_failure_before_submission_does_not_queue_work(owner):
    owner.revoked = True
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    assert not start_preview(owner, session)
    assert not owner.pending and not session.busy
    assert 'revoked' in owner.messages[-1]


def test_proven_dictionary_export_is_not_relabelled_cancelled_after_commit(owner, tmp_path):
    target = tmp_path / 'rules.json'
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    completed = []
    def export(snapshot, cancelled):
        export_dictionary((PronunciationRule('A', 'B'),), target, cancelled=cancelled)
        return target
    assert session.start(export, lambda snapshot, value: completed.append(value), durable=True)
    owner.pending[0][0].result(timeout=5)
    assert target.is_file()
    session.cancel()
    owner.finish()
    assert completed == [target]
    assert not owner.applied and owner.document.source_text == 'Original A A'


@pytest.mark.parametrize('key', ['Ctrl+A', 'Ctrl+Left', 'Ctrl+Shift+Home',
                               'Enter', 'NVDA+F3', 'Insert', 'CapsLock'])
def test_voice_reader_context_preserves_typing_and_screen_reader_keys(key):
    with pytest.raises(ValueError):
        validate_bindings(voice_reader_commands(), {'voice_reader.find': key})


def test_same_store_preserves_main_and_reading_desk_preferences(tmp_path):
    commands = (Command('host.open', 'Open host', 'Ctrl+O'),) + reader_commands() + voice_reader_commands()
    store = KeybindingStore(tmp_path)
    old = {'host.open': 'Ctrl+Shift+O', reader_commands()[0].identity: ''}
    store.save(commands, old)
    updated = {**store.load(), 'voice_reader.find': 'Ctrl+Shift+J', 'voice_reader.replace': ''}
    store.save(commands, updated)
    assert KeybindingStore(tmp_path).load() == updated
    values = validate_bindings(commands, updated)
    assert values['host.open'] == 'Ctrl+Shift+O'
    assert values['voice_reader.find'] == 'Ctrl+Shift+J' and values['voice_reader.replace'] == ''
    # Identical chords are allowed in distinct active window contexts.
    assert validate_bindings(commands, {'voice_reader.find': 'Ctrl+O'})['host.open'] == 'Ctrl+O'
    with pytest.raises(ValueError, match='already assigned'):
        validate_bindings(commands, {'voice_reader.find': 'Ctrl+H'})


def adapter(owner, *, revoked=False):
    value = object.__new__(VoiceReaderKeyboardAdapter)
    value.closed = False
    value.library = SimpleNamespace(root=Path('/captured-library'))
    value.parent = SimpleNamespace(library=value.library, _closing=False)
    value.root = value.library.root
    value.manager = None
    value.alive = lambda: owner.open
    calls = []
    enabled = SimpleNamespace(value=True)
    button = SimpleNamespace(IsEnabled=lambda: enabled.value)
    value.controls = {identity: (button, lambda event, key=identity: calls.append(key))
                      for identity in ('voice_reader.find', 'voice_reader.stop')}
    value.report = lambda message, **kwargs: owner.messages.append(message)
    value.guard = owner.guard
    owner.revoked = revoked
    return value, calls, enabled


def test_stop_remains_available_after_source_revocation(owner):
    value, calls, _ = adapter(owner, revoked=True)
    assert not value.invoke('voice_reader.find')
    assert value.invoke('voice_reader.stop')
    assert calls == ['voice_reader.stop']


@pytest.mark.parametrize('change', ['library', 'close', 'disabled'])
def test_reader_context_cannot_dispatch_to_stale_or_disabled_controls(owner, change):
    value, calls, enabled = adapter(owner)
    if change == 'library':
        value.parent.library = SimpleNamespace(root=value.root)
    elif change == 'close':
        value.closed = True
    else:
        enabled.value = False
    assert not value.invoke('voice_reader.find')
    assert not calls


def test_destroyed_owner_cancels_session_without_touching_dead_controls(owner):
    from carr_library.ui.reading_tools import ReadingToolsDialog
    view = object.__new__(ReadingToolsDialog)
    view._window_id, view.destroyed = 71, False
    session = ReaderToolSession(owner.host, report=owner.messages.append)
    view.session = session
    skipped = []
    child = SimpleNamespace(GetId=lambda: 72, Skip=lambda: skipped.append('child'))
    parent = SimpleNamespace(GetId=lambda: 71, Skip=lambda: skipped.append('owner'))
    start_preview(owner, session)
    owner.pending[0][0].result(timeout=5)
    view._destroyed(child)
    assert not session.closed and not view.destroyed
    view._destroyed(parent)
    assert session.closed and session.cancelled.is_set() and view.destroyed
    # A queued completion must not call any view callback after native deletion.
    session.changed = lambda: pytest.fail('Destroyed control was refreshed.')
    session.report = lambda message: pytest.fail('Destroyed control was reported through.')
    owner.finish()
    assert session.preview is None and not owner.applied
    assert skipped == ['child', 'owner']


def test_modal_finally_does_not_destroy_an_already_invalidated_wrapper(owner):
    from carr_library.ui.reading_tools import ReadingToolsDialog
    view = object.__new__(ReadingToolsDialog)
    view._window_id, view.destroyed, view.rules = 80, False, []
    view.session = ReaderToolSession(owner.host, report=owner.messages.append)
    view.pattern = SimpleNamespace(SetFocus=lambda: None)
    event = SimpleNamespace(GetId=lambda: 80, Skip=lambda: None)
    view.dialog = SimpleNamespace(ShowModal=lambda: view._destroyed(event),
        Destroy=lambda: pytest.fail('Destroyed SIP wrapper was called again.'))
    assert view.show() == ()
    assert view.session.closed and view.destroyed
