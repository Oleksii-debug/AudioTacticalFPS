"""Connect immutable cue originals to explicitly accepted reading derivatives.

Projects keep original script/cue text. Only the speech consumer asks for a
spoken reading copy. Mapping source offsets are global original-script offsets;
derived offsets are relative to the selected cue's spoken text.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json

from .pronunciation import NarrationDerivative


def _speech(project):
    return {cue.id: cue for scene in project.scenes for cue in scene.cues if cue.kind == 'speech'}


def _source_id(project_id, cue_id):
    return f'{project_id}:{cue_id}'


def _bound(project, cue, snapshot):
    derivative = NarrationDerivative.from_dict(snapshot)
    if (derivative.source_text != cue.text
            or derivative.source_id != _source_id(project.id, cue.id)):
        raise ValueError('A narration derivative must refer to the exact original cue and project.')
    return derivative


def validate_narration(project):
    values = project.narration
    cues = _speech(project)
    if type(values) is not dict or len(values) > len(cues):
        raise ValueError('Invalid project narration snapshots.')
    for cue_id, snapshot in values.items():
        if not isinstance(cue_id, str) or cue_id not in cues:
            raise ValueError('A narration derivative refers to an unknown speech cue.')
        _bound(project, cues[cue_id], snapshot)
    return values


def cue_derivative(project, cue_id):
    cue = _speech(project).get(cue_id)
    if cue is None:
        raise ValueError('Select an original speech cue.')
    snapshot = project.narration.get(cue_id)
    return (_bound(project, cue, snapshot) if snapshot is not None else
            NarrationDerivative.create(cue.text, _source_id(project.id, cue.id)))


def spoken_cue(project, cue):
    if cue.kind != 'speech' or project.script[cue.source_start:cue.source_end] != cue.text:
        raise ValueError('Reading copies require an exact original speech cue.')
    snapshot = project.narration.get(cue.id)
    if snapshot is None:
        # Long unmodified cues remain stream/chunk compatible; editing one
        # segment has a separate two-million-character bound.
        identity = {'source_id': _source_id(project.id, cue.id),
                    'source_sha256': sha256(cue.text.encode('utf-8')).hexdigest(), 'edits': []}
        text = cue.text
        mapping = [{'derived_start': 0, 'derived_end': len(text), 'source_start': cue.source_start,
                    'source_end': cue.source_end, 'kind': 'unchanged', 'edit_id': None}]
    else:
        derivative = _bound(project, cue, snapshot)
        text, spans = derivative.render()
        mapping = []
        for span in spans:
            row = asdict(span)
            row['source_start'] += cue.source_start
            row['source_end'] += cue.source_start
            mapping.append(row)
        identity = derivative.to_dict()
    checksum = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return {'text': text, 'mapping': mapping, 'derivative_sha256': checksum}


def set_cue_derivative(project, cue_id, derivative):
    if type(derivative) is not NarrationDerivative:
        raise ValueError('Apply a validated narration derivative.')
    cue = _speech(project).get(cue_id)
    if cue is None:
        raise ValueError('Select an original speech cue.')
    _bound(project, cue, derivative.to_dict())
    values = dict(project.narration)
    values[cue_id] = derivative.to_dict()
    if values == project.narration:
        return project
    return replace(project, narration=values, revision=project.revision + 1)


def retained_narration(previous, new_cues, project_id):
    """Keep only snapshots whose stable cue ID, source text and binding survive."""
    if previous is None or previous.id != project_id:
        return {}
    result = {}
    for cue in new_cues:
        snapshot = previous.narration.get(cue.id)
        if cue.kind != 'speech' or snapshot is None:
            continue
        derivative = NarrationDerivative.from_dict(snapshot)
        if (derivative.source_text == cue.text
                and derivative.source_id == _source_id(project_id, cue.id)):
            result[cue.id] = derivative.to_dict()
    return result
