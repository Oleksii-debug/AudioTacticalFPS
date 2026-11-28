"""Actual PCM and current WavePlayer queue transitions; native device is disclosed control.

Adapted from the existing PR152 queue acceptance cases. Loop and sleep remain
owned by current PlaybackNavigationHost and are exercised in its queue suite.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import stat
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


def test_explicit_queue_play_keeps_silently_restored_original_cursor(queued):
    player, mci = queued
    player.select_queue(1)
    player.seek(.375, start_if_stopped=False)
    assert not mci.starts and player.position_seconds == .375
    assert player.play_queue(1) and player.queue_active
    assert mci.starts[-1].endswith(' from 375')
    assert player.playlist_index == 1


def test_previous_next_and_stale_transition_ticket_keep_explicit_intent(queued):
    player, mci = queued
    assert player.move_queue(1) and player.playlist_index == 0 and not mci.starts
    assert not player.move_queue(-1)
    assert player.move_queue(1) and player.playlist_index == 1 and not mci.starts
    player.play_queue()
    old = player.queue_token
    mci.finish()
    player.pause()  # An explicit pause withdraws even a just-completed source.
    assert not player.advance_if_ready(expected_token=old)
    player.select_queue(0)
    player.play_queue()
    mci.finish()
    assert player.queue_pending
    assert not player.advance_if_ready(expected_token=old)
    assert player.playlist_index == 0
    assert player.advance_if_ready(expected_token=player.queue_token)
    assert player.playlist_index == 1


def test_native_close_refusal_keeps_same_alias_and_never_starts_next(queued):
    player, mci = queued
    player.play_queue()
    mci.finish()
    real = player._mci
    def refuse(command):
        if command.startswith('close '):
            raise OSError('controlled native close refusal')
        return real(command)
    player._mci = refuse
    before = list(mci.starts)
    with pytest.raises(OSError, match='close refusal'):
        player.advance_if_ready()
    assert player._open and mci.starts == before and not player.queue_active
    player._mci = real
    assert player.close() is True and not player._open


def test_typed_live_rows_and_iterators_are_rejected_before_consumption(recordings, monkeypatch):
    from carr_library.speech.playlist import PlaylistItem
    item, = prepare_playlist(recordings[:1])
    consumed = []
    def unbounded():
        consumed.append(True)
        yield item
    with pytest.raises(ValueError):
        prepare_playlist(unbounded())
    player = module.WavePlayer()
    try:
        with pytest.raises(ValueError):
            player.set_playlist(unbounded())
        assert not consumed
        for values in ({'frames': True}, {'sha256': 'a' * 63},
                       {'identity': (0, 0, 0, 0, 0)}, {'sample_width': 8}):
            with pytest.raises(ValueError):
                replace(item, **values)
    finally:
        assert player.close() is True


def test_review_uses_no_follow_descriptor_and_refuses_real_preopen_replacement(recordings, monkeypatch):
    from carr_library.speech import listening
    actual = listening._opened
    selected, replacement = recordings[:2]
    old = selected.read_bytes()
    source_bytes = replacement.read_bytes()
    @contextmanager
    def substituted(path, identity):
        if path == selected:
            selected.unlink()
            selected.write_bytes(source_bytes)
        with actual(path, identity) as stream:
            yield stream
    monkeypatch.setattr(listening, '_opened', substituted)
    with pytest.raises((ValueError, OSError), match='changed|replaced|identity'):
        prepare_playlist((selected,))
    assert selected.read_bytes() == source_bytes and old != source_bytes


def test_opened_growth_is_bounded_to_captured_size_plus_one(recordings, monkeypatch):
    from carr_library.speech import listening
    actual = listening._opened
    selected = recordings[0]
    original_size = selected.stat().st_size
    counts = []
    @contextmanager
    def observe(path, identity):
        with actual(path, identity) as stream:
            class Reader:
                appended = False
                def fileno(self): return stream.fileno()
                def tell(self): return stream.tell()
                def seek(self, *args):
                    result = stream.seek(*args)
                    if args == (0,) and not self.appended:
                        self.appended = True
                        try:
                            with selected.open('ab') as writer:
                                writer.write(b'x' * (2 * 1024**2))
                        except PermissionError:
                            # The real Windows read-only sharing handle is an
                            # equally strong refusal of this mutation.
                            counts.append(('write-denied', 0))
                    return result
                def read(self, size):
                    data = stream.read(size)
                    counts.append((size, len(data)))
                    return data
            yield Reader()
    monkeypatch.setattr(listening, '_opened', observe)
    try:
        items = prepare_playlist((selected,))
    except (ValueError, OSError):
        items = None
    header_reads = 44
    received = sum(value for request, value in counts if request != 'write-denied')
    assert received <= original_size + header_reads + 1
    if ('write-denied', 0) in counts:
        assert selected.stat().st_size == original_size
        assert items and items[0].sha256 == sha256(selected.read_bytes()).hexdigest()
    else:
        assert selected.stat().st_size == original_size + 2 * 1024**2 and items is None


def test_review_deadline_and_same_descriptor_cancellation(recordings, monkeypatch):
    import carr_library.speech.playlist as playlist
    selected = recordings[0]
    timeline = iter((100.0, 221.0))
    monkeypatch.setattr(playlist.time, 'monotonic', lambda: next(timeline))
    with pytest.raises(TimeoutError, match='two minutes'):
        prepare_playlist((selected,))
    monkeypatch.undo()
    original = selected.read_bytes()
    calls = []
    def stop():
        calls.append(True)
        return len(calls) >= 5
    with pytest.raises(InterruptedError):
        prepare_playlist((selected,), cancelled=stop)
    assert selected.read_bytes() == original
