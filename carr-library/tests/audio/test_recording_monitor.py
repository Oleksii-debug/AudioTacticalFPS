"""WinMM ownership contract and real file taps; native qualification is separate."""
import ctypes as C
from dataclasses import replace
from pathlib import Path
import threading
import wave

import pytest

from carr_library.audio.models import AudioCancelled, AudioError
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio import recording_monitor as monitor
from carr_library.speech.output_devices import OutputDevice


DEVICE = OutputDevice(2, 'Explicit output', 'opaque-interface')


class WinMM:
    def __init__(self):
        self.calls, self.headers = [], []
        self.failure, self.actual_id = None, DEVICE.index
    def waveOutOpen(self, output, index, fmt, callback, instance, flags):
        value = C.cast(fmt, C.POINTER(monitor._WaveFormat)).contents
        self.calls.append(('open', index, flags, value.rate, value.channels, value.align))
        assert callback == instance == 0
        if self.failure == ('query' if flags else 'open'):
            return 7
        if not flags:
            C.cast(output, C.POINTER(monitor.HANDLE)).contents.value = 1234
        return 0
    def waveOutGetID(self, handle, result):
        assert handle.value == 1234
        C.cast(result, C.POINTER(monitor.U32)).contents.value = self.actual_id
        return 0
    def waveOutPrepareHeader(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(monitor._WaveHeader)).contents
        self.calls.append(('prepare', value.data, value.length))
        assert size == C.sizeof(monitor._WaveHeader)
        if self.failure == 'prepare': return 1
        value.flags |= 2
        self.headers.append(value)
        return 0
    def waveOutWrite(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(monitor._WaveHeader)).contents
        self.calls.append(('write', value.data, C.string_at(value.data, value.length)))
        if self.failure == 'write': return 2
        value.flags |= 16
        return 0
    def waveOutUnprepareHeader(self, handle, pointer, size):
        value = C.cast(pointer, C.POINTER(monitor._WaveHeader)).contents
        self.calls.append(('unprepare', value.data))
        if self.failure == 'unprepare': return 33
        assert not value.flags & 16 or value.flags & 1
        value.flags &= ~2
        # WinMM no longer owns a successfully unprepared header. A ctypes
        # .contents view does not own the caller's _Buffer: keeping it here
        # would make a later Reset write through released Python memory.
        address = C.addressof(value)
        self.headers[:] = [header for header in self.headers
                           if C.addressof(header) != address]
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
        assert not self.headers, 'A closed driver must own no prepared header.'
        return 0


@pytest.fixture
def native(monkeypatch):
    library = WinMM()
    monkeypatch.setattr(monitor, '_api', lambda: library)
    monkeypatch.setattr(monitor, 'resolve_output_device', lambda selected, **kwargs: selected)
    monkeypatch.setattr(monitor, 'verify_output_device', lambda selected, **kwargs: selected)
    monkeypatch.setattr(monitor, '_UNRELEASED_MONITORS', [])
    yield library
    # Every test must either acknowledge its own retained handle or explicitly
    # inspect a failed-close latch before making this controlled retry succeed.
    library.failure = None
    for owner in tuple(monitor._UNRELEASED_MONITORS):
        owner.close()
    assert not monitor._UNRELEASED_MONITORS


def test_explicit_format_query_precedes_open_and_windows_structures_have_fixed_widths(native):
    assert C.sizeof(monitor._WaveFormat) == 18
    assert C.sizeof(monitor._WaveHeader) == (48 if C.sizeof(C.c_void_p) == 8 else 32)
    owner = monitor.DryMonitor()
    assert native.calls == []
    owner.open(24000, 1, DEVICE)
    assert native.calls == [('open', 2, 1, 24000, 1, 2), ('open', 2, 0, 24000, 1, 2)]
    owner.close()
    assert native.calls[-2:] == [('reset',), ('close',)]


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
    assert len(native.headers) == 3
    assert all(C.addressof(header) != C.addressof(saved[0].header) for header in native.headers)
    assert owner.submit(data) and len(owner.pending) == 4
    owner.close()
    assert not owner.handle.value and not owner.pending and not native.headers


@pytest.mark.parametrize('failure', ['prepare', 'write'])
def test_partial_submission_failure_keeps_exact_pcm_memory_until_acknowledged_close(native, failure):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    native.failure = failure
    with pytest.raises(AudioError):
        owner.submit(b'\x01\x02' * 20)
    assert len(owner.pending) == 1
    token = owner.pending[0]
    assert C.string_at(token.header.data, token.header.length) == b'\x01\x02' * 20
    native.failure = None
    owner.close()
    assert not owner.pending and not owner.handle.value


@pytest.mark.parametrize('failure', ['reset', 'unprepare', 'close'])
def test_unconfirmed_native_cleanup_retains_authority_and_prevents_new_monitor(native, failure):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    owner.submit(b'\x01\x02' * 20)
    native.failure = failure
    with pytest.raises(AudioError, match='retained'):
        owner.close()
    assert owner.handle.value == 1234 and monitor._UNRELEASED_MONITORS == [owner]
    if failure != 'close':
        assert len(owner.pending) == len(native.headers) == 1
        assert C.string_at(owner.pending[0].header.data, 4) == b'\x01\x02\x01\x02'
    else:
        assert not owner.pending and not native.headers
    before = list(native.calls)
    with pytest.raises(AudioError, match='earlier buffers'):
        monitor.DryMonitor().open(24000, 1, DEVICE)
    assert native.calls == before
    native.failure = None
    owner.close()
    assert not owner.handle.value and not owner.pending and not monitor._UNRELEASED_MONITORS


def test_close_failure_retry_never_resets_or_unprepares_released_header(native):
    owner = monitor.DryMonitor()
    owner.open(24000, 1, DEVICE)
    owner.submit(b'\x03\x04' * 20)
    # Keep this test-owned token alive even on a failing baseline assertion:
    # the old fixture's dangling view must fail the ownership assertion rather
    # than corrupt the pytest process during its controlled cleanup.
    token = owner.pending[0]
    address = C.addressof(token.header)
    native.failure = 'close'
    try:
        with pytest.raises(AudioError):
            owner.close()
        assert not owner.pending
        assert not native.headers, 'Acknowledged Unprepare must retire the exact driver pointer.'
        assert owner.handle.value == 1234 and monitor._UNRELEASED_MONITORS == [owner]
        unprepared = [call for call in native.calls if call[0] == 'unprepare']
        assert unprepared == [('unprepare', token.header.data)]
        # Poison the now application-owned header. Reset must not touch it.
        token.header.flags = 0
        native.failure = None
        owner.close()
        assert C.addressof(token.header) == address and token.header.flags == 0
        assert [call for call in native.calls if call[0] == 'unprepare'] == unprepared
        assert native.calls[-2:] == [('reset',), ('close',)]
        assert not owner.handle.value and not monitor._UNRELEASED_MONITORS
    finally:
        native.failure = None
        owner.close()


def test_selected_output_displacement_during_open_closes_without_submitting(native, monkeypatch):
    monkeypatch.setattr(monitor, 'verify_output_device',
                        lambda device, **kwargs: replace(device, interface='replacement'))
    owner = monitor.DryMonitor()
    with pytest.raises(AudioError, match='changed while opening'):
        owner.open(24000, 1, DEVICE)
    assert not owner.handle.value and not native.headers
    assert native.calls[-2:] == [('reset',), ('close',)]


def test_cancellation_after_native_open_confirms_close_before_returning(native):
    owner = monitor.DryMonitor()
    with pytest.raises(AudioCancelled):
        owner.open(24000, 1, DEVICE, cancelled=lambda: bool(owner.handle.value))
    assert not owner.handle.value and not native.headers
    assert native.calls[-2:] == [('reset',), ('close',)]


def test_cancellation_before_native_open_has_no_output_side_effect(native):
    with pytest.raises(AudioCancelled):
        monitor.DryMonitor().open(24000, 1, DEVICE, cancelled=lambda: True)
    assert native.calls == []


@pytest.mark.parametrize('data', [b'', b'\x00', b'\x00' * 2402, bytearray(b'\x00\x00')])
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
    assert tap.reader is tap.stream is None and sink.blocks == []
    path.unlink()  # No lingering reader handle, including on Windows.


def test_output_identity_tracks_opaque_selection_not_transient_ordinal():
    assert monitor.output_identity(DEVICE) == monitor.output_identity(replace(DEVICE, index=7))
    assert monitor.output_identity(DEVICE) != monitor.output_identity(replace(DEVICE, interface='replacement'))


def test_output_ordinal_reorder_resolves_the_original_identity_before_open(native, monkeypatch):
    moved = replace(DEVICE, index=7)
    native.actual_id = 7
    monkeypatch.setattr(monitor, 'resolve_output_device', lambda device, **kwargs: moved)
    owner = monitor.DryMonitor()
    try:
        owner.open(24000, 1, DEVICE)
        assert owner.device is moved
        assert [call[1] for call in native.calls if call[0] == 'open'] == [7, 7]
    finally:
        owner.close()


def test_missing_output_is_rejected_before_any_native_open(native, monkeypatch):
    def absent(*args, **kwargs):
        raise RuntimeError('The selected output was removed.')
    monkeypatch.setattr(monitor, 'resolve_output_device', absent)
    with pytest.raises(RuntimeError, match='removed'):
        monitor.DryMonitor().open(24000, 1, DEVICE)
    assert not native.calls


@pytest.mark.parametrize('case', ['rate', 'channels', 'frames', 'boolean-frames'])
def test_tap_rejects_unbounded_or_untyped_format(tmp_path, case):
    values = dict(sample_rate=24000, channels=1, maximum_frames=24000)
    values.update({'rate': {'sample_rate': 1}, 'channels': {'channels': 3},
                   'frames': {'maximum_frames': 24000 * 21600 + 1},
                   'boolean-frames': {'maximum_frames': True}}[case])
    with pytest.raises(ValueError, match='bounded PCM'):
        monitor.RecordingMonitorTap(tmp_path / 'absent.wav', monitor=Sink(), **values)


def test_capture_context_counts_actual_submitted_pcm_and_closes_before_return(native, tmp_path):
    path = tmp_path / 'capture.wav'
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(b'\x01\x02' * 2400)
    before = path.read_bytes()
    with monitor.monitored_capture(path, sample_rate=24000, channels=1,
            maximum_frames=24000, device=DEVICE) as capture:
        capture.poll()
        receipt = capture.receipt()
        assert len(capture.monitor.pending) == 2
        assert receipt == {'enabled': True, 'output_identity': DEVICE.identity,
                           'submitted_frames': 2400, 'dropped_frames': 0}
    assert not capture.monitor.handle.value and not capture.monitor.pending
    assert capture.tap.reader is capture.tap.stream is None
    assert path.read_bytes() == before


@pytest.mark.parametrize('wrapped', [False, True], ids=['direct-pending', 'masked-pending'])
@pytest.mark.parametrize('first', ['process', 'monitor'])
def test_capture_context_retains_both_native_owners_until_both_are_quiescent(
        native, tmp_path, monkeypatch, wrapped, first):
    child_done = threading.Event()
    process_owner = CleanupOwner(child_done.is_set)
    notices = []
    made = []

    def controlled_pending(attempt, *, message):
        error = AudioCleanupPending(message, cleanup_owner=CleanupOwner(attempt))
        made.append(error)
        return error

    monkeypatch.setattr(monitor, 'pending_cleanup', controlled_pending)
    path = tmp_path / 'capture.wav'
    with pytest.raises(AudioCleanupPending) as caught:
        with monitor.monitored_capture(path, sample_rate=24000, channels=1,
                maximum_frames=24000, device=DEVICE) as capture:
            capture.monitor.submit(b'\x03\x04' * 100)
            native.failure = 'reset'
            original = AudioCleanupPending('The owned capture child is still exiting.',
                                           cleanup_owner=process_owner)
            if wrapped:
                raise OSError('Status persistence failed.') from original
            raise original
    owner = caught.value.cleanup_owner
    assert caught.value is made[0]
    owner.add_quiescent_callback(lambda: notices.append('all-resources-closed'))
    assert not owner.quiescent and not process_owner.quiescent
    assert capture.monitor.handle.value and capture.monitor.pending
    if first == 'process':
        child_done.set()
    else:
        native.failure = None
    assert owner.close() is False
    assert not owner.quiescent and not notices
    if first == 'process':
        assert process_owner.quiescent and capture.monitor.handle.value
        native.failure = None
    else:
        assert not capture.monitor.handle.value and not process_owner.quiescent
        child_done.set()
    assert owner.close() is True
    assert process_owner.quiescent and not capture.monitor.handle.value
    assert not capture.monitor.pending and not monitor._UNRELEASED_MONITORS
    assert notices == ['all-resources-closed']
    assert owner.close() is True and notices == ['all-resources-closed']


def test_failed_open_cleanup_retains_driver_memory_through_existing_owner(native, tmp_path, monkeypatch):
    made = []
    def controlled_pending(attempt, *, message):
        error = AudioCleanupPending(message, cleanup_owner=CleanupOwner(attempt))
        made.append(error)
        return error
    monkeypatch.setattr(monitor, 'pending_cleanup', controlled_pending)
    native.actual_id = 17
    native.failure = 'reset'
    with pytest.raises(AudioCleanupPending) as caught:
        with monitor.monitored_capture(tmp_path / 'absent.wav', sample_rate=24000,
                channels=1, maximum_frames=24000, device=DEVICE):
            pytest.fail('A displaced device must not start capture.')
    assert caught.value is made[0] and monitor._UNRELEASED_MONITORS
    assert not caught.value.cleanup_owner.close()
    native.failure = None
    assert caught.value.cleanup_owner.close()
    assert not monitor._UNRELEASED_MONITORS
