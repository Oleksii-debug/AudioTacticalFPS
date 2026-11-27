"""Native, named controls for typed local audio effects."""
from __future__ import annotations

from ..audio.models import Effect, effect_controls


def choose_role_effect(parent, roles):
    import wx

    names = tuple(roles)
    if not names:
        raise ValueError('The current production plan has no roles.')
    role = names[0]
    if len(names) > 1:
        selected = wx.SingleChoiceDialog(parent, 'Choose the role to process.',
                                         'Audio effect role', list(names))
        selected.SetName('Role for the new audio effect')
        try:
            if selected.ShowModal() != wx.ID_OK:
                return None
            role = selected.GetStringSelection()
        finally:
            selected.Destroy()

    controls = effect_controls()
    choice = wx.SingleChoiceDialog(parent, 'Choose an effect. Original audio is kept.',
                                    'Add audio effect', list(controls))
    choice.SetName('Audio effect type')
    try:
        if choice.ShowModal() != wx.ID_OK:
            return None
        kind = choice.GetStringSelection()
    finally:
        choice.Destroy()
    definitions = controls[kind]
    if not definitions:
        return role, Effect(kind)

    dialog = wx.Dialog(parent, title=f'{kind} — {role}',
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName(f'Parameters for {kind} on {role}')
    layout = wx.BoxSizer(wx.VERTICAL)
    description = wx.StaticText(dialog, label='Enter the numeric settings below. Preview the plan before rendering.')
    layout.Add(description, 0, wx.ALL, 10)
    grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=12)
    grid.AddGrowableCol(1)
    entries = {}
    for name, definition in definitions.items():
        unit = definition['unit']
        minimum, maximum = definition['minimum'], definition['maximum']
        label = f'{name.replace("_", " ")} ({unit}; {minimum} to {maximum})'
        grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control = wx.TextCtrl(dialog, value=str(definition['default']))
        control.SetName(label)
        entries[name] = control
        grid.Add(control, 1, wx.EXPAND)
    layout.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
    layout.Add(dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
    dialog.SetSizerAndFit(layout)
    dialog.SetMinSize(dialog.GetSize())
    next(iter(entries.values())).SetFocus()
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        try:
            parameters = tuple((name, float(control.GetValue())) for name, control in entries.items())
        except (TypeError, ValueError) as exc:
            raise ValueError('Every effect setting must be a number in its displayed range.') from exc
        return role, Effect(kind, parameters)
    finally:
        dialog.Destroy()


def effect_reference():
    lines = ['Local effect controls', '',
             'Use Structured production script and Add role effect.',
             'The effect is inserted before the chosen role speaks. Each role has at most 16 effects.',
             'Reverb uses four reflections. Pitch and tempo are separate controls.',
             'No effect downloads a model or changes the original source.', '']
    for kind, parameters in effect_controls().items():
        lines.append(kind)
        if not parameters:
            lines.append('  Uses the bounded FFmpeg default settings.')
        for name, spec in parameters.items():
            lines.append(f'  {name}: default {spec["default"]}; '
                         f'{spec["minimum"]} to {spec["maximum"]} {spec["unit"]}')
        lines.append('')
    return '\n'.join(lines)
