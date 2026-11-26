"""Deterministic speaker-labelled scripts; prose is never executable code."""
from hashlib import sha256
import re

from .models import Line, PlanError, Project, Role, Scene


def dialogue_project(script, voices, *, title='Dialogue', pause_after=.3):
    """Parse ``Speaker: text`` and ``## Chapter`` with explicit voice assignments.

    This is a guided local planner, not a claim of general natural-language
    understanding. Unlabelled lines continue the previous speaker's paragraph.
    """
    if not isinstance(script, str) or not script.strip() or not voices:
        raise PlanError('Enter a script and assign at least one named voice.')
    if any(not isinstance(name, str) or not name.strip() or ':' in name or not isinstance(role, Role)
           for name, role in voices.items()):
        raise PlanError('Speaker names and voice assignments are invalid.')
    folded = {name.casefold(): role for name, role in voices.items()}
    if len(folded) != len(voices):
        raise PlanError('Speaker names must be distinct, ignoring case.')
    scenes, lines = [], []
    scene_title, scene_number = title, 1
    current_role, current_text = None, []

    def flush_line():
        nonlocal current_role, current_text
        if current_role is not None and ''.join(current_text).strip():
            lines.append(Line(f'line-{len(lines) + 1}', current_role.id, '\n'.join(current_text), pause_after))
        current_role, current_text = None, []

    def flush_scene():
        nonlocal lines, scene_number
        flush_line()
        if lines:
            scenes.append(Scene(f'scene-{scene_number}', scene_title, tuple(lines)))
            scene_number += 1
            lines = []

    for number, raw in enumerate(script.splitlines(), 1):
        if raw.startswith('## '):
            flush_scene()
            scene_title = raw[3:].strip()
            if not scene_title:
                raise PlanError(f'Scene title on line {number} is empty.')
            continue
        match = re.match(r'^([^:\n]{1,80}):\s?(.*)$', raw)
        if match:
            speaker = folded.get(match[1].strip().casefold())
            if speaker is None:
                raise PlanError(f'Assign a voice for speaker {match[1].strip()} on line {number}.')
            flush_line()
            current_role, current_text = speaker, [match[2]]
        elif current_role is not None:
            current_text.append(raw)
        elif raw.strip():
            raise PlanError(f'Line {number} needs a speaker label. Use Speaker name: text.')
    flush_scene()
    return Project(title, tuple(voices.values()), tuple(scenes),
        source_reference={'kind': 'user-authored-dialogue', 'script_sha256': sha256(script.encode('utf-8')).hexdigest()},
        authored_script=script)
