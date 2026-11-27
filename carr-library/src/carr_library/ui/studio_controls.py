"""Named native controls for the current structured Studio draft."""
from __future__ import annotations

from ..audio.models import FORMATS
from ..audio.studio_controls import (
    ScriptProductionEditor, role_fields, role_values, saved_voice_options, voice_options,
)


def show_production_editor(parent, editor, rows, *, current=lambda: True,
                           focus_output=False, on_ready=None):
    """Keep form edits private until explicit Apply; no save/render is started."""
    import wx

    if not isinstance(editor, ScriptProductionEditor):
        raise TypeError('Open production controls for the current structured script.')
    options = voice_options(rows)
    dialog = wx.Dialog(parent, title='Roles, voices and final audio', size=(820, 780),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Structured Studio roles and final audio')
    accepted = None
    selected_role = editor.roles[0]
    visible_voices, visible_languages = (), ()
    fields, buttons = {}, {}
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        'Structured production script only. Apply makes one undoable draft edit.\n'
        'Cancel leaves the draft unchanged. Save and preview before rendering.\n'
        'An unavailable saved voice stays selected; no replacement or language fallback is chosen.')),
        0, wx.EXPAND | wx.ALL, 10)
    grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
    grid.AddGrowableCol(1)

    def add(name, label, control):
        grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control.SetName(label)
        fields[name] = control
        grid.Add(control, 1, wx.EXPAND)
        return control

    role_choice = add('role', 'Production role', wx.Choice(dialog, choices=list(editor.roles)))
    voice_choice = add('voice', 'Installed voice for this role', wx.Choice(dialog))
    language_choice = add('language', 'Exact advertised voice language', wx.Choice(dialog))
    for name, label in (
            ('pan', 'Pan: minus 1 left, 0 centre, 1 right'),
            ('rate', 'Speech rate: engine scale, minus 10 to 10'),
            ('pitch', 'Speech pitch: engine scale, minus 10 to 10'),
            ('volume', 'Speech volume: 0 to 100 percent'),
            ('gain_db', 'Final role gain: minus 120 to 24 dB')):
        add(name, label, wx.TextCtrl(dialog))
    gain_enabled = add('gain_enabled', 'Enable final role gain',
                       wx.CheckBox(dialog, label='Enable final role gain'))
    layout.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
    capabilities = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 95))
    capabilities.SetName('Selected voice capabilities and retained effects')
    fields['capabilities'] = capabilities
    layout.Add(capabilities, 0, wx.EXPAND | wx.ALL, 10)
    output_grid = wx.FlexGridSizer(cols=2, vgap=6, hgap=10)
    output_grid.AddGrowableCol(1)
    output_grid.Add(wx.StaticText(dialog, label='Final audio format'), 0, wx.ALIGN_CENTER_VERTICAL)
    output_format = wx.Choice(dialog, choices=[name.upper() for name in FORMATS])
    output_format.SetName('Final audio format')
    output_format.SetSelection(FORMATS.index(editor.output_format))
    fields['format'] = output_format
    output_grid.Add(output_format, 1, wx.EXPAND)
    output_grid.Add(wx.StaticText(dialog, label='Separate audio stems'), 0, wx.ALIGN_CENTER_VERTICAL)
    stems = wx.CheckBox(dialog, label='Export separate role and background stems')
    stems.SetName('Export separate role and background stems')
    stems.SetValue(editor.export_stems)
    fields['stems'] = stems
    output_grid.Add(stems, 1, wx.EXPAND)
    layout.Add(output_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
    layout.Add(wx.StaticText(dialog, label=(
        f'Sample rate remains {editor.plan.sample_rate} Hz. Existing disk budget and master effects are retained.\n'
        'Gain changes only the final role gain step. Earlier effects stay in their original order.\n'
        'These are draft settings; codec availability is checked by the existing preview/render workflow.')),
        0, wx.EXPAND | wx.ALL, 10)
    status = wx.TextCtrl(dialog, value='Review a role or the final audio settings.',
                        style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 60))
    status.SetName('Role and output editing status')
    fields['status'] = status
    layout.Add(status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

    def report(message):
        status.ChangeValue(str(message))
        status.SetFocus()

    def assert_current():
        if not current():
            raise ValueError('The Studio draft or its owner changed. Cancel and review the current project again.')

    def selected_voice():
        index = voice_choice.GetSelection()
        if not 0 <= index < len(visible_voices):
            raise ValueError('Choose an installed voice or inspect the retained unavailable voice.')
        return visible_voices[index]

    def selected_language():
        index = language_choice.GetSelection()
        if not 0 <= index < len(visible_languages):
            raise ValueError('Choose an exact advertised voice language.')
        return visible_languages[index]

    def show_capabilities(language):
        nonlocal visible_languages
        voice = selected_voice()
        visible_languages = voice.languages
        if language not in visible_languages:
            visible_languages = (language, *visible_languages)
        language_choice.SetItems([
            code if code in voice.languages else 'Unavailable saved language — ' + code
            for code in visible_languages])
        language_choice.SetSelection(visible_languages.index(language))
        messages = [voice.name + ' — ' + voice.id,
                    ('Available controls: ' + ', '.join(voice.controls) if voice.available
                     else 'Unavailable: ' + (voice.reason or 'The saved voice is not installed.'))]
        for name, default in (('rate', 0), ('pitch', 0), ('volume', 100)):
            supported = voice.available and name in voice.controls
            try:
                nondefault = float(fields[name].GetValue()) != default
            except ValueError:
                nondefault = True
            # Unsupported inherited values stay visible and can be reset
            # explicitly, rather than being silently dropped on voice change.
            fields[name].Enable(supported or nondefault)
            if not supported:
                messages.append(f'{name.capitalize()} is unsupported; only its default {default} can be applied.')
        role = editor.role(selected_role)
        messages.append(f'{len(role.effects)} retained role effect step(s); only a final gain step is edited here.')
        if not voice.available:
            messages.append('Output-only changes may keep this saved role unchanged.')
        capabilities.ChangeValue('\n'.join(messages))

    def load_role(name):
        nonlocal selected_role, visible_voices
        selected_role = name
        role = editor.role(name)
        values = role_values(role)
        visible_voices = saved_voice_options(options, role.voice)
        voice_choice.SetItems([
            ('Unavailable — ' if not item.available else '') + item.name + ' — ' + item.id
            for item in visible_voices])
        voice_choice.SetSelection(next(index for index, item in enumerate(visible_voices) if item.id == role.voice))
        for key in ('pan', 'rate', 'pitch', 'volume', 'gain_db'):
            fields[key].ChangeValue(values[key])
        gain_enabled.SetValue(values['gain_enabled'])
        role_choice.SetSelection(editor.roles.index(name))
        show_capabilities(role.language)

    def retain_role():
        values = {name: fields[name].GetValue() for name in ('pan', 'rate', 'pitch', 'volume', 'gain_db')}
        values.update(voice=selected_voice().id, language=selected_language(),
                      gain_enabled=gain_enabled.GetValue())
        editor.set_role(selected_role, role_fields(editor.role(selected_role), values, options))

    def select_role(event):
        try:
            assert_current()
            index = role_choice.GetSelection()
            if not 0 <= index < len(editor.roles):
                raise ValueError('Choose an existing role.')
            name = editor.roles[index]
            retain_role()
            load_role(name)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            role_choice.SetSelection(editor.roles.index(selected_role))
            report(exc)

    def select_voice(event):
        try:
            assert_current()
            # Preserve the current exact language even if this voice does not
            # advertise it. The user must choose a new language explicitly.
            show_capabilities(selected_language())
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            report(exc)

    def apply(event):
        nonlocal accepted
        try:
            assert_current()
            retain_role()
            index = output_format.GetSelection()
            if not 0 <= index < len(FORMATS):
                raise ValueError('Choose a final audio format.')
            editor.set_output(FORMATS[index], stems.GetValue())
            result = editor.script()
            assert_current()
            accepted = result
            dialog.EndModal(wx.ID_OK)
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            report(exc)

    row = wx.BoxSizer(wx.HORIZONTAL)
    for identifier, key, label in ((wx.ID_OK, 'accept', 'Apply role and output changes'),
                                   (wx.ID_CANCEL, 'cancel', 'Cancel role and output changes')):
        button = wx.Button(dialog, identifier, label)
        button.SetName(label)
        if identifier == wx.ID_OK:
            button.Bind(wx.EVT_BUTTON, apply)
            button.SetDefault()
        buttons[key] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    dialog.SetSizer(layout)
    dialog.SetMinSize((740, 740))
    role_choice.Bind(wx.EVT_CHOICE, select_role)
    voice_choice.Bind(wx.EVT_CHOICE, select_voice)
    load_role(selected_role)
    (output_format if focus_output else role_choice).SetFocus()
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, fields, buttons, status)
        return accepted if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
