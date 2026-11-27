"""Named native gauges with throttled updates and explicit local sound options."""
import os
from pathlib import Path
import threading
import time

from ..progress import (ProgressSettings, ProgressSettingsStore, ProgressTonePolicy,
                        ProgressTracker, progress_audio_suppressed,
                        request_progress_tone, set_media_active, request_progress_voice,
                        cancel_progress_voice, progress_audio_quiescent, progress_sound_notice)

REFRESH_MS = 250
QUERY_SHORTCUT = 'Ctrl+Alt+P'


class ProgressView:
    def __init__(self, parent, *, settings_root, label='Operation progress', wx_module=None,
                 clock=time.monotonic, tone_sender=request_progress_tone, speech_sender=request_progress_voice):
        if wx_module is None:
            import wx as wx_module
        wx = self.wx = wx_module
        self.root = settings_root if callable(settings_root) else lambda: settings_root
        self.clock, self.tone_sender, self.speech_sender = clock, tone_sender, speech_sender
        self.speech_provider = None
        self.closed, self.tracker, self.audible = False, None, True
        self.settings, self.notice = ProgressSettings(), ''
        self.policy = ProgressTonePolicy(clock=clock)
        self._last_refresh, self._last_text, self._last_value = float('-inf'), '', None
        self._pending, self._pending_lock = None, threading.Lock()
        self._audio_owner, self._media_active, self._active = object(), False, True
        self.panel = wx.Panel(parent)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.gauge = wx.Gauge(self.panel, range=100, style=wx.GA_HORIZONTAL)
        self.gauge.SetName(label + ' percentage')
        self.gauge.SetHelpText('Unknown totals use an indeterminate bar. One hundred percent means a verified completed operation.')
        layout.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 3)
        self.status = wx.TextCtrl(self.panel, value='No operation is running.', style=wx.TE_READONLY)
        self.status.SetName(label + ' phase and completed units')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 3)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        query = wx.Button(self.panel, label='Current progress')
        query.Bind(wx.EVT_BUTTON, self.query)
        buttons.Add(query, 0, wx.RIGHT, 5)
        settings = wx.Button(self.panel, label='Progress sound settings...')
        settings.Bind(wx.EVT_BUTTON, self.configure)
        buttons.Add(settings)
        layout.Add(buttons, 0, wx.ALL, 3)
        self.panel.SetSizer(layout)
        self.timer = wx.Timer(self.panel)
        self.panel.Bind(wx.EVT_TIMER, self.refresh, self.timer)
        self.panel.Bind(wx.EVT_WINDOW_DESTROY, self._destroy)
        self.timer.Start(REFRESH_MS)

    def _destroy(self, event):
        if event.GetEventObject() is self.panel:
            self.close()
        event.Skip()

    def load_settings(self):
        try:
            provider = getattr(self, 'speech_provider', None)
            if provider is not None and provider.library_root != Path(self.root()).resolve():
                self.set_speech_provider(None)
            self.settings = ProgressSettingsStore(self.root()).load()
            self.notice = ('Select a local progress voice in Audio Studio for this session.'
                           if self.settings.mode in ('speech', 'both')
                           and getattr(self, 'speech_provider', None) is None else '')
        except (OSError, ValueError) as exc:
            self.settings = ProgressSettings()
            self.notice = 'Progress sounds remain off because preferences could not be loaded: ' + str(exc)

    def track(self, tracker, *, audible=True):
        if not isinstance(tracker, ProgressTracker):
            raise ValueError('Expected a shared operation progress tracker.')
        self.tracker, self.audible = tracker, audible
        self.load_settings()
        self._last_value, self._last_text = None, ''
        self.refresh(force=True)
        return tracker

    def defer(self, callback):
        """Keep only the latest view update from a worker, bounded to one slot."""
        with self._pending_lock:
            if not self.closed:
                self._pending = callback

    def refresh(self, event=None, *, force=False):
        if self.closed:
            return False
        now = self.clock()
        if not force and now - self._last_refresh < REFRESH_MS / 1000:
            return False
        self._last_refresh = now
        with self._pending_lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            pending()
        if self.tracker is None:
            return False
        snapshot = self.tracker.snapshot()
        if snapshot.percent is None and snapshot.state == 'running':
            self.gauge.Pulse()
            self._last_value = None
        else:
            value = snapshot.percent or 0
            if value != self._last_value:
                self.gauge.SetValue(value)
                self._last_value = value
        text = snapshot.description + (' ' + self.notice if self.notice else '')
        if text != self._last_text:
            self.status.ChangeValue(text)
            self._last_text = text
        self._active = bool(self.panel.GetTopLevelParent().IsActive())
        if self.audible:
            previous_state = self.policy.last_state
            cue = self.policy.choose(snapshot, self.settings, active=self._active,
                                     suppressed=self._media_active or progress_audio_suppressed())
            if cue is not None:
                tracker, settings = self.tracker, self.settings

                def still_current():
                    return (not self.closed and self.tracker is tracker and self.audible
                            and self.settings == settings and settings.mode != 'off'
                            and not settings.muted and settings.volume > 0
                            and (self._active or settings.background)
                            and tracker.snapshot().sequence == snapshot.sequence)
                try:
                    if settings.mode == 'beep':
                        self.tone_sender(cue, settings.volume, still_current)
                    else:
                        provider = getattr(self, 'speech_provider', None)
                        if provider is None:
                            self.notice = 'Select a local progress voice in Audio Studio for this session.'
                        else:
                            def voice_current():
                                return (still_current()
                                        and getattr(self, 'speech_provider', None) is provider
                                        and provider.library_root == Path(self.root()).resolve())
                            state = ('resumed' if previous_state == 'paused'
                                     and snapshot.state == 'running' else None)
                            message = provider.notification_text(snapshot, state=state)
                            sender = getattr(self, 'speech_sender', request_progress_voice)
                            sender(provider, message, settings.volume, voice_current,
                                   segments=cue if settings.mode == 'both' else ())
                    notice = progress_sound_notice()
                    if notice:
                        self.notice = notice
                except Exception:
                    self.notice = 'Progress sound could not be delivered. The operation result is unchanged.'
        return True

    def set_speech_provider(self, provider):
        if provider is not None:
            from ..audio.progress_speech import LocalProgressSpeech
            if not isinstance(provider, LocalProgressSpeech):
                raise ValueError('Use an explicitly selected local progress voice.')
            if provider.library_root != Path(self.root()).resolve():
                raise ValueError('This progress voice belongs to another library. Select it again.')
        previous = getattr(self, 'speech_provider', None)
        self.speech_provider = provider
        if previous is not None and previous is not provider:
            previous.close()
            cancel_progress_voice(previous)
        self.policy = ProgressTonePolicy(clock=self.clock)
        self.notice = ''  # Selecting a voice never enables output or changes saved settings.

    @property
    def audio_quiescent(self):
        return progress_audio_quiescent()

    def query(self, event=None):
        self.refresh(force=True)
        self.status.SetFocus()  # Only an explicit user query moves focus.

    def set_media_active(self, active):
        self._media_active = bool(active)
        set_media_active(self._audio_owner, bool(active))

    def configure(self, event=None):
        # The active library may have changed, or no operation has run since
        # startup. Display its saved choices rather than stale default values.
        self.load_settings()
        provider = getattr(self, 'speech_provider', None)
        selected = show_progress_settings(self.panel, self.root(), self.settings, wx_module=self.wx,
            speech_label=provider.label if provider is not None else '')
        if selected is not None:
            self.settings, self.notice = selected, ''
            self.refresh(force=True)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.set_speech_provider(None)
        self.timer.Stop()
        with self._pending_lock:
            self._pending = None
        self.set_media_active(False)


def show_progress_settings(parent, root, current=None, *, wx_module=None, speech_label=''):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    current = current or ProgressSettingsStore(root).load()
    dialog = wx.Dialog(parent, title='Progress sound settings', size=(620, 560),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    notice = wx.StaticText(dialog, label='Application sounds are off by default. Keep them off when your screen reader '
        'already announces progress. Speech uses only the local voice you explicitly select for this session. '
        'Sounds are suppressed during content playback/recording and never enter rendered tracks.' +
        (' Application tones require Windows.' if os.name != 'nt' else ''))
    notice.Wrap(560)
    layout.Add(notice, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Application progress output'), 0, wx.LEFT | wx.RIGHT, 10)
    mode = wx.Choice(dialog, choices=['Off', 'Beep', 'Speech: selected local voice', 'Beep + Speech'])
    mode.SetName('Application progress output')
    mode.SetSelection(('off', 'beep', 'speech', 'both').index(current.mode))
    layout.Add(mode, 0, wx.EXPAND | wx.ALL, 10)
    voice_notice = wx.StaticText(dialog, label=('Selected for this session: ' + speech_label
        if speech_label else 'No progress voice selected. In Audio Studio, choose Use voice for progress.'))
    voice_notice.SetName('Selected local progress voice')
    voice_notice.Wrap(560)
    layout.Add(voice_notice, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Notification volume, 0 to 100'), 0, wx.LEFT | wx.RIGHT, 10)
    volume = wx.Slider(dialog, value=current.volume, minValue=0, maxValue=100)
    volume.SetName('Progress sound volume')
    layout.Add(volume, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Minimum interval in seconds'), 0, wx.LEFT | wx.RIGHT, 10)
    interval = wx.SpinCtrl(dialog, min=1, max=60, initial=current.interval_seconds)
    interval.SetName('Minimum progress sound interval in seconds')
    layout.Add(interval, 0, wx.EXPAND | wx.ALL, 10)
    muted = wx.CheckBox(dialog, label='Mute application progress sounds')
    muted.SetValue(current.muted)
    background = wx.CheckBox(dialog, label='Allow sounds while this window is in the background')
    background.SetValue(current.background)
    layout.Add(muted, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    layout.Add(background, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY)
    status.SetName('Progress settings save status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 10)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    save = wx.Button(dialog, wx.ID_OK, 'Save settings')
    cancel = wx.Button(dialog, wx.ID_CANCEL, 'Cancel')
    buttons.Add(save, 0, wx.RIGHT, 8)
    buttons.Add(cancel)
    layout.Add(buttons, 0, wx.ALL, 10)
    dialog.SetSizer(layout)
    dialog.Fit()
    dialog.SetMinSize(dialog.GetSize())
    dialog.SetEscapeId(wx.ID_CANCEL)
    result = None
    def accepted(event):
        nonlocal result
        try:
            selection = mode.GetSelection()
            if selection not in (0, 1, 2, 3):
                raise ValueError('Choose Off, Beep, Speech or Beep + Speech.')
            if selection in (2, 3) and not speech_label:
                raise ValueError('Select a local progress voice in Audio Studio before enabling speech.')
            value = ProgressSettings(('off', 'beep', 'speech', 'both')[selection], volume.GetValue(),
                                     interval.GetValue(), muted.GetValue(), background.GetValue())
            result = ProgressSettingsStore(root).save(value)
            dialog.EndModal(wx.ID_OK)
        except (OSError, ValueError) as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()
    save.Bind(wx.EVT_BUTTON, accepted)
    mode.SetFocus()
    try:
        return result if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
