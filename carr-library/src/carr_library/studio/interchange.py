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
