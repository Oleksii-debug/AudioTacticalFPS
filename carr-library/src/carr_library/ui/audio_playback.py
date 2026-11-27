"""Standalone native entry for the same WavePlayer and playback controls.

One UI owner, one native timer, and the frame's existing serial worker for
bounded file review. Queue admission shares the normal media-quiet contract.
"""
from pathlib import Path
import threading
import time

from ..durable_state import local_path
from ..speech.player import WavePlayer
from ..speech.playlist import PlaylistStore, SavedPlaylist, retain_playlist
from .save_all import SaveResult, coordinator_for
from ..speech.progress_speech import play_when_quiet


class PlaybackOwner:
    def __init__(self, frame, *, schedule, player=None):
        self.frame, self.schedule = frame, schedule
        self.library = frame.library
        self.root = local_path(Path(self.library.root), exists=True)
        info = self.root.stat()
        self.root_identity = info.st_dev, info.st_ino
        self.player = player if player is not None else WavePlayer()
        self.progress = getattr(frame, 'operation_progress', None)
        self.closed = self.silence_confirmed = False
        self.cleanup_confirmed = False
        self.unregister_listening = None
        self.discard_on_close = False
        self.generation, self.pending = 0, None
        self.refresh = None
        self.review_cancel = None
        self.listening = None
        self.notice = ''
        self.store = PlaylistStore(self.root)
        self.loaded = False
        self.pending_saved, self.token = None, None
        self.saved = SavedPlaylist()
        self.last_saved_at = time.monotonic()
        self.unregister_save = lambda: None
        self.notice = 'Reading saved queue metadata on the existing worker.'
        coordinator = coordinator_for(frame)
        if coordinator is not None:
            self.unregister_save = coordinator.register('audio.playback', 'Audio playback queue',
                dirty=self.dirty, save=self.save_for_all, focus=self._refresh)
        if self.progress is not None:
            self.progress.register_player(self.player)

    def check(self, *, idle=False, admission=False):
        if self.closed or getattr(self.frame, '_closing', False):
            raise ValueError('This audio playback window is closed.')
        current = getattr(self.frame, 'library', None)
        if current is not self.library or Path(current.root) != self.root:
            raise ValueError('The active library changed. Open Audio playback again.')
        checked = local_path(self.root, exists=True)
        info = checked.stat()
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The library folder changed. Open Audio playback again.')
        if admission and self.frame.cancel_event.is_set():
            raise InterruptedError('Playback admission was cancelled.')
        if idle and getattr(self.frame, 'working', False):
            raise ValueError('Finish or cancel the current operation before starting playback.')

    def load_saved(self, completed, failed):
        self.check(idle=True)
        if self.loaded:
            raise ValueError('Saved queue metadata was already loaded.')
        generation = self.generation
        def work():
            self.check()
            result = self.store.load()
            self.check()
            return result
        def done(result):
            if self.closed or generation != self.generation or self.frame.cancel_event.is_set():
                self.close()
                return
            self.check()
            self.pending_saved, self.token = result
            self.saved = self.pending_saved or SavedPlaylist()
            self.loaded = True
            self.notice = (f'Saved queue: {len(self.pending_saved.items)} item(s), awaiting explicit review. '
                'No saved media or output device was opened.' if self.pending_saved is not None
                else 'No saved queue. Choose local PCM recordings explicitly.')
            completed()
        self.frame.run_job('Read saved playback queue metadata', work, done, failed=failed,
                           discard=lambda result: self.close())

    def review_outputs(self, completed, failed, *, active):
        self.check(idle=True, admission=True)
        if self.review_cancel is not None:
            raise ValueError('Wait for the current playback review.')
        cancel = threading.Event()
        self.review_cancel = cancel
        generation = self.generation
        def stopped():
            return (cancel.is_set() or self.closed or not active()
                or generation != self.generation or self.frame.cancel_event.is_set())
        def work():
            self.check()
            result = self.player.output_devices(cancelled=stopped)
            self.check()
            if stopped():
                raise InterruptedError('Output inventory was cancelled.')
            return result
        def done(value):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if stopped():
                failure(InterruptedError('Output inventory was cancelled.'))
                return
            self.check(admission=True)
            completed(value)
        def failure(exc):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if not self.closed and active():
                failed(exc)
        self.frame.run_job('Review available playback outputs', work, done, failed=failure)
        return cancel.set

    def dirty(self):
        if self.closed or not self.loaded or self.pending_saved is not None:
            return False
        self.check()
        return SavedPlaylist.capture(self.root, self.player) != self.saved

    def save_now(self):
        self.check()
        if self.review_cancel is not None:
            raise ValueError('Wait for playlist review to finish before saving its queue.')
        return self._save_snapshot()

    def _save_snapshot(self):
        if not self.loaded:
            return 'Saved queue metadata is not yet loaded; existing bytes are unchanged.'
        if self.pending_saved is not None:
            return 'The saved queue is retained unchanged; review it explicitly before playback.'
        current = SavedPlaylist.capture(self.root, self.player)
        if self.token is None or current != self.saved:
            token = self.store.save(current, expected_token=self.token)
            self.token, self.saved = token, current
        self.last_saved_at = time.monotonic()
        return 'Queue order, original-frame cursor and playback controls saved.'

    def before_close(self):
        self.check()
        # Cancellation invalidates the in-flight review before saving only the
        # previously adopted queue. Its late result cannot replace this state.
        if self.review_cancel is not None:
            self.review_cancel.set()
        return self._save_snapshot()

    def discard_changes(self):
        self.discard_on_close = True

    def save_for_all(self, completed):
        try:
            message = self.save_now()
        except Exception as exc:
            completed(SaveResult(False, str(exc)))
        else:
            completed(SaveResult(True, message))

    def _autosave(self):
        if self.closed or not self.loaded or self.pending_saved is not None or self.review_cancel is not None:
            return
        try:
            if self.dirty():
                self.save_now()
            self.last_saved_at = time.monotonic()
        except Exception as exc:
            self.notice = 'Queue changes remain unsaved: ' + str(exc)
            self.last_saved_at = time.monotonic()

    def queue_changed(self):
        self.check()
        self.pending_saved = None
        self._autosave()

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
        session = self.player.session_generation
        def run():
            self.check(idle=True, admission=True)
            if generation != self.generation or session != self.player.session_generation:
                return
            action()
            self._autosave()
            self._refresh()
        play_when_quiet((self.progress,), run, schedule=self.schedule,
            alive=lambda: (not self.closed and generation == self.generation
                           and session == self.player.session_generation
                           and not self.frame.cancel_event.is_set()),
            on_error=self.failed)

    def execute(self, action, *, audible=False):
        self.check(idle=True)
        if not self.loaded:
            raise ValueError('Wait for saved playlist metadata to load.')
        self.frame.cancel_event.clear()
        self.generation += 1
        self.pending = None
        self.notice = ''
        if audible:
            self._admit(action, self.generation)
        else:
            action()
            self._autosave()
            if self.progress is not None:
                self.progress.refresh_audio_state()

    def stop(self):
        self.generation += 1
        self.pending = None
        if self.review_cancel is not None:
            self.review_cancel.set()
        self.player.stop()
        self.notice = 'Stopped; pending queue transitions and repeat were cancelled.'
        self._autosave()
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
        self._autosave()
        self._refresh()

    def poll(self, event=None):
        if self.closed:
            return
        try:
            self.check()
            if not self.loaded:
                return
            if self.frame.cancel_event.is_set():
                self.stop()
                return
            outcome = self.player.poll(restart=False)
            if outcome == 'sleep':
                self.generation += 1
                self.pending = None
                self.notice = 'Sleep timer stopped the current recording and queue.'
            elif outcome in ('repeat_due', 'queue-pending'):
                if not getattr(self.frame, 'working', False) and self.pending != self.generation:
                    generation = self.generation
                    self.pending = generation
                    def advance():
                        try:
                            if generation == self.generation:
                                action = (self.player.restart_repeat if outcome == 'repeat_due'
                                          else self.player.advance_if_ready)
                                action()
                        finally:
                            if self.pending == generation:
                                self.pending = None
                    self._admit(advance, generation)
            if self.progress is not None:
                self.progress.refresh_audio_state()
            if time.monotonic() - self.last_saved_at >= 5:
                self._autosave()
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
            reviewed = self.player.prepare_playlist(chosen, cancelled=stopped)
            result = retain_playlist(self.root, reviewed, cancelled=stopped)
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

    def review_saved(self, completed, failed, *, active, use_current_controls=False):
        self.check(idle=True)
        if self.pending_saved is None:
            raise ValueError('There is no unreviewed saved queue in this window.')
        if self.review_cancel is not None:
            raise ValueError('Wait for the current review to stop.')
        saved, token, generation = self.pending_saved, self.token, self.generation
        cancel = threading.Event()
        self.review_cancel = cancel
        def stopped():
            return (cancel.is_set() or self.closed or not active()
                or generation != self.generation or self.frame.cancel_event.is_set())
        def work():
            self.check()
            current, current_token = self.store.load()
            if current_token != token or current != saved:
                raise ValueError('The saved playlist changed. Reopen Audio playback to review it.')
            result = saved.review(self.root, cancelled=stopped)
            self.check()
            if stopped():
                raise InterruptedError('Saved playlist review was cancelled.')
            return result
        def done(items):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if stopped():
                if not self.closed and active():
                    failed(InterruptedError('Saved playlist review was cancelled.'))
                return
            try:
                self.check()
                saved.apply(self.player, items, use_current_controls=use_current_controls)
                self.pending_saved = None
                self._autosave()
                completed()
            except Exception as exc:
                failure(exc)
        def failure(exc):
            if self.review_cancel is cancel:
                self.review_cancel = None
            if not self.closed and active():
                failed(exc)
        try:
            self.frame.run_job('Review retained playback queue', work, done, failed=failure)
        except BaseException:
            self.review_cancel = None
            raise
        return cancel.set

    def close(self):
        # Capture the original-frame cursor before native Close resets it.
        if not self.closed and not self.discard_on_close:
            self._autosave()
        self.closed = True
        self.generation += 1
        self.pending = None
        self.refresh = None
        errors = []
        self.cleanup_confirmed = False
        try:
            self.unregister_save()
        except Exception as exc:
            errors.append(exc)
        if self.review_cancel is not None:
            self.review_cancel.set()
        if self.listening is not None:
            try:
                self.listening.close()
            except Exception as exc:
                errors.append(exc)
        if not self.silence_confirmed:
            confirmed = False
            try:
                self.player.close()
                self.silence_confirmed = confirmed = True
            except Exception as exc:
                errors.append(exc)
            finally:
                if self.progress is not None:
                    try:
                        self.progress.unregister_player(self.player, silence_confirmed=confirmed)
                    except Exception as exc:
                        errors.append(exc)
        listening_done = (self.listening is None or
                          self.listening.operation is None and not self.listening._values)
        if listening_done and self.unregister_listening is not None:
            try:
                self.unregister_listening()
                self.unregister_listening = None
            except Exception as exc:
                errors.append(exc)
        self.cleanup_confirmed = self.silence_confirmed and listening_done and not errors
        if errors:
            raise RuntimeError('Playback cleanup is not confirmed: ' + str(errors[0]))


def show_audio_playback(frame):
    """Load only passive metadata first, through the existing frame worker."""
    import wx
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
            if not previous.cleanup_confirmed:
                frame.report('The previous playback review is still stopping. Wait for its owned cleanup.')
                return
        except Exception as exc:
            frame.report('The previous playback device still needs cleanup: ' + str(exc))
            return
    owner = PlaybackOwner(frame, schedule=wx.CallLater)
    frame._playback_owner = owner
    def failed(exc):
        try:
            owner.close()
        finally:
            if owner.cleanup_confirmed:
                frame._playback_owner = None
        if not getattr(frame, '_closing', False):
            frame.report('Audio playback: ' + str(exc))
    try:
        owner.load_saved(lambda: _show_loaded_playback(frame, owner), failed)
    except BaseException:
        owner.close()
        if owner.cleanup_confirmed:
            frame._playback_owner = None
        raise


def _show_loaded_playback(frame, owner):
    import wx
    from .reader_playback import show_reader_playback
    from .reader_transport import ReaderPlaybackSession

    owner.check(idle=True, admission=True)
    def submit(work, done, *, on_error=None):
        owner.check(idle=True)
        frame.run_job('Prepare private listening copy', work, done, failed=on_error,
                      discard=owner.listening.discard)
        return True
    owner.listening = ReaderPlaybackSession(owner.player, submit=submit,
        guard=owner.check, capture=lambda: (owner.library, owner.root_identity),
        verify=lambda snapshot: owner.check(),
        alive=lambda: not owner.closed and not getattr(frame, '_closing', False),
        busy=lambda: bool(getattr(frame, 'working', False)),
        cancelled=frame.cancel_event.is_set, clear_cancel=frame.cancel_event.clear,
        start=lambda action, on_error: owner._admit(action, owner.generation),
        stop=owner.stop, pause=owner.pause,
        report=lambda message, focus=False: frame.report(message), refresh=owner._refresh,
        state_root=owner.root / 'state')
    # A frame shutdown must drain this exact late result after ordinary work,
    # even when wx suppresses its queued completion. The existing executor owns
    # the finalizer; it never touches native controls or the playback device.
    owner.unregister_listening = frame.executor.register_cleanup(
        lambda session=owner.listening: session.close(worker_drained=True))
    timer_owner = timer = None
    try:
        timer_owner = wx.EvtHandler()
        timer = wx.Timer(timer_owner)
        timer_owner.Bind(wx.EVT_TIMER, owner.poll, timer)
        def subscribe(callback):
            if callback is None:
                try:
                    timer.Stop()
                finally:
                    owner.close()
            else:
                owner.subscribe(callback)
        timer.Start(100)
        show_reader_playback(frame, owner.player, execute=owner.execute,
            stop=owner.stop, pause=owner.pause, subscribe=subscribe,
            prepare_listening=owner.listening.prepare_listening,
            allow_queue=True, review_playlist=owner.review_playlist,
            status_message=lambda: owner.notice, queue_changed=owner.queue_changed,
            save_queue=owner.save_now, review_saved=owner.review_saved,
            saved_pending=lambda: owner.pending_saved is not None,
            close_queue=owner.before_close, discard_queue=owner.discard_changes,
            review_outputs=owner.review_outputs, guard=lambda: owner.check(admission=True),
            active=lambda: not owner.closed and not getattr(frame, '_closing', False))
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
                    if owner.cleanup_confirmed:
                        frame._playback_owner = None


def install_audio_playback(frame):
    """Install before the one existing KeyboardActions menu capture."""
    import wx
    bar = frame.GetMenuBar()
    menu = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'audio studio'), None)
    if menu is None:
        raise ValueError('Install the existing Audio studio menu before Audio playback.')
    item = menu.Append(wx.ID_ANY, 'Audio playback and saved playlist...')
    def invoke(event=None):
        try:
            show_audio_playback(frame)
        except Exception as exc:
            if not getattr(frame, '_closing', False):
                frame.report('Audio playback: ' + str(exc))
    frame.Bind(wx.EVT_MENU, invoke, item)
    return item
