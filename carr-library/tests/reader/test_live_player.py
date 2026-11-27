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


@pytest.mark.parametrize('stop_fails', [False, True])
def test_failed_native_close_retains_alias_and_pcm_until_a_confirmed_retry(chunk, monkeypatch, stop_fails):
    from types import SimpleNamespace
    import carr_library.speech.player as implementation
    player = implementation.WavePlayer()
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    commands, failed_close = [], [True]
    def mci(command):
        commands.append(command)
        if command.startswith('stop ') and stop_fails:
            raise RuntimeError('Synthetic native Stop failure')
        if command.startswith('close ') and failed_close[0]:
            raise RuntimeError('Synthetic native Close failure')
        return ''
    player._mci = mci
    player.path, player.duration_seconds = chunk, .2
    player._open, player._state = True, 'playing'
    facade = CanonicalPlayer(player)
    facade.path, facade.duration, facade._started = chunk, .2, True
    with pytest.raises(RuntimeError, match='Close failure'):
        facade.close()
    assert player._open and player.path == chunk
    assert facade.path == chunk and chunk.is_file()
    assert commands.count(f'close {player._alias}') == 1
    failed_close[0] = False
    assert facade.close()['state'] == 'closed'
    assert not player._open and player._state == 'stopped'
    assert facade.path is None
    assert commands.count(f'close {player._alias}') == 2
