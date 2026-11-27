"""Canonical WavePlayer guards reused from PR149, without its alternate facade."""
import ctypes
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
    player._mci_open = lambda path: player._mci(f'open "{path}" type waveaudio alias {player._alias}')
    return player


def test_windows_paused_seek_has_no_play_until_explicit_resume(chunk, monkeypatch):
    player = windows_player(chunk, monkeypatch)
    commands = []
    player._mci = lambda command: commands.append(command) or ''
    player.seek(.15)
    assert commands == [f'stop {player._alias}', f'close {player._alias}']
    assert player.state == 'paused' and not player._open
    assert player.position_seconds == .15
    player.resume()
    assert commands[-1] == f'play {player._alias} from 150'
    assert sum(command.startswith(('play ', 'resume ')) for command in commands) == 1
    assert player._state == 'playing' and player._open
    player.stop()
    assert player.state == 'stopped' and not player.repeat_enabled


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
    assert player._started is None and not player.repeat_enabled


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


class OpenParameters(ctypes.Structure):
    """Independent packed Win32 MCI_OPEN_PARMSW wire contract."""
    _pack_ = 1
    _fields_ = [('callback', ctypes.c_size_t), ('device', ctypes.c_uint32),
                ('kind', ctypes.c_wchar_p), ('element', ctypes.c_wchar_p),
                ('alias', ctypes.c_wchar_p)]


class OpenCommand:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, device, message, flags, address):
        assert (device, message, flags) == (0, 0x0803, 0x2602)
        assert self.argtypes == (ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t)
        assert self.restype is ctypes.c_uint
        value = ctypes.cast(address, ctypes.POINTER(OpenParameters)).contents
        self.calls.append((value.callback, value.kind, value.element, value.alias))
        value.device = 27
        return self.code


def typed_windows_player(monkeypatch, *, code=0):
    send, commands = OpenCommand(code), []
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(implementation.ctypes, 'windll',
        SimpleNamespace(winmm=SimpleNamespace(mciSendCommandW=send)), raising=False)
    player = implementation.WavePlayer()
    player._mci = lambda command: commands.append(command) or ''
    return player, send, commands


@pytest.mark.parametrize('leaf', ['f' * 64 + '.audio', 'Український запис — spaces.wav'])
def test_typed_open_preserves_admitted_unicode_element_and_packed_pointer_width(
        tmp_path, monkeypatch, leaf):
    source = tmp_path / leaf
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 1600)
    original = source.read_bytes()
    pointer = ctypes.sizeof(ctypes.c_void_p)
    layout = implementation._MciOpenParameters
    assert ctypes.sizeof(layout) == pointer * 4 + 4
    assert [getattr(layout, name).offset for name in
            ('dwCallback', 'wDeviceID', 'lpstrDeviceType', 'lpstrElementName', 'lpstrAlias')] == [
                0, pointer, pointer + 4, pointer * 2 + 4, pointer * 3 + 4]
    player, send, commands = typed_windows_player(monkeypatch)
    try:
        player.play(source)
        assert send.calls == [(0, 'waveaudio', str(source.resolve()), player._alias)]
        assert player._open and player._state == 'playing' and player.backend == 'Windows MCI'
        assert commands == [f'set {player._alias} time format milliseconds',
                            f'play {player._alias} from 0']
        assert not any(command.startswith('open ') for command in commands)
    finally:
        player.close()
    assert commands[-2:] == [f'stop {player._alias}', f'close {player._alias}']
    assert not player._open and source.read_bytes() == original


@pytest.mark.parametrize('code', [304, 320])
def test_typed_open_native_denial_is_visible_without_fallback_or_false_alias_ownership(
        chunk, monkeypatch, code):
    original = chunk.read_bytes()
    player, send, commands = typed_windows_player(monkeypatch, code=code)
    with pytest.raises(RuntimeError, match=f'MCI {code}') as error:
        player.play(chunk)
    assert len(send.calls) == 1 and commands == []
    assert str(chunk) not in str(error.value)
    assert not player._open and player._state == 'stopped'
    assert player._wave is None and player._process is None
    player.close()
    assert commands == [] and chunk.read_bytes() == original


@pytest.mark.parametrize('rejected', ['set ', 'play '])
def test_successful_typed_open_keeps_alias_owned_through_later_control_failure(
        chunk, monkeypatch, rejected):
    player, send, commands = typed_windows_player(monkeypatch)
    def control(command):
        commands.append(command)
        if command.startswith(rejected):
            raise RuntimeError('Synthetic post-open device denial')
        return ''
    player._mci = control
    with pytest.raises(RuntimeError, match='post-open device denial'):
        player.play(chunk)
    assert len(send.calls) == 1
    assert commands[-2:] == [f'stop {player._alias}', f'close {player._alias}']
    assert not player._open and player._process is None and player._wave is None


def test_failed_close_after_typed_open_retains_exact_alias_for_acknowledged_retry(chunk, monkeypatch):
    player, send, commands = typed_windows_player(monkeypatch)
    def control(command):
        commands.append(command)
        if command.startswith('set '):
            raise RuntimeError('Synthetic format denial')
        if command.startswith('close '):
            raise RuntimeError('Synthetic close unacknowledged')
        return ''
    player._mci = control
    with pytest.raises(RuntimeError, match='close unacknowledged'):
        player.play(chunk)
    assert player._open and len(send.calls) == 1
    player._mci = lambda command: commands.append(command) or ''
    player.close()
    assert not player._open and len(send.calls) == 1
    assert commands.count(f'close {player._alias}') == 2
