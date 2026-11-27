"""Standalone native entry for the same WavePlayer and playback controls.

One UI owner, one native timer, and the frame's existing serial worker for
bounded file review. Queue admission shares the normal media-quiet contract.
"""
from pathlib import Path
import threading

from ..durable_state import local_path
from ..speech.player import WavePlayer
from ..speech.progress_speech import play_when_quiet


class PlaybackOwner:
    def __init__(self, frame, *, schedule, player=None):
        self.frame, self.schedule = frame, schedule
        self.library = frame.library
        self.root = local_path(Path(self.library.root), exists=True)
        info = self.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.player = player if player is not None else WavePlayer()
        self.progress = getattr(frame, 'progress_panel', None)
        self.closed = self.silence_confirmed = False
        self.generation, self.pending = 0, None
        self.refresh = None
        self.review_cancel = None
        self.notice = ''
        if self.progress is not None:
            self.progress.register_player(self.player)

    def check(self, *, idle=False):
        if self.closed or getattr(self.frame, '_closing', False):
            raise ValueError('This audio playback window is closed.')
        current = getattr(self.frame, 'library', None)
        if current is not self.library or Path(current.root) != self.root:
            raise ValueError('The active library changed. Open Audio playback again.')
        checked = local_path(self.root, exists=True)
        info = checked.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The library folder changed. Open Audio playback again.')
        if idle and getattr(self.frame, 'working', False):
            raise ValueError('Finish or cancel the current operation before starting playback.')

    def _refresh(self):
        if not self.closed and self.refresh is not None:
            self.refresh()

    def subscribe(self, callback):
        self.refresh = callback

    def failed(self, exc):
        self.generation += 1
        self.pending = None
        self.notice = str(exc)
        try:
            self.player.stop()
        except Exception as cleanup:
            self.notice += ' Playback stop is unconfirmed: ' + str(cleanup)
        if self.progress is not None:
            try:
                self.progress.refresh_audio_state()
            except Exception as error:
                self.notice += ' Progress feedback is unavailable: ' + str(error)
        try:
            self._refresh()
        except Exception:
            pass  # A detached status control cannot restart or release native audio.

    def _admit(self, action, generation):
        def run():
            self.check(idle=True)
            if generation != self.generation:
                return
            action()
            self._refresh()
        play_when_quiet((self.progress,), run, schedule=self.schedule,
            alive=lambda: not self.closed and generation == self.generation,
            on_error=self.failed)

    def execute(self, action, *, audible=False):
        self.check(idle=True)
        self.generation += 1
        self.pending = None
        self.notice = ''
        if audible:
            self._admit(action, self.generation)
        else:
            action()
            if self.progress is not None:
                self.progress.refresh_audio_state()

    def stop(self):
        self.generation += 1
        self.pending = None
        if self.review_cancel is not None:
            self.review_cancel.set()
        self.player.stop()
        self.notice = 'Stopped; pending queue transitions and repeat were cancelled.'
        if self.progress is not None:
            self.progress.refresh_audio_state()
        self._refresh()

    def pause(self):
        self.check()
        self.generation += 1
        self.pending = None
        if self.player.state == 'paused':
            self._admit(self.player.resume, self.generation)
        else:
            self.player.pause()
            if self.progress is not None:
                self.progress.refresh_audio_state()
        self._refresh()

    def poll(self, event=None):
        if self.closed:
            return
        try:
            self.check()
            outcome = self.player.poll()
            if outcome == 'sleep':
                self.generation += 1
                self.pending = None
                self.notice = 'Sleep timer stopped the current recording and queue.'
            elif outcome in ('repeat-pending', 'queue-pending'):
                if not getattr(self.frame, 'working', False) and self.pending != self.generation:
                    generation = self.generation
                    self.pending = generation
                    def advance():
                        try:
                            if generation == self.generation:
                                action = (self.player.repeat_if_ready if outcome == 'repeat-pending'
                                          else self.player.advance_if_ready)
                                action()
                        finally:
                            if self.pending == generation:
                                self.pending = None
                    self._admit(advance, generation)
            if self.progress is not None:
                self.progress.refresh_audio_state()
            self._refresh()
        except Exception as exc:
            self.failed(exc)

    def review_playlist(self, paths, completed, failed, *, active):
        self.check(idle=True)
        if self.review_cancel is not None:
            raise ValueError('Wait for the current playlist review to stop.')
        cancel = threading.Event()
        self.review_cancel = cancel
        generation = self.generation
        chosen = tuple(paths)
        def stopped():
            return (cancel.is_set() or self.closed or not active()
                or generation != self.generation or self.frame.cancel_event.is_set())
        def work():
            self.check()
            result = self.player.prepare_playlist(chosen, cancelled=stopped)
            self.check()
            if stopped():
                raise InterruptedError('Playlist review was cancelled; no queue was replaced.')
            return result
        def done(value):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if stopped():
                if not self.closed and active():
                    failed(InterruptedError('Playlist review was cancelled; no queue was replaced.'))
                return
            self.check()
            completed(value)
        def failure(exc):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if not self.closed and active():
                failed(exc)
        try:
            self.frame.run_job('Review selected playback files', work, done, failed=failure)
        except BaseException:
            self.review_cancel = None
            raise
        return cancel.set

    def close(self):
        self.closed = True
        self.generation += 1
        self.pending = None
        self.refresh = None
        if self.review_cancel is not None:
            self.review_cancel.set()
        if self.silence_confirmed:
            return
        confirmed = False
        try:
            self.player.close()
            self.silence_confirmed = confirmed = True
        finally:
            if self.progress is not None:
                self.progress.unregister_player(self.player, silence_confirmed=confirmed)


def show_audio_playback(frame):
    import wx
    from .reader_playback import show_reader_playback

    if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
        frame.report('Finish or cancel the current operation before opening Audio playback.')
        return
    previous = getattr(frame, '_playback_owner', None)
    if previous is not None:
        if not previous.closed:
            frame.report('Audio playback is already open.')
            return
        try:
            previous.close()
        except Exception as exc:
            frame.report('The previous playback device still needs cleanup: ' + str(exc))
            return
    owner = PlaybackOwner(frame, schedule=wx.CallLater)
    frame._playback_owner = owner
    timer_owner = timer = None
    try:
        timer_owner = wx.EvtHandler()
        timer = wx.Timer(timer_owner)
        timer_owner.Bind(wx.EVT_TIMER, owner.poll, timer)
        def subscribe(callback):
            if callback is None:
                # Close before the modal destroys native controls. A queued
                # timer cannot admit work even if native cleanup raises.
                try:
                    timer.Stop()
                finally:
                    owner.close()
            else:
                owner.subscribe(callback)
        timer.Start(50)
        show_reader_playback(frame, owner.player, execute=owner.execute,
            stop=owner.stop, pause=owner.pause, subscribe=subscribe,
            allow_queue=True, review_playlist=owner.review_playlist,
            status_message=lambda: owner.notice)
    finally:
        try:
            if timer is not None:
                timer.Stop()
        finally:
            try:
                if timer_owner is not None and timer is not None:
                    timer_owner.Unbind(wx.EVT_TIMER, handler=owner.poll, source=timer)
            finally:
                try:
                    owner.close()
                finally:
                    if owner.silence_confirmed:
                        frame._playback_owner = None


def install_audio_playback(frame):
    import wx
    from .builtin_keyboard_commands import plain_caption
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command

    bar = frame.GetMenuBar()
    menu = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                if plain_caption(bar.GetMenuLabel(index)) == 'audio studio')
    item = menu.Append(wx.ID_ANY, 'Audio playback and session playlist...')
    def invoke(event=None):
        try:
            show_audio_playback(frame)
        except Exception as exc:
            if not getattr(frame, '_closing', False):
                frame.report('Audio playback: ' + str(exc))
    frame.Bind(wx.EVT_MENU, invoke, item)
    register_menu_command(frame, CommandSpec('audio.playback',
        'Audio playback and session playlist', 'main', ''), item)
