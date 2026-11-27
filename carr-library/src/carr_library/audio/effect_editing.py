"""Bounded role-effect edits in the existing production script and draft.

Only effect directives and a necessary implicit-role declaration are inserted
or replaced. Spoken words, scene directions and immutable narration sources are
never regenerated from a parsed plan. Presets are inert numeric data, selected
explicitly and applied only to the chosen role.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import shlex

from .jobs import read_json
from .models import Effect, Role
from .production import parse_script


PRESET_SCHEMA = 'carr-role-effect-preset/1'
MAX_PRESET_BYTES = 64 * 1024
MAX_EFFECTS = 16


def _chain(effects):
    if not isinstance(effects, tuple):
        raise ValueError('An effect chain must be a tuple of typed effects.')
    Role('Effect validation', effects=effects)
    return effects


def effect_directive(role, effect):
    """Serialize typed data through the parser's quoted, numeric grammar."""
    Role(role, effects=(effect,))
    parameters = ''.join(f' {name}={value!r}' for name, value in effect.parameters)
    enabled = '' if effect.enabled else ' enabled=false'
    return f'[effect {shlex.quote(role)} {effect.kind}{parameters}{enabled}]'


def _ending(line):
    if line.endswith('\r\n'):
        return '\r\n'
    if line.endswith('\r'):
        return '\r'
    if line.endswith('\n'):
        return '\n'
    return ''


def _rewrite_script(script, plan, chains, *, voice, language):
    changed = {role.name: role for role in plan.roles if role.effects != chains[role.name]}
    if not changed:
        return script
    lines = script.splitlines(keepends=True)
    positions = {name: [] for name in changed}
    declarations = {name: [] for name in changed}
    first_speech = {}
    # One bounded scan, independent of the number of selected roles.
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            parts = shlex.split(line[1:-1], posix=True)
            if len(parts) >= 2 and parts[1] in changed:
                if parts[0] == 'effect':
                    positions[parts[1]].append(index)
                elif parts[0] == 'role':
                    declarations[parts[1]].append(index)
        elif not line.startswith('# ') and ':' in line:
            name = line.split(':', 1)[0]
            if name in changed:
                first_speech.setdefault(name, index)
    default_ending = next((_ending(line) for line in lines if _ending(line)), '\n')
    insertions, omitted = {}, set()
    for name, role in changed.items():
        selected, declared = positions[name], declarations[name]
        if len(selected) != len(role.effects):
            raise ValueError('The effect directives no longer match the current plan.')
        insertion = (selected[0] if selected else declared[-1] + 1
                     if declared else first_speech.get(name, 0))
        ending = (_ending(lines[insertion]) if insertion < len(lines) else '') or default_ending
        available = list(zip(role.effects, (lines[index] for index in selected)))
        added = []
        effects = _chain(chains[name])
        if effects and not declared and name != 'Narrator':
            added.append(f'[role {shlex.quote(name)}]{ending}')
        for effect in effects:
            matching = next((index for index, (old, _) in enumerate(available) if old == effect), None)
            if matching is None:
                added.append(effect_directive(name, effect) + ending)
            else:
                raw = available.pop(matching)[1]
                added.append(raw if _ending(raw) else raw + ending)
        insertions.setdefault(insertion, []).extend(added)
        omitted.update(selected)
    result = []
    for index in range(len(lines) + 1):
        added = insertions.get(index)
        if added:
            if result and not _ending(result[-1]):
                result[-1] += default_ending
            result.extend(added)
        if index < len(lines) and index not in omitted:
            result.append(lines[index])
    updated = ''.join(result)
    after = parse_script(updated, voice=voice, language=language)
    expected = replace(plan, original_script=updated,
                       roles=tuple(replace(role, effects=chains[role.name]) for role in plan.roles))
    if after != expected:
        raise ValueError('The effect edit would change another part of the production plan.')
    return updated


class RoleEffectEditor:
    """An isolated working copy; accepting is one ordinary draft edit."""

    def __init__(self, script, *, voice='sapi5:default', language='en'):
        self.original_script = script
        self.voice, self.language = voice, language
        self.plan = parse_script(script, voice=voice, language=language)
        self.roles = tuple(role.name for role in self.plan.roles)
        self._effects = {role.name: role.effects for role in self.plan.roles}

    def effects(self, role):
        if role not in self._effects:
            raise ValueError('Choose a role from the current production plan.')
        return self._effects[role]

    def set_chain(self, role, effects):
        self.effects(role)
        candidate = _chain(effects)
        self._effects[role] = candidate

    def _index(self, role, index):
        effects = self.effects(role)
        if type(index) is not int or not 0 <= index < len(effects):
            raise ValueError('Choose an effect in the current role chain.')
        return effects

    def add(self, role, effect):
        if not isinstance(effect, Effect):
            raise ValueError('Choose a typed audio effect.')
        self.set_chain(role, (*self.effects(role), effect))

    def edit(self, role, index, effect):
        effects = self._index(role, index)
        if not isinstance(effect, Effect):
            raise ValueError('Choose a typed audio effect.')
        self.set_chain(role, (*effects[:index], effect, *effects[index + 1:]))

    def move(self, role, index, destination):
        effects = self._index(role, index)
        if type(destination) is not int or not 0 <= destination < len(effects):
            raise ValueError('Choose a position within the current effect chain.')
        moved = list(effects)
        moved.insert(destination, moved.pop(index))
        self.set_chain(role, tuple(moved))

    def bypass(self, role, index):
        effects = self._index(role, index)
        self.edit(role, index, replace(effects[index], enabled=not effects[index].enabled))

    def remove(self, role, index):
        effects = self._index(role, index)
        self.set_chain(role, (*effects[:index], *effects[index + 1:]))

    def preset(self, role, name):
        return EffectPreset(name, self.effects(role))

    def apply_preset(self, role, preset):
        if not isinstance(preset, EffectPreset):
            raise ValueError('Choose a validated effect preset.')
        self.set_chain(role, preset.effects)

    def script(self):
        return _rewrite_script(self.original_script, self.plan, self._effects,
                               voice=self.voice, language=self.language)


@dataclass(frozen=True)
class EffectPreset:
    name: str
    effects: tuple[Effect, ...]

    def __post_init__(self):
        if (not isinstance(self.name, str) or not self.name.strip()
                or self.name != self.name.strip() or len(self.name) > 120
                or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in self.name)):
            raise ValueError('Name the preset in one to 120 visible characters.')
        try:
            self.name.encode('utf-8')
        except UnicodeError as exc:
            raise ValueError('The preset name contains invalid Unicode.') from exc
        _chain(self.effects)

    def to_dict(self):
        return {'schema': PRESET_SCHEMA, 'name': self.name,
                'effects': [asdict(effect) for effect in self.effects]}

    def to_text(self):
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                         indent=2, allow_nan=False) + '\n'
        if len(raw.encode('utf-8')) > MAX_PRESET_BYTES:
            raise ValueError('The effect preset exceeds its size budget.')
        return raw

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'name', 'effects'}
                or value['schema'] != PRESET_SCHEMA or type(value['effects']) is not list
                or len(value['effects']) > MAX_EFFECTS):
            raise ValueError('Choose a supported, bounded role-effect preset.')
        effects = []
        for item in value['effects']:
            if type(item) is not dict or set(item) != {'kind', 'parameters', 'enabled'}:
                raise ValueError('A preset contains an invalid effect record.')
            effects.append(Effect.from_dict(item))
        return cls(value['name'], tuple(effects))


def load_preset(path):
    return EffectPreset.from_dict(read_json(Path(path), limit=MAX_PRESET_BYTES))


def save_preset(path, preset, *, private_root, approval, cancelled=lambda: False):
    """Use the same immutable native destination approval as other exports."""
    from ..research.export_file import write_export

    if not isinstance(preset, EffectPreset) or approval is None:
        raise ValueError('Review the preset and its selected export destination before saving.')
    return write_export(Path(path), preset.to_text(), private_root=private_root,
                        approval=approval, cancelled=cancelled)
