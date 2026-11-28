"""Actual borrowed worker, scope identity and shared-keymap navigation controls."""
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.keybindings import Command, KeybindingStore, validate_bindings
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.reading_tools import ReadingSnapshot
from carr_library.ui import reader_navigation
from carr_library.ui.keyboard_settings import VoiceReaderKeyboardAdapter, voice_reader_commands
from carr_library.ui.reading_tools import (ReaderEditHost, native_position,
                                          reading_selection, select_reading_range)

TEXT = '😀 Ім’я читача. Друге речення!\nДругий рядок.\n\nОстанній абзац.'
EXPECTED = {
    'word': 'Ім’я',
    'line': '😀 Ім’я читача. Друге речення!',
    'sentence': '😀 Ім’я читача.',
    'paragraph': '😀 Ім’я читача. Друге речення!\nДругий рядок.',
}


class NativeText:
    def __init__(self, owner, text):
        self.owner, self.text, self.selection = owner, text, (0, 0)
        self.shown = None

    def GetValue(self):
        self.owner.gui()
        return self.text

    def GetRange(self, start, end):
        self.owner.gui()
        if sys.platform == 'win32':
            raw = self.text.replace('\n', '\r\n').encode('utf-16-le')
            return raw[start * 2:end * 2].decode('utf-16-le').replace('\r\n', '\n')
        return self.text[start:end]

    def XYToPosition(self, column, row):
        self.owner.gui()
        lines = self.text.split('\n')
        if not 0 <= row < len(lines):
            return -1
        prefix = '\n'.join(lines[:row]) + ('\n' if row else '')
        units = (len(prefix.replace('\n', '\r\n').encode('utf-16-le')) // 2
                 if sys.platform == 'win32' else len(prefix))
        return units + column

    def GetSelection(self):
        self.owner.gui()
        return self.selection

    def SetSelection(self, start, end):
        self.owner.gui()
        self.selection = start, end

    def ShowPosition(self, value):
        self.owner.gui()
        self.shown = value


class BorrowedReader:
    def __init__(self):
        self.main = threading.get_ident()
        self.document = ReadingDocument(TEXT)
        self.open, self.revoked, self.running = True, False, False
        self.cancel = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='existing-reader')
        self.pending, self.messages, self.spoken = [], [], []
        self.invalid_touches = []
        self.field = NativeText(self, TEXT)
        self.focused = 0
        self.on_focus = lambda: None
        self.host = ReaderEditHost(capture=self.capture, selection=lambda: reading_selection(self.field),
            select=lambda first, last: select_reading_range(self.field, first, last),
            verify=self.verify, apply=self.forbidden_apply, submit=self.submit, guard=self.guard,
            report=self.report, alive=lambda: self.open, busy=lambda: self.running,
            cancelled=self.cancel.is_set, voice=lambda: ('uk', 'fixture:local', 'Fixture voice'))
        self.navigation = reader_navigation.ReaderNavigation(self.host, speak=self.speak, focus=self.focus)
        self.place(TEXT.index('Ім’я') + 1)

    def gui(self):
        assert threading.get_ident() == self.main, 'A native/source callback ran on the worker.'
        if not self.open:
            self.invalid_touches.append('native')
            raise AssertionError('A destroyed reader was touched.')

    def guard(self):
        if not self.open:
            raise ValueError('The reader was closed.')
        self.gui()
        if self.revoked:
            raise ValueError('Source scope revoked.')

    def capture(self):
        self.guard()
        self.document.set_text(self.field.text)
        return ReadingSnapshot.capture(self.document)

    def verify(self, snapshot):
        self.capture()
        snapshot.check(self.document)

    def forbidden_apply(self, *args, **kwargs):
        pytest.fail('Navigation must not rewrite the reading copy.')

    def report(self, message, **kwargs):
        self.gui()
        self.messages.append(message)

    def place(self, position):
        select_reading_range(self.field, position, position)

    def focus(self):
        self.gui()
        self.focused += 1
        self.on_focus()

    def speak(self):
        self.guard()
        selection = reading_selection(self.field)
        assert selection[0] < selection[1], 'Empty scope must never fall back to all text.'
        self.spoken.append((self.field.GetRange(*self.field.GetSelection()), selection,
                            self.document.source_sha256, self.document))

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
        future, done, failed = self.pending.pop(0)
        try:
            result = future.result(timeout=5)
        except BaseException as exc:
            self.running = False
            if failed is None:
                raise
            failed(exc)
        else:
            self.running = False
            done(result)

    def close(self):
        self.cancel.set()
        self.navigation.close()
        self.pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def reader():
    value = BorrowedReader()
    try:
        yield value
    finally:
        value.close()
        assert not value.invalid_touches


@pytest.mark.parametrize('unit', tuple(EXPECTED))
@pytest.mark.parametrize('read_aloud', [False, True], ids=['select-only', 'explicit-read'])
def test_current_unit_uses_actual_borrowed_worker_then_exact_native_selection(reader, monkeypatch,
                                                                           unit, read_aloud):
    actual = reader_navigation.unit_range
    workers = []
    def locate(*args, **kwargs):
        workers.append(threading.get_ident())
        assert workers[-1] != reader.main
        return actual(*args, **kwargs)
    monkeypatch.setattr(reader_navigation, 'unit_range', locate)
    original = reader.document.to_dict()
    assert reader.navigation.navigate(unit, read_aloud=read_aloud)
    assert reader.running and not reader.spoken
    reader.finish()
    assert len(workers) == 1 and not reader.running and not reader.navigation.session.busy
    assert reader.field.GetRange(*reader.field.GetSelection()) == EXPECTED[unit]
    assert reader.focused == 1 and reader.document.to_dict() == original
    assert len(reader.spoken) == int(read_aloud)
    if read_aloud:
        text, span, digest, document = reader.spoken[0]
        assert text == EXPECTED[unit] == TEXT[slice(*span)]
        assert digest == reader.document.source_sha256 and document is reader.document


@pytest.mark.parametrize('change', ['edit', 'identical-tab', 'scope', 'cursor', 'cancel',
                                     'session-cancel', 'session-close', 'destroy'])
def test_late_result_cannot_select_or_speak_after_captured_authority_changes(reader, change):
    original_selection = reader.field.selection
    assert reader.navigation.navigate('word', read_aloud=True)
    reader.pending[0][0].result(timeout=5)
    if change == 'edit':
        reader.field.text = 'New reading copy.'
        reader.document.set_text(reader.field.text)
    elif change == 'identical-tab':
        reader.document = ReadingDocument(TEXT)
    elif change == 'scope':
        reader.revoked = True
    elif change == 'cursor':
        reader.place(len(TEXT))
    elif change == 'cancel':
        reader.cancel.set()
    elif change == 'session-cancel':
        reader.navigation.cancel()
    elif change == 'session-close':
        reader.navigation.close()
    else:
        reader.open = False
    expected_selection = reader.field.selection
    reader.finish()
    assert reader.field.selection == expected_selection
    assert not reader.spoken and reader.focused == 0
    if change != 'cursor':
        assert expected_selection == original_selection


@pytest.mark.parametrize('change', ['cancel', 'local-cancel', 'scope', 'tab', 'cursor', 'destroy'])
def test_focus_callback_cannot_retarget_a_completed_unit_before_speech(reader, change):
    def change_during_focus():
        if change == 'cancel':
            reader.cancel.set()
        elif change == 'local-cancel':
            reader.navigation.cancel()
        elif change == 'scope':
            reader.revoked = True
        elif change == 'tab':
            reader.document = ReadingDocument(TEXT)
        elif change == 'cursor':
            reader.place(len(TEXT))
        else:
            reader.open = False
    reader.on_focus = change_during_focus
    assert reader.navigation.navigate('word', read_aloud=True)
    reader.finish()
    assert not reader.spoken


@pytest.mark.parametrize('unit', tuple(EXPECTED))
def test_empty_navigation_never_narrates_all_text_or_changes_selection(reader, unit):
    reader.field.text = ' \n\t'
    reader.document.set_text(reader.field.text)
    reader.place(0)
    original = reader.field.selection
    assert reader.navigation.navigate(unit, read_aloud=True)
    reader.finish()
    assert reader.field.selection == original and not reader.spoken and reader.focused == 0


def test_next_and_previous_select_bounded_adjacent_words_without_implicit_speech(reader):
    assert reader.navigation.navigate('word', direction=1)
    reader.finish()
    assert reader.field.GetRange(*reader.field.selection) == 'читача'
    assert reader.navigation.navigate('word', direction=-1)
    reader.finish()
    assert reader.field.GetRange(*reader.field.selection) == 'Ім’я'
    assert not reader.spoken


def test_revoked_source_before_submission_does_not_start_a_worker(reader):
    reader.revoked = True
    assert not reader.navigation.navigate('word', read_aloud=True)
    assert not reader.pending and not reader.spoken
    assert 'revoked' in reader.messages[-1]


def test_one_existing_worker_serializes_navigation_and_preserves_the_stop_boundary(reader, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual = reader_navigation.unit_range
    def wait_for_release(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return actual(*args, **kwargs)
    monkeypatch.setattr(reader_navigation, 'unit_range', wait_for_release)
    try:
        assert reader.navigation.navigate('word', read_aloud=True)
        assert entered.wait(3)
        assert not reader.navigation.navigate('sentence', read_aloud=True)
        reader.cancel.set()
        release.set()
        reader.finish()
        assert not reader.spoken and not reader.navigation.session.busy
        # The controller does not own shutdown of the reader's executor.
        assert reader.pool.submit(lambda: 19).result(timeout=3) == 19
    finally:
        release.set()


def test_cancellation_before_actual_worker_entry_cannot_read_or_start_speech(reader):
    occupied, release = threading.Event(), threading.Event()
    def occupy():
        occupied.set()
        assert release.wait(3)
    blocking = reader.pool.submit(occupy)
    try:
        assert occupied.wait(3)
        assert reader.navigation.navigate('word', read_aloud=True)
        reader.navigation.cancel()
        release.set()
        blocking.result(timeout=3)
        reader.finish()
        assert not reader.spoken and reader.focused == 0
    finally:
        release.set()


def test_new_optional_commands_preserve_saved_preferences_and_native_editing_keys(tmp_path):
    commands = voice_reader_commands()
    additions = {'voice_reader.read_' + unit for unit in EXPECTED} | {
        'voice_reader.previous_unit', 'voice_reader.current_unit', 'voice_reader.next_unit'}
    previous = tuple(command for command in commands if command.identity not in additions)
    store = KeybindingStore(tmp_path)
    saved = {'voice_reader.find': 'Ctrl+Shift+J', 'voice_reader.playback': 'Ctrl+Shift+P'}
    store.save(previous, saved)
    before = store.path.read_bytes()
    values = validate_bindings(commands, store.load())
    assert store.path.read_bytes() == before and store.load() == saved
    assert all(values[key] == '' for key in additions)
    assert all(command.context == 'voice_reader' for command in commands)
    store.save(commands, {**saved, 'voice_reader.read_word': 'Ctrl+Shift+W'})
    assert KeybindingStore(tmp_path).load()['voice_reader.read_word'] == 'Ctrl+Shift+W'
    accepted = store.path.read_bytes()
    for shortcut in ('Ctrl+A', 'Ctrl+Left', 'Insert', 'NVDA+F3', 'F8'):
        with pytest.raises(ValueError):
            store.save(commands, {**saved, 'voice_reader.read_word': shortcut})
        assert store.path.read_bytes() == accepted


def test_existing_voice_reader_adapter_dispatches_only_current_enabled_scope(reader):
    calls, bindings = [], {}
    identifiers = count(100)
    wx = SimpleNamespace(NewIdRef=lambda: next(identifiers), EVT_MENU='menu',
        AcceleratorTable=lambda entries: list(entries), ACCEL_CTRL=1, ACCEL_ALT=2,
        ACCEL_SHIFT=4, ACCEL_NORMAL=0, WXK_F6=306, WXK_F7=307, WXK_F8=308)
    dialog = SimpleNamespace(Bind=lambda event, function, **kw: bindings.setdefault(kw['id'], function),
        Unbind=lambda event, **kw: bindings.pop(kw['id'], None),
        SetAcceleratorTable=lambda entries: calls.append(('keys', entries)))
    control = SimpleNamespace(IsEnabled=lambda: True, SetToolTip=lambda value: calls.append(('tip', value)))
    library = SimpleNamespace(root=Path('retained-library'))
    parent = SimpleNamespace(library=library, _closing=False)
    adapter = VoiceReaderKeyboardAdapter(dialog, parent,
        {'voice_reader.read_word': (control, lambda event: reader.navigation.navigate('word', read_aloud=True))},
        guard=reader.guard, alive=lambda: reader.open, report=reader.report, wx=wx)
    try:
        assert adapter.invoke('voice_reader.read_word')
        reader.finish()
        assert reader.spoken[0][0] == 'Ім’я'
        parent.library = SimpleNamespace(root=library.root)
        assert not adapter.invoke('voice_reader.read_word')
        assert not reader.pending
        assert len(reader.spoken) == 1
    finally:
        adapter.close()
    assert not bindings
