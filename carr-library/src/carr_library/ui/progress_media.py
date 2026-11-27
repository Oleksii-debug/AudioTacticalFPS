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
        if not self.recording and not panel.preferences.quiet_during_media:
            return True
        self.media.flags()
        return not self.media.uncertain and panel.feedback.quiescent

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
        recording = recording or any(hold.recording for hold in self.holds)
        waiting = any(not hold.recording for hold in self.holds)
        return (recording or uncertain or panel._flag('progress_recording_active'),
                playing or waiting or panel._flag('progress_media_busy'))

    def refresh(self):
        panel = self.panel
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
        self.holds.add(hold)
        self.refresh()
        return hold

    def release(self, hold):
        if threading.get_ident() != self.ui_thread:
            self.panel.wx.CallAfter(lambda: self.release(hold))
            return
        self.holds.discard(hold)
        self.refresh()

    def register_player(self, player):
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
        self.holds.clear()
        # Uncertain transports stay referenced until their owner confirms close.
