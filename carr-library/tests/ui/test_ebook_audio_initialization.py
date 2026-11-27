"""Exact wrapped source positions and transactional recorded-EPUB startup.

These controls model the documented native UTF-16/CRLF and visual-row APIs.
Source admission, isolated EPUB indexing, PCM decoding, leases and the borrowed
reader executor remain real. The Windows probe separately exercises native wx.
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
from pathlib import Path
from threading import Event, Lock, get_ident
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.ebook_audio import EbookAudioSession
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.ebook_audio_dialog import EbookAudioDialog
from carr_library.ui.ebook_model import native_position

_spec = importlib.util.spec_from_file_location(
    'epub_initialization_fixtures',
    Path(__file__).parents[1] / 'reader' / 'test_ebook_audio.py')
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
audio = _fixtures.audio


class NativeText:
    """Wrapped native positions, with GUI ownership enforced on every access."""
    def __init__(self, value='', *, width=24, utf16=True, crlf=True):
        self.thread = get_ident()
        self.value, self.width, self.utf16, self.crlf = value, width, utf16, crlf
        self.enabled = True
        self.selection = (0, 0)
        self.position = 0
        self.items = []
        self.queries = []

    def _gui(self):
        assert get_ident() == self.thread, 'The worker touched a native control.'

    def _units(self, text):
        return len(text.encode('utf-16-le')) // 2 if self.utf16 else len(text)

    def _value(self):
        return self.value.replace('\n', '\r\n') if self.crlf else self.value

    def _rows(self):
        rows, start = [], 0
        logical = self.value.split('\n')
        for line_index, line in enumerate(logical):
            pieces = [line[i:i+self.width] for i in range(0, len(line), self.width)] or ['']
            for piece in pieces:
                length = self._units(piece)
                rows.append((start, length))
                start += length
            if line_index < len(logical) - 1:
                start += 2 if self.crlf else 1
        return rows

    def ChangeValue(self, value):
        self._gui()
        self.value = value

    def GetValue(self):
        self._gui()
        return self.value

    def GetRange(self, start, end):
        self._gui()
        self.queries.append((start, end))
        value = self._value()
        if self.utf16:
            result = value.encode('utf-16-le')[2*start:2*end].decode('utf-16-le')
        else:
            result = value[start:end]
        return result.replace('\r\n', '\n').replace('\r', '\n') if self.crlf else result

    def GetLastPosition(self):
        self._gui()
        return self._units(self._value())

    def GetNumberOfLines(self):
        self._gui()
        return len(self._rows())

    def XYToPosition(self, column, row):
        self._gui()
        rows = self._rows()
        if not 0 <= row < len(rows) or not 0 <= column <= rows[row][1]:
            return -1
        return rows[row][0] + column

    def SetSelection(self, start, end=None):
        self._gui()
        self.selection = start if end is None else (start, end)

    def Set(self, items):
        self._gui()
        self.items = list(items)

    def SetFocus(self):
        self._gui()

    def ShowPosition(self, position):
        self._gui()
        self.position = position

    def Enable(self, value=True):
        self._gui()
        self.enabled = value


@pytest.mark.parametrize('utf16,crlf', [(True, True), (True, False), (False, True), (False, False)])
def test_visual_wrapping_preserves_verified_source_positions(utf16, crlf):
    raw = ('A long source notice 😀 that occupies several visible rows. ' * 3
           + '\nRole: 😀 exact recorded words.\nSecond source segment.')
    control = NativeText(raw, width=19, utf16=utf16, crlf=crlf)
    assert control.GetNumberOfLines() > raw.count('\n') + 1
    first = raw.index('😀 exact')
    last = first + len('😀 exact recorded words.')
    prefix = raw[:first]
    logical_row = prefix.count('\n')
    logical_column = control._units(prefix.rsplit('\n', 1)[-1])
    candidate = control.XYToPosition(logical_column, logical_row)
    assert candidate < 0 or control.GetRange(0, candidate) != prefix
    for offset in (0, first, first + 1, last, raw.index('Second'), len(raw)):
        position = native_position(control, raw, offset)
        assert 0 <= position <= control.GetLastPosition()
        assert control.GetRange(0, position) == raw[:offset]
    assert control.GetRange(native_position(control, raw, first),
                            native_position(control, raw, last)) == raw[first:last]
    assert control.GetValue() == raw


@pytest.mark.parametrize('invalid', ['mismatched-prefix', 'split-surrogate'])
def test_wrapped_mapping_never_accepts_unverified_native_ranges(invalid):
    raw = 'Long wrapped original text before the exact target. ' * 3 + '\nTarget 😀.'
    control = NativeText(raw, width=17)
    control.XYToPosition = lambda column, row: -1
    def unavailable(start, end):
        if invalid == 'split-surrogate':
            raise UnicodeDecodeError('utf-16-le', b'\x00\xd8', 0, 2, 'unpaired surrogate')
        return 'different native prefix'
    control.GetRange = unavailable
    with pytest.raises(ValueError, match='could not locate'):
        native_position(control, raw, raw.index('Target'))
    assert control.selection == (0, 0)


def test_wrapped_mapping_rejects_changed_text_and_out_of_range_clamped_position():
    raw = 'Long wrapped source that must remain unchanged. ' * 3
    changed = NativeText(raw + ' changed', width=17)
    with pytest.raises(ValueError, match='changed the extracted text'):
        native_position(changed, raw, 4)
    control = NativeText(raw, width=17)
    control.XYToPosition = lambda column, row: -1
    control.GetLastPosition = lambda: len(raw) - 1
    with pytest.raises(ValueError, match='could not locate'):
        native_position(control, raw, len(raw))
    assert not control.queries, 'An out-of-bounds candidate reached a clamping native read.'


@pytest.fixture
def wrapped_import(tmp_path, audio):
    from carr_library.studio.publishing import NOTICE
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Wrapped recorded source')
    body = ('<h1>Exact recorded source</h1><p>' + NOTICE + '</p>'
            '<p>Role: <span id="one">First 😀 recorded phrase.</span></p>'
            '<p>Role: <span id="two">Second source phrase.</span></p>')
    source = tmp_path / 'wrapped.epub'
    original = _fixtures.make_epub(audio, body=body)
    source.write_bytes(original)
    imported = app.import_file(workspace, source)
    assert imported['state'] == 'complete'
    desk = ReadingDesk(app, workspace, imported['edition'])
    return app, source, original, desk


class Player:
    def __init__(self):
        self.state = 'stopped'
        self.plays = []
        self.closes = 0

    def play(self, path):
        assert Path(path).is_file()
        self.plays.append(Path(path))
        self.state = 'playing'

    def close(self):
        self.closes += 1
        self.state = 'stopped'


class QuietProgress:
    def __init__(self, failure=None):
        self.players = []
        self.removed = []
        self.failure = failure

    def register_player(self, player):
        self.players.append(player)
        if self.failure == 'register':
            raise RuntimeError('Synthetic registration acknowledgement failure.')

    def unregister_player(self, player, *, silence_confirmed):
        assert silence_confirmed
        self.removed.append(player)
        self.players.remove(player)

    def refresh_audio_state(self):
        pass

    def hold_audio(self):
        return SimpleNamespace(ready=True, close=lambda: None)


def dialog(executor, desk, *, failure=None):
    ui = EbookAudioDialog.__new__(EbookAudioDialog)
    ui.executor, ui.desk, ui.ordinal = executor, desk, 1
    parent_cancelled = Event()
    ui.parent_cancelled = parent_cancelled.is_set
    ui.cancelled = Event()
    ui.closed = ui.closing = ui.busy = ui.destroyed = False
    ui._owner_id = 301
    ui._result_lock, ui._completed = Lock(), {}
    ui.session = ui.transport = ui.player = ui._pending = None
    ui.guard = lambda: None
    ui.session_factory = EbookAudioSession
    players = []
    def player():
        value = Player()
        players.append(value)
        return value
    ui.player_factory = player
    ui.progress = QuietProgress(failure)
    ui.segments, ui.text, ui.status = NativeText(), NativeText(width=24), NativeText()
    ui.buttons = [NativeText() for _ in range(7)]
    timer = []
    def start(interval):
        timer.append(('start', interval))
        if failure == 'timer':
            raise RuntimeError('Synthetic native timer start failure.')
    ui.timer = SimpleNamespace(Start=start, Stop=lambda: timer.append(('stop',)))
    queued, delivered = [], Event()
    def after(callback):
        queued.append(callback)
        delivered.set()
    ui.wx = SimpleNamespace(CallAfter=after, CallLater=lambda _, callback: after(callback))
    return ui, parent_cancelled, queued, delivered, players, timer


def completed(ui, queued, delivered):
    assert delivered.wait(15), 'The existing EPUB worker did not deliver its bounded completion.'
    delivered.clear()
    with ui._result_lock:
        results = tuple(row[0] for row in ui._completed.values())
    callback = queued.pop(0)
    callback()
    assert not queued, 'An unexpected second UI callback escaped the same operation.'
    return results


def test_wrapped_actual_epub_load_and_play_keep_one_live_session(wrapped_import):
    app, source, original, desk = wrapped_import
    with ThreadPoolExecutor(max_workers=1) as executor:
        ui, _, queued, delivered, players, _ = dialog(executor, desk)
        try:
            ui.load()
            session, = completed(ui, queued, delivered)
            assert ui.session is session and not session._closed.is_set()
            assert ui.transport is not None and ui.player is players[0]
            assert not ui.stopped() and not ui.busy
            assert all(button.enabled for button in ui.buttons)
            assert ui.text.GetNumberOfLines() > session.page.raw_text.count('\n') + 1
            cue = session.chapter.cues[0]
            assert ui.text.GetRange(*ui.text.selection) == session.page.raw_text[cue.start:cue.end]
            assert players[0].plays == [], 'Opening a recorded source must not autoplay.'
            ui.play()
            prepared, = completed(ui, queued, delivered)
            assert ui.transport.prepared is prepared and ui.transport.active
            assert ui.player.state == 'playing' and ui.player.plays == [prepared.path]
            assert prepared.frames == 4800 and prepared.sample_rate == 24000
            assert not ui.stopped()
            assert app.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
            assert source.read_bytes() == original and not app._selections
            assert session.chapter.asset_sha256 == sha256(original).hexdigest()
        finally:
            ui.force_close()
    assert session.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('failure', ['mapping', 'register', 'timer'])
def test_failed_native_initialization_cannot_enable_a_discarded_session(wrapped_import, failure):
    app, source, original, desk = wrapped_import
    with ThreadPoolExecutor(max_workers=1) as executor:
        ui, _, queued, delivered, players, timer = dialog(executor, desk, failure=failure)
        if failure == 'mapping':
            ui.text.GetRange = lambda start, end: 'incorrect prefix'
        ui.load()
        session, = completed(ui, queued, delivered)
        assert session._closed.is_set() and not session.root.exists()
        assert ui.session is ui.transport is ui.player is None
        assert not ui.busy and not any(button.enabled for button in ui.buttons)
        assert players[0].closes == 1 and not players[0].plays
        assert not ui.progress.players
        expected = {'mapping': 'could not locate', 'register': 'registration acknowledgement',
                    'timer': 'native timer start'}[failure]
        assert expected in ui.status.GetValue()
        assert 'EPUB audio reading cancelled.' not in ui.status.GetValue()
        message = ui.status.GetValue()
        ui.play()
        assert not ui.busy and not queued and ui.status.GetValue() == message
        assert executor.submit(lambda: 'parent still owns the worker').result(timeout=2) == 'parent still owns the worker'
        assert source.read_bytes() == original
        assert app.assets.resolve(desk.identity.asset_sha256).read_bytes() == original
        ui.force_close()
    assert session.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('cancel_owner', ['child', 'parent'])
def test_cancel_after_worker_before_ui_adoption_discards_source_without_clearing_token(wrapped_import, cancel_owner):
    app, source, original, desk = wrapped_import
    with ThreadPoolExecutor(max_workers=1) as executor:
        ui, parent, queued, delivered, players, _ = dialog(executor, desk)
        ui.load()
        assert delivered.wait(15)
        (session, release), = ui._completed.values()
        token = ui.cancelled if cancel_owner == 'child' else parent
        token.set()
        completed(ui, queued, delivered)
        assert token.is_set() and ui.stopped()
        assert ui.session is ui.transport is ui.player is None and not players
        assert session._closed.is_set() and not session.root.exists()
        assert not ui.busy and not any(button.enabled for button in ui.buttons)
        assert executor.submit(lambda: 17).result(timeout=2) == 17
        assert source.read_bytes() == original
        ui.force_close()
    assert session.broker.snapshot()['leases'] == []


def test_failed_startup_keeps_unconfirmed_player_registration_until_retry(wrapped_import, monkeypatch):
    from carr_library.ui import ebook_audio_dialog
    app, source, original, desk = wrapped_import
    retries, confirmed, attempts = [], Event(), []
    monkeypatch.setattr(ebook_audio_dialog, 'pending_cleanup',
                        lambda retry, **options: retries.append(retry))
    with ThreadPoolExecutor(max_workers=1) as executor:
        ui, _, queued, delivered, players, _ = dialog(executor, desk, failure='timer')
        factory = ui.player_factory
        def unconfirmed_player():
            value = factory()
            close = value.close
            def retained_close():
                attempts.append(value)
                if not confirmed.is_set():
                    raise RuntimeError('Synthetic native player closure is unconfirmed.')
                close()
            value.close = retained_close
            return value
        ui.player_factory = unconfirmed_player
        ui.load()
        session, = completed(ui, queued, delivered)
        assert ui.session is ui.transport is ui.player is None
        assert session._closed.is_set() and not session.root.exists()
        assert len(retries) == len(ui.progress.players) == 1
        assert attempts == [players[0]] and not players[0].plays
        assert 'native timer start' in ui.status.GetValue()
        assert not any(button.enabled for button in ui.buttons)
        assert source.read_bytes() == original
        confirmed.set()
        retries.pop()()
        assert attempts == [players[0], players[0]]
        assert players[0].closes == 1 and not ui.progress.players
        assert len(ui.progress.removed) == 1
        ui.force_close()
    assert session.broker.snapshot()['leases'] == []


def test_wrapped_crlf_boundary_requires_both_exact_native_sides():
    raw = 'Long introductory row.' * 3 + '\nTarget 😀 in the next paragraph.'
    control = NativeText(raw, width=11)
    offset = raw.index('Target')
    prefix, suffix = raw[:offset], raw[offset:]
    boundary = control._units(prefix.replace('\n', '\r\n'))
    # Pinned wxTextBuffer::Translate maps the trailing CR of this partial
    # native range to LF. Prefix verification alone therefore accepts it.
    assert control.GetRange(0, boundary - 1) == prefix
    assert control.GetRange(boundary - 1, control.GetLastPosition()) != suffix
    assert native_position(control, raw, offset) == boundary
    assert control.GetRange(boundary, control.GetLastPosition()) == suffix
