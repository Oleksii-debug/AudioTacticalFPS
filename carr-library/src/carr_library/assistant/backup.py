"""Typed common-backup adapter for inert local assistant checkpoints.

The backup carries the reviewed plan and historical receipts, never permission
to execute it in a restored data directory. Original path bindings are retained.
"""
import re

from .models import canonical
from .validation import validate_state_document


_DOCUMENT = re.compile(r'state/assistant/([0-9a-f]{32})(\.control)?\.json\Z')
_TEMPORARY = re.compile(r'state/assistant/\.[0-9a-f]{32}(?:\.control)?\.json\.[0-9a-f]{32}\.tmp\Z')
_LOCK = re.compile(r'state/assistant/[0-9a-f]{32}\.lock\Z')


def accepts(name):
    return isinstance(name, str) and _DOCUMENT.fullmatch(name) is not None


def limit(name):
    matched = _DOCUMENT.fullmatch(name)
    if matched is None:
        raise ValueError('Unknown assistant backup document.')
    return 1024 if matched[2] else 64 * 1024 * 1024


def ignored(name):
    """Only producer-owned crash temporaries and OS lock files are omitted."""
    return isinstance(name, str) and bool(_TEMPORARY.fullmatch(name) or _LOCK.fullmatch(name))


def validate(name, raw):
    if type(raw) is not bytes or not 0 < len(raw) <= limit(name):
        raise ValueError('Invalid assistant backup document size.')
    return validate_state_document(name.rsplit('/', 1)[-1], raw)


def restored_bytes(name, raw):
    """Apply after the original source bytes and manifest digest were checked."""
    value = validate(name, raw)
    if not _DOCUMENT.fullmatch(name)[2]:
        value['requires_reapproval'] = True
        raw = (canonical(value) + '\n').encode('utf-8')
        validate(name, raw)
    return raw
