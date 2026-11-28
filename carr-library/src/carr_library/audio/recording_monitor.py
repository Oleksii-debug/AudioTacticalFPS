"""Explicit dry monitoring of the same private PCM capture, never another input.

The bounded waveOut buffer lifecycle is adapted from prepared f510053b
audio/playback.py and capture.py. No reader/player/decoder service is introduced.
Only the capture worker operates these buffers. An uncertain close retains their
memory; the existing CleanupOwner retries release and holds capture admission.
"""
from collections import deque
import ctypes as C
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import wave

from .models import AudioCancelled, AudioError, AudioUnavailable
from ..durable_state import local_path, regular
from .output_devices import OutputDevice, resolve_output_device

_BUFFER_MS, _BUFFER_COUNT = 50, 4
_UNRELEASED_MONITORS = []
U16, U32, UPTR, HANDLE = C.c_uint16, C.c_uint32, C.c_size_t, C.c_void_p


class _WaveFormat(C.Structure):
    _pack_ = 1
    _fields_ = [('format', U16), ('channels', U16), ('rate', U32), ('byte_rate', U32),
                ('align', U16), ('bits', U16), ('extra', U16)]


class _WaveHeader(C.Structure):
    _fields_ = [('data', HANDLE), ('length', U32), ('recorded', U32), ('user', UPTR),
                ('flags', U32), ('loops', U32), ('next', HANDLE), ('reserved', UPTR)]


@dataclass
class _Buffer:
    data: object
    header: _WaveHeader


def output_identity(device):
    if type(device) is not OutputDevice:
        raise ValueError('Monitoring needs an explicitly selected identifiable output.')
    return sha256(json.dumps(device.identity, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def _resolve_output(device, cancelled):
    if cancelled():
        raise AudioCancelled('Monitoring output selection cancelled.')
    current = resolve_output_device(device)
    if cancelled():
        raise AudioCancelled('Monitoring output selection cancelled.')
    return current


def _api():
    if os.name != 'nt':
        raise AudioUnavailable('Dry microphone monitoring requires an explicitly selected Windows output.')
    library = C.WinDLL('winmm.dll', winmode=0x0800, use_last_error=True)
    signatures = {
        'waveOutOpen': ([C.POINTER(HANDLE), U32, C.POINTER(_WaveFormat), UPTR, UPTR, U32], U32),
        'waveOutGetID': ([HANDLE, C.POINTER(U32)], U32),
        'waveOutPrepareHeader': ([HANDLE, C.POINTER(_WaveHeader), U32], U32),
        'waveOutWrite': ([HANDLE, C.POINTER(_WaveHeader), U32], U32),
        'waveOutUnprepareHeader': ([HANDLE, C.POINTER(_WaveHeader), U32], U32),
        'waveOutReset': ([HANDLE], U32), 'waveOutClose': ([HANDLE], U32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return library


def _check(code, operation):
    if code:
        raise AudioError(f'Windows monitoring could not {operation} (WinMM {code}).')


class DryMonitor:
    """One explicitly chosen waveOut handle; at most 200 ms of queued PCM."""
    def __init__(self):
        self.library, self.handle = None, HANDLE()
        self.pending = deque()
        self.sample_rate = self.channels = self.block_frames = 0
        self.device = None

    def open(self, sample_rate, channels, device, *, cancelled=lambda: False):
        if self.handle.value or self.pending:
            raise AudioError('A monitoring device is already owned.')
        if _UNRELEASED_MONITORS:
            raise AudioError('A monitoring driver has not released earlier buffers; retry its retained cleanup first.')
        if type(sample_rate) is not int or sample_rate not in (16000, 24000, 44100, 48000):
            raise ValueError('Unsupported monitoring sample rate.')
        if type(channels) is not int or channels not in (1, 2):
            raise ValueError('Unsupported monitoring channel count.')
        device = _resolve_output(device, cancelled)
        if cancelled():
            raise AudioCancelled('Monitoring cancelled before opening an output.')
        self.library = _api()
        self.sample_rate, self.channels = sample_rate, channels
        self.block_frames = sample_rate * _BUFFER_MS // 1000
        fmt = _WaveFormat(1, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16, 0)
        # WAVE_FORMAT_QUERY checks the format without opening a device.
        _check(self.library.waveOutOpen(None, device.index, C.byref(fmt), 0, 0, 1), 'accept this PCM format')
        if cancelled():
            raise AudioCancelled('Monitoring cancelled before opening an output.')
        try:
            _check(self.library.waveOutOpen(C.byref(self.handle), device.index, C.byref(fmt), 0, 0, 0),
                   'open the selected output')
            if not self.handle.value:
                raise AudioUnavailable('Windows returned no owned monitoring handle.')
            actual = U32()
            _check(self.library.waveOutGetID(self.handle, C.byref(actual)), 'identify the selected output')
            if actual.value != device.index or _resolve_output(device, cancelled).index != device.index:
                raise AudioUnavailable('The monitoring output changed while opening; no fallback was used.')
            if cancelled():
                raise AudioCancelled('Monitoring cancelled while opening an output.')
            self.device = device
        except BaseException:
            self.close()
            raise

    def poll(self):
        while self.pending and self.pending[0].header.flags & 1:  # WHDR_DONE
            token = self.pending[0]
            _check(self.library.waveOutUnprepareHeader(self.handle, C.byref(token.header), C.sizeof(token.header)),
                   'release a completed buffer')
            self.pending.popleft()

    def submit(self, data):
        if not self.handle.value:
            raise AudioError('Monitoring has no owned output handle.')
        if (type(data) is not bytes or not data or len(data) % (self.channels * 2)
                or len(data) > self.block_frames * self.channels * 2):
            raise ValueError('Monitoring accepts one bounded block of complete PCM frames.')
        self.poll()
        if len(self.pending) >= _BUFFER_COUNT:
            return False
        buffer = C.create_string_buffer(data, len(data))
        header = _WaveHeader(data=C.addressof(buffer), length=len(data))
        token = _Buffer(buffer, header)
        # Retain memory before either native call, including partial failure.
        self.pending.append(token)
        _check(self.library.waveOutPrepareHeader(self.handle, C.byref(header), C.sizeof(header)),
               'prepare a monitoring buffer')
        _check(self.library.waveOutWrite(self.handle, C.byref(header), C.sizeof(header)),
               'queue a monitoring buffer')
        return True

    def close(self):
        if not self.handle.value:
            return
        try:
            _check(self.library.waveOutReset(self.handle), 'stop queued monitoring audio')
            # Reset returns pending buffers. Memory remains owned until every
            # UnprepareHeader and then Close reports success.
            # Unprepare of a header not yet prepared returns zero (Microsoft):
            # https://learn.microsoft.com/windows/win32/api/mmeapi/nf-mmeapi-waveoutunprepareheader
            for token in tuple(self.pending):
                _check(self.library.waveOutUnprepareHeader(self.handle, C.byref(token.header), C.sizeof(token.header)),
                       'release a monitoring buffer')
                self.pending.remove(token)
            _check(self.library.waveOutClose(self.handle), 'close the monitoring device')
            self.handle = HANDLE()
            self.device = None
            if self in _UNRELEASED_MONITORS:
                _UNRELEASED_MONITORS.remove(self)
        except BaseException as exc:
            if self not in _UNRELEASED_MONITORS:
                _UNRELEASED_MONITORS.append(self)
            raise AudioError('Monitoring cleanup is unconfirmed; owned buffers remain retained for retry.') from exc


class RecordingMonitorTap:
    """Read only appended frames from this capture's private WAV, never a device.

    A slow output drops old monitoring samples instead of delaying capture or
    accumulating memory. The saved PCM is unchanged. The receipt counts submitted
    and dropped frames separately; submission is not a listening-quality claim.
    """
    def __init__(self, path, sample_rate, channels, maximum_frames, monitor):
        self.path, self.sample_rate, self.channels = local_path(path), sample_rate, channels
        self.maximum_frames, self.monitor = maximum_frames, monitor
        self.stream = self.reader = None
        self.offset = self.position = self.submitted_frames = self.dropped_frames = 0
        self.identity = None

    def poll(self, *, cancelled=lambda: False):
        if cancelled():
            raise AudioCancelled('Monitoring cancelled.')
        if self.reader is None:
            try:
                info = regular(self.path)
            except FileNotFoundError:
                return  # The admitted child has not written its header yet.
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            stream = os.fdopen(os.open(self.path, flags), 'rb')
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                stream.close()
                raise AudioError('The private recording changed before monitoring.')
            try:
                reader = wave.open(stream, 'rb')
            except (EOFError, wave.Error):
                stream.close()
                return  # Header construction is checked finally by the recorder.
            except BaseException:
                stream.close()
                raise
            if (reader.getcomptype() != 'NONE' or reader.getframerate() != self.sample_rate
                    or reader.getnchannels() != self.channels or reader.getsampwidth() != 2):
                reader.close()
                stream.close()
                raise AudioError('The monitoring PCM format differs from the selected capture.')
            self.stream, self.reader = stream, reader
            self.offset = stream.tell()
            self.identity = info.st_dev, info.st_ino
        current = regular(self.path)
        opened = os.fstat(self.stream.fileno())
        if (self.identity != (current.st_dev, current.st_ino)
                or self.identity != (opened.st_dev, opened.st_ino)):
            raise AudioError('The private recording changed during monitoring.')
        width = self.channels * 2
        available = min(self.maximum_frames, self.reader.getnframes(),
                        (opened.st_size - self.offset) // width)
        if available < self.position:
            raise AudioError('The private recording was truncated during monitoring.')
        block_frames = self.sample_rate * _BUFFER_MS // 1000
        backlog = max(0, available - self.position - block_frames * _BUFFER_COUNT)
        self.position += backlog
        self.dropped_frames += backlog
        self.reader.setpos(self.position)
        remaining = available - self.position
        while remaining:
            if cancelled():
                raise AudioCancelled('Monitoring cancelled.')
            count = min(remaining, block_frames)
            data = self.reader.readframes(count)
            if len(data) != count * width:
                raise AudioError('The private recording changed while monitoring a PCM block.')
            if self.monitor.submit(data):
                self.submitted_frames += count
            else:
                self.dropped_frames += count
            self.position += count
            remaining -= count

    def close(self):
        try:
            if self.reader is not None:
                self.reader.close()
        finally:
            if self.stream is not None:
                self.stream.close()
        self.stream = self.reader = None
