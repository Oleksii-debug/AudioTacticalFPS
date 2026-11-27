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
from carr_library.ui.save_all import SaveCoordinator
from carr_library.speech.playlist import PlaylistStore, SavedPlaylist, retain_playlist, prepare_playlist


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
        self._save_coordinator = SaveCoordinator()
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
    assert len(results) == 1 and results[0][0].display_name == pcm.name
    assert results[0][0].path.parent == frame.library.root / 'state' / 'studio' / 'inputs'
    assert results[0][0].path.read_bytes() == pcm.read_bytes()
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


def _retained(owner, pcm):
    return retain_playlist(owner.root, prepare_playlist((pcm,)))


def test_save_all_confirms_exact_queue_after_durable_cas_write(owner, frame, pcm):
    items = _retained(owner, pcm)
    owner.player.set_playlist(items)
    owner.player.select_queue(0)
    owner.player.seek(.0375, start_if_stopped=False)
    summaries = []
    assert owner.dirty()
    assert frame._save_coordinator.save_all(summaries.append)
    assert len(summaries) == 1 and summaries[0].complete
    saved, token = PlaylistStore(owner.root).load()
    assert token == owner.token and saved.frame == 300
    assert not owner.dirty() and not owner.player.queue_active
    assert saved.items[0].sha256 == items[0].sha256


def test_failed_save_all_keeps_dirty_queue_and_reports_incomplete(owner, frame, pcm, monkeypatch):
    owner.player.set_playlist(_retained(owner, pcm))
    def fail(*args, **kwargs):
        raise OSError('synthetic disk full')
    monkeypatch.setattr(owner.store, 'save', fail)
    summaries = []
    frame._save_coordinator.save_all(summaries.append)
    assert len(summaries) == 1 and not summaries[0].complete and owner.dirty()
    assert 'disk full' in summaries[0].remaining[0][1]
    assert owner.token is None and not owner.store.path.exists()


def test_close_flushes_actual_current_frame_before_native_stop_resets_it(owner, frame, pcm):
    owner.player.set_playlist(_retained(owner, pcm))
    owner.player.select_queue(0)
    owner.queue_changed()
    owner.player.seek(.0625, start_if_stopped=False)
    owner.close()
    saved, _ = PlaylistStore(owner.root).load()
    assert saved.frame == 500 and owner.player.position_seconds == 0
    assert 'audio.playback' not in frame._save_coordinator.participants


def test_reopen_does_not_overwrite_or_read_saved_media_until_explicit_review(owner, frame, pcm, monkeypatch):
    owner.player.set_playlist(_retained(owner, pcm))
    owner.player.select_queue(0)
    owner.player.seek(.025, start_if_stopped=False)
    owner.queue_changed()
    expected = owner.store.path.read_bytes()
    owner.close()
    pcm.unlink()
    real_open = Path.open
    def no_media(path, *args, **kwargs):
        if path.suffix == '.audio':
            pytest.fail('Metadata-only reopen opened saved audio')
        return real_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', no_media)
        reopened = module.PlaybackOwner(frame, schedule=lambda *args: None)
        try:
            assert reopened.pending_saved is not None
            assert reopened.player.playlist == () and reopened.player.path is None
            reopened.poll()
            reopened.save_now()
            assert reopened.store.path.read_bytes() == expected
        finally:
            reopened.close()
    active = module.PlaybackOwner(frame, schedule=lambda *args: None)
    completed, failed = [], []
    try:
        active.review_saved(lambda: completed.append(True), failed.append, active=lambda: True)
        assert active.player.path is None
        frame.drain()
        assert completed == [True] and not failed and active.pending_saved is None
        assert active.player.position_seconds == .025
        assert active.player.state == 'stopped' and not active.player.queue_active
        assert active.player._wave is None and not active.player._open
    finally:
        active.close()


def test_cancelled_saved_review_cannot_adopt_or_change_passive_bytes(owner, frame, pcm):
    owner.player.set_playlist(_retained(owner, pcm))
    owner.queue_changed()
    expected = owner.store.path.read_bytes()
    owner.close()
    active = module.PlaybackOwner(frame, schedule=lambda *args: None)
    done, errors = [], []
    try:
        active.review_saved(lambda: done.append(True), errors.append, active=lambda: True)
        frame.jobs[-1].result(timeout=5)
        active.stop()
        frame.drain()
        assert not done and len(errors) == 1
        assert active.player.playlist == () and active.pending_saved is not None
        assert active.store.path.read_bytes() == expected
    finally:
        active.close()


def test_bounded_existing_timer_autosaves_resume_without_another_worker(owner, frame, pcm, monkeypatch):
    owner.player.set_playlist(_retained(owner, pcm))
    owner.player.select_queue(0)
    owner.queue_changed()
    owner.player.seek(.05, start_if_stopped=False)
    before_jobs = len(frame.jobs)
    now = owner.last_saved_at + 5.1
    monkeypatch.setattr(module.time, 'monotonic', lambda: now)
    owner.poll()
    saved, _ = owner.store.load()
    assert saved.frame == 400 and len(frame.jobs) == before_jobs
    assert not owner.dirty()


def test_explicit_discard_does_not_retry_save_during_native_cleanup(owner, frame, pcm, monkeypatch):
    owner.player.set_playlist(_retained(owner, pcm))
    writes = []
    def fail(*args, **kwargs):
        writes.append(True)
        raise OSError('synthetic unavailable storage')
    monkeypatch.setattr(owner.store, 'save', fail)
    with pytest.raises(OSError):
        owner.before_close()
    owner.discard_changes()
    owner.close()
    assert writes == [True] and not owner.store.path.exists()
    assert owner.silence_confirmed


def test_native_probe_observes_frame_id_before_deleted_python_wrapper():
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_audio_player_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    observer, = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == 'frame_destroyed']
    module = ast.fix_missing_locations(ast.Module(body=[observer], type_ignores=[]))
    destroyed, skipped = [], []
    environment = dict(native_frame_id=-31994, destroyed=destroyed)
    exec(compile(module, str(script), 'exec'), environment)

    class Event:
        def __init__(self, identity): self.identity = identity
        def GetId(self): return self.identity
        def GetEventObject(self):
            pytest.fail('Deleted native owner identity must not depend on its Python wrapper.')
        def Skip(self): skipped.append(self.identity)

    environment['frame_destroyed'](Event(-31993))
    assert not destroyed
    environment['frame_destroyed'](Event(-31994))
    assert destroyed == [True] and skipped == [-31993, -31994]
    assignment, = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'native_frame_id' for target in node.targets)]
    bind, = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'Bind'
        and any(isinstance(arg, ast.Name) and arg.id == 'frame_destroyed' for arg in node.args)]
    assert assignment.lineno < bind.lineno and ast.unparse(assignment.value) == 'frame.GetId()'


@pytest.mark.parametrize('event_seen, peer_alive', [(False, False), (False, True), (True, True), (True, False)])
def test_native_probe_close_wait_requires_both_event_and_dead_original_peer(event_seen, peer_alive):
    import ast
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_audio_player_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name in ('frame_is_destroyed', 'pump')]
    assert {node.name for node in functions} == {'frame_is_destroyed', 'pump'}
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    clock, yields = [0.0], []

    class Peer:
        def __bool__(self): return peer_alive
        def Close(self): pytest.fail('Observation cannot force Close.')
        def Destroy(self): pytest.fail('Observation cannot force Destroy.')

    def sleep(seconds):
        assert seconds == .01
        clock[0] += seconds

    environment = dict(frame=Peer(), destroyed=[True] if event_seen else [], errors=[],
        app=SimpleNamespace(Yield=lambda: yields.append(True)),
        time=SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    exec(compile(module, str(script), 'exec'), environment)
    predicate = environment['frame_is_destroyed']
    assert predicate() is (event_seen and not peer_alive)
    if event_seen and not peer_alive:
        environment['pump'](predicate, 'owned native close')
        assert not yields and clock[0] == 0
    else:
        with pytest.raises(TimeoutError, match='owned native close'):
            environment['pump'](predicate, 'owned native close')
        assert 12 <= clock[0] < 12.02 and yields
    labels = {'Desktop did not acknowledge native Close', 'Failure-path desktop close'}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'pump'
             and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
             and node.args[1].value in labels]
    assert len(calls) == 2
    assert all(isinstance(call.args[0], ast.Name) and call.args[0].id == 'frame_is_destroyed' for call in calls)
