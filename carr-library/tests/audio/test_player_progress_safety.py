"""Native device calls are explicit doubles; resolver uses actual local files."""
from types import SimpleNamespace

import pytest

import carr_library.audio.player as module
from carr_library.audio.player import WavePlayer


def test_player_reuses_absolute_path_only_resolver(tmp_path, monkeypatch):
    candidate = tmp_path / 'ffplay'
    candidate.write_text('never execute this fixture', encoding='utf-8')
    candidate.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', '.')
    assert WavePlayer()._executable is None


def test_failed_stop_and_close_never_claim_silence(monkeypatch):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    player._open = True
    player._silence_confirmed = False
    def fail(command):
        raise RuntimeError('Synthetic native device failure')
    monkeypatch.setattr(player, '_mci', fail)
    player.close()
    assert player.state == 'stopped'  # Existing UI compatibility.
    assert not player.silence_confirmed  # Actual cleanup was not confirmed.
    player.close()
    assert not player.silence_confirmed


def test_successful_close_confirms_silence_even_if_stop_failed(monkeypatch):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    player._open = True
    player._silence_confirmed = False
    def native(command):
        if command.startswith('stop'):
            raise RuntimeError('Synthetic stop failure')
        return ''
    monkeypatch.setattr(player, '_mci', native)
    player.close()
    assert player.silence_confirmed
