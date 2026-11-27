"""Explicit Unicode clipboard snapshots in the existing recoverable voice reader.

Installing the command never reads the clipboard or starts a watcher or speech.
"""
from .speech_dialog import show_speech_reader


def _read_clipboard_text(wx):
    """Read one user-requested snapshot while retaining native clipboard ownership."""
    from ..speech._io import text_value
    clipboard = wx.TheClipboard
    if not clipboard.Open():
        raise ValueError('The clipboard is busy. Try again when the other application releases it.')
    try:
        if not clipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
            raise ValueError('The clipboard does not contain Unicode text to read.')
        data = wx.TextDataObject()
        if not clipboard.GetData(data):
            raise ValueError('The clipboard text could not be read. Copy the text again and retry.')
        text = data.GetText()
    finally:
        clipboard.Close()
    return text_value(text, 'Clipboard reading text')


def show_clipboard_reader(parent):
    """Open an independent reading copy without starting audio."""
    import wx
    try:
        if getattr(parent, '_closing', False):
            raise ValueError('The library is closing.')
        text = _read_clipboard_text(wx)
        show_speech_reader(parent, text=text, title='Clipboard reading copy', recovery_slot='clipboard')
    except Exception as exc:
        parent.report(f'The clipboard reading copy could not be opened. {exc}')


def install_clipboard_reader(frame):
    """Append to the existing Voice menu before its shared command capture."""
    import wx
    bar = frame.GetMenuBar()
    matches = [index for index in range(bar.GetMenuCount())
               if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'voice']
    if len(matches) != 1:
        raise ValueError('The existing Voice menu is unavailable or ambiguous.')
    menu = bar.GetMenu(matches[0])
    item = menu.Append(wx.ID_ANY, 'Read &clipboard...')
    frame.Bind(wx.EVT_MENU, lambda event: show_clipboard_reader(frame), item)
    return item
