"""Read-only projection of the existing Audio Studio completion contract.

No canonical Studio project/job schema is installed. The original source text,
saved ProductionPlan, sealed artifacts and integer-frame timeline are checked
before a transient serializer view is created.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
from types import SimpleNamespace

from ..research.export_file import _filename
from .jobs import checked_directory
from .models import Line, Sound, ProductionPlan, digest
from .publication_io import read_identity

_HASH = re.compile('[0-9a-f]{64}')
_MAX_METADATA = 32 * 1024**2
_MAX_ARTIFACTS = 100000


def render_id(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError('Choose an existing complete Audio Studio render ID.')
    return value


def _frame(value, label, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(label + ' must be a bounded integer frame position.')
    return value


def _document(path, check):
    from .publishing import _json
    raw, identity = read_identity(path, check, _MAX_METADATA, collect=True)
    value = _json(raw)
    if not isinstance(value, dict):
        raise ValueError('Audio Studio publication metadata must be an object.')
    return value, identity


def _artifact(record):
    if (not isinstance(record, dict) or set(record) != {'name', 'size', 'sha256'}
            or not isinstance(record['name'], str)
            or type(record['size']) is not int or record['size'] < 0
            or not isinstance(record['sha256'], str) or not _HASH.fullmatch(record['sha256'])):
        raise ValueError('Invalid completed Audio Studio artifact receipt.')
    _filename(record['name'])
    return record['name'], {'size': record['size'], 'sha256': record['sha256']}


def _legacy_plan_identity(plan, marker, root, check, maximum):
    """Reconstruct the existing run key; never guess a missing plan binding."""
    assets = {}
    composer = None
    for scene in plan.scenes:
        check()
        if scene.music is not None and composer is None:
            from . import music
            composer = {'implementation_sha256': read_identity(music.__file__, check, maximum)['sha256'],
                        'version': music.COMPOSER_VERSION}
        for event in scene.events:
            check()
            if isinstance(event, Sound) and event.asset not in assets:
                assets[event.asset] = read_identity(root / 'assets' / event.asset, check, maximum,
                                                   allow_hardlinks=True)['sha256']
    expected = digest({'pipeline_version': 1, 'plan': plan.to_dict(),
        'engines': marker.get('engines'), 'processor': marker.get('processor'),
        'assets': assets, 'music_models': marker.get('music_models'), 'composer': composer})
    if expected != marker['run_key']:
        raise ValueError('The historical render cannot attest this exact saved plan. '
                         'Render this plan with the current pipeline before publishing it.')


def load_source(project_root, job_id, check, budget):
    root = checked_directory(project_root)
    job_id = render_id(job_id)
    destination = checked_directory(root / 'renders' / job_id)
    marker, marker_identity = _document(destination / 'complete.json', check)
    if (type(marker.get('schema_version')) is not int or marker['schema_version'] != 1
            or marker.get('status') != 'complete' or marker.get('run_key') != job_id):
        raise ValueError('Choose a complete render owned by this Audio Studio output directory.')
    plan_value, plan_identity = _document(root / ('plan-' + job_id + '.json'), check)
    plan = ProductionPlan.from_dict(plan_value)
    plan_digest = digest(plan.to_dict())
    if 'plan_sha256' in marker:
        if marker['plan_sha256'] != plan_digest:
            raise ValueError('The saved production plan differs from the completed render identity.')
    else:
        _legacy_plan_identity(plan, marker, root, check, budget.max_output_bytes)
    if (marker.get('source_sha256') != plan.source_sha256
            or marker.get('input_kind', 'script') != plan.input_kind
            or marker.get('sample_rate') != plan.sample_rate):
        raise ValueError('The completed render belongs to different source text or settings.')
    records = marker.get('files')
    if not isinstance(records, list) or not 4 <= len(records) <= _MAX_ARTIFACTS:
        raise ValueError('The render needs its bounded completed artifact inventory.')
    paths, folded, total = {}, set(), marker_identity['size'] + plan_identity['size']
    for record in records:
        check()
        name, identity = _artifact(record)
        if name.casefold() in folded or name == 'complete.json':
            raise ValueError('Duplicate or ambiguous completed audio artifact.')
        folded.add(name.casefold())
        total += identity['size']
        if total > budget.max_output_bytes:
            raise ValueError('The source artifact graph exceeds the publication byte budget.')
        paths[name] = (destination / name, identity)
    actual = set()
    with os.scandir(destination) as entries:
        for entry in entries:
            check()
            if len(actual) >= _MAX_ARTIFACTS + 1:
                raise ValueError('The completed render exceeds its file count budget.')
            actual.add(entry.name)
    if actual != set(paths) | {'complete.json'}:
        raise ValueError('The completed render artifact graph has missing or unexpected files.')
    if not {'master.wav', 'timeline.json', 'subtitles.srt', 'chapters.ffmeta'} <= set(paths):
        raise ValueError('The completed render lacks its measured WAV, timeline or source navigation.')
    if marker.get('output_name') not in paths or not isinstance(marker.get('stem_names'), list):
        raise ValueError('The completed render has an invalid output selection.')
    if any(name not in paths for name in marker['stem_names']):
        raise ValueError('A completed stem is missing from the artifact inventory.')
    timeline = None
    for name, (path, identity) in paths.items():
        check()
        if name == 'timeline.json':
            from .publishing import _json
            raw, _ = read_identity(path, check, min(_MAX_METADATA, budget.max_output_bytes),
                                   expected=identity, collect=True, allow_hardlinks=True)
            timeline = _json(raw)
        else:
            # The existing renderer intentionally links sealed chapter PCM and
            # music companion files into this immutable completed artifact set.
            read_identity(path, check, budget.max_output_bytes, expected=identity,
                          allow_hardlinks=True)
    rate = plan.sample_rate
    frames = _frame(marker.get('frames'), 'Master frames', rate * 365 * 86400)
    if frames < 1 or marker.get('duration_seconds') != frames / rate:
        raise ValueError('The render has an invalid measured master duration.')
    if (not isinstance(timeline, dict)
            or set(timeline) != {'schema_version', 'sample_rate', 'source_sha256', 'chapters', 'lines', 'scenes'}
            or type(timeline['schema_version']) is not int or timeline['schema_version'] != 1
            or timeline['sample_rate'] != rate or timeline['source_sha256'] != plan.source_sha256
            or timeline['chapters'] != marker.get('chapters')
            or not isinstance(timeline['chapters'], list)
            or len(timeline['chapters']) != len(plan.scenes)
            or not isinstance(timeline['scenes'], list)
            or len(timeline['scenes']) != len(plan.scenes)
            or not isinstance(timeline['lines'], list)):
        raise ValueError('The saved timeline does not belong to the exact completed source.')
    expected_lines = [(scene, event) for scene in plan.scenes for event in scene.events if isinstance(event, Line)]
    if len(expected_lines) != len(timeline['lines']) or len(expected_lines) > 100000:
        raise ValueError('Every source line needs its exact measured frame interval.')
    measured, frame_rows = {}, []
    for row, (scene, event) in zip(timeline['lines'], expected_lines):
        check()
        if (not isinstance(row, dict)
                or set(row) - {'role', 'text', 'start_frame', 'end_frame', 'event_id',
                               'literal_narration', 'source_start', 'source_end', 'timing'}
                or row.get('event_id') != event.id or row.get('role') != event.role
                or row.get('text') != event.text or row.get('literal_narration', False) is not event.literal
                or row.get('source_start') != event.source_start or row.get('source_end') != event.source_end):
            raise ValueError('A measured line was rebound to different source text or identity.')
        start = _frame(row.get('start_frame'), 'Line start', frames)
        end = _frame(row.get('end_frame'), 'Line end', frames)
        if end <= start or (scene.id, event.id) in measured:
            raise ValueError('A measured source line needs a unique positive interval.')
        measured[scene.id, event.id] = (start, end)
        frame_rows.append({'event_id': event.id, 'scene_id': scene.id,
                           'start_frame': start, 'end_frame': end,
                           'literal_narration': event.literal,
                           'source_start': event.source_start, 'source_end': event.source_end})
    chapters, adapted_lines, adapted_scenes, cursor = [], [], [], 0
    for scene, chapter, stored_scene in zip(plan.scenes, timeline['chapters'], timeline['scenes']):
        check()
        if (not isinstance(chapter, dict)
                or set(chapter) != {'id', 'title', 'start_frame', 'end_frame', 'sample_rate'}
                or chapter['id'] != scene.id or chapter['title'] != scene.title
                or chapter['sample_rate'] != rate
                or not isinstance(stored_scene, dict) or set(stored_scene) != {'scene_id', 'events'}
                or stored_scene['scene_id'] != scene.id or not isinstance(stored_scene['events'], list)
                or len(stored_scene['events']) != len(scene.events)):
            raise ValueError('Measured chapter identity or event graph differs from the saved plan.')
        start = _frame(chapter['start_frame'], 'Chapter start', frames)
        end = _frame(chapter['end_frame'], 'Chapter end', frames)
        if start != cursor or end <= start:
            raise ValueError('Measured chapters must cover the master without gaps or overlaps.')
        local_cursor, projected_lines = 0, []
        for event, row in zip(scene.events, stored_scene['events']):
            check()
            if (not isinstance(row, dict)
                    or set(row) != {'event_id', 'kind', 'start_frame', 'frames', 'sha256'}
                    or row['event_id'] != event.id or row['kind'] != type(event).__name__.lower()
                    or not isinstance(row['sha256'], str) or not _HASH.fullmatch(row['sha256'])):
                raise ValueError('A measured chapter event differs from the source plan.')
            at = _frame(row['start_frame'], 'Event start', end - start)
            count = _frame(row['frames'], 'Event frames', end - start)
            if at != local_cursor or count < 1 or at + count > end - start:
                raise ValueError('A measured chapter event overlaps or exceeds its chapter.')
            local_cursor += count
            if isinstance(event, Line):
                if measured[scene.id, event.id] != (start + at, start + at + count):
                    raise ValueError('Line and chapter frame receipts disagree.')
                # Whitespace-only narration produces one or more silent frames.
                # Preserve it in the exact source, without inventing spoken text.
                if event.text.strip():
                    reference = (f'codepoints:{event.source_start}:{event.source_end}' if event.literal else '')
                    line = SimpleNamespace(id=event.id, role=event.role, text=event.text, source_ref=reference)
                    projected_lines.append(line)
                    adapted_lines.append({'scene': scene.id, 'line': event.id, 'role': event.role,
                        'text': event.text, 'source_ref': reference,
                        'start': (start + at) / rate, 'end': (start + at + count) / rate})
        if scene.events and local_cursor != end - start:
            raise ValueError('The chapter frame total differs from its measured events.')
        chapters.append({'id': scene.id, 'title': scene.title, 'start': start / rate, 'end': end / rate})
        adapted_scenes.append(SimpleNamespace(id=scene.id, title=scene.title, lines=tuple(projected_lines)))
        cursor = end
    if cursor != frames:
        raise ValueError('Measured chapters do not reach the actual master end.')
    view = SimpleNamespace(id=job_id, revision=1, title=plan.title,
        original_script=plan.original_script, sample_rate=rate, fingerprint=plan_digest,
        roles=tuple(SimpleNamespace(id=role.name, name=role.name, language=role.language) for role in plan.roles),
        scenes=tuple(adapted_scenes))
    adapted = {'schema': 1, 'project': job_id, 'revision': 1, 'source_sha256': plan.source_sha256,
        'sample_rate': rate, 'channels': 2, 'duration': frames / rate,
        'chapters': chapters, 'lines': adapted_lines}
    identity = {'run_key': job_id, 'marker': marker_identity, 'plan': plan_identity,
                'files': [{'name': name, **record} for name, (_, record) in sorted(paths.items())]}
    return SimpleNamespace(root=root, destination=destination, plan=plan, project=view,
        timeline=adapted, marker=marker, paths=paths, identity=identity, frames=frame_rows)


def completed_jobs(project_root, check=lambda: None):
    root = checked_directory(project_root)
    renders = root / 'renders'
    if not renders.exists():
        return ()
    checked_directory(renders)
    found = []
    with os.scandir(renders) as entries:
        for index, entry in enumerate(entries, 1):
            check()
            if index > 10000:
                raise ValueError('The audio project exceeds the completed-render listing budget.')
            if not _HASH.fullmatch(entry.name):
                raise ValueError('Unexpected entry in the completed-render directory.')
            directory = checked_directory(Path(entry.path))
            marker, _ = _document(directory / 'complete.json', check)
            plan, _ = _document(root / ('plan-' + entry.name + '.json'), check)
            if (marker.get('status') != 'complete' or marker.get('run_key') != entry.name
                    or not isinstance(plan.get('title'), str)):
                raise ValueError('A saved production is incomplete or inconsistent.')
            found.append({'id': entry.name, 'title': plan['title'],
                          'duration': marker.get('duration_seconds')})
    return tuple(sorted(found, key=lambda item: item['id']))
