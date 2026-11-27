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
