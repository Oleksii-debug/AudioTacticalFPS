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
        panel = self.media.panel
        if self.closed or panel.closed:
            return False
        # A preference can allow ordinary playback overlap, but cannot turn an
        # unacknowledged native close into confirmed silence.
        _recording, _playing, uncertain = self.media._observed_flags()
        if uncertain:
            return False
        if not self.recording and not panel.preferences.quiet_during_media:
            return True
        return panel.feedback.quiescent

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
        self._state_lock = threading.RLock()
        self._activities = {}

    def _activity_flags(self):
        with self._state_lock:
            activities = tuple(self._activities.values())
        busy = uncertain = False
        for activity in activities:
            try:
                uncertain = uncertain or bool(getattr(activity, 'silence_uncertain', False))
            except Exception:
                uncertain = True
            try:
                busy = busy or bool(activity())
            except Exception:
                # Undeclared ordinary work still suppresses optional feedback;
                # explicit native silence uncertainty also blocks new transport.
                busy = True
        return busy, uncertain

    @property
    def quiet_during_media(self):
        return self.panel.preferences.quiet_during_media

    @property
    def busy(self):
        return self._activity_flags()[0]

    @property
    def quiescent(self):
        if self.panel.closed:
            return False
        force_quiet, _playing = self.flags()
        if force_quiet:
            return False
        return not self.quiet_during_media or self.panel.feedback.quiescent

    def suspend(self):
        panel = self.panel
        if not panel.closed and self.quiet_during_media:
            task = panel.active_task
            panel.feedback.suspend(task.snapshot() if task is not None else None)

    def hold(self, activity):
        """Retain a plain live-reader activity until its worker confirms cleanup.

        Release may run after native destruction. It only removes this lease;
        it never schedules a GUI callback or starts another feedback worker.
        """
        if not callable(activity):
            raise ValueError('A plain media activity callback is required.')
        if self.panel.closed:
            raise RuntimeError('The progress view is closed.')
        token = object()
        with self._state_lock:
            self._activities[token] = activity
        self.suspend()

        def release():
            with self._state_lock:
                self._activities.pop(token, None)
        return release

    def _observed_flags(self):
        with self._state_lock:
            retained = tuple(self.uncertain)
            players = tuple(self.players)
        for player in retained:
            try:
                if player.state in ('stopped', 'paused', 'error'):
                    with self._state_lock:
                        self.uncertain.discard(player)
            except Exception:
                pass
        with self._state_lock:
            playing = uncertain = bool(self.uncertain)
        recording = False
        for player in players:
            try:
                state = player.state
                recording = recording or state in ('recording', 'monitoring')
                playing = playing or state == 'playing'
                uncertain = uncertain or state not in (
                    'playing', 'recording', 'monitoring', 'stopped', 'paused', 'error')
            except Exception:
                uncertain = True
        activity_busy, activity_uncertain = self._activity_flags()
        return recording, playing or activity_busy, uncertain or activity_uncertain

    def flags(self):
        panel = self.panel
        recording, playing, uncertain = self._observed_flags()
        with self._state_lock:
            holds = tuple(self.holds)
        recording = recording or any(hold.recording for hold in holds)
        waiting = any(not hold.recording for hold in holds)
        return (recording or uncertain or panel._flag('progress_recording_active'),
                playing or waiting or panel._flag('progress_media_busy'))

    def refresh(self):
        panel = self.panel
        if panel.closed:
            return
        if threading.get_ident() != self.ui_thread:
            panel.wx.CallAfter(self.refresh)
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
        with self._state_lock:
            self.holds.add(hold)
        self.refresh()
        return hold

    def release(self, hold):
        # Cleanup acknowledgements can arrive from the owning worker after the
        # native event loop has ended. Update plain ownership before any view.
        with self._state_lock:
            self.holds.discard(hold)
        self.refresh()

    def register_player(self, player):
        if self.panel.closed:
            return player
        with self._state_lock:
            try:
                self.players.add(player)
            except TypeError as exc:
                raise ValueError('A weak-referenceable playback object is required.') from exc
        self.refresh()
        return player

    def unregister_player(self, player, *, silence_confirmed=True):
        if type(silence_confirmed) is not bool:
            raise ValueError('Playback silence confirmation must be boolean.')
        with self._state_lock:
            if not silence_confirmed:
                self.uncertain.add(player)
            else:
                self.uncertain.discard(player)
            self.players.discard(player)
        self.refresh()

    def close(self):
        with self._state_lock:
            self.holds.clear()
        # Uncertain transports and plain reader leases stay referenced until
        # their owning worker confirms cleanup. No native callback is required.
