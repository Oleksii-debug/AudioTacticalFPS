"""One measured transport for Reader and Studio, with explicit media ownership."""
from contextlib import contextmanager
import threading
import time

from .player import WavePlayer
from .playback_resume import PreparedAudio, ResumeStore


class PlaybackTransport:
    """Serialize commands; UI callers run the potentially blocking work off-thread.

    An unavailable playhead is never converted to elapsed-wall-clock evidence.
    Resume positions are optional convenience data, not access capabilities.
    """

    def __init__(self, root, *, player=None, guard=lambda: None, progress=None, owner=None,
                 clock=time.monotonic):
        self.player = player if player is not None else WavePlayer()
        self.store = ResumeStore(root)
        self.guard, self.progress = guard, progress
        self.owner = owner if owner is not None else self
        self.clock = clock
        self.prepared = None
        self.segments = ()
        self.authorize = lambda: None
        self.warning = ''
        self.closed = False
        self._lock = threading.RLock()
        self._saved_at = 0
        self._saved_point = None

    @contextmanager
    def command(self):
        if not self._lock.acquire(blocking=False):
            raise ValueError('Wait for the current playback command to finish.')
        try:
            yield
        finally:
            self._lock.release()

    def check(self):
        if self.closed:
            raise ValueError('The player is closed.')
        self.guard()
        self.store.check()

    def load(self, prepared, *, segments=(), authorize=lambda: None):
        with self.command():
            self.check()
            if type(prepared) is not PreparedAudio:
                raise ValueError('Prepare and verify the local recording first.')
            prepared.check()
            self.stop()
            self.prepared, self.segments = prepared, tuple(segments)
            self.authorize = authorize
            self.warning = '' if prepared.owner is not None else (
                'Playback position is not saved. Save this project in the library to enable resume.')

    def _source(self):
        self.check()
        if self.prepared is None:
            raise ValueError('Prepare a recording before using playback controls.')
        self.prepared.check()
        return self.prepared

    def _release(self):
        if self.progress is not None:
            self.progress.set_media_state(owner=self.owner)

    def play(self, *, resume=True, device_id=None):
        with self.command():
            prepared = self._source()
            self.authorize()
            self.stop()
            position = 0
            if resume and prepared.owner is not None:
                try:
                    position = self.store.position(prepared.owner, prepared.audio) or 0
                except (ValueError, OSError) as exc:
                    self.warning = 'The recording can play, but its saved position was not loaded: ' + str(exc)
            if position >= prepared.audio.frames:
                position = 0
            if self.progress is not None and not self.progress.prepare_media(playback=True, owner=self.owner):
                self._release()
                raise ValueError('Operation feedback is still stopping. Playback has not started; try Play again.')
            try:
                self.check()
                self.player.play(prepared.path, start_seconds=position / prepared.audio.rate, device_id=device_id)
                if self.segments:
                    self.player.set_segments(self.segments)
            except Exception:
                # Failed stop deliberately retains suppression. Never claim a
                # quiet device just because starting the requested file failed.
                self.player.stop()
                self._release()
                raise
            self._saved_at = self.clock()
            return self.status_text()

    def checkpoint(self, status=None):
        prepared = self.prepared
        if prepared is None or prepared.owner is None:
            return
        status = status or self.player.status()
        if (status.state not in ('playing', 'paused', 'completed')
                or status.position_accuracy == 'external_player' or status.sample_rate != prepared.audio.rate
                or status.total_frames != prepared.audio.frames or status.channels != prepared.audio.channels
                or status.path != str(prepared.path)):
            return
        key = prepared.owner, prepared.audio, status.position_frames
        if self._saved_point == key:
            return
        try:
            self.check()
            prepared.check()
            self.store.remember(prepared.owner, prepared.audio, status.position_frames, guard=self.check)
            self._saved_at = self.clock()
            self._saved_point = key
            self.warning = ''
        except Exception as exc:
            self.warning = 'Playback position was not saved: ' + str(exc)

    def pause_resume(self):
        with self.command():
            self._source()
            if self.player.status().state == 'paused':
                self.player.resume()
            else:
                self.player.pause()
            return self.status_text()

    def seek(self, seconds):
        with self.command():
            self._source()
            self.player.seek(seconds)
            return self.status_text()

    def seek_relative(self, seconds):
        with self.command():
            self._source()
            target = min(self.player.duration_seconds, max(0, self.player.position_seconds + seconds))
            return self.seek(target)

    def loop(self, start, end):
        with self.command():
            self._source()
            self.player.set_ab_loop(start, end)
            return self.status_text()

    def clear_loop(self):
        with self.command():
            self._source()
            self.player.clear_ab_loop()
            return self.status_text()

    def sleep(self, seconds=None):
        with self.command():
            self._source()
            self.player.set_sleep_timer(seconds)
            return self.status_text()

    def seek_segment(self, index):
        with self.command():
            self._source()
            self.player.seek_segment(index)
            return self.status_text()

    def poll(self):
        with self.command():
            try:
                self.check()
                if self.prepared is not None:
                    self.prepared.check()
            except Exception:
                self.withdraw()
                raise
            before = self.player.status()
            # Save a measured position before sleep resets the native device.
            remaining = self.player.sleep_remaining_seconds
            if remaining is not None and remaining <= 0:
                self.checkpoint(before)
            event = self.player.poll()
            status = self.player.status()
            if status.state in ('paused', 'completed') or self.clock() - self._saved_at >= 15:
                self.checkpoint(status)
            if status.state not in ('playing', 'paused', 'starting'):
                self._release()
            if status.state == 'error':
                raise ValueError(status.error or 'The audio output device failed.')
            return ('Sleep timer stopped playback. ' if event == 'sleep' else '') + self.status_text()

    def stop(self, *, save=True):
        with self.command():
            if save and not self.closed:
                self.checkpoint()
            self.player.stop()
            self._release()
            return self.status_text()

    def withdraw(self):
        """Stop without old-scope writes, then forget the prepared capability.

        This remains callable after authorization is lost. A failed device
        stop preserves ownership and notification suppression for safe retry.
        """
        with self.command():
            self.stop(save=False)
            self.prepared = None
            self.segments = ()
            self.authorize = lambda: None
            self._saved_point = None
            self.warning = ''
            return 'Recording withdrawn. ' + self.status_text()

    def close(self):
        with self.command():
            if self.closed:
                return
            self.stop()
            self.player.close()
            self._release()
            self.closed = True

    def status_text(self):
        status = self.player.status()
        duration = (self.prepared.audio.frames / self.prepared.audio.rate if self.prepared is not None
                    else self.player.duration_seconds)
        if status.position_accuracy == 'external_player':
            position = 'Current position unavailable from ffplay; pause and seek controls require Windows WinMM.'
        else:
            position = f'Position {status.position_seconds:.2f} seconds of {duration:.2f} seconds. '
            position += ('Completed-buffer estimate; the device does not provide its precise position. '
                         if status.position_accuracy == 'completed_buffers' else
                         'Device-reported audio position. ' if status.position_accuracy == 'driver_reported' else
                         'Playback has not reported a measured position yet. ')
            position += 'Word and sentence timing is not available.'
        loop = self.player.ab_loop
        sleep = self.player.sleep_remaining_seconds
        return (f'{self.player.backend}. {status.state}. {position}'
                + (f' A–B: {loop[0]:.2f} to {loop[1]:.2f} seconds.' if loop else '')
                + (f' Sleep timer: {sleep:.0f} seconds remaining.' if sleep is not None else '')
                + (' ' + self.warning if self.warning else ''))
