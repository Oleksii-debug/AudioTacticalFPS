"""Bounded dry monitoring of the same owned capture, through existing WinMM.

Only explicitly selected, revalidated output identities may be opened. The
recorder worker owns the existing playback backend and at most 200 ms of queued
PCM. Slow outputs drop monitoring frames; retained microphone PCM is unchanged.
No second input, decoder, runtime download or default-device fallback is used.
"""
from collections import deque
from hashlib import sha256
import json
import os
from pathlib import Path
import struct

from .capture_types import AudioCancelled, AudioError
from .ffmpeg import WaveInfo
from .jobs import _open_json_file
from .playback_devices import PlaybackDevice, resolve_output
from ..operations._state import regular

_BUFFER_MS, _BUFFER_COUNT = 50, 4
_MAX_HEADER_BYTES, _MAX_HEADER_CHUNKS = 65536, 128


def output_identity(device):
    if not isinstance(device, PlaybackDevice) or not device.selectable:
        raise ValueError('Monitoring needs an explicitly selected identifiable output.')
    return sha256(json.dumps(device.identity, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class DryMonitor:
    """Borrow the established native buffer owner; use no additional player."""
    def __init__(self):
        self.backend = None
        self.pending = deque()
        self.sample_rate = self.channels = self.block_frames = 0
        self.device = None
        self.started = False

    def open(self, sample_rate, channels, device, *, cancelled=lambda: False):
        if self.backend is not None or self.pending:
            raise AudioError('A monitoring output is already owned.')
        if type(sample_rate) is not int or sample_rate not in (16000, 24000, 44100, 48000):
            raise ValueError('Unsupported monitoring sample rate.')
        if type(channels) is not int or channels not in (1, 2):
            raise ValueError('Unsupported monitoring channel count.')
        device = resolve_output(device, cancelled=cancelled)
        if cancelled():
            raise AudioCancelled('Monitoring cancelled before opening an output.')
        from .playback import _WinMMBackend
        self.sample_rate, self.channels = sample_rate, channels
        self.block_frames = sample_rate * _BUFFER_MS // 1000
        # Retain the established owner before its constructor can acquire a
        # native handle. An opening failure can therefore be retried by the
        # recorder's actual cleanup owner without losing partial native state.
        try:
            self.backend = _WinMMBackend.__new__(_WinMMBackend)
            info = WaveInfo(sample_rate, channels, 0, 0, 0)
            _WinMMBackend.__init__(self.backend, info, device_id=device.index)
            if (self.backend.device.id != device.index
                    or resolve_output(device, cancelled=cancelled).index != device.index):
                raise AudioError('The selected monitoring output changed while opening; no fallback was used.')
            if cancelled():
                raise AudioCancelled('Monitoring cancelled while opening an output.')
            self.device = device
        except BaseException:
            self.close()
            raise

    def poll(self):
        if self.backend is None:
            return
        while self.pending and self.backend.completed(self.pending[0]):
            token = self.pending[0]
            self.backend.release(token)
            self.pending.popleft()

    def submit(self, data):
        if self.backend is None or self.device is None:
            raise AudioError('Monitoring has no verified owned output.')
        if (type(data) is not bytes or not data or len(data) % (self.channels * 2)
                or len(data) > self.block_frames * self.channels * 2):
            raise ValueError('Monitoring accepts one bounded block of complete PCM frames.')
        self.poll()
        if len(self.pending) >= _BUFFER_COUNT:
            return False
        token = self.backend.submit(data)
        self.pending.append(token)
        if not self.started:
            self.backend.resume()
            self.started = True
        return True

    def close(self):
        # The recording service wraps any failed attempt in the same retained
        # CleanupOwner used by its input process and resource reservation.
        backend = self.backend
        if backend is None:
            return
        handle = getattr(backend, 'handle', None)
        if handle is not None and handle.value:
            backend.close()
        if getattr(getattr(backend, 'handle', None), 'value', None) or getattr(backend, 'buffers', ()):
            raise AudioError('The monitoring backend has not released its native buffers.')
        self.backend = None
        self.pending.clear()
        self.device = None
        self.started = False


def _capture_header(stream, sample_rate, channels):
    """Inspect a bounded, still-growing RIFF header written by our owned child."""
    stream.seek(0)
    data = stream.read(_MAX_HEADER_BYTES)
    if len(data) < 12:
        return None
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise AudioError('The monitoring source is not the selected RIFF PCM capture.')
    offset, format_seen = 12, False
    for _ in range(_MAX_HEADER_CHUNKS):
        if offset + 8 > len(data):
            return None
        name, size = struct.unpack_from('<4sI', data, offset)
        offset += 8
        if name == b'data':
            if not format_seen:
                raise AudioError('The monitoring capture has no preceding PCM format.')
            return offset, size
        if size == 0xffffffff:
            return None  # Our writer has not yet finalized this non-data header.
        if size > _MAX_HEADER_BYTES or offset + size + (size & 1) > _MAX_HEADER_BYTES:
            raise AudioError('The monitoring header exceeded its bounded allowance.')
        if offset + size > len(data):
            return None
        if name == b'fmt ':
            if format_seen or size < 16:
                raise AudioError('The monitoring capture has an invalid PCM format.')
            encoding, actual_channels, rate, byte_rate, align, bits = struct.unpack_from('<HHIIHH', data, offset)
            if encoding == 65534 and size >= 40:
                extra, valid_bits = struct.unpack_from('<HH', data, offset + 16)
                if extra < 22 or valid_bits not in (0, 16) or data[offset + 24:offset + 40] != bytes.fromhex('0100000000001000800000aa00389b71'):
                    raise AudioError('The monitoring capture is not integer PCM.')
                encoding = 1
            if (encoding != 1 or actual_channels != channels or rate != sample_rate
                    or bits != 16 or align != channels * 2 or byte_rate != sample_rate * channels * 2):
                raise AudioError('The monitoring PCM format differs from the selected capture.')
            format_seen = True
        offset += size + (size & 1)
    raise AudioError('The monitoring header exceeded its chunk allowance.')


class RecordingMonitorTap:
    """Read appended PCM only, never reopen the input or rewrite captured frames.

    The stream deliberately permits the already-owned FFmpeg writer. Identity is
    checked on each poll; whole-file/range immutability is checked after the
    actual child has finalized, before the recording receipt can be published.
    """
    def __init__(self, path, sample_rate, channels, maximum_frames, monitor):
        self.path, self.sample_rate, self.channels = Path(path), sample_rate, channels
        self.maximum_frames, self.monitor = maximum_frames, monitor
        self.stream = None
        self.offset = self.position = self.submitted_frames = self.dropped_frames = 0
        self.identity = None
        self.declared_bytes = None

    def _same(self):
        current, opened = regular(self.path), os.fstat(self.stream.fileno())
        if (self.identity != (current.st_dev, current.st_ino)
                or self.identity != (opened.st_dev, opened.st_ino)
                or opened.st_nlink != 1):
            raise AudioError('The private recording changed during monitoring.')
        return opened

    def poll(self, *, cancelled=lambda: False):
        if cancelled():
            raise AudioCancelled('Monitoring cancelled.')
        if self.stream is None:
            try:
                before = regular(self.path)
            except FileNotFoundError:
                return
            stream = _open_json_file(self.path)  # Shared native read; never a write.
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
                stream.close()
                raise AudioError('The private recording changed while monitoring opened it.')
            self.stream, self.identity = stream, (before.st_dev, before.st_ino)
        opened = self._same()
        header = _capture_header(self.stream, self.sample_rate, self.channels)
        if header is None:
            if self.offset:
                raise AudioError('The private recording header became incomplete during monitoring.')
            return
        if self.offset and header[0] != self.offset:
            raise AudioError('The private recording PCM offset changed during monitoring.')
        self.offset, self.declared_bytes = header
        width = self.channels * 2
        available = min(self.maximum_frames, max(0, (opened.st_size - self.offset) // width))
        if self.declared_bytes != 0xffffffff:
            available = min(available, self.declared_bytes // width)
        if available < self.position:
            raise AudioError('The private recording was truncated during monitoring.')
        block_frames = self.sample_rate * _BUFFER_MS // 1000
        backlog = max(0, available - self.position - block_frames * _BUFFER_COUNT)
        self.position += backlog
        self.dropped_frames += backlog
        self.stream.seek(self.offset + self.position * width)
        remaining = available - self.position
        while remaining:
            if cancelled():
                raise AudioCancelled('Monitoring cancelled.')
            count = min(remaining, block_frames)
            data = self.stream.read(count * width)
            if len(data) != count * width:
                raise AudioError('The private recording changed while monitoring a PCM block.')
            self._same()
            if self.monitor.submit(data):
                self.submitted_frames += count
            else:
                self.dropped_frames += count
            self.position += count
            remaining -= count

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
