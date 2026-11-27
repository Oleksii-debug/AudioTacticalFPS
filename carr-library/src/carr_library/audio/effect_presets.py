"""Typed effect chains and durable user presets for the existing FFmpeg adapter."""
from dataclasses import dataclass
from hashlib import sha256
import os
import re
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from .dsp import effect_controls, effect_filter, narration_preset
from .models import Effect, integer

_MAX_PRESETS = 256
_MAX_PRESET_BYTES = 64 * 1024
_MAX_INVENTORY_ENTRIES = 1024
_ID = re.compile(r'[0-9a-f]{32}')


def checked_chain(effects, sample_rate=24000, *, available=None):
    integer(sample_rate, 'Preset sample rate', 8000, 192000)
    if not isinstance(effects, (tuple, list)) or len(effects) > 32:
        raise ValueError('An effects chain supports at most 32 typed effects.')
    result = []
    for effect in effects:
        effect_filter(effect, sample_rate)  # Includes all bypassed parameter checks.
        if available is not None and effect.enabled and effect.kind not in available:
            raise ValueError(f'The installed audio runtime does not support enabled {effect.kind}.')
        result.append(Effect(effect.kind, dict(effect.parameters), enabled=effect.enabled))
    return tuple(result)


def read_effect(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'parameters', 'enabled'}:
        raise ValueError('A saved effect must contain its kind, numeric controls and enabled state.')
    return Effect(value['kind'], value['parameters'], enabled=value['enabled'])


def default_effect(kind, sample_rate=24000):
    integer(sample_rate, 'Effect sample rate', 8000, 192000)
    definitions = effect_controls()
    if kind not in definitions:
        raise ValueError('Choose a supported effect kind.')
    parameters = {name: value['default'] for name, value in definitions[kind].items()}
    frequency = definitions[kind].get('frequency')
    if frequency and frequency['unit'] == 'Hz':
        parameters['frequency'] = min(parameters['frequency'], sample_rate * .45)
    return Effect(kind, parameters)


def builtin_presets():
    return (('Dry audio', ()),
            ('Narration cleanup', narration_preset()),
            ('Podcast loudness', (Effect('highpass', frequency=70),
                Effect('compressor', threshold_db=-20, ratio=2.5, makeup_db=0),
                Effect('loudness', target_lufs=-18, peak_db=-1, range_lu=7))))


@dataclass(frozen=True)
class EffectPreset:
    id: str
    name: str
    sample_rate: int
    effects: tuple
    revision: int = 1

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Invalid effect preset identity.')
        if (not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 120
                or any(ord(character) < 32 for character in self.name)):
            raise ValueError('Use a preset name of 1 to 120 characters without control characters.')
        self.name.encode('utf-8', 'strict')
        integer(self.revision, 'Preset revision', 1, 2**31 - 1)
        object.__setattr__(self, 'effects', checked_chain(self.effects, self.sample_rate))

    def to_dict(self):
        return {'schema': 1, 'id': self.id, 'name': self.name, 'sample_rate': self.sample_rate,
                'effects': [effect.to_dict() for effect in self.effects], 'revision': self.revision}

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict)
                or set(value) != {'schema', 'id', 'name', 'sample_rate', 'effects', 'revision'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['effects'], list) or len(value['effects']) > 32):
            raise ValueError('Unsupported effect preset shape or schema.')
        return cls(value['id'], value['name'], value['sample_rate'],
                   tuple(read_effect(row) for row in value['effects']), value['revision'])


class EffectPresetStore:
    """Named settings only; no filter graphs, executable paths or model payloads."""
    def __init__(self, library_root):
        self.library_root = local_path(library_root, exists=True)
        self.warning = ''
        self.root = local_path(self.library_root / 'state' / 'studio' / 'effect-presets',
                               root=self.library_root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.root, exists=True)

    def _path(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose an existing effect preset identity.')
        return local_path(self.root / (identity + '.json'), root=self.root)

    def get(self, identity):
        path = self._path(identity)
        before = regular(path)
        if before.st_size > _MAX_PRESET_BYTES:
            raise ValueError('The effect preset exceeds its 64 KiB record budget.')
        payload = read_json(path, maximum=_MAX_PRESET_BYTES)
        after = regular(path)
        def stamp(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if stamp(before) != stamp(after):
            raise ValueError('The effect preset changed while reading. Reload its current value.')
        value = EffectPreset.from_dict(payload)
        if value.id != identity:
            raise ValueError('The saved effect preset identity differs from its filename.')
        return value

    def list(self):
        values = []
        with os.scandir(local_path(self.root, exists=True)) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > _MAX_INVENTORY_ENTRIES:
                    raise ValueError('The effect preset directory exceeds its bounded inventory allowance.')
                if not entry.name.endswith('.json'):
                    continue
                identity = entry.name[:-5]
                if not _ID.fullmatch(identity):
                    raise ValueError('An effect preset has an unexpected filename.')
                if len(values) >= _MAX_PRESETS:
                    raise ValueError('The effect preset inventory exceeds 256 records.')
                values.append(self.get(identity))
        return tuple(sorted(values, key=lambda item: (item.name.casefold(), item.id)))

    def save(self, name, effects, *, sample_rate=24000, previous=None):
        self.warning = ''
        if previous is not None and not isinstance(previous, EffectPreset):
            raise ValueError('Review the exact saved preset before updating it.')
        # Reject an arbitrary/unbounded iterator before consuming its elements.
        selected = checked_chain(effects, sample_rate)
        value = EffectPreset(previous.id if previous else uuid4().hex, name, sample_rate,
                             selected, previous.revision + 1 if previous else 1)
        publication_attempted = False
        try:
            with state_lock(self.root, 'effect-presets'):
                path = self._path(value.id)
                if previous is not None:
                    current = self.get(previous.id)
                    if current.fingerprint != previous.fingerprint:
                        raise ValueError('This preset changed after review. Reload it before saving.')
                elif os.path.lexists(path):
                    raise FileExistsError('The new preset identity already exists.')
                elif len(self.list()) >= _MAX_PRESETS:
                    raise ValueError('The preset collection has reached its 256-record allowance.')
                if len(encode(value.to_dict())) > _MAX_PRESET_BYTES:
                    raise ValueError('The effect preset exceeds its 64 KiB record budget.')
                publication_attempted = True
                atomic_json(path, value.to_dict(), replace=previous is not None)
        except (OSError, RuntimeError, KeyboardInterrupt) as exc:
            if not publication_attempted:
                raise
            # A directory-sync or lock-release interruption can follow the
            # publication. Read back the exact bounded value without claiming
            # that this call is known to have committed it or released its lock.
            try:
                observed = self.get(value.id)
            except (ValueError, OSError, RuntimeError):
                raise exc
            if observed.fingerprint != value.fingerprint:
                raise
            self.warning = ('The preset is present and checked; save completion could not be confirmed. '
                            'Keep a backup and retry if needed. ' + str(exc)[:1000])
        return value


class EffectChainSession:
    """One editable ordered chain with bounded reversible in-memory changes."""
    def __init__(self, effects=(), *, sample_rate=24000, available=None):
        self.sample_rate = sample_rate
        self.available = frozenset(effect_controls() if available is None else available)
        self.effects = checked_chain(effects, sample_rate, available=self.available)
        self._undo, self._redo = [], []

    def _index(self, index):
        if type(index) is not int or not 0 <= index < len(self.effects):
            raise ValueError('Select an existing effect in the ordered chain.')
        return index

    def set(self, effects):
        candidate = checked_chain(effects, self.sample_rate, available=self.available)
        if candidate == self.effects:
            return False
        self._undo.append(self.effects)
        del self._undo[:-32]
        self._redo.clear()
        self.effects = candidate
        return True

    def add(self, kind):
        if kind not in self.available:
            raise ValueError('Choose an effect supported by the installed runtime.')
        self.set((*self.effects, default_effect(kind, self.sample_rate)))
        return len(self.effects) - 1

    def edit(self, index, effect):
        index = self._index(index)
        changed = list(self.effects)
        changed[index] = effect
        return self.set(changed)

    def toggle(self, index):
        current = self.effects[self._index(index)]
        return self.edit(index, Effect(current.kind, dict(current.parameters), enabled=not current.enabled))

    def remove(self, index):
        index = self._index(index)
        return self.set(self.effects[:index] + self.effects[index + 1:])

    def move(self, index, direction):
        index = self._index(index)
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Move one position up or down.')
        target = index + direction
        self._index(target)
        changed = list(self.effects)
        changed[index], changed[target] = changed[target], changed[index]
        self.set(changed)
        return target

    def history(self, *, forward=False):
        if type(forward) is not bool:
            raise ValueError('Choose undo or redo explicitly.')
        stack, opposite = (self._redo, self._undo) if forward else (self._undo, self._redo)
        if not stack:
            raise ValueError('No effect-chain change is available to redo.' if forward else
                             'No effect-chain change is available to undo.')
        candidate = checked_chain(stack[-1], self.sample_rate, available=self.available)
        stack.pop()
        opposite.append(self.effects)
        self.effects = candidate
        return self.effects
