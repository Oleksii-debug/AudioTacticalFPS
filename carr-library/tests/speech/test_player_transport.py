"""Canonical WavePlayer guards reused from PR149, without its alternate facade."""
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import player as implementation


@pytest.fixture
def chunk(tmp_path):
    path = tmp_path / 'Українська 🚂.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 1600)
    return path


def windows_player(chunk, monkeypatch):
    player = implementation.WavePlayer()
    # This is the MCI command contract, not an actual Windows driver.
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    player.path, player.duration_seconds = chunk, .2
    player._open, player._state, player._offset = True, 'paused', .05
    return player


def test_windows_paused_seek_has_no_play_until_explicit_resume(chunk, monkeypatch):
    player = windows_player(chunk, monkeypatch)
    commands = []
    player._mci = lambda command: commands.append(command) or ''
    player.seek(.15)
    assert commands == [f'seek {player._alias} to 150 wait']
    assert player.state == 'paused' and player._resume_from_seek is True
    player.resume()
    assert commands == [f'seek {player._alias} to 150 wait', f'play {player._alias} from 150']
    assert player._state == 'playing' and player._resume_from_seek is False
    player.stop()
    assert player._resume_from_seek is False


@pytest.mark.parametrize('seconds', [True, -.01, .21, float('nan'), float('inf'), 10 ** 400])
def test_invalid_paused_seek_does_not_touch_device_or_position(chunk, monkeypatch, seconds):
    player = windows_player(chunk, monkeypatch)
    commands = []
    player._mci = lambda command: commands.append(command) or ''
    with pytest.raises(ValueError):
        player.seek(seconds)
    assert not commands and player._state == 'paused' and player._offset == .05


@pytest.mark.parametrize('stop_fails', [False, True])
def test_failed_native_close_retains_alias_and_pcm_until_confirmed_retry(chunk, monkeypatch, stop_fails):
    player = windows_player(chunk, monkeypatch)
    commands, failed_close = [], [True]
    def mci(command):
        commands.append(command)
        if command.startswith('stop ') and stop_fails:
            raise RuntimeError('Synthetic native Stop failure')
        if command.startswith('close ') and failed_close[0]:
            raise RuntimeError('Synthetic native Close failure')
        return ''
    player._mci = mci
    player._state = 'playing'
    with pytest.raises(RuntimeError, match='Close failure'):
        player.close()
    assert player._open and player.path == chunk and chunk.is_file()
    assert commands.count(f'close {player._alias}') == 1
    failed_close[0] = False
    player.close()
    assert not player._open and player._state == 'stopped'
    assert commands.count(f'close {player._alias}') == 2


def test_successful_native_close_establishes_silence_after_failed_stop(chunk, monkeypatch):
    player = windows_player(chunk, monkeypatch)
    def mci(command):
        if command.startswith('stop '):
            raise RuntimeError('Synthetic Stop error')
        return ''
    player._mci = mci
    player.close()
    assert not player._open and player._state == 'stopped'
    assert player._started is None and not player._resume_from_seek


def test_ffplay_keeps_canonical_sanitized_environment_and_owned_launch(chunk, monkeypatch):
    player = implementation.WavePlayer()
    player._executable = '/explicit-fixture/ffplay'
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setenv('FFREPORT', 'file=unapproved-report.log')
    launched = []
    child = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(implementation.subprocess, 'Popen',
        lambda args, **kwargs: launched.append((args, kwargs)) or child)
    closed = []
    monkeypatch.setattr(implementation, 'stop_process', lambda process: closed.append(process))
    player.play(chunk)
    args, options = launched[0]
    assert args[0] == '/explicit-fixture/ffplay' and args[-1] == str(chunk)
    assert options['shell'] is False and options['start_new_session'] is True
    assert 'FFREPORT' not in options['env']
    assert options['stdin'] == implementation.subprocess.DEVNULL
    player.close()
    assert closed == [child] and player._process is None
