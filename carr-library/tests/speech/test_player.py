"""Native playback protocol/lifecycle checks; no claim about physical audio output."""
import wave

import pytest

from carr_library.speech.player import WavePlayer


def audio_file(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 8000, 8000, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x02' * 8000)
    return path


def native_protocol_player(monkeypatch):
    player = WavePlayer()
    player._windows = True
    calls = []
    def command(value):
        calls.append(value)
        if value.endswith(' mode'):
            return 'playing'
        if value.endswith(' position'):
            return '250'
        return ''
    monkeypatch.setattr(player, '_mci', command)
    return player, calls


def test_pause_resume_seek_preserves_paused_state_and_closes_device(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    player.play(audio_file(tmp_path / 'speech.wav'), start_seconds=0.1)
    assert player.state == 'playing' and player.position_seconds == 0.25
    assert player.pause() and player.state == 'paused'
    player.seek(0.6)
    assert player.state == 'paused'
    assert any(command.startswith('play ') and command.endswith(' from 600') for command in calls)
    assert player.resume() and player.state == 'playing'
    player.close()
    assert player.state == 'stopped' and not player._open
    assert calls[-1].startswith('close ')


def test_invalid_position_never_opens_device(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    source = audio_file(tmp_path / 'speech.wav')
    for position in (-1, 2, float('nan'), True):
        with pytest.raises(ValueError, match='position'):
            player.play(source, start_seconds=position)
    assert calls == []


def test_play_failure_releases_opened_device_before_file_cleanup(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    command = player._mci
    def fail_play(value):
        if value.startswith('play '):
            raise RuntimeError('Synthetic audio-device failure')
        return command(value)
    monkeypatch.setattr(player, '_mci', fail_play)
    source = audio_file(tmp_path / 'speech.wav')
    with pytest.raises(RuntimeError, match='device failure'):
        player.play(source)
    assert not player._open and calls[-1].startswith('close ')
    source.unlink()
