"""Accessible, session-only routing for the existing reader WavePlayer."""
from ..speech.output_devices import output_devices, resolve_output_device


class ReaderOutputControls:
    def __init__(self, parent, layout, player, *, submit, guard, alive, busy,
                 cancelled, report, invalidate, wx=None):
        if wx is None:
            import wx
        self.wx, self.parent, self.player = wx, parent, player
        self._submit, self._guard = submit, guard
        self._alive, self._busy, self._cancelled = alive, busy, cancelled
        self._report, self._invalidate = report, invalidate
        self._closed = self._pending = False
        self._generation = 0
        self._devices = ()
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(parent, label='Reader output device'), 0,
                wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.choice = wx.Choice(parent, choices=['System default output'])
        self.choice.SetName('Reader output device')
        self.choice.SetSelection(0)
        self.choice.Bind(wx.EVT_CHOICE, lambda event: self.sync())
        row.Add(self.choice, 1, wx.ALL, 4)
        self.refresh = wx.Button(parent, label='Refresh outputs')
        self.refresh.SetName('Refresh reader output devices')
        self.refresh.Bind(wx.EVT_BUTTON, self.refresh_devices)
        row.Add(self.refresh, 0, wx.ALL, 4)
        self.apply = wx.Button(parent, label='Use selected output')
        self.apply.SetName('Use selected reader output device')
        self.apply.Bind(wx.EVT_BUTTON, self.apply_device)
        row.Add(self.apply, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
        self.status = wx.TextCtrl(parent, style=wx.TE_READONLY)
        self.status.SetName('Reader output device status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 4)
        self._describe()
        self.sync()

    def _describe(self, message=None):
        if self._closed or not self._alive():
            return
        if not self.player.output_selection_supported:
            text = ('This playback backend uses its system-default output. '
                    'Explicit per-reader output selection requires Windows MCI.')
        else:
            selected = self.player.output_device
            label = 'System default output' if selected is None else selected.name
            text = (f'Current reader output: {label}. Session only. '
                    'Applying another output pauses this reader; resume or read explicitly. '
                    'System and screen-reader settings are unchanged.')
        self.status.ChangeValue((message + ' ' if message else '') + text)

    def sync(self):
        if self._closed or not self._alive():
            return
        enabled = bool(self.player.output_selection_supported and not self._pending and not self._busy())
        self.refresh.Enable(enabled)
        self.choice.Enable(enabled)
        self.apply.Enable(enabled and self.choice.GetSelection() != self.wx.NOT_FOUND)

    def _check(self):
        if self._closed or not self._alive():
            raise ValueError('The reader is closed.')
        self._guard()
        if self._pending or self._busy():
            raise ValueError('A reader operation is running. Wait or stop it before changing the output.')

    def _request(self, work, complete, *, require_same_audio=False):
        self._check()
        self._pending = True
        self._generation += 1
        generation, session = self._generation, self.player.session_generation
        self.sync()

        def stopped():
            return self._closed or generation != self._generation or self._cancelled()

        def perform():
            try:
                if stopped():
                    raise InterruptedError('Reader output selection cancelled.')
                value = work(stopped)
                if stopped():
                    raise InterruptedError('Reader output selection cancelled.')
                return value, None
            except Exception as exc:
                return None, exc

        def done(result):
            if self._closed or not self._alive() or generation != self._generation:
                return
            self._pending = False
            try:
                self._guard()
                if self._cancelled():
                    raise InterruptedError('Reader output selection cancelled; the earlier output is unchanged.')
                if require_same_audio and session != self.player.session_generation:
                    raise ValueError('Playback changed during output validation. Select the output again.')
                value, error = result
                if error is not None:
                    raise error
                complete(value)
            except Exception as exc:
                self._describe(str(exc))
                self._report(str(exc), focus=True)
            finally:
                self.sync()
        try:
            # This is the reader's existing single worker; explicit refresh or
            # Apply may begin a new operation after Stop, but never begins audio.
            accepted = self._submit(perform, done)
            if accepted is False:
                self._pending = False
                self.sync()
        except Exception:
            self._pending = False
            self.sync()
            raise

    def refresh_devices(self, event=None):
        if self._closed or not self._alive():
            return
        try:
            def refreshed(devices):
                self._devices = tuple(devices)
                self.choice.SetItems(['System default output'] + [item.name for item in self._devices])
                selected = self.player.output_device
                matches = [index + 1 for index, item in enumerate(self._devices)
                           if selected is not None and item.interface == selected.interface]
                self.choice.SetSelection(0 if selected is None else
                                         matches[0] if len(matches) == 1 else self.wx.NOT_FOUND)
                notice = ('Outputs refreshed. Select one explicitly; no audio started.'
                          if selected is None or matches else
                          'The previously selected output is unavailable. Choose another explicitly.')
                self._describe(notice)
                self._report(notice)
            self._request(lambda cancelled: output_devices(cancelled=cancelled), refreshed)
        except Exception as exc:
            self._report(str(exc), focus=True)

    def apply_device(self, event=None):
        if self._closed or not self._alive():
            return
        try:
            selection = self.choice.GetSelection()
            if selection == self.wx.NOT_FOUND or not 0 <= selection <= len(self._devices):
                raise ValueError('Choose an output explicitly before applying it.')
            selected = None if selection == 0 else self._devices[selection - 1]
            def validated(current):
                # The existing admission generation invalidates queued F7,
                # seek and repeat actions before any fallible device stop.
                self._invalidate()
                self.player.set_output_device(current)
                self._describe('Output selected; no audio started.')
                self._report('Reader output selected. Resume or read explicitly. '
                             'A/B repeat and sleep timer are off.', focus=True)
            self._request(
                lambda cancelled: None if selected is None else
                    resolve_output_device(selected, cancelled=cancelled),
                validated, require_same_audio=True)
        except Exception as exc:
            self._report(str(exc), focus=True)

    def close(self):
        # No calls through a possibly invalidated wx owner during destruction.
        self._closed = True
        self._generation += 1
