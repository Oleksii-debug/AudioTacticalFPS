"""Native A/B and sleep controls for the reader's existing WavePlayer."""
import math


class ReaderPlaybackControls:
    POLL_MS = 100

    def __init__(self, parent, layout, player, *, start, guard, cancelled,
                 alive, report, wx=None):
        if wx is None:
            import wx
        self.wx, self.parent, self.player = wx, parent, player
        self._start, self._guard = start, guard
        self._cancelled, self._alive, self._report = cancelled, alive, report
        self._closed = self._scope_failed = self._pending = False
        self._sequence = 0
        self._error_reported = False
        self._session = player.session_generation
        self._a = self._b = None
        self._last_status = None
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.mark_a = self._button(row, 'Mark A at current position', self.set_a)
        self.mark_b = self._button(row, 'Mark B at current position', self.set_b)
        self.times = self._button(row, 'Set A/B times...', self.set_times)
        self.repeat = wx.CheckBox(parent, label='Repeat A to B')
        self.repeat.SetName('Enable A/B repeat')
        self.repeat.SetValue(False)
        self.repeat.Bind(wx.EVT_CHECKBOX, self.toggle_repeat)
        row.Add(self.repeat, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        self.clear = self._button(row, 'Clear A/B', self.clear_loop)
        layout.Add(row, 0, wx.EXPAND)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.sleep = self._button(row, 'Sleep timer...', self.set_sleep)
        self.query = self._button(row, 'Repeat and sleep status', self.query_status)
        layout.Add(row, 0, wx.EXPAND)
        self.status = wx.TextCtrl(parent, style=wx.TE_READONLY)
        self.status.SetName('A/B repeat and sleep timer status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 6)
        self._sync()
        self.timer = wx.Timer(parent)
        parent.Bind(wx.EVT_TIMER, self.poll, self.timer)
        self.timer.Start(self.POLL_MS)

    def _button(self, row, label, handler):
        control = self.wx.Button(self.parent, label=label)
        control.SetName(label)
        control.Bind(self.wx.EVT_BUTTON, handler)
        row.Add(control, 0, self.wx.ALL, 4)
        return control

    def _check(self):
        if self._closed or not self._alive():
            raise ValueError('The reader is closed.')
        if self._scope_failed:
            raise ValueError('The reading source changed. Open the reader again.')
        self._guard()
        if self._cancelled():
            raise InterruptedError('Playback is cancelled. Read again to begin a new session.')

    def _session_changed(self):
        if self._session != self.player.session_generation:
            self._session = self.player.session_generation
            self._sequence += 1
            self._pending = False
            self._a = self._b = None
            self._error_reported = False
            return True
        return False

    def _describe(self, *, position=False):
        a = 'unset' if self._a is None else f'{self._a:.3f} seconds'
        b = 'unset' if self._b is None else f'{self._b:.3f} seconds'
        remaining = self.player.sleep_remaining_seconds
        sleep = 'Off' if remaining is None else f'{math.ceil(remaining)} seconds remaining, including pauses'
        text = (f'A: {a}; B: {b}. Repeat {"On" if self.player.repeat_enabled else "Off"}; '
                f'{self.player.repeat_count} completed repeats. Sleep timer: {sleep}.')
        if position and self.player.path is not None:
            text += (f' Playback {self.player.state}; {self.player.position_seconds:.3f} of '
                     f'{self.player.duration_seconds:.3f} seconds.')
            if self.player.position_is_approximate:
                text += ' Position is an estimate on this playback backend.'
        return text

    def _sync(self):
        if self._closed or not self._alive():
            return
        available = bool(self.player.path is not None and not self._scope_failed and not self._cancelled())
        for control in (self.mark_a, self.mark_b, self.times, self.clear, self.sleep):
            control.Enable(available)
        self.repeat.Enable(available and self._a is not None and self._b is not None)
        self.repeat.SetValue(self.player.repeat_enabled)
        text = self._describe()
        if text != self._last_status:
            self.status.ChangeValue(text)
            self._last_status = text

    def _failure(self, error, *, automatic=False):
        self._pending = False
        if self._closed or not self._alive():
            return
        if automatic:
            self._sequence += 1
            try:
                self.player.stop()
                self._session_changed()
            except Exception as stop_error:
                error = RuntimeError(f'{error} Playback stop was not confirmed: {stop_error}')
        self._sync()
        self._report(str(error), focus=not automatic)

    def _admit(self, action, *, automatic=False):
        """Keep delayed native starts bound to this source, session and action."""
        self._check()
        self._session_changed()
        sequence, session = self._sequence, self._session
        self._pending = True

        def current():
            return (not self._closed and self._alive() and not self._cancelled()
                    and sequence == self._sequence
                    and session == self.player.session_generation)

        def failed(error):
            if current():
                self._failure(error, automatic=automatic)

        def accepted():
            if not current():
                return
            try:
                self._check()
                action()
            finally:
                if current():
                    self._pending = False
                    self._sync()

        try:
            self._start(accepted, on_error=failed)
        except Exception:
            if current():
                self._pending = False
            raise

    def _new_action(self):
        self._check()
        self._session_changed()
        self._sequence += 1
        self._pending = False

    def set_a(self, event=None):
        try:
            self._new_action()
            position = self.player.position_seconds
            if not 0 <= position < self.player.duration_seconds:
                raise ValueError('Point A must be before the end of this recording.')
            def apply():
                self.player.clear_ab_loop()
                self._a, self._b = position, None
            self._admit(apply)
        except Exception as exc:
            self._failure(exc)

    def set_b(self, event=None):
        try:
            self._new_action()
            if self._a is None:
                raise ValueError('Set point A before point B.')
            position = self.player.position_seconds
            def apply():
                self.player.set_ab_loop(self._a, position)
                self._b = position
            self._admit(apply)
        except Exception as exc:
            self._failure(exc)

    def set_times(self, event=None):
        try:
            self._new_action()
            initial = f'{self._a or 0:g}, {self._b if self._b is not None else self.player.duration_seconds:g}'
            with self.wx.TextEntryDialog(self.parent,
                f'Enter A and B in seconds, separated by a comma. '
                f'Use 0 <= A < B <= {self.player.duration_seconds:.3f}. Setting points does not enable repeat.',
                'Set A/B playback times', initial) as entry:
                if entry.ShowModal() != self.wx.ID_OK:
                    return
                values = entry.GetValue().split(',')
            if len(values) != 2:
                raise ValueError('Enter two times, for example 0.5, 4.5.')
            start, end = (float(value.strip()) for value in values)
            # The modal can outlive source authorization or a cancelled session.
            self._check()
            def apply():
                self.player.set_ab_loop(start, end)
                self._a, self._b = start, end
            self._admit(apply)
        except Exception as exc:
            self._failure(exc)

    def toggle_repeat(self, event=None):
        try:
            self._new_action()
            enabled = bool(self.repeat.GetValue())
            def apply():
                if enabled:
                    self.player.set_ab_loop(self._a, self._b)
                self.player.enable_repeat(enabled)
            self._admit(apply)
        except Exception as exc:
            self._failure(exc)

    def clear_loop(self, event=None):
        try:
            self._new_action()
            def apply():
                self.player.clear_ab_loop()
                self._a = self._b = None
            self._admit(apply)
        except Exception as exc:
            self._failure(exc)

    def set_sleep(self, event=None):
        try:
            self._new_action()
            remaining = self.player.sleep_remaining_seconds
            with self.wx.TextEntryDialog(self.parent,
                'Minutes from now; pauses count. Enter zero to turn the timer off. '
                'At expiry playback pauses, repeat clears, and resume remains an explicit action.',
                'Reader sleep timer', str(round(remaining / 60, 3)) if remaining is not None else '0') as entry:
                if entry.ShowModal() != self.wx.ID_OK:
                    return
                minutes = float(entry.GetValue().strip())
            self._check()
            if not math.isfinite(minutes) or minutes < 0:
                raise ValueError('Choose zero or a positive finite number of minutes.')
            self.player.set_sleep_timer(minutes * 60 if minutes else None)
            self._sync()
        except Exception as exc:
            self._failure(exc)

    def query_status(self, event=None):
        try:
            self._check()
            self._session_changed()
            self._sync()
            self.status.ChangeValue(self._describe(position=True))
            self.status.SetFocus()
        except Exception as exc:
            self._failure(exc)

    def poll(self, event=None):
        if self._closed:
            return
        if not self._alive():
            self.close()
            return
        self._session_changed()
        if self._scope_failed:
            return
        try:
            if self.player.path is not None:
                self._guard()
            if self._cancelled():
                if self.player.repeat_enabled or self.player.sleep_remaining_seconds is not None or self._pending:
                    self._sequence += 1
                    self._pending = False
                    self.player.stop()
                    self._session_changed()
                self._sync()
                return
            result = self.player.poll(restart=False)
            if result == 'sleep':
                self._sequence += 1
                self._pending = False
                self._a = self._b = None
                self._report(f'Sleep timer expired. Playback is {self.player.state}; '
                             'repeat is off. F7 resumes paused playback.', focus=False)
            elif result == 'repeat_due' and not self._pending:
                self._admit(self.player.restart_repeat, automatic=True)
            elif result == 'error' and not self._error_reported:
                self._error_reported = True
                self._sequence += 1
                self._pending = False
                self._a = self._b = None
                self._report('The playback device stopped. Repeat and the sleep timer are off.', focus=False)
            self._sync()
        except Exception as exc:
            self._sequence += 1
            self._pending = False
            self._scope_failed = True
            self._failure(RuntimeError(f'Playback controls were disarmed; '
                                      f'open this reader again. {exc}'), automatic=True)

    def close(self, *, stop=True):
        if self._closed:
            return
        self._closed = True
        self._sequence += 1
        self._pending = False
        try:
            self.timer.Stop()
        finally:
            if stop:
                self.player.stop()
