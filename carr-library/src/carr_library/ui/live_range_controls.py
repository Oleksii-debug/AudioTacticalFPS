"""Source-range repeat and sleep controls for the existing LiveReader.

The explicit numeric/action layout adapts the reviewed PR152 reader-playback
controls. Live text ranges use source characters rather than an ephemeral WAV
timeline. This view creates no worker, player, native timer or saved authority.
"""
import math
import weakref


def create_live_range_controls(parent, *, length, initial, start_repeat, selected_range,
                               set_sleep, cancel_sleep, stop, status, subscribe,
                               guard=lambda: None, alive=lambda: True, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    if (type(length) is not int or length < 0 or type(initial) is not tuple or len(initial) != 2
            or any(type(value) is not int for value in initial)
            or not 0 <= initial[0] <= initial[1] <= length):
        raise ValueError('Open an exact current source range before configuring repeat.')

    class LiveRangeDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Live text A–B repeat and sleep', size=(750, 650),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id, self._destroy_requested = self.GetId(), False
            self.alive, self.unsubscribe = True, None
            try:
                self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
                parent_id, reference = parent.GetId(), weakref.ref(self)
                def parent_destroyed(event):
                    view = reference()
                    if event.GetId() == parent_id and view is not None:
                        view.dispose()
                    event.Skip()
                parent.Bind(wx.EVT_WINDOW_DESTROY, parent_destroyed)
                guard()
                panel = wx.ScrolledWindow(self, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
                panel.SetScrollRate(0, 12)
                box = wx.BoxSizer(wx.VERTICAL)
                notice = wx.StaticText(panel, label=(
                    'A and B are exact character offsets in the currently reviewed source or reading copy. '
                    'A is included and B is excluded. Repeat may span several real speech chunks, '
                    'but never continues to another source location. Opening these controls starts no audio. '
                    'Stop cancels repeat and sleep. Closing only this window keeps the active reader running. '
                    'The sleep timer counts monotonic elapsed time, including pause and synthesis; '
                    'normal source, audio and session limits still apply.'))
                notice.Wrap(690)
                box.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
                def field(label, value):
                    box.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 3)
                    control = wx.TextCtrl(panel, value=str(value))
                    control.SetName(label)
                    box.Add(control, 0, wx.ALL | wx.EXPAND, 3)
                    return control
                self.a = field('A source character, inclusive', initial[0])
                self.b = field('B source character, exclusive', initial[1])
                self.seconds = field('Sleep timer in seconds, 1 to 86400', 1800)
                self.status = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
                self.status.SetName('Live text repeat and sleep status')
                box.Add(self.status, 0, wx.ALL | wx.EXPAND, 6)
                self.buttons = {}
                row = wx.WrapSizer(wx.HORIZONTAL)
                for name, label, handler in (
                        ('selection', 'Use current selected A–B range', self.use_selection),
                        ('repeat', 'Start A–B text repeat', self.repeat),
                        ('sleep', 'Set sleep timer', self.sleep),
                        ('clear_sleep', 'Cancel sleep timer', self.clear_sleep),
                        ('stop', 'Stop voice, repeat and sleep', self.stop)):
                    button = wx.Button(panel, label=label)
                    button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, handler)
                    self.buttons[name] = button
                    row.Add(button, 0, wx.ALL, 4)
                box.Add(row, 0, wx.EXPAND | wx.ALL, 3)
                panel.SetSizer(box)
                panel.FitInside()
                layout = wx.BoxSizer(wx.VERTICAL)
                layout.Add(panel, 1, wx.EXPAND)
                self.close_button = wx.Button(self, wx.ID_CANCEL, 'Close repeat and sleep controls')
                self.close_button.SetName('Close repeat and sleep controls')
                self.close_button.Bind(wx.EVT_BUTTON, self.close)
                layout.Add(self.close_button, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
                self.SetSizer(layout)
                self.SetEscapeId(wx.ID_CANCEL)
                self.Bind(wx.EVT_CLOSE, self.close)
                self.unsubscribe = subscribe(self.refresh)
                self.refresh()
                self.a.SetFocus()
            except BaseException:
                self.Destroy()
                raise

        def current(self):
            return self.alive and alive()

        def dispose(self):
            if not self.alive:
                return
            self.alive = False
            if self.unsubscribe is not None:
                self.unsubscribe()
                self.unsubscribe = None

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if self._destroy_requested:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def close(self, event=None):
            if not self.alive:
                return
            self.dispose()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def report(self, message):
            if self.current():
                self.status.ChangeValue(str(message))

        def refresh(self):
            if not self.current():
                return
            value = status()
            message = 'No active A–B repeat. Ordinary reading never repeats automatically.'
            if value['repeat_range'] is not None:
                ordinal, first, last = value['repeat_range']
                message = f'Location {ordinal}; exact A–B characters {first} to {last}; pass {value["repeat_pass"]}.'
            if value['sleep_remaining'] is not None:
                message += f' Sleep in at most {math.ceil(value["sleep_remaining"])} seconds; owned cleanup follows cancellation.'
            self.status.ChangeValue(message)
            self.buttons['repeat'].Enable(not value['active'])
            self.buttons['selection'].Enable(not value['active'])
            self.buttons['sleep'].Enable(value['active'])
            self.buttons['clear_sleep'].Enable(value['sleep_remaining'] is not None)
            self.buttons['stop'].Enable(value['active'])

        def action(self, operation):
            if not self.current():
                return
            try:
                guard()
                operation()
                self.refresh()
            except Exception as exc:
                self.report(exc)

        def bounds(self):
            first, last = self.a.GetValue().strip(), self.b.GetValue().strip()
            if (not first.isascii() or not last.isascii() or not first.isdecimal()
                    or not last.isdecimal() or len(first) > 12 or len(last) > 12):
                raise ValueError('Enter whole source-character offsets for A and B.')
            first, last = int(first), int(last)
            if not 0 <= first < last <= length:
                raise ValueError(f'Choose a nonempty exact A–B range within 0 to {length}.')
            return first, last

        def repeat(self, event=None):
            self.action(lambda: start_repeat(*self.bounds()))

        def use_selection(self, event=None):
            def use():
                first, last = selected_range()
                if (type(first) is not int or type(last) is not int
                        or not 0 <= first < last <= length):
                    raise ValueError('Choose a nonempty selection in the same exact source.')
                self.a.ChangeValue(str(first)); self.b.ChangeValue(str(last))
            self.action(use)

        def sleep(self, event=None):
            def apply():
                raw = self.seconds.GetValue().strip()
                if len(raw) > 64:
                    raise ValueError('Enter a finite sleep duration in seconds.')
                seconds = float(raw)
                if not math.isfinite(seconds) or not 1 <= seconds <= 86400:
                    raise ValueError('Choose a finite sleep duration from 1 second to 24 hours.')
                set_sleep(seconds)
            self.action(apply)

        def clear_sleep(self, event=None):
            self.action(cancel_sleep)

        def stop(self, event=None):
            self.action(stop)

    return LiveRangeDialog()


def show_live_range_controls(parent, **options):
    dialog = create_live_range_controls(parent, **options)
    try:
        dialog.ShowModal()
    finally:
        if dialog:
            dialog.Destroy()
