"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate. Optional
listening copies reuse the existing bounded FFmpeg converter; explicit Windows
levels reuse an owned WinMM handle and never an output-device ordinal.
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
    def __init__(self, *, output_device=None):
        from ..audio.output_devices import OutputDevice
        if output_device is not None and type(output_device) is not OutputDevice:
            raise ValueError('Choose a device from the Windows output list.')
        self.output_device = output_device
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
        self._ended = False
        self._listening = None
        self._listening_request = None
        self._wave = None
        self._live_controls = False
        self._levels = None
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = shutil.which('ffplay') if os.name != 'nt' else None

    @property
    def available(self):
        return os.name == 'nt' or bool(self._executable)

    def select_output(self, selected):
        from ..audio.output_devices import OutputDevice
        if selected is not None and type(selected) is not OutputDevice:
            raise ValueError('Choose a device from the Windows output list.')
        if self._open or self._process is not None or self._wave is not None:
            raise ValueError('Close the current playback device before changing its output.')
        self.output_device = selected

    @property
    def live_mix_available(self):
        return os.name == 'nt'

    @property
    def levels(self):
        from ._wave_output import PlaybackLevels
        return self._levels if self._levels is not None else PlaybackLevels()

    def set_levels(self, volume, *, muted=False, balance=0):
        """Apply per-instance controls; never change a system/device-wide mixer."""
        from ._wave_output import PlaybackLevels, WaveOutput
        levels = PlaybackLevels(volume, muted, balance)
        if not self.live_mix_available:
            raise RuntimeError('Live volume, mute and balance require the Windows owned-output backend.')
        if self._wave is not None:
            try:
                self._wave.set_levels(levels)
            except BaseException:
                self._close_transport()
                raise
            self._levels = levels
            return levels
        state = self.state
        if state not in ('playing', 'paused'):
            # Retain a session request only. No device open and no autoplay.
            self._levels, self._live_controls = levels, True
            return levels
        position = self.position_seconds
        path, identity = self.path, self._identity
        end = self._end_seconds
        if identity is None or self._stamp(path) != identity:
            self.stop()
            raise ValueError('The current recording changed. Select it again.')
        self._check_listening()
        self._close_transport()  # Old MCI alias must acknowledge closure first.
        if self._stamp(path) != identity:
            raise ValueError('The recording changed before switching playback controls.')
        self._wave = WaveOutput()
        try:
            self._wave.open(self.listening_path,
                start_seconds=self._to_transport(position),
                end_seconds=None if end is None else self._to_transport(end),
                device=self.output_device, levels=levels, paused=state == 'paused',
                expected=identity if self._listening is None else self._stamp(self.listening_path))
        except BaseException:
            self._close_transport()
            raise
        self._levels, self._live_controls = levels, True
        self.backend = 'Windows waveOut'
        self._state, self._offset = state, position
        self._started = time.monotonic() if state == 'playing' else None
        self._play_start = position
        self._end_seconds = end
        return levels

    @property
    def listening_settings(self):
        from .listening import ListeningSettings
        return self._listening.request.settings if self._listening is not None else ListeningSettings()

    @property
    def listening_duration_seconds(self):
        return self._listening.mapping.output_duration if self._listening is not None else self.duration_seconds

    @property
    def listening_path(self):
        return self._listening.path if self._listening is not None else self.path

    def listening_request(self, speed, pitch_semitones=0, *, position_seconds=0):
        """Freeze this stopped recording's identity; preparation belongs to its worker."""
        from .listening import ListeningRequest, ListeningSettings
        settings = ListeningSettings(speed, pitch_semitones)
        if self.path is None or self.state in ('playing', 'paused'):
            raise ValueError('Stop the current recording before preparing listening settings.')
        if self._identity is None or self._stamp(self.path) != self._identity:
            raise ValueError('The current original recording changed; render or select it again.')
        if (type(position_seconds) not in (float, int) or not math.isfinite(position_seconds) or not 0 <= position_seconds <= self.duration_seconds):
            raise ValueError('Choose a resume position within the original recording.')
        request = ListeningRequest(self.path, self._identity, settings, float(position_seconds), object())
        self._listening_request = request
        return request

    def prepare_listening(self, request, directory, *, state_root=None, cancelled=lambda: False,
                          runtime_factory=None, budget=None, broker=None):
        """Heavy work only: no native transport call and no playback admission."""
        from .listening import prepare_listening
        options = {} if budget is None else {'budget': budget}
        return prepare_listening(request, directory, state_root=state_root,
            cancelled=lambda: cancelled() or self._listening_request is not request,
            runtime_factory=runtime_factory, broker=broker, **options)

    def apply_listening(self, prepared):
        """Adopt a worker result on the transport owner; never autoplay."""
        from .listening import PreparedListening
        if (not isinstance(prepared, PreparedListening)
                or self._listening_request is not prepared.request
                or self.path != prepared.request.source or self._identity != prepared.request.identity
                or self.state in ('playing', 'paused')):
            raise ValueError('The listening request is stale or playback resumed; prepare it again.')
        prepared.check_live()
        self._close_transport()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
        prepared.claim(self)
        self._listening = prepared
        self._offset = prepared.request.position_seconds
        self._state, self._started = 'stopped', None
        self._end_seconds = None

    def _to_transport(self, seconds):
        return self._listening.mapping.to_output(seconds) if self._listening is not None else seconds

    def _from_transport(self, seconds):
        return self._listening.mapping.to_source(seconds) if self._listening is not None else seconds

    def _check_listening(self):
        if self._listening is not None:
            self._listening.check_live()

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

    def _confirm_output(self, selected):
        from ..audio.output_devices import confirm_output_index
        index = int(self._mci(f'status {self._alias} output'))
        confirm_output_index(selected, index)

    @property
    def state(self):
        if self._state == 'playing':
            end = self.duration_seconds if self._end_seconds is None else self._end_seconds
            if self._wave is not None:
                try:
                    self._wave.pump()
                    if self._wave.ended:
                        self._state = 'stopped'
                        self._offset = end
                        self._ended = self._play_start < end
                        self._started = None
                except BaseException:
                    self._ended = False
                    self._close_transport()
                    self._state = 'error'
                    raise
            elif os.name == 'nt' and self._open:
                mode = self._mci(f'status {self._alias} mode')
                if mode == 'stopped':
                    position = self._from_transport(int(self._mci(f'status {self._alias} position')) / 1000)
                    precision = self._from_transport(.002)
                    self._ended = self._play_start < end and position >= end - precision
                    self._state = 'stopped'
                    self._offset = min(self.duration_seconds, max(0, position))
                    self._started = None
            elif self._process is not None and self._process.poll() is not None:
                if self._process.returncode:
                    self._state = 'error'
                    self._ended = False
                else:
                    self._state = 'stopped'
                    self._offset = end
                    self._ended = self._play_start < end
                self._started = None
        return self._state

    @property
    def position_seconds(self):
        if self._wave is not None:
            return self._from_transport(self._wave.position_seconds)
        if os.name == 'nt' and self._open:
            return self._from_transport(int(self._mci(f'status {self._alias} position')) / 1000)
        if self.state == 'playing' and self._started is not None:
            end = self.duration_seconds if self._end_seconds is None else self._end_seconds
            return min(end, self._offset + self._from_transport(time.monotonic() - self._started))
        return self._offset

    @staticmethod
    def _stamp(path):
        info = local_file(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _play_command(self, seconds):
        command = f'play {self._alias} from {round(self._to_transport(seconds) * 1000)}'
        if self._end_seconds is not None:
            command += f' to {round(self._to_transport(self._end_seconds) * 1000)}'
        return command

    def open(self, path):
        """Select checked PCM silently; no native device is opened or started."""
        from .listening import _opened
        target = local_file(path)
        identity = self._stamp(target)
        with _opened(target, identity) as stream:
            with wave.open(stream, 'rb') as audio:
                if audio.getcomptype() != 'NONE' or audio.getnframes() <= 0:
                    raise ValueError('Playback requires nonempty PCM WAV audio.')
                duration = audio.getnframes() / audio.getframerate()
        self.close()
        if self._stamp(target) != identity:
            raise ValueError('The recording changed before silent playback selection.')
        self.path, self.duration_seconds, self._identity = target, duration, identity
        self._end_seconds, self._ended = None, False
        return self

    def play(self, path, *, start_seconds=0, end_seconds=None):
        """Explicitly play a new recording; prior private listening preparation is released."""
        return self._play(path, start_seconds=start_seconds, end_seconds=end_seconds, new_recording=True)

    def _play(self, path, *, start_seconds=0, end_seconds=None, expected=None, new_recording=False):
        target = local_file(path)
        identity = self._stamp(target)
        if expected is not None and identity != expected:
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        if not new_recording:
            self._check_listening()
        from .listening import _opened
        with _opened(target, identity) as stream:
            with wave.open(stream, 'rb') as audio:
                if audio.getcomptype() != 'NONE' or audio.getnframes() <= 0:
                    raise ValueError('Playback requires nonempty PCM WAV audio.')
                duration = audio.getnframes() / audio.getframerate()
        if (type(start_seconds) not in (float, int) or not math.isfinite(start_seconds) or not 0 <= start_seconds <= duration):
            raise ValueError('Choose a playback position within the recording.')
        if (end_seconds is not None and (type(end_seconds) not in (float, int)
                or not math.isfinite(end_seconds) or not start_seconds < end_seconds <= duration)):
            raise ValueError('Choose a playback end after its start and inside the recording.')
        if self._stamp(target) != identity:
            raise ValueError('The recording changed while its PCM header was read.')
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        output = None
        if self.output_device is not None:
            from ..audio.output_devices import resolve_output_device
            output = resolve_output_device(self.output_device)
        if new_recording:
            self.close()
        else:
            self._close_transport()
        if self._stamp(target) != identity:
            raise ValueError('The recording changed before native playback.')
        self._check_listening()
        self.path, self.duration_seconds, self._identity = target, duration, identity
        self._offset = self._play_start = float(start_seconds)
        self._end_seconds = None if end_seconds is None else float(end_seconds)
        self._ended = False
        actual = target if self._listening is None else self._listening.path
        if os.name == 'nt' and self._live_controls:
            from ._wave_output import WaveOutput
            self._wave = WaveOutput()
            try:
                self._wave.open(actual, start_seconds=self._to_transport(start_seconds),
                    end_seconds=None if end_seconds is None else self._to_transport(end_seconds),
                    device=output, levels=self.levels,
                    expected=identity if self._listening is None else self._stamp(actual))
                self.backend = 'Windows waveOut'
            except BaseException:
                self._close_transport()
                raise
        elif os.name == 'nt':
            self.backend = 'Windows MCI'
            self._mci(f'open "{actual}" type waveaudio alias {self._alias}')
            self._open = True
            try:
                if output is not None:
                    self._mci(f'set {self._alias} output {output.index}')
                    self._confirm_output(output)
                self._mci(f'set {self._alias} time format milliseconds')
                self._mci(self._play_command(start_seconds))
            except Exception:
                self._close_transport()
                raise
        else:
            limit = [] if end_seconds is None else ['-t', str(float(self._to_transport(end_seconds - start_seconds)))]
            self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                '-nodisp', '-autoexit', '-nostats', '-ss', str(float(self._to_transport(start_seconds))),
                '-protocol_whitelist', 'file,pipe', '-i', str(actual), *limit],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, shell=False, env=native_audio_environment())
        self._started = time.monotonic()
        self._state = 'playing'

    def _restart(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before controlling playback.')
        path, identity, end = self.path, self._identity, self._end_seconds
        if identity is None or self._stamp(path) != identity:
            self.stop()
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        self._play(path, start_seconds=seconds, end_seconds=end, expected=identity)
        return True

    def play_current(self):
        """Explicit playback of the current prepared recording; adoption stays silent."""
        if self.path is None:
            raise ValueError('Open a recording before playing its current listening copy.')
        state = self.state
        if state == 'playing':
            return False
        if state == 'paused':
            return self.resume()
        end = self.duration_seconds if self._end_seconds is None else self._end_seconds
        position = self._offset if 0 <= self._offset < end else 0
        return self._restart(position)

    def _silent_position(self, seconds, *, paused):
        if self._wave is not None:
            # A paused seek never opens/starts a replacement native stream.
            self._wave.close()
            self._wave = None
        elif os.name == 'nt' and self._open:
            self._mci(f'seek {self._alias} to {round(self._to_transport(seconds) * 1000)} wait')
        elif self._process is not None:
            # A POSIX paused seek must not play then SIGSTOP a fresh child.
            stop_process(self._process)
            self._process = None
        self._offset, self._started = float(seconds), None
        self._state = 'paused' if paused else 'stopped'
        self._resume_from_seek = True
        self._ended = False

    def pause(self):
        if self.state != 'playing':
            return False
        position = self.position_seconds
        if self._wave is not None:
            self._wave.pause()
        elif os.name == 'nt':
            self._mci(f'pause {self._alias}')
        else:
            os.kill(self._process.pid, signal.SIGSTOP)
        self._offset = position
        self._state = 'paused'
        return True

    def resume(self):
        if self.state != 'paused':
            return False
        if self._identity is not None and self._stamp(self.path) != self._identity:
            self.stop()
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        self._check_listening()
        if self._wave is not None:
            self._wave.resume()
        elif self._resume_from_seek and (os.name != 'nt' or self._live_controls):
            return self._restart(self._offset)
        elif os.name == 'nt':
            if self.output_device is not None:
                self._confirm_output(self.output_device)
            if self._resume_from_seek:
                self._mci(self._play_command(self._offset))
            else:
                self._mci(f'resume {self._alias}')
        else:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = time.monotonic()
        self._state = 'playing'
        self._play_start = self._offset
        self._resume_from_seek = False
        return True

    def seek(self, seconds, *, start_if_stopped=True):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        end = self.duration_seconds if self._end_seconds is None else self._end_seconds
        if (type(seconds) not in (float, int) or not math.isfinite(seconds)
                or not 0 <= seconds <= end or type(start_if_stopped) is not bool):
            raise ValueError('Choose a playback position within the recording.')
        state = self.state
        if state == 'paused' or state != 'playing' and not start_if_stopped:
            self._silent_position(seconds, paused=state == 'paused')
            return
        self._restart(seconds)

    def stop(self):
        self._listening_request = None
        self._ended = False
        if self._wave is not None:
            self._wave.close()
            self._wave = None
        elif os.name == 'nt' and self._open:
            self._mci(f'stop {self._alias}')
        elif self._process is not None:
            stop_process(self._process)
            self._process = None
        self._state, self._offset, self._started = 'stopped', 0.0, None
        self._resume_from_seek = False

    def _close_transport(self):
        stop_error = None
        try:
            self.stop()
        except BaseException as exc:
            stop_error = exc
        try:
            if os.name == 'nt' and self._open:
                self._mci(f'close {self._alias}')
                # Retain the native alias until the device confirms closure.
                # A failed close must remain retryable by its existing owner.
                self._open = False
        except BaseException as exc:
            if stop_error is not None:
                raise stop_error from exc
            raise
        if stop_error is not None:
            raise stop_error
        return True

    def close(self):
        self._close_transport()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
        return True
