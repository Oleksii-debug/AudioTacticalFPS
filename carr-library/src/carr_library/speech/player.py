"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured position and native pause/seek. Explicit live
controls use an owned waveOut handle in this same player. The POSIX ffplay
fallback is explicit; elapsed position there is only a clock estimate.
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
        self._output_device = None
        self._opened_output = None
        self._resume_from_seek = False
        self._identity = None
        self._end_seconds = None
        self._play_start = 0.0
        self._ab = None
        self._repeat_armed = False
        self._ended = False
        self._sleep_deadline = None
        self._skip_intervals = (10.0, 30.0)
        self._listening = None
        self._listening_request = None
        self._wave = None
        self._live_controls = False
        self._levels = None
        self._playlist = ()
        self._playlist_index = None
        self._playlist_armed = False
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = shutil.which('ffplay') if os.name != 'nt' else None

    @property
    def available(self):
        return os.name == 'nt' or bool(self._executable)

    @property
    def output_device(self):
        """Explicit session choice; None retains the backend's automatic output."""
        return self._output_device

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
        end, loop, sleep = self._end_seconds, self._ab, self._sleep_deadline
        if sleep is not None and time.monotonic() >= sleep:
            self.stop()
            self._levels, self._live_controls = levels, True
            return levels
        queued = self._playlist_armed
        if identity is None or self._stamp(path) != identity:
            self.stop()
            raise ValueError('The current recording changed. Select it again.')
        self._check_listening()
        self._close_transport()  # Old MCI alias must acknowledge closure first.
        if self._stamp(path) != identity:
            raise ValueError('The recording changed before switching playback controls.')
        self._wave = WaveOutput(owner=self)
        try:
            self._wave.open(self.listening_path,
                start_seconds=self._to_transport(position),
                end_seconds=None if end is None else self._to_transport(end),
                device=self._output_device, levels=levels, paused=state == 'paused',
                expected=identity if self._listening is None else self._listening.identity)
        except BaseException:
            self._close_transport()
            raise
        self._levels, self._live_controls = levels, True
        self.backend = 'Windows waveOut'
        self._state, self._offset = state, position
        self._started = time.monotonic() if state == 'playing' else None
        self._play_start = position
        self._end_seconds, self._ab, self._sleep_deadline = end, loop, sleep
        self._repeat_armed = state == 'playing' and loop is not None
        self._playlist_armed = queued
        return levels

    @staticmethod
    def prepare_playlist(paths, *, cancelled=lambda: False):
        from .playlist import prepare_playlist
        return prepare_playlist(paths, cancelled=cancelled)

    @property
    def playlist(self):
        return self._playlist

    @property
    def playlist_index(self):
        return self._playlist_index

    @property
    def queue_active(self):
        return self._playlist_armed

    def set_playlist(self, items):
        """Adopt reviewed entries in order; replacement stops and never plays."""
        from .playlist import MAX_ITEMS, PlaylistItem
        if (not isinstance(items, (list, tuple)) or len(items) > MAX_ITEMS
                or any(not isinstance(item, PlaylistItem) for item in items)):
            raise ValueError('Use a bounded, explicitly reviewed session playlist.')
        for item in items:
            if self._stamp(item.path) != item.identity:
                raise ValueError('A reviewed playlist file changed. Review the selection again.')
        self.close()
        self._playlist, self._playlist_index = tuple(items), None
        self.path, self._identity, self.duration_seconds = None, None, 0

    def select_queue(self, index):
        """Select silently. A later explicit Play or Play queue admits audio."""
        if type(index) is not int or not 0 <= index < len(self._playlist):
            raise ValueError('Choose an existing playlist item.')
        item = self._playlist[index]
        if self._stamp(item.path) != item.identity:
            self.stop()
            raise ValueError('The selected playlist file changed. Review it again.')
        self.close()
        self.path, self._identity = item.path, item.identity
        self.duration_seconds, self._playlist_index = item.duration_seconds, index

    def play_queue(self, index=None):
        """Explicitly play this item and arm later, separately admitted transitions."""
        index = self._playlist_index if index is None else index
        if index is None:
            index = 0
        if type(index) is not int or not 0 <= index < len(self._playlist):
            raise ValueError('Choose an item in a reviewed playlist.')
        item = self._playlist[index]
        if self._playlist_index == index and self.path == item.path and self._identity == item.identity:
            state = self.state
            if state == 'paused':
                resumed = self.resume()
                self._playlist_armed = bool(resumed)
                return resumed
            if state == 'playing':
                if self._stamp(item.path) != item.identity:
                    self.stop()
                    raise ValueError('The current playlist file changed. Review it again.')
                self._playlist_armed = True
                return False
            started = self.play_current()
            self._playlist_armed = bool(started)
            return started
        return self._play_queue_item(index)

    def _play_queue_item(self, index):
        item = self._playlist[index]
        sleep = self._sleep_deadline
        if sleep is not None and time.monotonic() >= sleep:
            self.stop()
            return False
        self._playlist_armed = False
        try:
            self._play(item.path, expected=item.identity, new_recording=True)
        except BaseException:
            # Validation can fail before the old native handle has closed.
            self.stop()
            raise
        self._playlist_index, self._playlist_armed = index, True
        self._sleep_deadline = sleep
        return True

    @property
    def queue_pending(self):
        return bool(self._playlist_armed and self._ab is None
            and self.state == 'stopped' and self._ended
            and self._playlist_index is not None
            and self._playlist_index + 1 < len(self._playlist))

    def advance_if_ready(self):
        """UI admission only. poll() itself never opens or starts the next file."""
        if self._sleep_deadline is not None and time.monotonic() >= self._sleep_deadline:
            self.stop()
            return False
        if not self.queue_pending:
            return False
        return self._play_queue_item(self._playlist_index + 1)

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
        if (type(position_seconds) not in (float, int) or not 0 <= position_seconds <= self.duration_seconds):
            raise ValueError('Choose a resume position within the original recording.')
        request = ListeningRequest(self.path, self._identity, settings, float(position_seconds), object())
        self._listening_request = request
        return request

    def prepare_listening(self, request, directory, *, state_root=None, cancelled=lambda: False,
                          runtime_factory=None):
        """Heavy work only: no native transport call and no playback admission."""
        from .listening import prepare_listening
        return prepare_listening(request, directory, state_root=state_root,
            cancelled=lambda: cancelled() or self._listening_request is not request,
            runtime_factory=runtime_factory)

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
        self._ab, self._end_seconds = None, None

    def _to_transport(self, seconds):
        return self._listening.mapping.to_output(seconds) if self._listening is not None else seconds

    def _from_transport(self, seconds):
        return self._listening.mapping.to_source(seconds) if self._listening is not None else seconds

    def _check_listening(self):
        if self._listening is not None:
            self._listening.check_live()

    def output_devices(self, *, cancelled=lambda: False):
        from .playback_devices import output_devices
        return output_devices(cancelled=cancelled)

    def select_output(self, device):
        """Select the next route only after explicit Stop; never start audio."""
        if self.state in ('playing', 'paused'):
            raise ValueError('Stop playback before selecting a different output.')
        if device is not None:
            from .playback_devices import resolve_output
            device = resolve_output(device)
        # Preserve the old choice and retry authority if native Close fails.
        self._close_transport()
        self._output_device = device

    def _confirm_output(self, output):
        if self._mci(f'status {self._alias} output').strip() != str(output.index):
            raise RuntimeError('Windows did not confirm the selected playback output.')
        from .playback_devices import resolve_output
        if resolve_output(output).index != output.index:
            raise RuntimeError('The selected output moved during playback admission. Retry explicitly.')

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
            if self._wave is not None:
                try:
                    self._wave.pump()
                    if self._wave.ended:
                        self._state = 'stopped'
                        self._offset = end
                        self._ended = self._play_start < end
                        self._started = None
                except BaseException:
                    self._playlist_armed = self._repeat_armed = self._ended = False
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
                    self._repeat_armed = False
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

    def play(self, path, *, start_seconds=0, end_seconds=None):
        """Explicitly play a new recording; prior repeat/sleep choices are reset."""
        return self._play(path, start_seconds=start_seconds, end_seconds=end_seconds, new_recording=True)

    def _play(self, path, *, start_seconds=0, end_seconds=None, expected=None, new_recording=False):
        target = local_file(path)
        identity = self._stamp(target)
        if expected is not None and identity != expected:
            raise ValueError('The opened recording changed. Select its current version explicitly.')
        if not new_recording:
            self._check_listening()
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
        output = None
        if self._output_device is not None:
            from .playback_devices import resolve_output
            output = resolve_output(self._output_device)
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
            self._wave = WaveOutput(owner=self)
            try:
                self._wave.open(actual, start_seconds=self._to_transport(start_seconds),
                    end_seconds=None if end_seconds is None else self._to_transport(end_seconds),
                    device=output, levels=self.levels,
                    expected=identity if self._listening is None else self._listening.identity)
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
                    self._opened_output = output
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
        loop, sleep, queued = self._ab, self._sleep_deadline, self._playlist_armed
        self._repeat_armed = False
        self._play(path, start_seconds=seconds, end_seconds=end_seconds, expected=identity)
        self._ab, self._sleep_deadline = loop, sleep
        self._repeat_armed = loop is not None
        self._playlist_armed = queued
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
        self._repeat_armed = False
        self._ended = False

    def pause(self):
        self._playlist_armed = False
        state = self.state
        if state == 'stopped' and self._ended and self._repeat_armed and self._ab is not None:
            # Pause between native B and the next UI tick must suppress repeat.
            self._silent_position(self._ab[0], paused=True)
            return True
        if state != 'playing':
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
        self._check_listening()
        if self._wave is not None:
            try:
                self._wave.resume()
            except BaseException:
                self._close_transport()
                raise
        elif os.name == 'nt' and self._live_controls:
            return self._restart(self._offset, self._end_seconds)
        elif os.name == 'nt':
            if self._output_device is not None:
                try:
                    if not self._open or self._opened_output is None:
                        raise RuntimeError('The selected playback route is no longer open. Play explicitly again.')
                    self._confirm_output(self._opened_output)
                except Exception:
                    # A paused route must never be resumed through a replacement
                    # ordinal or silently fall back. Close retains retry authority.
                    self.close()
                    raise
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
        if self.repeat_pending:
            return 'repeat-pending'
        if self.queue_pending:
            return 'queue-pending'
        if self._ended and self._playlist_index == len(self._playlist) - 1:
            self._playlist_armed = False
        return None

    def stop(self):
        # Invalidate automatic work before touching a possibly failing device.
        self._listening_request = None
        self._repeat_armed = self._ended = self._playlist_armed = False
        self._sleep_deadline = None
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
        except (OSError, RuntimeError) as exc:
            stop_error = exc
        if os.name == 'nt' and self._open:
            # A failed native close retains the alias and retry authority.
            # Successful close establishes silence even if Stop itself failed.
            self._mci(f'close {self._alias}')
            self._open = False
            self._opened_output = None
            self._state, self._offset, self._started = 'stopped', 0.0, None
            self._resume_from_seek = False
        elif stop_error is not None:
            raise stop_error
        self._ab, self._end_seconds = None, None

    def close(self):
        self._close_transport()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
