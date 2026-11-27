"""Independent structural checks of the supplied worker's exported derivatives.

Status counters and worker-provided paths never authorize an output. Expected
paths, owners and hashes are derived from the approved input/profile snapshot.
These checks establish consistency, not speech-recognition accuracy.
"""
from datetime import datetime
from hashlib import sha256
import math
from pathlib import Path
import re

from .files import canonical, parse_json, read_bytes, read_json
from .models import FORMATS, checksum, exact_keys


_OUTPUT_SETTINGS = frozenset(('output_mode', 'include_headers', 'output_txt',
                              'output_srt', 'output_vtt', 'output_json'))


def same(left, right):
    return canonical(left) == canonical(right)


def owner(job, source, settings, runtime):
    path = Path(job) / 'inputs' / source['name']
    return {'schema': 1, 'source': str(path),
            'locator': {'group': 'inputs', 'relative': source['name'], 'root': str(path.parent)},
            'audio': {'sha256': source['sha256'], 'bytes': source['bytes']},
            'model': parse_json(runtime.model_json), 'runtime': runtime.worker_identity(),
            'settings': {key: value for key, value in settings.items() if key not in _OUTPUT_SETTINGS}}


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**9:
        raise ValueError(f'Invalid transcript {label}.')
    return value


def _text(value, label, maximum):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
        raise ValueError(f'Invalid transcript {label}.')
    value.encode('utf-8')
    return value


def _metadata(value, expected_owner, settings, languages):
    exact_keys(value, ('source', 'group', 'relative', 'created', 'selected_language',
        'detected_language', 'duration', 'settings', 'segments', 'text', 'v43_attempt',
        'v43_attempt_no', 'requested_settings', 'reuse_identity'), 'transcript metadata')
    if (value['source'] != expected_owner['source'] or value['group'] != 'inputs'
            or value['relative'] != expected_owner['locator']['relative']
            or not same(value['reuse_identity'], expected_owner)
            or not same(value['requested_settings'], settings)):
        raise ValueError('Transcript ownership differs from the approved input/runtime/settings.')
    if value['selected_language'] != settings['language'] or value['detected_language'] not in languages:
        raise ValueError('Transcript language does not match the selected profile.')
    _text(value['created'], 'creation time', 64)
    datetime.fromisoformat(value['created'])
    candidates = [('original', settings),
                  ('beam1_no_previous', {**settings, 'beam_size': 1, 'condition_on_previous_text': False}),
                  ('beam1_no_vad_no_previous', {**settings, 'beam_size': 1, 'vad_filter': False,
                                                'condition_on_previous_text': False})]
    attempts, seen = [], set()
    for name, candidate in candidates:
        encoded = canonical(candidate)
        if encoded not in seen:
            attempts.append((name, candidate))
            seen.add(encoded)
    index = value['v43_attempt_no']
    if (type(index) is not int or not 1 <= index <= len(attempts)
            or attempts[index - 1][0] != value['v43_attempt']
            or not same(attempts[index - 1][1], value['settings'])):
        raise ValueError('The transcript reports unapproved decoder settings or retry identity.')
    duration = _number(value['duration'], 'duration')
    segments = value['segments']
    if type(segments) is not list or len(segments) > 1000000:
        raise ValueError('Invalid transcript segment budget.')
    previous_start = 0
    lines = []
    for segment in segments:
        exact_keys(segment, ('id', 'seek', 'start', 'end', 'text', 'words'), 'segment')
        for key in ('id', 'seek'):
            if segment[key] is not None and (type(segment[key]) is not int or segment[key] < 0):
                raise ValueError('Invalid transcript segment identity.')
        start, end = _number(segment['start'], 'segment start'), _number(segment['end'], 'segment end')
        if start < previous_start or end < start or end > duration + 1:
            raise ValueError('Invalid transcript segment order or duration.')
        previous_start = start
        text = _text(segment['text'], 'segment text', 1000000)
        if text != text.strip():
            raise ValueError('Segment text has inconsistent normalization.')
        lines.extend(line.strip() for line in text.splitlines() if line.strip())
        words = segment['words']
        if type(words) is not list or len(words) > 100000:
            raise ValueError('Invalid optional word record budget.')
        previous_word = start
        for word in words:
            exact_keys(word, ('start', 'end', 'word'), 'word record')
            a, b = _number(word['start'], 'word start'), _number(word['end'], 'word end')
            if a < previous_word or b < a or a < start or b > end:
                raise ValueError('Invalid optional word record timing.')
            previous_word = a
            _text(word['word'], 'word', 1000000)
    text = _text(value['text'], 'text', 256 * 1024**2)
    if text != '\n'.join(lines):
        raise ValueError('Transcript text and segment text disagree.')
    return value


def _timestamp(value, separator):
    match = re.fullmatch(r'(\d{2,}):(\d{2}):(\d{2})' + re.escape(separator) + r'(\d{3})', value)
    if match is None:
        raise ValueError('Invalid subtitle timestamp syntax.')
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60 or hours > 277777:
        raise ValueError('Invalid subtitle timestamp range.')
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def validate_subtitles(raw, segments, fmt):
    text = raw.decode('utf-8')
    if '\r' in text:
        raise ValueError('Unexpected subtitle line encoding.')
    if fmt == 'vtt':
        if not text.startswith('WEBVTT\n\n'):
            raise ValueError('Missing VTT header.')
        text = text[len('WEBVTT\n\n'):]
    actual = text.splitlines()
    cursor = 0
    nonempty = [(number, row) for number, row in enumerate(segments, 1) if row['text']]
    for position, (number, segment) in enumerate(nonempty):
        if fmt == 'srt':
            if cursor >= len(actual) or actual[cursor] != str(number):
                raise ValueError('Invalid subtitle segment identity.')
            cursor += 1
        if cursor >= len(actual) or actual[cursor].count(' --> ') != 1:
            raise ValueError('Missing subtitle timing.')
        a, b = actual[cursor].split(' --> ')
        cursor += 1
        separator = ',' if fmt == 'srt' else '.'
        if (_timestamp(a, separator) != round(segment['start'] * 1000)
                or _timestamp(b, separator) != round(segment['end'] * 1000)):
            raise ValueError('Subtitle timing differs from verified segment metadata.')
        lines = segment['text'].splitlines()
        if actual[cursor:cursor + len(lines)] != lines:
            raise ValueError('Subtitle text differs from verified segment metadata.')
        cursor += len(lines)
        if position < len(nonempty) - 1:
            if cursor >= len(actual) or actual[cursor] != '':
                raise ValueError('Missing subtitle separator.')
            cursor += 1
    if cursor != len(actual) or bool(nonempty) != text.endswith('\n'):
        raise ValueError('Unexpected or truncated subtitle content.')


def preflight_outputs(job, sources, settings, runtime, maximum, check):
    """A resume may reuse owned partial exports; it cannot adopt unrelated TXT."""
    root = Path(job) / 'results'
    names = set(path.name for path in root.iterdir())
    if names - {'inputs'}:
        raise ValueError('Unexpected existing output paths; no files were overwritten.')
    if not names:
        return
    existing = set(path.name for path in (root / 'inputs').iterdir())
    for source in sources:
        expected_owner = owner(job, source, settings, runtime)
        for fmt in FORMATS:
            if not settings['output_' + fmt]:
                continue
            name = Path(source['name']).stem + '.' + fmt
            proof_name = name + '.v43-output.json'
            if name not in existing and proof_name not in existing:
                continue
            if proof_name not in existing:
                raise ValueError('An existing output has no ownership proof; original was kept.')
            proof, _ = read_json(root / 'inputs' / proof_name, maximum, check)
            exact_keys(proof, ('schema', 'owners', 'export_settings', 'sha256', 'bytes'), 'output ownership')
            checksum(proof['sha256'])
            if type(proof['bytes']) is not int or not 0 <= proof['bytes'] <= maximum:
                raise ValueError('An existing output ownership record exceeds its budget.')
            expected = {'schema': 1, 'owners': [expected_owner],
                        'export_settings': {'output_mode': 'separate', 'include_headers': False} if fmt == 'txt' else {},
                        'sha256': proof['sha256'], 'bytes': proof['bytes']}
            if not same(proof, expected):
                raise ValueError('An existing output belongs to another source or runtime; original was kept.')
            existing.remove(proof_name)
            if name in existing:
                raw = read_bytes(root / 'inputs' / name, maximum, check)
                if len(raw) != proof['bytes'] or sha256(raw).hexdigest() != proof['sha256']:
                    raise ValueError('An existing output was edited; original was kept.')
                existing.remove(name)
    if existing:
        raise ValueError('Unexpected existing output files; no files were overwritten.')


def verify_outputs(job, sources, settings, runtime, maximum, check):
    """Read only the known outputs and compare exact independent ownership."""
    result_root = Path(job) / 'results' / 'inputs'
    output_records, observed = [], []
    expected_names = set()
    for source in sources:
        check()
        expected_owner = owner(job, source, settings, runtime)
        base = Path(source['name']).stem
        requested = tuple(fmt for fmt in FORMATS if settings['output_' + fmt])
        payloads = {}
        for fmt in requested:
            name = base + '.' + fmt
            proof_name = name + '.v43-output.json'
            expected_names.update((name, proof_name))
            raw = read_bytes(result_root / name, maximum, check)
            proof, proof_raw = read_json(result_root / proof_name, maximum, check)
            expected = {'schema': 1, 'owners': [expected_owner],
                        'export_settings': {'output_mode': 'separate', 'include_headers': False} if fmt == 'txt' else {},
                        'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}
            if not same(proof, expected):
                raise ValueError('Transcript output checksum, length or ownership does not match.')
            payloads[fmt] = raw
            for filename, contents in ((name, raw), (proof_name, proof_raw)):
                output_records.append({'path': 'results/inputs/' + filename,
                                       'sha256': sha256(contents).hexdigest(), 'bytes': len(contents)})
        meta = _metadata(parse_json(payloads['json']), expected_owner, settings, runtime.languages)
        if 'txt' in payloads and payloads['txt'] != (meta['text'] + ('\n' if meta['text'] else '')).encode('utf-8'):
            raise ValueError('TXT output differs from verified transcript metadata.')
        for fmt in ('srt', 'vtt'):
            if fmt in payloads:
                validate_subtitles(payloads[fmt], meta['segments'], fmt)
        observed.append({'source': source['name'], 'detected_language': meta['detected_language'],
                         'duration': meta['duration'], 'attempt': meta['v43_attempt'],
                         'attempt_no': meta['v43_attempt_no'], 'effective_settings': meta['settings']})
    # Extra output names never become user-visible artifacts through status data.
    if (set(path.name for path in result_root.iterdir()) != expected_names
            or set(path.name for path in result_root.parent.iterdir()) != {'inputs'}):
        raise ValueError('The worker produced unexpected output paths.')
    check()
    return output_records, observed
