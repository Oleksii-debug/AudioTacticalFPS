"""Portable chapter CUE sheets for the existing measured PCM/WAVE production.

CUE indices use 75 frames per second. They are editor navigation markers, not
sample-exact edit decisions; the original project/timeline retain exact text,
scene identity and measured times. No audio or paths are read by this serializer.
"""
from fractions import Fraction
import unicodedata

from .publishing import _segments

_MAX_CHAPTERS = 10000
_MAX_INDEX = 2 ** 31 - 1
_NOTICE = ('CUE chapter markers are rounded down to 1/75 second. '
           'Use timeline.json for measured timing and exact source scene IDs. '
           'Import master.wav and optional stems at time zero; stems share that origin. '
           'A CUE sheet is not a lossless editor session or a proprietary project format. '
           'Editor-specific import behavior requires a separate check.')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Chapter interchange cancelled.')


def _label(value, fallback):
    if (not isinstance(value, str) or not value
            or any(character in '"\\' or unicodedata.category(character).startswith('C')
                   for character in value)):
        return fallback, False
    if len(value.encode('utf-8')) > 240:
        return fallback, False
    return value, True


def _stamp(index):
    minutes, remainder = divmod(index, 75 * 60)
    seconds, frames = divmod(remainder, 75)
    return f'{minutes:02d}:{seconds:02d}:{frames:02d}'


def chapter_cues(project, timeline, *, cancelled=lambda: False):
    """Return bounded CUE byte artifacts and their explicit interchange report.

    Unrepresentable adjacent chapter positions are reported without pretending
    that two distinct chapters became one. Invalid source/timeline identity is
    an error. The caller retains its existing transactional artifact publisher.
    """
    _check(cancelled)
    if len(project.scenes) > _MAX_CHAPTERS:
        return (), {'schema': 1, 'format': 'CUE/PCM WAVE', 'status': 'unavailable',
                    'reason': 'CUE export supports up to 10000 chapters per production.',
                    'notice': _NOTICE, 'files': [], 'editor_import': 'NOT_RUN'}
    _segments(project, timeline, lambda: _check(cancelled))
    rows = timeline['chapters']
    indices = []
    for row in rows:
        _check(cancelled)
        seconds = Fraction(str(row['start']))
        value = seconds * 75
        index = value.numerator // value.denominator
        if index > _MAX_INDEX or (indices and index <= indices[-1]):
            return (), {'schema': 1, 'format': 'CUE/PCM WAVE', 'status': 'unavailable',
                        'reason': 'Distinct chapter starts cannot be represented by ordered 75 Hz CUE indices.',
                        'notice': _NOTICE, 'files': [], 'editor_import': 'NOT_RUN'}
        indices.append(index)
    title, title_exact = _label(project.title, 'Resonance production')
    artifacts, sheets = [], []
    for offset in range(0, len(rows), 99):
        _check(cancelled)
        name = f'chapters-{offset // 99 + 1:04d}.cue'
        lines = ['REM Generated from measured Resonance chapter timing',
                 'REM Exact scene identity and original titles are retained in timeline.json',
                 f'TITLE "{title}"', 'FILE "master.wav" WAVE']
        bindings = []
        for number, row in enumerate(rows[offset:offset + 99], 1):
            _check(cancelled)
            ordinal = offset + number
            chapter_title, title_preserved = _label(row['title'], f'Chapter {ordinal:06d}')
            index = indices[ordinal - 1]
            lines.extend([f'  TRACK {number:02d} AUDIO',
                          f'    TITLE "{chapter_title}"',
                          f'    INDEX 01 {_stamp(index)}'])
            bindings.append({'track': number, 'chapter': ordinal, 'scene': row['id'],
                             'index_75hz': index, 'original_title_preserved': title_preserved})
        payload = ('\r\n'.join(lines) + '\r\n').encode('utf-8')
        if len(payload) > 64 * 1024:
            raise ValueError('The chapter CUE sheet exceeded its serialized budget.')
        artifacts.append((name, payload))
        sheets.append({'file': name, 'tracks': bindings})
    _check(cancelled)
    return tuple(artifacts), {'schema': 1, 'format': 'CUE/PCM WAVE', 'status': 'ready',
        'encoding': 'UTF-8', 'audio_file': 'master.wav', 'timebase': 75,
        'rounding': 'floor', 'maximum_marker_shift_seconds_exclusive': 1 / 75,
        'project_title_preserved': title_exact, 'notice': _NOTICE,
        'files': sheets, 'editor_import': 'NOT_RUN'}

def validate_report_shape(report):
    """Recognize only the current inert, bounded CUE declaration."""
    from ..backup_producers import fields, number, ID
    import re
    common = {'schema', 'format', 'status', 'notice', 'files', 'editor_import'}
    if type(report) is not dict:
        raise ValueError('Invalid chapter interchange declaration.')
    if report.get('status') == 'unavailable':
        fields(report, common | {'reason'})
        if report['reason'] not in (
                'CUE export supports up to 10000 chapters per production.',
                'Distinct chapter starts cannot be represented by ordered 75 Hz CUE indices.'):
            raise ValueError('Unknown chapter interchange refusal.')
        if type(report['files']) is not list or report['files']:
            raise ValueError('Unavailable chapter interchange cannot declare files.')
    elif report.get('status') == 'ready':
        fields(report, common | {'encoding', 'audio_file', 'timebase', 'rounding',
            'maximum_marker_shift_seconds_exclusive', 'project_title_preserved'})
        if (report['encoding'] != 'UTF-8' or report['audio_file'] != 'master.wav'
                or type(report['timebase']) is not int or report['timebase'] != 75
                or report['rounding'] != 'floor'
                or type(report['maximum_marker_shift_seconds_exclusive']) is not float
                or report['maximum_marker_shift_seconds_exclusive'] != 1 / 75
                or type(report['project_title_preserved']) is not bool
                or type(report['files']) is not list or not 1 <= len(report['files']) <= 102):
            raise ValueError('Invalid chapter interchange bounds.')
        ordinal = 0
        for number_, sheet in enumerate(report['files'], 1):
            fields(sheet, {'file', 'tracks'})
            if (sheet['file'] != f'chapters-{number_:04d}.cue'
                    or type(sheet['tracks']) is not list or not 1 <= len(sheet['tracks']) <= 99):
                raise ValueError('Invalid chapter interchange sheet.')
            for track_number, track in enumerate(sheet['tracks'], 1):
                fields(track, {'track', 'chapter', 'scene', 'index_75hz', 'original_title_preserved'})
                ordinal += 1
                if (type(track['track']) is not int or track['track'] != track_number
                        or type(track['chapter']) is not int or track['chapter'] != ordinal
                        or ordinal > 10000 or type(track['scene']) is not str
                        or not re.fullmatch(ID, track['scene'])
                        or type(track['original_title_preserved']) is not bool):
                    raise ValueError('Invalid chapter interchange source binding.')
                number(track['index_75hz'], 0, 2 ** 31 - 1, integer=True)
    else:
        raise ValueError('Unknown chapter interchange status.')
    if (type(report['schema']) is not int or report['schema'] != 1
            or report['format'] != 'CUE/PCM WAVE' or report['notice'] != _NOTICE
            or report['editor_import'] != 'NOT_RUN'):
        raise ValueError('Unknown chapter interchange schema or qualification.')



def storage_allowance(project):
    """Conservative extra bytes for CUE payloads and their inert declarations."""
    count = len(project.scenes)
    if count > _MAX_CHAPTERS:
        return {'private_bytes': 4096, 'export_bytes': 2048}
    sheets = (count + 98) // 99
    payload = sheets * 64 * 1024
    # Includes bounded scene IDs, fixed notice/keys, flags and sheet records.
    # Exact user titles remain in the existing project/timeline allowance.
    declaration = 2048 + count * 1024 + sheets * 512
    return {'private_bytes': payload + 2 * declaration,
            'export_bytes': payload + declaration}


def validated_cues(project, timeline, *, cancelled=lambda: False):
    """Recompute the declared bytes from the exact project and measured timeline."""
    from ..durable_state import encode
    validate_report_shape(timeline.get('editor_interchange'))
    _segments(project, timeline, lambda: _check(cancelled))
    artifacts, expected = chapter_cues(project, timeline, cancelled=cancelled)
    if encode(timeline['editor_interchange']) != encode(expected):
        raise ValueError('Chapter interchange differs from its exact project and measured timeline.')
    return artifacts


def validate_backup_state(root, entries, *, cancelled=lambda: False):
    """Validate copied CUE declarations without reading external media or running tools.

    New CUE sheets use the publisher's reserved pending files. Only the exact
    project and measured timeline remain in private state after publication.
    Legacy timelines without this declaration keep their existing recovery codec.
    """
    from pathlib import Path, PurePosixPath
    from ..backup_compat import _json, read_regular
    from .project import Project
    root = Path(root)
    inventory = {entry['filename'].casefold(): entry for entry in entries}
    for entry in entries:
        _check(cancelled)
        path = PurePosixPath(entry['filename'])
        folded = tuple(part.casefold() for part in path.parts)
        if len(folded) != 4 or folded[:2] != ('studio', 'jobs') or folded[3] != 'timeline.json':
            continue
        # Preserve the historical inert malformed-JSON behavior unless a current
        # typed declaration can be decoded. A real result in job.json is checked
        # separately before restore authority is withdrawn.
        if not 0 < entry['size_bytes'] <= 32 * 1024 * 1024:
            continue
        raw = read_regular(root / path, 32 * 1024 * 1024)
        try:
            timeline = _json(raw)
        except ValueError:
            continue
        if type(timeline) is not dict or 'editor_interchange' not in timeline:
            continue
        project_key = '/'.join((*folded[:3], 'project.json'))
        project_entry = inventory.get(project_key)
        if project_entry is None:
            raise ValueError('Chapter interchange needs its exact retained project checkpoint.')
        if project_entry['size_bytes'] > 32 * 1024 * 1024:
            raise ValueError('Chapter interchange project exceeds its recovery JSON budget.')
        project = Project.from_dict(_json(read_regular(
            root / project_entry['filename'], 32 * 1024 * 1024)))
        validated_cues(project, timeline, cancelled=cancelled)
    _check(cancelled)
