"""Native language/profile choice for reviewed scanned-page import."""
from pathlib import Path

from ..extract.page_ocr import Config

# Compatible quick presets for existing Import Center queues and controls.
# The full installed/managed selector is the authority for custom selections.
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
    from .ocr_selection import choose_model_config
    return choose_model_config(parent)

def choose_installed_languages(parent):
    import wx
    from ..extract.language_manager import installed_languages, language_selection
    models = tuple(model for model in installed_languages() if model.kind in ('language', 'script'))
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
