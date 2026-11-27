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
        self._subscriber = None
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
        callback = getattr(self, '_subscriber', None)
        if callback is not None:
            try:
                callback()
            except Exception:
                self._subscriber = None
                raise

    def subscribe(self, callback):
        """Borrow the existing poll timer; the popup never creates another."""
        if callback is not None and not callable(callback):
            raise TypeError('A playback status callback must be callable.')
        self._subscriber = callback

    def refresh_controls(self):
        self._session_changed()
        points = self.player.ab_loop
        self._a, self._b = points if points is not None else (None, None)
        self._sync()

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
        self._subscriber = None
        self._sequence += 1
        self._pending = False
        try:
            self.timer.Stop()
        finally:
            if stop:
                self.player.stop()


def show_reader_playback(parent, player, *, execute, stop, pause, subscribe, prepare_listening=None,
                         allow_queue=False, review_playlist=None, status_message=lambda: '',
                         queue_changed=lambda: None, save_queue=None, review_saved=None,
                         saved_pending=lambda: False, close_queue=None, discard_queue=lambda: None,
                         review_outputs=None, guard=lambda: None, active=lambda: True):
    import wx

    if type(allow_queue) is not bool or allow_queue and not callable(review_playlist):
        raise ValueError('Standalone queue controls require an owned review callback.')
    if save_queue is not None and (not allow_queue or not callable(save_queue)
            or not callable(review_saved) or not callable(saved_pending)):
        raise ValueError('Saved queues need their existing owned store and review callbacks.')
    dialog = wx.Dialog(parent, title=('Audio playback and saved playlist' if allow_queue
                                     else 'Reader playback controls'), size=(700, 740),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog_id = dialog.GetId()
    destroyed = False
    outer = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(panel, label=(
        'Controls apply to the current rendered recording. Opening this dialog '
        'does not start audio. Stop also cancels pending repeat and sleep.')),
        0, wx.EXPAND | wx.ALL, 6)
    queue_list, queue_buttons, queue_note, queue_seen = None, {}, '', None
    if allow_queue:
        layout.Add(wx.StaticText(panel, label=(
            'Playlist: review explicit local PCM WAV files. Private copies are retained for '
            'recovery and backup. Queue edits never start audio. Play queue arms later items.')),
            0, wx.EXPAND | wx.ALL, 4)
        queue_list = wx.ListBox(panel, choices=[], size=(-1, 120))
        queue_list.SetName('Reviewed session playlist; choose an item explicitly')
        layout.Add(queue_list, 0, wx.EXPAND | wx.ALL, 4)
        row = wx.GridSizer(cols=3, hgap=4, vgap=4)
        for key, label in (
                ('replace', 'Replace session playlist...'), ('add', 'Add files to session playlist...'),
                ('select', 'Stop and select item'), ('play', 'Play queue from selected item'),
                ('up', 'Stop and move item up'), ('down', 'Stop and move item down'),
                ('remove', 'Stop and remove item'), ('clear', 'Stop and clear playlist'),
                ('cancel', 'Cancel playlist review')):
            control = wx.Button(panel, label=label)
            control.SetName(label)
            row.Add(control, 0, wx.EXPAND)
            queue_buttons[key] = control
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 4)
    save_button = saved_button = current_controls = None
    if save_queue is not None:
        current_controls = wx.CheckBox(panel,
            label='Review saved queue using current controls and output instead of saved settings')
        current_controls.SetName('Explicitly replace saved playback settings with current controls')
        current_controls.SetValue(False)
        layout.Add(current_controls, 0, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        save_button = wx.Button(panel, label='Save playback queue (Ctrl+S)')
        save_button.SetName('Save playback queue')
        saved_button = wx.Button(panel, label='Review saved queue without playing')
        saved_button.SetName('Review saved queue without playing')
        row.Add(save_button, 0, wx.ALL, 4)
        row.Add(saved_button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND | wx.ALL, 4)
    outputs, output_note = (None,), ''
    output = reload_outputs = apply_output = None
    if callable(review_outputs):
        layout.Add(wx.StaticText(panel, label='Playback output for this reader session'), 0, wx.ALL, 4)
        output = wx.Choice(panel, choices=['Automatic output chosen by the playback backend'])
        output.SetName('Requested playback output; apply explicitly after selection')
        output.SetSelection(0 if player.output_device is None else -1)
        layout.Add(output, 0, wx.EXPAND | wx.ALL, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        reload_outputs = wx.Button(panel, label='Reload available outputs')
        reload_outputs.SetName('Reload available playback outputs without playing audio')
        apply_output = wx.Button(panel, label='Stop audio and use selected output')
        apply_output.SetName('Stop audio and use selected playback output')
        row.Add(reload_outputs, 0, wx.ALL, 4)
        row.Add(apply_output, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    fields = {}
    mix_button = muted = None
    if callable(getattr(player, 'set_levels', None)):
        levels = player.levels
        for name, label, initial in (
                ('volume', 'This player volume, 0 to 100', levels.volume),
                ('balance', 'This player stereo balance, minus 100 left to 100 right', levels.balance * 100)):
            layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(panel, value=format(initial, '.12g'))
            control.SetName(label)
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            fields[name] = control
        muted = wx.CheckBox(panel, label='Mute this player only')
        muted.SetName('Mute this player only')
        muted.SetValue(levels.muted)
        layout.Add(muted, 0, wx.ALL, 4)
        mix_button = wx.Button(panel, label='Apply player volume, mute and balance')
        mix_button.SetName('Apply player volume, mute and balance')
        layout.Add(mix_button, 0, wx.ALL, 4)
        layout.Add(wx.StaticText(panel, label=(
            'Uses this player’s owned Windows output only. Other applications and '
            'screen-reader volume are unchanged. Unsupported devices report a failure.'
            if player.live_mix_available else
            'Live volume, mute and balance are unavailable on this backend.')),
            0, wx.EXPAND | wx.ALL, 4)
    listening_button = listening_cancel = None
    if prepare_listening is not None:
        from ..speech.listening import ListeningSettings
        settings = player.listening_settings
        for name, label, initial in (
                ('speed', 'Listening speed, 0.25 to 4; synthesis rate is unchanged', settings.speed),
                ('pitch', 'Listening pitch, minus 12 to 12 semitones; speed is independent', settings.pitch_semitones)):
            layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(panel, value=format(initial, '.12g'))
            control.SetName(label)
            layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            fields[name] = control
        row = wx.BoxSizer(wx.HORIZONTAL)
        listening_button = wx.Button(panel, label='Stop and prepare listening settings')
        listening_button.SetName('Stop and prepare listening speed and pitch without autoplay')
        listening_cancel = wx.Button(panel, label='Cancel listening preparation')
        listening_cancel.SetName('Cancel private listening-copy preparation')
        row.Add(listening_button, 0, wx.ALL, 4)
        row.Add(listening_cancel, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    first, last = player.ab_loop or (0, player.duration_seconds)
    backward, forward = player.skip_intervals
    for name, label, initial in (
            ('a', 'Repeat A in seconds', first),
            ('b', 'Repeat B in seconds', last),
            ('back', 'Backward skip in seconds, 0.1 to 600', backward),
            ('forward', 'Forward skip in seconds, 0.1 to 600', forward),
            ('sleep', 'Sleep timer in minutes, greater than zero and at most 10080', 30)):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 4)
        control = wx.TextCtrl(panel, value=format(initial, '.12g'))
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
        fields[name] = control
    status = wx.TextCtrl(panel, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 90))
    status.SetName('Current reader playback status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    actions = wx.GridSizer(cols=3, hgap=4, vgap=4)
    buttons = {}
    for key, label in (
            ('play', 'Play current recording'), ('pause', 'Pause or resume recording'),
            ('stop', 'Stop current recording'), ('back', 'Skip backward'),
            ('forward', 'Skip forward'), ('skips', 'Apply skip intervals'),
            ('ab', 'Set A–B points'), ('repeat', 'Enable A–B repeat'),
            ('disable_repeat', 'Disable A–B repeat'), ('clear_ab', 'Clear A–B repeat'),
            ('sleep', 'Set sleep timer'), ('clear_sleep', 'Clear sleep timer')):
        button = wx.Button(panel, label=label)
        button.SetName(label)
        actions.Add(button, 0, wx.EXPAND)
        buttons[key] = button
    layout.Add(actions, 0, wx.EXPAND | wx.ALL, 6)
    panel.SetSizer(layout)
    panel.FitInside()
    outer.Add(panel, 1, wx.EXPAND)
    close = wx.Button(dialog, wx.ID_CANCEL, 'Close playback controls')
    close.SetName('Close playback controls')
    outer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
    dialog.SetSizer(outer)
    dialog.SetEscapeId(wx.ID_CANCEL)
    alive, preparing, cancel_preparation, listening_note = True, False, None, ''
    detached = False
    destroy_errors = []

    def check():
        if not alive or not active():
            raise InterruptedError('These playback controls are no longer active.')
        guard()

    def detach():
        nonlocal alive, detached, cancel_preparation
        alive = False
        error = None
        cancel_now, cancel_preparation = cancel_preparation, None
        if preparing and callable(cancel_now):
            try:
                cancel_now()
            except Exception as exc:
                error = exc
        try:
            if not detached:
                subscribe(None)
                detached = True
        finally:
            if error is not None:
                raise error

    def owner_destroyed(event):
        nonlocal destroyed
        if event.GetId() == dialog_id:
            destroyed = True
            try:
                detach()
            except Exception as exc:
                destroy_errors.append(exc)
        event.Skip()

    dialog.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)

    def refresh():
        nonlocal queue_seen
        if not alive or not active():
            return
        try:
            state, position = player.state, player.position_seconds
        except Exception as exc:
            status.ChangeValue('Playback state is unavailable. Stop or close to retry cleanup. ' + str(exc))
            return
        loop = player.ab_loop
        remaining = player.sleep_remaining_seconds
        text = (f'{state}; {position:.2f} of '
                f'{player.duration_seconds:.2f} seconds. '
                + (f'A/B points {loop[0]:.2f} to {loop[1]:.2f} seconds; '
                   f'repeat {"on" if player.repeat_enabled else "off"}. ' if loop else 'Repeat off. ')
                + (f'Sleep in {remaining / 60:.2f} minutes. ' if remaining is not None else 'Sleep off. ')
                + ('Position is approximate on this backend.' if player.position_is_approximate else
                   'Position is reported by the owned Windows playback device.'))
        if queue_list is not None:
            values = player.playlist
            if values != queue_seen:
                selected = queue_list.GetSelection()
                queue_list.Set([f'{index + 1}. {getattr(value, "display_name", value.path.name)}; {value.duration_seconds:.2f} seconds'
                                for index, value in enumerate(values)])
                if values:
                    queue_list.SetSelection(min(max(0, selected), len(values) - 1))
                queue_seen = values
            current = player.playlist_index
            text += (f' Playlist: {len(values)} item(s); '
                     + (f'current item {current + 1}. ' if current is not None else 'none selected for playback. ')
                     + ('Sequential playback armed. ' if player.queue_active else 'Sequential playback off. ')
                     + queue_note)
            for name, button in queue_buttons.items():
                button.Enable(preparing if name == 'cancel' else not preparing)
            queue_list.Enable(not preparing)
        if save_button is not None:
            save_button.Enable(not preparing)
            saved_button.Enable(not preparing and saved_pending())
            current_controls.Enable(not preparing and saved_pending())
        if mix_button is not None:
            levels = player.levels
            text += (f' Player volume {levels.volume:g}; mute {"on" if levels.muted else "off"}; '
                     f'balance {levels.balance * 100:g}. ')
            enabled = bool(player.live_mix_available) and not preparing
            mix_button.Enable(enabled)
            muted.Enable(enabled)
            fields['volume'].Enable(enabled)
            fields['balance'].Enable(enabled)
        message = status_message()
        if message:
            text += ' ' + str(message)
        if output is not None:
            selected = player.output_device
            text += (' Output: ' + (selected.name if selected is not None else 'automatic')
                     + '. ' + output_note)
        if listening_button is not None:
            settings = player.listening_settings
            text += (f' Listening: {settings.speed:g} times speed; {settings.pitch_semitones:g} semitones; '
                     f'{player.listening_duration_seconds:.2f} seconds of output. '
                     'Position and repeat use the original timeline; transformed positions are linear estimates. '
                     + listening_note)
            listening_button.Enable(not preparing)
            listening_cancel.Enable(preparing)
            for name in ('speed', 'pitch'):
                fields[name].Enable(not preparing)
            for name, button in buttons.items():
                button.Enable(not preparing or name == 'stop')
            if output is not None:
                reload_outputs.Enable(not preparing)
                apply_output.Enable(not preparing)
        if preparing:
            for name, button in buttons.items():
                button.Enable(name == 'stop')
            if output is not None:
                reload_outputs.Enable(False)
                apply_output.Enable(False)
        elif listening_button is None:
            for button in buttons.values():
                button.Enable(True)
            if output is not None:
                reload_outputs.Enable(True)
                apply_output.Enable(True)
        if status.GetValue() != text:
            status.ChangeValue(text)

    def report_error(exc):
        if alive and active():
            status.ChangeValue(str(exc))
            status.SetFocus()

    def run(prepare, *, audible=False):
        def invoke(_event=None):
            if not alive or not active():
                return
            try:
                check()
                # Read wx controls now; a deferred quiet-mode callback receives
                # only frozen numeric values and owned player methods.
                action = prepare()
                def performed():
                    if not alive or not active():
                        return
                    check()
                    action()
                    refresh()
                execute(performed, audible=audible)
            except Exception as exc:
                report_error(exc)
        return invoke

    def mix_action():
        from ..speech._wave_output import PlaybackLevels
        levels = PlaybackLevels(float(fields['volume'].GetValue()), bool(muted.GetValue()),
                                float(fields['balance'].GetValue()) / 100)
        return lambda: player.set_levels(levels.volume, muted=levels.muted, balance=levels.balance)

    def queue_index():
        index = queue_list.GetSelection()
        if not 0 <= index < len(player.playlist):
            raise ValueError('Choose a reviewed playlist item.')
        return index

    def queue_action(kind):
        index = queue_index()
        captured = tuple(player.playlist)
        def checked(action):
            def apply():
                if tuple(player.playlist) != captured:
                    raise ValueError('The playlist changed while this action was waiting. Choose the item again.')
                action()
            return apply
        if kind == 'play':
            return checked(lambda: player.play_queue(index))
        if kind == 'select':
            return checked(lambda: player.select_queue(index))
        values = list(captured)
        if kind == 'remove':
            del values[index]
        else:
            destination = index + (-1 if kind == 'up' else 1)
            if not 0 <= destination < len(values):
                raise ValueError('This item is already at that end of the playlist.')
            values[index], values[destination] = values[destination], values[index]
        reviewed = tuple(values)
        def replace_queue():
            player.set_playlist(reviewed)
            queue_changed()
        return checked(replace_queue)

    def choose_queue(*, append=False):
        nonlocal preparing, cancel_preparation, queue_note
        from ..speech.playlist import MAX_ITEMS
        if preparing:
            raise ValueError('Wait for or cancel the current file review.')
        with wx.FileDialog(dialog, 'Choose local PCM WAV files for the session playlist',
                wildcard='PCM WAV audio (*.wav)|*.wav',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE) as chooser:
            if chooser.ShowModal() != wx.ID_OK:
                return
            paths = tuple(chooser.GetPaths())
        check()  # A native picker may outlive this source or window.
        previous = tuple(player.playlist) if append else ()
        if not paths or len(paths) + len(previous) > MAX_ITEMS:
            raise ValueError(f'Choose at most {MAX_ITEMS} total playlist recordings.')
        stop()
        preparing = True
        queue_note = 'Reviewing file bytes on the existing worker; audio is stopped.'
        refresh()
        def completed(values):
            nonlocal preparing, cancel_preparation, queue_note
            if not alive or not active():
                return
            try:
                check()
                player.set_playlist((*previous, *values))
                queue_changed()
            except Exception as exc:
                failed(exc)
                return
            preparing, cancel_preparation = False, None
            queue_note = 'Playlist reviewed. Select an item and start playback explicitly.'
            refresh()
        def failed(exc):
            nonlocal preparing, cancel_preparation, queue_note
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            queue_note = 'Playlist unchanged. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            cancel_preparation = review_playlist(paths, completed, failed, active=lambda: alive and active())
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Playlist review did not start.')
        except Exception as exc:
            failed(exc)

    def review_recovery():
        nonlocal preparing, cancel_preparation, queue_note
        if preparing:
            raise ValueError('Wait for or cancel the current file review.')
        use_current = bool(current_controls.GetValue())
        stop()
        preparing = True
        queue_note = 'Reviewing retained queue bytes; no audio will be started.'
        refresh()
        def completed():
            nonlocal preparing, cancel_preparation, queue_note
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            queue_note = ('Saved queue reviewed and selected silently. Start playback explicitly; '
                          'repeat and sleep remain off.')
            backward, forward = player.skip_intervals
            fields['back'].ChangeValue(format(backward, '.12g'))
            fields['forward'].ChangeValue(format(forward, '.12g'))
            if mix_button is not None:
                levels = player.levels
                fields['volume'].ChangeValue(format(levels.volume, '.12g'))
                fields['balance'].ChangeValue(format(levels.balance * 100, '.12g'))
                muted.SetValue(levels.muted)
            refresh()
        def failed(exc):
            nonlocal preparing, cancel_preparation, queue_note
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            queue_note = 'Saved queue remains unreviewed. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            cancel_preparation = review_saved(completed, failed, active=lambda: alive and active(),
                                              use_current_controls=use_current)
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Saved queue review did not start.')
        except Exception as exc:
            failed(exc)

    def save_clicked(event=None):
        nonlocal queue_note
        if not alive or not active():
            return
        try:
            check()
            queue_note = save_queue()
            refresh()
        except Exception as exc:
            report_error(exc)

    def clear_queue():
        player.set_playlist(())
        queue_changed()

    def loop_action():
        first, last = float(fields['a'].GetValue()), float(fields['b'].GetValue())
        if not 0 <= first < last <= player.duration_seconds or last - first + 1e-9 < .05:
            raise ValueError('Choose A and B inside this recording, at least 0.05 seconds apart.')
        return lambda: player.set_ab_loop(first, last)

    def skips_action():
        backward = float(fields['back'].GetValue())
        forward = float(fields['forward'].GetValue())
        return lambda: player.set_skip_intervals(backward, forward)

    def sleep_action():
        minutes = float(fields['sleep'].GetValue())
        if not 0 < minutes <= 10080:
            raise ValueError('Choose a sleep timer greater than zero and at most seven days.')
        seconds = minutes * 60
        return lambda: player.set_sleep_timer(seconds)

    def load_outputs():
        nonlocal outputs, output_note, preparing, cancel_preparation
        check()
        if preparing:
            raise ValueError('Wait for or cancel the current playback review.')
        outputs = (None,)
        output.Set(['Automatic output chosen by the playback backend'])
        output.SetSelection(0 if player.output_device is None else -1)
        output_note = 'Reviewing available outputs on the existing worker.'
        preparing = True
        refresh()
        def completed(values):
            nonlocal outputs, output_note, preparing, cancel_preparation
            if not alive or not active():
                return
            try:
                check()
                selected = player.output_device
                outputs = (None, *values)
                output.Set(['Automatic output chosen by the playback backend',
                    *(f'{value.name}; device {value.index + 1}' for value in values)])
                index = 0 if selected is None else next((index for index, value in enumerate(outputs)
                    if value is not None and value.identity == selected.identity), -1)
                output.SetSelection(index)
                output_note = (f'{len(values)} Windows output(s) listed; no sound was played. '
                    'Apply the selected output explicitly.'
                    + (' The previously selected output is unavailable.' if index < 0 else ''))
                preparing, cancel_preparation = False, None
                refresh()
            except Exception as exc:
                failed(exc)
        def failed(exc):
            nonlocal output_note, preparing, cancel_preparation
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            output_note = str(exc)
            refresh()
            report_error(exc)
        try:
            cancel_preparation = review_outputs(completed, failed, active=lambda: alive and active())
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Output review did not start.')
        except Exception as exc:
            failed(exc)

    def output_action():
        index = output.GetSelection()
        if not 0 <= index < len(outputs):
            raise ValueError('Choose an enumerated output or automatic output explicitly.')
        selected = outputs[index]
        def apply():
            nonlocal output_note
            # Reader Stop revokes its queued quiet/repeat action before the
            # borrowed player releases its alias. Apply never invokes Play.
            stop()
            player.select_output(selected)
            output_note = 'Playback stopped. Start audio explicitly to use this output; repeat and sleep were cleared.'
        return apply

    def prepare_settings():
        nonlocal preparing, cancel_preparation, listening_note
        if preparing:
            raise ValueError('Wait for or cancel the current listening preparation.')
        settings = ListeningSettings(float(fields['speed'].GetValue()), float(fields['pitch'].GetValue()))
        position = player.position_seconds
        stop()
        request = player.listening_request(settings.speed, settings.pitch_semitones, position_seconds=position)
        preparing = True
        listening_note = 'Preparing a private listening copy on the reader worker; originals are unchanged.'
        refresh()
        def completed():
            nonlocal preparing, cancel_preparation, listening_note
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            listening_note = 'Listening copy verified and selected. Start audio explicitly; repeat and sleep were cleared.'
            refresh()
        def failed(exc):
            nonlocal preparing, cancel_preparation, listening_note
            if not alive or not active():
                return
            preparing, cancel_preparation = False, None
            listening_note = 'Listening preparation did not change the original recording. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            # The existing reader owns worker execution, cancellation, current
            # tab/source generation, result adoption and unused-result cleanup.
            cancel_preparation = prepare_listening(request, completed, failed, active=lambda: alive and active())
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Listening preparation did not start.')
        except Exception as exc:
            failed(exc)

    def cancel_settings(event=None):
        nonlocal listening_note, queue_note
        if not alive or not active() or not preparing:
            return
        if callable(cancel_preparation):
            cancel_preparation()
        stop()
        listening_note = 'Cancellation requested; waiting for private conversion cleanup.'
        queue_note = 'Cancellation requested; waiting for bounded file review to stop.'
        refresh()

    if queue_list is not None:
        queue_buttons['replace'].Bind(wx.EVT_BUTTON, run(lambda: lambda: choose_queue(append=False)))
        queue_buttons['add'].Bind(wx.EVT_BUTTON, run(lambda: lambda: choose_queue(append=True)))
        for name in ('select', 'play', 'up', 'down', 'remove'):
            queue_buttons[name].Bind(wx.EVT_BUTTON,
                run(lambda name=name: queue_action(name), audible=name == 'play'))
        queue_buttons['clear'].Bind(wx.EVT_BUTTON, run(lambda: clear_queue))
        queue_buttons['cancel'].Bind(wx.EVT_BUTTON, cancel_settings)
    if save_button is not None:
        save_button.Bind(wx.EVT_BUTTON, save_clicked)
        saved_button.Bind(wx.EVT_BUTTON, run(lambda: review_recovery))
        save_id = int(wx.NewIdRef())
        dialog.Bind(wx.EVT_MENU, save_clicked, id=save_id)
        dialog.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CTRL, ord('S'), save_id)]))
    if mix_button is not None:
        mix_button.Bind(wx.EVT_BUTTON, run(mix_action, audible=True))
    if output is not None:
        reload_outputs.Bind(wx.EVT_BUTTON, run(lambda: load_outputs))
        apply_output.Bind(wx.EVT_BUTTON, run(output_action))
    if listening_button is not None:
        listening_button.Bind(wx.EVT_BUTTON, run(lambda: prepare_settings))
        listening_cancel.Bind(wx.EVT_BUTTON, cancel_settings)

    def stop_clicked(event=None):
        try:
            stop()
        except Exception as exc:
            report_error(exc)

    buttons['play'].Bind(wx.EVT_BUTTON, run(lambda: player.play_current, audible=True))
    buttons['pause'].Bind(wx.EVT_BUTTON, run(lambda: pause))
    buttons['stop'].Bind(wx.EVT_BUTTON, stop_clicked)
    buttons['back'].Bind(wx.EVT_BUTTON, run(lambda: player.skip_backward, audible=True))
    buttons['forward'].Bind(wx.EVT_BUTTON, run(lambda: player.skip_forward, audible=True))
    buttons['skips'].Bind(wx.EVT_BUTTON, run(skips_action))
    buttons['ab'].Bind(wx.EVT_BUTTON, run(loop_action, audible=True))
    buttons['repeat'].Bind(wx.EVT_BUTTON, run(lambda: lambda: player.enable_repeat(True), audible=True))
    buttons['disable_repeat'].Bind(wx.EVT_BUTTON, run(lambda: lambda: player.enable_repeat(False), audible=True))
    buttons['clear_ab'].Bind(wx.EVT_BUTTON, run(lambda: player.clear_ab_loop, audible=True))
    buttons['sleep'].Bind(wx.EVT_BUTTON, run(sleep_action))
    buttons['clear_sleep'].Bind(wx.EVT_BUTTON, run(lambda: lambda: player.set_sleep_timer(None)))
    def close_requested(event=None):
        if not alive:
            return
        if save_queue is not None:
            try:
                (close_queue if close_queue is not None else save_queue)()
            except Exception as exc:
                answer = wx.MessageBox('Queue changes are not saved: ' + str(exc)
                    + '\nClose without saving these changes?', 'Playback queue',
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, parent=dialog)
                if not alive or not active():
                    return
                if answer != wx.YES:
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    report_error(exc)
                    return
                discard_queue()
        dialog.EndModal(wx.ID_CANCEL)
    close.Bind(wx.EVT_BUTTON, close_requested)
    dialog.Bind(wx.EVT_CLOSE, close_requested)
    try:
        subscribe(refresh)
        refresh()
        (queue_list if queue_list is not None else fields['a']).SetFocus()
        dialog.ShowModal()
    finally:
        try:
            detach()
        finally:
            if not destroyed:
                dialog.Destroy()
        if destroy_errors:
            raise RuntimeError('Playback cleanup needs attention: ' + str(destroy_errors[0]))
