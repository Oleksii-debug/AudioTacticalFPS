"""Exact Unicode selection and real worker/cursor lifetime without native wx.

The controllable player is an ownership observer, not an audio-device or voice
quality claim. Actual wx/SAPI execution has its own installed Windows probe.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import os

import pytest

from carr_library.audio.engines import Voice
from carr_library.audio.ffmpeg import silence
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader.live_speech import SpeechPositionStore, SpeechSelection, SpeechSession, SpeechSettings
from carr_library.ui.live_reading import (LiveReadingDialog, native_offset, source_offset,
                                        selected_source_offsets, validate_input)


class NativeText:
    def __init__(self, source, units, newline):
        self.value = source.replace('\r\n', '\n').replace('\r', '\n').replace('\n', newline)
        self.units = units
        self.selection = (0, self.GetLastPosition())

    def GetLastPosition(self):
        return len(self.value) if self.units == 'characters' else len(self.value.encode('utf-16-le')) // 2

    def GetRange(self, first, last):
        if self.units == 'characters':
            return self.value[first:last]
        return self.value.encode('utf-16-le')[2 * first:2 * last].decode('utf-16-le')

    def GetValue(self):
        return self.value.replace('\r\n', '\n').replace('\r', '\n')

    def GetSelection(self):
        return self.selection


@pytest.mark.parametrize('units', ['characters', 'utf16'])
@pytest.mark.parametrize('newline', ['\n', '\r', '\r\n'])
def test_every_native_boundary_roundtrips_emoji_combining_text_and_line_endings(units, newline):
    text = 'А😀 e\u0301\r\nOther 🦉 line\n最後。'
    control = NativeText(text, units, newline)
    for offset in range(len(text) + 1):
        if 0 < offset < len(text) and text[offset - 1:offset + 1] == '\r\n':
            with pytest.raises(ValueError, match='exact source offset'):
                native_offset(control, text, offset)
            continue
        position = native_offset(control, text, offset)
        assert source_offset(text, control.GetRange(0, position)) == offset
    first, last = text.index('😀'), text.index('🦉') + 1
    control.selection = native_offset(control, text, first), native_offset(control, text, last)
    assert selected_source_offsets(control, text) == (first, last)


def test_selected_visible_text_cannot_borrow_offsets_from_different_source():
    control = NativeText('Source replaced.', 'utf16', '\r\n')
    control.selection = (0, 6)
    with pytest.raises(ValueError, match='changed'):
        selected_source_offsets(control, 'Source original.')


@pytest.mark.parametrize('changes', [
    {'cursor': True}, {'cursor': -1}, {'selection': (3, 2)}, {'selection': (0, True)},
    {'ordinal': 0}, {'total': 1_000_001}, {'source_loader': 'command'},
    {'text': '\ud800'}, {'text': 'bad\0source'}, {'text': ''},
])
def test_invalid_input_is_rejected_before_native_or_worker_owners_are_created(changes):
    values = dict(text='Exact source.', cursor=0, selection=None, location=None,
                  source_loader=None, ordinal=1, total=1)
    values.update(changes)
    with pytest.raises(ValueError):
        validate_input(**values)


class PcmRegistry:
    def __init__(self):
        self.hook = lambda: None

    def voices(self, *, cancelled):
        cancelled()
        return (Voice('fixture:voice', 'Synthetic PCM', ('en',), 'fixture'),)

    def cache_identity(self, voice, *, cancelled):
        cancelled()
        return {'voice': voice, 'fixture': 1}

    def synthesize(self, text, output, *, cancelled, **settings):
        silence(output, 800, sample_rate=8000)
        self.hook()
        cancelled()
        return output


class Player:
    def __init__(self):
        self.native = object()
        self.path = None
        self.closed = self.stopped = False
        self.refuse_stop = False
        self.state = 'completed'
        self.calls = []

    def play(self, path, *, device_id=None):
        self.calls.append(path)
        self.path = path

    def status(self):
        return SimpleNamespace(state=self.state, error='', position_seconds=.1,
                               duration_seconds=.1, position_accuracy='observed synthetic completion')

    def stop(self):
        if self.refuse_stop:
            raise RuntimeError('The native audio owner has not released its handle.')
        self.stopped = True
        self.path = None

    def close(self):
        self.stop()
        self.closed = True

    def pause(self):
        raise RuntimeError('Pause refused by the selected player.')


def drain(window, queue, *, predicate=lambda window: window.future is None):
    deadline = monotonic() + 5
    while not predicate(window):
        if monotonic() >= deadline:
            raise AssertionError('The actual worker did not reach its UI completion boundary.')
        if queue:
            queue.popleft()()
        else:
            sleep(.001)
    while queue:
        queue.popleft()()


@pytest.fixture
def window(tmp_path):
    value = object.__new__(LiveReadingDialog)
    queue, messages, selected, disposed = deque(), [], [], []
    value.wx = SimpleNamespace(CallAfter=queue.append, ID_CLOSE=1)
    value.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=lambda code: disposed.append(code),
                                   Destroy=lambda: disposed.append('destroy'))
    value.root, value.source, value.location = tmp_path, 'First. Second!', None
    value.source_loader, value.ordinal, value.total = None, 1, 1
    value.guard = lambda: None
    value.executor = ThreadPoolExecutor(max_workers=1)
    value.cancelled = Event()
    value.future = value.media_request = None
    value.progress = None
    value.trust_controller, value.operation = None, None
    value.device = SimpleNamespace(GetSelection=lambda: 0)
    value.device_ids = [None]
    value.closed = value.close_pending = value.playing = value.through_book = False
    value.showing, value.destroyed = True, False
    value.auto_advance = value.pause_requested = value.cleanup_pending = False
    value.warning = ''
    value.player = Player()
    value.unregister_player = lambda: disposed.append('unregister')
    value.timer = SimpleNamespace(Stop=lambda: disposed.append('timer stopped'))
    value._say, value._select, value._enable = messages.append, selected.append, lambda: None
    value.modes = ['cursor']
    value.mode = SimpleNamespace(SetSelection=lambda index: None)
    registry = PcmRegistry()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1, memory_bytes=64*1024**2))
    session = SpeechSession(tmp_path, value.source, SpeechSettings('fixture:voice', 'en'),
                            SpeechSelection(0, len(value.source)), registry=registry, broker=broker)
    value.session = session
    yield value, registry, broker, queue, messages, selected, disposed
    value.executor.shutdown(wait=True, cancel_futures=True)
    session.close()


def test_close_waits_for_real_synthesis_worker_before_destroying_utterance(window):
    value, registry, broker, queue, messages, selected, disposed = window
    entered, release = Event(), Event()
    def barrier():
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Speech barrier was not released.')
    registry.hook = barrier
    session = value.session
    value._work(session.render_next, lambda result: pytest.fail('Cancelled speech became playable.'))
    try:
        assert entered.wait(5)
        value.close()
        assert not value.closed and value.close_pending and session.directory.exists()
        assert disposed == []
    finally:
        release.set()
    drain(value, queue, predicate=lambda window: window.closed)
    assert value.player.closed and value.session is None and not session.directory.exists()
    assert broker.snapshot()['leases'] == []
    assert SpeechPositionStore(value.root).load(value.source)['cursor'] == 0
    assert disposed == ['unregister', 'timer stopped', 1]


def test_stop_refusal_preserves_pcm_until_actual_owner_release(window):
    value, registry, broker, queue, messages, selected, disposed = window
    session = value.session
    excerpt = session.render_next()
    value.player.path, value.player.refuse_stop = excerpt.path, True
    value.close()
    assert not value.closed and excerpt.path.exists() and value.future is None
    assert 'has not released' in messages[-1]
    value.player.refuse_stop = False
    value.close()
    drain(value, queue, predicate=lambda window: window.closed)
    assert not excerpt.path.exists() and value.session is None


def test_completed_playback_stops_its_owner_before_advancing_and_saving_cursor(window):
    value, registry, broker, queue, messages, selected, disposed = window
    session = value.session
    excerpt = session.render_next()
    value.player.path, value.playing = excerpt.path, True
    release = session.release
    def observed_release(*, completed=False):
        if completed:
            assert value.player.stopped
        release(completed=completed)
    session.release = observed_release
    value.tick()
    drain(value, queue, predicate=lambda window: window.session is None)
    assert SpeechPositionStore(value.root).load(value.source)['cursor'] == 6
    assert not excerpt.path.exists() and session.cursor == 6


def test_failed_pause_does_not_lose_active_playback_completion_observer(window):
    value, registry, broker, queue, messages, selected, disposed = window
    value.playing = True
    value.player.state = 'playing'
    value.pause()
    assert value.playing and 'Pause refused' in messages[-1]


@pytest.mark.parametrize('revoked_at', ['before', 'after'])
def test_resume_revalidates_source_on_each_side_of_saved_state_read(window, monkeypatch, revoked_at):
    value, registry, broker, queue, messages, selected, disposed = window
    value.session.close()
    value.session = None
    SpeechPositionStore(value.root).remember(value.source, 6, SpeechSettings('fixture:voice', 'en'))
    revoked = [revoked_at == 'before']
    def guard():
        if revoked[0]:
            raise PermissionError('Source access was revoked.')
    value.guard = guard
    original = SpeechPositionStore.load
    def load(store, text, location):
        result = original(store, text, location)
        revoked[0] = True
        return result
    monkeypatch.setattr(SpeechPositionStore, 'load', load)
    value.restore_cursor()
    drain(value, queue)
    assert not selected and 'revoked' in messages[-1]


def test_guard_failure_before_show_releases_every_created_native_owner(window):
    value, registry, broker, queue, messages, selected, disposed = window
    value.session.close()
    value.session = None
    value.on_ready = None
    value.dialog.IsModal = lambda: False
    def revoked():
        raise PermissionError('Source access was revoked.')
    value.guard = revoked
    with pytest.raises(PermissionError, match='revoked'):
        value.show()
    assert value.closed and value.destroyed and value.player.closed
    assert disposed == ['unregister', 'timer stopped', 'destroy']


def alter_pcm(path):
    from carr_library.audio.ffmpeg import wav_info
    offset = wav_info(path).data_offset
    with path.open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x01\x00')


def test_replaced_valid_pcm_is_rejected_before_any_player_receives_it(window):
    value, registry, broker, queue, messages, selected, disposed = window
    excerpt = value.session.render_next()
    alter_pcm(excerpt.path)
    value.play()
    drain(value, queue)
    assert not value.player.calls and not value.playing and value.session.cursor == 0
    assert 'changed' in messages[-1]


def test_verified_pcm_stays_owned_through_delayed_media_handoff(window):
    value, registry, broker, queue, messages, selected, disposed = window
    callbacks = []
    def defer(begin, **kwargs):
        callbacks.append(begin)
        return SimpleNamespace(close=lambda: callbacks.clear())
    value.progress = SimpleNamespace(media_action=defer)
    excerpt = value.session.render_next()
    value.play()
    drain(value, queue)
    assert len(callbacks) == 1 and not value.player.calls
    assert value.session._playback_reader is not None
    if os.name == 'nt':
        with pytest.raises(OSError):
            alter_pcm(excerpt.path)
        callbacks[0]()
        assert value.player.calls == [excerpt.path]
    else:
        alter_pcm(excerpt.path)
        callbacks[0]()
        assert not value.player.calls and 'changed' in messages[-1]
    value.close()
    drain(value, queue, predicate=lambda window: window.closed)
    assert SpeechPositionStore(value.root).load(value.source)['cursor'] == 0
    assert not excerpt.path.exists()

