"""Native language choice for the application's reviewed scanned-page import."""
from pathlib import Path

from ..extract.page_ocr import Config

LANGUAGES = (
    ('English', 'eng'),
    ('Ukrainian', 'ukr'),
    ('Slovak', 'slk'),
    ('English, Ukrainian and Slovak', 'eng+ukr+slk'),
)


def choose_ocr_config(parent, paths):
    """Return None on cancellation; native text-only imports need no OCR dialog."""
    if not any(Path(path).suffix.casefold() == '.pdf' for path in paths):
        return Config()
    import wx
    dialog = wx.SingleChoiceDialog(parent, 'Choose the languages used on scanned pages.',
                                  'Text recognition languages', [label for label, _ in LANGUAGES])
    try:
        dialog.SetSelection(0)
        if dialog.ShowModal() != wx.ID_OK:
            return None
        selected = dialog.GetSelection()
        if not 0 <= selected < len(LANGUAGES):
            raise ValueError('Choose languages for text recognition.')
        return Config(languages=LANGUAGES[selected][1])
    finally:
        dialog.Destroy()
