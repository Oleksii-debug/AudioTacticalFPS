"""Keyboard controls for the existing playback owner.

The host supplies its current worker and native player. This dialog owns only a
request cancellation latch; selecting settings never starts sound.
"""
from __future__ import annotations

from ..speech.listening import ListeningSettings
from ..speech._wave_output import PlaybackLevels


def create_playback_settings_dialog(parent, *, settings, levels, prepare, apply_levels,
        mix_available, alive=lambda: True, play=None, stop=None, wx_module=None,
        preparation_label='Stop and prepare listening copy',
        notice='Preparing speed or pitch stops this preview and clears repeat and sleep. '
               'Start playback explicitly after preparation.'):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    if type(settings) is not ListeningSettings or type(levels) is not PlaybackLevels:
        raise ValueError('Playback controls require validated current settings.')

    class PlaybackSettingsDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Independent playback settings', size=(740, 680),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self.alive, self.pending, self._destroy_requested = True, False, False
            self._cancel_preparation = None
            self.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)
            self.Bind(wx.EVT_CLOSE, self.close)
            outer = wx.BoxSizer(wx.VERTICAL)
            body = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
            body.SetScrollRate(0, 12)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(body, label=(
                'These are playback settings. Synthesis rate, voice volume, the original '
                'recording and exported files stay unchanged. ' + notice)),
                0, wx.EXPAND | wx.ALL, 6)
            self.fields, self.buttons = {}, {}
            def field(name, label, value):
                layout.Add(wx.StaticText(body, label=label), 0, wx.ALL, 4)
                control = wx.TextCtrl(body, value=format(value, '.12g'))
                control.SetName(label)
                layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
                self.fields[name] = control
            field('speed', 'Playback speed, 0.25 to 4; independent of synthesis rate', settings.speed)
            field('pitch', 'Playback pitch, minus 12 to 12 semitones; independent of speed',
                  settings.pitch_semitones)
            def button(name, label, callback):
                control = wx.Button(body, label=label)
                control.SetName(label)
                control.Bind(wx.EVT_BUTTON, callback)
                layout.Add(control, 0, wx.ALL, 4)
                self.buttons[name] = control
            button('prepare', preparation_label, self.prepare)
            button('cancel', 'Cancel listening-copy preparation', self.cancel)
            button('reset', 'Enter original playback speed and pitch', self.reset)
            field('volume', 'This player volume, 0 to 100', levels.volume)
            field('balance', 'Stereo balance, minus 100 left to 100 right', levels.balance * 100)
            self.muted = wx.CheckBox(body, label='Mute this player only')
            self.muted.SetName('Mute this player only')
            self.muted.SetValue(levels.muted)
            layout.Add(self.muted, 0, wx.ALL, 4)
            button('levels', 'Apply player volume, mute and balance', self.levels)
            layout.Add(wx.StaticText(body, label=(
                'Changes only this player’s PCM buffers. Already queued audio briefly keeps '
                'its previous levels. Balance requires stereo.'
                if mix_available else 'Live volume, mute and balance are unavailable on this backend.')),
                0, wx.EXPAND | wx.ALL, 4)
            if play is not None:
                button('play', 'Play current listening copy', self.play)
            if stop is not None:
                button('stop', 'Stop current playback', self.stop)
            self.status = wx.TextCtrl(body, value='No settings have been applied by this window.',
                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 90))
            self.status.SetName('Independent playback settings status')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
            body.SetSizer(layout)
            body.FitInside()
            outer.Add(body, 1, wx.EXPAND)
            close = wx.Button(self, wx.ID_CANCEL, 'Close playback settings')
            close.SetName('Close playback settings')
            close.Bind(wx.EVT_BUTTON, self.close)
            outer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
            self.SetSizer(outer)
            self.SetEscapeId(wx.ID_CANCEL)
            self._enable()

        def current(self):
            return self.alive and alive() and bool(self)

        def message(self, text):
            if self.current():
                self.status.ChangeValue(str(text))
                self.status.SetFocus()

        def _enable(self):
            if not self.current():
                return
            for key in ('speed', 'pitch'):
                self.fields[key].Enable(not self.pending)
            for key in ('prepare', 'reset', 'play'):
                if key in self.buttons:
                    self.buttons[key].Enable(not self.pending)
            self.buttons['cancel'].Enable(self.pending)
            for key in ('volume', 'balance'):
                self.fields[key].Enable(bool(mix_available) and not self.pending)
            self.muted.Enable(bool(mix_available) and not self.pending)
            self.buttons['levels'].Enable(bool(mix_available) and not self.pending)

        def reset(self, event=None):
            if self.current() and not self.pending:
                self.fields['speed'].ChangeValue('1')
                self.fields['pitch'].ChangeValue('0')
                self.message('Original speed and pitch entered. Apply them explicitly when ready.')

        def prepare(self, event=None):
            if not self.current() or self.pending:
                return
            try:
                selected = ListeningSettings(float(self.fields['speed'].GetValue()),
                                             float(self.fields['pitch'].GetValue()))
                self.pending = True
                self._enable()
                self.message('Applying playback settings. No automatic playback will start.')
                def completed(message='Playback settings are ready. Start reading or playback explicitly.'):
                    if not self.current():
                        return
                    self.pending, self._cancel_preparation = False, None
                    self._enable()
                    self.message(message)
                def failed(error):
                    if not self.current():
                        return
                    self.pending, self._cancel_preparation = False, None
                    self._enable()
                    self.message(error)
                cancel = prepare(selected, completed, failed, active=lambda: self.alive and alive())
                if self.pending:
                    if not callable(cancel):
                        raise RuntimeError('Playback preparation was not admitted by its owner.')
                    self._cancel_preparation = cancel
            except (Exception, KeyboardInterrupt) as exc:
                self.pending, self._cancel_preparation = False, None
                self._enable()
                self.message(exc)

        def cancel(self, event=None):
            if self.current() and self.pending:
                if self._cancel_preparation is not None:
                    self._cancel_preparation()
                self.message('Cancellation requested. Waiting for the existing audio worker to finish cleanup.')

        def levels(self, event=None):
            if not self.current() or self.pending:
                return
            try:
                if not mix_available:
                    raise RuntimeError('Live volume, mute and balance are unavailable on this backend.')
                selected = PlaybackLevels(float(self.fields['volume'].GetValue()),
                    bool(self.muted.GetValue()), float(self.fields['balance'].GetValue()) / 100)
                apply_levels(selected)
                self.message('Player level request accepted. Queued audio may retain its previous levels briefly.')
            except (Exception, KeyboardInterrupt) as exc:
                self.message(exc)

        def play(self, event=None):
            if self.current() and not self.pending:
                try:
                    play()
                except (Exception, KeyboardInterrupt) as exc:
                    self.message(exc)

        def stop(self, event=None):
            if self.current():
                try:
                    if self._cancel_preparation is not None:
                        self._cancel_preparation()
                    stop()
                    self.message('Stop requested; waiting for the playback owner to finish.')
                except (Exception, KeyboardInterrupt) as exc:
                    self.message(exc)

        def dispose(self):
            if not self.alive:
                return
            self.alive = False
            if self._cancel_preparation is not None:
                self._cancel_preparation()

        def _destroyed(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def Destroy(self):
            if self._destroy_requested:
                return False
            self._destroy_requested = True
            self.dispose()
            return super().Destroy() if self else False

        def close(self, event=None):
            if not self.current():
                return
            self.dispose()
            if self and self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            elif self:
                self.Destroy()

    return PlaybackSettingsDialog()


def show_playback_settings(parent, **options):
    dialog = create_playback_settings_dialog(parent, **options)
    try:
        dialog.ShowModal()
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()
