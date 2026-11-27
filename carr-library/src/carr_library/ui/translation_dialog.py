"""Native text translation through the existing optional connection gateway."""
from pathlib import Path

from ..providers import MAX_TRANSLATION_CHARACTERS, ProviderError, ProviderRequest
from .api_connections import ConnectionsController, parse_budget, show_api_connections


def translation_request(text, source_language, target_language):
    """Keep exact source text; normalize only language-control whitespace."""
    if not isinstance(source_language, str) or not isinstance(target_language, str):
        raise ProviderError('Enter source and target language codes as text.')
    return ProviderRequest('translation', text,
        source_language=source_language.strip() or None,
        target_language=target_language.strip())


def show_translation_result(frame, result):
    import wx
    dialog = wx.Dialog(frame, title='Translation result', size=(850, 600))
    layout = wx.BoxSizer(wx.VERTICAL)
    status = wx.TextCtrl(dialog, value='Translation saved with its original text and provenance:\n'
        + result['output'], style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Saved translation information')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 8)
    text = wx.TextCtrl(dialog, value=result['translation_text'], style=wx.TE_MULTILINE | wx.TE_READONLY)
    text.SetName('Translated text')
    layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 8)
    dialog.Bind(wx.EVT_BUTTON, lambda event: dialog.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
    dialog.SetSizer(layout)
    text.SetFocus()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def show_translation_dialog(frame, source_text=''):
    """Translate pasted or explicitly selected text; opening stays offline."""
    import wx
    controller = ConnectionsController(frame.library.root)

    class Dialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Translate text — Google Basic v2', size=(850, 700))
            self.rows = []
            self.task = None
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(self, label=(
                f'Translate one plain-text excerpt of up to {MAX_TRANSLATION_CHARACTERS} characters. '
                'Your original is preserved. Google requires internet and your own connected API account.')),
                0, wx.EXPAND | wx.ALL, 8)
            fields = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
            fields.AddGrowableCol(1)
            self.profiles = wx.Choice(self)
            self.source_language = wx.TextCtrl(self, value='')
            self.target_language = wx.TextCtrl(self, value='uk')
            self.budget = wx.TextCtrl(self, value='')
            for label, control, name in (
                    ('Google &account', self.profiles, 'Google Translation account'),
                    ('&Source code (blank: automatic)', self.source_language, 'Source language code; blank for automatic detection'),
                    ('&Target code (for example uk, en, pl)', self.target_language, 'Target language code'),
                    ('Estimated &maximum cost in USD', self.budget, 'Reviewed maximum estimated cost in USD')):
                fields.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                control.SetName(name)
                fields.Add(control, 1, wx.EXPAND)
            layout.Add(fields, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='&Original text'), 0, wx.LEFT | wx.TOP, 8)
            self.text = wx.TextCtrl(self, value=source_text, style=wx.TE_MULTILINE)
            self.text.SetName('Original text to translate')
            layout.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.status.SetName('Translation status')
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            for label, action in (('&Manage API connections', self.manage),
                                  ('&Review and translate...', self.run)):
                button = wx.Button(self, label=label)
                button.Bind(wx.EVT_BUTTON, action)
                buttons.Add(button, 0, wx.ALL, 4)
            layout.Add(buttons, 0, wx.ALL, 4)
            layout.Add(self.CreateButtonSizer(wx.CANCEL), 0, wx.ALL, 8)
            self.SetSizer(layout)
            self.refresh()
            self.text.SetFocus()

        def refresh(self):
            self.rows = [profile for profile in controller.store.profiles()
                         if profile.provider == 'google-translate-v2']
            self.profiles.SetItems([profile.name for profile in self.rows])
            if self.rows:
                self.profiles.SetSelection(0)
            self.status.ChangeValue(
                'No request has been sent. Language support depends on Google Basic v2; '
                'unsupported languages fail without substitution. A saved result includes both texts.'
                if self.rows else 'Add a Google Cloud Translation Basic v2 profile through Manage API connections.')

        def manage(self, event):
            try:
                show_api_connections(frame)
                self.refresh()
            except Exception as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()

        def run(self, event):
            try:
                choice = self.profiles.GetSelection()
                if not 0 <= choice < len(self.rows):
                    raise ProviderError('Choose a connected Google Translation profile.')
                profile = self.rows[choice]
                request = translation_request(self.text.GetValue(), self.source_language.GetValue(),
                                              self.target_language.GetValue())
                estimate = parse_budget(self.budget.GetValue())
                if not profile.credential_ref:
                    raise ProviderError('Connect this profile to your Google API key before translating.')
                target = wx.FileDialog(self, 'Save translation with original text',
                    defaultFile='translation.json', wildcard='Translation document (*.json)|*.json', style=wx.FD_SAVE)
                try:
                    if target.ShowModal() != wx.ID_OK:
                        return
                    output = Path(target.GetPath())
                finally:
                    target.Destroy()
                notice = (
                    f'Account: {profile.name}\nEndpoint: {profile.endpoint}\nModel: {profile.model}\n'
                    f'Languages: {request.source_language or "automatic detection"} to {request.target_language}\n'
                    f'Text: {len(request.text)} Unicode characters\n'
                    f'Approved estimate: USD {self.budget.GetValue()}\nOutput: {output}\n\n'
                    'Send the exact text shown in this dialog to Google now? '
                    'Google billing can differ from your estimate. There is no automatic retry or language fallback.')
                if wx.MessageBox(notice, 'Approve translation and external transfer',
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    return
                self.task = (profile, request, output, estimate)
                self.EndModal(wx.ID_OK)
            except Exception as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()

    dialog = Dialog()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        task = dialog.task
    finally:
        dialog.Destroy()
    if accepted and task is not None:
        profile, request, output, estimate = task
        frame.run_job('Translating approved text',
            lambda: controller.run(profile, request, output, approved=True,
                estimate_micros=estimate, cancelled=frame.cancel_event.is_set),
            done=lambda result: show_translation_result(frame, result))


def install_translation_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, 'Translate &text...')
    frame.Bind(wx.EVT_MENU, lambda event: show_translation_dialog(frame), item)
    frame.menu_bar.Append(menu, '&Translation')
    register_menu_command(frame, CommandSpec('translation.text.open', 'Translate text'), item)
