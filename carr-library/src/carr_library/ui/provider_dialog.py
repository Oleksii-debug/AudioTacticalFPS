"""Native opt-in provider configuration. Opening this dialog never reads a key."""
from dataclasses import asdict

from ..providers import ProviderManager, ProviderProfile


def show_provider_dialog(frame):
    import wx
    manager = ProviderManager(frame.library.root / 'provider-settings')
    dialog = wx.Dialog(frame, title='Optional providers and API keys', size=(760, 710))
    layout = wx.BoxSizer(wx.VERTICAL)
    explanation = wx.StaticText(dialog, label=(
        'LOCAL ONLY is the default. Provider profiles are optional. Saving settings makes no network call. '
        'Each request needs its own destination, data and budget approval. API keys are stored in the OS credential vault.'))
    explanation.Wrap(700)
    layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
    hybrid = wx.CheckBox(dialog, label='Allow explicitly approved provider requests: HYBRID mode')
    hybrid.SetName('Optional provider network policy')
    hybrid.SetValue(manager.mode == 'HYBRID')
    layout.Add(hybrid, 0, wx.ALL, 8)
    existing = wx.Choice(dialog)
    existing.SetName('Saved provider profile')
    layout.Add(wx.StaticText(dialog, label='Saved profile; choose one to edit or enter a new ID'), 0, wx.LEFT, 8)
    layout.Add(existing, 0, wx.EXPAND | wx.ALL, 8)
    grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
    grid.AddGrowableCol(1)
    controls = {}
    for field, label, value in (
        ('id', 'Profile ID', ''), ('name', 'Provider name', ''),
        ('endpoint', 'Exact HTTPS endpoint or local loopback endpoint', ''),
        ('model', 'Model or engine identifier', ''),
        ('request_budget_cents', 'Maximum reservation per request, cents', '0'),
        ('total_budget_cents', 'Total reservation budget, cents', '0')):
        grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control = wx.TextCtrl(dialog, value=value)
        control.SetName(label)
        controls[field] = control
        grid.Add(control, 0, wx.EXPAND)
    grid.Add(wx.StaticText(dialog, label='Provider task'))
    task = wx.Choice(dialog, choices=['llm', 'tts', 'music', 'transcription'])
    task.SetName('Provider task')
    task.SetSelection(0)
    grid.Add(task, 0, wx.EXPAND)
    grid.Add(wx.StaticText(dialog, label='New API key; blank retains the saved key'))
    key = wx.TextCtrl(dialog, style=wx.TE_PASSWORD)
    key.SetName('New API key; never displayed or exported')
    grid.Add(key, 0, wx.EXPAND)
    layout.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
    enabled = wx.CheckBox(dialog, label='Enable this provider for approved requests')
    enabled.SetName('Provider enabled')
    needs_key = wx.CheckBox(dialog, label='This provider requires an API key')
    needs_key.SetValue(True)
    layout.Add(enabled, 0, wx.ALL, 8)
    layout.Add(needs_key, 0, wx.ALL, 8)
    status = wx.TextCtrl(dialog, value='No provider request has been made.', style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Provider configuration status')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 8)
    profiles = []

    def refresh():
        profiles[:] = manager.profiles()
        existing.SetItems([f'{p.name}; {p.task}; {"enabled" if p.enabled else "disabled"}; {p.id}' for p in profiles])

    def load(event):
        i = existing.GetSelection()
        if not 0 <= i < len(profiles):
            return
        profile = profiles[i]
        for name, control in controls.items():
            control.ChangeValue(str(getattr(profile, name)))
        task.SetStringSelection(profile.task)
        enabled.SetValue(profile.enabled)
        needs_key.SetValue(profile.requires_key)
        key.ChangeValue('')
        status.ChangeValue('Profile loaded. Its stored API key was not read or displayed.')

    def save(event):
        try:
            values = {name: control.GetValue().strip() for name, control in controls.items()}
            for name in ('request_budget_cents', 'total_budget_cents'):
                values[name] = int(values[name])
            profile = ProviderProfile(**values, task=task.GetStringSelection(), enabled=enabled.GetValue(), requires_key=needs_key.GetValue())
            secret = key.GetValue()
            manager.save(profile, key=secret if secret else None)
            manager.set_mode('HYBRID' if hybrid.GetValue() else 'LOCAL_ONLY')
            key.ChangeValue('')
            refresh()
            status.ChangeValue('Profile saved. Policy: ' + manager.mode + '. No request was sent. Budget values are local reservations, not provider price guarantees.')
        except Exception as exc:
            status.ChangeValue(f'Could not save provider settings: {exc}. Your entered settings are retained.')
        status.SetInsertionPoint(0)
        status.SetFocus()

    def policy(event):
        manager.set_mode('HYBRID' if hybrid.GetValue() else 'LOCAL_ONLY')
        status.ChangeValue('Network policy saved: ' + manager.mode + '. No provider request was sent.')
        status.SetFocus()

    def selected_profile():
        index = existing.GetSelection()
        if not 0 <= index < len(profiles):
            raise ValueError('Choose a saved provider profile first.')
        return profiles[index]

    def disable(event):
        try:
            profile = selected_profile()
            manager.disable(profile.id)
            key.ChangeValue('')
            enabled.SetValue(False)
            refresh()
            status.ChangeValue('Provider disabled. Its settings, OS-vault key and request history were retained; enable it again when needed.')
        except Exception as exc:
            status.ChangeValue(f'Could not disable the provider: {exc}')
        status.SetInsertionPoint(0)
        status.SetFocus()

    def remove(event):
        try:
            profile = selected_profile()
            answer = wx.MessageBox(f'Remove provider "{profile.name}" and its saved API key? '
                'Request receipts will remain. Disable the provider instead if you want to retain its key.',
                'Remove optional provider', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, dialog)
            if answer != wx.YES:
                return
            result = manager.remove(profile.id)
            key.ChangeValue('')
            refresh()
            message = 'Provider removed. Request receipts were retained.'
            if result['credential_removed']:
                message += ' Its saved OS-vault key was removed.'
            else:
                message += ' The OS vault could not remove its key; remove the unused entry in your operating-system credential manager.'
            status.ChangeValue(message)
        except Exception as exc:
            status.ChangeValue(f'Could not remove the provider: {exc}')
        status.SetInsertionPoint(0)
        status.SetFocus()

    existing.Bind(wx.EVT_CHOICE, load)
    buttons = wx.GridSizer(cols=3, hgap=4, vgap=4)
    for label, handler in (('&Save profile', save), ('Save network &policy', policy),
                           ('&Disable selected profile', disable), ('&Remove selected profile and key', remove)):
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        buttons.Add(button, 0, wx.ALL, 8)
    buttons.Add(wx.Button(dialog, wx.ID_CANCEL, '&Close'), 0, wx.ALL, 8)
    layout.Add(buttons, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)
    refresh()
    controls['id'].SetFocus()
    try:
        dialog.ShowModal()
    finally:
        key.ChangeValue('')
        dialog.Destroy()


def install_provider_actions(frame):
    import wx
    index = frame.menu_bar.FindMenu('Tools')
    if index == wx.NOT_FOUND:
        menu = wx.Menu()
        frame.menu_bar.Append(menu, '&Tools')
    else:
        menu = frame.menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Optional providers and API keys')
    frame.Bind(wx.EVT_MENU, lambda event: show_provider_dialog(frame), item)
