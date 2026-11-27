"""Typed form edits over the existing structured production script.

There is no second project representation on disk. Only selected role/output
directives change; the existing parser checks the complete resulting plan.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import shlex

from .effect_editing import RoleEffectEditor
from .models import Effect, Role, FORMATS, number
from .production import parse_script


ROLE_FIELDS = ('voice', 'language', 'pan', 'rate', 'pitch', 'volume')
FORM_FIELDS = (*ROLE_FIELDS, 'gain_db', 'gain_enabled')


@dataclass(frozen=True)
class VoiceOption:
    id: str
    name: str
    languages: tuple[str, ...]
    controls: tuple[str, ...]
    available: bool
    reason: str = ''
    advertised: bool = True


def _single_line(value, label, limit=2048):
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
        raise ValueError(f'Choose a valid {label}.')
    value.encode('utf-8')
    return value


def voice_options(rows):
    """Retain exact advertised IDs/languages; never invent a default or route."""
    if not isinstance(rows, (list, tuple)) or len(rows) > 4096:
        raise ValueError('The installed voice list is invalid or exceeds its bound.')
    result, identities = [], set()
    for row in rows:
        if type(row) is not dict:
            raise ValueError('The installed voice list contains an invalid record.')
        identifier = _single_line(row.get('id'), 'installed voice identifier')
        name = _single_line(row.get('name'), 'installed voice name')
        if identifier in identities or type(row.get('available', True)) is not bool:
            raise ValueError('The installed voice list has duplicate or invalid records.')
        languages, controls = row.get('languages', ()), row.get('controls', ())
        if (not isinstance(languages, (list, tuple)) or len(languages) > 4096
                or not isinstance(controls, (list, tuple)) or len(controls) > 32):
            raise ValueError('The installed voice capabilities are invalid.')
        languages = tuple(_single_line(item, 'advertised language', 80) for item in languages)
        controls = tuple(_single_line(item, 'advertised control', 80) for item in controls)
        if len(set(languages)) != len(languages) or len(set(controls)) != len(controls):
            raise ValueError('The installed voice capabilities are ambiguous.')
        reason = row.get('reason') or ''
        if type(reason) is not str or len(reason) > 4096:
            raise ValueError('The installed voice availability reason is invalid.')
        result.append(VoiceOption(identifier, name, languages, controls, row.get('available', True), reason))
        identities.add(identifier)
    return tuple(result)


def saved_voice_options(options, saved):
    """Keep an unavailable saved ID selected and visible, without substituting."""
    _single_line(saved, 'saved voice identifier')
    if any(option.id == saved for option in options):
        return tuple(options)
    return (VoiceOption(saved, 'Retained voice ID — not in the current list', (), (), False,
                        'An unlisted ID may be a retained engine alias. Choose an advertised voice for role edits; '
                        'no substitute was selected.', advertised=False),
            *options)


def validate_role_voice(role, options):
    selected = next((item for item in options if item.id == role.voice), None)
    if selected is None or not selected.available:
        raise ValueError('The selected voice is unavailable. Choose an installed voice explicitly.')
    if role.language not in selected.languages:
        raise ValueError('Choose an exact language advertised by the selected voice; no fallback is selected.')
    for name, default in (('rate', 0), ('pitch', 0), ('volume', 1)):
        if name not in selected.controls and getattr(role, name) != default:
            raise ValueError(f'The selected voice does not support {name}; explicitly restore its default.')
    return selected


def gain_settings(role):
    """The form owns only a final gain effect, preserving every earlier step."""
    if role.effects and role.effects[-1].kind == 'gain':
        effect = role.effects[-1]
        return dict(effect.parameters).get('gain_db', 0), effect.enabled, True
    return 0, False, False


def role_values(role):
    gain, enabled, _ = gain_settings(role)
    return {'voice': role.voice, 'language': role.language, 'pan': str(role.pan),
            'rate': str(role.rate), 'pitch': str(role.pitch),
            'volume': str(Decimal(str(role.volume)) * 100),
            'gain_db': str(gain), 'gain_enabled': enabled}


def role_fields(previous, fields, options):
    """Native text uses existing role units and explicit percentage conversion."""
    if not isinstance(previous, Role) or type(fields) is not dict or set(fields) != set(FORM_FIELDS):
        raise ValueError('Enter each displayed role setting.')
    if type(fields['gain_enabled']) is not bool:
        raise ValueError('Choose whether the final role gain is enabled.')
    for name in FORM_FIELDS[:-1]:
        if type(fields[name]) is not str:
            raise ValueError('Enter role settings as the displayed text.')
    _single_line(fields['voice'], 'voice identifier')
    _single_line(fields['language'], 'language', 80)
    try:
        pan, gain = float(fields['pan']), float(fields['gain_db'])
        rate, pitch = int(fields['rate']), int(fields['pitch'])
        volume = float(Decimal(fields['volume']) / 100)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise ValueError('Enter finite numbers in the displayed role ranges.') from exc
    number(gain, -120, 24, 'Final role gain in dB')
    effects = previous.effects
    old_gain, old_enabled, exists = gain_settings(previous)
    if (gain, fields['gain_enabled']) != (old_gain, old_enabled):
        final = Effect('gain', (('gain_db', gain),), enabled=fields['gain_enabled'])
        effects = (*effects[:-1], final) if exists else (*effects, final)
    candidate = replace(previous, voice=fields['voice'], language=fields['language'],
                        pan=pan, rate=rate, pitch=pitch, volume=volume, effects=effects)
    # An unavailable saved role remains inspectable and can accompany an
    # output-only edit. Editing that role requires an explicit usable choice.
    if candidate != previous:
        validate_role_voice(candidate, options)
    return candidate


def _ending(line):
    for value in ('\r\n', '\n', '\r'):
        if line.endswith(value):
            return value
    return ''


def _role_directive(role):
    values = ' '.join(name + '=' + shlex.quote(str(getattr(role, name))) for name in ROLE_FIELDS)
    return '[role ' + shlex.quote(role.name) + ' ' + values + ']'


class ScriptProductionEditor:
    """One isolated working copy, accepted as one normal NarrationDraft edit."""

    def __init__(self, script, *, voice='sapi5:default', language='en'):
        self.original_script, self.voice, self.language = script, voice, language
        self.plan = parse_script(script, voice=voice, language=language)
        self.roles = tuple(role.name for role in self.plan.roles)
        self._roles = {role.name: role for role in self.plan.roles}
        self.output_format, self.export_stems = self.plan.output_format, self.plan.export_stems

    def role(self, name):
        if name not in self._roles:
            raise ValueError('Choose an existing role from the current production plan.')
        return self._roles[name]

    def set_role(self, name, role):
        self.role(name)
        if not isinstance(role, Role) or role.name != name:
            raise ValueError('Role editing must preserve the selected role identity.')
        for field in ('voice', 'language'):
            _single_line(getattr(role, field), field, 2048 if field == 'voice' else 80)
        self._roles[name] = role

    def set_output(self, output_format, export_stems):
        # The current ProductionPlan is the sole output contract.
        replace(self.plan, output_format=output_format, export_stems=export_stems)
        self.output_format, self.export_stems = output_format, export_stems

    def changed_roles(self):
        return tuple(role for role in self.plan.roles if self.role(role.name) != role)

    def script(self):
        if (not self.changed_roles()
                and (self.output_format, self.export_stems) == (self.plan.output_format, self.plan.export_stems)):
            return self.original_script
        chains = RoleEffectEditor(self.original_script, voice=self.voice, language=self.language)
        for name in self.roles:
            chains.set_chain(name, self.role(name).effects)
        intermediate = chains.script()
        lines = intermediate.splitlines(keepends=True)
        changed = {role.name: self.role(role.name) for role in self.plan.roles
                   if any(getattr(role, field) != getattr(self.role(role.name), field) for field in ROLE_FIELDS)}
        declarations, first_speech, output = {name: [] for name in changed}, {}, []
        for index, raw in enumerate(lines):
            line = raw.strip()
            if line.startswith('[') and line.endswith(']'):
                parts = shlex.split(line[1:-1], posix=True)
                if parts[0] == 'role' and parts[1] in changed:
                    declarations[parts[1]].append(index)
                if parts[0] == 'output':
                    output.append((index, parts))
            elif line and not line.startswith('# '):
                name = line.split(':', 1)[0] if ':' in line else 'Narrator'
                if name not in self.roles:
                    name = 'Narrator'
                first_speech.setdefault(name, index)
        ending = next((_ending(line) for line in lines if _ending(line)), '\n')
        replacements, insertions = {}, {}
        for name, role in changed.items():
            places = declarations[name]
            if places:
                index = places[-1]
                replacements[index] = _role_directive(role) + _ending(lines[index])
            else:
                index = 0 if name == 'Narrator' else first_speech[name]
                insertions.setdefault(index, []).append(_role_directive(role) + ending)
        if (self.output_format, self.export_stems) != (self.plan.output_format, self.plan.export_stems):
            index, parts = output[-1] if output else (len(lines), ['output'])
            values = dict(part.split('=', 1) for part in parts[1:])
            values.update(format=self.output_format, stems='true' if self.export_stems else 'false')
            directive = '[output ' + ' '.join(key + '=' + shlex.quote(value) for key, value in values.items()) + ']'
            if output:
                replacements[index] = directive + _ending(lines[index])
            else:
                insertions.setdefault(index, []).append(directive + ending)
        result = []
        for index in range(len(lines) + 1):
            additions = insertions.get(index, ())
            if additions:
                if result and not _ending(result[-1]):
                    result[-1] += ending
                result.extend(additions)
            if index < len(lines):
                result.append(replacements.get(index, lines[index]))
        updated = ''.join(result)
        expected = replace(self.plan, original_script=updated,
                           roles=tuple(self.role(name) for name in self.roles),
                           output_format=self.output_format, export_stems=self.export_stems)
        if parse_script(updated, voice=self.voice, language=self.language) != expected:
            raise ValueError('These settings would change another part of the production plan.')
        return updated
