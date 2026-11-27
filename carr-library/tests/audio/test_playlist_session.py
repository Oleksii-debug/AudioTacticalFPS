"""Explicit review, native-observed frames and owned input lifetimes."""
from dataclasses import replace
from pathlib import Path

import pytest

from carr_library.audio import playlist_session as module
from carr_library.audio.playlist import SavedPlaylist, SaveReceipt
from carr_library.audio.playlist_session import PlaylistSession
from tests.audio.playlist_fixture import Player, retained


@pytest.fixture
def session(tmp_path):
    app, sources, rows, saved, receipt = retained(tmp_path)
    player = Player()
    value = PlaylistSession(app.root, player)
    yield value, player, app, sources, rows, saved, receipt
    player.stop_failure = player.close_failure = player.play_failure = False
    player.current = replace(player.current, position_accuracy='not_started')
    value.close()


def ready(value):
    value.adopt(value.review())
    return value


def playing(value):
    ready(value)
    admitted = value.prepare_play()
    value.start(admitted)
    return admitted


def test_reopen_reads_only_passive_state_until_explicit_review(session):
    value, player, _, _, _, saved, receipt = session
    assert value.value == saved and value.token == receipt.token and not value.reviewed
    assert not player.calls and value.admission is None
    with pytest.raises(ValueError, match='Review'):
        value.prepare_play()
    ready(value)
    assert value.reviewed and all(call[0] != 'play' for call in player.calls)


def test_explicit_play_uses_saved_original_frame_and_default_device(session):
    value, player, _, _, _, _, _ = session
    ready(value)
    value.select(1, frame=4321)
    admitted = value.prepare_play()
    try:
        assert not any(call[0] == 'play' for call in player.calls)
        value.start(admitted)
        assert player.calls[-1] == ('play', 1, 4321 / 8000, None)
        assert value.admission is admitted and admitted.readers is not None
    finally:
        value.stop()
    assert admitted.readers is None


@pytest.mark.parametrize('accuracy', ['driver_reported', 'completed_buffers'])
def test_only_actual_native_observed_frames_become_a_saved_cursor(session, accuracy):
    value, player, *_ = session
    playing(value)
    player.current = replace(player.current, position_frames=3467, position_accuracy=accuracy)
    snapshot, _ = value.snapshot()
    assert snapshot.frame == 3467 and snapshot.accuracy == accuracy
    assert value.admission is None and player.status().state == 'stopped'
    assert value.dirty


@pytest.mark.parametrize('accuracy', ['not_started', 'external_player', 'unknown'])
def test_unreported_playhead_never_becomes_a_wall_clock_guess(session, accuracy):
    value, player, *_ = session
    playing(value)
    player.current = replace(player.current, position_frames=7654, position_accuracy=accuracy)
    value.capture_position()
    assert value.value.frame == 0 and value.value.accuracy == 'manual'


def test_automatic_next_track_updates_selection_only_from_current_pcm_status(session):
    value, player, *_ = session
    playing(value)
    player.current = replace(player.current, state='completed', position_frames=8000)
    player.next_poll = lambda: player.play_index(1, start_seconds=.125)
    value.poll()
    assert value.value.index == 1 and value.value.frame == 1000
    assert value.admission is not None
    player.current = replace(player.current, state='completed', position_frames=8000)
    value.poll()
    assert value.value.index == 1 and value.value.frame == 8000
    assert value.admission is None


@pytest.mark.parametrize('operation', ['stop', 'close'])
def test_failed_native_cleanup_retains_verified_readers_for_retry(session, operation):
    value, player, *_ = session
    admitted = playing(value)
    setattr(player, operation + '_failure', True)
    with pytest.raises(RuntimeError, match='did not settle'):
        getattr(value, operation)()
    assert value.admission is admitted and admitted.readers is not None
    setattr(player, operation + '_failure', False)
    getattr(value, operation)()
    assert value.admission is None and admitted.readers is None


def test_bad_status_does_not_prevent_actual_native_stop_or_reader_release(session):
    value, player, *_ = session
    admitted = playing(value)
    player.current = replace(player.current, path='another.wav', position_frames=2000)
    with pytest.raises(ValueError, match='identity'):
        value.stop()
    assert player.status().state == 'stopped'
    assert value.admission is None and admitted.readers is None


def test_native_start_failure_keeps_reader_until_failed_cleanup_can_be_retried(session):
    value, player, *_ = session
    ready(value)
    admitted = value.prepare_play()
    player.play_failure = True
    # The first set_playlist must still finish; inject cleanup refusal only
    # after the native facade has begun owning its input.
    actual = player.play_index
    def fail_after_start(*args, **kwargs):
        try:
            return actual(*args, **kwargs)
        finally:
            player.stop_failure = True
    player.play_index = fail_after_start
    with pytest.raises(RuntimeError, match='stop did not settle'):
        value.start(admitted)
    assert value.admission is admitted and admitted.readers is not None
    player.stop_failure = False
    value.stop()
    assert admitted.readers is None


def test_admission_cannot_replay_after_queue_selection_changes(session):
    value, player, *_ = session
    ready(value)
    admitted = value.prepare_play()
    try:
        value.select(1)
        with pytest.raises(ValueError, match='exact current'):
            value.start(admitted)
        assert not any(call[0] == 'play' for call in player.calls)
    finally:
        admitted.close()


def test_changed_retained_bytes_fail_before_any_player_start(session):
    value, player, _, _, rows, *_ = session
    ready(value)
    path = rows[0].path
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    with pytest.raises((ValueError, RuntimeError)):
        value.prepare_play()
    assert not any(call[0] == 'play' for call in player.calls)


def test_cancelled_preparation_closes_all_actual_input_readers(session):
    value, player, *_ = session
    ready(value)
    count = 0
    def cancelled():
        nonlocal count
        count += 1
        return count >= 3
    with pytest.raises(InterruptedError):
        value.prepare_play(cancelled=cancelled)
    assert value.admission is None and not any(call[0] == 'play' for call in player.calls)
    # A completed cancelled review must not leave a Windows deny-write handle.
    for item in value.items:
        with item.path.open('r+b') as stream:
            stream.seek(0)


def test_review_result_is_rejected_when_another_window_saved_newer_state(session):
    value, player, app, _, _, saved, receipt = session
    reviewed = value.review()
    value.store.save(replace(saved, index=1), expected_token=receipt.token)
    with pytest.raises(ValueError, match='changed during review'):
        value.adopt(reviewed)
    assert not value.reviewed and not player.calls


def test_append_captures_current_cursor_before_stopping(session):
    value, player, _, _, rows, *_ = session
    playing(value)
    player.current = replace(player.current, position_frames=3210)
    value.append((rows[1],))
    assert value.value.frame == 3210 and value.value.index == 0 and len(value.items) == 3
    assert value.admission is None and all(row.path.exists() for row in value.items)


def test_reorder_and_remove_preserve_the_selected_recording_and_frame(session):
    value, _, _, _, rows, *_ = session
    ready(value)
    value.select(1, frame=900)
    assert value.move(1, -1) == 0
    assert value.value.index == 0 and value.value.frame == 900 and value.items[0] == rows[1]
    value.remove(1)
    assert value.value.index == 0 and value.value.frame == 900
    value.remove(0)
    assert value.value == SavedPlaylist() and value.items == ()


@pytest.mark.parametrize('index,frame', [(True, 0), (-1, 0), (2, 0), (0, -1), (0, 8001), (0, False)])
def test_invalid_manual_selection_preserves_an_existing_valid_draft(session, index, frame):
    value, *_ = session
    ready(value)
    before = value.value
    with pytest.raises(ValueError):
        value.select(index, frame=frame)
    assert value.value == before


def test_save_acknowledgement_retains_newer_edits_as_dirty(session):
    value, _, _, _, _, saved, receipt = session
    ready(value)
    value.select(1, frame=100)
    frozen, token = value.snapshot()
    written = value.store.save(frozen, expected_token=token)
    value.select(0, frame=200)
    value.acknowledge(written)
    assert value.token == written.token and value.baseline == frozen
    assert value.value.frame == 200 and value.dirty


def test_unconfirmed_post_commit_receipt_keeps_dirty_state_and_retry_token(session):
    value, _, _, _, _, saved, receipt = session
    ready(value)
    newer = replace(saved, frame=800)
    value.value = newer
    value.acknowledge(SaveReceipt('a' * 64, newer, False, 'retry'))
    assert value.dirty and value.token == 'a' * 64 and value.baseline == saved


def test_pause_resume_requires_an_explicit_active_admission(session):
    value, player, *_ = session
    ready(value)
    with pytest.raises(ValueError, match='Choose Play'):
        value.resume()
    playing(value)
    player.current = replace(player.current, position_frames=400)
    assert value.pause() and player.status().state == 'paused' and value.value.frame == 400
    assert value.resume() and player.status().state == 'playing'


def test_closed_session_does_not_admit_new_work(session):
    value, *_ = session
    value.close()
    with pytest.raises(ValueError, match='closed'):
        value.review()
