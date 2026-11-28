"""Typed native-message boundary; devices use native_playback_probe.py."""
from array import array
import ctypes
import wave

import pytest

from carr_library.audio.playback import (
    LocalPlayer, MCI_OPEN, MCI_CLOSE, MCI_PLAY, MCI_PAUSE, MCI_RESUME, MCI_STOP,
    MCI_SEEK, MCI_SET, MCI_STATUS, MCI_STATUS_LENGTH, MCI_STATUS_POSITION,
    MCI_STATUS_MODE, MCI_FORMAT_MILLISECONDS, _Status,
    _Open, _Generic, _Set, _Position, _Play,
)


class BoundaryDevice:
    """Deterministic WinMM boundary test; never audible-playback evidence."""
    def __init__(self, count=1):
        self.count, self.mode, self.position, self.calls = count, 525, 0, []

    def device_count(self):
        return self.count

    def send(self, device, message, flags, parameters):
        self.calls.append((device, message, flags, parameters))
        if message == MCI_OPEN:
            parameters.device = 41
        elif message == MCI_SET:
            assert parameters.time_format == MCI_FORMAT_MILLISECONDS
        elif message == MCI_STATUS:
            parameters.result = {MCI_STATUS_LENGTH: 2000, MCI_STATUS_POSITION: self.position,
                                 MCI_STATUS_MODE: self.mode}[parameters.item]
        elif message in (MCI_PLAY, MCI_RESUME):
            self.mode = 526
        elif message == MCI_PAUSE:
            self.mode = 529
        elif message == MCI_STOP:
            self.mode = 525
        elif message == MCI_SEEK:
            self.position, self.mode = parameters.position, 525


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "Audio 'quotes' — source.wav"
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        writer.writeframes(array('h', [0] * 16000).tobytes())
    return path


def test_open_does_not_autoplay_or_interpret_filename(wav):
    api = BoundaryDevice()
    player = LocalPlayer(_api=api)
    assert player.open(wav).state == 'stopped'
    call = next(call for call in api.calls if call[1] == MCI_OPEN)
    assert call[3].element == str(wav) and call[3].device_type == 'waveaudio'
    assert not any(call[1] == MCI_PLAY for call in api.calls)
    player.close()
    assert player.status().state == 'closed'


def test_play_pause_seek_resume_and_seconds_to_milliseconds(wav):
    api = BoundaryDevice()
    with LocalPlayer(_api=api) as player:
        player.open(wav)
        assert player.play().state == 'playing'
        assert player.pause().state == 'paused'
        assert player.resume().state == 'playing'
        state = player.seek(1.25)
        assert state.position_seconds == 1.25 and state.state == 'playing'
        assert api.position == 1250
        assert player.stop().state == 'stopped'
        player.seek(2)
        player.play()
        assert api.position == 0
    assert api.calls[-1][1] == MCI_CLOSE


@pytest.mark.parametrize('position', [-1, float('nan'), float('inf'), True, 2.001, '1'])
def test_invalid_seek_never_reaches_native_command(wav, position):
    api = BoundaryDevice()
    with LocalPlayer(_api=api) as player:
        player.open(wav)
        with pytest.raises(ValueError):
            player.seek(position)
    assert not any(call[1] == MCI_SEEK for call in api.calls)


def test_no_audio_device_is_reported_instead_of_false_playback(wav):
    api = BoundaryDevice(count=0)
    with LocalPlayer(_api=api) as player:
        with pytest.raises(RuntimeError, match='No audio output'):
            player.open(wav)
    assert api.calls == []


def test_win32_return_field_is_pointer_sized():
    assert _Status.result.size == ctypes.sizeof(ctypes.c_void_p)
    assert _Status.item.size == 4


@pytest.mark.parametrize('structure', [_Open, _Generic, _Set, _Position, _Play, _Status])
def test_winmm_parameter_blocks_use_sdk_byte_packing(structure):
    # MMSYSTEM.H includes MCIAPI.H inside PSHPACK1.H/POPPACK.H.  Native CI
    # additionally compares every field with the installed Windows SDK.
    assert ctypes.alignment(structure) == 1
    pointer = ctypes.sizeof(ctypes.c_void_p)
    if structure is _Open:
        assert _Open.device.offset == pointer
        assert _Open.device_type.offset == pointer + 4
        assert _Open.element.offset == 2 * pointer + 4
        assert _Open.alias.offset == 3 * pointer + 4
        assert _Open.buffer_seconds.offset == 4 * pointer + 4
        assert ctypes.sizeof(_Open) == 4 * pointer + 8
    elif structure is _Position:
        assert _Position.position.offset == pointer
        assert ctypes.sizeof(_Position) == pointer + 4
