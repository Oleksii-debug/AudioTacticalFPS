"""Native metadata entry for the existing Studio's synchronized book exporters."""
def show_publication_options(parent, book_format, *, language='und', on_ready=None):
    import wx
    from ..audio.publishing import language_tag, _text
    if book_format not in ('epub', 'daisy-2.02'):
        raise ValueError('Choose EPUB or DAISY publication.')
    dialog = wx.Dialog(parent, title='Synchronized audiobook metadata',
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER, size=(700, 460))
    layout, fields, result = wx.BoxSizer(wx.VERTICAL), {}, []
    for name, label, value in (
            ('language', 'Publication language code', language or 'und'),
            ('creator', 'Creator name (optional)', ''),
            ('publisher', 'Publisher (required for DAISY)', '')):
        if name == 'publisher' and book_format != 'daisy-2.02':
            continue
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 5)
        control = wx.TextCtrl(dialog, value=value)
        control.SetName(label)
        fields[name] = control
        layout.Add(control, 0, wx.EXPAND | wx.ALL, 5)
    status = wx.TextCtrl(dialog, value=(
        'Speech is synchronized at measured line boundaries, without word alignment. '
        'Audio between lines remains explicitly untranscribed. The exact source and provenance '
        'are retained. Choose a new output filename; existing books are never overwritten.'),
        style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Audiobook export scope and validation status')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)
    buttons = dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 7)
    dialog.SetSizer(layout)

    def confirm(event):
        try:
            value = {'language': language_tag(fields['language'].GetValue().strip()),
                     'creator': _text(fields['creator'].GetValue(), 'Creator', maximum=512, empty=True)}
            if book_format == 'daisy-2.02':
                from ..audio.daisy import language_tag as daisy_language
                daisy_language(value['language'])
                value['publisher'] = _text(fields['publisher'].GetValue(), 'Publisher', maximum=512)
        except ValueError as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()
            return
        result.append(value)
        dialog.EndModal(wx.ID_OK)

    dialog.Bind(wx.EVT_BUTTON, confirm, id=wx.ID_OK)
    try:
        fields['language'].SetFocus()
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, fields, status)
        return result[0] if dialog.ShowModal() == wx.ID_OK and result else None
    finally:
        dialog.Destroy()
