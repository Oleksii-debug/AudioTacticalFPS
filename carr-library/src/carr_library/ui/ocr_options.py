"""Installed-model choices for the application's reviewed scanned-page import."""
from pathlib import Path

from ..extract.page_ocr import Config
from ..extract.ocr_languages import model_label, split_languages
from ..extract.pinned_tessdata import POLICY

# Compatibility quick choices; the full installed/managed selector supplies
# the actual model inventory and preserves custom ordered selections.
LANGUAGES = (('English', 'eng'), ('Ukrainian', 'ukr'), ('Slovak', 'slk'),
             ('English, Ukrainian and Slovak', 'eng+ukr+slk'))


def config_summary(config):
    profile = {'installed': 'Installed model profile', 'fast': 'Managed Fast models',
               'best': 'Managed Best models'}[config.model_profile]
    if config.model_policy == POLICY:
        profile += '; pinned fast policy'
    names = ', '.join(model_label(code) for code in split_languages(config.languages))
    return names + '; ' + profile + ('; orientation check on' if config.orientation else '')


def choose_model_config(parent, initial=None, *, preset_root=None):
    from .ocr_selection import choose_model_config as choose
    options = {}
    if initial is not None:
        options['initial'] = initial
    if preset_root is not None:
        options['preset_root'] = preset_root
    return choose(parent, **options)


def choose_ocr_config(parent, paths):
    """Preserve direct-import API; native text-only imports need no model dialog."""
    if not any(Path(path).suffix.casefold() == '.pdf' for path in paths):
        return Config()
    library = getattr(parent, 'library', None)
    return choose_model_config(parent, preset_root=getattr(library, 'root', None))
