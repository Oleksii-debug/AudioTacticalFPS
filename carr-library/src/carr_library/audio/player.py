"""Keyboard-addressable local WAV playback using an installed native backend.

Windows MCI provides measured play position and native pause/seek. The POSIX
ffplay fallback is explicit; elapsed position there is only a clock estimate.
"""
import ctypes
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from uuid import uuid4
from .engines import _stop as stop_process
from .ffmpeg import wav_info
from .model_manifest import local_path


@dataclass(frozen=True)
class _QueueItem:
    path: Path
    duration: float
    identity: tuple


def _queue_item(path):
    target = local_path(path)
    duration = wav_info(target).seconds
    info = target.stat()
    return _QueueItem(target, duration, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))


class WavePlayer:
    MAX_PLAYLIST = 256
    MAX_SEGMENTS = 10000

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
        self._queue = ()
        self.playlist_index = None
        self._auto_advance = False
        self._ended = False
        self._end_seconds = 0.0
        self._ab = None
        self._sleep_deadline = None
        self.segments = ()
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
                    self._offset = self._end_seconds
                    self._ended = True
            elif self._process is not None and self._process.poll() is not None:
                if self._process.returncode:
                    self._state = 'error'
                else:
                    self._state = 'stopped'
                    self._offset = self._end_seconds
                    self._ended = True
        return self._state

    @property
    def position_seconds(self):
        if os.name == 'nt' and self._open:
            return int(self._mci(f'status {self._alias} position')) / 1000
        if self.state == 'playing' and self._started is not None:
            return min(self._end_seconds, self._offset + self._clock() - self._started)
        return self._offset

    def play(self, path, *, start_seconds=0):
        item = _queue_item(path)
        self._start(item.path, start_seconds=start_seconds)
        self._queue, self.playlist_index = (item,), 0
        self._ab, self.segments, self._auto_advance = None, (), True

    def _start(self, path, *, start_seconds=0, end_seconds=None):
        target = local_path(path)
        # The production reader handles RIFF and RF64, including multi-hour
        # PCM files, without loading or decoding their waveform into memory.
        duration = wav_info(target).seconds
        if (type(start_seconds) not in (float, int) or not math.isfinite(start_seconds)
                or not 0 <= start_seconds <= duration):
            raise ValueError('Choose a playback position within the recording.')
        if not self.available:
            raise RuntimeError('No local playback engine is installed. Install ffplay or use Windows audio.')
        self._close_device()
        self.path, self.duration_seconds = target, duration
        self._offset = float(start_seconds)
        self._end_seconds = float(end_seconds) if end_seconds is not None else duration
        if os.name == 'nt':
            self._mci(f'open "{target}" type waveaudio alias {self._alias}')
            self._open = True
            try:
                self._mci(f'set {self._alias} time format milliseconds')
                limit = '' if end_seconds is None else f' to {round(end_seconds * 1000)}'
                self._mci(f'play {self._alias} from {round(start_seconds * 1000)}' + limit)
            except Exception:
                self._close_device()
                raise
        else:
            limit = [] if end_seconds is None else ['-t', str(end_seconds - start_seconds)]
            self._process = subprocess.Popen([self._executable, '-hide_banner', '-loglevel', 'error',
                '-nodisp', '-autoexit', '-nostats', '-ss', str(float(start_seconds)),
                '-protocol_whitelist', 'file,pipe', '-i', str(target), *limit],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, shell=False)
        self._started = self._clock()
        self._state = 'playing'
        self._ended = False

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
        self._started = self._clock()
        self._state = 'playing'
        return True

    def seek(self, seconds):
        if self.path is None:
            raise ValueError('Open a recording before seeking.')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= self.duration_seconds:
            raise ValueError('Choose a playback position within the recording.')
        paused = self.state == 'paused'
        end = self._ab[1] if self._ab is not None and seconds < self._ab[1] else None
        self._start(self.path, start_seconds=seconds, end_seconds=end)
        if paused:
            self.pause()

    def close(self):
        self._auto_advance, self._sleep_deadline = False, None
        self._close_device()

    def _close_device(self):
        try:
            self._halt()
        except (OSError, RuntimeError):
            pass  # A removed device must not prevent closing the Studio.
        finally:
            if os.name == 'nt' and self._open:
                try:
                    self._mci(f'close {self._alias}')
                except (OSError, RuntimeError):
                    pass
                finally:
                    self._open = False
            self._state, self._offset, self._started = 'stopped', 0.0, None

    def stop(self):
        self._auto_advance, self._sleep_deadline = False, None
        self._halt()

    def _halt(self):
        if os.name == 'nt' and self._open:
            self._mci(f'stop {self._alias}')
        elif self._process is not None:
            stop_process(self._process)
            self._process = None
        self._state, self._offset, self._started = 'stopped', 0.0, None
        self._ended = False

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
        self.close()
        self._queue, self.playlist_index = items, None
        self.path, self.duration_seconds = None, 0
        self._ab, self.segments = None, ()

    def enqueue(self, paths):
        self._queue = self._checked_queue(paths, self._queue)

    def play_index(self, index):
        if type(index) is not int or not 0 <= index < len(self._queue):
            raise ValueError('Choose a recording in the current playlist.')
        item = self._queue[index]
        if _queue_item(item.path) != item:
            raise ValueError('The queued recording changed. Add its current version to the playlist again.')
        same_file = self.path == item.path
        loop = self._ab if same_file else None
        self._start(item.path, start_seconds=loop[0] if loop else 0,
                    end_seconds=loop[1] if loop else None)
        if not same_file:
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
        state, position = self.state, self.position_seconds
        self._ab = float(start), float(end)
        if state in ('playing', 'paused'):
            self._start(self.path, start_seconds=position if start <= position < end else start, end_seconds=end)
            if state == 'paused':
                self.pause()

    def clear_ab_loop(self):
        state, position = self.state, self.position_seconds
        self._ab = None
        if state in ('playing', 'paused'):
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
            self._sleep_deadline = self._clock() + seconds

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
            self._start(self.path, start_seconds=self._ab[0], end_seconds=self._ab[1])
            return 'repeat'
        if self._ended and self._auto_advance:
            self._ended, self._auto_advance = False, False
            if self.next_track():
                return 'next'
        return None
