"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate.
"""
import ctypes
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from uuid import uuid4
from .engines import _stop as stop_process
from .ffmpeg import find_local_tool, wav_info
from .model_manifest import local_path


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
        self._silence_confirmed = True
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = find_local_tool('ffplay') if os.name != 'nt' else None

    @property
    def silence_confirmed(self):
        """Whether this instance has confirmed that its own output is stopped.

        Closing a dialog remains best effort after device removal. Recording
        adapters must inspect this separately instead of treating Python state
        reset as proof that an unsuccessful native stop actually silenced audio.
        """
        return self._silence_confirmed

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
                    self._silence_confirmed = True
            elif self._process is not None and self._process.poll() is not None:
                self._silence_confirmed = True
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
        target = local_path(path)
        # The production reader handles RIFF and RF64, including multi-hour
        # PCM files, without loading or decoding their waveform into memory.
        duration = wav_info(target).seconds
        if (type(start_seconds) not in (float, int) or not math.isfinite(start_seconds)
                or not 0 <= start_seconds <= duration):
            raise ValueError('Choose a playback position within the recording.')
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self.close()
        if not self.silence_confirmed:
            raise RuntimeError('Previous playback could not be confirmed stopped; reconnect the audio device.')
        self.path, self.duration_seconds = target, duration
        self._offset = float(start_seconds)
        if os.name == 'nt':
            self._mci(f'open "{target}" type waveaudio alias {self._alias}')
            self._open = True
            try:
                self._mci(f'set {self._alias} time format milliseconds')
                self._silence_confirmed = False
                self._mci(f'play {self._alias} from {round(start_seconds * 1000)}')
            except Exception:
                self.close()
                raise
        else:
            self._silence_confirmed = False
            self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                '-nodisp', '-autoexit', '-nostats', '-ss', str(float(start_seconds)),
                '-protocol_whitelist', 'file,pipe', '-i', str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, shell=False)
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
            self._mci(f'resume {self._alias}')
        else:
            if self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = time.monotonic()
        self._state = 'playing'
        return True

    def seek(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        paused = self.state == 'paused'
        self.play(self.path, start_seconds=seconds)
        if paused:
            self.pause()

    def close(self):
        try:
            self.stop()
        except (OSError, RuntimeError):
            pass  # A removed device must not prevent closing the Studio.
        finally:
            if os.name == 'nt' and self._open:
                try:
                    self._mci(f'close {self._alias}')
                    self._silence_confirmed = True
                except (OSError, RuntimeError):
                    self._silence_confirmed = False
                    pass
                finally:
                    self._open = False
            self._state, self._offset, self._started = 'stopped', 0.0, None

    def stop(self):
        if os.name == 'nt' and self._open:
            self._silence_confirmed = False
            self._mci(f'stop {self._alias}')
            self._silence_confirmed = True
        elif self._process is not None:
            self._silence_confirmed = False
            stop_process(self._process)
            self._process = None
            self._silence_confirmed = True
        self._state, self._offset, self._started = 'stopped', 0.0, None
