"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate.
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
            if os.name == 'nt' and self._open:
                mode = self._mci(f'status {self._alias} mode')
                if mode == 'stopped':
                    self._state = 'stopped'
                    self._offset = self.duration_seconds
            elif self._process is not None and self._process.poll() is not None:
                if self._process.returncode:
                    self._state = 'error'
                else:
                    self._state = 'stopped'
                    self._offset = self.duration_seconds
        return self._state

    @property
    def position_seconds(self):
        if os.name == 'nt' and self._open:
            return int(self._mci(f'status {self._alias} position')) / 1000
        if self.state == 'playing' and self._started is not None:
            return min(self.duration_seconds, self._offset + time.monotonic() - self._started)
        return self._offset

    def play(self, path, *, start_seconds=0):
        target = local_file(path)
        with wave.open(str(target), 'rb') as audio:
            if audio.getcomptype() != 'NONE' or audio.getnframes() <= 0:
                raise ValueError('Playback requires nonempty PCM WAV audio.')
            duration = audio.getnframes() / audio.getframerate()
        if (type(start_seconds) not in (float, int) or not math.isfinite(start_seconds)
                or not 0 <= start_seconds <= duration):
            raise ValueError('Choose a playback position within the recording.')
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self.close()
        self.path, self.duration_seconds = target, duration
        self._offset = float(start_seconds)
        if os.name == 'nt':
            self._mci(f'open "{target}" type waveaudio alias {self._alias}')
            self._open = True
            try:
                self._mci(f'set {self._alias} time format milliseconds')
                self._mci(f'play {self._alias} from {round(start_seconds * 1000)}')
            except Exception:
                self.close()
                raise
        else:
            self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                '-nodisp', '-autoexit', '-nostats', '-ss', str(float(start_seconds)),
                '-protocol_whitelist', 'file,pipe', '-i', str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, shell=False, env=native_audio_environment())
        self._started = time.monotonic()
        self._state = 'playing'

    def pause(self):
        if self.state != 'playing':
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
        if os.name == 'nt':
            if self._resume_from_seek:
                self._mci(f'play {self._alias} from {round(self._offset * 1000)}')
            else:
                self._mci(f'resume {self._alias}')
        else:
            if self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = time.monotonic()
        self._state = 'playing'
        self._resume_from_seek = False
        return True

    def seek(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        if (type(seconds) not in (float, int) or not 0 <= seconds <= self.duration_seconds):
            raise ValueError('Choose a playback position within the recording.')
        paused = self.state == 'paused'
        if paused and os.name == 'nt' and self._open:
            # MCI seek moves silently and leaves the native device stopped.
            # Keep the logical pause until an explicit resume starts playback.
            # Reopening with play followed by pause can leak a short burst.
            self._mci(f'seek {self._alias} to {round(seconds * 1000)} wait')
            self._offset, self._started = float(seconds), None
            self._resume_from_seek = True
            return
        self.play(self.path, start_seconds=seconds)
        if paused:
            self.pause()

    def stop(self):
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
