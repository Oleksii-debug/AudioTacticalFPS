"""Reviewed local tags and cover art through the existing FFmpeg runtime.

This profile copies one encoded audio stream into the same container family.
It does not modify a Studio project, its completed output, or its fingerprint.
FFmpeg's streamhash, explicit stream mapping and attached_pic contracts:
https://ffmpeg.org/ffmpeg.html
https://ffmpeg.org/ffmpeg-formats.html#streamhash
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil

from .models import AudioCancelled, AudioError, AudioInfo
from .process import publish_file, run_process
from .audiobook import _owned_publication
from .cleanup import AudioCleanupPending, _pending_owners, retained_temporary_directory
from ..durable_state import encode, local_path, regular
from ..research.export_file import _filename


TAG_FIELDS = ('title', 'artist', 'album', 'album_artist', 'composer', 'genre',
              'date', 'track', 'disc', 'comment', 'copyright')
FORMATS = {'.mp3': ('mp3', 'mp3'), '.flac': ('flac', 'flac'),
           '.m4a': ('mov', 'ipod'), '.m4b': ('mov', 'ipod')}
MAX_COVER_BYTES = 8 * 1024**2
MAX_COVER_PIXELS = 16_000_000
MAX_METADATA_BYTES = 256 * 1024
_SYSTEM_TAGS = {'encoder', 'major_brand', 'minor_version', 'compatible_brands'}
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _check(cancelled):
    if cancelled():
        raise AudioCancelled('Audio metadata work cancelled; incomplete exports are discarded.')


def _text(value, name, maximum=4096):
    if (type(value) is not str or len(value) > maximum
            or any(ord(char) < 32 and char not in '\r\n\t' for char in value)):
        raise ValueError(f'Use bounded text without control characters for {name}.')
    value.encode('utf-8', 'strict')
    return value


@dataclass(frozen=True)
class MetadataChanges:
    """Only listed keys change; an empty value explicitly removes that tag."""
    tags: tuple[tuple[str, str], ...] = ()
    cover_mode: str = 'keep'
    cover_path: str | None = None

    def __post_init__(self):
        if not isinstance(self.tags, (tuple, list, dict)):
            raise ValueError('Choose explicit portable metadata fields.')
        rows = tuple(self.tags.items()) if isinstance(self.tags, dict) else tuple(self.tags)
        if len(rows) > len(TAG_FIELDS):
            raise ValueError('Too many portable metadata fields.')
        found = set()
        for row in rows:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ValueError('Each metadata edit needs a field and text value.')
            name, value = row
            if name not in TAG_FIELDS or name in found:
                raise ValueError('Unknown or duplicate portable metadata field.')
            _text(value, name)
            if name in ('track', 'disc') and value and not re.fullmatch(r'[1-9][0-9]{0,3}(?:/[1-9][0-9]{0,3})?', value):
                raise ValueError('Track and disc use a positive number, optionally followed by /total.')
            if name in ('track', 'disc') and '/' in value:
                first, total = map(int, value.split('/'))
                if first > total:
                    raise ValueError('A track or disc number cannot exceed its total.')
            found.add(name)
        if self.cover_mode not in ('keep', 'replace', 'remove'):
            raise ValueError('Choose keep, replace or remove for the cover.')
        if (self.cover_mode == 'replace') != (self.cover_path is not None):
            raise ValueError('Choose a local PNG or JPEG only when replacing the cover.')
        if self.cover_path is not None and (not isinstance(self.cover_path, (str, os.PathLike))
                                           or not os.fspath(self.cover_path)):
            raise ValueError('Choose an explicit local cover file.')
        object.__setattr__(self, 'tags', tuple(sorted(tuple(row) for row in rows)))
        if self.cover_path is not None:
            object.__setattr__(self, 'cover_path', os.fspath(self.cover_path))


@dataclass(frozen=True)
class BoundFile:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class CoverInfo:
    codec: str
    width: int
    height: int
    sha256: str
    size: int


@dataclass(frozen=True)
class Chapter:
    start: Fraction
    end: Fraction
    tags: tuple[tuple[str, str], ...]

    def to_dict(self):
        return {'start': str(self.start), 'end': str(self.end), 'tags': dict(self.tags)}


@dataclass(frozen=True)
class MetadataSnapshot:
    source: BoundFile
    audio: AudioInfo
    tags: tuple[tuple[str, str], ...]
    chapters: tuple[Chapter, ...]
    cover: CoverInfo | None


@dataclass(frozen=True)
class MetadataPlan:
    snapshot: MetadataSnapshot
    output: Path
    parent_identity: tuple[int, int]
    changes: MetadataChanges
    cover_source: BoundFile | None
    replacement_cover: CoverInfo | None


@dataclass(frozen=True)
class MetadataResult:
    audio: AudioInfo
    receipt: dict
    receipt_path: Path | None
    warning: str = ''


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def _input_stream(path):
    """Open the selected regular inode without following a substituted FIFO/link."""
    before = regular(path)
    flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
             | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOINHERIT', 0))
    descriptor = os.open(path, flags)
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise AudioError('A selected input changed while opening it.')
        yield stream


def _file(runtime, path, maximum, cancelled, *, expected=None, copy_to=None):
    path = runtime._input(path)
    before = regular(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('The selected audio or cover exceeds its byte allowance.')
    digest, count = sha256(), 0
    output = None
    try:
        with _input_stream(path) as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise AudioError('A selected input changed while opening it.')
            if copy_to is not None:
                output = Path(copy_to).open('xb')
            while block := stream.read(min(64 * 1024, before.st_size - count + 1)):
                _check(cancelled)
                count += len(block)
                if count > maximum or count > before.st_size:
                    raise AudioError('A selected input grew during review.')
                digest.update(block)
                if output is not None:
                    output.write(block)
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise AudioError('A selected input changed during review.')
        if output is not None:
            output.flush(); os.fsync(output.fileno())
    finally:
        if output is not None:
            output.close()
    if count != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise AudioError('A selected input changed during review.')
    value = BoundFile(path, digest.hexdigest(), count)
    if expected is not None and value != expected:
        raise AudioError('The selected audio or cover changed after review. Review it again.')
    return value


def _json(runtime, args, cancelled):
    result = run_process([runtime.ffprobe, '-v', 'error', '-max_alloc', str(runtime.budget.max_alloc_bytes),
                          *args, '-of', 'json'],
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)), cancelled=cancelled, owned_tree=True)
    try:
        def reject(value): raise ValueError('Non-finite inspection value.')
        value = json.loads(result.stdout.decode('utf-8'), parse_constant=reject)
        if type(value) is not dict:
            raise ValueError('Missing inspection object.')
        return value
    except (ValueError, UnicodeError) as exc:
        raise AudioError('FFprobe did not return a complete bounded metadata inspection.') from exc


def _tags(value):
    if type(value) is not dict or len(value) > 128:
        raise AudioError('The source has too many metadata fields for this profile.')
    rows = {}
    for key, content in value.items():
        _text(key, 'metadata field', 128); _text(content, 'metadata value', 65536)
        key = key.casefold()
        if key in rows:
            raise AudioError('The source has ambiguous duplicate metadata fields.')
        if key not in _SYSTEM_TAGS:
            rows[key] = content
    if len(encode(rows)) > MAX_METADATA_BYTES:
        raise AudioError('The source metadata exceeds its byte allowance.')
    return tuple(sorted(rows.items()))


def _picture(codec, width, height, raw):
    if (codec not in ('png', 'mjpeg') or type(width) is not int or type(height) is not int
            or not 1 <= width <= 8192 or not 1 <= height <= 8192
            or width * height > MAX_COVER_PIXELS or not 0 < len(raw) <= MAX_COVER_BYTES):
        raise ValueError('Use one PNG/JPEG cover within 8 MiB, 8192 pixels per side and 16 million pixels.')
    signature = b'\x89PNG\r\n\x1a\n' if codec == 'png' else b'\xff\xd8\xff'
    if not raw.startswith(signature):
        raise AudioError('Cover bytes do not match the inspected image format.')
    if codec == 'png':
        # Forced png_pipe deliberately ignores APNG animation chunks. Inspect
        # their actual bounded chunk headers rather than mistaking that first
        # decoded frame for a still-image proof, or matching payload text.
        offset = 8
        while offset + 12 <= len(raw):
            length = int.from_bytes(raw[offset:offset + 4], 'big')
            kind = raw[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(raw):
                raise AudioError('The PNG cover has an incomplete chunk.')
            if kind in (b'acTL', b'fcTL', b'fdAT'):
                raise ValueError('Animated PNG covers are not supported. Select one still PNG or JPEG.')
            if kind == b'IEND':
                if length != 0 or end != len(raw):
                    raise AudioError('Use one complete PNG cover without trailing image data.')
                break
            offset = end
        else:
            raise AudioError('The PNG cover has no complete end marker.')
    return CoverInfo(codec, width, height, sha256(raw).hexdigest(), len(raw))


def _cover(runtime, path, cancelled):
    bound = _file(runtime, path, MAX_COVER_BYTES, cancelled)
    with _input_stream(bound.path) as stream:
        signature = stream.read(8)
    demuxer = 'png_pipe' if signature == b'\x89PNG\r\n\x1a\n' else 'jpeg_pipe' if signature.startswith(b'\xff\xd8\xff') else None
    if demuxer is None:
        raise ValueError('Choose a PNG or JPEG cover; other image formats are not admitted.')
    value = _json(runtime, ['-protocol_whitelist', 'file', '-f', demuxer, '-count_packets',
        '-show_entries', 'stream=codec_name,width,height,nb_read_packets', str(bound.path)], cancelled)
    try:
        row, = value['streams']
        if row.get('nb_read_packets') != '1':
            raise ValueError('Use one still image, not an image sequence.')
        with _input_stream(bound.path) as stream:
            raw = stream.read(MAX_COVER_BYTES + 1)
        picture = _picture(row['codec_name'], row['width'], row['height'], raw)
    except (KeyError, TypeError) as exc:
        raise AudioError('The selected cover has no complete PNG/JPEG image.') from exc
    _file(runtime, bound.path, MAX_COVER_BYTES, cancelled, expected=bound)
    if (picture.sha256, picture.size) != (bound.sha256, bound.size):
        raise AudioError('The cover changed during inspection.')
    # A header alone is not proof of a complete image. Decode the one bounded
    # still frame in the existing owned worker, with errors made fatal.
    run_process([*runtime._base_args(), '-xerror', '-err_detect', 'explode',
        '-protocol_whitelist', 'file', '-f', demuxer, '-i', str(bound.path),
        '-map', '0:v:0', '-frames:v', '1', '-c:v', 'rawvideo', '-threads:v', '1', '-f', 'null', '-'],
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)), cancelled=cancelled, owned_tree=True)
    _file(runtime, bound.path, MAX_COVER_BYTES, cancelled, expected=bound)
    return bound, picture


def inspect_metadata(runtime, source, *, cancelled=lambda: False):
    """Read one admitted local audio file on the caller's worker thread."""
    _check(cancelled)
    source = runtime._input(source)
    if source.suffix.casefold() not in FORMATS:
        raise ValueError('This metadata profile supports MP3, FLAC and M4A/M4B only.')
    bound = _file(runtime, source, runtime.budget.max_output_bytes, cancelled)
    audio = runtime.inspect(source, cancelled=cancelled)
    family, _ = FORMATS[source.suffix.casefold()]
    if family not in audio.format.split(','):
        raise ValueError('The actual audio container does not match its selected filename.')
    value = _json(runtime, ['-protocol_whitelist', 'file', '-format_whitelist', 'mp3,flac,mov',
        *(['-enable_drefs', '0', '-use_absolute_path', '0'] if family == 'mov' else []), '-show_chapters',
        '-show_entries', 'stream=index,codec_type,codec_name,sample_rate,channels,width,height:stream_disposition=attached_pic:format_tags',
        str(source)], cancelled)
    streams, chapter_rows = value.get('streams'), value.get('chapters')
    if (type(streams) is not list or not 1 <= len(streams) <= 4
            or type(chapter_rows) is not list or len(chapter_rows) > 10000):
        raise AudioError('Unsupported source stream or chapter inventory.')
    voices = [row for row in streams if row.get('codec_type') == 'audio']
    pictures = [row for row in streams if row.get('codec_type') == 'video']
    data = [row for row in streams if row.get('codec_type') == 'data']
    if (len(voices) != 1 or len(pictures) > 1 or len(data) > int(bool(chapter_rows) and family == 'mov')
            or len(voices) + len(pictures) + len(data) != len(streams)
            or any(row.get('codec_name') != 'bin_data' for row in data)
            or any(row.get('disposition', {}).get('attached_pic') != 1 for row in pictures)):
        raise ValueError('Use one audio stream and at most one PNG/JPEG cover; video or extra tracks are not silently dropped.')
    voice, = voices
    if (voice.get('codec_name') != audio.codec or int(voice.get('sample_rate', 0)) != audio.sample_rate
            or voice.get('channels') != audio.channels):
        raise AudioError('The source audio properties changed during inspection.')
    chapters = []
    previous = Fraction(0)
    for row in chapter_rows:
        try:
            base = Fraction(row['time_base'])
            start, end = int(row['start']) * base, int(row['end']) * base
            if not previous <= start < end <= Fraction(str(audio.duration)) + Fraction(15, 100):
                raise ValueError('Invalid or overlapping chapter interval.')
            chapters.append(Chapter(start, end, _tags(row.get('tags', {}))))
            previous = end
        except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise AudioError('The source has an invalid bounded chapter timeline.') from exc
    cover = None
    if pictures:
        row, = pictures
        # Stream copy returns the exact compressed image, not a re-encoded thumbnail.
        result = run_process([*runtime._base_args(), *runtime._input_args(source, cancelled=cancelled),
            '-map', '0:v:0', '-c:v', 'copy', '-frames:v', '1', '-f', 'image2pipe', 'pipe:1'],
            budget=replace(runtime.budget, max_log_bytes=MAX_COVER_BYTES + 1,
                           timeout=min(30, runtime.budget.timeout)), cancelled=cancelled, owned_tree=True)
        cover = _picture(row.get('codec_name'), row.get('width'), row.get('height'), result.stdout)
    tags = _tags(value.get('format', {}).get('tags', {}))
    _file(runtime, source, runtime.budget.max_output_bytes, cancelled, expected=bound)
    return MetadataSnapshot(bound, audio, tags, tuple(chapters), cover)


def _parent(plan, runtime):
    parent = local_path(plan.output.parent, exists=True)
    info = regular(parent, directory=True)
    if (parent != plan.output.parent or not parent.is_relative_to(runtime.root)
            or (info.st_dev, info.st_ino) != plan.parent_identity):
        raise AudioError('The approved metadata output directory changed. Review the destination again.')


def review_metadata(runtime, source, output, changes, *, cancelled=lambda: False):
    if not isinstance(changes, MetadataChanges):
        raise ValueError('Review typed metadata and cover choices first.')
    snapshot = inspect_metadata(runtime, source, cancelled=cancelled)
    target = runtime._output(output)
    _filename(target.name)
    if len(target.name.encode('utf-8')) > 220 or len(target.name.encode('utf-16-le')) > 440:
        raise ValueError('Use an output basename of at most 220 encoded bytes for its adjacent receipt.')
    if target.suffix.casefold() not in FORMATS or FORMATS[target.suffix.casefold()][0] != FORMATS[snapshot.source.path.suffix.casefold()][0]:
        raise ValueError('Metadata editing copies audio within its existing MP3, FLAC or M4A/M4B container family.')
    if os.path.lexists(target.with_name(target.name + '.metadata.json')):
        raise FileExistsError('The metadata receipt already exists. Choose a new output filename.')
    cover_source = picture = None
    if changes.cover_mode == 'replace':
        cover_source, picture = _cover(runtime, changes.cover_path, cancelled)
    info = regular(target.parent, directory=True)
    plan = MetadataPlan(snapshot, target, (info.st_dev, info.st_ino), changes, cover_source, picture)
    _parent(plan, runtime)
    _check(cancelled)
    return plan


def _parse_stream_hash(raw):
    """Accept only one mapped audio packet digest in supported FFmpeg formats.

    FFmpeg 9.1 emits the version-2 stream-checksum header. Both layouts attest
    the same encoded packet bytes because this caller explicitly uses -c:a copy.
    Extradata declarations are checked as syntax but are not substituted for
    the packet hash stored in the existing audio_packet_sha256 receipt field.
    """
    error = 'FFmpeg did not attest the encoded audio stream.'
    if type(raw) is not bytes or not 0 < len(raw) <= 65536:
        raise AudioError(error)
    try:
        value = raw.decode('ascii', 'strict').strip()
    except UnicodeDecodeError:
        raise AudioError(error) from None
    prefix = '0,a,SHA256='
    if value.startswith(prefix) and _DIGEST.fullmatch(value[len(prefix):]):
        return value[len(prefix):]
    lines = value.splitlines()
    if (not 8 <= len(lines) <= 16 or any(not line or len(line) > 1024 for line in lines)
            or lines[:3] != ['#format: stream checksums', '#version: 2', '#hash: SHA256']
            or lines[-2] != '#stream#, size, hash'):
        raise AudioError(error)
    patterns = {
        'extradata': r'#extradata 0, *[0-9]{1,10}, [0-9a-f]{64}',
        'software': r'#software: Lavf[0-9]+\.[0-9]+\.[0-9]+',
        'timebase': r'#tb 0: [1-9][0-9]{0,9}/[1-9][0-9]{0,9}',
        'media': r'#media_type 0: audio',
        'codec': r'#codec_id 0: [a-z0-9_]{1,80}',
        'rate': r'#sample_rate 0: [1-9][0-9]{0,8}',
        'layout': r'#channel_layout_name 0: [ -~]{1,256}',
    }
    observed = set()
    for line in lines[3:-2]:
        matches = [name for name, pattern in patterns.items() if re.fullmatch(pattern, line)]
        if len(matches) != 1 or matches[0] in observed:
            raise AudioError(error)
        observed.add(matches[0])
    if not {'software', 'timebase', 'media', 'codec', 'rate', 'layout'} <= observed:
        raise AudioError(error)
    result = re.fullmatch(r'0, *[1-9][0-9]{0,19}, ([0-9a-f]{64})', lines[-1])
    if result is None:
        raise AudioError(error)
    return result[1]


def _stream_hash(runtime, path, cancelled):
    result = run_process([*runtime._base_args(), *runtime._input_args(path, cancelled=cancelled), '-map', '0:a:0',
        '-c:a', 'copy', '-f', 'streamhash', '-hash', 'sha256', 'pipe:1'],
        budget=runtime.budget, cancelled=cancelled, owned_tree=True)
    return _parse_stream_hash(result.stdout)


def _warning(completed, message):
    result = completed['result']
    completed['result'] = replace(result, warning=(result.warning + ' ' + message).strip())


@contextmanager
def _metadata_staging(parent, completed):
    try:
        with retained_temporary_directory(prefix='.audio-metadata-', dir=parent) as temporary:
            yield Path(temporary)
    except AudioCleanupPending:
        # The existing cleanup owner retains both the directory and the caller's
        # admission. It must confirm worker shutdown before either is released.
        raise
    except (Exception, KeyboardInterrupt) as exc:
        if completed['result'] is None or _pending_owners(exc):
            raise
        _warning(completed, 'Audio was saved and verified. Final staging cleanup needs '
                 'attention: ' + type(exc).__name__ + ': ' + str(exc)[:1000])


def _linked_file(staged, target, owner, parent, checksum, maximum):
    # Reuse the existing descriptor-bound two-link proof, without another
    # publication writer or a byte-equivalent foreign-output adoption rule.
    try:
        _owned_publication(staged, target, owner, parent, checksum, maximum)
    except (OSError, ValueError, AudioError) as exc:
        raise AudioError('The metadata file is not this attempt\'s exact verified publication.') from exc


def _verify_completed(runtime, plan, staged, owner, checksum, size):
    _parent(plan, runtime)
    if plan.output.lstat().st_nlink == 2:
        parent = regular(plan.output.parent, directory=True)
        _linked_file(staged, plan.output, owner, parent, checksum, runtime.budget.max_output_bytes)
    else:
        observed = regular(plan.output)
        if (observed.st_dev, observed.st_ino) != (owner.st_dev, owner.st_ino):
            raise AudioError('The saved metadata output was replaced during final cleanup.')
        _file(runtime, plan.output, runtime.budget.max_output_bytes, lambda: False,
              expected=BoundFile(plan.output, checksum, size))
    _parent(plan, runtime)


def metadata_disk_bytes(plan):
    """Declared peak scratch estimate for the existing shared resource broker."""
    if not isinstance(plan, MetadataPlan):
        raise ValueError('Review the audio metadata plan before reserving scratch space.')
    cover = (plan.snapshot.cover if plan.changes.cover_mode == 'keep'
             else plan.replacement_cover if plan.changes.cover_mode == 'replace' else None)
    return plan.snapshot.source.size * 2 + 3 * (cover.size if cover else 0) + MAX_METADATA_BYTES


def publish_metadata(runtime, plan, *, cancelled=lambda: False, progress=None):
    """Verify the approved bytes and publish a new audio file without encoding."""
    if not isinstance(plan, MetadataPlan):
        raise ValueError('Review the audio metadata plan before saving.')
    _check(cancelled); _parent(plan, runtime)
    target = runtime._output(plan.output)
    receipt_path = target.with_name(target.name + '.metadata.json')
    if os.path.lexists(receipt_path):
        raise FileExistsError('The metadata receipt already exists. Choose a new filename.')
    expected_tags = dict(plan.snapshot.tags)
    for key, value in plan.changes.tags:
        if value:
            expected_tags[key] = value
        else:
            expected_tags.pop(key, None)
    expected_cover = (plan.snapshot.cover if plan.changes.cover_mode == 'keep'
                      else plan.replacement_cover if plan.changes.cover_mode == 'replace' else None)
    estimate = metadata_disk_bytes(plan)
    if (estimate > runtime.budget.max_output_bytes
            or shutil.disk_usage(target.parent).free < estimate + runtime.budget.reserve_disk_bytes):
        raise AudioError('Not enough approved output budget or free disk for the source snapshot and tagged copy.')

    def stopping():
        _parent(plan, runtime)
        return bool(cancelled())

    completed = {'result': None}
    with _metadata_staging(target.parent, completed) as staging:
        source = staging / ('source' + plan.snapshot.source.path.suffix)
        _file(runtime, plan.snapshot.source.path, runtime.budget.max_output_bytes, stopping,
              expected=plan.snapshot.source, copy_to=source)
        cover_path = None
        if plan.cover_source is not None:
            cover_path = staging / ('cover.png' if expected_cover.codec == 'png' else 'cover.jpg')
            _file(runtime, plan.cover_source.path, MAX_COVER_BYTES, stopping,
                  expected=plan.cover_source, copy_to=cover_path)
        staged = staging / ('tagged' + target.suffix)
        command = [*runtime._base_args(), *runtime._input_args(source, cancelled=stopping)]
        if cover_path is not None:
            demuxer = 'png_pipe' if expected_cover.codec == 'png' else 'jpeg_pipe'
            command += ['-protocol_whitelist', 'file', '-f', demuxer, '-i', str(cover_path)]
        command += ['-map', '0:a:0']
        if expected_cover is not None:
            command += ['-map', '1:v:0' if cover_path is not None else '0:v:0']
        command += ['-map_metadata', '0', '-map_chapters', '0', '-c', 'copy']
        if expected_cover is not None:
            command += ['-disposition:v:0', 'attached_pic']
            if cover_path is not None:
                command += ['-metadata:s:v:0', 'title=Cover', '-metadata:s:v:0', 'comment=Cover (front)']
        for key, value in plan.changes.tags:
            command += ['-metadata', key + '=' + value]
        if target.suffix.casefold() == '.mp3':
            command += ['-id3v2_version', '4']
        if progress is not None:
            command += ['-stats_period', '1', '-progress', 'pipe:1']
        command += ['-f', FORMATS[target.suffix.casefold()][1], str(staged)]
        run_process(command, budget=runtime.budget, cancelled=stopping, watch=(staging,), progress=progress, owned_tree=True)
        observed = inspect_metadata(runtime, staged, cancelled=stopping)
        original = plan.snapshot.audio
        if (dict(observed.tags) != expected_tags or observed.cover != expected_cover
                or observed.audio.codec != original.codec or observed.audio.sample_rate != original.sample_rate
                or observed.audio.channels != original.channels
                or abs(observed.audio.duration - original.duration) > .15
                or len(observed.chapters) != len(plan.snapshot.chapters)):
            raise AudioError('The tagged output differs from the reviewed tags, cover or audio properties.')
        for actual, expected in zip(observed.chapters, plan.snapshot.chapters):
            if (actual.tags != expected.tags or abs(actual.start - expected.start) > Fraction(1, 1000)
                    or abs(actual.end - expected.end) > Fraction(1, 1000)):
                raise AudioError('Metadata publication changed an existing measured chapter.')
        packet_hash = _stream_hash(runtime, source, stopping)
        if _stream_hash(runtime, staged, stopping) != packet_hash:
            raise AudioError('Metadata publication changed the encoded audio packets.')
        _file(runtime, plan.snapshot.source.path, runtime.budget.max_output_bytes, stopping, expected=plan.snapshot.source)
        if plan.cover_source is not None:
            _file(runtime, plan.cover_source.path, MAX_COVER_BYTES, stopping, expected=plan.cover_source)
        receipt = {'schema': 'carr-audio-metadata/1',
            'source': {'name': plan.snapshot.source.path.name, 'sha256': plan.snapshot.source.sha256, 'size': plan.snapshot.source.size},
            'output': {'name': target.name, 'sha256': observed.source.sha256, 'size': observed.source.size},
            'tags': expected_tags, 'cover': asdict(expected_cover) if expected_cover else None,
            'chapters': [row.to_dict() for row in observed.chapters],
            'audio_packet_sha256': packet_hash, 'audio_reencoded': False,
            'sample_rate': observed.audio.sample_rate, 'channels': observed.audio.channels,
            'codec': observed.audio.codec, 'cover_rights': 'User-selected input; no automatic rights grant.'}
        raw = encode(receipt)
        if len(raw) > runtime.budget.max_log_bytes:
            raise AudioError('The verified metadata receipt exceeds its byte allowance.')
        staged_receipt = staging / 'receipt.json'
        staged_receipt.write_bytes(raw)
        _parent(plan, runtime)
        owner = regular(staged)
        _file(runtime, staged, runtime.budget.max_output_bytes, stopping, expected=observed.source)
        parent = regular(target.parent, directory=True)
        publication_error = None
        try:
            publish_file(staged, target, cancelled=stopping)
        except FileExistsError:
            raise
        except AudioCancelled as exc:
            if not os.path.lexists(target):
                raise
            publication_error = exc
        except (Exception, KeyboardInterrupt) as exc:
            publication_error = exc
        try:
            _linked_file(staged, target, owner, parent, observed.source.sha256,
                         runtime.budget.max_output_bytes)
        except Exception:
            if publication_error is not None:
                raise publication_error
            raise
        completed['result'] = MetadataResult(
            replace(observed.audio, path=target, sha256=observed.source.sha256), receipt, None)
        if publication_error is not None:
            _warning(completed, 'Audio was saved and verified, but its writer reported a late '
                     'interruption: ' + type(publication_error).__name__ + ': ' + str(publication_error)[:1000])
        # The verified audio is already committed. Finish its bounded receipt
        # despite late cancellation; a receipt failure must report the saved file.
        receipt_owner = regular(staged_receipt)
        receipt_error = None
        try:
            publish_file(staged_receipt, receipt_path, cancelled=lambda: (_parent(plan, runtime), False)[1])
        except (Exception, KeyboardInterrupt) as exc:
            receipt_error = exc
        try:
            _linked_file(staged_receipt, receipt_path, receipt_owner, parent, sha256(raw).hexdigest(),
                         runtime.budget.max_log_bytes)
        except (OSError, ValueError, AudioError):
            _warning(completed, 'Audio was saved, but its adjacent metadata receipt could not '
                     'be verified: ' + str(receipt_error or 'The receipt changed before verification.')[:1000])
        else:
            completed['result'] = replace(completed['result'], receipt_path=receipt_path)
            if receipt_error is not None:
                _warning(completed, 'The adjacent receipt was saved and verified, but its writer '
                         'reported a late interruption: ' + type(receipt_error).__name__ + ': ' + str(receipt_error)[:1000])
    # A late cleanup warning never grants authority to a replacement directory,
    # a foreign output inode, or changed bytes. Do not consult cancellation here.
    _verify_completed(runtime, plan, staged, owner, observed.source.sha256, observed.source.size)
    if completed['result'].receipt_path is not None:
        try:
            if receipt_path.lstat().st_nlink == 2:
                _linked_file(staged_receipt, receipt_path, receipt_owner, parent,
                             sha256(raw).hexdigest(), runtime.budget.max_log_bytes)
            else:
                current = regular(receipt_path)
                if (current.st_dev, current.st_ino) != (receipt_owner.st_dev, receipt_owner.st_ino):
                    raise AudioError('The saved metadata receipt was replaced during cleanup.')
                _file(runtime, receipt_path, runtime.budget.max_log_bytes, lambda: False,
                      expected=BoundFile(receipt_path, sha256(raw).hexdigest(), len(raw)))
        except (Exception, KeyboardInterrupt) as exc:
            completed['result'] = replace(completed['result'], receipt_path=None)
            _warning(completed, 'Audio was saved, but its adjacent receipt changed during final '
                     'verification: ' + type(exc).__name__ + ': ' + str(exc)[:1000])
    return completed['result']
