"""Strict Tesseract model identifiers, including official script subdirectory IDs.

Keep spelling and selection order intact: model names are case-sensitive on
some platforms. This accepts identifiers, never arbitrary filesystem paths.
"""
import re


_MODEL_ID = re.compile(r'(?:script/)?[A-Za-z0-9_]+')
AUXILIARY_MODELS = frozenset(('osd', 'equ'))


def is_model_id(value):
    """Accept a root model or exactly ``script/<model>`` without path traversal."""
    return isinstance(value, str) and _MODEL_ID.fullmatch(value) is not None


def model_ids(languages):
    """Validate a plus-separated selection without rewriting model identity."""
    if not isinstance(languages, str):
        raise ValueError('invalid language selection')
    selected = tuple(languages.split('+'))
    if not all(is_model_id(name) for name in selected):
        raise ValueError('invalid language selection')
    return selected


def reading_model_ids(languages):
    """Reading languages/scripts exclude separate orientation/equation detectors."""
    selected = model_ids(languages)
    if any(name.casefold() in AUXILIARY_MODELS for name in selected):
        raise ValueError('Choose reading languages or scripts; auxiliary detectors are separate options.')
    return selected


MAX_PRESET_BYTES = 128 * 1024
MAX_PRESETS = 128


def validate_presets_bytes(raw):
    """Validate the existing language-manager producer without model discovery."""
    import json
    from dataclasses import asdict
    from ..durable_state import _unique
    from .page_ocr import Config, config_from_dict

    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PRESET_BYTES:
        raise ValueError('OCR presets exceed their storage budget.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid preset number.')))
    if (type(value) is not dict or set(value) != {'schema', 'presets'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['presets']) is not dict or len(value['presets']) > MAX_PRESETS):
        raise ValueError('Invalid OCR preset catalogue.')
    required = set(asdict(Config())) - {'automatic_languages'}
    for name, settings in value['presets'].items():
        if (type(name) is not str or not name.strip() or len(name) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or type(settings) is not dict
                or set(settings) not in (required, required | {'automatic_languages'})
                or any(type(settings[key]) is not int for key in ('dpi', 'timeout', 'max_pixels'))):
            raise ValueError('Invalid named OCR preset.')
        config_from_dict(settings)
    return value
