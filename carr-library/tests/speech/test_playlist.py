"""Actual local PCM review and production WavePlayer queue transitions."""
from hashlib import sha256
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import player as module
from carr_library.speech.playlist import prepare_playlist


@pytest.fixture
def recordings(tmp_path):
    result = []
    for index in range(3):
        path = tmp_path / f'Черга {index}.wav'
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
            stream.writeframes(bytes((index, 0)) * 8000)
        result.append(path)
    return result


class Mci:
    def __init__(self):
        self.calls, self.mode, self.position = [], 'stopped', 0

    def __call__(self, command):
        self.calls.append(command)
        action = command.split()[0]
        if action == 'status':
            return self.mode if command.endswith('mode') else str(self.position)
        if action in ('play', 'resume'):
            self.mode, self.position = 'playing', 0
        elif action == 'pause':
            self.mode = 'paused'
        elif action in ('stop', 'close'):
            self.mode = 'stopped'
        return ''

    @property
    def starts(self):
        return [value for value in self.calls if value.startswith(('play ', 'resume '))]

    def finish(self):
        self.mode, self.position = 'stopped', 1000


@pytest.fixture
def queued(recordings, monkeypatch):
    value = module.WavePlayer()
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    mci = Mci()
    value._mci = mci
    value.set_playlist(prepare_playlist(recordings))
    yield value, mci
    value.close()


def test_review_has_exact_pcm_hash_and_never_opens_a_playback_device(recordings, monkeypatch):
    monkeypatch.setattr(module.WavePlayer, '_mci', lambda *args: pytest.fail('review played audio'))
    items = prepare_playlist(recordings)
    assert tuple(row.path for row in items) == tuple(recordings)
    for item, path in zip(items, recordings):
        assert item.sha256 == sha256(path.read_bytes()).hexdigest()
        assert (item.frames, item.sample_rate, item.duration_seconds) == (8000, 8000, 1)


def test_cancelled_or_truncated_review_never_returns_partial_authority(recordings):
    calls = []
    def cancelled():
        calls.append(1)
        return len(calls) > 3
    with pytest.raises(InterruptedError):
        prepare_playlist(recordings, cancelled=cancelled)
    path = recordings[0]
    with path.open('r+b') as stream:
        stream.truncate(100)
    with pytest.raises(ValueError, match='complete'):
        prepare_playlist([path])


@pytest.mark.parametrize('paths', [[], (), 'source.wav', [None] * 257])
def test_review_entry_bound_is_enforced_before_open(paths):
    with pytest.raises(ValueError):
        prepare_playlist(paths)


def test_select_replace_and_open_are_passive(queued):
    player, mci = queued
    assert player.path is None and player.playlist_index is None and not mci.starts
    player.select_queue(1)
    assert player.path == player.playlist[1].path and player.state == 'stopped'
    assert not mci.starts and not player.queue_active
    items = player.playlist
    player.set_playlist((items[2], items[0]))
    assert player.path is None and player.playlist_index is None and not mci.starts


def test_poll_reports_transition_without_autoplay_and_admission_starts_once(queued):
    player, mci = queued
    assert player.play_queue(0) and len(mci.starts) == 1
    mci.finish()
    assert player.poll() == 'queue-pending'
    assert player.poll() == 'queue-pending' and len(mci.starts) == 1
    assert player.advance_if_ready()
    assert player.path == player.playlist[1].path and player.playlist_index == 1
    assert len(mci.starts) == 2 and not player.advance_if_ready()


@pytest.mark.parametrize('action', ['stop', 'pause', 'select', 'replace', 'clear', 'close'])
def test_explicit_actions_revoke_waiting_queue_transition(queued, action):
    player, mci = queued
    player.play_queue()
    mci.finish()
    assert player.queue_pending
    before = list(mci.starts)
    {
        'stop': player.stop, 'pause': player.pause, 'select': lambda: player.select_queue(2),
        'replace': lambda: player.set_playlist(tuple(reversed(player.playlist))),
        'clear': lambda: player.set_playlist(()), 'close': player.close,
    }[action]()
    assert not player.advance_if_ready() and mci.starts == before


def test_same_file_twice_is_a_real_second_item_not_deduplicated(queued):
    player, mci = queued
    item = player.playlist[0]
    player.set_playlist((item, item))
    player.play_queue()
    mci.finish()
    assert player.advance_if_ready() and player.playlist_index == 1
    mci.finish()
    assert player.poll() is None and not player.queue_active
    assert len(mci.starts) == 2


def test_sleep_is_global_across_queue_and_wins_over_transition(queued, monkeypatch):
    player, mci = queued
    now = [100.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    player.play_queue()
    player.set_sleep_timer(5)
    mci.finish()
    now[0] = 102
    assert player.advance_if_ready() and player.sleep_remaining_seconds == 3
    mci.finish()
    now[0] = 105
    before = list(mci.starts)
    assert not player.advance_if_ready() and player.state == 'stopped'
    assert mci.starts == before and not player.queue_active


def test_ab_repeat_does_not_advance_to_another_recording(queued):
    player, mci = queued
    player.play_queue()
    player.set_ab_loop(.1, .5)
    mci.mode, mci.position = 'stopped', 500
    assert player.poll() == 'repeat-pending' and not player.queue_pending
    before = list(mci.starts)
    assert not player.advance_if_ready() and mci.starts == before


def test_changed_next_source_stops_without_playing_replacement(queued):
    player, mci = queued
    player.play_queue()
    mci.finish()
    path = player.playlist[1].path
    with path.open('ab') as stream:
        stream.write(b'new bytes')
    before = list(mci.starts)
    with pytest.raises(ValueError, match='changed'):
        player.advance_if_ready()
    assert mci.starts == before and not player.queue_active and player.state == 'stopped'


def test_explicit_foreign_play_disarms_existing_queue(queued, recordings):
    player, mci = queued
    player.play_queue()
    player.play(recordings[2])
    mci.finish()
    assert player.poll() is None and not player.advance_if_ready()


def test_explicit_play_queue_resumes_current_paused_file_without_restart(queued):
    player, mci = queued
    player.play_queue()
    mci.position = 400
    player.pause()
    assert not player.queue_active and player.state == 'paused'
    opened = sum(command.startswith('open ') for command in mci.calls)
    assert player.play_queue(0) and player.queue_active
    assert sum(command.startswith('open ') for command in mci.calls) == opened
    assert mci.starts[-1].startswith('resume ')
