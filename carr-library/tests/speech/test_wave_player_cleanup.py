"""Native transport decisions with controlled MCI; no device/listening claims."""
from types import SimpleNamespace

import pytest

from carr_library.speech import player as module


@pytest.fixture
def player(monkeypatch):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    value = module.WavePlayer()
    value._open, value._state = True, 'playing'
    value.path, value.duration_seconds = 'synthetic.wav', 5
    return value


def test_failed_close_preserves_alias_for_explicit_retry(player, monkeypatch):
    commands = []
    def unavailable(command):
        commands.append(command)
        raise RuntimeError('Device refused stop/close')
    monkeypatch.setattr(player, '_mci', unavailable)
    with pytest.raises(RuntimeError):
        player.close()
    assert player._open and player._state == 'playing'
    assert commands == [f'stop {player._alias}', f'close {player._alias}']
    commands.clear()
    monkeypatch.setattr(player, '_mci', commands.append)
    player.close()
    assert not player._open and player.state == 'stopped'
    assert commands == [f'stop {player._alias}', f'close {player._alias}']
    player.close()
    assert len(commands) == 2


def test_confirmed_close_establishes_silence_after_failed_stop(player, monkeypatch):
    def close_only(command):
        if command.startswith('stop '):
            raise RuntimeError('Stop was refused')
        assert command == f'close {player._alias}'
    monkeypatch.setattr(player, '_mci', close_only)
    player.close()
    assert not player._open and player.state == 'stopped'


def test_paused_seek_stays_silent_until_explicit_resume(player, monkeypatch):
    commands = []
    player._state = 'paused'
    monkeypatch.setattr(player, '_mci', commands.append)
    player.seek(2.5)
    assert commands == [f'seek {player._alias} to 2500 wait']
    assert player.state == 'paused'
    assert player.resume()
    assert commands[-1] == f'play {player._alias} from 2500'
    assert player._state == 'playing'
