"""Native controls for the existing reader-owned WavePlayer.

The reader owns admission, its timer and cleanup. This dialog supplies reviewed
numeric values; it creates no player, worker, engine or playback loop.
"""


def show_reader_playback(parent, player, *, execute, stop, pause, subscribe):
    import wx

    dialog = wx.Dialog(parent, title='Reader playback controls', size=(700, 660),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        'Controls apply to the current rendered recording. Opening this dialog '
        'does not start audio. Stop also cancels pending repeat and sleep.')),
        0, wx.EXPAND | wx.ALL, 6)
    fields = {}
    first, last = player.ab_loop or (0, player.duration_seconds)
    backward, forward = player.skip_intervals
    for name, label, initial in (
            ('a', 'Repeat A in seconds', first),
            ('b', 'Repeat B in seconds', last),
            ('back', 'Backward skip in seconds, 0.1 to 600', backward),
            ('forward', 'Forward skip in seconds, 0.1 to 600', forward),
            ('sleep', 'Sleep timer in minutes, greater than zero and at most 10080', 30)):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = wx.TextCtrl(dialog, value=format(initial, '.12g'))
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 4)
        fields[name] = control
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
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
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        actions.Add(button, 0, wx.EXPAND)
        buttons[key] = button
    layout.Add(actions, 0, wx.EXPAND | wx.ALL, 6)
    close = wx.Button(dialog, wx.ID_CANCEL, 'Close playback controls')
    close.SetName('Close playback controls')
    layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
    dialog.SetSizer(layout)
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
        if status.GetValue() != text:
            status.ChangeValue(text)

    def report_error(exc):
        if alive:
            status.ChangeValue(str(exc))
            status.SetFocus()

    def run(prepare, *, audible=False):
        def invoke(_event=None):
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
