"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
import json
import re


FILENAME = 'keybindings.json'


MAX_BYTES = 128 * 1024


MAX_COMMANDS = 500


_KEYS = frozenset(('Space', 'Enter', 'Home', 'End', 'PageUp', 'PageDown',
                  'Left', 'Right', 'Up', 'Down', 'Insert', 'Delete', 'Backspace'))


_FORBIDDEN = frozenset(('Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Y', 'Ctrl+Z',
                       'Alt+Tab', 'Alt+F4', 'Ctrl+Alt+Delete', 'Ctrl+Tab', 'Ctrl+Shift+Tab'))


def normalize_shortcut(value):
    """Return wx menu accelerator spelling; empty text removes a shortcut."""
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError('Enter a keyboard shortcut of at most 48 characters.')
    value = value.strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split('+')]
    if len(parts) > 4 or any(not part for part in parts):
        raise ValueError('Use a shortcut such as Ctrl+Shift+S, Alt+F8, or F6.')
    modifiers = []
    for part in parts[:-1]:
        modifier = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}.get(part.lower())
        if modifier is None or modifier in modifiers:
            raise ValueError('Use Ctrl, Alt and Shift at most once each.')
        modifiers.append(modifier)
    key = parts[-1]
    names = {name.lower(): name for name in _KEYS}
    if re.fullmatch('[a-zA-Z0-9]', key):
        key = key.upper()
    elif re.fullmatch(r'(?i)f(?:[1-9]|1[0-9]|2[0-4])', key):
        key = key.upper()
    elif key.lower() in names:
        key = names[key.lower()]
    else:
        raise ValueError('Use a letter, digit, F1–F24, or a named navigation key.')
    if not re.fullmatch(r'F\d+', key) and not ({'Ctrl', 'Alt'} & set(modifiers)):
        raise ValueError('Letters and navigation keys need Ctrl or Alt so ordinary typing remains available.')
    result = '+'.join([*(m for m in ('Ctrl', 'Alt', 'Shift') if m in modifiers), key])
    if result in _FORBIDDEN or (modifiers == ['Alt'] and len(key) == 1):
        raise ValueError('This key is reserved for editing, system controls, or Alt-menu navigation.')
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate keyboard preference field.')
        result[key] = value
    return result


def decode_preferences(raw):
    """Strict structural validation, also used by checked library backups."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Keyboard preferences are empty or too large.')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid keyboard preferences JSON.') from exc
    if (type(value) is not dict or set(value) != {'version', 'kind', 'overrides'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'carr-keybindings' or type(value['overrides']) is not dict
            or len(value['overrides']) > MAX_COMMANDS):
        raise ValueError('Unsupported keyboard preferences format.')
    result = {}
    for identity, shortcut in value['overrides'].items():
        if (not isinstance(identity, str) or not 0 < len(identity) <= 500
                or any(ord(ch) < 32 for ch in identity)):
            raise ValueError('Invalid keyboard command identity.')
        result[identity] = normalize_shortcut(shortcut)
    return result
