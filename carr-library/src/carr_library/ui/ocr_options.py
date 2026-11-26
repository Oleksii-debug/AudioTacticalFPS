"""Native language/profile choice for reviewed scanned-page import."""
from pathlib import Path

from ..extract.page_ocr import Config
from ..extract.ocr_languages import model_label, split_languages
from ..extract.pinned_tessdata import POLICY

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


def config_summary(config):
    profile = 'pinned fast models' if config.model_policy == POLICY else 'installed model profile'
    names = ', '.join(model_label(code) for code in split_languages(config.languages))
    return names + '; ' + profile + ('; orientation check on' if config.orientation else '')


def choose_model_config(parent, initial=None, *, preset_root=None):
    import wx
    from .ocr_language_dialog import create_ocr_dialog
    dialog = create_ocr_dialog(parent, initial, preset_root=preset_root)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        return dialog.result
    finally:
        dialog.Destroy()
