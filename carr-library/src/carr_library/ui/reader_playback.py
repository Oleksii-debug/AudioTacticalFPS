"""Native controls for the existing reader-owned WavePlayer.

The reader owns admission, its timer and cleanup. This dialog supplies reviewed
numeric values; it creates no player, worker, engine or playback loop.
"""


def show_reader_playback(parent, player, *, execute, stop, pause, subscribe):
    import wx

    dialog = wx.Dialog(parent, title='Reader playback controls', size=(700, 740),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    outer = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(panel, label=(
        'Controls apply to the current rendered recording. Opening this dialog '
        'does not start audio. Stop also cancels pending repeat and sleep.')),
        0, wx.EXPAND | wx.ALL, 6)
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
    alive = True

    def refresh():
        if not alive:
            return
        loop = player.ab_loop
        remaining = player.sleep_remaining_seconds
        text = (f'{player.state}; {player.position_seconds:.2f} of '
                f'{player.duration_seconds:.2f} seconds. '
                + (f'Repeat {loop[0]:.2f} to {loop[1]:.2f} seconds. ' if loop else 'Repeat off. ')
                + (f'Sleep in {remaining / 60:.2f} minutes. ' if remaining is not None else 'Sleep off. ')
                + ('Position is approximate on this backend.' if player.position_is_approximate else
                   'Position is reported by Windows MCI.'))
        if output is not None:
            selected = player.output_device
            text += (' Output: ' + (selected.name if selected is not None else 'automatic')
                     + '. ' + output_note)
        if status.GetValue() != text:
            status.ChangeValue(text)

    def report_error(exc):
        if alive:
            status.ChangeValue(str(exc))
            status.SetFocus()

    def run(prepare, *, audible=False):
        def invoke(_event=None):
            if not alive:
                return
            try:
                # Read wx controls now; a deferred quiet-mode callback receives
                # only frozen numeric values and owned player methods.
                action = prepare()
                def performed():
                    if not alive:
                        return
                    action()
                    refresh()
                execute(performed, audible=audible)
            except Exception as exc:
                report_error(exc)
        return invoke

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
        if not alive:
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

    if output is not None:
        reload_outputs.Bind(wx.EVT_BUTTON, run(lambda: load_outputs))
        apply_output.Bind(wx.EVT_BUTTON, run(output_action))

    buttons['play'].Bind(wx.EVT_BUTTON, run(lambda: player.play_current, audible=True))
    buttons['pause'].Bind(wx.EVT_BUTTON, run(lambda: pause))
    buttons['stop'].Bind(wx.EVT_BUTTON, lambda _event: stop())
    buttons['back'].Bind(wx.EVT_BUTTON, run(lambda: player.skip_backward, audible=True))
    buttons['forward'].Bind(wx.EVT_BUTTON, run(lambda: player.skip_forward, audible=True))
    buttons['skips'].Bind(wx.EVT_BUTTON, run(skips_action))
    buttons['ab'].Bind(wx.EVT_BUTTON, run(loop_action, audible=True))
    buttons['clear_ab'].Bind(wx.EVT_BUTTON, run(lambda: player.clear_ab_loop, audible=True))
    buttons['sleep'].Bind(wx.EVT_BUTTON, run(sleep_action))
    buttons['clear_sleep'].Bind(wx.EVT_BUTTON, run(lambda: lambda: player.set_sleep_timer(None)))
    close.Bind(wx.EVT_BUTTON, lambda _event: dialog.EndModal(wx.ID_CANCEL))
    try:
        subscribe(refresh)
        refresh()
        fields['a'].SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        subscribe(None)
        dialog.Destroy()
