"""Stable, context-aware application commands without a wx dependency.

This module describes application-local shortcuts. It never captures keyboard
input, installs a system-wide hotkey, or replaces native text-editing commands.
"""
from dataclasses import dataclass
import re
from types import MappingProxyType


MAX_COMMANDS = 512
MAX_SHORTCUT_LENGTH = 64
_IDENTIFIER = re.compile(r'[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z')
_MODIFIERS = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}
_MODIFIER_ORDER = ('Ctrl', 'Alt', 'Shift')
_KEY_NAMES = {
    'pgup': 'PageUp', 'pageup': 'PageUp',
    'pgdn': 'PageDown', 'pgdown': 'PageDown', 'pagedown': 'PageDown',
    'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down',
    'home': 'Home', 'end': 'End', 'space': 'Space', 'spacebar': 'Space',
    'tab': 'Tab', 'esc': 'Escape', 'escape': 'Escape',
    'enter': 'Enter', 'return': 'Enter', 'del': 'Delete', 'delete': 'Delete',
    'back': 'Backspace', 'backspace': 'Backspace',
    'ins': 'Insert', 'insert': 'Insert', 'capslock': 'CapsLock',
}
_CURSOR_KEYS = {'Left', 'Right', 'Up', 'Down', 'Home', 'End', 'PageUp', 'PageDown'}


class ShortcutError(ValueError):
    """An invalid, conflicting or reserved application shortcut."""


def _identifier(value, field):
    if (not isinstance(value, str) or len(value) > 100
            or _IDENTIFIER.fullmatch(value) is None):
        raise ShortcutError(f'{field} must be a stable lowercase command identifier.')
    return value


def normalize_shortcut(value):
    """Return an unambiguous shortcut spelling, or reject protected keys.

    Empty text means unassigned. Bare function keys are supported; ordinary
    characters and navigation keys require modifiers. Native editing, menu
    mnemonics, focus navigation, OS window controls and common screen-reader
    modifier keys remain outside the configurable application map.
    """
    if not isinstance(value, str) or len(value) > MAX_SHORTCUT_LENGTH:
        raise ShortcutError('A shortcut must be text of at most 64 characters.')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ShortcutError('A shortcut cannot contain control characters.')
    value = value.strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split('+')]
    if not all(parts) or len(parts) > 4:
        raise ShortcutError('Use a shortcut such as Ctrl+Shift+K, or leave it empty.')
    modifiers = set()
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.lower())
        if modifier is None:
            raise ShortcutError('Only Ctrl, Alt and Shift modifiers are supported.')
        if modifier in modifiers:
            raise ShortcutError('A shortcut cannot repeat a modifier.')
        modifiers.add(modifier)
    raw_key = parts[-1]
    if len(raw_key) == 1 and raw_key.isascii() and raw_key.isalnum():
        key = raw_key.upper()
    elif re.fullmatch(r'[fF](?:[1-9]|1[0-9]|2[0-4])', raw_key):
        key = raw_key.upper()
    else:
        key = _KEY_NAMES.get(raw_key.lower())
        if key is None:
            raise ShortcutError('Unsupported key. Use a letter, digit, F1–F24 or a named navigation key.')

    protected = False
    if key in {'Insert', 'CapsLock', 'Tab', 'Escape', 'Enter'}:
        protected = True
    elif not modifiers and not re.fullmatch(r'F(?:[1-9]|1[0-9]|2[0-4])', key):
        protected = True
    elif modifiers == {'Shift'} and len(key) == 1:
        protected = True
    elif key == 'F10' and modifiers <= {'Shift'}:
        protected = True
    elif 'Alt' in modifiers and key in {'F4', 'Space', 'Backspace'}:
        protected = True
    elif modifiers == {'Alt'} and key in {'Up', 'Down'}:
        protected = True
    elif modifiers <= {'Alt', 'Shift'} and 'Alt' in modifiers and len(key) == 1:
        protected = True
    elif modifiers <= {'Ctrl', 'Shift'} and 'Ctrl' in modifiers:
        # Ctrl+PageUp/Down intentionally remain available to the modal reader.
        protected = (key in {'A', 'C', 'X', 'V', 'Z', 'Y', 'Backspace', 'Delete',
                             'Left', 'Right', 'Up', 'Down', 'Home', 'End'})
    elif modifiers == {'Shift'} and key in _CURSOR_KEYS | {'Delete', 'Backspace', 'Space'}:
        protected = True
    elif modifiers >= {'Ctrl', 'Alt'} and key == 'Delete':
        protected = True
    if protected:
        raise ShortcutError('This key combination is reserved for native editing, navigation, '
                            'screen-reader or operating-system controls.')
    return '+'.join([modifier for modifier in _MODIFIER_ORDER if modifier in modifiers] + [key])


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    label: str
    context: str = 'main'
    default_shortcut: str = ''

    def __post_init__(self):
        _identifier(self.command_id, 'Command ID')
        _identifier(self.context, 'Context')
        if (not isinstance(self.label, str) or not self.label.strip()
                or len(self.label) > 500
                or any(ord(character) < 32 or ord(character) == 127 for character in self.label)):
            raise ShortcutError('A command needs a nonempty single-line label.')
        object.__setattr__(self, 'default_shortcut', normalize_shortcut(self.default_shortcut))


class CommandRegistry:
    """Semantic command identities and complete-context conflict validation."""

    def __init__(self, specs=()):
        self._specs = {}
        for spec in specs:
            self.register(spec)

    @property
    def specs(self):
        return MappingProxyType(self._specs)

    def get(self, command_id):
        try:
            return self._specs[command_id]
        except (KeyError, TypeError) as exc:
            raise ShortcutError(f'Unknown command: {command_id!r}.') from exc

    def register(self, spec):
        if not isinstance(spec, CommandSpec):
            raise ShortcutError('Register a CommandSpec.')
        if spec.command_id in self._specs:
            raise ShortcutError(f'Command ID is already registered: {spec.command_id}.')
        if len(self._specs) >= MAX_COMMANDS:
            raise ShortcutError('The command registry is full.')
        for existing in self._specs.values():
            if (spec.default_shortcut and existing.context == spec.context
                    and existing.default_shortcut == spec.default_shortcut):
                raise ShortcutError(self._conflict(spec.default_shortcut, spec, existing))
        self._specs[spec.command_id] = spec
        return spec

    def commands(self, context=None):
        return tuple(spec for spec in self._specs.values()
                     if context is None or spec.context == context)

    @staticmethod
    def _conflict(shortcut, first, second):
        return (f'{shortcut} is assigned to both "{first.label}" and "{second.label}" '
                f'in the {first.context} context. Clear or change one assignment.')

    def validate(self, overrides):
        if not isinstance(overrides, dict) or len(overrides) > MAX_COMMANDS:
            raise ShortcutError('Shortcut bindings must be a bounded command-to-shortcut object.')
        normalized = {}
        for command_id, shortcut in overrides.items():
            self.get(command_id)
            normalized[command_id] = normalize_shortcut(shortcut)
        occupied = {}
        for spec in self._specs.values():
            shortcut = normalized.get(spec.command_id, spec.default_shortcut)
            if shortcut:
                scope_key = spec.context, shortcut
                if scope_key in occupied:
                    raise ShortcutError(self._conflict(shortcut, spec, occupied[scope_key]))
                occupied[scope_key] = spec
        return normalized

    def effective(self, overrides):
        normalized = self.validate(overrides)
        return {spec.command_id: normalized.get(spec.command_id, spec.default_shortcut)
                for spec in self._specs.values()}

    def shortcut(self, command_id, overrides):
        self.get(command_id)
        return self.effective(overrides)[command_id]
