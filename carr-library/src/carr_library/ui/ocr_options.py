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
