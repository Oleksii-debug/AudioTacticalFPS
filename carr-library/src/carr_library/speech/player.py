"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate.
"""
import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import uuid4
import wave

from ._io import local_file, stop_process
from ..audio.process import native_audio_environment


class WavePlayer:
    def __init__(self):
        self.path = None
        self.duration_seconds = 0
        self._state = 'stopped'
        self._offset = 0.0
        self._started = None
        self._process = None
        self._alias = 'carr_' + uuid4().hex
        self._open = False
        self._resume_from_seek = False
        self._identity = None
        self._end_seconds = None
        self._play_start = 0.0
        self._ab = None
        self._repeat_armed = False
        self._ended = False
        self._sleep_deadline = None
        self._skip_intervals = (10.0, 30.0)
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = shutil.which('ffplay') if os.name != 'nt' else None

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
            end = self.duration_seconds if self._end_seconds is None else self._end_seconds
            if os.name == 'nt' and self._open:
                mode = self._mci(f'status {self._alias} mode')
                if mode == 'stopped':
                    position = int(self._mci(f'status {self._alias} position')) / 1000
                    self._ended = self._play_start < end and position >= end - .002
                    self._state = 'stopped'
                    self._offset = min(self.duration_seconds, max(0, position))
                    self._started = None
            elif self._process is not None and self._process.poll() is not None:
                if self._process.returncode:
                    self._state = 'error'
                    self._ended = False
                    self._repeat_armed = False
                else:
                    self._state = 'stopped'
                    self._offset = end
                    self._ended = self._play_start < end
                self._started = None
        return self._state

    @property
    def position_seconds(self):
        if os.name == 'nt' and self._open:
            return int(self._mci(f'status {self._alias} position')) / 1000
        if self.state == 'playing' and self._started is not None:
            end = self.duration_seconds if self._end_seconds is None else self._end_seconds
            return min(end, self._offset + time.monotonic() - self._started)
        return self._offset

    @staticmethod
    def _stamp(path):
        info = local_file(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _play_command(self, seconds):
        command = f'play {self._alias} from {round(seconds * 1000)}'
        if self._end_seconds is not None:
            command += f' to {round(self._end_seconds * 1000)}'
        return command

    def play(self, path, *, start_seconds=0, end_seconds=None):
        """Explicitly play a new recording; prior repeat/sleep choices are reset."""
        return self._play(path, start_seconds=start_seconds, end_seconds=end_seconds)

    def _play(self, path, *, start_seconds=0, end_seconds=None, expected=None):
        target = local_file(path)
        identity = self._stamp(target)
        if expected is not None and identity != expected:
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        with wave.open(str(target), 'rb') as audio:
            if audio.getcomptype() != 'NONE' or audio.getnframes() <= 0:
                raise ValueError('Playback requires nonempty PCM WAV audio.')
            duration = audio.getnframes() / audio.getframerate()
        if (type(start_seconds) not in (float, int) or not 0 <= start_seconds <= duration):
            raise ValueError('Choose a playback position within the recording.')
        if (end_seconds is not None and (type(end_seconds) not in (float, int)
                or not start_seconds < end_seconds <= duration)):
            raise ValueError('Choose a playback end after its start and inside the recording.')
        if self._stamp(target) != identity:
            raise ValueError('The recording changed while its PCM header was read.')
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self.close()
        if self._stamp(target) != identity:
            raise ValueError('The recording changed before native playback.')
        self.path, self.duration_seconds, self._identity = target, duration, identity
        self._offset = self._play_start = float(start_seconds)
        self._end_seconds = None if end_seconds is None else float(end_seconds)
        self._ended = False
        if os.name == 'nt':
            self._mci(f'open "{target}" type waveaudio alias {self._alias}')
            self._open = True
            try:
                self._mci(f'set {self._alias} time format milliseconds')
                self._mci(self._play_command(start_seconds))
            except Exception:
                self.close()
                raise
        else:
            limit = [] if end_seconds is None else ['-t', str(float(end_seconds - start_seconds))]
            self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                '-nodisp', '-autoexit', '-nostats', '-ss', str(float(start_seconds)),
                '-protocol_whitelist', 'file,pipe', '-i', str(target), *limit],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, shell=False, env=native_audio_environment())
        self._started = time.monotonic()
        self._state = 'playing'

    def _restart(self, seconds, end_seconds=None):
        """Restart the same admitted file while retaining this session's controls."""
        if self.path is None:
            raise ValueError('Open a recording before controlling playback.')
        if self._sleep_deadline is not None and time.monotonic() >= self._sleep_deadline:
            self.stop()
            return False
        path, identity = self.path, self._identity
        if identity is not None and self._stamp(path) != identity:
            self.stop()
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        loop, sleep = self._ab, self._sleep_deadline
        self._repeat_armed = False
        self._play(path, start_seconds=seconds, end_seconds=end_seconds, expected=identity)
        self._ab, self._sleep_deadline = loop, sleep
        self._repeat_armed = loop is not None
        return True

    def play_current(self):
        """An explicit replay/resume action; setting controls alone never calls it."""
        if self.path is None:
            raise ValueError('Read text once before playing its current recording.')
        state = self.state
        if state == 'playing':
            return False
        if state == 'paused':
            return self.resume()
        position = self._offset
        if self._ab is not None:
            first, last = self._ab
            position = position if first <= position < last else first
        else:
            last = None
            position = position if 0 <= position < self.duration_seconds else 0
        return self._restart(position, last)

    def _silent_position(self, seconds, *, paused):
        if os.name == 'nt' and self._open:
            self._mci(f'seek {self._alias} to {round(seconds * 1000)} wait')
        elif self._process is not None:
            # A POSIX paused seek must not play then SIGSTOP a fresh child.
            stop_process(self._process)
            self._process = None
        self._offset, self._started = float(seconds), None
        self._state = 'paused' if paused else 'stopped'
        self._resume_from_seek = True
        self._repeat_armed = False
        self._ended = False

    def pause(self):
        state = self.state
        if state == 'stopped' and self._ended and self._repeat_armed and self._ab is not None:
            # Pause between native B and the next UI tick must suppress repeat.
            self._silent_position(self._ab[0], paused=True)
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
        self._repeat_armed = False
        self._ended = False
        return True

    def resume(self):
        if self.state != 'paused':
            return False
        if self._sleep_deadline is not None and time.monotonic() >= self._sleep_deadline:
            self.stop()
            return False
        if self._identity is not None and self._stamp(self.path) != self._identity:
            self.stop()
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        if os.name == 'nt':
            if self._resume_from_seek:
                self._mci(self._play_command(self._offset))
            else:
                self._mci(f'resume {self._alias}')
        elif self._resume_from_seek:
            return self._restart(self._offset, self._end_seconds)
        else:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = time.monotonic()
        self._state = 'playing'
        self._play_start = self._offset
        self._resume_from_seek = False
        self._repeat_armed = self._ab is not None
        return True

    def seek(self, seconds, *, start_if_stopped=True):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        if (type(seconds) not in (float, int) or not 0 <= seconds <= self.duration_seconds
                or type(start_if_stopped) is not bool):
            raise ValueError('Choose a playback position within the recording.')
        if self._ab is not None and not self._ab[0] <= seconds < self._ab[1]:
            raise ValueError('Choose a position inside A–B and before B, or clear repeat first.')
        state = self.state
        if state == 'paused' or state != 'playing' and not start_if_stopped:
            self._silent_position(seconds, paused=state == 'paused')
            return
        self._restart(seconds, self._end_seconds)

    def skip(self, seconds):
        if self.path is None or type(seconds) not in (float, int) or not -600 <= seconds <= 600:
            raise ValueError('Choose a skip of at most ten minutes within the current recording.')
        first, last = self._ab or (0, self.duration_seconds)
        # B is an end boundary, never a valid repeat start.
        upper = max(first, last - .001) if self._ab is not None else last
        target = min(upper, max(first, self.position_seconds + seconds))
        self.seek(target, start_if_stopped=False)
        return target

    @property
    def skip_intervals(self):
        return self._skip_intervals

    def set_skip_intervals(self, backward, forward):
        if (any(type(value) not in (float, int) for value in (backward, forward))
                or not all(.1 <= value <= 600 for value in (backward, forward))):
            raise ValueError('Choose backward and forward skips from 0.1 to 600 seconds.')
        self._skip_intervals = float(backward), float(forward)
        return self._skip_intervals

    def skip_backward(self):
        return self.skip(-self._skip_intervals[0])

    def skip_forward(self):
        return self.skip(self._skip_intervals[1])

    @property
    def ab_loop(self):
        return self._ab

    def set_ab_loop(self, first, last):
        if (self.path is None or any(type(value) not in (float, int) for value in (first, last))
                or not 0 <= first < last <= self.duration_seconds or last - first + 1e-9 < .05):
            raise ValueError('Choose A and B inside the current recording, at least 0.05 seconds apart.')
        state, position = self.state, self.position_seconds
        loop = float(first), float(last)
        target = position if first <= position < last else first
        self._ab, self._end_seconds = loop, loop[1]
        self._ended = False
        if state == 'playing':
            self._restart(target, loop[1])
        else:
            self._silent_position(target, paused=state == 'paused')
        return loop

    def clear_ab_loop(self):
        state, position = self.state, self.position_seconds
        self._ab, self._end_seconds = None, None
        self._repeat_armed = self._ended = False
        if state == 'playing':
            self._restart(position)
        elif self.path is not None:
            self._silent_position(position, paused=state == 'paused')

    def set_sleep_timer(self, seconds=None):
        if seconds is None:
            self._sleep_deadline = None
        elif (self.path is None or type(seconds) not in (float, int)
                or not 0 < seconds <= 7 * 86400):
            raise ValueError('Open a recording and choose a sleep timer from zero to seven days.')
        else:
            self._sleep_deadline = time.monotonic() + float(seconds)

    @property
    def sleep_remaining_seconds(self):
        return None if self._sleep_deadline is None else max(0, self._sleep_deadline - time.monotonic())

    @property
    def repeat_pending(self):
        return bool(self._ab is not None and self._repeat_armed and self.state == 'stopped' and self._ended)

    def repeat_if_ready(self):
        if self._sleep_deadline is not None and time.monotonic() >= self._sleep_deadline:
            self.stop()
            return False
        if not self.repeat_pending:
            return False
        first, last = self._ab
        self._repeat_armed = False
        return self._restart(first, last)

    def poll(self):
        """Caller-owned timer: stop expired sleep, or report a pending repeat.

        This method never starts audio. The UI admits repeat through its existing
        quiet/progress and source-lifetime guards before repeat_if_ready().
        """
        if self._sleep_deadline is not None and time.monotonic() >= self._sleep_deadline:
            self.stop()
            return 'sleep'
        return 'repeat-pending' if self.repeat_pending else None

    def stop(self):
        # Invalidate automatic work before touching a possibly failing device.
        self._repeat_armed = self._ended = False
        self._sleep_deadline = None
        if os.name == 'nt' and self._open:
            self._mci(f'stop {self._alias}')
        elif self._process is not None:
            stop_process(self._process)
            self._process = None
        self._state, self._offset, self._started = 'stopped', 0.0, None
        self._resume_from_seek = False

    def close(self):
        stop_error = None
        try:
            self.stop()
        except (OSError, RuntimeError) as exc:
            stop_error = exc
        if os.name == 'nt' and self._open:
            # A failed native close retains the alias and retry authority.
            # Successful close establishes silence even if Stop itself failed.
            self._mci(f'close {self._alias}')
            self._open = False
            self._state, self._offset, self._started = 'stopped', 0.0, None
            self._resume_from_seek = False
        elif stop_error is not None:
            raise stop_error
        self._ab, self._end_seconds = None, None
