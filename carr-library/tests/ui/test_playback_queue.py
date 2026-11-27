"""Real worker, file and Save All lifetimes with an explicit device double."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from carr_library.audio.playlist import PlaylistStore, SaveReceipt, state_paths
from carr_library.audio.playlist_session import PlaylistSession
from carr_library.ui import playback_queue as module
from carr_library.ui.playback_queue import PlaybackQueueDialog
from carr_library.ui.save_all import SaveCoordinator
from tests.audio.playlist_fixture import Player, pcm, retained


class Control:
    def __init__(self, value=''):
        self.value, self.items, self.selection, self.enabled = value, [], -1, True
    def ChangeValue(self, value):
        self.value = value
    def GetValue(self):
        return self.value
    def SetItems(self, values):
        self.items = list(values)
    def SetSelection(self, index):
        self.selection = index
    def GetSelection(self):
        return self.selection
    def Enable(self, value=True):
        self.enabled = bool(value)
    def SetLabel(self, value):
        self.label = value
    def SetName(self, value):
        self.name = value
    def SetFocus(self):
        pass


def drain(value, callbacks, predicate=lambda value: value.future is None):
    deadline = monotonic() + 8
    while not predicate(value):
        if monotonic() >= deadline:
            raise AssertionError('The actual queue worker did not settle.')
        if callbacks:
            callbacks.popleft()()
        else:
            sleep(.001)
    while callbacks:
        callbacks.popleft()()


@pytest.fixture
def window(tmp_path):
    app, sources, rows, saved, receipt = retained(tmp_path, count=1)
    value = object.__new__(PlaybackQueueDialog)
    callbacks, disposed = deque(), []
    value.frame = SimpleNamespace(library=app, _closing=False)
    value.library, value.root = app, app.root
    info = app.root.stat()
    value.root_identity = info.st_dev, info.st_ino
    value.trust_controller = getattr(app, 'trust_controller', None)
    value.initial_paths, value.on_ready = (), None
    value.close_choice = 2
    value.wx = SimpleNamespace(CallAfter=lambda action, *args: callbacks.append(lambda: action(*args)),
        ID_CLOSE=4, ID_YES=1, ID_NO=2, ID_CANCEL=3, YES=1, NO=2,
        YES_NO=8, NO_DEFAULT=16, CANCEL=32, CANCEL_DEFAULT=64, ICON_QUESTION=128,
        MessageBox=lambda *args: 1,
        MessageDialog=lambda *args: SimpleNamespace(ShowModal=lambda: value.close_choice, Destroy=lambda: None))
    value.dialog = SimpleNamespace(IsModal=lambda: True,
        EndModal=lambda code: disposed.append(code), Destroy=lambda: disposed.append('destroy'))
    value.executor = ThreadPoolExecutor(max_workers=1)
    value.cancelled, value.serial = Event(), 0
    value.closed = value.destroyed = value.closing = value.closing_worker = False
    value.stopping = value.settling = False
    value.showing = True
    value.future = value.media_request = value.prepared = None
    value.progress = None
    value.player = Player()
    value.session = PlaylistSession(app.root, value.player)
    value.timer = SimpleNamespace(Stop=lambda: disposed.append('timer'))
    value.unregister_player = lambda: disposed.append('player')
    value.queue, value.seconds, value.status = Control(), Control('0'), Control()
    value.controls = {name: Control() for name in ('review', 'add', 'studio', 'remove', 'up', 'down',
        'clear', 'seek', 'back', 'forward', 'play', 'pause', 'resume', 'stop', 'save', 'save_all', 'close')}
    value.coordinator = SaveCoordinator()
    unregister = value.coordinator.register('queue', 'Saved playback queue',
        dirty=lambda: not value.closed and value.session.dirty, save=value.save_draft)
    def removed():
        unregister()
        disposed.append('save')
    value.unregister_save = removed
    value._display()
    value._enable()
    yield value, callbacks, disposed, tmp_path
    value.player.stop_failure = value.player.close_failure = value.player.play_failure = False
    value.player.current = replace(value.player.current, position_accuracy='not_started')
    value.frame.library, value.frame._closing = value.library, False
    if value.future is not None:
        drain(value, callbacks)
    if not value.closed:
        value.close_choice = value.wx.ID_NO
        value.close()
        drain(value, callbacks, lambda current: current.closed)
    value.executor.shutdown(wait=True, cancel_futures=True)


def review(value, callbacks):
    value.review()
    drain(value, callbacks)
    assert value.session.reviewed


def test_initial_window_is_passive_and_review_never_plays(window):
    value, callbacks, _, _ = window
    assert not value.session.reviewed and not value.player.calls
    assert not value.controls['play'].enabled and value.controls['review'].enabled
    review(value, callbacks)
    assert value.controls['play'].enabled
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_add_then_save_all_captures_real_independent_pcm_and_exact_order(window):
    value, callbacks, _, root = window
    review(value, callbacks)
    source = pcm(root / 'chosen.wav', value=456)
    original = source.read_bytes()
    assert value.add_paths((source,))
    drain(value, callbacks)
    assert len(value.session.items) == 2 and value.session.dirty
    assert value.session.items[-1].path.read_bytes() == original
    source.write_bytes(b'A separately changed original')
    summaries = []
    assert value.coordinator.save_all(summaries.append)
    drain(value, callbacks, lambda current: not current.coordinator.busy)
    assert len(summaries) == 1 and summaries[0].complete
    assert not value.session.dirty and PlaylistStore(value.root).load()[0] == value.session.value


def test_cancelled_capture_does_not_append_late_result_but_preserves_committed_copy(window, monkeypatch):
    value, callbacks, _, root = window
    review(value, callbacks)
    source = pcm(root / 'new.wav', value=456)
    entered, release = Event(), Event()
    original = module.retain_playlist
    def retained_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Capture completion barrier was not released.')
        return result
    monkeypatch.setattr(module, 'retain_playlist', retained_then_wait)
    value.add_paths((source,))
    try:
        assert entered.wait(5)
        value.stop()
    finally:
        release.set()
    drain(value, callbacks, lambda current: current.future is None and not current.stopping)
    assert len(value.session.items) == 1
    assert len([path for path in state_paths(value.root) if path.suffix == '.wav']) == 2
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_late_play_review_after_library_change_disposes_readers_without_start(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    entered, release, received = Event(), Event(), []
    original = value.session.prepare_play
    def prepare(**kwargs):
        result = original(**kwargs)
        received.append(result)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Playback review barrier was not released.')
        return result
    monkeypatch.setattr(value.session, 'prepare_play', prepare)
    value.play()
    try:
        assert entered.wait(5)
        value.frame.library = SimpleNamespace(root=value.root)
    finally:
        release.set()
    drain(value, callbacks)
    assert received[0].readers is None and value.session.admission is None
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_delayed_media_action_is_cancelled_before_it_can_start_stale_playback(window):
    value, callbacks, _, _ = window
    review(value, callbacks)
    pending, closed = [], []
    def media_action(action, **kwargs):
        pending.append(action)
        return SimpleNamespace(close=lambda: closed.append(True))
    value.progress = SimpleNamespace(media_action=media_action)
    value.play()
    drain(value, callbacks)
    prepared = value.prepared
    assert prepared is not None and prepared.readers is not None and len(pending) == 1
    value.stop()
    drain(value, callbacks, lambda current: not current.stopping)
    pending[0]()
    assert prepared.readers is None and closed
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_media_admission_failure_releases_prepared_readers(window):
    value, callbacks, _, _ = window
    review(value, callbacks)
    pending = []
    def media_action(action, **kwargs):
        pending.append(kwargs['failed'])
        return SimpleNamespace(close=lambda: None)
    value.progress = SimpleNamespace(media_action=media_action)
    value.play()
    drain(value, callbacks)
    prepared = value.prepared
    pending[0](RuntimeError('Progress audio could not settle.'))
    assert prepared.readers is None and value.prepared is None
    assert 'could not settle' in value.status.value
    # The real shared progress owner also closes abandoned scope requests
    # without a failure callback. The queue's timer releases that review too.
    request = SimpleNamespace(closed=False)
    request.close = lambda: setattr(request, 'closed', True)
    value.progress = SimpleNamespace(media_action=lambda action, **kwargs: request)
    value.play()
    drain(value, callbacks)
    abandoned = value.prepared
    request.closed = True
    value.tick()
    assert abandoned.readers is None and value.prepared is None
    assert not any(call[0] == 'play' for call in value.player.calls)


@pytest.mark.parametrize('operation', ['stop', 'close'])
def test_failed_native_cleanup_keeps_window_and_input_owner_for_retry(window, operation):
    value, callbacks, disposed, _ = window
    review(value, callbacks)
    value.play()
    drain(value, callbacks)
    admitted = value.session.admission
    setattr(value.player, operation + '_failure', True)
    getattr(value, operation)()
    drain(value, callbacks)
    assert not value.closed and not disposed and value.player.native is not None
    if operation == 'stop':
        assert value.session.admission is admitted and admitted.readers is not None
    else:
        # Close first completed an actual stop, so input readers may safely
        # close while the failed native handle owner stays available to retry.
        assert value.session.admission is None and admitted.readers is None
        assert value.player.status().state == 'stopped'
    setattr(value.player, operation + '_failure', False)
    getattr(value, operation)()
    drain(value, callbacks, lambda current: current.closed if operation == 'close' else not current.stopping)
    assert admitted.readers is None


def test_save_all_waits_for_publication_and_keeps_newer_draft_dirty(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    value.session.select(0, frame=100)
    entered, release = Event(), Event()
    original = value.session.store.save
    def written_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Save completion barrier was not released.')
        return result
    monkeypatch.setattr(value.session.store, 'save', written_then_wait)
    summaries = []
    assert value.coordinator.save_all(summaries.append)
    try:
        assert entered.wait(5)
        assert not summaries and value.coordinator.busy
        value.session.select(0, frame=200)
    finally:
        release.set()
    drain(value, callbacks, lambda current: not current.coordinator.busy)
    assert not summaries[0].complete and value.session.dirty and value.session.value.frame == 200
    assert value.session.store.load()[0].frame == 100


def test_save_commit_is_acknowledged_when_stop_arrives_after_publication(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    value.session.select(0, frame=1234)
    entered, release = Event(), Event()
    original = value.session.store.save
    def written_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Save completion barrier was not released.')
        return result
    monkeypatch.setattr(value.session.store, 'save', written_then_wait)
    results = []
    value.save_draft(results.append)
    try:
        assert entered.wait(5)
        value.stop()
    finally:
        release.set()
    drain(value, callbacks, lambda current: current.future is None and not current.stopping)
    assert len(results) == 1 and results[0].saved
    assert value.session.store.load()[0].frame == 1234 and not value.session.dirty


def test_unconfirmed_save_keeps_dirty_retry_token_and_does_not_complete_save_all(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    value.session.select(0, frame=234)
    original = value.session.store.save
    def unconfirmed(*args, **kwargs):
        receipt = original(*args, **kwargs)
        return replace(receipt, confirmed=False, warning='Retry the final directory flush.')
    monkeypatch.setattr(value.session.store, 'save', unconfirmed)
    summaries = []
    value.coordinator.save_all(summaries.append)
    drain(value, callbacks, lambda current: not current.coordinator.busy)
    assert not summaries[0].complete and value.session.dirty
    assert value.session.token == value.session.store.load()[1]
    monkeypatch.setattr(value.session.store, 'save', original)
    value.coordinator.save_all(summaries.append)
    drain(value, callbacks, lambda current: not current.coordinator.busy)
    assert summaries[-1].complete and not value.session.dirty


@pytest.mark.parametrize('answer,closed,saved', [(1, True, True), (2, True, False), (3, False, False)])
def test_dirty_close_uses_actual_save_discard_or_cancel(window, answer, closed, saved):
    value, callbacks, disposed, _ = window
    review(value, callbacks)
    before = value.session.store.load()
    value.session.select(0, frame=600)
    value.close_choice = answer
    value.close()
    drain(value, callbacks, lambda current: current.closed or not current.closing)
    assert value.closed == closed
    current = value.session.store.load()
    assert (current[0].frame == 600) == saved
    if not saved:
        assert current == before


def test_close_during_save_all_waits_for_the_current_confirmed_save(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    value.session.select(0, frame=345)
    entered, release = Event(), Event()
    original = value.session.store.save
    def written_then_wait(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Save barrier was not released.')
        return result
    monkeypatch.setattr(value.session.store, 'save', written_then_wait)
    summaries = []
    value.coordinator.save_all(summaries.append)
    try:
        assert entered.wait(5)
        value.close()
        assert not value.closed and not value.closing
    finally:
        release.set()
    drain(value, callbacks, lambda current: current.closed)
    assert summaries[0].complete and value.session.store.load()[0].frame == 345


def test_revoked_host_still_allows_cleanup_and_close_without_discarding_saved_state(window):
    value, callbacks, disposed, _ = window
    review(value, callbacks)
    value.play()
    drain(value, callbacks)
    admitted = value.session.admission
    before = value.session.store.load()
    value.frame._closing = True
    value.close_choice = value.wx.ID_NO
    value.close()
    drain(value, callbacks, lambda current: current.closed)
    assert admitted.readers is None and disposed
    assert value.session.store.load() == before


def test_manual_selection_and_skip_stop_playback_and_never_autoplay(window):
    value, callbacks, _, _ = window
    review(value, callbacks)
    value.play()
    drain(value, callbacks)
    count = sum(call[0] == 'play' for call in value.player.calls)
    value.seconds.value = '.75'
    value.seek()
    assert value.session.value.frame == 6000 and value.session.admission is None
    value.skip(-1)
    assert value.session.value.frame == 0
    value.skip(1)
    assert value.session.value.frame == 8000
    assert sum(call[0] == 'play' for call in value.player.calls) == count


def test_unreviewed_saved_queue_cannot_be_saved_as_if_its_media_were_checked(window):
    value, _, _, _ = window
    results = []
    value.save_draft(results.append)
    assert len(results) == 1 and not results[0].saved and 'Review' in results[0].message


def test_stale_delayed_play_admission_is_rejected_and_closed_by_ui_owner(window):
    value, callbacks, _, _ = window
    review(value, callbacks)
    delayed = []
    value.progress = SimpleNamespace(media_action=lambda action, **kwargs:
        (delayed.append(action) or SimpleNamespace(close=lambda: None)))
    value.play()
    drain(value, callbacks)
    admission = value.prepared
    value.session.select(0, frame=99)
    delayed[0]()
    assert admission.readers is None and value.prepared is None and value.session.admission is None
    assert not any(call[0] == 'play' for call in value.player.calls)
    assert 'exact current playback review' in value.status.value


@pytest.mark.parametrize('operation', ['review', 'add'])
def test_current_trust_admission_refuses_new_review_or_capture_before_mutation(window, monkeypatch, operation):
    value, callbacks, _, root = window
    if operation == 'add':
        review(value, callbacks)
        source = pcm(root / 'blocked.wav', value=999)
    before = state_paths(value.root)
    def refused(controller, domain):
        assert domain == 'data.read'
        raise PermissionError('Current library admission revoked.')
    monkeypatch.setattr(module, 'admit_new_operation', refused)
    value.review() if operation == 'review' else value.add_paths((source,))
    drain(value, callbacks)
    assert state_paths(value.root) == before and 'admission revoked' in value.status.value
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_delayed_play_rechecks_current_trust_and_disposes_refused_admission(window, monkeypatch):
    value, callbacks, _, _ = window
    review(value, callbacks)
    delayed = []
    value.progress = SimpleNamespace(media_action=lambda action, **kwargs:
        (delayed.append(action) or SimpleNamespace(close=lambda: None)))
    value.play()
    drain(value, callbacks)
    prepared = value.prepared
    def refused(controller, domain):
        assert domain == 'data.read'
        raise PermissionError('Current playback admission revoked.')
    monkeypatch.setattr(module, 'admit_new_operation', refused)
    delayed[0]()
    assert prepared.readers is None and value.session.admission is None
    assert not any(call[0] == 'play' for call in value.player.calls)


def test_real_first_thread_start_failure_cannot_leave_an_unowned_stale_action(window, monkeypatch):
    from threading import Thread
    value, callbacks, _, root = window
    original = Thread.start
    calls = 0
    def fail_first(thread):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('Actual first thread start refused.')
        return original(thread)
    monkeypatch.setattr(Thread, 'start', fail_first)
    stale_output = root / 'unowned-stale-result.txt'
    with pytest.raises(RuntimeError, match='first thread start refused'):
        value._work(lambda: stale_output.write_text('This stale action must never run.', encoding='utf-8'),
                    lambda _: pytest.fail('A failed submission delivered a stale success.'))
    assert value.future is None and not stale_output.exists()
    received = []
    assert value._work(lambda: 'later explicitly admitted action', received.append)
    drain(value, callbacks)
    assert received == ['later explicitly admitted action'] and not stale_output.exists()
