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
                                  'Text recognition languages', [label for label, _ in LANGUAGES] +
                                  ['Choose from all installed languages and scripts...'])
    try:
        dialog.SetSelection(0)
        if dialog.ShowModal() != wx.ID_OK:
            return None
        selected = dialog.GetSelection()
        if selected == len(LANGUAGES):
            return choose_installed_languages(parent)
        if not 0 <= selected < len(LANGUAGES):
            raise ValueError('Choose languages for text recognition.')
        return Config(languages=LANGUAGES[selected][1])
    finally:
        dialog.Destroy()


def choose_installed_languages(parent):
    import wx
    from ..extract.language_manager import installed_languages, language_selection
    models = tuple(model for model in installed_languages() if model.kind != 'orientation')
    dialog = wx.MultiChoiceDialog(parent,
        'Select one or several installed reading models. Use a small set matching the source; '
        'script detection does not identify the language. No models are downloaded.',
        'Installed OCR languages and scripts', [f'{model.name} ({model.code})' for model in models])
    try:
        english = next((i for i, model in enumerate(models) if model.code == 'eng'), None)
        if english is not None:
            dialog.SetSelections([english])
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return Config(languages=language_selection([models[i].code for i in dialog.GetSelections()], models))
    finally:
        dialog.Destroy()
