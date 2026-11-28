"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import dataclass
import json
import re
import unicodedata


MAX_SELECTED_MODELS = 8


PRESET_FILE = 'ocr-language-presets.json'


FILENAME = PRESET_FILE


MAX_PRESETS = 64


MAX_PRESET_BYTES = 64 * 1024


_MODEL = re.compile(r'(?:script/)?[A-Za-z][A-Za-z0-9_]{0,63}\Z')


_AUXILIARY = {'osd', 'equ'}


class OcrLanguageError(ValueError):
    """An unavailable or unsafe model choice cannot start recognition."""


def valid_model_code(value):
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        return False
    # These names address devices rather than traineddata files on Windows.
    name = value.rsplit('/', 1)[-1].casefold()
    return name not in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                        *(f'lpt{i}' for i in range(1, 10))}


def split_languages(value):
    """Accept only bounded CLI data, including official script/Name selectors."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_SELECTED_MODELS * 72:
        raise OcrLanguageError('Choose at least one text recognition language or script.')
    codes = tuple(value.split('+'))
    if len(codes) > MAX_SELECTED_MODELS:
        raise OcrLanguageError(f'Choose at most {MAX_SELECTED_MODELS} models for one OCR pass.')
    if (any(not valid_model_code(code) for code in codes)
            or len({code.casefold() for code in codes}) != len(codes)):
        raise OcrLanguageError('Invalid or repeated text recognition model.')
    if any(code.casefold() in _AUXILIARY for code in codes):
        raise OcrLanguageError('OSD and equation detection are auxiliary models, not recognition languages.')
    return codes


def _name(value):
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 80
            or any(unicodedata.category(char) in ('Cc', 'Cf', 'Cs') for char in value)):
        raise OcrLanguageError('Use a preset name of 1 to 80 printable characters.')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OcrLanguageError('The saved OCR presets contain a repeated field.')
        result[key] = value
    return result


def validate_presets_bytes(raw):
    """Validate portable preference data without running OCR or opening sources."""
    from .pinned_tessdata import MODELS, POLICY
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PRESET_BYTES:
        raise OcrLanguageError('The saved OCR preset file is too large or empty.')
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OcrLanguageError('The saved OCR presets are invalid.') from exc
    if (type(data) is not dict or set(data) != {'version', 'presets'}
            or type(data['version']) is not int or data['version'] != 1
            or type(data['presets']) is not list or len(data['presets']) > MAX_PRESETS):
        raise OcrLanguageError('The saved OCR preset format is unsupported.')
    names = set()
    for item in data['presets']:
        required = {'name', 'languages', 'model_policy', 'orientation'}
        if (type(item) is not dict or not required <= set(item)
                or set(item) - required - {'model_profile'}):
            raise OcrLanguageError('The saved OCR preset fields are invalid.')
        name = _name(item['name']).casefold()
        if name in names:
            raise OcrLanguageError('The saved OCR presets contain repeated names.')
        names.add(name)
        codes = split_languages(item['languages'])
        profile = item.get('model_profile', 'installed')
        if (type(item['orientation']) is not bool or item['model_policy'] not in ('default', POLICY)
                or profile not in ('installed', 'fast', 'best')
                or (item['model_policy'] == POLICY and
                    (profile == 'best' or any(code not in MODELS for code in codes)))):
            raise OcrLanguageError('The saved OCR preset settings are invalid.')
    return data
