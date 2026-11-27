"""Synthetic native boundary tests are not device or listening acceptance."""
import ctypes
from dataclasses import dataclass
import hashlib
from pathlib import Path
import struct
import sys
import threading
import time
import wave

import pytest

from carr_library.audio.playback import (OutputDevice, PCMPlayer, PlaybackError,
    PlaybackUnavailable, _MMTIME, _PositionCounter, _WAVEFORMATEX, _WAVEHDR,
    _WAVEOUTCAPSW, native_playback_probe, playback_capabilities)


@dataclass
class Token:
    data: bytes
    done: bool = False


class SyntheticDevice:
    """A controlled in-process driver boundary, never a real audio output."""
    def __init__(self, info, device_id):
        self.info, self.device = info, OutputDevice(0, "Synthetic device boundary", 2, True)
        self.active, self.read_bytes, self.maximum_queued, self.position_value = [], 0, 0, 0
        self.paused, self.closed, self.resets = True, False, 0
        self.threads = {threading.get_ident()}

    def submit(self, data):
        self.threads.add(threading.get_ident())
        token = Token(data)
        self.active.append(token)
        self.read_bytes += len(data)
        self.maximum_queued = max(self.maximum_queued, sum(len(t.data) for t in self.active))
        return token

    def completed(self, token):
        return token.done

    def release(self, token):
        assert token.done
        self.active.remove(token)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def advance(self):
        if not self.paused:
            for token in list(self.active):
                if not token.done:
                    self.position_value += len(token.data) // (self.info.channels * 2)
                    token.done = True

    def position(self):
        return self.position_value

    def reset(self):
        self.active.clear()
        self.position_value = 0
        self.resets += 1

    def close(self):
        self.reset()
        self.closed = True


def source(path, *, rf64=False):
    pcm = b"\x01\x00\x02\x00" * 8000
    if rf64:
        fmt = struct.pack("<HHIIHH", 1, 2, 8000, 32000, 4, 16)
        path.write_bytes(b"RF64\xff\xff\xff\xffWAVEds64" + struct.pack("<IQQQI", 28, len(pcm) + 72, len(pcm), 8000, 0)
                         + b"fmt " + struct.pack("<I", 16) + fmt + b"data\xff\xff\xff\xff" + pcm)
    else:
        with wave.open(str(path), "wb") as stream:
            stream.setparams((2, 2, 8000, 8000, "NONE", "PCM"))
            stream.writeframes(pcm)
    return path


def wait(player, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = player.status()
        if predicate(current):
            return current
        if current.state in ("error", "unavailable"):
            pytest.fail(current.error)
        time.sleep(.005)
    pytest.fail("Timed out: " + str(player.status()))


def player_fixture(**kwargs):
    devices = []
    def factory(info, device_id):
        devices.append(SyntheticDevice(info, device_id))
        return devices[-1]
    return PCMPlayer(_backend_factory=factory, **kwargs), devices


def test_windows_abi_uses_fixed_width_fields_on_every_test_host():
    assert ctypes.sizeof(_WAVEFORMATEX) == 18
    assert ctypes.sizeof(_WAVEHDR) == (48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32)
    assert ctypes.sizeof(_MMTIME) == 12
    assert ctypes.sizeof(_WAVEOUTCAPSW) == 84


@pytest.mark.parametrize("rf64", [False, True])
def test_streaming_pause_seek_resume_stop_is_bounded_and_preserves_source(tmp_path, rf64):
    path = source(tmp_path / "Голос.wav", rf64=rf64)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    player, devices = player_fixture(buffer_ms=20, buffer_count=3)
    try:
        player.play(path)
        playing = wait(player, lambda status: status.state == "playing")
        device = devices[0]
        assert playing.duration_seconds == 1
        assert playing.buffered_bytes <= 3 * 160 * 4
        assert device.read_bytes < path.stat().st_size
        device.advance()
        wait(player, lambda status: status.position_frames >= 480)
        player.pause()
        wait(player, lambda status: status.state == "paused")
        position = player.status().position_frames
        device.advance()
        time.sleep(.03)
        assert player.status().position_frames == position
        player.seek(.75)
        wait(player, lambda status: status.state == "paused" and status.position_frames == 6000)
        assert device.resets == 1
        assert device.paused
        player.resume()
        wait(player, lambda status: status.state == "playing")
        player.stop()
        assert player.status().state == "stopped"
        assert player.status().position_frames == 0
        assert device.closed and not device.active
        assert device.maximum_queued <= 3 * 160 * 4
        assert len(device.threads) == 1
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    finally:
        player.close()


def test_natural_completion_releases_buffers_and_reports_exact_position(tmp_path):
    player, devices = player_fixture(buffer_ms=100)
    try:
        player.play(source(tmp_path / "complete.wav"), start_seconds=.8)
        wait(player, lambda status: status.state == "playing")
        devices[0].advance()
        result = wait(player, lambda status: status.state == "completed")
        assert result.position_frames == result.total_frames == 8000
        deadline = time.monotonic() + 1
        while not devices[0].closed and time.monotonic() < deadline:
            time.sleep(.005)
        assert devices[0].closed
        assert devices[0].read_bytes == 1600 * 4
    finally:
        player.close()


def test_mutated_source_stops_and_cleans_up_without_rewriting_it(tmp_path):
    path = source(tmp_path / "changed.wav")
    player, devices = player_fixture()
    try:
        player.play(path)
        wait(player, lambda status: status.state == "playing")
        with path.open("r+b") as stream:
            stream.truncate(64)
        result = wait(player, lambda status: status.state == "error")
        assert "changed" in result.error
        assert devices[0].closed
        assert path.stat().st_size == 64
    finally:
        player.close()


def test_device_removal_is_reported_and_cleanup_is_attempted(tmp_path):
    player, devices = player_fixture()
    try:
        player.play(source(tmp_path / "device.wav"))
        wait(player, lambda status: status.state == "playing")
        def removed():
            raise PlaybackError("Selected output device was removed")
        devices[0].position = removed
        result = wait(player, lambda status: status.state == "error")
        assert "removed" in result.error
        assert devices[0].closed
    finally:
        player.close()


@pytest.mark.parametrize("worker_error", [False, True])
def test_worker_cleanup_failure_blocks_stop_close_and_replay(tmp_path, worker_error):
    path = source(tmp_path / "retained-device.wav")
    player, devices = player_fixture()
    player.play(path)
    wait(player, lambda status: status.state == "playing")

    def failed_cleanup():
        raise PlaybackError("Synthetic driver retained its buffers")

    devices[0].close = failed_cleanup
    if worker_error:
        def disconnected():
            raise PlaybackError("Synthetic device disconnected")
        devices[0].position = disconnected
        wait(player, lambda status: status.state == "error")
    with pytest.raises(PlaybackError, match="Restart.*retained its buffers"):
        player.stop()
    assert player.status().state == "error"
    assert "retained its buffers" in player.status().error
    with pytest.raises(PlaybackError, match="retained its buffers"):
        player.close()
    with pytest.raises(PlaybackError, match="retained its buffers"):
        player.play(path)
    assert len(devices) == 1


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 1.1])
def test_seek_bounds_are_checked_before_enqueuing(tmp_path, value):
    player, _ = player_fixture()
    try:
        player.play(source(tmp_path / "bounds.wav"))
        wait(player, lambda status: status.state == "playing")
        with pytest.raises(ValueError):
            player.seek(value)
    finally:
        player.close()


def test_native_dword_counter_wrap_and_alternative_time_formats():
    samples = _PositionCounter(48000, 4)
    assert samples.update(2, 2**32 - 100) == 2**32 - 100
    assert samples.update(2, 30) == 2**32 + 30
    byte_count = _PositionCounter(48000, 4)
    assert byte_count.update(4, 192000) == 48000
    milliseconds = _PositionCounter(48000, 4)
    assert milliseconds.update(1, 1250) == 60000
    assert milliseconds.update(8, 1) is None


class SyntheticWinMM:
    """Exercise actual ctypes headers without claiming an OS device call."""
    def __init__(self):
        self.headers, self.calls, self.reset_error, self.write_error = [], [], 0, 0

    def waveOutGetNumDevs(self):
        return 1

    def waveOutOpen(self, handle, device, fmt, callback, instance, flags):
        from carr_library.audio.playback import HANDLE
        ctypes.cast(handle, ctypes.POINTER(HANDLE))[0] = HANDLE(123)
        self.calls.append("open")
        assert callback == instance == flags == 0
        return 0

    def waveOutGetID(self, handle, identity):
        ctypes.cast(identity, ctypes.POINTER(ctypes.c_uint32))[0] = 0
        return 0

    def waveOutGetDevCapsW(self, identity, pointer, size):
        caps = ctypes.cast(pointer, ctypes.POINTER(_WAVEOUTCAPSW)).contents
        for index, char in enumerate("Synthetic WinMM boundary"):
            caps.name[index] = ord(char)
        caps.channels, caps.support = 2, 0x20
        return 0

    def waveOutGetErrorTextW(self, code, target, maximum):
        target.value = "Synthetic driver error"
        return 0

    def waveOutPrepareHeader(self, handle, pointer, size):
        header = ctypes.cast(pointer, ctypes.POINTER(_WAVEHDR)).contents
        assert header.flags == 0
        header.flags |= 2
        self.headers.append(header)
        self.calls.append("prepare")
        return 0

    def waveOutWrite(self, handle, pointer, size):
        header = ctypes.cast(pointer, ctypes.POINTER(_WAVEHDR)).contents
        if self.write_error:
            return self.write_error
        assert header.flags & 2
        header.flags |= 16
        self.calls.append("write")
        return 0

    def waveOutUnprepareHeader(self, handle, pointer, size):
        header = ctypes.cast(pointer, ctypes.POINTER(_WAVEHDR)).contents
        if header.flags & 16:
            return 33
        header.flags &= ~2
        # The caller may free the header as soon as this function returns.
        # ctypes pointer.contents is a view, not an owner of that allocation;
        # retaining it would make this fake inspect freed memory at close.
        address = ctypes.addressof(header)
        self.headers[:] = [item for item in self.headers if ctypes.addressof(item) != address]
        self.calls.append("unprepare")
        return 0

    def waveOutPause(self, handle):
        self.calls.append("pause")
        return 0

    def waveOutRestart(self, handle):
        self.calls.append("resume")
        return 0

    def waveOutReset(self, handle):
        self.calls.append("reset")
        if self.reset_error:
            return self.reset_error
        for header in self.headers:
            header.flags = (header.flags | 1) & ~16
        return 0

    def waveOutClose(self, handle):
        assert not self.headers
        self.calls.append("close")
        return 0


def test_ctypes_buffers_remain_owned_until_reset_unprepare_close(tmp_path, monkeypatch):
    import carr_library.audio.playback as playback
    api = SyntheticWinMM()
    monkeypatch.setattr(playback, "_api", lambda: api)
    backend = playback._WinMMBackend(playback.wav_info(source(tmp_path / "abi.wav")))
    token = backend.submit(b"\x01\x00\x02\x00" * 100)
    assert ctypes.string_at(token.header.data, token.header.length) == b"\x01\x00\x02\x00" * 100
    assert not backend.completed(token)
    assert len(backend.buffers) == 1
    backend.close()
    assert not backend.buffers and not backend.handle.value
    assert api.calls[-3:] == ["reset", "unprepare", "close"]


def test_failed_native_cleanup_retains_driver_owned_memory_and_blocks_new_opens(tmp_path, monkeypatch):
    import carr_library.audio.playback as playback
    api, retained = SyntheticWinMM(), []
    monkeypatch.setattr(playback, "_api", lambda: api)
    monkeypatch.setattr(playback, "_UNRELEASED_DEVICES", retained)
    info = playback.wav_info(source(tmp_path / "failed-driver.wav"))
    backend = playback._WinMMBackend(info)
    backend.submit(b"\0" * 400)
    api.reset_error = 6
    with pytest.raises(PlaybackError, match="Stop queued audio"):
        backend.close()
    assert retained == [backend] and backend.buffers
    with pytest.raises(PlaybackUnavailable, match="did not release"):
        playback._WinMMBackend(info)
    # Complete the synthetic lifecycle; no real driver owns this test memory.
    api.reset_error = 0
    backend.close()


def test_failed_open_cleanup_is_not_lost_before_backend_assignment(tmp_path, monkeypatch):
    import carr_library.audio.playback as playback
    api, retained = SyntheticWinMM(), []
    monkeypatch.setattr(playback, "_api", lambda: api)
    monkeypatch.setattr(playback, "_UNRELEASED_DEVICES", retained)
    monkeypatch.setattr(api, "waveOutGetDevCapsW", lambda *_: 6)
    api.reset_error = 6
    player = PCMPlayer(_backend_factory=playback._WinMMBackend)
    try:
        player.play(source(tmp_path / "failed-open-cleanup.wav"))
        wait(player, lambda status: status.state == "error")
        with pytest.raises(PlaybackError, match="Restart.*Stop queued audio"):
            player.close()
        assert len(retained) == 1
        assert "close" not in api.calls
    finally:
        # Release the synthetic handle; no actual OS device is involved.
        api.reset_error = 0
        for backend in retained:
            backend.close()


def test_native_write_failure_still_unprepares_the_allocated_header(tmp_path, monkeypatch):
    import carr_library.audio.playback as playback
    api = SyntheticWinMM()
    monkeypatch.setattr(playback, "_api", lambda: api)
    backend = playback._WinMMBackend(playback.wav_info(source(tmp_path / "queue-failure.wav")))
    api.write_error = 7
    try:
        with pytest.raises(PlaybackError, match="Queue audio buffer"):
            backend.submit(b"\0" * 400)
        assert len(backend.buffers) == 1
    finally:
        backend.close()
    assert not backend.buffers
    assert api.calls[-3:] == ["reset", "unprepare", "close"]


def test_closed_player_cannot_restart_or_leak_worker(tmp_path):
    player, _ = player_fixture()
    player.close()
    with pytest.raises(PlaybackError, match="closed"):
        player.play(source(tmp_path / "closed.wav"))


@pytest.mark.parametrize("rf64", [False, True])
def test_native_endpoint_never_queues_pcm_beyond_b(tmp_path, rf64):
    player, devices = player_fixture(buffer_ms=100)
    try:
        player.play(source(tmp_path / "range.wav", rf64=rf64), start_seconds=.2, end_seconds=.35)
        wait(player, lambda status: status.state == "playing")
        devices[0].advance()
        result = wait(player, lambda status: status.state == "completed")
        assert result.position_frames == 2800 and result.total_frames == 8000
        assert result.duration_seconds == 1 and result.position_seconds == .35
        assert devices[0].read_bytes == (2800 - 1600) * 4
    finally:
        player.close()


@pytest.mark.parametrize("end", [True, -.1, float("nan"), 1.1, .1])
def test_invalid_endpoint_preserves_existing_worker(tmp_path, end):
    player, devices = player_fixture()
    path = source(tmp_path / "existing.wav")
    try:
        player.play(path)
        wait(player, lambda status: status.state == "playing")
        with pytest.raises(ValueError):
            player.play(path, start_seconds=.2, end_seconds=end)
        assert len(devices) == 1 and not devices[0].closed
        assert player.status().state == "playing"
    finally:
        player.close()


def test_direct_seek_respects_active_endpoint(tmp_path):
    player, devices = player_fixture()
    try:
        player.play(source(tmp_path / "seek-range.wav"), end_seconds=.7)
        wait(player, lambda status: status.state == "playing")
        with pytest.raises(ValueError):
            player.seek(.8)
        assert devices[0].resets == 0
        player.seek(.5)
        wait(player, lambda status: status.position_frames == 4000)
        assert devices[0].resets == 1
    finally:
        player.close()
