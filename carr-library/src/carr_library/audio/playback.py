"""Windows PCM preview using typed WinMM messages, never command text.

No microphone, global device-volume change, shell, network path, browser or
file association is used. RF64/compressed preview needs a different verified
backend. Public time values are seconds; native values are milliseconds.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import math
import os
import stat
import wave

from .voices import _local_path


MCI_OPEN, MCI_CLOSE, MCI_PLAY = 0x0803, 0x0804, 0x0806
MCI_SEEK, MCI_STOP, MCI_PAUSE = 0x0807, 0x0808, 0x0809
MCI_SET, MCI_STATUS, MCI_RESUME = 0x080D, 0x0814, 0x0855
MCI_WAIT, MCI_FROM, MCI_TO = 0x0002, 0x0004, 0x0008
MCI_OPEN_ELEMENT, MCI_OPEN_TYPE = 0x0200, 0x2000
MCI_SET_TIME_FORMAT, MCI_STATUS_ITEM = 0x0400, 0x0100
MCI_STATUS_LENGTH, MCI_STATUS_POSITION, MCI_STATUS_MODE = 1, 2, 4
MCI_FORMAT_MILLISECONDS = 0
_MODES = {524: 'not_ready', 525: 'stopped', 526: 'playing', 529: 'paused', 530: 'open', 528: 'seeking'}


class _Open(C.Structure):
    _fields_ = [('callback', C.c_size_t), ('device', C.c_uint32),
                ('device_type', C.c_wchar_p), ('element', C.c_wchar_p),
                ('alias', C.c_wchar_p), ('buffer_seconds', C.c_uint32)]


class _Generic(C.Structure):
    _fields_ = [('callback', C.c_size_t)]


class _Set(C.Structure):
    _fields_ = [('callback', C.c_size_t), ('time_format', C.c_uint32), ('audio', C.c_uint32),
                ('input', C.c_uint32), ('output', C.c_uint32),
                ('format_tag', C.c_uint16), ('reserved2', C.c_uint16),
                ('channels', C.c_uint16), ('reserved3', C.c_uint16),
                ('sample_rate', C.c_uint32), ('bytes_per_second', C.c_uint32),
                ('block_align', C.c_uint16), ('reserved4', C.c_uint16),
                ('bits', C.c_uint16), ('reserved5', C.c_uint16)]


class _Position(C.Structure):
    _fields_ = [('callback', C.c_size_t), ('position', C.c_uint32)]


class _Play(C.Structure):
    _fields_ = [('callback', C.c_size_t), ('start', C.c_uint32), ('end', C.c_uint32)]


class _Status(C.Structure):
    _fields_ = [('callback', C.c_size_t), ('result', C.c_size_t),
                ('item', C.c_uint32), ('track', C.c_uint32)]


@dataclass(frozen=True)
class PlaybackStatus:
    state: str
    position_seconds: float
    duration_seconds: float


class _WinMM:
    def __init__(self):
        if os.name != 'nt':
            raise RuntimeError('The native WAV preview is available on Windows only.')
        # System32-only loading avoids DLL lookup through the current directory.
        self.dll = C.WinDLL('winmm.dll', winmode=0x800)
        self.dll.mciSendCommandW.argtypes = (C.c_uint32, C.c_uint32, C.c_size_t, C.c_void_p)
        self.dll.mciSendCommandW.restype = C.c_uint32
        self.dll.mciGetErrorStringW.argtypes = (C.c_uint32, C.c_wchar_p, C.c_uint32)
        self.dll.mciGetErrorStringW.restype = C.c_int
        self.dll.waveOutGetNumDevs.argtypes = ()
        self.dll.waveOutGetNumDevs.restype = C.c_uint32

    def device_count(self):
        return int(self.dll.waveOutGetNumDevs())

    def send(self, device, message, flags, parameters):
        result = self.dll.mciSendCommandW(device, message, flags, C.byref(parameters))
        if result:
            description = C.create_unicode_buffer(256)
            if self.dll.mciGetErrorStringW(result, description, len(description)):
                detail = description.value
            else:
                detail = f'Native audio error {result}.'
            raise OSError(result, detail)


class LocalPlayer:
    """WAV preview with a keyboard-friendly control interface.

    Operate on the desktop/UI thread. Preview never changes NVDA's device
    volume or autoplays when opening. ``status`` returns a PlaybackStatus.
    """
    def __init__(self, *, _api=None):
        self._api = _api if _api is not None else _WinMM()
        self._device = None
        self._path = None

    @property
    def output_device_count(self):
        return self._api.device_count()

    def open(self, path):
        path = _local_path(path)
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Choose an ordinary local audio file for preview.')
        try:
            with wave.open(str(path), 'rb') as audio:
                if audio.getcomptype() != 'NONE' or audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2):
                    raise ValueError('Preview requires mono or stereo 16-bit PCM WAV.')
                if audio.getnframes() <= 0 or audio.getnframes() / audio.getframerate() * 1000 > 0xFFFFFFFF:
                    raise ValueError('Choose a nonempty chapter file for preview.')
        except (wave.Error, EOFError) as exc:
            raise ValueError('Preview requires a RIFF PCM WAV; use a chapter preview for RF64 or compressed output.') from exc
        if self.output_device_count == 0:
            raise RuntimeError('No audio output device is available. Reconnect a device and try again.')
        self.close()
        parameters = _Open(device_type='waveaudio', element=str(path))
        self._api.send(0, MCI_OPEN, MCI_WAIT | MCI_OPEN_TYPE | MCI_OPEN_ELEMENT, parameters)
        if not parameters.device:
            raise RuntimeError('Windows did not return an opened audio device.')
        self._device = int(parameters.device)
        self._path = path
        try:
            self._api.send(self._device, MCI_SET, MCI_WAIT | MCI_SET_TIME_FORMAT,
                           _Set(time_format=MCI_FORMAT_MILLISECONDS))
            return self.status()
        except BaseException:
            self.close()
            raise

    def _require_open(self):
        if self._device is None:
            raise ValueError('Open a local audio preview first.')

    def _status_value(self, item):
        self._require_open()
        parameters = _Status(item=item)
        self._api.send(self._device, MCI_STATUS, MCI_WAIT | MCI_STATUS_ITEM, parameters)
        return int(parameters.result)

    def status(self):
        if self._device is None:
            return PlaybackStatus('closed', 0, 0)
        return PlaybackStatus(_MODES.get(self._status_value(MCI_STATUS_MODE), 'unknown'),
                              self._status_value(MCI_STATUS_POSITION) / 1000,
                              self._status_value(MCI_STATUS_LENGTH) / 1000)

    def play(self):
        self._require_open()
        status = self.status()
        if status.position_seconds >= status.duration_seconds:
            self.seek(0)
        self._api.send(self._device, MCI_PLAY, 0, _Play())
        return self.status()

    def pause(self):
        self._require_open()
        if self.status().state == 'playing':
            self._api.send(self._device, MCI_PAUSE, MCI_WAIT, _Generic())
        return self.status()

    def resume(self):
        self._require_open()
        if self.status().state == 'paused':
            self._api.send(self._device, MCI_RESUME, 0, _Generic())
        elif self.status().state != 'playing':
            self.play()
        return self.status()

    def stop(self):
        self._require_open()
        self._api.send(self._device, MCI_STOP, MCI_WAIT, _Generic())
        return self.status()

    def seek(self, seconds):
        self._require_open()
        duration = self.status().duration_seconds
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or not 0 <= seconds <= duration):
            raise ValueError('Seek time must be within this audio file, in seconds.')
        was_playing = self.status().state == 'playing'
        self._api.send(self._device, MCI_SEEK, MCI_WAIT | MCI_TO,
                       _Position(position=round(seconds * 1000)))
        if was_playing and seconds < duration:
            self.play()
        return self.status()

    def close(self):
        if self._device is not None:
            try:
                self._api.send(self._device, MCI_CLOSE, MCI_WAIT, _Generic())
            finally:
                self._device = self._path = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
