"""Real RIFF/RF64 preflight; native device calls are explicit unit-test doubles."""
from types import SimpleNamespace
import math
import struct
import wave

import pytest

import carr_library.audio.player as player_module
from carr_library.audio.player import WavePlayer


def recording(path, *, rf64=False):
    data = b'\x00\x00' * 8000
    if rf64:
        path.write_bytes(b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE' +
            b'ds64' + struct.pack('<IQQQI', 28, len(data) + 72, len(data), 8000, 0) +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16) +
            b'data' + struct.pack('<I', 0xffffffff) + data)
    else:
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(data)
    return path


@pytest.mark.parametrize('rf64', [False, True])
def test_pcm_preflight_pause_resume_seek_stop_and_close(tmp_path, monkeypatch, rf64):
    path = recording(tmp_path / 'recording.wav', rf64=rf64)
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    state = {'mode': 'stopped', 'position': 0}
    commands = []
    def mci(self, command):
        commands.append(command)
        if command.startswith('status'):
            return state['mode'] if command.endswith('mode') else str(state['position'])
        if command.startswith('play'):
            state.update(mode='playing', position=int(command.rsplit(' ', 1)[1]))
        elif command.startswith('pause'):
            state['mode'] = 'paused'
        elif command.startswith('resume'):
            state['mode'] = 'playing'
        elif command.startswith('stop'):
            state.update(mode='stopped', position=0)
        return ''
    monkeypatch.setattr(WavePlayer, '_mci', mci)
    player = WavePlayer()
    player.play(path)
    assert player.duration_seconds == 1 and player.state == 'playing'
    assert player.pause() and player.state == 'paused'
    player.seek(.5)
    assert player.state == 'paused' and player.position_seconds == .5
    assert player.resume() and player.state == 'playing'
    player.stop()
    assert player.state == 'stopped' and player.position_seconds == 0
    player.close()
    assert not player._open
    assert any(command.startswith('close ') for command in commands)


@pytest.mark.parametrize('position', [-1, math.nan, math.inf, 2, True])
def test_invalid_seek_rejected_before_device_call(tmp_path, monkeypatch, position):
    path = recording(tmp_path / 'recording.wav')
    monkeypatch.setattr(WavePlayer, '_mci', lambda *args: pytest.fail('No native call before validation.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path, start_seconds=position)


def test_truncated_wav_fails_before_playback(tmp_path, monkeypatch):
    path = recording(tmp_path / 'recording.wav')
    path.write_bytes(path.read_bytes()[:-1])
    monkeypatch.setattr(WavePlayer, '_mci', lambda *args: pytest.fail('No device call for damaged PCM.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path)


def test_removed_windows_device_cannot_block_studio_cleanup(monkeypatch):
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    player._open = True
    def disconnected(*args):
        raise RuntimeError('Device disconnected')
    monkeypatch.setattr(player, '_mci', disconnected)
    player.close()
    assert not player._open and player.state == 'stopped'
