"""Keyboard-first review of the existing typed offline audio effect chain."""
from ..audio.dsp import effect_controls
from ..audio.effect_presets import (EffectChainSession, EffectPresetStore,
                                   builtin_presets, default_effect)
from ..audio.models import Effect


def _number(value):
    return format(float(value), '.12g')


def effect_label(effect):
    controls = ', '.join(f'{key}={_number(value)}' for key, value in effect.parameters.items())
    return f'{effect.kind}; {"enabled" if effect.enabled else "bypassed"}; {controls or "default controls"}'


def edit_effect(parent, effect, *, sample_rate):
    """Return a reviewed value; an invalid numeric form stays open for repair."""
    import wx
    dialog = wx.Dialog(parent, title=f'Edit {effect.kind} effect', size=(660, 620),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    outer = wx.BoxSizer(wx.VERTICAL)
    panel = wx.ScrolledWindow(dialog)
    panel.SetScrollRate(0, 16)
    layout = wx.BoxSizer(wx.VERTICAL)
    definitions = effect_controls()[effect.kind]
    controls = {}
    for name, definition in definitions.items():
        maximum = definition['maximum']
        label = (f"{name.replace('_', ' ')} ({definition['unit']}); "
                 f"{_number(definition['minimum'])} to {_number(maximum)}")
        if name == 'frequency' and definition['unit'] == 'Hz':
            label += f'; below {sample_rate / 2:g} Hz'
        layout.Add(wx.StaticText(panel, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(panel, value=_number(effect.parameters.get(name, definition['default'])))
        control.SetName(label)
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
        controls[name] = control
    if not controls:
        layout.Add(wx.StaticText(panel, label='This effect has no adjustable numeric controls.'),
                   0, wx.ALL, 8)
    enabled = wx.CheckBox(panel, label='Enable this effect')
    enabled.SetName('Enable this effect')
    enabled.SetValue(effect.enabled)
    layout.Add(enabled, 0, wx.ALL, 8)
    reset = wx.Button(panel, label='Reset numeric controls to defaults')
    reset.SetName('Reset numeric controls to defaults')
    layout.Add(reset, 0, wx.ALL, 5)
    panel.SetSizer(layout)
    outer.Add(panel, 1, wx.EXPAND | wx.ALL, 5)
    status = wx.TextCtrl(dialog, value='Review values, then Apply. Cancel keeps the existing effect.',
                         style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Effect validation status')
    outer.Add(status, 0, wx.EXPAND | wx.ALL, 5)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    apply = wx.Button(dialog, wx.ID_OK, 'Apply effect')
    apply.SetName('Apply effect')
    cancel = wx.Button(dialog, wx.ID_CANCEL, 'Cancel')
    buttons.Add(apply, 0, wx.ALL, 5)
    buttons.Add(cancel, 0, wx.ALL, 5)
    outer.Add(buttons, 0, wx.ALL, 5)
    dialog.SetSizer(outer)
    reviewed = None

    def restore(_event=None):
        defaults = default_effect(effect.kind, sample_rate)
        for name, control in controls.items():
            control.ChangeValue(_number(defaults.parameters[name]))

    def accept(_event=None):
        nonlocal reviewed
        try:
            candidate = Effect(effect.kind, {key: float(control.GetValue())
                               for key, control in controls.items()}, enabled=enabled.GetValue())
            # Validate every field, including a bypassed effect, before close.
            from ..audio.dsp import effect_filter
            effect_filter(candidate, sample_rate)
        except Exception as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()
            return
        reviewed = candidate
        dialog.EndModal(wx.ID_OK)

    reset.Bind(wx.EVT_BUTTON, restore)
    apply.Bind(wx.EVT_BUTTON, accept)
    try:
        (next(iter(controls.values())) if controls else enabled).SetFocus()
        return reviewed if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def show_effect_chain(parent, library_root, effects=(), *, sample_rate=24000, available=None):
    """Edit one in-memory chain; only an explicit Save writes a user preset."""
    import wx
    session = EffectChainSession(effects, sample_rate=sample_rate, available=available)
    presets = EffectPresetStore(library_root)
    dialog = wx.Dialog(parent, title='Ordered audio effects', size=(900, 720),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        f'Effects run from top to bottom at {sample_rate} Hz. Bypass keeps settings. '
        'Apply returns the chain; audio is processed only after choosing an output file.')),
        0, wx.EXPAND | wx.ALL, 8)
    ordered = wx.ListBox(dialog, choices=[])
    ordered.SetName('Ordered audio effects')
    layout.Add(ordered, 1, wx.EXPAND | wx.ALL, 8)
    kinds = sorted(set(effect_controls()) & session.available)
    kind = wx.Choice(dialog, choices=kinds)
    kind.SetName('Effect type to add')
    if kinds:
        kind.SetSelection(0)
    layout.Add(kind, 0, wx.EXPAND | wx.ALL, 5)
    buttons = {}
    actions = wx.GridSizer(cols=4, hgap=5, vgap=5)
    for key, label in (
            ('add', 'Add effect'), ('edit', 'Edit selected effect'), ('toggle', 'Toggle effect bypass'),
            ('remove', 'Remove effect'), ('up', 'Move effect up'), ('down', 'Move effect down'),
            ('undo', 'Undo chain change'), ('redo', 'Redo chain change'),
            ('load', 'Load effect preset'), ('save', 'Save chain as a new preset')):
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        actions.Add(button, 0, wx.EXPAND)
        buttons[key] = button
    layout.Add(actions, 0, wx.EXPAND | wx.ALL, 8)
    status = wx.TextCtrl(dialog, value='', style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Effect chain status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 8)
    footer = wx.BoxSizer(wx.HORIZONTAL)
    apply = wx.Button(dialog, wx.ID_OK, 'Apply effect chain')
    apply.SetName('Apply effect chain')
    cancel = wx.Button(dialog, wx.ID_CANCEL, 'Cancel')
    footer.Add(apply, 0, wx.ALL, 5)
    footer.Add(cancel, 0, wx.ALL, 5)
    layout.Add(footer, 0, wx.ALL, 5)
    dialog.SetSizer(layout)

    def refresh(index=None, message=None):
        selected = ordered.GetSelection() if index is None else index
        ordered.Set([f'{number + 1}. {effect_label(value)}'
                     for number, value in enumerate(session.effects)])
        if session.effects:
            ordered.SetSelection(min(max(selected, 0), len(session.effects) - 1))
        selected = ordered.GetSelection()
        for key in ('edit', 'toggle', 'remove'):
            buttons[key].Enable(selected != wx.NOT_FOUND)
        buttons['up'].Enable(selected > 0)
        buttons['down'].Enable(0 <= selected < len(session.effects) - 1)
        buttons['add'].Enable(bool(kinds) and len(session.effects) < 32)
        buttons['undo'].Enable(bool(session._undo))
        buttons['redo'].Enable(bool(session._redo))
        status.ChangeValue(message or f'{len(session.effects)} effects. Source audio remains unchanged.')

    def run(action):
        def invoke(_event=None):
            try:
                selected = action()
                refresh(selected if type(selected) is int else None,
                        selected if isinstance(selected, str) else None)
                (status if isinstance(selected, str) else ordered).SetFocus()
            except Exception as exc:
                status.ChangeValue(str(exc))
                status.SetFocus()
        return invoke

    def edit():
        index = session._index(ordered.GetSelection())
        value = edit_effect(dialog, session.effects[index], sample_rate=sample_rate)
        if value is not None:
            session.edit(index, value)
        return index

    def load_preset():
        choices = [(name + ' (built-in)', chain) for name, chain in builtin_presets()]
        choices.extend((f'{item.name} (saved at {item.sample_rate} Hz; {item.id[:8]})', item.effects)
                       for item in presets.list())
        picker = wx.SingleChoiceDialog(dialog, 'Choose a preset. It replaces this editable chain.',
                                        'Load effect preset', [item[0] for item in choices])
        try:
            if picker.ShowModal() == wx.ID_OK:
                session.set(choices[picker.GetSelection()][1])
        finally:
            picker.Destroy()

    def save_preset():
        picker = wx.TextEntryDialog(dialog, 'Name for a new saved effect preset', 'Save effect preset')
        try:
            if picker.ShowModal() == wx.ID_OK:
                value = presets.save(picker.GetValue().strip(), session.effects, sample_rate=sample_rate)
                # The saved preset is independent of Cancel in this editing dialog.
                return f'Saved preset: {value.name}. It is available from Load effect preset.'
        finally:
            picker.Destroy()

    handlers = {
        'add': lambda: session.add(kinds[kind.GetSelection()]),
        'edit': edit, 'toggle': lambda: session.toggle(ordered.GetSelection()),
        'remove': lambda: session.remove(ordered.GetSelection()),
        'up': lambda: session.move(ordered.GetSelection(), -1),
        'down': lambda: session.move(ordered.GetSelection(), 1),
        'undo': lambda: session.history(), 'redo': lambda: session.history(forward=True),
        'load': load_preset, 'save': save_preset}
    for key, handler in handlers.items():
        buttons[key].Bind(wx.EVT_BUTTON, run(handler))
    ordered.Bind(wx.EVT_LISTBOX, lambda _event: refresh())
    ordered.Bind(wx.EVT_LISTBOX_DCLICK, run(edit))
    refresh()
    try:
        (ordered if session.effects else kind).SetFocus()
        return session.effects if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
