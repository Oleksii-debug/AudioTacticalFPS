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


def test_native_media_lifetime_outlives_reporter_binding_and_includes_pause(tmp_path):
    from carr_library.progress import ProgressHub, current_progress
    hub = ProgressHub()
    reporter = hub.start("Open player")
    player, devices = player_fixture(progress_hub=hub)
    try:
        with reporter.bind():
            player.play(source(tmp_path / "media-lifetime.wav"))
            wait(player, lambda status: status.state == "playing")
        assert current_progress() is None and hub.media_active
        player.pause()
        wait(player, lambda status: status.state == "paused")
        assert hub.media_active
        player.resume()
        wait(player, lambda status: status.state == "playing")
        closed_while_active = []
        close = devices[0].close
        def closing():
            closed_while_active.append(hub.media_active)
            close()
        devices[0].close = closing
        player.stop()
        assert closed_while_active == [True]
        assert devices[0].closed and not hub.media_active
        player.close()
        assert not hub.media_active
    finally:
        player.close()


@pytest.mark.parametrize("ending", ["complete", "device-error"])
def test_native_media_context_releases_on_worker_exit_without_a_ui_poll(tmp_path, ending):
    from carr_library.progress import ProgressHub
    hub = ProgressHub()
    player, devices = player_fixture(progress_hub=hub)
    try:
        player.play(source(tmp_path / "media-ending.wav"), start_seconds=.9)
        wait(player, lambda status: status.state == "playing")
        assert hub.media_active
        if ending == "complete":
            devices[0].advance()
        else:
            def failed():
                raise PlaybackError("Synthetic media device failed")
            devices[0].position = failed
        # The worker must release media ownership itself. There is no status()
        # or UI timer call between this event and the lifecycle assertion.
        player._thread.join(3)
        assert not player._thread.is_alive()
        assert not hub.media_active and devices[0].closed
        assert player.status().state == ("completed" if ending == "complete" else "error")
    finally:
        player.close()


def test_native_media_open_failure_releases_context_before_returning_error(tmp_path):
    from carr_library.progress import ProgressHub
    hub, observed = ProgressHub(), []
    def unavailable(info, device_id):
        observed.append(hub.media_active)
        raise PlaybackUnavailable("Synthetic output unavailable")
    player = PCMPlayer(_backend_factory=unavailable, progress_hub=hub)
    try:
        player.play(source(tmp_path / "media-open-error.wav"))
        player._thread.join(3)
        assert not player._thread.is_alive()
        assert observed == [True] and not hub.media_active
        assert player.status().state == "unavailable"
    finally:
        player.close()


def test_native_media_cleanup_error_releases_only_after_cleanup_attempt(tmp_path):
    from carr_library.progress import ProgressHub
    hub = ProgressHub()
    player, devices = player_fixture(progress_hub=hub)
    player.play(source(tmp_path / "media-cleanup-error.wav"))
    wait(player, lambda status: status.state == "playing")
    observed, close = [], devices[0].close
    def failed_close():
        observed.append(hub.media_active)
        close()  # No actual driver owns this synthetic fixture.
        raise PlaybackError("Synthetic close error")
    devices[0].close = failed_close
    with pytest.raises(PlaybackError, match="Synthetic close error"):
        player.close()
    assert observed == [True] and devices[0].closed
    assert not hub.media_active and not player._thread.is_alive()


def test_concurrent_native_players_release_only_their_own_media_context(tmp_path):
    from carr_library.progress import ProgressHub
    hub = ProgressHub()
    first, _ = player_fixture(progress_hub=hub)
    second, _ = player_fixture(progress_hub=hub)
    path = source(tmp_path / "two-media-owners.wav")
    try:
        first.play(path)
        second.play(path)
        wait(first, lambda status: status.state == "playing")
        wait(second, lambda status: status.state == "playing")
        assert hub.media_active
        first.close()
        assert hub.media_active and second.status().state == "playing"
        second.close()
        assert not hub.media_active
    finally:
        first.close()
        second.close()


def test_failed_native_thread_start_never_acquires_a_media_context(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.progress import ProgressHub
    from carr_library.audio import playback
    hub = ProgressHub()
    player, devices = player_fixture(progress_hub=hub)
    class Unstarted:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError("Synthetic thread allocation failure")
        def is_alive(self):
            return False
    monkeypatch.setattr(playback, "threading", SimpleNamespace(
        Thread=Unstarted, current_thread=threading.current_thread))
    try:
        with pytest.raises(RuntimeError, match="thread allocation"):
            player.play(source(tmp_path / "media-thread-failure.wav"))
        assert not devices and not hub.media_active
    finally:
        player.close()


def test_default_player_does_not_capture_notification_reporter_as_media(tmp_path):
    from carr_library.progress import ProgressHub
    hub = ProgressHub()
    reporter = hub.start("Notification output")
    with reporter.bind():
        player, _ = player_fixture()
    try:
        player.play(source(tmp_path / "notification-output.wav"))
        wait(player, lambda status: status.state == "playing")
        assert not hub.media_active
    finally:
        player.close()


@pytest.mark.parametrize("when", ["opening", "closing"])
def test_quarantined_native_handle_owns_media_until_its_actual_release(tmp_path, monkeypatch, when):
    import gc
    import carr_library.audio.playback as playback
    from carr_library.progress import ProgressHub
    api, retained, hub = SyntheticWinMM(), [], ProgressHub()
    def position(handle, pointer, size):
        value = ctypes.cast(pointer, ctypes.POINTER(_MMTIME)).contents
        value.kind, value.time.value = 2, 0
        return 0
    monkeypatch.setattr(api, "waveOutGetPosition", position, raising=False)
    monkeypatch.setattr(playback, "_api", lambda: api)
    monkeypatch.setattr(playback, "_UNRELEASED_DEVICES", retained)
    player = PCMPlayer(_backend_factory=playback._WinMMBackend, progress_hub=hub)
    if when == "opening":
        monkeypatch.setattr(api, "waveOutGetDevCapsW", lambda *_: 6)
        api.reset_error = 6
    try:
        player.play(source(tmp_path / "quarantined-media.wav"))
        if when == "closing":
            wait(player, lambda status: status.state == "playing")
            assert hub.media_active
            api.reset_error = 6
            with pytest.raises(PlaybackError, match="did not release"):
                player.stop()
        else:
            wait(player, lambda status: status.state == "error")
        player._thread.join(3)
        assert not player._thread.is_alive()
        assert len(retained) == 1 and retained[0].handle.value
        if when == "closing":
            assert retained[0].buffers  # The driver still owns queued memory.
        assert hub.media_active
        with pytest.raises(PlaybackError, match="did not release"):
            player.close()
        player = None
        gc.collect()
        assert hub.media_active
    finally:
        # Only this synthetic driver is repaired/released. Production retains
        # its quarantined handle and suppression until application restart.
        api.reset_error = 0
        for backend in retained:
            backend.close()
    assert not hub.media_active
    assert all(not backend.handle.value for backend in retained)



def test_live_pcm_input_preserves_split_frames_and_uses_existing_native_worker():
    from carr_library.progress import ProgressHub
    hub, received, devices = ProgressHub(), [], []

    class Advancing(SyntheticDevice):
        def submit(self, data):
            received.append(data)
            return super().submit(data)
        def position(self):
            self.advance()
            return super().position()

    def factory(info, identity):
        value = Advancing(info, identity)
        devices.append(value)
        return value

    player = PCMPlayer(buffer_ms=20, buffer_count=2, _backend_factory=factory, progress_hub=hub)
    try:
        player.start_monitor(sample_rate=8000, channels=2, max_frames=8000, device_id=0)
        assert hub.media_active
        raw = b"\x01\x00\x02\x00\x03\x00\x04\x00"
        player.feed_pcm(raw[:1])
        player.feed_pcm(raw[1:3])
        player.feed_pcm(raw[3:])
        player.finish_monitor()
        assert b"".join(received) == raw
        assert player.status().state == "completed"
        assert player.status().position_frames == player.status().total_frames == 2
        assert player.monitor_quiescent and not hub.media_active
        assert devices[0].closed and len(devices[0].threads) == 1
    finally:
        player.close()


def test_live_pcm_incomplete_frame_cannot_be_declared_complete():
    player, devices = player_fixture(buffer_ms=20, buffer_count=2)
    try:
        player.start_monitor(sample_rate=8000, channels=2, max_frames=8000, device_id=0)
        player.feed_pcm(b"\x01\x00\x02")
        with pytest.raises(PlaybackError, match="inside a sample frame"):
            player.finish_monitor()
        with pytest.raises(PlaybackError, match="positioning"):
            player.seek(0)
    finally:
        player.close()
    assert player.monitor_quiescent and devices[0].closed


def test_live_pcm_backpressure_is_bounded_and_stop_releases_the_same_output():
    player, devices = player_fixture(buffer_ms=20, buffer_count=2)
    try:
        player.start_monitor(sample_rate=8000, channels=1, max_frames=8000, device_id=0)
        started = time.monotonic()
        with pytest.raises(PlaybackError, match="bounded live PCM queue"):
            player.feed_pcm(b"\x01\x00" * 8000)
        assert time.monotonic() - started < 5
        assert player._live_pcm.queue.qsize() <= 2
        assert devices[0].maximum_queued <= 2 * 160 * 2
        player.stop()
        assert player.monitor_quiescent and devices[0].closed
    finally:
        player.close()


@pytest.mark.parametrize("when", ["opening", "closing"])
def test_live_monitor_retains_actual_backend_owner_until_confirmed_close(monkeypatch, when):
    import carr_library.audio.playback as playback
    from carr_library.progress import ProgressHub
    api, retained, hub = SyntheticWinMM(), [], ProgressHub()

    def position(handle, pointer, size):
        value = ctypes.cast(pointer, ctypes.POINTER(_MMTIME)).contents
        value.kind, value.time.value = 2, 0
        return 0

    monkeypatch.setattr(api, "waveOutGetPosition", position, raising=False)
    monkeypatch.setattr(playback, "_api", lambda: api)
    monkeypatch.setattr(playback, "_UNRELEASED_DEVICES", retained)
    player = PCMPlayer(_backend_factory=playback._WinMMBackend, progress_hub=hub)
    if when == "opening":
        monkeypatch.setattr(api, "waveOutGetDevCapsW", lambda *_: 6)
        api.reset_error = 6
    try:
        if when == "opening":
            with pytest.raises(PlaybackError):
                player.start_monitor(sample_rate=8000, channels=1, max_frames=8000, device_id=0)
        else:
            player.start_monitor(sample_rate=8000, channels=1, max_frames=8000, device_id=0)
            player.feed_pcm(b"\x01\x00" * 100)
            wait(player, lambda status: status.buffered_bytes > 0)
            api.reset_error = 6
            with pytest.raises(PlaybackError, match="did not release"):
                player.stop()
        player._thread.join(3)
        assert not player.monitor_quiescent and hub.media_active
        assert len(retained) == 1 and retained[0].handle.value
        with pytest.raises(PlaybackError):
            player.confirm_monitor_shutdown()
        assert not player.monitor_quiescent and hub.media_active
        api.reset_error = 0
        assert player.confirm_monitor_shutdown() is True
        assert player.monitor_quiescent and not hub.media_active
        assert not retained[0].handle.value
    finally:
        api.reset_error = 0
        for backend in retained:
            backend.close()
