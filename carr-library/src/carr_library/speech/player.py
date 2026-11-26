"""Optional native Windows PCM player, with no network or command-string parser.

Uses documented WinMM MCI message structures, not mciSendString or a shell.
Only the installed waveaudio device and the default audio output are selected.
MCI is a legacy compatibility backend; richer MediaPlayer/mpv support is separate.
"""
from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import stat
import threading
import wave

from ..audio.ffmpeg import _local_path


class PlayerError(RuntimeError):
    """The local playback operation failed."""


class PlayerUnavailable(PlayerError):
    """The native backend or requested playback capability is unavailable."""


_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_PTR = ctypes.c_size_t


class _Packed(ctypes.Structure):
    # mmsystem.h includes mciapi.h under pshpack1.h, also on 64-bit Windows.
    _pack_ = 1


class _Open(_Packed):
    _fields_ = [("callback", _PTR), ("device", _DWORD), ("device_type", ctypes.c_wchar_p),
                ("element", ctypes.c_wchar_p), ("alias", ctypes.c_wchar_p), ("buffer_seconds", _DWORD)]


class _Generic(_Packed):
    _fields_ = [("callback", _PTR)]


class _Play(_Packed):
    _fields_ = [("callback", _PTR), ("start", _DWORD), ("end", _DWORD)]


class _Seek(_Packed):
    _fields_ = [("callback", _PTR), ("position", _DWORD)]


class _Status(_Packed):
    _fields_ = [("callback", _PTR), ("value", _PTR), ("item", _DWORD), ("track", _DWORD)]


class _Set(_Packed):
    # Full MCI_WAVE_SET_PARMS, even though only dwTimeFormat is set.
    _fields_ = [("callback", _PTR), ("time_format", _DWORD), ("audio", _DWORD),
                ("input", _DWORD), ("output", _DWORD), ("format_tag", _WORD), ("reserved2", _WORD),
                ("channels", _WORD), ("reserved3", _WORD), ("sample_rate", _DWORD),
                ("bytes_per_second", _DWORD), ("block_align", _WORD), ("reserved4", _WORD),
                ("bits_per_sample", _WORD), ("reserved5", _WORD)]


_OPEN, _CLOSE, _PLAY, _SEEK = 0x0803, 0x0804, 0x0806, 0x0807
_STOP, _PAUSE, _SET, _STATUS, _RESUME = 0x0808, 0x0809, 0x080D, 0x0814, 0x0855
_WAIT, _TO, _OPEN_ELEMENT, _OPEN_TYPE = 2, 8, 0x200, 0x2000
_SET_TIME_FORMAT, _STATUS_ITEM = 0x400, 0x100
_STATUS_LENGTH, _STATUS_POSITION, _STATUS_MODE, _STATUS_TIME_FORMAT = 1, 2, 4, 6
_MODE_NAMES = {524: "not_ready", 525: "stopped", 526: "playing", 527: "recording",
               528: "seeking", 529: "paused", 530: "open"}
_CAPABILITIES = ("local-pcm-wav", "play", "pause", "resume", "stop", "seek", "position")


class _WinMM:
    def __init__(self):
        if os.name != "nt":
            raise PlayerUnavailable("The native PCM player requires Windows. No playback backend is installed here.")
        try:
            # LOAD_LIBRARY_SEARCH_SYSTEM32 disallows a CWD/PATH DLL substitution.
            self.dll = ctypes.WinDLL("winmm.dll", winmode=0x00000800)
            self.dll.mciSendCommandW.argtypes = [_DWORD, _DWORD, _PTR, ctypes.c_void_p]
            self.dll.mciSendCommandW.restype = _DWORD
            self.dll.mciGetErrorStringW.argtypes = [_DWORD, ctypes.c_wchar_p, _DWORD]
            self.dll.mciGetErrorStringW.restype = ctypes.c_int
            self.dll.waveOutGetNumDevs.argtypes = []
            self.dll.waveOutGetNumDevs.restype = _DWORD
            if self.dll.waveOutGetNumDevs() == 0:
                raise PlayerUnavailable("Windows reports no available audio output device.")
        except OSError as exc:
            raise PlayerUnavailable("The installed Windows audio API could not be loaded.") from exc

    def _send(self, device, command, flags=0, parameters=None):
        pointer = ctypes.byref(parameters) if parameters is not None else None
        error = self.dll.mciSendCommandW(device, command, flags, pointer)
        if error:
            message = ctypes.create_unicode_buffer(512)
            self.dll.mciGetErrorStringW(error, message, len(message))
            detail = message.value.strip() or "Native playback command failed."
            raise PlayerError(f"Windows audio error {error}: {detail}")

    def open(self, path):
        params = _Open(device_type="waveaudio", element=str(path))
        self._send(0, _OPEN, _OPEN_TYPE | _OPEN_ELEMENT | _WAIT, params)
        device = params.device
        try:
            self._send(device, _SET, _SET_TIME_FORMAT | _WAIT, _Set(time_format=0))
            if self.query(device, _STATUS_TIME_FORMAT) != 0:
                raise PlayerError("Native audio player did not accept millisecond positioning.")
        except BaseException:
            try:
                self.close(device)
            except PlayerError:
                pass
            raise
        return device

    def query(self, device, item):
        params = _Status(item=item)
        self._send(device, _STATUS, _STATUS_ITEM | _WAIT, params)
        return int(params.value)

    def play(self, device):
        self._send(device, _PLAY, 0, _Play())  # asynchronous, never block the UI until audio ends

    def pause(self, device):
        self._send(device, _PAUSE, _WAIT, _Generic())

    def resume(self, device):
        self._send(device, _RESUME, 0, _Generic())

    def stop(self, device):
        self._send(device, _STOP, _WAIT, _Generic())

    def seek(self, device, milliseconds):
        self._send(device, _SEEK, _TO | _WAIT, _Seek(position=milliseconds))

    def close(self, device):
        self._send(device, _CLOSE, _WAIT, _Generic())


def _new_backend():
    return _WinMM()


def available() -> bool:
    """Report API/output-device availability; this does not prove audible playback."""
    try:
        _new_backend()
        return True
    except PlayerUnavailable:
        return False


def _input_file(value) -> tuple[Path, float]:
    raw = os.fspath(value)
    if (not isinstance(raw, str) or raw.startswith(("//", "\\\\"))
            or "://" in raw or any(ord(ch) < 32 for ch in raw)):
        raise ValueError("Playback requires a local PCM WAV file, not a URL or network path.")
    path = _local_path(raw).absolute()
    if path.suffix.lower() != ".wav" or ":" in path.name:
        raise ValueError("This native player supports local PCM WAV files only.")
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("Playback paths must not contain symlinks or reparse points.")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Playback requires a regular local audio file.")
    try:
        with wave.open(str(path), "rb") as audio:
            rate, frames, channels, width = (audio.getframerate(), audio.getnframes(),
                                             audio.getnchannels(), audio.getsampwidth())
            if (audio.getcomptype() != "NONE" or width != 2 or channels not in (1, 2)
                    or not 8_000 <= rate <= 192_000 or frames <= 0):
                raise ValueError("This player requires nonempty mono/stereo PCM16 WAV audio.")
            duration = frames / rate
            if duration * 1000 > 0xFFFFFFFF:
                raise ValueError("Audio duration exceeds this native player's position range.")
            audio.setpos(frames - 1)
            if len(audio.readframes(1)) != channels * width:
                raise ValueError("The selected WAV audio is truncated.")
    except (wave.Error, EOFError) as exc:
        raise ValueError("The selected file is not supported PCM WAV audio; RF64 needs another playback backend.") from exc
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise PlayerError("The selected audio file changed during validation.")
    return path, duration


class LocalPlayer:
    """One native playback device. Methods return the current accessible status.

    No automatic playback occurs in open(). close() is idempotent. Playback rate
    is separate from synthesis and explicitly unavailable in this PCM backend.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._device = None
        self._path = None
        self._duration = 0.0
        self._paused_seek = False
        self._reason = None
        try:
            self._backend = _new_backend()
        except PlayerUnavailable as exc:
            self._backend = None
            self._reason = str(exc)

    def available(self) -> bool:
        return self._backend is not None

    def _require(self):
        if self._backend is None:
            raise PlayerUnavailable(self._reason)
        if self._device is None:
            raise PlayerError("Open a local audio file before using the player.")

    def open(self, path):
        with self._lock:
            if self._backend is None:
                raise PlayerUnavailable(self._reason)
            path, duration = _input_file(path)
            candidate = self._backend.open(path)
            try:
                native_duration = self._backend.query(candidate, _STATUS_LENGTH) / 1000.0
                if abs(native_duration - duration) > .01:
                    raise PlayerError("Native player duration disagrees with the validated PCM file.")
                if self._device is not None:
                    self._backend.close(self._device)
            except BaseException:
                try:
                    self._backend.close(candidate)
                except PlayerError:
                    pass
                raise
            self._device, self._path, self._duration = candidate, path, duration
            self._paused_seek = False
            return self.status()

    def status(self) -> dict:
        with self._lock:
            state, position = "closed", 0.0
            if self._backend is None:
                state = "unavailable"
            elif self._device is not None:
                mode = self._backend.query(self._device, _STATUS_MODE)
                state = _MODE_NAMES.get(mode, "unknown")
                position = self._backend.query(self._device, _STATUS_POSITION) / 1000.0
                position = min(self._duration, max(0., position))
                if self._paused_seek and state == "stopped":
                    state = "paused"
                elif state == "stopped" and position >= self._duration - .001:
                    state = "ended"
            return {"available": self.available(), "backend": "winmm-mci", "state": state,
                    "path": str(self._path) if self._path is not None else None,
                    "position_seconds": position, "duration_seconds": self._duration,
                    "rate": 1.0, "capabilities": _CAPABILITIES if self.available() else (),
                    "reason": self._reason}

    def play(self):
        with self._lock:
            self._require()
            status = self.status()
            if status["state"] == "playing":
                return status
            if status["state"] == "ended":
                self._backend.seek(self._device, 0)
            if status["state"] == "paused" and not self._paused_seek:
                self._backend.resume(self._device)
            else:
                self._backend.play(self._device)
            self._paused_seek = False
            return self.status()

    def pause(self):
        with self._lock:
            self._require()
            if self.status()["state"] == "playing":
                self._backend.pause(self._device)
            return self.status()

    def resume(self):
        return self.play()

    def stop(self):
        """Stop and return to the beginning; pause() retains position."""
        with self._lock:
            self._require()
            self._backend.stop(self._device)
            self._backend.seek(self._device, 0)
            self._paused_seek = False
            return self.status()

    def seek(self, position_seconds):
        with self._lock:
            self._require()
            if (type(position_seconds) not in (int, float) or not math.isfinite(position_seconds)
                    or not 0 <= position_seconds <= self._duration):
                raise ValueError("Seek position must be a finite time within the current audio.")
            previous = self.status()["state"]
            self._backend.seek(self._device, round(position_seconds * 1000))
            self._paused_seek = previous == "paused"
            if previous == "playing" and position_seconds < self._duration:
                self._backend.play(self._device)
            return self.status()

    def set_rate(self, rate):
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate != 1.0:
            raise PlayerUnavailable("Independent playback-speed control is not supported by this PCM player.")
        return self.status()

    def close(self):
        with self._lock:
            if self._device is not None:
                self._backend.close(self._device)
            self._device, self._path, self._duration = None, None, 0.0
            self._paused_seek = False
            return self.status()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
