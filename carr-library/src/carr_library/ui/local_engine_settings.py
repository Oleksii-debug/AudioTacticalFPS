"""Native explicit configuration for the sole Audio Studio engine registry."""
from dataclasses import asdict

from ..audio.engine_settings import EngineSettings, load_settings, save_settings


def show_local_engine_settings(frame):
    import wx

    try:
        settings = load_settings(frame.library.root)
    except Exception as exc:
        frame.report('Local engine settings could not be opened: ' + str(exc))
        return
    dialog = wx.Dialog(frame, title='Local voice and audio settings', size=(900, 500),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    description = wx.TextCtrl(dialog, value=(
        'Installed Windows SAPI voices are discovered automatically. Optional programs and models must '
        'already be installed and reviewed. Choose their local files explicitly. No model is downloaded '
        'and no external account is contacted. Saved projects cannot choose executables.'),
        style=wx.TE_MULTILINE | wx.TE_READONLY)
    description.SetName('Local engine configuration instructions')
    layout.Add(description, 0, wx.EXPAND | wx.ALL, 8)
    fields = {}
    labels = {'ffmpeg': 'FFmpeg executable (blank uses installed discovery)',
              'piper_executable': 'Reviewed Piper executable',
              'piper_manifest': 'Verified Piper model manifest',
              'dordiai_manifest': 'Verified dordiai-voices model manifest'}
    for key, label in labels.items():
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 8)
        row = wx.BoxSizer(wx.HORIZONTAL)
        control = wx.TextCtrl(dialog, value=asdict(settings)[key] or '')
        control.SetName(label)
        fields[key] = control
        row.Add(control, 1, wx.EXPAND | wx.ALL, 8)
        browse = wx.Button(dialog, label='Browse...')
        browse.SetName('Browse for ' + label)
        row.Add(browse, 0, wx.ALL, 8)

        def choose(event, destination=control, title=label):
            with wx.FileDialog(dialog, title, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as picker:
                if picker.ShowModal() == wx.ID_OK:
                    destination.ChangeValue(picker.GetPath())

        browse.Bind(wx.EVT_BUTTON, choose)
        layout.Add(row, 0, wx.EXPAND)
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local engine settings save status')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 8)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    save = wx.Button(dialog, label='Save local settings')
    close = wx.Button(dialog, wx.ID_CANCEL, 'Close')
    buttons.Add(save, 0, wx.ALL, 8)
    buttons.Add(close, 0, wx.ALL, 8)
    layout.Add(buttons, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)

    def save_selected(event):
        try:
            value = EngineSettings(**{key: field.GetValue().strip() or None for key, field in fields.items()})
            save_settings(frame.library.root, value)
            status.ChangeValue('Local settings saved. Reload voices in the reader or reopen Audio Studio to apply them.')
        except Exception as exc:
            status.ChangeValue('Settings were not saved: ' + str(exc))
        status.SetFocus()

    save.Bind(wx.EVT_BUTTON, save_selected)
    try:
        fields['ffmpeg'].SetFocus()
        dialog.ShowModal()
    finally:
        dialog.Destroy()
