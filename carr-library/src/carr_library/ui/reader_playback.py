"""Native controls for the existing reader-owned WavePlayer.

The reader owns admission, its timer and cleanup. This dialog supplies reviewed
numeric values; it creates no player, worker, engine or playback loop.
"""


def show_reader_playback(parent, player, *, execute, stop, pause, subscribe, prepare_listening=None,
                         allow_queue=False, review_playlist=None, status_message=lambda: '',
                         queue_changed=lambda: None, save_queue=None, review_saved=None,
                         saved_pending=lambda: False, close_queue=None, discard_queue=lambda: None,
                         scope_current=lambda: True):
    import wx

    if type(allow_queue) is not bool or allow_queue and not callable(review_playlist):
        raise ValueError('Standalone queue controls require an owned review callback.')
    if save_queue is not None and (not allow_queue or not callable(save_queue)
            or not callable(review_saved) or not callable(saved_pending)):
        raise ValueError('Saved queues need their existing owned store and review callbacks.')
    dialog = wx.Dialog(parent, title=('Audio playback and saved playlist' if allow_queue
                                     else 'Reader playback controls'), size=(700, 740),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
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
    if callable(getattr(player, 'output_devices', None)):
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
            ('ab', 'Apply A–B repeat'), ('clear_ab', 'Clear A–B repeat'),
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

    def refresh():
        nonlocal queue_seen
        if not alive or not scope_current():
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
                + (f'Repeat {loop[0]:.2f} to {loop[1]:.2f} seconds. ' if loop else 'Repeat off. ')
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
        if alive and scope_current():
            status.ChangeValue(str(exc))
            status.SetFocus()

    def run(prepare, *, audible=False):
        def invoke(_event=None):
            if not alive or not scope_current():
                return
            try:
                # Read wx controls now; a deferred quiet-mode callback receives
                # only frozen numeric values and owned player methods.
                action = prepare()
                def performed():
                    if not alive or not scope_current():
                        return
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
            if not alive or not scope_current():
                return
            paths = tuple(chooser.GetPaths())
        previous = tuple(player.playlist) if append else ()
        if not paths or len(paths) + len(previous) > MAX_ITEMS:
            raise ValueError(f'Choose at most {MAX_ITEMS} total playlist recordings.')
        stop()
        preparing = True
        queue_note = 'Reviewing file bytes on the existing worker; audio is stopped.'
        refresh()
        def completed(values):
            nonlocal preparing, cancel_preparation, queue_note
            if not alive or not scope_current():
                return
            try:
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
            if not alive or not scope_current():
                return
            preparing, cancel_preparation = False, None
            queue_note = 'Playlist unchanged. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            cancel_preparation = review_playlist(paths, completed, failed, active=lambda: alive and scope_current())
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
            if not alive or not scope_current():
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
            if not alive or not scope_current():
                return
            preparing, cancel_preparation = False, None
            queue_note = 'Saved queue remains unreviewed. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            cancel_preparation = review_saved(completed, failed, active=lambda: alive and scope_current(),
                                              use_current_controls=use_current)
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Saved queue review did not start.')
        except Exception as exc:
            failed(exc)

    def save_clicked(event=None):
        nonlocal queue_note
        if not alive or not scope_current():
            return
        try:
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
        nonlocal outputs, output_note
        # Discard old display rows even if a refresh fails. The player's selected
        # identity remains unchanged and cannot silently become automatic.
        outputs = (None,)
        output.Set(['Automatic output chosen by the playback backend'])
        output.SetSelection(0 if player.output_device is None else -1)
        output_note = 'Reloading available playback outputs.'
        try:
            values = player.output_devices(cancelled=lambda: not alive)
        except Exception as exc:
            output_note = str(exc)
            raise
        if not alive or not scope_current():
            return
        selected = player.output_device
        outputs = (None, *values)
        output.Set(['Automatic output chosen by the playback backend',
            *(f'{value.name}; device {value.index + 1}'
              + ('' if value.selectable else '; no stable identity, unavailable') for value in values)])
        index = 0 if selected is None else next((index for index, value in enumerate(outputs)
            if value is not None and value.identity == selected.identity), -1)
        output.SetSelection(index)
        output_note = (f'{len(values)} Windows output(s) listed; no sound was played. '
                       'Apply the selected output explicitly.'
                       + (' The previously selected output is unavailable.' if index < 0 else ''))

    def output_action():
        index = output.GetSelection()
        if not 0 <= index < len(outputs):
            raise ValueError('Choose an enumerated output or automatic output explicitly.')
        selected = outputs[index]
        if selected is not None and not selected.selectable:
            raise ValueError('This output has no stable interface identity. Choose a different output or automatic output.')
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
            if not alive or not scope_current():
                return
            preparing, cancel_preparation = False, None
            listening_note = 'Listening copy verified and selected. Start audio explicitly; repeat and sleep were cleared.'
            refresh()
        def failed(exc):
            nonlocal preparing, cancel_preparation, listening_note
            if not alive or not scope_current():
                return
            preparing, cancel_preparation = False, None
            listening_note = 'Listening preparation did not change the original recording. ' + str(exc)
            refresh()
            report_error(exc)
        try:
            # The existing reader owns worker execution, cancellation, current
            # tab/source generation, result adoption and unused-result cleanup.
            cancel_preparation = prepare_listening(request, completed, failed, active=lambda: alive and scope_current())
            if preparing and not callable(cancel_preparation):
                raise RuntimeError('Listening preparation did not start.')
        except Exception as exc:
            failed(exc)

    def cancel_settings(event=None):
        nonlocal listening_note, queue_note
        if not alive or not preparing:
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
    buttons['clear_ab'].Bind(wx.EVT_BUTTON, run(lambda: player.clear_ab_loop, audible=True))
    buttons['sleep'].Bind(wx.EVT_BUTTON, run(sleep_action))
    buttons['clear_sleep'].Bind(wx.EVT_BUTTON, run(lambda: lambda: player.set_sleep_timer(None)))
    def close_requested(event=None):
        if not scope_current():
            discard_queue()
            dialog.EndModal(wx.ID_CANCEL)
            return
        if save_queue is not None:
            try:
                (close_queue if close_queue is not None else save_queue)()
            except Exception as exc:
                answer = wx.MessageBox('Queue changes are not saved: ' + str(exc)
                    + '\nClose without saving these changes?', 'Playback queue',
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, parent=dialog)
                if answer != wx.YES:
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    report_error(exc)
                    return
                discard_queue()
        dialog.EndModal(wx.ID_CANCEL)
    close.Bind(wx.EVT_BUTTON, close_requested)
    if save_queue is not None:
        dialog.Bind(wx.EVT_CLOSE, close_requested)
    try:
        subscribe(refresh)
        refresh()
        (queue_list if queue_list is not None else fields['a']).SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        try:
            if preparing and callable(cancel_preparation):
                cancel_preparation()
        finally:
            try:
                subscribe(None)
            finally:
                dialog.Destroy()
