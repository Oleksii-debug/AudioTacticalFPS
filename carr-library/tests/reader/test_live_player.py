"""Real PCM boundaries over a controlled canonical WavePlayer contract."""
from pathlib import Path
import wave

import pytest

from carr_library.reader.live_player import CanonicalPlayer


class Backend:
    available = True
    backend = 'Controlled canonical player boundary'
    position_is_approximate = False

    def __init__(self):
        self.state, self.position_seconds = 'stopped', 0.0
        self.played, self.closed = [], 0

    def play(self, path):
        self.played.append(Path(path))
        self.state, self.position_seconds = 'playing', 0.0

    def pause(self):
        self.state = 'paused'

    def resume(self):
        self.state = 'playing'

    def seek(self, seconds):
        self.position_seconds = seconds

    def stop(self):
        self.state, self.position_seconds = 'stopped', 0.0

    def close(self):
        self.closed += 1
        self.stop()


@pytest.fixture
def chunk(tmp_path):
    path = tmp_path / 'Українська 🚂.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 1600)
    return path


def test_open_validates_real_pcm_without_sound_and_actual_end_is_reported(chunk):
    backend = Backend()
    player = CanonicalPlayer(backend)
    opened = player.open(chunk)
    assert opened['duration_seconds'] == .2 and opened['state'] == 'stopped'
    assert not backend.played
    assert player.play()['state'] == 'playing' and backend.played == [chunk]
    backend.state, backend.position_seconds = 'stopped', .2
    assert player.status()['state'] == 'ended'
    player.close()
    assert player.status()['state'] == 'closed'


def test_checked_chunk_cannot_be_replaced_before_native_playback(chunk):
    backend = Backend()
    player = CanonicalPlayer(backend)
    player.open(chunk)
    with chunk.open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='changed'):
        player.play()
    assert not backend.played


def test_paused_seek_preserves_backend_state_and_early_stop_is_not_completion(chunk):
    backend = Backend()
    player = CanonicalPlayer(backend)
    player.open(chunk)
    player.play()
    player.pause()
    assert player.seek(.15)['state'] == 'paused'
    assert player.status()['position_seconds'] == .15 and len(backend.played) == 1
    player.resume()
    backend.state, backend.position_seconds = 'stopped', .05
    assert player.status()['state'] == 'stopped'
    backend.position_is_approximate = True
    assert player.status()['position_is_approximate'] is True
    with pytest.raises(ValueError, match='active speech chunk'):
        player.seek(.3)


def test_canonical_windows_paused_seek_never_sends_play_before_explicit_resume(chunk, monkeypatch):
    from types import SimpleNamespace
    import carr_library.speech.player as implementation
    player = implementation.WavePlayer()
    commands = []
    # The command contract is tested here, not an actual Windows audio driver.
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    player._mci = lambda command: commands.append(command) or ''
    player.path, player.duration_seconds = chunk, .2
    player._open, player._state, player._offset = True, 'paused', .05
    player.seek(.15)
    assert commands == [f'seek {player._alias} to 150 wait']
    assert player.state == 'paused' and player._resume_from_seek is True
    player.resume()
    assert commands == [f'seek {player._alias} to 150 wait', f'play {player._alias} from 150']
    assert player._state == 'playing' and player._resume_from_seek is False
    player.stop()
    assert player._resume_from_seek is False
    with pytest.raises(ValueError):
        player.seek(float('nan'))


@pytest.mark.parametrize('failure', ['stop', 'close', 'both', 'interrupt-close'])
def test_canonical_native_close_retains_unconfirmed_alias_and_original_stop_error(chunk, monkeypatch, failure):
    from types import SimpleNamespace
    import carr_library.speech.player as implementation
    player = implementation.WavePlayer()
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    player.path, player.duration_seconds = chunk, .2
    player._open, player._state = True, 'playing'
    before = chunk.read_bytes()
    commands = []
    stop_error = RuntimeError('Synthetic native stop failure')
    close_error = (KeyboardInterrupt('Synthetic interrupted native close') if failure == 'interrupt-close'
                   else RuntimeError('Synthetic native close failure'))
    def native(command):
        commands.append(command)
        if command.startswith('stop ') and failure in ('stop', 'both'):
            raise stop_error
        if command.startswith('close ') and failure in ('close', 'both', 'interrupt-close'):
            raise close_error
        return ''
    monkeypatch.setattr(player, '_mci', native)
    expected = stop_error if failure in ('stop', 'both') else close_error
    with pytest.raises(type(expected)) as caught:
        player.close()
    assert caught.value is expected
    assert commands == [f'stop {player._alias}', f'close {player._alias}']
    assert player._open is (failure != 'stop')
    if failure == 'both':
        assert caught.value.__cause__ is close_error
    assert player.path == chunk and chunk.read_bytes() == before
    # Retry the same retained native alias; no second player or guessed silence.
    commands.clear()
    monkeypatch.setattr(player, '_mci', lambda command: commands.append(command) or '')
    assert player.close() is True
    assert not player._open and player._state == 'stopped'
    assert commands == ([] if failure == 'stop' else [f'stop {player._alias}', f'close {player._alias}'])
    assert chunk.read_bytes() == before
