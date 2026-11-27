"""Native, named controls for typed local audio effects and explicit presets."""
from __future__ import annotations

from ..audio.models import Effect, effect_controls


def effect_fields(kind, fields, *, existing=None):
    """Validate native numeric text without changing omitted legacy defaults."""
    definitions = effect_controls().get(kind)
    if definitions is None or type(fields) is not dict or set(fields) != set(definitions):
        raise ValueError('Enter each displayed setting for the chosen effect.')
    if existing is not None and (not isinstance(existing, Effect) or existing.kind != kind):
        raise ValueError('The edited effect kind changed.')
    values = {}
    try:
        for name, raw in fields.items():
            if not isinstance(raw, str):
                raise ValueError('Enter numeric text.')
            values[name] = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('Every effect setting must be a number in its displayed range.') from exc
    if existing is None:
        parameters = tuple(values.items())
    else:
        previous = dict(existing.parameters)
        ordered = [name for name, _ in existing.parameters]
        ordered.extend(name for name, spec in definitions.items()
                       if name not in previous and values[name] != spec['default'])
        parameters = tuple((name, values[name]) for name in ordered)
    return Effect(kind, parameters, enabled=True if existing is None else existing.enabled)


def edit_effect_parameters(parent, role, effect, *, new=False):
    import wx

    definitions = effect_controls()[effect.kind]
    if not definitions:
        return effect
    dialog = wx.Dialog(parent, title=f'{effect.kind} — {role}',
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName(f'Parameters for {effect.kind} on {role}')
    layout = wx.BoxSizer(wx.VERTICAL)
    description = ('Enter the numeric settings below. Preview the plan before rendering.' if new else
                   'Edit the numeric settings. Unchanged omitted settings keep their original defaults.')
    layout.Add(wx.StaticText(dialog, label=description), 0, wx.ALL, 10)
    grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=12)
    grid.AddGrowableCol(1)
    previous, entries = dict(effect.parameters), {}
    for name, definition in definitions.items():
        unit = definition['unit']
        minimum, maximum = definition['minimum'], definition['maximum']
        label = f'{name.replace("_", " ")} ({unit}; {minimum} to {maximum})'
        grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control = wx.TextCtrl(dialog, value=str(previous.get(name, definition['default'])))
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
        return effect_fields(effect.kind, {name: control.GetValue() for name, control in entries.items()},
                             existing=None if new else effect)
    finally:
        dialog.Destroy()


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
    effect = edit_effect_parameters(parent, role, Effect(kind), new=True)
    return None if effect is None else (role, effect)


def effect_summary(effect):
    parameters = ', '.join(f'{name.replace("_", " ")} {value:g}' for name, value in effect.parameters)
    return (effect.kind + (' — enabled' if effect.enabled else ' — bypassed')
            + (': ' + parameters if parameters else ': original defaults'))


def show_effect_editor(parent, editor, *, private_root, protect_destination=lambda path: None,
                       on_ready=None):
    """Keep pending chain edits isolated until the native Apply button is used."""
    import wx
    from pathlib import Path
    from ..audio.effect_editing import RoleEffectEditor, load_preset, save_preset
    from .export_destination import approve_export_destination

    if not isinstance(editor, RoleEffectEditor):
        raise TypeError('Open an effect editor for the current production script.')
    dialog = wx.Dialog(parent, title='Role and master effect chains', size=(900, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Role and master effect chains')
    accepted, loaded = None, None
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        'Effects run from top to bottom. Bypass retains a step and its settings.\n'
        'Apply chain changes updates the reading draft; Cancel keeps its current script.\n'
        'Saving a preset writes a separate file immediately. Original text and audio stay unchanged.')),
        0, wx.EXPAND | wx.ALL, 8)
    layout.Add(wx.StaticText(dialog, label='Processing target'), 0, wx.LEFT | wx.TOP, 8)
    role_choice = wx.Choice(dialog, choices=[f'Role: {name}' for name in editor.roles]
                            + ['Master: entire programme'])
    role_choice.SetName('Effect chain target')
    role_choice.SetSelection(0)
    layout.Add(role_choice, 0, wx.EXPAND | wx.ALL, 8)
    layout.Add(wx.StaticText(dialog, label=(
        'Master dry/wet: 0 keeps dry audio; 1 uses the complete chain. Linear blend, no automatic gain normalization.\n'
        'Tempo and pitch are role-only. Master chapters keep exact frame boundaries; stems remain pre-master.')),
        0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
    mix = wx.TextCtrl(dialog, value=str(editor.master_mix))
    mix.SetName('Master dry wet mix from zero to one')
    layout.Add(mix, 0, wx.EXPAND | wx.ALL, 8)
    chain = wx.ListBox(dialog, style=wx.LB_SINGLE | wx.LB_HSCROLL, size=(-1, 160))
    chain.SetName('Effects in processing order')
    layout.Add(chain, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
    buttons = {}
    status = wx.TextCtrl(dialog, value='Choose a role or master and an effect to edit.',
                        style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 65))
    status.SetName('Effect editing status')

    def current_role():
        index = role_choice.GetSelection()
        if not 0 <= index < len(editor.targets):
            raise ValueError('Choose a role or the master.')
        return editor.targets[index]

    def target_name(role):
        return 'Master: entire programme' if role is None else role

    def current_index():
        index = chain.GetSelection()
        if not 0 <= index < len(editor.effects(current_role())):
            raise ValueError('Choose an effect in the current role chain.')
        return index

    def selected(event=None):
        effects, index = editor.effects(current_role()), chain.GetSelection()
        valid = 0 <= index < len(effects)
        for name in ('edit', 'bypass', 'remove'):
            if name in buttons:
                buttons[name].Enable(valid)
        if 'up' in buttons:
            buttons['up'].Enable(valid and index > 0)
            buttons['down'].Enable(valid and index + 1 < len(effects))
            buttons['add'].Enable(len(effects) < 16)
            label = 'Bypass effect' if valid and effects[index].enabled else 'Enable effect'
            buttons['bypass'].SetLabel(label)
            buttons['bypass'].SetName(label)

    def refresh(index=0):
        effects = editor.effects(current_role())
        mix.Enable(current_role() is None)
        chain.SetItems([f'{number}. {effect_summary(effect)}' for number, effect in enumerate(effects, 1)])
        chain.SetSelection(min(index, len(effects) - 1) if effects else wx.NOT_FOUND)
        selected()

    def report(message):
        status.ChangeValue(str(message))
        status.SetFocus()

    def change(action):
        try:
            role = current_role()
            index = chain.GetSelection()
            if action == 'add':
                result = choose_role_effect(dialog, (target_name(role),))
                if result is None:
                    return
                editor.add(role, result[1])
                index = len(editor.effects(role)) - 1
            elif action == 'edit':
                index = current_index()
                result = edit_effect_parameters(dialog, target_name(role), editor.effects(role)[index])
                if result is None:
                    return
                editor.edit(role, index, result)
            elif action in ('up', 'down'):
                index = current_index()
                target = index + (-1 if action == 'up' else 1)
                editor.move(role, index, target)
                index = target
            elif action == 'bypass':
                index = current_index()
                editor.bypass(role, index)
            elif action == 'remove':
                index = current_index()
                editor.remove(role, index)
            refresh(max(0, index))
            report('Pending chain updated. Choose Apply chain changes to keep it in the draft.')
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            report(exc)

    row = wx.BoxSizer(wx.HORIZONTAL)
    for action, label in (('add', 'Add effect'), ('edit', 'Edit settings'), ('up', 'Move up'),
                          ('down', 'Move down'), ('bypass', 'Bypass effect'), ('remove', 'Remove effect')):
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, lambda event, choice=action: change(choice))
        buttons[action] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND | wx.LEFT, 4)
    layout.Add(wx.StaticText(dialog, label='Name for a saved preset'), 0, wx.LEFT | wx.TOP, 8)
    preset_name = wx.TextCtrl(dialog, value='')
    preset_name.SetName('Effect preset name')
    layout.Add(preset_name, 0, wx.EXPAND | wx.ALL, 8)
    preview = wx.TextCtrl(dialog, value='No preset loaded.', style=wx.TE_READONLY | wx.TE_MULTILINE,
                          size=(-1, 85))
    preview.SetName('Loaded effect preset preview')
    layout.Add(preview, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

    def preset_action(action):
        nonlocal loaded
        try:
            role = current_role()
            if action == 'apply_preset':
                editor.apply_preset(role, loaded)
                refresh()
                report(f'Preset "{loaded.name}" replaces the pending chain for {target_name(role)}. Apply changes to keep it.')
                return
            preset = editor.preset(role, preset_name.GetValue()) if action == 'save_preset' else None
            picker = wx.FileDialog(dialog,
                'Save a reusable effect preset' if preset else 'Load an effect preset',
                wildcard='Effect presets (*.json)|*.json',
                style=wx.FD_SAVE if preset else wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                path = Path(picker.GetPath())
            finally:
                picker.Destroy()
            if preset is not None:
                protect_destination(path)
                approval = approve_export_destination(dialog, path, private_root=private_root)
                if approval is None:
                    return
                save_preset(path, preset, private_root=private_root, approval=approval)
                report(f'Preset "{preset.name}" saved separately. The project draft is not yet changed.')
            else:
                candidate = load_preset(path)
                loaded = candidate
                preset_name.ChangeValue(candidate.name)
                preview.ChangeValue(candidate.name + '\n' + (
                    '\n'.join(f'{index}. {effect_summary(effect)}' for index, effect in enumerate(candidate.effects, 1))
                    or 'Empty chain: applying it removes every effect from the selected target.'))
                buttons['apply_preset'].Enable(True)
                report('Preset checked. Review its steps, choose a role or master, then Apply loaded preset. Master restrictions apply.')
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            report(exc)

    row = wx.BoxSizer(wx.HORIZONTAL)
    for action, label in (('load_preset', 'Load preset...'), ('apply_preset', 'Apply loaded preset'),
                          ('save_preset', 'Save named preset...')):
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, lambda event, choice=action: preset_action(choice))
        buttons[action] = button
        row.Add(button, 0, wx.ALL, 4)
    buttons['apply_preset'].Enable(False)
    layout.Add(row, 0, wx.EXPAND | wx.LEFT, 4)
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 8)

    def apply(event):
        nonlocal accepted
        try:
            editor.set_master_mix(float(mix.GetValue()))
            accepted = editor.script()
            dialog.EndModal(wx.ID_OK)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            report(exc)

    row = wx.BoxSizer(wx.HORIZONTAL)
    for identifier, key, label in ((wx.ID_OK, 'accept', 'Apply chain changes'),
                                    (wx.ID_CANCEL, 'cancel', 'Cancel chain changes')):
        button = wx.Button(dialog, identifier, label)
        button.SetName(label)
        if identifier == wx.ID_OK:
            button.Bind(wx.EVT_BUTTON, apply)
        buttons[key] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
    dialog.SetSizer(layout)
    dialog.SetMinSize((750, 720))
    role_choice.Bind(wx.EVT_CHOICE, lambda event: refresh())
    chain.Bind(wx.EVT_LISTBOX, selected)
    refresh()
    role_choice.SetFocus()
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, {'role': role_choice, 'chain': chain,
                'master_mix': mix, 'preset_name': preset_name, 'preset_preview': preview}, buttons, status)
        return accepted if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def effect_reference():
    lines = ['Local effect controls', '',
             'Use Structured production script and Role and master effect chains.',
             'Edit, move, bypass or remove steps, then Apply chain changes.',
             'Each role and the master has at most 16 effects; they run in the displayed order.',
             'Load a named JSON preset, review it, then apply it to one selected role or the master.',
             'Save named preset writes a separate reusable file after destination approval.',
             'Preset files contain only a name and typed numeric effects, never voices or executable paths.',
             'Reverb uses four reflections. Pitch and tempo are separate controls.',
             'Master dry/wet uses a linear blend from 0 (dry) to 1 (fully processed), with no automatic gain normalization.',
             'The master uses a 0.95 safety ceiling. Tempo and pitch remain role-only before timing is measured.',
             'Master chapter files are exact frame slices of the processed programme; stems are pre-master.',
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
