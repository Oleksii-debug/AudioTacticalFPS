"""Prepared playback ownership and native-handler policy, without a device claim."""
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.capture_types import AudioBudget
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import file_identity, regular_file
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.playback_processing import PlaybackControls, PreparedPlayback
from carr_library.audio.player import WavePlayer
from carr_library.ui import playback_controls as module


def recording(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
        stream.writeframes(b'\x10\x00' * 8000)
    return path


class Device:
    def __init__(self):
        self.snapshot = PlaybackStatus()
        self.plays, self.stops = [], 0
        self.blocked = False
    def status(self):
        return self.snapshot
    def play(self, path, *, start_seconds=0, end_seconds=None, device_id=None):
        info = wav_info(path)
        self.plays.append((Path(path), start_seconds, device_id))
        self.snapshot = PlaybackStatus(state='playing', path=str(path),
            total_frames=info.frames, sample_rate=info.rate, channels=info.channels,
            position_frames=round(start_seconds * info.rate), position_accuracy='completed_buffers')
    def stop(self):
        self.stops += 1
        if self.blocked:
            raise RuntimeError('The actual owned player has not released its file.')
        self.snapshot = replace(self.snapshot, state='stopped', position_frames=0)
    def pause(self):
        self.snapshot = replace(self.snapshot, state='paused')
    def resume(self):
        self.snapshot = replace(self.snapshot, state='playing')


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    source, other = recording(tmp_path / 'source.wav'), recording(tmp_path / 'other.wav')
    player = WavePlayer()
    player.native = Device()
    player.set_playlist([source, other])
    player.play_index(0)
    selected = [True]
    value = module.PlaybackWorkspace(player, source, library_root=root, current=lambda: selected[0])
    prepared = []
    def fake_prepare(path, controls, **kwargs):
        # UI lifecycle test only: actual filters are qualified separately by
        # test_playback_processing against the real local FFmpeg process.
        temporary = TemporaryDirectory(prefix='synthetic-playback-', dir=tmp_path)
        target = Path(temporary.name) / 'pcm.wav'
        shutil.copyfile(path, target)
        info = wav_info(path)
        result = PreparedPlayback(path, file_identity(regular_file(path)),
            sha256(Path(path).read_bytes()).hexdigest(), info.frames, info.rate, controls,
            target, file_identity(regular_file(target)), sha256(target.read_bytes()).hexdigest(),
            info.frames, {'test_only': True}, temporary)
        result.pin()
        prepared.append(result)
        return result
    monkeypatch.setattr(module, 'prepare_playback', fake_prepare)
    yield value, selected, prepared, (source, other)
    value.player.native.blocked = False
    value.close(restore=False)


def prepare(value):
    value.begin(AudioBudget(threads=1, reserve_disk_bytes=0))
    return value.prepare(PlaybackControls(speed=1.5, pitch_semitones=3, volume=.5, pan=-.2))


def test_close_without_preparation_does_not_change_existing_queue_or_playback(workspace):
    value, current, copies, paths = workspace
    before = value.player.native.stops
    assert value.close() == []
    assert value.player.playlist == paths and value.player.path == paths[0]
    assert value.player.native.stops == before and value.player.status().state == 'playing'


def test_prepared_file_and_source_readers_survive_until_same_player_confirms_stop(workspace):
    value, current, copies, paths = workspace
    derivative = prepare(value)
    assert not value.player.native.plays[-1][0] == derivative.audio
    value.play(.3)
    assert value.player.native.plays[-1][0] == derivative.audio
    value.player.native.blocked = True
    with pytest.raises(RuntimeError, match='owned player'):
        value.close()
    assert derivative.audio.is_file() and derivative._reader is not None
    assert value.selection.source_owner is not None
    value.player.native.blocked = False
    count = len(value.player.native.plays)
    assert value.close() == []
    assert not derivative.audio.exists() and len(value.player.native.plays) == count
    assert value.player.playlist == paths and value.player.playlist_index == 0


def test_revoked_source_scope_refuses_play_then_closes_without_restoring_or_resuming(workspace):
    value, current, copies, paths = workspace
    derivative = prepare(value)
    before = len(value.player.native.plays)
    current[0] = False
    with pytest.raises(InterruptedError, match='selected library'):
        value.play()
    warnings = value.close()
    assert warnings and value.player.playlist == ()
    assert len(value.player.native.plays) == before and not derivative.audio.exists()


def test_delayed_source_reader_protects_or_detects_the_selected_original(workspace):
    value, current, copies, paths = workspace
    derivative = prepare(value)
    offset = wav_info(paths[0]).data_offset
    if os.name == 'nt':
        with pytest.raises(PermissionError):
            with paths[0].open('r+b') as stream:
                stream.seek(offset)
                stream.write(b'\x30\x00')
        assert value.ready() == derivative.audio
        assert value.close() == []
    else:
        with paths[0].open('r+b') as stream:
            stream.seek(offset)
            stream.write(b'\x30\x00')
        with pytest.raises(ValueError, match='source recording changed'):
            value.ready()
        assert value.close()
    assert not derivative.audio.exists()


def test_cancel_between_preparation_and_adoption_releases_both_readers_and_private_copy(
        workspace, monkeypatch):
    value, current, copies, paths = workspace
    cancelled = threading.Event()
    original = module.prepare_playback
    def stopped(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(module, 'prepare_playback', stopped)
    value.begin(AudioBudget(threads=1, reserve_disk_bytes=0))
    with pytest.raises(InterruptedError, match='cancelled'):
        value.prepare(PlaybackControls(), cancelled=cancelled.is_set)
    assert copies and not copies[-1].audio.exists() and value.selection is None
    offset = wav_info(paths[0]).data_offset
    with paths[0].open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x40\x00')


def test_source_final_validation_error_does_not_leak_private_pcm(workspace, monkeypatch):
    value, current, copies, paths = workspace
    original = module.verified_reader
    @contextmanager
    def refuse(*args, **kwargs):
        with original(*args, **kwargs) as stream:
            yield stream
        raise RuntimeError('Injected source reader refusal.')
    monkeypatch.setattr(module, 'verified_reader', refuse)
    derivative = prepare(value)
    assert value.close()
    assert not derivative.audio.exists() and value.selection is None


def test_pending_close_owner_waits_for_actual_future_before_touching_player(workspace, monkeypatch):
    value, current, copies, paths = workspace
    window = module.PlaybackControlsDialog.__new__(module.PlaybackControlsDialog)
    window.workspace, window._retained, window._cleanup_lock = value, None, threading.Lock()
    future = window._future = Future()
    attempts, owners = [], []
    def retained(attempt, *, message):
        attempts.append(attempt)
        owners.append(SimpleNamespace(quiescent_event=threading.Event()))
        return SimpleNamespace(cleanup_owner=owners[-1])
    monkeypatch.setattr(module, 'pending_cleanup', retained)
    window._retain_cleanup()
    window._retain_cleanup()
    before = value.player.native.stops
    assert len(attempts) == 1 and attempts[0]() is False
    assert value.player.native.stops == before and not value.closed
    future.set_result(None)
    attempts[0]()
    assert value.closed


def test_observer_failure_does_not_replace_real_operation_exception():
    calls = []
    class Observer:
        def bind(self, token):
            class Bound:
                def __enter__(self):
                    calls.append('bound')
                def __exit__(self, *args):
                    calls.append('unbound')
                    raise RuntimeError('Observer ended.')
            return Bound()
    with pytest.raises(ValueError, match='actual operation'):
        with module._observed(Observer(), 'token'):
            raise ValueError('actual operation')
    assert calls == ['bound', 'unbound']


def test_delayed_media_action_rechecks_original_scope_and_keeps_selected_position(workspace):
    value, current, copies, paths = workspace
    prepare(value)
    queued, reports = [], []
    class Progress:
        def media_action(self, action, **kwargs):
            queued.append((action, kwargs))
            return SimpleNamespace(close=lambda: None)
    window = module.PlaybackControlsDialog.__new__(module.PlaybackControlsDialog)
    window.workspace, window.progress = value, Progress()
    window.busy = window.closed = window.close_requested = window._destroyed = False
    window.alive, window._content_request = lambda: current[0], None
    window.report = reports.append
    position = ['.25']
    window.position = SimpleNamespace(GetValue=lambda: position[0])
    window.play()
    position[0] = '.8'
    assert queued and queued[0][1]['alive']()
    queued[0][0]()
    assert value.player.native.plays[-1][1] == .25
    window.play()
    before = len(value.player.native.plays)
    current[0] = False
    assert not queued[-1][1]['alive']()
    queued[-1][0]()
    assert len(value.player.native.plays) == before


def test_actual_studio_hook_reuses_player_executor_and_preserves_timer_intervals(tmp_path, monkeypatch):
    from carr_library.ui.audio_studio import AudioStudioDialog
    source = recording(tmp_path / 'source.wav')
    root = tmp_path / 'library'; root.mkdir()
    player = WavePlayer(); player.native = Device(); player.set_playlist([source])
    events, calls = [], []
    class Timer:
        def __init__(self, interval): self.interval, self.running = interval, True
        def IsRunning(self): return self.running
        def GetInterval(self): return self.interval
        def Stop(self): self.running = False; events.append(('stop', self.interval))
        def Start(self, interval): self.running = True; events.append(('start', interval))
    frame = SimpleNamespace(library=object(), _closing=False)
    choice = SimpleNamespace(GetSelection=lambda: 0, SetSelection=lambda index: calls.append(('select', index)))
    owner = SimpleNamespace(
        busy=False, closed=False, close_pending=False, editing_dirty=False,
        controller=SimpleNamespace(private_root=root, ffmpeg=None, result=None),
        parent=frame, playback=player, playlist_choice=choice, playback_device=choice,
        playback_device_ids=[None], cancelled=threading.Event(), executor=object(),
        operation_progress=object(), dialog=object(), timer=Timer(30000), playback_timer=Timer(500),
        _cancel_content_request=lambda: calls.append('cancel-content'),
        _error=lambda error: pytest.fail(str(error)),
        _refresh_playback_choices=lambda: calls.append('refresh'), refresh_playback=lambda: calls.append('status'))
    def show(parent, workspace, **kwargs):
        assert parent is owner.dialog and workspace.player is player
        assert kwargs['executor'] is owner.executor and kwargs['progress'] is owner.operation_progress
        assert owner.busy and not owner.timer.running and not owner.playback_timer.running
        assert kwargs['alive']()
        calls.append('same-player')
        workspace.close()
    monkeypatch.setattr(module, 'show_playback_controls', show)
    AudioStudioDialog.independent_playback(owner)
    assert not owner.busy and events == [('stop', 30000), ('stop', 500), ('start', 30000), ('start', 500)]
    assert 'same-player' in calls and ('select', 0) in calls and player.playlist == (source,)


@pytest.mark.parametrize('parent_destroyed', [False, True], ids=['studio-open', 'studio-destroyed'])
def test_forced_child_destroy_keeps_parent_player_and_timers_owned_until_real_future_quiescence(
        tmp_path, monkeypatch, parent_destroyed):
    from concurrent.futures import ThreadPoolExecutor
    from carr_library.ui.audio_studio import AudioStudioDialog
    source = recording(tmp_path / 'source.wav')
    root = tmp_path / 'library'; root.mkdir()
    player = WavePlayer(); player.native = Device(); player.set_playlist([source])
    started, release, posted = threading.Event(), threading.Event(), threading.Event()
    queued, events, errors, futures = [], [], [], []
    class Timer:
        def __init__(self, interval): self.interval, self.running = interval, True
        def IsRunning(self): return self.running
        def GetInterval(self): return self.interval
        def Stop(self): self.running = False; events.append(('stop', self.interval))
        def Start(self, interval): self.running = True; events.append(('start', interval))
    def post(action):
        queued.append(action)
        posted.set()
    choice = SimpleNamespace(GetSelection=lambda: 0, SetSelection=lambda index: events.append(('select', index)))
    frame = SimpleNamespace(library=object(), _closing=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = SimpleNamespace(
            busy=False, closed=False, close_pending=False, editing_dirty=False,
            controller=SimpleNamespace(private_root=root, ffmpeg=None, result=None),
            parent=frame, playback=player, playlist_choice=choice, playback_device=choice,
            playback_device_ids=[None], cancelled=threading.Event(), executor=executor,
            wx=SimpleNamespace(CallAfter=post), operation_progress=None, dialog=object(),
            timer=Timer(30000), playback_timer=Timer(500), _cancel_content_request=lambda: None,
            _error=errors.append, _refresh_playback_choices=lambda: events.append('refresh'),
            refresh_playback=lambda: events.append('status'))
        def forced_show(parent, workspace, **kwargs):
            def work():
                workspace.begin(AudioBudget(threads=1, reserve_disk_bytes=0))
                started.set()
                if not release.wait(5):
                    raise AssertionError('Test did not release the actual background operation.')
            future = executor.submit(work)
            futures.append(future)
            assert started.wait(3)
            child = module.PlaybackControlsDialog.__new__(module.PlaybackControlsDialog)
            child.workspace, child._future, child._retained = workspace, future, None
            child._cleanup_lock = threading.Lock()
            child.busy, child.closed, child._destroyed = True, False, False
            child.cancelled = threading.Event()
            child.report = lambda message: None
            child._cancel_content = lambda: None
            child.timer = SimpleNamespace(Stop=lambda: None)
            child.dialog = SimpleNamespace(ShowModal=lambda: 0, Destroy=lambda: None)
            # This executes the actual forced-return finally and hands out the
            # existing retained cleanup owner, not a fabricated completion flag.
            return child.show()
        monkeypatch.setattr(module, 'show_playback_controls', forced_show)
        try:
            AudioStudioDialog.independent_playback(owner)
            assert errors and owner._playback_cleanup_owner is errors[-1].cleanup_owner
            assert owner.busy and not owner.timer.running and not owner.playback_timer.running
            assert not owner._playback_cleanup_owner.quiescent_event.is_set()
            # Re-entry must not borrow/restart the same player while cleanup owns it.
            monkeypatch.setattr(module, 'show_playback_controls',
                lambda *a, **k: pytest.fail('Parent reused an owned player before quiescence.'))
            AudioStudioDialog.independent_playback(owner)
            assert len(futures) == 1 and not futures[0].done()
            owner.closed = parent_destroyed
            release.set()
            assert posted.wait(5)
            assert futures[0].done() and owner.busy
            assert owner._playback_cleanup_owner.quiescent_event.is_set()
            assert not any(event[0] == 'start' for event in events if isinstance(event, tuple))
            for action in tuple(queued):
                action()
                action()  # A repeated notification cannot restart timers twice.
            assert not owner.busy and owner._playback_cleanup_owner is None
            starts = [event for event in events if isinstance(event, tuple) and event[0] == 'start']
            assert starts == ([] if parent_destroyed else [('start', 30000), ('start', 500)])
        finally:
            release.set()
            for future in futures:
                future.result(timeout=5)
            pending = getattr(owner, '_playback_cleanup_owner', None)
            if pending is not None:
                assert pending.quiescent_event.wait(5)
