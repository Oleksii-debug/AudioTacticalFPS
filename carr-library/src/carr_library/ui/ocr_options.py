"""Native selection of installed OCR models; no downloads or source approval."""
from dataclasses import replace
from pathlib import Path

from ..extract.ocr_runtime import discover_ocr_languages
from ..extract.page_ocr import Config


# Friendly labels are optional presentation; every safe installed ID is offered.
LANGUAGE_NAMES = {
    'eng': 'English', 'ukr': 'Ukrainian', 'slk': 'Slovak', 'ces': 'Czech',
    'pol': 'Polish', 'deu': 'German', 'fra': 'French', 'spa': 'Spanish',
    'ita': 'Italian', 'por': 'Portuguese', 'hun': 'Hungarian', 'ron': 'Romanian',
    'jpn': 'Japanese', 'kor': 'Korean', 'chi_sim': 'Simplified Chinese',
    'chi_tra': 'Traditional Chinese', 'ara': 'Arabic', 'heb': 'Hebrew',
}


def language_label(code):
    if code.startswith('script/'):
        return f'{code.split("/", 1)[1]} script ({code})'
    name = LANGUAGE_NAMES.get(code)
    return f'{name} ({code})' if name else code


def available_language_choices():
    languages = discover_ocr_languages()
    if not languages:
        # Native PDF text still works without an OCR engine/model. The existing
        # extractor marks scanned pages for review instead of inventing text.
        return (('No local OCR languages found; scanned text unavailable', 'eng'),)
    return tuple((language_label(code), code) for code in languages)


def choose_ocr_config(parent, paths, *, initial=None, force=False):
    """Choose one or more installed text models; return None on cancellation.

    Native text-only imports need no dialog. OSD is never presented as a text
    language. Discovery errors remain visible and never select another runtime.
    """
    initial = initial or Config()
    if not force and not any(Path(path).suffix.casefold() == '.pdf' for path in paths):
        return initial
    choices = available_language_choices()
    previous = set(initial.languages.split('+'))
    selected = [index for index, (_, code) in enumerate(choices) if code in previous]
    if not selected:
        selected = [0]
    import wx
    dialog = wx.MultiChoiceDialog(parent,
        'Select the installed languages or scripts used on scanned pages. '
        'Space checks or unchecks a focused item. Choose up to 32 models; '
        'orientation data is handled separately. No models are downloaded.',
        'Text recognition languages', [label for label, _ in choices])
    try:
        dialog.SetSelections(selected)
        if dialog.ShowModal() != wx.ID_OK:
            return None
        selected = tuple(dialog.GetSelections())
        if (not selected or len(set(selected)) != len(selected)
                or any(type(index) is not int or not 0 <= index < len(choices) for index in selected)):
            raise ValueError('Choose at least one installed text recognition language.')
        return replace(initial, languages='+'.join(choices[index][1] for index in selected))
    finally:
        dialog.Destroy()
