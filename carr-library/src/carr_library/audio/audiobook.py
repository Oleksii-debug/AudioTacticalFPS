"""Measured M4B chapter publication through the existing local FFmpeg runtime."""
from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import json
import os
import stat
from time import monotonic
from pathlib import Path
import shutil

from .models import AudioCancelled, AudioError, finite, integer
from .cleanup import retained_temporary_directory
from .process import publish_file, run_process
from ..durable_state import local_path, regular


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
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)),
        cancelled=cancelled, owned_tree=True)
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not isinstance(value.get('chapters'), list):
            raise ValueError('Missing chapter list.')
        if len(value['chapters']) > MAX_CHAPTERS:
            raise ValueError('Too many embedded chapters.')
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        raise AudioError('The rendered audiobook has invalid chapter metadata.') from exc


def _owned_publication(staged, target, owner, parent_owner, checksum, maximum, *, check=lambda: None):
    """Verify only this attempt's linked, already inspected container."""
    local_path(staged.parent, exists=True)

    def parent():
        observed = regular(local_path(target.parent, exists=True), directory=True)
        if (observed.st_dev, observed.st_ino) != (parent_owner.st_dev, parent_owner.st_ino):
            raise AudioError('The audiobook output directory changed during publication.')

    def validate(info):
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (info.st_dev, info.st_ino) != (owner.st_dev, owner.st_ino)
                or info.st_size != owner.st_size or info.st_nlink != 2
                or not 0 < info.st_size <= maximum):
            raise AudioError('The M4B is not this attempt\'s atomically published file.')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_nlink)

    check()
    parent()
    before = validate(target.lstat())
    if validate(staged.lstat()) != before:
        raise AudioError('The inspected M4B staging identity changed.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    digest, count = sha256(), 0
    with os.fdopen(os.open(target, flags), 'rb') as stream:
        if validate(os.fstat(stream.fileno())) != before:
            raise AudioError('The published M4B changed before verification.')
        while block := stream.read(min(1024 * 1024, owner.st_size - count + 1)):
            check()
            count += len(block)
            if count > owner.st_size:
                raise AudioError('The published M4B grew during verification.')
            digest.update(block)
        check()
        parent()
        local_path(staged.parent, exists=True)
        if (validate(os.fstat(stream.fileno())) != before
                or validate(target.lstat()) != before or validate(staged.lstat()) != before):
            raise AudioError('The published M4B changed during verification.')
    if count != owner.st_size or digest.hexdigest() != checksum:
        raise AudioError('The published M4B differs from its inspected bytes.')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def _bounded_digest(path, expected, cancelled):
    """Hash only the captured regular file, including an exact EOF check."""
    if cancelled():
        raise AudioCancelled('Audiobook verification cancelled.')
    path = local_path(path, exists=True)
    if _stamp(regular(path)) != expected:
        raise AudioError('The source audio changed during audiobook publication.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != expected:
            raise AudioError('The source audio changed before audiobook verification.')
        while True:
            if cancelled():
                raise AudioCancelled('Audiobook verification cancelled.')
            block = stream.read(min(1024 * 1024, expected[2] - count + 1))
            if not block:
                break
            count += len(block)
            if count > expected[2]:
                raise AudioError('The source audio grew during audiobook verification.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != expected:
            raise AudioError('The source audio changed during audiobook verification.')
    if count != expected[2] or _stamp(regular(local_path(path, exists=True))) != expected:
        raise AudioError('The source audio changed after audiobook verification.')
    return digest.hexdigest()


def publish_m4b(runtime, source, output, *, chapters, title, sample_rate=24000,
                channels=2, bitrate=192, cancelled=lambda: False, progress=None):
    """Encode once, then copy the audio stream while embedding reviewed chapters."""
    if cancelled():
        raise AudioCancelled('Audiobook publication cancelled.')
    source, target = runtime._input(source), runtime._output(output)
    if target.suffix.lower() != '.m4b':
        raise ValueError('Measured audiobook publication requires an M4B destination.')
    parent_owner = regular(local_path(target.parent, exists=True), directory=True)
    snapshot = _stamp(regular(source))
    measured = runtime.inspect(source, cancelled=cancelled)
    raw, rows = metadata_bytes(chapters, title=title, duration=measured.duration, sample_rate=sample_rate)
    source_digest = _bounded_digest(source, snapshot, cancelled)
    estimate = runtime._space(measured.duration, sample_rate, channels, 16, target.parent)
    if shutil.disk_usage(target.parent).free < 2 * estimate + len(raw) + runtime.budget.reserve_disk_bytes:
        raise AudioError('Not enough free disk for encoded audiobook and verified chapter staging.')
    with retained_temporary_directory(prefix='.audiobook-', dir=target.parent) as temporary:
        staging = Path(temporary)
        encoded, tagged, metadata = staging / 'encoded.m4b', staging / 'chapters.m4b', staging / 'metadata.txt'
        runtime.convert(source, encoded, sample_rate=sample_rate, channels=channels,
                        bitrate=bitrate, cancelled=cancelled, progress=progress)
        metadata.write_bytes(raw)
        command = [*runtime._base_args(), *runtime._input_args(encoded, cancelled=cancelled),
                   '-protocol_whitelist', 'file', '-f', 'ffmetadata', '-i', str(metadata),
                   '-map', '0:a:0', '-map_metadata', '1', '-map_chapters', '1',
                   '-c:a', 'copy', '-vn', '-sn', '-dn', '-f', 'ipod', str(tagged)]
        run_process(command, budget=runtime.budget, cancelled=cancelled,
                    watch=(staging,), owned_tree=True)
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
        if _bounded_digest(runtime._input(source), snapshot, cancelled) != source_digest:
            raise AudioError('The source audio changed during audiobook publication.')
        staged_owner = regular(tagged)
        staged_stamp = _stamp(staged_owner)
        checksum = _bounded_digest(tagged, staged_stamp, cancelled)

        def publication_cancelled():
            if cancelled():
                return True
            current_parent = regular(local_path(target.parent, exists=True), directory=True)
            if ((current_parent.st_dev, current_parent.st_ino) !=
                    (parent_owner.st_dev, parent_owner.st_ino)
                    or not target.parent.is_relative_to(runtime.root)):
                raise AudioError('The audiobook output directory changed during publication.')
            if _stamp(regular(tagged)) != staged_stamp or _stamp(regular(source)) != snapshot:
                raise AudioError('Audiobook staging or source changed before publication.')
            return False

        failure = None
        try:
            # publish_file invokes this same guard again after its blocking fsync,
            # immediately before its existing create-only hard-link commit.
            publish_file(tagged, target, cancelled=publication_cancelled)
        except Exception as exc:
            if not os.path.lexists(target):
                raise
            failure = exc
        deadline = monotonic() + max(5, min(30, runtime.budget.timeout))

        def final_check():
            if monotonic() >= deadline:
                raise AudioError('The published M4B needs explicit byte verification.')

        try:
            # The only accepted target is this attempt's exact second link to
            # the inspected staging inode, with unchanged parent and byte hash.
            _owned_publication(tagged, target, staged_owner, parent_owner, checksum,
                               runtime.budget.max_output_bytes, check=final_check)
        except Exception as exc:
            if failure is not None:
                raise failure from exc
            raise AudioError('An M4B was published, but its final identity could not be confirmed. '
                             'The output was retained for explicit verification.') from exc
        if failure is not None and not isinstance(failure, AudioCancelled):
            raise AudioError('M4B was published and its exact bytes were verified; '
                             'a later publication finalization error needs review. The output was retained.') from failure
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
            # LRC permits several leading timestamp tags for the same lyric.
            # Place an invisible literal-text boundary before *all* of the
            # original text, including its whitespace. Removing this marker
            # before LRC parsing would restore the ambiguous timestamp syntax.
            if text.lstrip().startswith('['):
                text = '\u2060' + text
            result.append(stamp + text)
    return '\n'.join(result) + ('\n' if result else '')
