"""Private bounded waveOut transport for the existing WavePlayer.

Reuses the reviewed WinMM buffer ABI from audio.recording_monitor. Only the
WavePlayer owner calls this helper, including poll; no worker, timer or second
player is created. Levels affect only the PCM copied into this owner's buffers;
no session, endpoint, device or native stream volume setting is changed.
"""
from collections import deque
import ctypes as C
from dataclasses import dataclass
import os
import stat
import wave

from ..audio.recording_monitor import _WaveFormat, _WaveHeader, _Buffer, _api
from ._io import local_file
from ..audio.output_devices import _WaveOutCaps as _Caps, resolve_output_device as resolve_output
from .listening import _open_stream

U32, HANDLE = C.c_uint32, C.c_void_p
_BLOCK_MS, _BUFFER_COUNT = 50, 4
_UNRELEASED_OUTPUTS = []


class PlaybackCleanupError(RuntimeError):
    """The native driver has not acknowledged release of this player's memory."""


@dataclass(frozen=True)
class PlaybackLevels:
    volume: float = 100
    muted: bool = False
    balance: float = 0

    def __post_init__(self):
        if (type(self.volume) not in (int, float) or not 0 <= self.volume <= 100
                or type(self.muted) is not bool
                or type(self.balance) not in (int, float) or not -1 <= self.balance <= 1):
            raise ValueError('Choose volume from 0 to 100, a mute flag and balance from -1 to 1.')

    @property
    def packed(self):
        level = 0 if self.muted else self.volume / 100
        left = round(65535 * level * (1 - max(0, self.balance)))
        right = round(65535 * level * (1 + min(0, self.balance)))
        return left | (right << 16)


# The accepted format ceiling and one 50 ms queue block are the processing bound.
_MAX_BLOCK_BYTES = 384000 * _BLOCK_MS // 1000 * 2 * 4
_GAIN_ONE = 1 << 24


def _pcm_levels(data, *, width, channels, levels):
    """Scale one bounded little-endian PCM block without touching the input."""
    if (type(data) is not bytes or type(width) is not int or width not in (1, 2, 3, 4)
            or type(channels) is not int or channels not in (1, 2)
            or not isinstance(levels, PlaybackLevels) or len(data) > _MAX_BLOCK_BYTES
            or len(data) % (width * channels)):
        raise ValueError('Invalid bounded playback PCM block.')
    if channels == 1 and levels.balance:
        raise ValueError('Stereo balance requires a stereo recording.')
    level = 0 if levels.muted else levels.volume / 100
    gains = (round(_GAIN_ONE * level * (1 - max(0, levels.balance))),
             round(_GAIN_ONE * level * (1 + min(0, levels.balance))))
    if gains == (_GAIN_ONE, _GAIN_ONE):
        return data
    if gains == (0, 0):
        return (b'\x80' if width == 1 else b'\x00') * len(data)
    # Gain never exceeds one. Integer rounding and explicit saturation make the
    # full unsigned-8/signed-16/24/32 PCM range deterministic without overflow.
    minimum, maximum = -(1 << (width * 8 - 1)), (1 << (width * 8 - 1)) - 1
    result = bytearray(len(data))
    for offset in range(0, len(data), width):
        sample = int.from_bytes(data[offset:offset + width], 'little', signed=width != 1)
        if width == 1:
            sample -= 128
        magnitude = (abs(sample) * gains[(offset // width) % channels] + _GAIN_ONE // 2) // _GAIN_ONE
        scaled = min(maximum, max(minimum, -magnitude if sample < 0 else magnitude))
        if width == 1:
            scaled += 128
        result[offset:offset + width] = scaled.to_bytes(width, 'little', signed=width != 1)
    return bytes(result)


class _TimeValue(C.Union):
    _fields_ = [('value', U32), ('reserved', C.c_ubyte * 8)]


class _MMTime(C.Structure):
    _fields_ = [('kind', U32), ('data', _TimeValue)]


def _check(code, operation):
    if code:
        raise RuntimeError(f'Windows playback could not {operation} (WinMM {code}).')


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _library():
    library = _api()
    signatures = {
        'waveOutPause': ([HANDLE], U32),
        'waveOutRestart': ([HANDLE], U32),
        'waveOutGetPosition': ([HANDLE, C.POINTER(_MMTime), U32], U32),
        'waveOutGetDevCapsW': ([C.c_size_t, C.POINTER(_Caps), U32], U32),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return library


class WaveOutput:
    """At most four sample-grid PCM blocks; native ownership until Close ACK."""
    def __init__(self):
        self.library, self.handle = None, HANDLE()
        self.pending = deque()
        self.path = self.identity = self.stream = self.reader = self.device = None
        self.rate = self.channels = self.width = self.block_frames = 0
        self.first_frame = self.last_frame = self.next_frame = 0
        self.paused, self.ended = True, False
        self.stopping = False
        self.levels = PlaybackLevels()
        self._caps = 0

    def _check_source(self):
        current = local_file(self.path).stat()
        opened = os.fstat(self.stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _stamp(current) != self.identity or _stamp(opened) != self.identity):
            raise ValueError('The selected audio changed during playback. Select it again.')

    def open(self, path, *, start_seconds=0, end_seconds=None, device=None,
             levels=PlaybackLevels(), paused=False, expected=None):
        if self.handle.value or self.stream is not None:
            raise RuntimeError('This native playback transport is already owned.')
        if _UNRELEASED_OUTPUTS:
            raise PlaybackCleanupError('An earlier playback driver has not released its buffers. Close the application.')
        if not isinstance(levels, PlaybackLevels) or type(paused) is not bool:
            raise ValueError('Invalid native playback controls.')
        self.path = local_file(path)
        self.identity = _stamp(self.path.stat())
        if expected is not None and self.identity != expected:
            raise ValueError('The reviewed recording changed before native playback.')
        self.stream = _open_stream(self.path)
        try:
            self._check_source()  # Before wave.open reads any source bytes.
            self.reader = wave.open(self.stream, 'rb')
            self.rate, self.channels, self.width = (
                self.reader.getframerate(), self.reader.getnchannels(), self.reader.getsampwidth())
            frames = self.reader.getnframes()
            if (self.reader.getcomptype() != 'NONE' or self.channels not in (1, 2)
                    or self.width not in (1, 2, 3, 4) or not 1 <= self.rate <= 384000 or frames <= 0):
                raise ValueError('Live controls require nonempty mono or stereo PCM WAV audio.')
            duration = frames / self.rate
            end = duration if end_seconds is None else end_seconds
            if (type(start_seconds) not in (int, float) or type(end) not in (int, float)
                    or not 0 <= start_seconds <= end <= duration):
                raise ValueError('Choose native playback bounds inside the recording.')
            # Explicit sample-grid rounding; no word/acoustic timing is inferred.
            self.first_frame = min(frames, round(start_seconds * self.rate))
            self.last_frame = min(frames, round(end * self.rate))
            self.next_frame = self.first_frame
            self.reader.setpos(self.first_frame)
            self.block_frames = max(1, self.rate * _BLOCK_MS // 1000)
            self._check_source()
            selected = resolve_output(device) if device is not None else None
            index = selected.index if selected is not None else 0xFFFFFFFF  # WAVE_MAPPER
            self.library = _library()
            align = self.channels * self.width
            fmt = _WaveFormat(1, self.channels, self.rate, self.rate * align,
                              align, self.width * 8, 0)
            _check(self.library.waveOutOpen(None, index, C.byref(fmt), 0, 0, 1),
                   'accept this PCM format')
            _check(self.library.waveOutOpen(C.byref(self.handle), index, C.byref(fmt), 0, 0, 0),
                   'open this playback instance')
            if not self.handle.value:
                raise RuntimeError('Windows returned no owned playback handle.')
            # Native pause precedes the first PCM write, including paused seeks.
            _check(self.library.waveOutPause(self.handle), 'hold playback before admission')
            actual = U32()
            _check(self.library.waveOutGetID(self.handle, C.byref(actual)), 'identify the playback output')
            if selected is not None:
                if actual.value != selected.index or resolve_output(selected).index != selected.index:
                    raise RuntimeError('The selected output changed during playback admission. No fallback was used.')
                self.device = selected
            caps = _Caps()
            _check(self.library.waveOutGetDevCapsW(self.handle.value, C.byref(caps), C.sizeof(caps)),
                   'inspect this playback instance')
            self._caps = caps.support
            self.set_levels(levels)
            self.paused, self.ended = True, False
            self.pump()
            if not paused:
                self.resume()
        except BaseException:
            self.close()
            raise

    def check_output(self):
        if self.stopping:
            raise PlaybackCleanupError('Playback cleanup is unconfirmed; retry Close before any control.')
        if self.device is not None:
            actual = U32()
            _check(self.library.waveOutGetID(self.handle, C.byref(actual)), 'identify this playback instance')
            if actual.value != self.device.index or resolve_output(self.device).index != self.device.index:
                raise RuntimeError('The explicitly selected playback output changed. Select it again.')

    def set_levels(self, levels):
        if not isinstance(levels, PlaybackLevels) or not self.handle.value:
            raise ValueError('Live controls need a validated setting and an owned playback handle.')
        if levels.balance and self.channels != 2:
            raise RuntimeError('Stereo balance requires a stereo recording.')
        self.check_output()
        if levels == self.levels:
            return
        if self.paused and self.pending:
            # Queued native buffers may not be modified. A paused change resets
            # and releases them, then queues newly scaled bytes at the observed
            # frame without ever restarting sound.
            frame = min(self.last_frame, max(self.first_frame,
                        round(self.position_seconds * self.rate)))
            try:
                _check(self.library.waveOutReset(self.handle), 'discard paused playback buffers')
                self._release_done()
                if self.pending:
                    raise PlaybackCleanupError('Windows did not release the paused playback buffers.')
                _check(self.library.waveOutPause(self.handle), 'keep playback paused after a level change')
                self.first_frame = self.next_frame = frame
                self.reader.setpos(frame)
                self.levels, self.ended = levels, False
                self.pump()
            except BaseException:
                # The same owner retains every buffer and refuses more writes
                # until its normal Close path confirms native release.
                self.stopping = True
                raise
        else:
            # Playing buffers already belong to the driver and stay immutable.
            # New levels reach later buffers within the existing four blocks:
            # max(1, floor(rate / 20)) frames each, normally at most 200 ms.
            self.levels = levels

    def _release_done(self):
        while self.pending and self.pending[0].header.flags & 1:  # WHDR_DONE
            token = self.pending[0]
            _check(self.library.waveOutUnprepareHeader(
                self.handle, C.byref(token.header), C.sizeof(token.header)), 'release a completed playback buffer')
            self.pending.popleft()

    def pump(self):
        if self.stopping:
            raise PlaybackCleanupError('Playback is closing; no further PCM may be queued.')
        if not self.handle.value:
            return
        self._check_source()
        self._release_done()
        while len(self.pending) < _BUFFER_COUNT and self.next_frame < self.last_frame:
            count = min(self.block_frames, self.last_frame - self.next_frame)
            data = self.reader.readframes(count)
            self._check_source()
            if len(data) != count * self.channels * self.width:
                raise ValueError('The PCM recording ended before its measured frame boundary.')
            data = _pcm_levels(data, width=self.width, channels=self.channels, levels=self.levels)
            raw = C.create_string_buffer(data, len(data))
            header = _WaveHeader(data=C.addressof(raw), length=len(data))
            token = _Buffer(raw, header)
            self.pending.append(token)  # Native memory survives every error path.
            _check(self.library.waveOutPrepareHeader(
                self.handle, C.byref(header), C.sizeof(header)), 'prepare playback frames')
            _check(self.library.waveOutWrite(
                self.handle, C.byref(header), C.sizeof(header)), 'queue playback frames')
            self.next_frame += count
        self.ended = self.next_frame >= self.last_frame and not self.pending

    @property
    def position_seconds(self):
        if self.stopping and self.handle.value:
            raise PlaybackCleanupError('Playback cleanup is unconfirmed; no live position is asserted.')
        if not self.handle.value:
            return self.first_frame / self.rate if self.rate else 0
        value = _MMTime(kind=2)  # TIME_SAMPLES; inspect the returned unit.
        _check(self.library.waveOutGetPosition(self.handle, C.byref(value), C.sizeof(value)),
               'read the playback position')
        if value.kind == 2:
            frames = value.data.value
        elif value.kind == 4:  # TIME_BYTES
            frames = value.data.value // (self.channels * self.width)
        elif value.kind == 1:  # TIME_MS: the driver's explicit fallback precision.
            frames = round(value.data.value * self.rate / 1000)
        else:
            raise RuntimeError('Windows returned an unsupported playback position unit.')
        return min(self.last_frame, self.first_frame + frames) / self.rate

    def pause(self):
        if self.stopping:
            raise PlaybackCleanupError('Playback is closing; retry Close before any control.')
        _check(self.library.waveOutPause(self.handle), 'pause this playback instance')
        self.paused = True

    def resume(self):
        self.check_output()
        self.pump()
        _check(self.library.waveOutRestart(self.handle), 'resume this playback instance')
        self.paused = False

    def close(self):
        self.stopping = True
        try:
            if self.handle.value:
                _check(self.library.waveOutReset(self.handle), 'stop queued playback audio')
                for token in tuple(self.pending):
                    _check(self.library.waveOutUnprepareHeader(
                        self.handle, C.byref(token.header), C.sizeof(token.header)), 'release playback memory')
                    self.pending.remove(token)
                _check(self.library.waveOutClose(self.handle), 'close this playback instance')
                self.handle = HANDLE()
        except BaseException as exc:
            if self not in _UNRELEASED_OUTPUTS:
                _UNRELEASED_OUTPUTS.append(self)
            raise PlaybackCleanupError('Playback cleanup is unconfirmed; native buffers were retained. Close the application.') from exc
        # Release source handles only after the native driver released all memory.
        try:
            if self.reader is not None:
                self.reader.close()
        finally:
            if self.stream is not None:
                self.stream.close()
        self.reader = self.stream = self.device = None
        self.paused = True
        if self in _UNRELEASED_OUTPUTS:
            _UNRELEASED_OUTPUTS.remove(self)
