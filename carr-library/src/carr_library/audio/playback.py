"""Bounded local PCM16/RF64 playback through Windows' installed WinMM API.

One worker owns the native handle and a fixed-size buffer queue. No shell,
decoder download, full-file read, Python driver callback or source write is
used. See docs/AUDIO_PLAYBACK.md for the reviewed native contracts and limits.
"""
from __future__ import annotations

from collections import deque
import ctypes as C
from dataclasses import asdict, dataclass, replace
import math
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import wave

from .ffmpeg import WaveInfo, wav_info
from .jobs import regular_file
from .model_manifest import local_path


class PlaybackError(RuntimeError):
    """The device or immutable source could not complete playback."""


class PlaybackUnavailable(PlaybackError):
    """Native playback or the selected audio device is unavailable."""


class _PlaybackCleanupError(PlaybackError):
    """Opening failed after acquiring a device that could not be released."""


@dataclass(frozen=True)
class OutputDevice:
    id: int
    name: str
    channels: int
    sample_accurate_position: bool

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PlaybackStatus:
    state: str = "stopped"
    path: str | None = None
    position_frames: int = 0
    total_frames: int = 0
    sample_rate: int = 0
    channels: int = 0
    device_id: int | None = None
    device_name: str = ""
    buffered_bytes: int = 0
    position_accuracy: str = "not_started"
    error: str | None = None

    @property
    def position_seconds(self):
        return self.position_frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def duration_seconds(self):
        return self.total_frames / self.sample_rate if self.sample_rate else 0.0

    def to_dict(self):
        return {**asdict(self), "position_seconds": self.position_seconds,
                "duration_seconds": self.duration_seconds}


# Fixed-width Windows types are deliberate: c_ulong and c_wchar differ on
# Linux, where the ABI layout and state-machine regressions also run.
U16, U32, UPTR, HANDLE = C.c_uint16, C.c_uint32, C.c_size_t, C.c_void_p


class _WAVEFORMATEX(C.Structure):
    _pack_ = 1
    _fields_ = [("format", U16), ("channels", U16), ("rate", U32),
                ("byte_rate", U32), ("align", U16), ("bits", U16), ("extra", U16)]


class _WAVEHDR(C.Structure):
    _fields_ = [("data", HANDLE), ("length", U32), ("recorded", U32),
                ("user", UPTR), ("flags", U32), ("loops", U32),
                ("next", HANDLE), ("reserved", UPTR)]


class _MMTIME_VALUE(C.Union):
    _fields_ = [("value", U32), ("smpte", C.c_uint8 * 8)]


class _MMTIME(C.Structure):
    _fields_ = [("kind", U32), ("time", _MMTIME_VALUE)]


class _WAVEOUTCAPSW(C.Structure):
    _fields_ = [("manufacturer", U16), ("product", U16), ("version", U32),
                ("name", U16 * 32), ("formats", U32), ("channels", U16),
                ("reserved", U16), ("support", U32)]


_UNRELEASED_DEVICES = []


def _api():
    if sys.platform != "win32":
        raise PlaybackUnavailable("Native PCM playback requires Windows; no player is launched on this platform.")
    # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents an application/current-directory DLL
    # from impersonating the operating system's multimedia API.
    dll = C.WinDLL("winmm.dll", winmode=0x00000800, use_last_error=True)
    signatures = {
        "waveOutGetNumDevs": ([], U32),
        "waveOutGetDevCapsW": ([UPTR, C.POINTER(_WAVEOUTCAPSW), U32], U32),
        "waveOutOpen": ([C.POINTER(HANDLE), U32, C.POINTER(_WAVEFORMATEX), UPTR, UPTR, U32], U32),
        "waveOutGetID": ([HANDLE, C.POINTER(U32)], U32),
        "waveOutPrepareHeader": ([HANDLE, C.POINTER(_WAVEHDR), U32], U32),
        "waveOutWrite": ([HANDLE, C.POINTER(_WAVEHDR), U32], U32),
        "waveOutUnprepareHeader": ([HANDLE, C.POINTER(_WAVEHDR), U32], U32),
        "waveOutPause": ([HANDLE], U32), "waveOutRestart": ([HANDLE], U32),
        "waveOutReset": ([HANDLE], U32), "waveOutClose": ([HANDLE], U32),
        "waveOutGetPosition": ([HANDLE, C.POINTER(_MMTIME), U32], U32),
        "waveOutGetErrorTextW": ([U32, C.c_wchar_p, U32], U32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
    return dll


def _check(dll, code, operation, *, opening=False):
    if not code:
        return
    description = C.create_unicode_buffer(256)
    dll.waveOutGetErrorTextW(code, description, len(description))
    message = description.value
    error = PlaybackUnavailable if opening else PlaybackError
    raise error(f"{operation} failed ({code}): {message or 'Windows audio device error.'}")


def _device(dll, identity):
    caps = _WAVEOUTCAPSW()
    _check(dll, dll.waveOutGetDevCapsW(identity, C.byref(caps), C.sizeof(caps)), "Audio device inspection")
    name = bytes(caps.name).decode("utf-16-le", "replace").split("\0", 1)[0]
    return OutputDevice(identity, name, caps.channels, bool(caps.support & 0x20))


def output_devices() -> tuple[OutputDevice, ...]:
    """Inspect installed Windows outputs without opening or playing audio."""
    dll = _api()
    count = int(dll.waveOutGetNumDevs())
    if count > 4096:
        raise PlaybackError("Windows reported an invalid audio device count.")
    return tuple(_device(dll, index) for index in range(count))


def playback_capabilities() -> dict:
    try:
        devices = output_devices()
    except (PlaybackError, OSError) as exc:
        return {"available": False, "backend": "Windows WinMM waveOut", "reason": str(exc), "devices": []}
    return {"available": bool(devices), "backend": "Windows WinMM waveOut",
            "reason": None if devices else "Windows reports no audio output device.",
            "devices": [device.to_dict() for device in devices],
            "formats": "PCM16 mono/stereo RIFF or RF64; device format support is checked when opened.",
            "controls": ["play", "pause", "resume", "stop", "seek_seconds", "position"],
            "manual_device_listening_and_nvda": "NOT_RUN"}


class _PositionCounter:
    """Unwrap the native DWORD position for multi-hour playback."""
    def __init__(self, rate, align):
        self.rate, self.align = rate, align
        self.kind, self.last, self.wraps, self.frames = None, 0, 0, 0

    def update(self, kind, raw):
        if kind not in (1, 2, 4):
            return None
        if kind != self.kind:
            self.kind, self.last, self.wraps = kind, raw, 0
        if raw < self.last and self.last - raw > 2**31:
            self.wraps += 2**32
        self.last = raw
        total = self.wraps + raw
        value = total if kind == 2 else total // self.align if kind == 4 else total * self.rate // 1000
        self.frames = max(self.frames, value)
        return self.frames


@dataclass
class _NativeBuffer:
    data: object
    header: _WAVEHDR


class _WinMMBackend:
    """Only the player worker may call this object or inspect its headers."""
    def __init__(self, info: WaveInfo, device_id=None):
        if _UNRELEASED_DEVICES:
            raise PlaybackUnavailable("A driver did not release an earlier audio buffer. Restart the application before playing again.")
        self.dll, self.handle, self.buffers = _api(), HANDLE(), []
        self.info = info
        self.counter = _PositionCounter(info.rate, info.channels * 2)
        count = int(self.dll.waveOutGetNumDevs())
        if not count:
            raise PlaybackUnavailable("Windows reports no audio output device.")
        if device_id is not None and device_id >= count:
            raise PlaybackUnavailable("The selected output device is no longer installed.")
        fmt = _WAVEFORMATEX(1, info.channels, info.rate, info.rate * info.channels * 2, info.channels * 2, 16, 0)
        _check(self.dll, self.dll.waveOutOpen(C.byref(self.handle), 0xffffffff if device_id is None else device_id,
               C.byref(fmt), 0, 0, 0), "Open audio output", opening=True)
        try:
            actual = U32()
            _check(self.dll, self.dll.waveOutGetID(self.handle, C.byref(actual)), "Identify audio output")
            self.device = _device(self.dll, actual.value)
            self.pause()
        except BaseException:
            try:
                self.close()
            except Exception as exc:
                raise _PlaybackCleanupError(str(exc)) from exc
            raise

    def submit(self, data):
        memory = C.create_string_buffer(data, len(data))
        header = _WAVEHDR(data=C.addressof(memory), length=len(data))
        token = _NativeBuffer(memory, header)
        _check(self.dll, self.dll.waveOutPrepareHeader(self.handle, C.byref(header), C.sizeof(header)), "Prepare audio buffer")
        self.buffers.append(token)  # Keep both objects alive before the driver sees them.
        _check(self.dll, self.dll.waveOutWrite(self.handle, C.byref(header), C.sizeof(header)), "Queue audio buffer")
        return token

    def completed(self, token):
        return bool(token.header.flags & 1)  # WHDR_DONE, written by the driver.

    def release(self, token):
        _check(self.dll, self.dll.waveOutUnprepareHeader(self.handle, C.byref(token.header), C.sizeof(token.header)), "Release audio buffer")
        self.buffers.remove(token)

    def pause(self):
        _check(self.dll, self.dll.waveOutPause(self.handle), "Pause audio")

    def resume(self):
        _check(self.dll, self.dll.waveOutRestart(self.handle), "Resume audio")

    def reset(self):
        _check(self.dll, self.dll.waveOutReset(self.handle), "Stop queued audio")
        for token in tuple(self.buffers):
            self.release(token)
        self.counter = _PositionCounter(self.info.rate, self.info.channels * 2)

    def position(self):
        position = _MMTIME(kind=2)  # Request TIME_SAMPLES, check returned format.
        _check(self.dll, self.dll.waveOutGetPosition(self.handle, C.byref(position), C.sizeof(position)), "Read audio position")
        return self.counter.update(position.kind, position.time.value)

    def close(self):
        if not self.handle.value:
            return
        try:
            self.reset()
            _check(self.dll, self.dll.waveOutClose(self.handle), "Close audio output")
            self.handle = HANDLE()
        except BaseException:
            # Never free memory still referenced by a failed native driver.
            # One retained device disables new native opens until app restart.
            if self not in _UNRELEASED_DEVICES:
                _UNRELEASED_DEVICES.append(self)
            raise


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class PCMPlayer:
    """Threaded local player; callbacks may run on the worker thread.

    pause/resume/seek enqueue bounded commands. Poll status() or marshal
    on_status via wx.CallAfter. stop() joins the worker with a bounded timeout.
    The private backend hook is for state-machine tests, not native acceptance.
    """
    def __init__(self, *, buffer_ms=80, buffer_count=4, on_status=None, _backend_factory=None):
        if type(buffer_ms) is not int or not 10 <= buffer_ms <= 250:
            raise ValueError("Playback buffers must be between 10 and 250 milliseconds.")
        if type(buffer_count) is not int or not 2 <= buffer_count <= 8:
            raise ValueError("Playback requires between two and eight buffers.")
        self.buffer_ms, self.buffer_count, self.on_status = buffer_ms, buffer_count, on_status
        self._factory = _backend_factory or _WinMMBackend
        self._native = _backend_factory is None
        self._lock, self._commands = threading.Lock(), queue.Queue(maxsize=32)
        self._wake, self._cancel = threading.Event(), threading.Event()
        self._thread, self._snapshot, self._closed = None, PlaybackStatus(), False
        self._cleanup_error = None
        self._range_end = 0

    def status(self) -> PlaybackStatus:
        with self._lock:
            return self._snapshot

    def _update(self, **values):
        with self._lock:
            self._snapshot = replace(self._snapshot, **values)
            snapshot = self._snapshot
        if self.on_status is not None:
            try:
                self.on_status(snapshot)
            except Exception:
                pass  # An already-closed GUI observer must not leak a native buffer.

    def play(self, path, *, start_seconds=0, end_seconds=None, device_id=None):
        if self._closed:
            raise PlaybackError("The audio player is closed.")
        if self._thread is threading.current_thread():
            raise PlaybackError("Schedule a new play request outside the playback status callback.")
        if device_id is not None and (type(device_id) is not int or not 0 <= device_id <= 4095):
            raise ValueError("Choose an installed numeric output device ID or the default output.")
        if self._native and sys.platform != "win32":
            message = "Native PCM playback requires Windows."
            self._update(state="unavailable", error=message)
            raise PlaybackUnavailable(message)
        selected = local_path(path)
        identity = _identity(regular_file(selected))
        info = wav_info(selected)
        start = self._frame(start_seconds, info.rate, info.frames)
        end = info.frames if end_seconds is None else self._frame(end_seconds, info.rate, info.frames)
        if end < start:
            raise ValueError("The playback end must not precede the starting position.")
        self.stop()
        self._range_end = end
        self._commands = queue.Queue(maxsize=32)
        self._cancel.clear()
        self._wake.clear()
        self._update(state="starting", path=str(selected), position_frames=start,
            total_frames=info.frames, sample_rate=info.rate, channels=info.channels,
            device_id=device_id, device_name="", buffered_bytes=0, error=None, position_accuracy="not_started")
        self._thread = threading.Thread(target=self._run, args=(selected, identity, info, start, end, device_id),
                                        name="CARR native PCM playback", daemon=True)
        self._thread.start()

    @staticmethod
    def _frame(seconds, rate, total):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= total / rate:
            raise ValueError("Choose a playback position within this audio file, in seconds.")
        return min(total, round(seconds * rate))

    def _command(self, operation, value=None):
        if self._closed:
            raise PlaybackError("The audio player is closed.")
        if self._thread is None or not self._thread.is_alive():
            raise PlaybackError("Start playback before using this control.")
        try:
            self._commands.put_nowait((operation, value))
        except queue.Full as exc:
            raise PlaybackError("Playback controls are busy; wait for the current request.") from exc
        self._wake.set()

    def pause(self):
        self._command("pause")

    def resume(self):
        self._command("resume")

    def seek(self, seconds):
        status = self.status()
        if not status.sample_rate:
            raise PlaybackError("Load an audio file before seeking.")
        self._command("seek", self._frame(seconds, status.sample_rate, self._range_end))

    def stop(self, *, timeout=3):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError("Use a positive playback cleanup timeout of at most 30 seconds.")
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._cancel.set()
            self._wake.set()
            if thread is threading.current_thread():
                return
            thread.join(timeout)
            if thread.is_alive():
                raise PlaybackError("The audio device has not released playback yet; another player was not started.")
        with self._lock:
            cleanup_error = self._cleanup_error
        if cleanup_error is not None:
            raise PlaybackError("The audio device did not release playback. Restart the application before continuing: " + cleanup_error)
        if self.status().state not in ("error", "unavailable", "closed"):
            self._update(state="stopped", position_frames=0, buffered_bytes=0)

    def close(self):
        self.stop()
        self._closed = True
        if self.status().state not in ("error", "unavailable"):
            self._update(state="closed")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _run(self, path, expected_identity, info, start, end, device_id):
        backend, pending = None, deque()
        state, failure, cleanup_error = "stopped", None, None
        base, cursor, completed = start, start, 0
        paused, native_paused = False, True
        align = info.channels * 2
        block_frames = max(1, info.rate * self.buffer_ms // 1000)
        try:
            with path.open("rb") as source:
                if _identity(os.fstat(source.fileno())) != expected_identity:
                    raise PlaybackError("The selected audio file changed before playback started.")
                source.seek(info.data_offset + start * align)
                backend = self._factory(info, device_id)
                self._update(device_id=backend.device.id, device_name=backend.device.name)
                while not self._cancel.is_set():
                    while True:
                        try:
                            operation, value = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        if operation == "pause":
                            backend.pause()
                            paused = native_paused = True
                        elif operation == "resume":
                            paused = False
                        elif operation == "seek":
                            backend.reset()
                            backend.pause()
                            native_paused = True
                            pending.clear()
                            base = cursor = value
                            completed = 0
                            source.seek(info.data_offset + cursor * align)
                    if _identity(os.fstat(source.fileno())) != expected_identity or _identity(regular_file(path)) != expected_identity:
                        raise PlaybackError("The audio source changed during playback; playback was stopped.")
                    while pending and backend.completed(pending[0][0]):
                        token, count = pending.popleft()
                        backend.release(token)
                        completed += count
                    while len(pending) < self.buffer_count and cursor < end and not self._cancel.is_set():
                        count = min(block_frames, end - cursor)
                        data = source.read(count * align)
                        if len(data) != count * align:
                            raise PlaybackError("The PCM audio source was truncated during playback.")
                        pending.append((backend.submit(data), count))
                        cursor += count
                    if not pending and cursor == end:
                        state = "completed"
                        self._update(state=state, position_frames=end, buffered_bytes=0)
                        break
                    if not paused and native_paused:
                        backend.resume()
                        native_paused = False
                    position = backend.position()
                    precise = position is not None
                    position = max(completed, position if precise else completed)
                    self._update(state="paused" if paused else "playing",
                        position_frames=min(cursor, base + position),
                        buffered_bytes=sum(count * align for _, count in pending),
                        position_accuracy="driver_reported" if precise else "completed_buffers")
                    self._wake.wait(.02)
                    self._wake.clear()
        except Exception as exc:
            state = "unavailable" if isinstance(exc, PlaybackUnavailable) else "error"
            failure = str(exc)
            if isinstance(exc, _PlaybackCleanupError):
                cleanup_error = failure
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception as exc:
                    state, failure = "error", str(exc)
                    cleanup_error = failure
            with self._lock:
                self._cleanup_error = cleanup_error
            self._update(state=state, buffered_bytes=0, error=failure,
                         **({"position_frames": 0} if state == "stopped" else {}))


PlaybackController = PCMPlayer


def native_playback_probe(path=None) -> dict:
    """Exercise the real Windows API; no-device is explicit, not fake success.

    By default use one second of synthetic silence. Providing a path is an
    explicit request to exercise a small audible portion of that local file.
    """
    capability = playback_capabilities()
    if not capability["available"]:
        return {"status": "UNAVAILABLE", "reason": capability["reason"], "manual_listening": "NOT_RUN"}
    with tempfile.TemporaryDirectory(prefix="carr-playback-probe-") as temporary:
        if path is None:
            path = Path(temporary) / "silence.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 44100, 44100, "NONE", "PCM"))
                output.writeframes(b"\0" * (44100 * 2))
        player = PCMPlayer()
        def wait_for(predicate):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = player.status()
                if status.state == "unavailable":
                    raise PlaybackUnavailable(status.error)
                if status.state == "error":
                    raise PlaybackError(status.error)
                if predicate(status):
                    return status
                time.sleep(.01)
            raise PlaybackError("The native playback probe timed out.")
        try:
            player.play(path)
            active = wait_for(lambda status: status.state == "playing")
            player.pause()
            wait_for(lambda status: status.state == "paused")
            target = min(active.duration_seconds / 2, .2)
            player.seek(target)
            wait_for(lambda status: status.state == "paused" and abs(status.position_seconds - target) < .01)
            player.resume()
            wait_for(lambda status: status.state in ("playing", "completed"))
            player.stop()
            if player.status().state == "error":
                raise PlaybackError(player.status().error)
            result = {"status": "PASS", "backend": "Windows WinMM waveOut", "device": active.device_name,
                    "controls": "play/pause/seek/resume/stop", "manual_listening": "NOT_RUN",
                    "physical_keyboard_and_nvda": "NOT_RUN"}
        except PlaybackUnavailable as exc:
            result = {"status": "UNAVAILABLE", "reason": str(exc), "manual_listening": "NOT_RUN"}
        except (PlaybackError, OSError, ValueError) as exc:
            result = {"status": "ERROR", "reason": str(exc), "manual_listening": "NOT_RUN"}
        finally:
            try:
                player.close()
            except PlaybackError as exc:
                result = {"status": "ERROR", "reason": str(exc), "manual_listening": "NOT_RUN"}
        return result
