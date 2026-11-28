"""Owned media quiet boundaries for the shared native progress view.

This observes playback and recording state; it does not measure work or own a
second progress tracker. Transport starts wait for the existing feedback worker
to become silent, with the same explicit opt-out for playback as preferences.
"""
import threading
import weakref


class _AudioQuietHold:
    def __init__(self, media, recording):
        self.media, self.recording, self.closed = media, recording, False

    @property
    def ready(self):
        media, panel = self.media, self.media.panel
        if self.closed or panel.closed:
            return False
        media.flags()
        if media.uncertain or media.live_feedback.uncertain:
            return False
        if not self.recording and any(hold.recording and not hold.closed
                                      for hold in media.hold_snapshot()):
            return False
        for player in tuple(media.players):
            try:
                state = player.state
            except Exception:
                return False
            if state not in ('playing', 'stopped', 'paused', 'error'):
                return False
            if self.recording and state == 'playing':
                return False
        if self.recording and (media.live_feedback.busy
                               or panel._flag('progress_media_busy') or media.audio_active):
            return False
        if not self.recording and not panel.preferences.quiet_during_media:
            return True
        return not media.uncertain and panel.feedback.quiescent

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.media.release(self)


class ProgressMedia:
    """Observe real players without allowing stale or uncertain audio to leak."""
    def __init__(self, panel):
        self.panel = panel
        self.ui_thread = threading.get_ident()
        self.players = weakref.WeakSet()
        self.uncertain, self.holds = set(), set()
        self._lock = threading.Lock()
        self.audio_active = False
        self.live_feedback = LiveMediaFeedback(self)

    def hold_snapshot(self):
        with self._lock:
            return tuple(self.holds)

    def flags(self):
        panel = self.panel
        for player in tuple(self.uncertain):
            try:
                if player.state in ('stopped', 'paused', 'error'):
                    self.uncertain.discard(player)
            except Exception:
                pass
        playing = uncertain = bool(self.uncertain)
        recording = False
        for player in tuple(self.players):
            try:
                state = player.state
                recording = recording or state in ('recording', 'monitoring')
                playing = playing or state == 'playing'
                uncertain = uncertain or state not in (
                    'playing', 'recording', 'monitoring', 'stopped', 'paused', 'error')
            except Exception:
                uncertain = True
        holds = self.hold_snapshot()
        recording = recording or any(hold.recording and not hold.closed for hold in holds)
        waiting = any(not hold.recording and not hold.closed for hold in holds)
        return (recording or uncertain or self.live_feedback.uncertain
                or panel._flag('progress_recording_active'),
                playing or waiting or self.audio_active or self.live_feedback.busy
                or panel._flag('progress_media_busy'))

    def refresh(self):
        panel = self.panel
        if panel.closed:
            return
        if threading.get_ident() != self.ui_thread:
            panel.wx.CallAfter(self.refresh)
            return
        if panel.closed:
            return
        force_quiet, playing = self.flags()
        if force_quiet or playing and panel.preferences.quiet_during_media:
            task = panel.active_task
            panel.feedback.suspend(task.snapshot() if task is not None else None)

    def hold_audio(self, *, recording=False):
        if type(recording) is not bool:
            raise ValueError('Recording activity must be boolean.')
        if threading.get_ident() != self.ui_thread:
            raise RuntimeError('Prepare audio transport on the native UI thread.')
        if self.panel.closed:
            raise RuntimeError('The progress view is closed.')
        hold = _AudioQuietHold(self, recording)
        with self._lock:
            self.holds.add(hold)
        self.refresh()
        return hold

    def release(self, hold):
        with self._lock:
            self.holds.discard(hold)
        if self.panel.closed:
            return
        if threading.get_ident() != self.ui_thread:
            self.panel.wx.CallAfter(self.refresh)
            return
        self.refresh()

    def register_player(self, player):
        if self.panel.closed:
            return player
        if threading.get_ident() != self.ui_thread:
            self.panel.wx.CallAfter(lambda: self.register_player(player))
            return player
        if not self.panel.closed:
            try:
                self.players.add(player)
            except TypeError as exc:
                raise ValueError('A weak-referenceable playback object is required.') from exc
            self.refresh()
        return player

    def unregister_player(self, player, *, silence_confirmed=True):
        if type(silence_confirmed) is not bool:
            raise ValueError('Playback silence confirmation must be boolean.')
        if self.panel.closed:
            if not silence_confirmed:
                self.uncertain.add(player)
            else:
                self.uncertain.discard(player)
            self.players.discard(player)
            return
        if threading.get_ident() != self.ui_thread:
            self.panel.wx.CallAfter(lambda: self.unregister_player(
                player, silence_confirmed=silence_confirmed))
            return
        if not silence_confirmed:
            self.uncertain.add(player)
        else:
            self.uncertain.discard(player)
        self.players.discard(player)
        self.refresh()

    def close(self):
        with self._lock:
            self.holds.clear()
        # Uncertain transports stay referenced until their owner confirms close.


class LiveMediaFeedback:
    """Plain owned media callbacks; late release never requires a native window."""
    def __init__(self, media):
        self.media = media
        self._lock = threading.Lock()
        self._activities = {}

    def _snapshot(self):
        with self._lock:
            return tuple(self._activities.values())

    @property
    def quiet_during_media(self):
        return self.media.panel.preferences.quiet_during_media

    @property
    def uncertain(self):
        for activity in self._snapshot():
            try:
                if getattr(activity, 'silence_uncertain', False):
                    return True
            except Exception:
                return True
        return False

    @property
    def quiescent(self):
        panel = self.media.panel
        if (panel.closed or self.media.uncertain or self.uncertain
                or any(hold.recording and not hold.closed for hold in self.media.hold_snapshot())):
            return False
        return not self.quiet_during_media or panel.feedback.quiescent

    @property
    def busy(self):
        for activity in self._snapshot():
            try:
                if activity():
                    return True
            except Exception:
                return True
        return False

    def suspend(self):
        if self.quiet_during_media:
            task = self.media.panel.active_task
            self.media.panel.feedback.suspend(task.snapshot() if task is not None else None)

    def hold(self, activity):
        if not callable(activity):
            raise ValueError('A plain media activity callback is required.')
        token = object()
        with self._lock:
            self._activities[token] = activity
        self.suspend()

        def release():
            with self._lock:
                self._activities.pop(token, None)
        return release
