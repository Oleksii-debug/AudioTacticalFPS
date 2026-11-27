"""Current Studio worker and Save All own real queue commits."""
from concurrent.futures import Future
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest

from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.player import WavePlayer
from carr_library.audio.playlist import PlaylistStore
from carr_library.ui.audio_studio import AudioStudioDialog
from carr_library.ui.playback_queue import StudioQueue
from carr_library.ui.save_all import SaveCoordinator
from tests.audio.test_managed_playback_queue import project


class DeferredExecutor:
    def __init__(self):
        self.pending = []
        self.reject_after_enqueue = False

    def submit(self, action):
        future = Future()
        self.pending.append((action, future))
        if self.reject_after_enqueue:
            self.reject_after_enqueue = False
            raise RuntimeError('Synthetic native thread start refused after enqueue')
        return future

    def complete(self):
        action, future = self.pending.pop(0)
        try:
            future.set_result(action())
        except BaseException as error:
            future.set_exception(error)


class Field:
    def __init__(self):
        self.selection = 0
        self.enabled = True

    def Enable(self, enabled=True):
        self.enabled = enabled

    def Disable(self):
        self.enabled = False

    def SetFocus(self):
        pass

    def GetSelection(self):
        return self.selection

    def SetSelection(self, selected):
        self.selection = selected


class Device:
    def __init__(self):
        self.snapshot = PlaybackStatus()
        self.plays = []

    def status(self):
        return self.snapshot

    def stop(self):
        self.snapshot = replace(self.snapshot, state='stopped', position_frames=0)

    def play(self, path, *, start_seconds=0, end_seconds=None, device_id=None):
        self.plays.append((path, start_seconds, device_id))
        self.snapshot = PlaybackStatus(state='playing', path=str(path), position_frames=4000,
            sample_rate=8000, total_frames=8000, channels=1, device_id=device_id,
            position_accuracy='completed_buffers')


@pytest.fixture
def window(project):
    _, controller, store, paths = project
    import threading
    callbacks, reports, errors = [], [], []
    player = WavePlayer()
    player.native = Device()
    owner = SimpleNamespace(controller=controller, playback=player, closed=False, busy=False,
        close_pending=False, cancelled=threading.Event(), executor=DeferredExecutor(),
        controls=[], windows_playback_controls=[], cancel_button=Field(), playlist_choice=Field(),
        playback_device=Field(), playback_device_ids=[None, 4],
        wx=SimpleNamespace(CallAfter=callbacks.append), operation_progress=None,
        _cancel_content_request=lambda: None, _refresh_playback_choices=lambda: None,
        _message=lambda text, **kwargs: reports.append(text), _error=errors.append)
    owner._run = MethodType(AudioStudioDialog._run, owner)
    owner._playback_action = lambda action: action()
    queue = owner.queue_state = StudioQueue(owner)
    queue.bind()
    queue.store = store
    queue._apply(store.append(paths), install=True)
    coordinator = SaveCoordinator()
    queue.register(coordinator)
    def drain():
        while callbacks:
            callbacks.pop(0)()
    return owner, queue, coordinator, drain, errors


def measured(window):
    owner, queue, *_ = window
    owner.playback.play_index(1, device_id=4)
    queue.observe()
    assert queue.cursor == (1, 4000, 'completed_buffers')


def test_save_all_acknowledges_only_after_actual_commit_and_ui_settlement(window):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    before = queue.store.path.read_bytes()
    results = []
    assert coordinator.save_all(results.append)
    assert results == [] and owner.busy and queue.store.path.read_bytes() == before
    owner.executor.complete()
    assert results == [] and queue.store.path.read_bytes() != before
    drain()
    assert len(results) == 1 and results[0].complete and not owner.busy and not errors
    assert queue.store.review().state.frame == 4000


def test_save_all_keeps_dirty_cursor_after_failed_durable_publication(window, monkeypatch):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    before = queue.store.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Synthetic full disk before queue publication')
    monkeypatch.setattr(queue.store, '_write', fail)
    results = []
    coordinator.save_all(results.append)
    owner.executor.complete()
    drain()
    assert len(results) == 1 and not results[0].complete and queue.dirty()
    assert queue.store.path.read_bytes() == before and errors


def test_later_observed_position_is_not_erased_by_older_save_completion(window):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    results = []
    coordinator.save_all(results.append)
    owner.playback.native.snapshot = replace(owner.playback.native.snapshot, position_frames=4800)
    queue.observe()
    owner.executor.complete()
    drain()
    assert queue.store.review().state.frame == 4000
    assert queue.cursor[1] == 4800 and queue.dirty()
    assert not results[0].complete and not errors


def test_failed_thread_admission_cannot_replay_queue_write_after_later_submission(window):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    before = queue.store.path.read_bytes()
    owner.executor.reject_after_enqueue = True
    results = []
    coordinator.save_all(results.append)
    assert len(results) == 1 and not results[0].complete and not owner.busy
    assert len(owner.executor.pending) == 1
    reads = []
    owner._run('Later accepted read', lambda: 7, reads.append, quiet=True)
    owner.executor.complete()
    assert queue.store.path.read_bytes() == before
    owner.executor.complete()
    drain()
    assert reads == [7] and queue.store.path.read_bytes() == before


def test_restore_is_inert_and_explicit_resume_uses_fresh_output_choice(window):
    owner, queue, _, drain, errors = window
    measured(window)
    queue.save_now()
    owner.playback.stop()
    count = len(owner.playback.native.plays)
    queue.restore()
    owner.executor.complete()
    drain()
    assert len(owner.playback.native.plays) == count and owner.playback.status().state == 'stopped'
    owner.playback_device.SetSelection(1)
    queue.restore(resume=True)
    owner.executor.complete()
    drain()
    assert owner.playback.native.plays[-1] == (queue.store.paths(queue.review)[1], .5, 4)
    assert len(owner.playback.native.plays) == count + 1 and not errors


def test_external_player_unknown_cursor_refuses_resume_without_fabricated_position(window):
    owner, queue, _, drain, errors = window
    queue.cursor = (0, None, 'external_player')
    queue.save_now()
    queue.restore(resume=True)
    owner.executor.complete()
    drain()
    assert owner.playback.native.plays == []
    assert any('No measured cursor' in str(error) for error in errors)


def test_autosave_uses_same_worker_without_stopping_current_playback(window):
    owner, queue, _, drain, errors = window
    measured(window)
    queue.autosave()
    assert owner.playback.status().state == 'playing'
    owner.executor.complete()
    drain()
    assert queue.store.review().state.frame == 4000 and not errors


def test_project_switch_discards_in_memory_activation_only_after_bounded_save(window):
    owner, queue, _, _, _ = window
    measured(window)
    queue.save_now()
    previous = queue.store
    owner.controller.new('Another project')
    queue.bind()
    assert queue.review is None and owner.playback.playlist == ()
    assert previous.review().state.frame == 4000


def test_library_switch_revokes_queued_write_and_still_settles_save_all(window):
    owner, queue, coordinator, drain, errors = window
    library = object()
    owner.parent = SimpleNamespace(library=library)
    queue.library = library
    measured(window)
    before = queue.store.path.read_bytes()
    results = []
    coordinator.save_all(results.append)
    owner.parent.library = object()
    owner.executor.complete()
    drain()
    assert len(results) == 1 and not results[0].complete and not owner.busy
    assert queue.store.path.read_bytes() == before and errors



@pytest.mark.parametrize('revocation', ['closed-before-worker', 'closed-after-commit', 'stale', 'destroyed'], ids=str)
def test_lost_window_still_settles_save_all_once_after_the_accepted_future(window, revocation):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    before = queue.store.path.read_bytes()
    results = []
    assert coordinator.save_all(results.append)
    assert coordinator.busy and not results
    if revocation == 'closed-after-commit':
        owner.executor.complete()
        assert queue.store.path.read_bytes() != before
    class DeadControl:
        def Enable(self, *args):
            raise AssertionError('A destroyed control was accessed')
        Disable = Enable
        SetFocus = Enable
    owner.controls = [DeadControl()]
    owner.cancel_button = DeadControl()
    if revocation == 'stale':
        owner._job_token = object()
    elif revocation == 'destroyed':
        owner.dialog = SimpleNamespace(IsBeingDeleted=lambda: True)
    else:
        owner.closed = True
    # This is the existing forced-show teardown's actual unregister operation.
    queue.unregister()
    assert coordinator.busy and not results
    if revocation != 'closed-after-commit':
        owner.executor.complete()
    assert coordinator.busy and not results
    drain()
    assert len(results) == 1 and not results[0].complete and not coordinator.busy
    assert bool(owner.busy) is (revocation == 'stale')
    if revocation == 'closed-before-worker':
        assert queue.store.path.read_bytes() == before
    if revocation == 'closed-after-commit':
        assert queue.store.review().state.frame == 4000
    assert not errors
    drain()
    assert len(results) == 1


def test_native_completion_observer_failure_cannot_leave_save_all_waiting(window):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    results = []
    coordinator.save_all(results.append)
    class BrokenObserver:
        def Enable(self, *args):
            raise RuntimeError('Synthetic native control observer failure')
    owner.controls = [BrokenObserver()]
    owner.executor.complete()
    drain()
    assert len(results) == 1 and not results[0].complete and not coordinator.busy
    assert queue.store.review().state.frame == 4000
    assert any('observer failure' in str(error) for error in errors)


def test_save_all_adopts_committed_receipt_and_requires_review_without_repeating_write(window, monkeypatch):
    from carr_library.audio import playlist
    owner, queue, coordinator, drain, errors = window
    measured(window)
    writes = []
    real = playlist.replace_json_file
    def replace_once(source, destination):
        writes.append(destination)
        return real(source, destination)
    def refuse(directory):
        raise OSError('Synthetic final directory durability failure')
    monkeypatch.setattr(playlist, 'replace_json_file', replace_once)
    monkeypatch.setattr(playlist, '_sync_directory', refuse)
    results = []
    coordinator.save_all(results.append)
    owner.playback.native.snapshot = replace(owner.playback.native.snapshot, position_frames=4800)
    queue.observe()
    owner.executor.complete()
    drain()
    assert len(results) == 1 and not results[0].complete
    assert queue.review.state.frame == 4000 and queue.cursor[1] == 4800
    assert queue.review.digest == playlist.sha256(queue.store.path.read_bytes()).hexdigest()
    assert isinstance(queue._save_warning, playlist.QueueChangeCommittedError)
    assert queue.dirty() and len(writes) == 1
    queue.autosave()
    assert not owner.executor.pending and len(writes) == 1
    results.clear()
    coordinator.save_all(results.append)
    assert len(results) == 1 and not results[0].complete and len(writes) == 1
    queue.restore()
    owner.executor.complete()
    drain()
    assert queue._save_warning is None and not queue.dirty()
    assert queue.cursor[1] == 4000 and len(writes) == 1


@pytest.mark.parametrize('flush', ['success', 'failed'], ids=str)
def test_save_as_flushes_original_queue_before_controller_path_changes(window, tmp_path, monkeypatch, flush):
    from carr_library.ui import audio_studio
    from carr_library.research.export_file import capture_export_destination
    owner, queue, _, _, errors = window
    measured(window)
    old_store = queue.store
    old_path = owner.controller.project_path
    destination = tmp_path / 'explicit-copy.json'
    owner.dialog = object()
    owner._capture = lambda: None
    owner._file = lambda *args, **kwargs: destination
    owner._refresh_text = queue.bind
    monkeypatch.setattr(audio_studio, 'approve_export_destination',
                        lambda dialog, path, **kwargs: capture_export_destination(
                            path, private_root=kwargs['private_root']))
    if flush == 'failed':
        def refuse(*args, **kwargs):
            raise OSError('Synthetic old-queue flush refused')
        monkeypatch.setattr(old_store, 'checkpoint', refuse)
    result = AudioStudioDialog.save_as(owner)
    if flush == 'success':
        assert result is True and owner.controller.project_path == destination
        assert old_store.review().state.frame == 4000
        assert destination.is_file() and queue.review is None and not errors
    else:
        assert result is False and owner.controller.project_path == old_path
        assert not destination.exists() and queue.dirty()
        assert any('old-queue flush' in str(error) for error in errors)


def test_append_postcommit_failure_keeps_receipt_without_auto_install_or_retry(window, monkeypatch, tmp_path):
    from carr_library.audio import playlist
    from tests.audio.test_managed_playback_queue import wav
    owner, queue, _, drain, errors = window
    original_paths = owner.playback.playlist
    source = wav(tmp_path / 'third.wav', b'\x30\x00')
    def refuse(directory):
        raise OSError('Synthetic queue sync failure')
    monkeypatch.setattr(playlist, '_sync_directory', refuse)
    queue.append([source])
    owner.executor.complete()
    drain()
    assert len(queue.review.state.items) == 3
    assert isinstance(queue._save_warning, playlist.QueueChangeCommittedError)
    assert owner.playback.playlist == original_paths
    assert owner.playback.native.plays == []
    queue.autosave()
    assert not owner.executor.pending and errors


def test_historical_cursor_is_explicit_and_remains_inert_until_resume(window):
    owner, queue, _, drain, errors = window
    queue.review = queue.store.checkpoint(queue.review, index=0, frame=2000,
                                          accuracy='historical_cursor')
    queue.restore()
    owner.executor.complete()
    drain()
    assert queue.cursor == (0, 2000, 'historical_cursor')
    assert owner.playback.native.plays == []
    queue.restore(resume=True)
    owner.executor.complete()
    drain()
    assert owner.playback.native.plays[-1][1] == .25
    queue.observe()
    assert queue.cursor[2] == 'completed_buffers' and not errors



def test_destroyed_native_window_cannot_receive_save_all_failure_focus(window):
    owner, queue, coordinator, drain, errors = window
    measured(window)
    results, touched = [], []
    coordinator.save_all(results.append)
    owner.dialog = SimpleNamespace(IsBeingDeleted=lambda: True)
    owner.playlist_choice.SetFocus = lambda: touched.append('dead native focus')
    # Even before ordinary show teardown unregisters the participant, a
    # completed worker must settle the coordinator without focusing the HWND.
    owner.executor.complete()
    drain()
    assert len(results) == 1 and not results[0].complete and not coordinator.busy
    assert touched == [] and not errors
