"""Shared-worker review, quiet admission and native owner lifetime boundaries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
import time
import wave

import pytest

from carr_library.speech.player import WavePlayer
from carr_library.ui import audio_playback as module


class Hold:
    def __init__(self, progress):
        self.progress, self.closed = progress, False
    @property
    def ready(self): return self.progress.ready and not self.closed
    def close(self): self.closed = True


class Progress:
    def __init__(self):
        self.ready = False
        self.holds, self.registered, self.removed = [], [], []
    def hold_audio(self):
        hold = Hold(self)
        self.holds.append(hold)
        return hold
    def register_player(self, player): self.registered.append(player)
    def unregister_player(self, player, *, silence_confirmed=True):
        self.removed.append((player, silence_confirmed))
    def refresh_audio_state(self): pass


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.progress_panel = Progress()
        self.working = self._closing = False
        self.cancel_event = Event()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.completed, self.jobs = [], []
    def run_job(self, title, work, done, failed=None):
        assert not self.working
        self.working = True
        future = self.executor.submit(work)
        self.jobs.append(future)
        def completed(value):
            self.completed.append((value, done, failed))
        future.add_done_callback(completed)
    def drain(self):
        deadline = time.monotonic() + 5
        while not self.completed:
            if time.monotonic() >= deadline: raise TimeoutError('review callback')
            time.sleep(.005)
        future, done, failed = self.completed.pop(0)
        self.working = False
        try: done(future.result())
        except Exception as exc: failed(exc)


@pytest.fixture
def frame(tmp_path):
    value = Frame(tmp_path)
    yield value
    value.cancel_event.set()
    value.executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def owner(frame):
    callbacks = []
    value = module.PlaybackOwner(frame, schedule=lambda milliseconds, callback: callbacks.append(callback))
    value.callbacks = callbacks
    yield value
    value.close()


@pytest.fixture
def pcm(tmp_path):
    path = tmp_path / 'Actual queued PCM.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\0\0' * 800)
    return path


@pytest.mark.parametrize('action', ['stop', 'pause', 'close', 'root-change'])
def test_waiting_quiet_callback_cannot_start_after_owner_boundary(owner, frame, action):
    starts = []
    owner.execute(lambda: starts.append(True), audible=True)
    assert len(owner.callbacks) == 1 and not starts
    if action == 'root-change':
        frame.library = SimpleNamespace(root=frame.library.root)
    else:
        getattr(owner, action)()
    frame.progress_panel.ready = True
    owner.callbacks.pop(0)()
    assert not starts
    assert all(hold.closed for hold in frame.progress_panel.holds)


def test_quiet_completion_runs_owned_action_once_and_closes_hold(owner, frame):
    calls = []
    owner.execute(lambda: calls.append(get_ident()), audible=True)
    frame.progress_panel.ready = True
    owner.callbacks.pop(0)()
    assert calls == [get_ident()]
    assert all(hold.closed for hold in frame.progress_panel.holds)


def test_shared_worker_returns_passive_actual_file_review_on_ui(owner, frame, pcm):
    caller = get_ident()
    threads, results, errors = [], [], []
    real = owner.player.prepare_playlist
    def reviewed(paths, **kwargs):
        threads.append(get_ident())
        return real(paths, **kwargs)
    owner.player.prepare_playlist = reviewed
    owner.review_playlist((pcm,), results.append, errors.append, active=lambda: True)
    assert owner.player.path is None
    frame.drain()
    assert threads and threads[0] != caller and not errors
    assert len(results) == 1 and results[0][0].path == pcm
    assert owner.player.path is None and owner.review_cancel is None


def test_stop_after_worker_finishes_before_callback_still_settles_review(owner, frame, pcm):
    results, errors = [], []
    owner.review_playlist((pcm,), results.append, errors.append, active=lambda: True)
    frame.jobs[-1].result(timeout=5)
    owner.stop()
    frame.drain()
    assert not results and len(errors) == 1 and isinstance(errors[0], InterruptedError)
    assert owner.review_cancel is None and owner.player.path is None


def test_closed_modal_never_receives_late_worker_callback(owner, frame, pcm):
    calls = []
    owner.review_playlist((pcm,), lambda value: calls.append('done'),
                          lambda error: calls.append('error'), active=lambda: not owner.closed)
    owner.close()
    frame.drain()
    assert not calls and owner.player.path is None


def test_actual_source_identity_change_during_quiet_wait_refuses_queue(owner, frame, pcm):
    owner.player.set_playlist(owner.player.prepare_playlist([pcm]))
    owner.execute(lambda: owner.player.play_queue(), audible=True)
    with pcm.open('ab') as stream: stream.write(b'changed')
    frame.progress_panel.ready = True
    owner.callbacks.pop(0)()
    assert 'changed' in owner.notice and not owner.player.queue_active
    assert owner.player._process is None and not owner.player._open


def test_uncertain_close_stays_owned_and_successful_retry_unregisters_truthfully(owner, frame):
    calls = []
    real = owner.player.close
    def close():
        calls.append(True)
        if len(calls) == 1: raise RuntimeError('device refused Close')
        real()
    owner.player.close = close
    with pytest.raises(RuntimeError):
        owner.close()
    assert owner.closed and not owner.silence_confirmed
    assert frame.progress_panel.removed[-1] == (owner.player, False)
    owner.close()
    assert owner.silence_confirmed and frame.progress_panel.removed[-1] == (owner.player, True)
    assert len(calls) == 2


def test_scope_change_while_reviewing_never_adopts_result(owner, frame, pcm):
    entered, release = Event(), Event()
    actual = owner.player.prepare_playlist
    def work(paths, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(paths, **kwargs)
    owner.player.prepare_playlist = work
    result, errors = [], []
    owner.review_playlist((pcm,), result.append, errors.append, active=lambda: True)
    assert entered.wait(5)
    frame.library = SimpleNamespace(root=frame.library.root)
    release.set()
    frame.drain()
    assert not result and len(errors) == 1
    assert owner.player.path is None


@pytest.mark.parametrize('failure', ['stop', 'unbind'])
def test_native_timer_failure_cannot_skip_player_close(frame, monkeypatch, failure):
    import sys
    from carr_library.ui import reader_playback
    calls = []
    class TimerOwner:
        def Bind(self, *args): pass
        def Unbind(self, *args, **kwargs):
            calls.append('unbind')
            if failure == 'unbind': raise RuntimeError('unbind failed')
    class Timer:
        def __init__(self, owner): pass
        def Start(self, milliseconds): pass
        def Stop(self):
            calls.append('timer-stop')
            if failure == 'stop': raise RuntimeError('timer stop failed')
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        EvtHandler=TimerOwner, Timer=Timer, EVT_TIMER='timer',
        CallLater=lambda milliseconds, callback: None))
    real_close = module.PlaybackOwner.close
    def close(self):
        calls.append('player-close')
        return real_close(self)
    monkeypatch.setattr(module.PlaybackOwner, 'close', close)
    monkeypatch.setattr(reader_playback, 'show_reader_playback', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError):
        module.show_audio_playback(frame)
    assert 'player-close' in calls and frame._playback_owner is None
    assert frame.progress_panel.removed[-1][1] is True
