"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate.
A/B and sleep policy reuse this transport; callers poll from their existing
event loop, with repeat restarts admitted through their shared quiet policy.
"""
import ctypes
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import uuid4
import wave

from ._io import local_file, stop_process


class WavePlayer:
    def __init__(self, *, clock=time.monotonic):
        self.path = None
        self.duration_seconds = 0
        self._state = 'stopped'
        self._offset = 0.0
        self._started = None
        self._process = None
        self._alias = 'carr_' + uuid4().hex
        self._open = False
        self._clock = clock
        self._end_seconds = 0.0
        self._ended = False
        self._ab = None
        self._repeat_enabled = False
        self.repeat_count = 0
        self._sleep_deadline = None
        self._session_generation = 0
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = shutil.which('ffplay') if os.name != 'nt' else None

    @property
    def session_generation(self):
        return self._session_generation

    @property
    def available(self):
        return os.name == 'nt' or bool(self._executable)

    def _mci(self, command):
        buffer = ctypes.create_unicode_buffer(1024)
        library = ctypes.windll.winmm
        send = library.mciSendStringW
        send.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p)
        send.restype = ctypes.c_uint
        code = send(command, buffer, len(buffer), None)
        if code:
            # Keep source paths out of playback diagnostics.
            raise RuntimeError(f'Windows audio device rejected the operation (MCI {code}).')
        return buffer.value

    @property
    def state(self):
        if self._state == 'playing':
            if os.name == 'nt' and self._open:
                mode = self._mci(f'status {self._alias} mode')
                if mode == 'stopped':
                    self._state, self._ended = 'stopped', True
                    self._offset = self._end_seconds
            elif self._process is not None and self._process.poll() is not None:
                if self._process.returncode:
                    self._state = 'error'
                    self._disarm()
                else:
                    self._state, self._ended = 'stopped', True
                    self._offset = self._end_seconds
        return self._state

    @property
    def position_seconds(self):
        if os.name == 'nt' and self._open:
            return int(self._mci(f'status {self._alias} position')) / 1000
        if self.state == 'playing' and self._started is not None:
            return min(self._end_seconds, self._offset + (self._clock() - self._started))
        return self._offset

    @staticmethod
    def _seconds(value, duration):
        if (type(value) not in (float, int) or not math.isfinite(value)
                or not 0 <= value <= duration):
            raise ValueError('Choose a playback position within the recording.')
        return float(value)

    def _start(self, path, *, start_seconds=0, end_seconds=None, paused=False):
        """Restart the same native transport, retaining caller-owned policy.

        Reconfiguring a paused recording never briefly plays it. Native output
        is reopened only when the user resumes.
        """
        target = local_file(path)
        with wave.open(str(target), 'rb') as audio:
            if (audio.getcomptype() != 'NONE' or audio.getnframes() <= 0
                    or audio.getframerate() <= 0):
                raise ValueError('Playback requires nonempty PCM WAV audio.')
            duration = audio.getnframes() / audio.getframerate()
        start = self._seconds(start_seconds, duration)
        end = duration if end_seconds is None else self._seconds(end_seconds, duration)
        if end < start or end_seconds is not None and end == start:
            raise ValueError('The playback end must follow its starting position.')
        # MCI waveaudio exposes milliseconds. Round inward so it never plays
        # past an explicit B endpoint; intervals below its resolution reject.
        if os.name == 'nt' and end_seconds is not None:
            start_ms, end_ms = math.ceil(start * 1000), math.floor(end * 1000)
            if start_ms >= end_ms:
                raise ValueError('The A/B interval is shorter than the playback device resolution.')
            start, end = start_ms / 1000, end_ms / 1000
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self._close_backend()
        self.path, self.duration_seconds = target, duration
        self._offset, self._end_seconds, self._ended = start, end, False
        if paused:
            self._state, self._started = 'paused', None
            return
        try:
            if os.name == 'nt':
                self._mci(f'open "{target}" type waveaudio alias {self._alias}')
                self._open = True
                self._mci(f'set {self._alias} time format milliseconds')
                suffix = '' if end_seconds is None else f' to {end_ms}'
                self._mci(f'play {self._alias} from {round(start * 1000)}{suffix}')
            else:
                limit = [] if end_seconds is None else ['-t', str(end - start)]
                self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                    '-nodisp', '-autoexit', '-nostats', '-ss', str(start),
                    '-protocol_whitelist', 'file,pipe', '-i', str(target), *limit],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, shell=False)
        except Exception:
            self._disarm()
            self._close_backend()
            raise
        self._started = self._clock()
        self._state = 'playing'

    def play(self, path, *, start_seconds=0):
        # Explicitly opening audio is a new playback session, even at the same
        # path. A previous document's repeat or sleep policy never carries over.
        self._session_generation += 1
        self._disarm()
        self._start(path, start_seconds=start_seconds)

    def pause(self):
        state = self.state
        if state == 'stopped' and self._repeat_enabled and self._ended:
            # B may have finished while its automatic restart waits for quiet.
            # A user pause still wins: retain a paused A position without
            # opening/playing a device, and keep the original sleep deadline.
            self._start(self.path, start_seconds=self._ab[0],
                        end_seconds=self._ab[1], paused=True)
            return True
        if state != 'playing':
            return False
        position = self.position_seconds
        if os.name == 'nt':
            self._mci(f'pause {self._alias}')
        else:
            os.kill(self._process.pid, signal.SIGSTOP)
        self._offset = position
        self._state = 'paused'
        return True

    def resume(self):
        if self.state != 'paused':
            return False
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
            return False
        # A bounds change or sleep expiry can release the paused device. Open
        # it again from the retained position only after this explicit action.
        if not self._open and self._process is None:
            self._start(self.path, start_seconds=self._offset,
                        end_seconds=self._ab[1] if self._repeat_enabled else None)
            return True
        if os.name == 'nt':
            self._mci(f'resume {self._alias}')
        else:
            if self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = self._clock()
        self._state = 'playing'
        return True

    def seek(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        seconds = self._seconds(seconds, self.duration_seconds)
        if self._repeat_enabled and not self._ab[0] <= seconds < self._ab[1]:
            raise ValueError('Disable A/B repeat before seeking outside its range.')
        paused = self.state == 'paused'
        self._start(self.path, start_seconds=seconds,
                    end_seconds=self._ab[1] if self._repeat_enabled else None, paused=paused)

    @property
    def ab_loop(self):
        return self._ab

    @property
    def repeat_enabled(self):
        return self._repeat_enabled

    def set_ab_loop(self, start, end):
        """Set validated points only; enabling repeat is a separate action."""
        if self.path is None:
            raise ValueError('Open a recording before setting A and B.')
        start, end = self._seconds(start, self.duration_seconds), self._seconds(end, self.duration_seconds)
        if start >= end:
            raise ValueError('Point B must follow point A within this recording.')
        if os.name == 'nt' and math.ceil(start * 1000) >= math.floor(end * 1000):
            raise ValueError('The A/B interval is shorter than the playback device resolution.')
        if self._repeat_enabled:
            self.enable_repeat(False)
        self._ab, self.repeat_count = (start, end), 0

    def enable_repeat(self, enabled=True):
        if type(enabled) is not bool:
            raise ValueError('Repeat enabled must be boolean.')
        if enabled and self._ab is None:
            raise ValueError('Set points A and B before enabling repeat.')
        if enabled == self._repeat_enabled:
            return
        state, position = self.state, self.position_seconds
        if state == 'error':
            raise RuntimeError('The playback device stopped. Open the recording again.')
        self._repeat_enabled = enabled
        try:
            if enabled:
                start, end = self._ab
                position = position if start <= position < end else start
                self._start(self.path, start_seconds=position, end_seconds=end, paused=state == 'paused')
            elif state in ('playing', 'paused'):
                self._start(self.path, start_seconds=position, paused=state == 'paused')
        except Exception:
            self._disarm()
            raise

    def clear_ab_loop(self):
        self.enable_repeat(False)
        self._ab, self.repeat_count = None, 0

    def set_sleep_timer(self, seconds=None):
        """Count monotonic elapsed time, including pauses, until explicit expiry."""
        if seconds is None:
            self._sleep_deadline = None
            return
        if (self.path is None or type(seconds) not in (int, float)
                or not math.isfinite(seconds) or seconds <= 0):
            raise ValueError('Open a recording and choose a positive finite sleep interval, or turn it off.')
        deadline = self._clock() + seconds
        if not math.isfinite(deadline):
            raise ValueError('The sleep interval is outside the supported clock range.')
        self._sleep_deadline = deadline

    @property
    def sleep_remaining_seconds(self):
        return None if self._sleep_deadline is None else max(0.0, self._sleep_deadline - self._clock())

    def _expire_sleep(self):
        position, state = self.position_seconds, self.state
        completed_interval = (state == 'stopped' and self._ended
                              and self._end_seconds < self.duration_seconds)
        self._disarm()
        if state in ('playing', 'paused') or completed_interval:
            # Stop and release actual output while retaining a resumable
            # position; no new device is opened by this paused restart.
            self._start(self.path, start_seconds=position, paused=True)

    def restart_repeat(self):
        """One admitted automatic restart; never resume a user-paused player."""
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
            return False
        if not self._repeat_enabled or self.state == 'paused':
            return False
        if self.state == 'error':
            self._disarm()
            return False
        if not self._ended:
            return False
        self._start(self.path, start_seconds=self._ab[0], end_seconds=self._ab[1])
        self.repeat_count += 1
        return True

    def poll(self, *, restart=True):
        """Poll from an existing event loop; no busy loop or new timer thread.

        The native endpoint stops at B. A delayed poll can delay the next
        repetition, never extend the previous one. GUI callers use restart=False
        and admit restart_repeat through their existing progress quiet boundary.
        Sleep expiry takes priority over a pending repetition.
        """
        if type(restart) is not bool:
            raise ValueError('Repeat restart policy must be boolean.')
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
            return 'sleep'
        state = self.state
        if state == 'error':
            self._disarm()
            return 'error'
        if self._repeat_enabled and state == 'stopped' and self._ended:
            if restart:
                return 'repeat' if self.restart_repeat() else None
            return 'repeat_due'
        return None

    def _disarm(self):
        self._ab, self._repeat_enabled, self._sleep_deadline = None, False, None
        self.repeat_count, self._ended = 0, False

    def _halt(self):
        if os.name == 'nt' and self._open:
            self._mci(f'stop {self._alias}')
        elif self._process is not None:
            stop_process(self._process)
            self._process = None
        self._state, self._offset, self._started = 'stopped', 0.0, None
        self._ended = False

    def stop(self):
        # Disarm before a fallible device call: a failed stop must never leave
        # an automatic restart armed.
        self._session_generation += 1
        self._disarm()
        self._halt()

    def _close_backend(self):
        try:
            self._halt()
        finally:
            if os.name == 'nt' and self._open:
                try:
                    self._mci(f'close {self._alias}')
                finally:
                    self._open = False

    def close(self):
        self._session_generation += 1
        self._disarm()
        self._close_backend()
