"""Explicit native choice of derivative audio in a complete library backup."""


def choose_backup_options(parent):
    import wx
    dialog = wx.Dialog(parent, title='Library backup contents', size=(620, 300))
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        description = wx.StaticText(dialog, label=(
            'Back up completed libraries, original files, research notes, saved '
            'projects and their input audio. Unfinished imports are excluded. '
            'Generated audio can make the backup substantially larger.'))
        description.Wrap(560)
        layout.Add(description, 0, wx.EXPAND | wx.ALL, 12)
        generated = wx.CheckBox(dialog, label='Include generated audio saved in this library')
        generated.SetName('Include generated audio saved in this library')
        generated.SetValue(False)
        layout.Add(generated, 0, wx.EXPAND | wx.ALL, 12)
        layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0,
                   wx.EXPAND | wx.ALL, 12)
        dialog.SetSizer(layout)
        generated.SetFocus()
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return bool(generated.GetValue())
    finally:
        dialog.Destroy()
