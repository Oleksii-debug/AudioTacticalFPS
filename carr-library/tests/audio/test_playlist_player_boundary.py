"""Existing WavePlayer keeps its default queue behavior with explicit resume."""
import math
import pytest

from tests.audio.test_player import native_player, recording


def test_existing_wave_player_resumes_exact_queue_member_at_explicit_position(tmp_path, native_player):
    player, state = native_player
    one = recording(tmp_path / 'one.wav')
    two = recording(tmp_path / 'two.wav')
    player.set_playlist((one, two))
    player.play_index(1, start_seconds=.375)
    assert player.path == two and player.playlist_index == 1
    assert player.position_seconds == .375
    player.play_index(0)
    assert player.path == one and player.position_seconds == 0


@pytest.mark.parametrize('seconds', [-1, 2, math.nan, math.inf, True])
def test_invalid_explicit_queue_cursor_does_not_interrupt_current_valid_playback(tmp_path, native_player, seconds):
    player, state = native_player
    path = recording(tmp_path / 'recording.wav')
    player.set_playlist((path,))
    player.play_index(0)
    calls = len(state['plays'])
    with pytest.raises(ValueError):
        player.play_index(0, start_seconds=seconds)
    assert len(state['plays']) == calls and player.path == path and player.state == 'playing'
