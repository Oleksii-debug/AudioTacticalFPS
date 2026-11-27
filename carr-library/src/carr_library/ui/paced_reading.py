"""Optional timed presentation of the current reader's exact source text."""
from time import monotonic

from ..reader.pacing import MAX_TEXT_CHARACTERS, PaceOptions, PacedText
from .reader_appearance import ReaderAppearance


def create_paced_reader(parent, root, text, selection, *, current_text, highlight,
                        restore_selection, guard=lambda: None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    if type(text) is not str or not 0 < len(text) <= MAX_TEXT_CHARACTERS or '\0' in text:
        raise ValueError('Choose a current text section of at most 200,000 characters.')
    try:
        text.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Paced reading requires valid Unicode source text.') from exc
    if (type(selection) not in (tuple, list) or len(selection) != 2
            or any(type(value) is not int for value in selection)
            or not 0 <= selection[0] <= selection[1] <= len(text)
            or any(not callable(value) for value in (current_text, highlight, restore_selection, guard))):
        raise ValueError('Paced reading requires the current exact reader selection.')

    class PacedDialog(wx.Dialog):
        def __init__(self):
            guard()
            super().__init__(parent, title='Paced reading — exact source text', size=(760, 640),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.wx = wx
            self._native_id = self.GetId()
            self.alive, self.invalid, self._destroy_requested = True, False, False
            self.model = self.timer = self.appearance = None
            try:
                self._build()
            except BaseException:
                self.dispose()
                super().Destroy()
                raise

        def _check(self):
            if not self.alive or not self or not parent:
                raise ValueError('The source reader has closed.')
            guard()
            if current_text() != text:
                raise ValueError('The source text changed. Reopen paced reading.')

        def _build(self):
            self._check()
            panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
            panel.SetScrollRate(0, 12)
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(panel, label='Optional visual pacing; no speech or text changes. '
                'Words are exact whitespace-delimited source tokens. Start is explicit. '
                'A delayed timer never skips unseen groups. Close restores the earlier selection.')
            notice.Wrap(690)
            box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
            def field(label, factory=wx.TextCtrl, **kwargs):
                box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
                control = factory(panel, **kwargs)
                control.SetName(label.replace('&', ''))
                box.Add(control, 0, wx.ALL | wx.EXPAND, 4)
                return control
            self.range = field('Source &range', wx.Choice,
                choices=['Selected text', 'From the initial cursor', 'Whole current text section'])
            self.range.SetSelection(0 if selection[0] != selection[1] else 1)
            self.rate = field('&Words per minute', wx.SpinCtrl, min=40, max=1200, initial=240)
            self.group = field('Words per &group', wx.SpinCtrl, min=1, max=5, initial=1)
            self.preview = field('Current exact source group', value='Press Start or Next.',
                                 style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 180))
            self.status = field('Pacing status', value='Ready; the source selection is unchanged.',
                                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 85))
            row = wx.WrapSizer(wx.HORIZONTAL)
            self.buttons = {}
            for key, label, action in (
                    ('start', '&Start / resume', self.on_start), ('pause', '&Pause', self.on_pause),
                    ('previous', 'Pre&vious', lambda event: self.on_step(-1)),
                    ('next', '&Next', lambda event: self.on_step(1)),
                    ('stop', 'S&top and rewind', self.on_stop), ('close', '&Close', self.on_close)):
                button = wx.Button(panel, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label)
                button.SetName(label.replace('&', ''))
                button.Bind(wx.EVT_BUTTON, action)
                row.Add(button, 0, wx.ALL, 4)
                self.buttons[key] = button
            box.Add(row, 0, wx.EXPAND)
            panel.SetSizer(box)
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(panel, 1, wx.EXPAND)
            self.SetSizer(layout)
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.range.Bind(wx.EVT_CHOICE, self.on_change)
            for control in (self.rate, self.group):
                control.Bind(wx.EVT_SPINCTRL, self.on_change)
                control.Bind(wx.EVT_TEXT, self.on_change)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
            self.appearance = ReaderAppearance(self, self.preview, root,
                guard=self._check, alive=lambda: self.alive, report=self.report, wx_module=wx)
            panel.FitInside()
            self._controls()
            if self.appearance.notice:
                self.report(self.appearance.notice)
            self.buttons['start'].SetFocus()

        def report(self, value):
            if self.alive and self:
                self.status.ChangeValue(str(value))

        def _controls(self):
            if not self.alive:
                return
            playing = self.model is not None and self.model.state == 'playing'
            self.buttons['start'].Enable(not playing and not self.invalid)
            self.buttons['pause'].Enable(playing)
            for key in ('previous', 'next'):
                self.buttons[key].Enable(not self.invalid)
            self.buttons['stop'].Enable(self.model is not None)

        def _model(self):
            self._check()
            if self.invalid:
                raise ValueError('Reopen paced reading for the current source.')
            if self.model is None:
                selected = self.range.GetSelection()
                if selected == 0:
                    first, last = selection
                elif selected == 1:
                    first, last = selection[0], len(text)
                elif selected == 2:
                    first, last = 0, len(text)
                else:
                    raise ValueError('Choose an explicit source range.')
                self.model = PacedText(text, first, last,
                    PaceOptions(self.rate.GetValue(), self.group.GetValue()))
            return self.model

        def _show(self, frame):
            self._check()
            highlight(frame.start, frame.end)
            self.preview.ChangeValue(frame.text)
            self.report(f'Group {frame.index + 1} of {frame.total}; source characters '
                        f'{frame.start + 1}–{frame.end}. {self.model.state.capitalize()}.')
            self._controls()

        def _error(self, error):
            if self.model is not None:
                self.model.pause()
            if self.timer is not None:
                self.timer.Stop()
            self.report(error)
            self._controls()

        def on_start(self, event=None):
            if not self.alive:
                return
            try:
                self._show(self._model().play(monotonic()))
                self.timer.Start(25)
            except (OSError, ValueError, RuntimeError) as exc:
                self._error(exc)

        def on_pause(self, event=None):
            if not self.alive:
                return
            if self.model is not None:
                self.model.pause()
                self.timer.Stop()
                self.report('Paused at the current exact source group. Start resumes here.')
                self._controls()

        def on_step(self, direction):
            if not self.alive:
                return
            try:
                self.timer.Stop()
                self._show(self._model().step(direction))
            except (OSError, ValueError, RuntimeError) as exc:
                self._error(exc)

        def on_tick(self, event=None):
            if not self.alive or self.model is None or self.model.state != 'playing':
                return
            now = monotonic()
            if now < self.model.deadline:
                return
            try:
                self._check()
                frame = self.model.tick(now)
                if frame is not None:
                    self._show(frame)
                elif self.model.state == 'complete':
                    self.timer.Stop()
                    self.report('The chosen range is complete. No source words were skipped.')
                    self._controls()
            except (OSError, ValueError, RuntimeError) as exc:
                self.invalid = True
                self._error(exc)

        def on_stop(self, event=None):
            if not self.alive:
                return
            if self.timer is not None:
                self.timer.Stop()
            if self.model is not None:
                self.model.stop()
            self.model = None
            try:
                self._check()
                restore_selection()
                self.report('Stopped and rewound; the earlier reader selection is restored.')
            except (OSError, ValueError, RuntimeError) as exc:
                self.invalid = True
                self.report(exc)
            self._controls()

        def on_change(self, event=None):
            if self.alive:
                self.on_stop()
                if not self.invalid:
                    self.report('Settings changed. Press Start to use the reviewed range and pace.')

        def dispose(self):
            if not self.alive:
                return
            if self.timer is not None:
                self.timer.Stop()
            if self.model is not None:
                self.model.stop()
            self.alive = False
            if self.appearance is not None:
                self.appearance.close()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self.dispose()
            event.Skip()

        def on_close(self, event=None):
            if self.alive:
                self.on_stop()
            self.dispose()
            if self and self.IsModal():
                self.EndModal(wx.ID_CANCEL)

        def Destroy(self):
            self.dispose()
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

    return PacedDialog()


def show_paced_reader(parent, root, text, selection, *, on_ready=None, **kwargs):
    dialog = create_paced_reader(parent, root, text, selection, **kwargs)
    try:
        if on_ready is not None:
            dialog.wx.CallAfter(on_ready, dialog)
        dialog.ShowModal()
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()
