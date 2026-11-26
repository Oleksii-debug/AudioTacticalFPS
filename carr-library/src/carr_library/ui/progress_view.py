"""Named native gauges with throttled updates and explicit local sound options."""
import os
import threading
import time

from ..progress import (ProgressSettings, ProgressSettingsStore, ProgressTonePolicy,
                        ProgressTracker, progress_audio_suppressed,
                        request_progress_tone, set_media_active)

REFRESH_MS = 250
QUERY_SHORTCUT = 'Ctrl+Alt+P'


class ProgressView:
    def __init__(self, parent, *, settings_root, label='Operation progress', wx_module=None,
                 clock=time.monotonic, tone_sender=request_progress_tone):
        if wx_module is None:
            import wx as wx_module
        wx = self.wx = wx_module
        self.root = settings_root if callable(settings_root) else lambda: settings_root
        self.clock, self.tone_sender = clock, tone_sender
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
            self.settings = ProgressSettingsStore(self.root()).load()
            self.notice = ''
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
            cue = self.policy.choose(snapshot, self.settings, active=self._active,
                                     suppressed=self._media_active or progress_audio_suppressed())
            if cue is not None:
                tracker = self.tracker
                settings = self.settings
                def still_current():
                    return (not self.closed and self.tracker is tracker and self.audible
                            and self.settings == settings and settings.mode == 'beep'
                            and not settings.muted and settings.volume > 0
                            and (self._active or self.settings.background)
                            and tracker.snapshot().sequence == snapshot.sequence)
                self.tone_sender(cue, self.settings.volume, still_current)
        return True

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
        selected = show_progress_settings(self.panel, self.root(), self.settings, wx_module=self.wx)
        if selected is not None:
            self.settings, self.notice = selected, ''
            self.refresh(force=True)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.Stop()
        with self._pending_lock:
            self._pending = None
        self.set_media_active(False)


def show_progress_settings(parent, root, current=None, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    current = current or ProgressSettingsStore(root).load()
    dialog = wx.Dialog(parent, title='Progress sound settings', size=(620, 470),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    notice = wx.StaticText(dialog, label='Application tones are off by default. Keep them off when your screen reader '
        'already announces progress. Application speech is not available in this build. '
        'Sounds are suppressed during Audio Studio playback and never enter rendered tracks.' +
        (' Application tones require Windows.' if os.name != 'nt' else ''))
    notice.Wrap(560)
    layout.Add(notice, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Application progress output'), 0, wx.LEFT | wx.RIGHT, 10)
    mode = wx.Choice(dialog, choices=['Off', 'Beep'])
    mode.SetName('Application progress output')
    mode.SetSelection(1 if current.mode == 'beep' else 0)
    layout.Add(mode, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Tone volume, 0 to 100'), 0, wx.LEFT | wx.RIGHT, 10)
    volume = wx.Slider(dialog, value=current.volume, minValue=0, maxValue=100)
    volume.SetName('Progress tone volume')
    layout.Add(volume, 0, wx.EXPAND | wx.ALL, 10)
    layout.Add(wx.StaticText(dialog, label='Minimum interval in seconds'), 0, wx.LEFT | wx.RIGHT, 10)
    interval = wx.SpinCtrl(dialog, min=1, max=60, initial=current.interval_seconds)
    interval.SetName('Minimum progress tone interval in seconds')
    layout.Add(interval, 0, wx.EXPAND | wx.ALL, 10)
    muted = wx.CheckBox(dialog, label='Mute application progress tones')
    muted.SetValue(current.muted)
    background = wx.CheckBox(dialog, label='Allow tones while this window is in the background')
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
            if selection not in (0, 1):
                raise ValueError('Choose Off or Beep.')
            value = ProgressSettings('beep' if selection else 'off', volume.GetValue(),
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
