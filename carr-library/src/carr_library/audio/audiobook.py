"""Measured M4B chapter publication through the existing local FFmpeg runtime."""
from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from .models import AudioCancelled, AudioError, finite, integer
from .process import publish_file, run_process
from ..durable_state import local_path


MAX_CHAPTERS = 10_000
MAX_METADATA_BYTES = 2_000_000


def _title(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 4096
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise ValueError('Use a nonempty bounded audiobook or chapter title.')
    value.encode('utf-8', 'strict')
    return value


def _escape(value):
    # FFmetadata's documented escaping; no shell or option interpolation.
    return ''.join('\\' + char if char in '\\=;#\n' else char
                   for char in value.replace('\r\n', '\n').replace('\r', '\n'))


def metadata_bytes(chapters, *, title, duration, sample_rate):
    title = _title(title)
    integer(sample_rate, 'Chapter sample rate', 8000, 192000)
    duration = finite(duration, 'Audiobook duration', .000001)
    if not isinstance(chapters, (tuple, list)) or not 1 <= len(chapters) <= MAX_CHAPTERS:
        raise ValueError('An audiobook needs a bounded, nonempty measured chapter list.')
    rows, previous, total = [], 0, round(duration * sample_rate)
    lines = [';FFMETADATA1', 'title=' + _escape(title)]
    for chapter in chapters:
        if not isinstance(chapter, dict) or set(chapter) - {'id', 'title', 'start', 'end'}:
            raise ValueError('Unsupported measured chapter fields.')
        name = _title(chapter.get('title'))
        first = round(finite(chapter.get('start'), 'Chapter start', 0) * sample_rate)
        last = round(finite(chapter.get('end'), 'Chapter end', 0) * sample_rate)
        if first != previous or not first < last <= total:
            raise ValueError('Measured chapters must cover the audio in order without gaps or overlaps.')
        rows.append({'title': name.replace('\r\n', '\n').replace('\r', '\n'),
                     'start': first, 'end': last})
        lines.extend(('[CHAPTER]', f'TIMEBASE=1/{sample_rate}', f'START={first}',
                      f'END={last}', 'title=' + _escape(name)))
        previous = last
    if previous != total:
        raise ValueError('The final chapter must end at the measured audio duration.')
    raw = ('\n'.join(lines) + '\n').encode('utf-8')
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError('Audiobook chapter metadata exceeds its size budget.')
    return raw, tuple(rows)


def inspect_chapters(runtime, path, *, cancelled=lambda: False):
    """Read actual local container metadata with bounded, cancellable ffprobe."""
    path = runtime._input(path)
    result = run_process([runtime.ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
        '-format_whitelist', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0',
        '-show_chapters', '-show_entries', 'format_tags=title', '-of', 'json', str(path)],
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)), cancelled=cancelled)
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not isinstance(value.get('chapters'), list):
            raise ValueError('Missing chapter list.')
        if len(value['chapters']) > MAX_CHAPTERS:
            raise ValueError('Too many embedded chapters.')
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        raise AudioError('The rendered audiobook has invalid chapter metadata.') from exc


def publish_m4b(runtime, source, output, *, chapters, title, sample_rate=24000,
                channels=2, bitrate=192, cancelled=lambda: False, progress=None):
    """Encode once, then copy the audio stream while embedding reviewed chapters."""
    from .dsp import _digest, _identity
    if cancelled():
        raise AudioCancelled('Audiobook publication cancelled.')
    source, target = runtime._input(source), runtime._output(output)
    if target.suffix.lower() != '.m4b':
        raise ValueError('Measured audiobook publication requires an M4B destination.')
    measured = runtime.inspect(source, cancelled=cancelled)
    raw, rows = metadata_bytes(chapters, title=title, duration=measured.duration, sample_rate=sample_rate)
    snapshot, source_digest = _identity(source), _digest(source, cancelled)
    estimate = runtime._space(measured.duration, sample_rate, channels, 16, target.parent)
    if shutil.disk_usage(target.parent).free < 2 * estimate + len(raw) + runtime.budget.reserve_disk_bytes:
        raise AudioError('Not enough free disk for encoded audiobook and verified chapter staging.')
    with TemporaryDirectory(prefix='.audiobook-', dir=target.parent) as temporary:
        staging = Path(temporary)
        encoded, tagged, metadata = staging / 'encoded.m4b', staging / 'chapters.m4b', staging / 'metadata.txt'
        runtime.convert(source, encoded, sample_rate=sample_rate, channels=channels,
                        bitrate=bitrate, cancelled=cancelled, progress=progress)
        metadata.write_bytes(raw)
        command = [*runtime._base_args(), *runtime._input_args(encoded),
                   '-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(metadata),
                   '-map', '0:a:0', '-map_metadata', '1', '-map_chapters', '1',
                   '-c:a', 'copy', '-vn', '-sn', '-dn', '-f', 'ipod', str(tagged)]
        run_process(command, budget=runtime.budget, cancelled=cancelled, watch=(encoded, tagged))
        info = runtime.inspect(tagged, cancelled=cancelled)
        embedded = inspect_chapters(runtime, tagged, cancelled=cancelled)
        expected_title = title.replace('\r\n', '\n').replace('\r', '\n')
        if (embedded.get('format', {}).get('tags', {}).get('title') != expected_title
                or len(embedded['chapters']) != len(rows)):
            raise AudioError('The embedded audiobook title or chapter count differs from the approved metadata.')
        for actual, expected in zip(embedded['chapters'], rows):
            try:
                base = Fraction(actual['time_base'])
                first, last = int(actual['start']) * base, int(actual['end']) * base
                tolerance = max(Fraction(1, sample_rate), Fraction(1, 1000))
                if (actual.get('tags', {}).get('title') != expected['title']
                        or abs(first - Fraction(expected['start'], sample_rate)) > tolerance
                        or abs(last - Fraction(expected['end'], sample_rate)) > tolerance):
                    raise ValueError('Changed chapter identity.')
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                raise AudioError('The embedded chapter names or times differ from the measured timeline.') from exc
        if (info.sample_rate != sample_rate or info.channels != channels
                or abs(info.duration - measured.duration) > .15 or info.size > runtime.budget.max_output_bytes):
            raise AudioError('The chaptered audiobook does not match the requested audio properties.')
        if _identity(source) != snapshot or _digest(source, cancelled) != source_digest:
            raise AudioError('The source audio changed during audiobook publication.')
        checksum = _digest(tagged, cancelled)
        if local_path(target.parent, exists=True) != target.parent or not target.parent.is_relative_to(runtime.root):
            raise AudioError('The audiobook output directory changed during publication.')
        publish_file(tagged, target, cancelled=cancelled)
        return replace(info, path=target, sha256=checksum)


def line_lrc(lines):
    """Simple line-level LRC; measured starts only, without inferred word times."""
    result, previous = [], -1
    for line in lines:
        start = finite(line['start'], 'LRC line start', 0)
        end = finite(line['end'], 'LRC line end', 0)
        if end <= start or start < previous or not isinstance(line['text'], str):
            raise ValueError('Invalid measured LRC line.')
        previous = start
        centiseconds = round(start * 100)
        minutes, seconds = divmod(centiseconds, 6000)
        stamp = f'[{minutes:02}:{seconds // 100:02}.{seconds % 100:02}]'
        # Every visual line belongs to the same measured narration interval.
        # Repeated stamps do not invent internal word or paragraph alignment.
        for text in line['text'].splitlines() or ['']:
            if '\x00' in text:
                raise ValueError('Invalid LRC text.')
            result.append(stamp + text)
    return '\n'.join(result) + ('\n' if result else '')
