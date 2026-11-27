"""Native optional visual pacing and clearly labelled approximate audio follow."""
from time import monotonic

from ..speech.focus import VisualPacer
from ..speech.navigation import native_cursor_position


def create_reading_focus(parent, *, plan, cursor, guard, authorize, player=None, audio=None,
                         pause_audio=lambda: None, stop_audio=lambda: None, goto=lambda _offset: None,
                         wx_module=None, clock=monotonic):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class ReadingFocus(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Reading focus and audio follow', size=(820, 650),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self.following, self.shown = True, False, None
            self.pacer, self.span = VisualPacer(plan, cursor), None
            self.buttons, self.timer = {}, wx.Timer(self)
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Paced visual reading starts only when you press Start. Audio follow estimates word position '
                'from measured recording duration; this voice has no exact word timings. '
                'Text boundaries are navigation aids. Original text is unchanged.'))
            notice.Wrap(780)
            layout.Add(notice, 0, wx.ALL | wx.EXPAND, 8)
            self.mode = wx.Choice(self, choices=['Paced visual reading', 'Audio follow — approximate'])
            self.mode.SetName('Reading focus mode')
            self.mode.SetSelection(0)
            layout.Add(self.mode, 0, wx.ALL | wx.EXPAND, 8)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(wx.StaticText(self, label='Visual words per minute (40 to 1000)'), 0, wx.ALL, 6)
            self.speed = wx.SpinCtrl(self, min=40, max=1000, initial=250)
            self.speed.SetName('Visual reading words per minute')
            row.Add(self.speed, 0, wx.ALL, 6)
            layout.Add(row, 0, wx.EXPAND)
            self.word = wx.TextCtrl(self, style=wx.TE_READONLY)
            self.word.SetName('Current visual reading word')
            font = self.word.GetFont()
            font.SetPointSize(24)
            self.word.SetFont(font)
            layout.Add(self.word, 0, wx.ALL | wx.EXPAND, 8)
            self.preview = wx.TextCtrl(self, value=plan.text,
                style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_RICH2 | wx.TE_NOHIDESEL)
            self.preview.SetName('Reading focus text with highlighted word')
            layout.Add(self.preview, 1, wx.ALL | wx.EXPAND, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 75))
            self.status.SetName('Reading focus status and timing accuracy')
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
            actions = (
                ('start', 'Start visual reading or audio follow', self.on_start),
                ('pause', 'Pause visual reading or audio follow', self.on_pause),
                ('previous', 'Previous visual word', lambda _e: self.move(-1)),
                ('next', 'Next visual word', lambda _e: self.move(1)),
                ('goto', 'Use highlighted cursor and close', self.on_goto),
                ('audio_pause', 'Pause or resume current audio (F7)', self.on_audio_pause),
                ('audio_stop', 'Stop current audio (F8)', self.on_audio_stop),
                ('close', 'Close reading focus', self.on_close),
            )
            for start in (0, 4):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for identity, label, callback in actions[start:start + 4]:
                    short_label = {'start': 'Start visual/follow', 'pause': 'Pause visual/follow',
                        'previous': 'Previous word', 'next': 'Next word', 'goto': 'Use cursor and close',
                        'audio_pause': 'Pause/resume audio (F7)', 'audio_stop': 'Stop audio (F8)',
                        'close': 'Close'}[identity]
                    button = wx.Button(self, label=short_label)
                    button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, callback)
                    self.buttons[identity] = button
                    row.Add(button, 0, wx.ALL, 4)
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout)
            self.mode.Bind(wx.EVT_CHOICE, self.on_mode)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
            accelerators = []
            for key, callback in ((wx.WXK_F7, self.on_audio_pause), (wx.WXK_F8, self.on_audio_stop)):
                identity = wx.NewIdRef()
                self.Bind(wx.EVT_MENU, callback, id=int(identity))
                accelerators.append((0, key, int(identity)))
            self.SetAcceleratorTable(wx.AcceleratorTable(accelerators))
            try:
                self.check()
                self.display(self.pacer.index, plan.spans[self.pacer.index], 'Paused visual reading.')
                self.timer.Start(100)
                self.buttons['start'].SetFocus()
            except BaseException:
                self.alive = False
                try:
                    self.timer.Stop()
                finally:
                    self.Destroy()
                raise

        def check(self, *, full=False):
            if not self.alive:
                raise ValueError('Reading focus has closed.')
            guard()
            plan.check(plan.document)
            if full:
                authorize()

        def display(self, index, span, note):
            state = (index, span, note)
            if state == self.shown:
                return
            self.shown, self.span = state, span
            first, last = span
            self.word.ChangeValue(plan.text[first:last])
            start = native_cursor_position(self.preview, plan.text, first)
            end = native_cursor_position(self.preview, plan.text, last)
            self.preview.SetSelection(start, end)
            self.preview.ShowPosition(end)
            self.status.ChangeValue(note + ' Reading-copy characters ' + str(first + 1) + '–' + str(last)
                + '. ' + plan.source_label(span))
            # Timer updates never steal keyboard focus from the user's control.

        def error(self, exc):
            self.pacer.running, self.following = False, False
            if self.alive:
                self.status.ChangeValue(str(exc))

        def on_start(self, event=None):
            if not self.alive:
                return
            try:
                self.check(full=True)
                if self.mode.GetSelection() == 0:
                    self.following = False
                    self.pacer.start(clock(), self.speed.GetValue())
                else:
                    if audio is None or player is None:
                        raise ValueError('Render this exact reading text once, then reopen focus for audio follow.')
                    audio.check(plan, player.path)
                    self.pacer.running, self.following = False, True
                self.on_tick()
            except Exception as exc:
                self.error(exc)

        def on_pause(self, event=None):
            if not self.alive:
                return
            try:
                self.check()
                self.pacer.pause(clock())
                was_following, self.following = self.following, False
                span = self.span if was_following and self.span is not None else plan.spans[self.pacer.index]
                self.display(self.pacer.index, span,
                    'Audio follow paused.' if was_following else 'Paused visual reading.')
            except Exception as exc:
                self.error(exc)

        def on_mode(self, event=None):
            self.on_pause(event)

        def move(self, direction):
            if not self.alive:
                return
            try:
                self.check(full=True)
                self.following = False
                index = self.pacer.move(direction)
                self.display(index, plan.spans[index], 'Paused visual reading.')
            except Exception as exc:
                self.error(exc)

        def on_tick(self, event=None):
            if not self.alive:
                return
            try:
                self.check()
                if self.following:
                    index, span = audio.span_at(plan, player.path, player.position_seconds)
                    self.pacer.index = index
                    note = 'Approximate audio follow; no engine word timings. Playback is ' + player.state + '.'
                    self.display(index, span, note)
                elif self.pacer.running:
                    index = self.pacer.tick(clock())
                    note = 'Paced visual reading.' if self.pacer.running else 'Visual reading finished.'
                    self.display(index, plan.spans[index], note)
            except Exception as exc:
                self.error(exc)

        def on_audio_pause(self, event=None):
            if not self.alive:
                return
            try:
                self.check(full=True)
                if audio is None or player is None:
                    raise ValueError('There is no current matching reading audio.')
                audio.check(plan, player.path)
                pause_audio()
            except Exception as exc:
                self.error(exc)

        def on_audio_stop(self, event=None):
            if not self.alive:
                return
            try:
                self.check(full=True)
                stop_audio()
            except Exception as exc:
                self.error(exc)

        def on_goto(self, event=None):
            if not self.alive:
                return
            try:
                self.check(full=True)
                if self.span is not None:
                    goto(self.span[0])
                self.on_close()
            except Exception as exc:
                self.error(exc)

        def on_close(self, event=None):
            if self.alive:
                self.alive, self.pacer.running, self.following = False, False, False
                try:
                    self.timer.Stop()
                finally:
                    self.EndModal(wx.ID_CANCEL)

    return ReadingFocus()


def show_reading_focus(parent, **options):
    dialog = create_reading_focus(parent, **options)
    try:
        dialog.ShowModal()
    finally:
        dialog.alive = False
        try:
            dialog.timer.Stop()
        finally:
            dialog.Destroy()
