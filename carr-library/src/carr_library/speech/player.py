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
from .output_devices import OutputDevice, verify_output_device
from ..audio.process import native_audio_environment


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
        self._output_device = None
        self._identity = None
        self._wave = None
        self._live_controls, self._levels = False, None
        self._skip_intervals = (10.0, 30.0)
        self._listening = self._listening_request = None
        self._playlist, self._playlist_index, self._playlist_armed = (), None, False
        self.backend = 'Windows MCI' if os.name == 'nt' else 'ffplay'
        self.position_is_approximate = os.name != 'nt'
        self._executable = shutil.which('ffplay') if os.name != 'nt' else None

    @property
    def session_generation(self):
        return self._session_generation

    @property
    def available(self):
        return os.name == 'nt' or bool(self._executable)

    @property
    def output_selection_supported(self):
        return os.name == 'nt'

    @property
    def output_device(self):
        """This reader's session selection; None keeps its original mapper."""
        return self._output_device

    @property
    def active_output_index(self):
        if self._wave is not None and self._output_device is not None:
            self._wave.check_output()
            return self._wave.device.index
        if os.name != 'nt' or not self._open or self._output_device is None:
            return None
        return int(self._mci(f'status {self._alias} output'))

    def set_output_device(self, selected):
        """Adopt an explicit prepared selection without starting audio.

        Changing an active route leaves its position paused, disarms A/B and
        sleep, and invalidates queued restarts. Only an explicit resume/read
        opens the new route; the caller retains its normal quiet admission.
        """
        if selected is not None and not isinstance(selected, OutputDevice):
            raise ValueError('Select a verified native output device or System default output.')
        if selected is not None and not self.output_selection_supported:
            raise RuntimeError('This playback backend does not support per-reader output selection.')
        state = self.state
        position = self.position_seconds if self.path is not None else 0.0
        self._session_generation += 1
        self._listening_request = None
        self._disarm()
        self._close_backend()
        self._output_device = selected
        if self.path is not None and state in ('playing', 'paused'):
            self._offset, self._end_seconds = position, self.duration_seconds
            self._state, self._started = 'paused', None

    def _verify_output_route(self):
        if self._output_device is None:
            return None
        current = verify_output_device(self._output_device)
        if self._open and self.active_output_index != current.index:
            raise RuntimeError('Windows did not retain the explicitly selected reader output. '
                               'Playback was stopped; no default fallback is allowed.')
        return current


    @staticmethod
    def _stamp(path):
        info = local_file(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @property
    def live_mix_available(self):
        return os.name == 'nt'

    @property
    def levels(self):
        from ._wave_output import PlaybackLevels
        return self._levels if self._levels is not None else PlaybackLevels()

    def set_levels(self, volume, *, muted=False, balance=0):
        """Use this same player's owned output; no system-wide mixer operation."""
        from ._wave_output import PlaybackLevels
        levels = PlaybackLevels(volume, muted, balance)
        if not self.live_mix_available:
            raise RuntimeError('Live volume, mute and balance require the Windows owned-output backend.')
        if self._wave is not None:
            try:
                self._wave.set_levels(levels)
            except BaseException:
                self._disarm()
                self._close_backend()
                raise
            self._levels = levels
            return levels
        state = self.state
        if state not in ('playing', 'paused'):
            self._levels, self._live_controls = levels, True
            return levels
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
            self._levels, self._live_controls = levels, True
            return levels
        position = self.position_seconds
        if self._identity is None or self._stamp(self.path) != self._identity:
            self.stop()
            raise ValueError('The current recording changed. Select it again.')
        self._check_listening()
        old_levels, old_live = self._levels, self._live_controls
        self._levels, self._live_controls = levels, True
        try:
            self._start(self.path, start_seconds=position,
                        end_seconds=self._ab[1] if self._repeat_enabled else None,
                        paused=state == 'paused')
        except BaseException:
            self._levels, self._live_controls = old_levels, old_live
            raise
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
        if sleep is not None and self._clock() >= sleep:
            self.stop()
            return False
        self._playlist_armed = False
        try:
            self._replace_recording(item.path, expected=item.identity)
        except BaseException:
            # Validation can fail before the old native handle has closed.
            self.stop()
            raise
        self._playlist_index, self._playlist_armed = index, True
        self._sleep_deadline = sleep
        return True

    @property
    def queue_pending(self):
        return bool(self._playlist_armed and not self._repeat_enabled
            and self.state == 'stopped' and self._ended
            and self._playlist_index is not None
            and self._playlist_index + 1 < len(self._playlist))

    def advance_if_ready(self):
        """UI admission only. poll() itself never opens or starts the next file."""
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
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
                          runtime_factory=None, broker=None):
        """Heavy work only: no native transport call and no playback admission."""
        from .listening import prepare_listening
        return prepare_listening(request, directory, state_root=state_root,
            cancelled=lambda: cancelled() or self._listening_request is not request,
            runtime_factory=runtime_factory, broker=broker)

    def apply_listening(self, prepared):
        """Adopt a worker result on the transport owner; never autoplay."""
        from .listening import PreparedListening
        if (not isinstance(prepared, PreparedListening)
                or self._listening_request is not prepared.request
                or self.path != prepared.request.source or self._identity != prepared.request.identity
                or self.state in ('playing', 'paused')):
            raise ValueError('The listening request is stale or playback resumed; prepare it again.')
        prepared.check_live()
        self._close_backend()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
        prepared.claim(self)
        self._listening = prepared
        self._listening_request = None
        self._session_generation += 1
        self._offset = prepared.request.position_seconds
        self._state, self._started = 'stopped', None
        self._disarm()
        self._end_seconds = self.duration_seconds

    def _to_transport(self, seconds):
        return self._listening.mapping.to_output(seconds) if self._listening is not None else seconds

    def _from_transport(self, seconds):
        return self._listening.mapping.to_source(seconds) if self._listening is not None else seconds

    def _check_listening(self):
        if self._listening is not None:
            self._listening.check_live()

    def output_devices(self, *, cancelled=lambda: False):
        from .output_devices import output_devices
        return output_devices(cancelled=cancelled)

    def select_output(self, device):
        """Explicit stopped-route selection; an obsolete ordinal never falls back."""
        if self.state in ('playing', 'paused'):
            raise ValueError('Stop playback before selecting a different output.')
        if device is not None:
            verify_output_device(device)
        self.set_output_device(device)

    def play_current(self):
        """Explicitly replay the retained recording or resume its paused position."""
        if self.path is None:
            raise ValueError('Open a recording before playing it.')
        if self.state == 'playing':
            return False
        if self.state == 'paused':
            return self.resume()
        position = self._offset
        if self._repeat_enabled:
            first, last = self._ab
            position = position if first <= position < last else first
        else:
            last = None
            position = position if 0 <= position < self.duration_seconds else 0
        self._start(self.path, start_seconds=position, end_seconds=last)
        return True

    def skip(self, seconds):
        if self.path is None or type(seconds) not in (float, int) or not -600 <= seconds <= 600:
            raise ValueError('Choose a skip of at most ten minutes within the current recording.')
        first, last = self._ab if self._repeat_enabled else (0, self.duration_seconds)
        # B is an end boundary, never a valid repeat start.
        upper = max(first, last - .001) if self._repeat_enabled else last
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
            if self._wave is not None:
                try:
                    self._wave.pump()
                    if self._wave.ended:
                        self._state, self._ended = 'stopped', True
                        self._offset, self._started = self._end_seconds, None
                except BaseException:
                    self._disarm()
                    self._close_backend()
                    self._state = 'error'
                    raise
            elif os.name == 'nt' and self._open:
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
        if self._wave is not None:
            return self._from_transport(self._wave.position_seconds)
        if os.name == 'nt' and self._open:
            return self._from_transport(int(self._mci(f'status {self._alias} position')) / 1000)
        if self.state == 'playing' and self._started is not None:
            return min(self._end_seconds, self._from_transport(
                self._to_transport(self._offset) + (self._clock() - self._started)))
        return self._offset

    @staticmethod
    def _seconds(value, duration):
        if (type(value) not in (float, int) or not 0 <= value <= duration
                or not math.isfinite(value)):
            raise ValueError('Choose a playback position within the recording.')
        return float(value)

    def _start(self, path, *, start_seconds=0, end_seconds=None, paused=False):
        """Reconfigure this same transport in original-source time.

        A paused reconfiguration opens no device. Listening conversions map
        original frames to their measured derivative; loop policy stays here.
        """
        target = local_file(path)
        identity = self._stamp(target)
        if target == self.path and self._identity is not None and identity != self._identity:
            self.stop()
            raise ValueError('The current original recording changed. Select it again.')
        with wave.open(str(target), 'rb') as audio:
            if (audio.getcomptype() != 'NONE' or audio.getnframes() <= 0
                    or audio.getframerate() <= 0):
                raise ValueError('Playback requires nonempty PCM WAV audio.')
            duration = audio.getnframes() / audio.getframerate()
        if self._stamp(target) != identity:
            raise ValueError('The recording changed while its PCM header was read.')
        start = self._seconds(start_seconds, duration)
        end = duration if end_seconds is None else self._seconds(end_seconds, duration)
        if end < start or end_seconds is not None and end == start:
            raise ValueError('The playback end must follow its starting position.')
        self._check_listening()
        transport = self._listening.path if self._listening is not None else target
        transport_identity = self._stamp(transport)
        transport_start, transport_end = self._to_transport(start), self._to_transport(end)
        if os.name == 'nt' and end_seconds is not None and not self._live_controls:
            start_ms, end_ms = math.ceil(transport_start * 1000), math.floor(transport_end * 1000)
            if start_ms >= end_ms:
                raise ValueError('The A/B interval is shorter than the playback device resolution.')
            transport_start, transport_end = start_ms / 1000, end_ms / 1000
            start, end = self._from_transport(transport_start), self._from_transport(transport_end)
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self._close_backend()
        if self._stamp(target) != identity or self._stamp(transport) != transport_identity:
            raise ValueError('The recording changed before native playback admission.')
        self.path, self._identity, self.duration_seconds = target, identity, duration
        self._offset, self._end_seconds, self._ended = start, end, False
        if paused:
            self._state, self._started = 'paused', None
            return
        try:
            if os.name == 'nt' and self._live_controls:
                from ._wave_output import WaveOutput
                self._wave = WaveOutput()
                self._wave.open(transport, start_seconds=transport_start,
                    end_seconds=None if end_seconds is None else transport_end,
                    device=self._output_device, levels=self.levels, expected=transport_identity)
                self.backend = 'Windows waveOut'
            elif os.name == 'nt':
                selected = self._verify_output_route()
                self._mci(f'open "{transport}" type waveaudio alias {self._alias}')
                self._open = True
                self._mci(f'set {self._alias} time format milliseconds')
                if selected is not None:
                    self._mci(f'set {self._alias} output {selected.index}')
                    self._verify_output_route()
                suffix = '' if end_seconds is None else f' to {end_ms}'
                self._mci(f'play {self._alias} from {round(transport_start * 1000)}{suffix}')
                self.backend = 'Windows MCI'
            else:
                limit = [] if end_seconds is None else ['-t', str(transport_end - transport_start)]
                self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                    '-nodisp', '-autoexit', '-nostats', '-ss', str(transport_start),
                    '-protocol_whitelist', 'file,pipe', '-i', str(transport), *limit],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, shell=False, env=native_audio_environment())
        except BaseException:
            self._disarm()
            self._close_backend()
            raise
        self._started = self._clock()
        self._state = 'playing'

    def _replace_recording(self, path, *, start_seconds=0, expected=None):
        target = local_file(path)
        if expected is not None and self._stamp(target) != expected:
            raise ValueError('The reviewed playlist source changed before playback.')
        self._session_generation += 1
        self._listening_request = None
        self._disarm()
        self._close_backend()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
        # The old path may have changed; an explicit new review grants only
        # the newly selected bytes, without retaining the old derivative.
        self.path, self._identity = None, None
        if expected is not None and self._stamp(target) != expected:
            raise ValueError('The reviewed playlist source changed before playback.')
        self._start(target, start_seconds=start_seconds)

    def play(self, path, *, start_seconds=0):
        self._playlist_index = None
        self._replace_recording(path, start_seconds=start_seconds)

    def pause(self):
        self._playlist_armed = False
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
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._expire_sleep()
            return False
        # A bounds change or sleep expiry can release the paused device. Open
        # it again from the retained position only after this explicit action.
        if not self._open and self._process is None and self._wave is None:
            self._start(self.path, start_seconds=self._offset,
                        end_seconds=self._ab[1] if self._repeat_enabled else None)
            return True
        if self._identity is not None and self._stamp(self.path) != self._identity:
            self.stop()
            raise ValueError('The current original recording changed. Select it again.')
        self._check_listening()
        if self._wave is not None:
            try:
                self._wave.resume()
            except BaseException:
                self._disarm()
                self._close_backend()
                raise
        elif os.name == 'nt':
            try:
                self._verify_output_route()
                self._mci(f'resume {self._alias}')
            except Exception:
                self._disarm()
                self._close_backend()
                raise
        else:
            if self._process.poll() is not None:
                raise RuntimeError('The playback device stopped. Start playback again.')
            os.kill(self._process.pid, signal.SIGCONT)
        self._started = self._clock()
        self._state = 'playing'
        return True

    def seek(self, seconds, *, start_if_stopped=True):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        seconds = self._seconds(seconds, self.duration_seconds)
        if type(start_if_stopped) is not bool:
            raise ValueError('The seek playback policy must be boolean.')
        if self._repeat_enabled and not self._ab[0] <= seconds < self._ab[1]:
            raise ValueError('Disable A/B repeat before seeking outside its range.')
        state = self.state
        paused = state == 'paused'
        if state != 'playing' and not paused and not start_if_stopped:
            self._close_backend()
            self._offset, self._end_seconds = seconds, self.duration_seconds
            self._state, self._started, self._ended = 'stopped', None, False
            return
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
                or seconds <= 0 or seconds > 1e300 or not math.isfinite(seconds)):
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
        if self.queue_pending:
            return 'queue-pending'
        if self._ended and self._playlist_index == len(self._playlist) - 1:
            self._playlist_armed = False
        return None

    def _disarm(self):
        self._playlist_armed = False
        self._ab, self._repeat_enabled, self._sleep_deadline = None, False, None
        self.repeat_count, self._ended = 0, False

    def _halt(self):
        if self._wave is not None:
            self._wave.close()
            self._wave = None
        if os.name == 'nt' and self._open:
            self._mci(f'stop {self._alias}')
        elif self._process is not None:
            stop_process(self._process)
            self._process = None
        self._state, self._offset, self._started = 'stopped', 0.0, None
        self._ended = False

    def stop(self):
        self._listening_request = None
        # Disarm before a fallible device call: a failed stop must never leave
        # an automatic restart armed.
        self._session_generation += 1
        self._disarm()
        self._halt()

    def _close_backend(self):
        stop_error = None
        try:
            self._halt()
        except (OSError, RuntimeError) as exc:
            stop_error = exc
        if os.name == 'nt' and self._open:
            # Keep the alias owned after failed Close; a successful Close
            # proves silence even if the preceding Stop failed.
            self._mci(f'close {self._alias}')
            self._open = False
            self._state, self._offset, self._started = 'stopped', 0.0, None
            self._ended = False
        elif stop_error is not None:
            raise stop_error

    def close(self):
        self._session_generation += 1
        self._listening_request = None
        self._disarm()
        self._close_backend()
        if self._listening is not None:
            self._listening.release(self)
            self._listening = None
