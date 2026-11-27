"""WinMM ownership contract and real file taps; native qualification is separate."""
import ctypes as C
from dataclasses import replace
from pathlib import Path
import wave

import pytest

from carr_library.audio.capture_types import AudioCancelled, AudioError
from carr_library.audio import playback
from carr_library.audio.playback import PlaybackError, PlaybackUnavailable
from carr_library.audio import recording_monitor as monitor
from carr_library.audio.playback_devices import PlaybackDevice


DEVICE = PlaybackDevice(2, 'Explicit output', 'opaque-interface', 1, 2, 3, 2)


class WinMM:
    def __init__(self):
        self.calls, self.headers = [], []
        self.failure, self.actual_id = None, DEVICE.index
    def waveOutGetNumDevs(self): return 3
    def waveOutPause(self, handle):
        self.calls.append(('pause',)); return 0
    def waveOutRestart(self, handle):
        self.calls.append(('resume',)); return 0
    def waveOutGetErrorTextW(self, code, target, count): return 0
    def waveOutGetDevCapsW(self, index, pointer, length):
        caps = C.cast(pointer, C.POINTER(playback._WAVEOUTCAPSW)).contents
        caps.channels = 2
        raw = (DEVICE.name + '\0').encode('utf-16-le')
        C.memmove(C.addressof(caps) + playback._WAVEOUTCAPSW.name.offset, raw, len(raw))
        return 0
    def waveOutOpen(self, output, index, fmt, callback, instance, flags):
        value = C.cast(fmt, C.POINTER(playback._WAVEFORMATEX)).contents
        self.calls.append(('open', index, flags, value.rate, value.channels, value.align))
        assert callback == instance == 0
        if self.failure == ('query' if flags else 'open'):
            return 7
        if not flags:
            C.cast(output, C.POINTER(playback.HANDLE)).contents.value = 1234
        return 0
    def waveOutGetID(self, handle, result):
        assert handle.value == 1234
        C.cast(result, C.POINTER(playback.U32)).contents.value = self.actual_id
        return 0
    def waveOutPrepareHeader(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(playback._WAVEHDR)).contents
        self.calls.append(('prepare', value.data, value.length))
        assert size == C.sizeof(playback._WAVEHDR)
        if self.failure == 'prepare': return 1
        self.headers.append(value)
        value.flags |= 2
        return 0
    def waveOutWrite(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(playback._WAVEHDR)).contents
        self.calls.append(('write', value.data, C.string_at(value.data, value.length)))
        if self.failure == 'write': return 2
        value.flags |= 16
        return 0
    def waveOutUnprepareHeader(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(playback._WAVEHDR)).contents
        self.calls.append(('unprepare', value.data))
        if self.failure == 'unprepare': return 33
        assert not value.flags & 16 or value.flags & 1
        value.flags &= ~2
        return 0
    def waveOutReset(self, handle):
        self.calls.append(('reset',))
        if self.failure == 'reset': return 9
        for header in self.headers:
            header.flags |= 1
        return 0
    def waveOutClose(self, handle):
        self.calls.append(('close',))
        if self.failure == 'close': return 33
        assert all(not header.flags & 2 for header in self.headers)
        self.headers.clear()
        return 0


@pytest.fixture
def native(monkeypatch):
    library = WinMM()
    monkeypatch.setattr(playback, '_api', lambda: library)
    monkeypatch.setattr(monitor, 'resolve_output', lambda selected, **kwargs: selected)
    monkeypatch.setattr(playback, '_UNRELEASED_DEVICES', [])
    yield library
    # Every test must either acknowledge its own retained handle or explicitly
    # inspect a failed-close latch before making this controlled retry succeed.
    library.failure = None
    for owner in tuple(playback._UNRELEASED_DEVICES):
        owner.close()
    assert not playback._UNRELEASED_DEVICES


def test_monitor_reuses_actual_backend_abi_and_opens_only_selected_output(native):
    assert C.sizeof(playback._WAVEFORMATEX) == 18
    assert C.sizeof(playback._WAVEHDR) == (48 if C.sizeof(C.c_void_p) == 8 else 32)
    owner = monitor.DryMonitor()
    assert native.calls == []
    owner.open(24000, 1, DEVICE)
    assert native.calls == [('open', 2, 0, 24000, 1, 2), ('pause',)]
    assert isinstance(owner.backend, playback._WinMMBackend)
    owner.close()
    assert native.calls[-2:] == [('reset',), ('close',)] and owner.backend is None

def test_four_native_buffers_bound_latency_and_release_only_after_done_and_unprepare(native):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    data = b'\x12\x34' * 1200
    assert all(owner.submit(data) for _ in range(4))
    saved = tuple(owner.pending)
    assert not owner.submit(data) and len(native.headers) == 4
    assert sum(token.header.length for token in saved) == 9600  # 200 ms, mono PCM16.
    owner.poll()
    assert tuple(owner.pending) == saved and not any(call[0] == 'unprepare' for call in native.calls)
    saved[0].header.flags |= 1
    owner.poll()
    assert tuple(owner.pending) == saved[1:]
    assert native.calls[-1] == ('unprepare', saved[0].header.data)
    assert owner.submit(data) and len(owner.pending) == 4
    owner.close()
    assert owner.backend is None and not owner.pending


@pytest.mark.parametrize('failure', ['prepare', 'write'])
def test_partial_submission_failure_keeps_exact_pcm_memory_until_acknowledged_close(native, failure):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    native.failure = failure
    try:
        with pytest.raises(PlaybackError):
            owner.submit(b'\x01\x02' * 20)
        if failure == 'write':
            assert len(owner.backend.buffers) == 1
            token = owner.backend.buffers[0]
            assert C.string_at(token.header.data, token.header.length) == b'\x01\x02' * 20
        else:
            assert owner.backend.buffers == []
    finally:
        native.failure = None
        owner.close()
    assert owner.backend is None and not owner.pending

@pytest.mark.parametrize('failure', ['reset', 'unprepare', 'close'])
def test_unconfirmed_native_cleanup_retains_authority_and_prevents_new_monitor(native, failure):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    owner.submit(b'\x01\x02' * 20)
    backend = owner.backend
    native.failure = failure
    with pytest.raises(PlaybackError):
        owner.close()
    assert backend.handle.value == 1234 and playback._UNRELEASED_DEVICES == [backend]
    if failure != 'close':
        assert len(backend.buffers) == 1
        assert C.string_at(backend.buffers[0].header.data, 4) == b'\x01\x02\x01\x02'
    before = list(native.calls)
    with pytest.raises(PlaybackUnavailable, match='earlier audio buffer'):
        monitor.DryMonitor().open(24000, 1, DEVICE)
    assert native.calls == before
    native.failure = None
    owner.close()
    assert not backend.handle.value and owner.backend is None
    assert not owner.pending and not playback._UNRELEASED_DEVICES
    reopened = monitor.DryMonitor()
    reopened.open(24000, 1, DEVICE)
    reopened.close()

def test_selected_output_displacement_during_open_closes_without_submitting(native, monkeypatch):
    calls = []
    def resolve(device, **kwargs):
        calls.append(device)
        return device if len(calls) == 1 else replace(device, index=3)
    monkeypatch.setattr(monitor, 'resolve_output', resolve)
    owner = monitor.DryMonitor()
    with pytest.raises(AudioError, match='changed while opening'):
        owner.open(24000, 1, DEVICE)
    assert owner.backend is None and not native.headers
    assert native.calls[-2:] == [('reset',), ('close',)]


def test_cancellation_after_native_open_confirms_close_before_returning(native):
    owner = monitor.DryMonitor()
    with pytest.raises(AudioCancelled):
        owner.open(24000, 1, DEVICE, cancelled=lambda: bool(getattr(getattr(owner.backend, 'handle', None), 'value', 0)))
    assert owner.backend is None and not native.headers
    assert native.calls[-2:] == [('reset',), ('close',)]


def test_cancellation_before_native_open_has_no_output_side_effect(native):
    with pytest.raises(AudioCancelled):
        monitor.DryMonitor().open(24000, 1, DEVICE, cancelled=lambda: True)
    assert native.calls == []


@pytest.mark.parametrize('data', [b'', b'\x00', b'\x00' * 2402, bytearray(b'\x00\x00')],
                         ids=['empty','partial-frame','oversized-block','mutable-buffer'])
def test_monitor_refuses_partial_or_oversized_frames_before_native_buffer_allocation(native, data):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    try:
        with pytest.raises(ValueError, match='bounded block'):
            owner.submit(data)
        assert not native.headers
    finally:
        owner.close()


class Sink:
    def __init__(self): self.blocks = []
    def submit(self, value):
        self.blocks.append(value)
        return True


def test_real_growing_pcm_tap_reads_only_bounded_tail_without_changing_capture(tmp_path):
    path, sink = tmp_path / 'capture.wav', Sink()
    first = bytes(range(256)) * 100  # 12,800 mono frames.
    next_block = b'\x98\x76' * 1200
    with path.open('wb') as output, wave.open(output, 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        # Match FFmpeg's unknown-length streaming header: monitoring must not
        # allocate according to this declared frame count.
        writer.setnframes(2**30)
        writer.writeframesraw(first)
        output.flush()
        tap = monitor.RecordingMonitorTap(path, 24000, 1, 48000, sink)
        tap.poll()
        assert len(sink.blocks) == 4 and b''.join(sink.blocks) == first[-9600:]
        assert tap.submitted_frames == 4800 and tap.dropped_frames == 8000
        writer.writeframesraw(next_block)
        output.flush()
        tap.poll()
        assert sink.blocks[-1] == next_block and tap.submitted_frames == 6000
        tap.close()
    with wave.open(str(path), 'rb') as reader:
        assert reader.readframes(reader.getnframes()) == first + next_block


def test_growing_pcm_identity_replacement_is_refused_before_more_output(tmp_path):
    path, sink = tmp_path / 'capture.wav', Sink()
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(b'\x01\x02' * 100)
    tap = monitor.RecordingMonitorTap(path, 24000, 1, 24000, sink)
    tap.poll()
    # Replacing an open target is a platform-specific primitive. Changing the
    # tap's path to another actual file exercises its same-inode requirement on
    # both systems without claiming Windows MoveFileEx allows open replacement.
    other = tmp_path / 'other.wav'
    other.write_bytes(path.read_bytes())
    tap.path = other
    try:
        with pytest.raises(AudioError, match='changed during monitoring'):
            tap.poll()
        assert len(sink.blocks) == 1
    finally:
        tap.close()


def test_invalid_partial_header_closes_reader_and_does_not_submit(tmp_path):
    path, sink = tmp_path / 'capture.wav', Sink()
    path.write_bytes(b'RIFF')
    tap = monitor.RecordingMonitorTap(path, 24000, 1, 24000, sink)
    tap.poll()
    assert sink.blocks == []
    tap.close()
    assert tap.stream is None
    path.unlink()  # No lingering reader handle, including on Windows.


def test_output_identity_tracks_opaque_selection_not_transient_ordinal():
    assert monitor.output_identity(DEVICE) == monitor.output_identity(replace(DEVICE, index=7))
    assert monitor.output_identity(DEVICE) != monitor.output_identity(replace(DEVICE, interface='replacement'))


def test_finalized_growing_header_updates_frame_limit_without_repeating_previous_pcm(tmp_path):
    path, sink = tmp_path / 'capture.wav', Sink()
    with path.open('wb') as output, wave.open(output, 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(b'\x01\x02' * 100)
        output.flush()
        tap = monitor.RecordingMonitorTap(path, 24000, 1, 24000, sink)
        try:
            tap.poll()
            writer.writeframes(b'\x03\x04' * 80)
            output.flush()
            tap.poll()
            assert b''.join(sink.blocks) == b'\x01\x02' * 100 + b'\x03\x04' * 80
            assert tap.submitted_frames == 180 and tap.dropped_frames == 0
        finally:
            tap.close()


def test_malformed_monitor_header_is_bounded_and_never_opens_another_input(tmp_path):
    import struct
    path, sink = tmp_path / 'capture.wav', Sink()
    path.write_bytes(b'RIFF' + struct.pack('<I', 0xffffffff) + b'WAVE'
                     + b'JUNK' + struct.pack('<I', 65536))
    tap = monitor.RecordingMonitorTap(path, 24000, 1, 24000, sink)
    try:
        with pytest.raises(AudioError, match='bounded allowance'):
            tap.poll()
        assert sink.blocks == []
    finally:
        tap.close()
