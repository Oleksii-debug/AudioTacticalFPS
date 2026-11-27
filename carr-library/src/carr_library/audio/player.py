"""One local PCM/RF64 player shared by the desktop and public audio API.

Windows streams a bounded queue through WinMM, including exact A–B endpoints.
The optional POSIX ffplay fallback exposes observed process state and file
metadata; it does not manufacture a playhead position from elapsed wall time.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import shutil
import subprocess
import time

from .ffmpeg import wav_info
from .model_manifest import local_path
from .playback import PlaybackStatus

_DEVICE_UNSET = object()


@dataclass(frozen=True)
class _QueueItem:
    path: Path
    duration: float
    identity: tuple


def _queue_item(path):
    target = local_path(path)
    from os import fstat
    from .jobs import file_identity, verified_reader
    from ..native_file_metadata import stream_change_time
    with verified_reader(target) as stream:
        duration = wav_info(target).seconds
        identity = file_identity(fstat(stream.fileno())) + (stream_change_time(stream),)
    return _QueueItem(target, duration, identity)


@dataclass(frozen=True)
class PlaybackConfiguration:
    """In-memory settings for borrowing this exact player; never an audio start."""
    owner: object
    queue: tuple
    current: object
    index: int | None
    loop: object
    segments: tuple
    device: object
    sleep_deadline: object


def _snapshot_item(path, *, cancelled=lambda: False):
    from .jobs import file_identity, regular_file, sha256_file, verified_reader
    from ..native_file_metadata import stream_change_time
    with verified_reader(path) as stream:
        item = _queue_item(path)
        identity = file_identity(regular_file(path))
        change = stream_change_time(stream)
        digest = sha256_file(path, expected_identity=identity, cancelled=cancelled)
    return item, identity, change, digest


class WavePlayer:
    MAX_PLAYLIST = 256
    MAX_SEGMENTS = 10000

    def __init__(self, *, clock=time.monotonic):
        from .playback import PCMPlayer
        self.process = None
        self.native = PCMPlayer() if os.name == 'nt' else None
        self._external_status = PlaybackStatus()
        self._clock = clock
        self._queue, self.playlist_index = (), None
        self._path, self._duration, self._device_id = None, 0, None
        self._selection_cleared = False
        self._auto_advance, self._ended = False, False
        self._end_seconds, self._ab, self._sleep_deadline = 0, None, None
        self.segments = ()

    @property
    def available(self):
        return self.native is not None or shutil.which('ffplay') is not None

    @property
    def backend(self):
        return 'Windows WinMM waveOut' if self.native is not None else 'ffplay'

    @property
    def state(self):
        state = self.status().state
        if state == 'completed':
            self._ended = True
            return 'stopped'
        return state

    @property
    def path(self):
        return self._path

    @property
    def duration_seconds(self):
        return self._duration

    @property
    def position_seconds(self):
        return self.status().position_seconds

    @property
    def position_is_approximate(self):
        return self.native is None

    def play(self, path, *, start_seconds=0, device_id=None):
        item = _queue_item(path)
        self._start(item.path, start_seconds=start_seconds, device_id=device_id)
        self._queue, self.playlist_index = (item,), 0
        self._ab, self.segments, self._auto_advance = None, (), True

    def _start(self, path, *, start_seconds=0, end_seconds=None, device_id=_DEVICE_UNSET):
        from .playback import PCMPlayer
        selected = local_path(path)
        # Validate all bounds before interrupting an existing valid recording.
        info = wav_info(selected)
        start = PCMPlayer._frame(start_seconds, info.rate, info.frames)
        end = info.frames if end_seconds is None else PCMPlayer._frame(end_seconds, info.rate, info.frames)
        if end < start:
            raise ValueError('The playback end must not precede the starting position.')
        selected_device = self._device_id if device_id is _DEVICE_UNSET else device_id
        if self.native is not None:
            self.native.play(selected, start_seconds=start_seconds, end_seconds=end_seconds,
                             device_id=selected_device)
        else:
            if selected_device is not None:
                raise ValueError('Selecting an output device requires native Windows playback.')
            executable = shutil.which('ffplay')
            if executable is None:
                raise ValueError('Local playback is unavailable. Open the exported WAV in your audio player.')
            self._halt()
            limit = [] if end_seconds is None else ['-t', str(float(end_seconds - start_seconds))]
            self.process = subprocess.Popen([executable, '-hide_banner', '-nodisp', '-autoexit',
                '-loglevel', 'error', '-nostats', '-ss', str(float(start_seconds)),
                '-protocol_whitelist', 'file,pipe', '-i', str(selected), *limit],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                shell=False, start_new_session=True)
            self._external_status = PlaybackStatus(state='playing', path=str(selected),
                position_frames=start, total_frames=info.frames, sample_rate=info.rate, channels=info.channels,
                device_name='System output through local ffplay', position_accuracy='external_player')
        self._path, self._duration, self._device_id = selected, info.seconds, selected_device
        self._end_seconds, self._ended, self._selection_cleared = end / info.rate, False, False

    def status(self):
        if self.native is not None:
            snapshot = self.native.status()
        else:
            if self.process is not None and self.process.poll() is not None:
                code = self.process.returncode
                self._external_status = replace(self._external_status, state='completed' if code == 0 else 'error',
                    error=None if code == 0 else f'The local audio player exited with code {code}.')
            snapshot = self._external_status
        if getattr(self, '_selection_cleared', False) and snapshot.state not in ('error', 'unavailable', 'closed'):
            return PlaybackStatus()
        return snapshot

    def _check_current_recording(self):
        index = self.playlist_index
        if (type(index) is not int or not 0 <= index < len(self._queue)
                or self._queue[index].path != self.path):
            raise ValueError('Review the current recording before changing playback.')
        if _queue_item(self.path) != self._queue[index]:
            raise ValueError('The queued recording changed. Restore and review it before playback.')
        return self._queue[index]

    def _native_control(self, operation, *args):
        if self.native is None:
            raise ValueError('Pause, resume and timeline seeking use the native Windows player.')
        return getattr(self.native, operation)(*args)

    def pause(self):
        if self.status().state not in ('playing', 'starting'):
            return False
        self._native_control('pause')
        return True

    def resume(self):
        if self.status().state != 'paused':
            return False
        self._check_current_recording()
        self._native_control('resume')
        return True

    def seek(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= self.duration_seconds:
            raise ValueError('Choose a playback position within the recording.')
        if self.native is None:
            raise ValueError('Timeline seeking uses the native Windows player.')
        self._check_current_recording()
        paused = self.state == 'paused'
        end = self._ab[1] if self._ab is not None and seconds < self._ab[1] else None
        self._start(self.path, start_seconds=seconds, end_seconds=end)
        if paused:
            self.pause()

    def _halt(self):
        if self.native is not None:
            self.native.stop()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            self.process = None
            self._external_status = replace(self._external_status, state='stopped', position_frames=0)
        self._ended = False

    def stop(self):
        self._auto_advance, self._sleep_deadline = False, None
        self._halt()

    def close(self):
        self._auto_advance, self._sleep_deadline = False, None
        if self.native is not None:
            self.native.close()
            self._ended = False
        else:
            self._halt()

    @property
    def playlist(self):
        return tuple(item.path for item in self._queue)

    def _checked_queue(self, paths, existing=()):
        if not isinstance(paths, (list, tuple)) or len(existing) + len(paths) > self.MAX_PLAYLIST:
            raise ValueError(f'Select a bounded playlist of at most {self.MAX_PLAYLIST} recordings.')
        return tuple(existing) + tuple(_queue_item(path) for path in paths)

    def set_playlist(self, paths):
        """Validate the entire replacement before changing the current queue."""
        items = self._checked_queue(paths)
        self.stop()
        self._queue, self.playlist_index = items, None
        self._path, self._duration, self._selection_cleared = None, 0, True
        self._ab, self.segments = None, ()

    def capture_configuration(self, *, cancelled=lambda: False):
        """Stop and capture existing queue/selection settings without a resume.

        Byte readers for a new derivative are separate caller-owned resources.
        Run on an operation worker: every saved recording is byte-hashed under
        its native reader. Cancellation stays active during each bounded read.
        """
        queue, path, index = self._queue, self.path, self.playlist_index
        loop, segments = self._ab, tuple(self.segments)
        device, sleep_deadline = self._device_id, self._sleep_deadline
        self.stop()
        states = tuple(_snapshot_item(item.path, cancelled=cancelled) for item in queue)
        if any(state[0] != item for state, item in zip(states, queue)):
            raise ValueError('A queued recording changed. Review the playlist before opening playback controls.')
        current = _snapshot_item(path, cancelled=cancelled) if path is not None else None
        if index is not None and (not 0 <= index < len(states)
                or current is None or states[index][0] != current[0]):
            raise ValueError('The current playback selection changed.')
        return PlaybackConfiguration(self, states, current, index, loop, segments, device, sleep_deadline)

    def restore_configuration(self, saved, *, cancelled=lambda: False):
        """Restore checked settings in a stopped state, never open an output.

        The original sleep deadline continues to elapse while another playback
        view borrows the player. An expired timer is not renewed on return.
        """
        if type(saved) is not PlaybackConfiguration or saved.owner is not self:
            raise ValueError('Playback settings belong to another player.')
        if (type(saved.queue) is not tuple or len(saved.queue) > self.MAX_PLAYLIST
                or any(type(row) is not tuple or len(row) != 4
                       or type(row[0]) is not _QueueItem for row in saved.queue)):
            raise ValueError('Invalid captured playback queue.')
        checked = tuple(_snapshot_item(row[0].path, cancelled=cancelled) for row in saved.queue)
        if checked != saved.queue:
            raise ValueError('A saved playlist recording changed while playback controls were open.')
        if saved.current is not None and (type(saved.current) is not tuple or len(saved.current) != 4
                or type(saved.current[0]) is not _QueueItem):
            raise ValueError('Invalid saved current recording.')
        current = None if saved.current is None else _snapshot_item(saved.current[0].path, cancelled=cancelled)
        if current != saved.current:
            raise ValueError('The saved current recording changed while playback controls were open.')
        if saved.index is not None and (type(saved.index) is not int
                or not 0 <= saved.index < len(checked) or current is None
                or checked[saved.index][0] != current[0]):
            raise ValueError('Invalid saved playback selection.')
        duration = current[0].duration if current is not None else 0
        if saved.loop is not None and (not isinstance(saved.loop, tuple) or len(saved.loop) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in saved.loop)
                or not 0 <= saved.loop[0] < saved.loop[1] <= duration
                or saved.loop[1] - saved.loop[0] < .05):
            raise ValueError('Invalid saved A–B range.')
        if (type(saved.segments) is not tuple or len(saved.segments) > self.MAX_SEGMENTS
                or saved.sleep_deadline is not None and (
                    type(saved.sleep_deadline) not in (int, float)
                    or not math.isfinite(saved.sleep_deadline))):
            raise ValueError('Invalid saved playback navigation.')
        previous = -1
        for row in saved.segments:
            if (type(row) is not tuple or len(row) != 2
                    or type(row[0]) not in (int, float) or not math.isfinite(row[0])
                    or not previous < row[0] < duration or row[0] < 0
                    or type(row[1]) is not str or not row[1].strip()
                    or len(row[1]) > 500 or '\0' in row[1]):
                raise ValueError('Invalid saved playback segment.')
            previous = row[0]
        if saved.device is not None and (type(saved.device) is not int or not 0 <= saved.device <= 4095):
            raise ValueError('Invalid saved playback output.')
        self.stop()
        self._queue, self.playlist_index = tuple(row[0] for row in checked), saved.index
        self._path, self._duration = (None, 0) if current is None else (current[0].path, duration)
        self._ab, self.segments, self._device_id = saved.loop, saved.segments, saved.device
        self._sleep_deadline = (saved.sleep_deadline if saved.sleep_deadline is not None
                               and saved.sleep_deadline > self._clock() else None)
        self._auto_advance, self._ended, self._selection_cleared = False, False, True

    def enqueue(self, paths):
        self._queue = self._checked_queue(paths, self._queue)

    def play_index(self, index, *, device_id=_DEVICE_UNSET, start_seconds=None):
        if type(index) is not int or not 0 <= index < len(self._queue):
            raise ValueError('Choose a recording in the current playlist.')
        item = self._queue[index]
        if _queue_item(item.path) != item:
            raise ValueError('The queued recording changed. Add its current version to the playlist again.')
        same_file = self.path == item.path
        loop = self._ab if same_file and start_seconds is None else None
        start = (loop[0] if loop else 0) if start_seconds is None else start_seconds
        self._start(item.path, start_seconds=start,
                    end_seconds=loop[1] if loop else None, device_id=device_id)
        if not same_file or start_seconds is not None:
            self._ab, self.segments = None, ()
        self.playlist_index, self._auto_advance = index, True
        return True

    def next_track(self):
        index = 0 if self.playlist_index is None else self.playlist_index + 1
        return self.play_index(index) if index < len(self._queue) else False

    def previous_track(self):
        index = 0 if self.playlist_index is None else self.playlist_index - 1
        return self.play_index(index) if self._queue and index >= 0 else False

    def set_ab_loop(self, start, end):
        if (self.path is None or any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
                or not 0 <= start < end <= self.duration_seconds or end - start < .05):
            raise ValueError('Choose A and B inside the opened recording, at least 0.05 seconds apart.')
        self._check_current_recording()
        state, position = self.state, self.position_seconds
        self._ab = float(start), float(end)
        if state in ('starting', 'playing', 'paused'):
            self._start(self.path, start_seconds=position if start <= position < end else start, end_seconds=end)
            if state == 'paused':
                self.pause()

    def clear_ab_loop(self):
        if self.path is not None:
            self._check_current_recording()
        state, position = self.state, self.position_seconds
        self._ab = None
        if state in ('starting', 'playing', 'paused'):
            self._start(self.path, start_seconds=position)
            if state == 'paused':
                self.pause()

    @property
    def ab_loop(self):
        return self._ab

    def set_sleep_timer(self, seconds=None):
        if seconds is None:
            self._sleep_deadline = None
        elif type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 7 * 86400:
            raise ValueError('Choose a sleep timer between zero and seven days, or turn it off.')
        else:
            started = self._clock()
            deadline = started + seconds
            # A same-tick read can otherwise return 60.00000000000001 for
            # a requested sixty seconds when addition crosses a float binade.
            # Correct only upward rounding; retain exact ordinary deadlines
            # and the original deadline when another view borrows the player.
            if deadline - started > seconds:
                deadline = math.nextafter(deadline, -math.inf)
            self._sleep_deadline = deadline

    @property
    def sleep_remaining_seconds(self):
        return None if self._sleep_deadline is None else max(0, self._sleep_deadline - self._clock())

    def set_segments(self, segments):
        if self.path is None or not isinstance(segments, (list, tuple)) or len(segments) > self.MAX_SEGMENTS:
            raise ValueError('Open a recording and supply a bounded segment list.')
        checked = []
        previous = -1
        for row in segments:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise ValueError('Each segment needs a start time and readable title.')
            start, title = row
            if (type(start) not in (int, float) or not math.isfinite(start)
                    or not previous < start < self.duration_seconds or start < 0
                    or not isinstance(title, str) or not title.strip() or len(title) > 500 or '\x00' in title):
                raise ValueError('Segments need strictly ordered in-range times and bounded titles.')
            checked.append((float(start), title))
            previous = start
        self.segments = tuple(checked)

    def seek_segment(self, index):
        if type(index) is not int or not 0 <= index < len(self.segments):
            raise ValueError('Choose an available segment.')
        self.seek(self.segments[index][0])
        return self.segments[index][1]

    def next_segment(self):
        index = bisect_right([row[0] for row in self.segments], self.position_seconds + .01)
        return self.seek_segment(index) if index < len(self.segments) else None

    def previous_segment(self):
        index = bisect_left([row[0] for row in self.segments], self.position_seconds - .01) - 1
        return self.seek_segment(index) if index >= 0 else None

    def poll(self):
        """Advance controls from the caller's timer; never create a device thread.

        The Studio calls this every 500 ms. Native A–B endpoints prevent audio
        overshoot; restart/playlist/sleep actions follow the next timer tick.
        """
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self.stop()
            return 'sleep'
        state = self.state
        if self._ab is not None and (self._ended or state == 'playing' and self.position_seconds >= self._ab[1]):
            self._check_current_recording()
            self._start(self.path, start_seconds=self._ab[0], end_seconds=self._ab[1])
            return 'repeat'
        if self._ended and self._auto_advance:
            self._ended, self._auto_advance = False, False
            if self.next_track():
                return 'next'
        return None
