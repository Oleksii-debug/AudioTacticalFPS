"""Actual worker/file lifetimes with a controllable output owner, no device claim."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import os

import pytest

from carr_library.audio.ffmpeg import silence
from carr_library.audio.jobs import file_identity, regular_file
from carr_library.audio.playback import PlaybackStatus
from carr_library.reader.media_overlay import OverlaySession, PreparedOverlay
from carr_library.reader.publication_state import PublicationPositions
from carr_library.ui import media_overlay_dialog as module
from carr_library.ui.media_overlay_dialog import MediaOverlayDialog, choice_window


class Control:
    def __init__(self, value='', items=()):
        self.value, self.items, self.selected = value, list(items), 0
        self.enabled = True
    def ChangeValue(self, value):
        self.value = value
    def GetValue(self):
        return self.value
    def GetLastPosition(self):
        return len(self.value)
    def SetSelection(self, first, last=None):
        self.selected = first if last is None else (first, last)
    def SetItems(self, items):
        self.items = list(items)
    def GetSelection(self):
        return self.selected
    def Enable(self, value=True):
        self.enabled = bool(value)


class Player:
    def __init__(self):
        self.native = object()
        self.state, self.frames, self.accuracy = 'stopped', 0, 'not_started'
        self.calls = []
        self.close_calls = 0
        self.close_failure = self.stop_failure = False
    def status(self):
        return PlaybackStatus(state=self.state, position_frames=self.frames, sample_rate=8000,
            total_frames=8000, position_accuracy=self.accuracy)
    def play(self, path, *, start_seconds=0, device_id=None):
        self.calls.append((path, start_seconds, device_id))
        self.state, self.frames, self.accuracy = 'playing', round(start_seconds * 8000), 'driver_reported'
    def stop(self):
        if self.stop_failure:
            raise RuntimeError('Native audio still owns its input.')
        self.state, self.frames = 'stopped', 0
    def pause(self):
        self.state = 'paused'
    def resume(self):
        self.state = 'playing'
    def close(self):
        if self.close_failure:
            raise RuntimeError('Native handle close was refused.')
        self.stop()
        self.close_calls += 1


def make_document(root, identity='0'):
    temporary = TemporaryDirectory(prefix='prepared-', dir=root)
    path = Path(temporary.name) / 'playback.wav'
    silence(path, 8000, sample_rate=8000)
    return PreparedOverlay('Recorded 😀', 'epub', identity * 64, {},
        ({'id': 'one', 'title': 'Перша', 'first_interval': 0},
         {'id': 'two', 'title': 'Second', 'first_interval': 1}),
        ({'id': 'a', 'chapter': 'one', 'kind': 'line', 'start': 0., 'end': .5, 'text': 'Original 😀'},
         {'id': 'b', 'chapter': 'two', 'kind': 'line', 'start': .5, 'end': 1., 'text': 'Second.'}),
        path, file_identity(regular_file(path)), sha256(path.read_bytes()).hexdigest(),
        1., 8000, 8000, temporary)


def drain(window, queue, predicate=lambda value: value.future is None):
    end = monotonic() + 8
    while not predicate(window):
        if monotonic() >= end:
            raise AssertionError('The real reader worker did not settle.')
        if queue:
            queue.popleft()()
        else:
            sleep(.001)
    while queue:
        queue.popleft()()


@pytest.fixture
def window(tmp_path):
    value = object.__new__(MediaOverlayDialog)
    queue, messages, disposed = deque(), [], []
    root = tmp_path / 'library'
    root.mkdir()
    value.root, value.library = root, SimpleNamespace(root=root)
    root_info = root.stat()
    value.root_identity = (root_info.st_dev, root_info.st_ino)
    value.trust_controller = None
    value.frame = SimpleNamespace(library=value.library, _closing=False)
    value.wx = SimpleNamespace(CallAfter=lambda action, *args: queue.append(lambda: action(*args)), ID_CLOSE=1)
    value.dialog = SimpleNamespace(IsModal=lambda: True,
        EndModal=lambda code: disposed.append(code), Destroy=lambda: disposed.append('destroy'))
    value.executor = ThreadPoolExecutor(max_workers=1)
    value.cancelled = Event()
    value.closed = value.close_pending = value.stop_pending = value.settling = False
    value.destroyed = False
    value.showing = True
    value.future = value.media_request = None
    value.progress = None
    value.player = Player()
    document = make_document(tmp_path)
    value.session = OverlaySession(document, value.player)
    value.timer = SimpleNamespace(Stop=lambda: disposed.append('timer'))
    value.unregister_player = lambda: disposed.append('unregister')
    value.serial = 0
    value.warning = ''
    value.device_ids = [None]
    value.chapter_start = value.interval_start = 0
    for name in ('path', 'max_mib', 'minutes', 'chapter', 'interval', 'seek_seconds',
                 'device', 'text', 'mapping', 'status'):
        setattr(value, name, Control())
    value.max_mib.value, value.minutes.value = '16', '2'
    for name, default in (('playback_speed', '1'), ('playback_pitch', '0'),
                          ('playback_volume', '1'), ('playback_pan', '0')):
        setattr(value, name, Control(default))
    value.controls = {name: Control() for name in ('choose', 'load', 'previous_chapter',
        'next_chapter', 'previous_interval', 'next_interval', 'play', 'pause',
        'stop', 'seek', 'where', 'cancel', 'close')}
    value._say = messages.append
    yield value, queue, messages, disposed, tmp_path
    value.player.close_failure = value.player.stop_failure = False
    if not value.closed:
        value.close()
        drain(value, queue, lambda current: current.closed)
    value.executor.shutdown(wait=True, cancel_futures=True)
    document.close()


def test_close_waits_for_actual_worker_and_disposes_late_prepared_result(window):
    value, queue, messages, disposed, root = window
    entered, release = Event(), Event()
    old = value.session.document
    new = make_document(root, '1')
    def prepare():
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Test preparation barrier was not released.')
        return new, None, ()
    value._work(prepare, lambda result: pytest.fail('Cancelled preparation became the active book.'),
                dispose=value._dispose_loaded)
    try:
        assert entered.wait(5)
        value.close()
        assert not value.closed and value.close_pending
        assert new.audio.is_file() and old.audio.is_file() and not disposed
    finally:
        release.set()
    drain(value, queue, lambda current: current.closed)
    assert not new.audio.exists() and not old.audio.exists()
    assert disposed == ['unregister', 'timer', 1] and value.session is None


def test_revoked_frame_guard_still_releases_actual_reader_and_finishes_close(window):
    value, queue, messages, disposed, root = window
    session = value.session
    session.verify_audio()
    value.frame._closing = True
    value.close()
    drain(value, queue, lambda current: current.closed)
    assert not session.document.audio.exists() and session._reader is None
    assert 'could not be saved' in value.warning
    assert not PublicationPositions(value.root).path.exists()
    assert disposed == ['unregister', 'timer', 1]


def test_native_stop_refusal_preserves_every_owner_and_allows_explicit_retry(window):
    value, queue, messages, disposed, root = window
    session = value.session
    session.verify_audio()
    session.play()
    value.player.stop_failure = True
    value.close()
    assert value.future is None and not value.closed and not value.close_pending
    assert session.document.audio.is_file() and session._reader is not None
    assert not PublicationPositions(value.root).path.exists()
    value.player.stop_failure = False
    value.close()
    drain(value, queue, lambda current: current.closed)
    assert not session.document.audio.exists()


def test_native_close_refusal_does_not_destroy_prepared_document_and_can_retry(window):
    value, queue, messages, disposed, root = window
    document = value.session.document
    value.player.close_failure = True
    value.close()
    drain(value, queue)
    assert not value.closed and not value.close_pending and document.audio.is_file()
    assert disposed == [] and 'close was refused' in messages[-1]
    value.player.close_failure = False
    value.close()
    drain(value, queue, lambda current: current.closed)
    assert not document.audio.exists()


def test_stop_captures_observed_position_before_native_reset_and_saves_without_deleting_pcm(window):
    value, queue, messages, disposed, root = window
    session = value.session
    session.verify_audio()
    session.play()
    value.player.frames = 6000
    value.stop()
    drain(value, queue, lambda current: current.future is None and not current.stop_pending)
    assert session.cursor == .75 and session._reader is None
    assert PublicationPositions(value.root).load(session.document) == .75
    assert session.document.audio.is_file() and not value.closed and not disposed


def test_delayed_playback_keeps_verified_reader_and_rejects_changed_pcm(window):
    value, queue, messages, disposed, root = window
    callbacks = []
    def defer(begin, **kwargs):
        callbacks.append(begin)
        return SimpleNamespace(close=lambda: None)
    value.progress = SimpleNamespace(media_action=defer)
    session = value.session
    value.play()
    drain(value, queue)
    assert len(callbacks) == 1 and session._reader is not None and not value.player.calls
    def alter():
        with session.document.audio.open('r+b') as stream:
            stream.seek(-2, 2)
            stream.write(b'\x01\x00')
    if os.name == 'nt':
        with pytest.raises(OSError):
            alter()
        callbacks[0]()
        assert len(value.player.calls) == 1
    else:
        alter()
        callbacks[0]()
        assert not value.player.calls and 'Verify' in messages[-1]
        # Final metadata validation fails closed; retain the warning and
        # explicitly release the quiescent document without publishing a cursor.
        value.close()
        drain(value, queue)
        assert not value.closed and session.invalid_audio
        assert not PublicationPositions(value.root).path.exists()
        value.close()
        drain(value, queue, lambda current: current.closed)
        assert not PublicationPositions(value.root).path.exists()


def test_cancelled_delayed_callback_cannot_start_replaced_or_closed_reader(window):
    value, queue, messages, disposed, root = window
    callbacks = []
    value.progress = SimpleNamespace(media_action=lambda begin, **kwargs:
        (callbacks.append(begin), SimpleNamespace(close=lambda: None))[1])
    value.play()
    drain(value, queue)
    value.close()
    callbacks[0]()
    drain(value, queue, lambda current: current.closed)
    assert not value.player.calls


def test_new_load_keeps_shared_native_player_open_and_retains_old_cursor(window, monkeypatch):
    value, queue, messages, disposed, root = window
    old = value.session
    old.seek(.5)
    new = make_document(root, '2')
    monkeypatch.setattr(module, 'prepare_overlay', lambda *args, **kwargs: new)
    monkeypatch.setattr(module, 'output_devices', lambda: ())
    value.path.value = 'explicit publication.epub'
    value.load()
    drain(value, queue)
    assert value.session.document is new and not old.document.audio.exists()
    assert value.player.close_calls == 0 and not value.player.calls
    assert PublicationPositions(value.root).load(old.document) == .5
    assert value.text.value == 'Original 😀'


def test_guard_revocation_disposes_new_result_without_replacing_existing_document(window):
    value, queue, messages, disposed, root = window
    new = make_document(root, '3')
    old = value.session.document
    value._work(lambda: (new, None, ()), lambda result: pytest.fail('Revoked result was adopted.'),
                dispose=value._dispose_loaded)
    # The real worker has produced an owned result; its UI callback is still queued.
    # Revoke precisely that boundary, not before worker admission.
    assert value.future.result(timeout=8)[0] is new
    value.frame.library = SimpleNamespace(root=value.root)
    drain(value, queue)
    assert not new.audio.exists() and old.audio.is_file() and value.session.document is old
    assert 'library changed' in messages[-1]


def test_manual_interval_navigation_never_starts_playback_or_modifies_source(window):
    value, queue, messages, disposed, root = window
    before = value.session.document.intervals
    value.move_interval(1)
    assert value.session.index == 1 and value.session.cursor == .5
    assert value.text.value == 'Second.' and not value.player.calls
    value.move_chapter(-1)
    assert value.session.index == 0 and value.text.value == 'Original 😀'
    assert value.session.document.intervals == before and not value.player.calls


@pytest.mark.parametrize('index,start,selected', [(0, 0, 0), (199, 0, 199), (200, 200, 0), (400, 400, 0)])
def test_bounded_native_choices_keep_every_large_navigation_window_reachable(index, start, selected):
    rows = tuple(range(401))
    actual, window, current = choice_window(rows, index)
    assert (actual, current) == (start, selected)
    assert len(window) <= 200 and window[current] == index


@pytest.mark.parametrize('size', [0, -1, 201, True])
def test_invalid_navigation_window_is_rejected_before_native_population(size):
    with pytest.raises(ValueError):
        choice_window((1, 2), 0, size)


def test_changed_host_authority_cannot_start_delayed_playback(window):
    from carr_library.security.controller import TrustController
    value, queue, messages, disposed, root = window
    callbacks = []
    value.progress = SimpleNamespace(media_action=lambda begin, **kwargs:
        (callbacks.append(begin), SimpleNamespace(close=lambda: None))[1])
    value.play()
    drain(value, queue)
    assert len(callbacks) == 1 and value.session._reader is not None
    value.library.trust_controller = TrustController()
    callbacks[0]()
    assert not value.player.calls and 'library changed' in messages[-1]
    value.close()
    drain(value, queue, lambda current: current.closed)
    assert not PublicationPositions(value.root).path.exists()


def test_reading_existing_audio_does_not_consult_fresh_render_permission(window, monkeypatch):
    from carr_library.security.controller import TrustController
    value, queue, messages, disposed, root = window
    authority = TrustController()
    value.trust_controller = value.library.trust_controller = authority
    monkeypatch.setattr(authority, 'begin_operation',
        lambda *_: pytest.fail('Protected recorded reading requested fresh premium execution.'))
    value.play()
    drain(value, queue)
    assert len(value.player.calls) == 1
