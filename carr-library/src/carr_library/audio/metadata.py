"""Reviewed local tags and cover art through the existing FFmpeg runtime.

This profile copies one encoded audio stream into the same container family.
It does not modify a Studio project, its completed output, or its fingerprint.
FFmpeg's hash muxer, explicit stream mapping and attached_pic contracts:
https://ffmpeg.org/ffmpeg.html
https://ffmpeg.org/ffmpeg-formats.html#hash
"""
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from .models import AudioCancelled, AudioError, AudioInfo
from .process import publish_file, run_process
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


def _file(runtime, path, maximum, cancelled, *, expected=None, copy_to=None):
    path = runtime._input(path)
    before = regular(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('The selected audio or cover exceeds its byte allowance.')
    digest, count = sha256(), 0
    output = None
    try:
        with path.open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise AudioError('A selected input changed while opening it.')
            if copy_to is not None:
                output = Path(copy_to).open('xb')
            while block := stream.read(64 * 1024):
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
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)), cancelled=cancelled)
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
    with bound.path.open('rb') as stream:
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
        with bound.path.open('rb') as stream:
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
        budget=replace(runtime.budget, timeout=min(30, runtime.budget.timeout)), cancelled=cancelled)
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
        '-enable_drefs', '0', '-use_absolute_path', '0', '-show_chapters',
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
        result = run_process([*runtime._base_args(), *runtime._input_args(source),
            '-map', '0:v:0', '-c:v', 'copy', '-frames:v', '1', '-f', 'image2pipe', 'pipe:1'],
            budget=replace(runtime.budget, max_log_bytes=MAX_COVER_BYTES + 1,
                           timeout=min(30, runtime.budget.timeout)), cancelled=cancelled)
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


def _stream_hash(runtime, path, cancelled):
    # Exactly one mapped stream is copied, so hash and legacy streamhash digest
    # the same encoded packet bytes. streamhash defaults changed to a headered
    # version 2 in FFmpeg 9.1; hash retains its single SHA256=... receipt.
    result = run_process([*runtime._base_args(), *runtime._input_args(path), '-map', '0:a:0',
        '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', 'pipe:1'],
        budget=runtime.budget, cancelled=cancelled)
    value = result.stdout.decode('ascii', 'strict').strip()
    prefix = 'SHA256='
    if not value.startswith(prefix) or not _DIGEST.fullmatch(value[len(prefix):]):
        raise AudioError('FFmpeg did not attest the encoded audio stream.')
    return value[len(prefix):]


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
    estimate = plan.snapshot.source.size * 2 + 3 * (expected_cover.size if expected_cover else 0) + MAX_METADATA_BYTES
    if (estimate > runtime.budget.max_output_bytes
            or shutil.disk_usage(target.parent).free < estimate + runtime.budget.reserve_disk_bytes):
        raise AudioError('Not enough approved output budget or free disk for the source snapshot and tagged copy.')

    def stopping():
        _parent(plan, runtime)
        return bool(cancelled())

    with TemporaryDirectory(prefix='.audio-metadata-', dir=target.parent) as temporary:
        staging = Path(temporary)
        source = staging / ('source' + plan.snapshot.source.path.suffix)
        _file(runtime, plan.snapshot.source.path, runtime.budget.max_output_bytes, stopping,
              expected=plan.snapshot.source, copy_to=source)
        cover_path = None
        if plan.cover_source is not None:
            cover_path = staging / ('cover.png' if expected_cover.codec == 'png' else 'cover.jpg')
            _file(runtime, plan.cover_source.path, MAX_COVER_BYTES, stopping,
                  expected=plan.cover_source, copy_to=cover_path)
        staged = staging / ('tagged' + target.suffix)
        command = [*runtime._base_args(), *runtime._input_args(source)]
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
        run_process(command, budget=runtime.budget, cancelled=stopping, watch=(staging,), progress=progress)
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
        publish_file(staged, target, cancelled=stopping)
        # The verified audio is already committed. Finish its bounded receipt
        # despite late cancellation; a receipt failure must report the saved file.
        warning = ''
        saved_receipt = receipt_path
        try:
            publish_file(staged_receipt, receipt_path, cancelled=lambda: (_parent(plan, runtime), False)[1])
        except Exception as exc:
            saved_receipt = None
            warning = f'Audio was saved, but its adjacent metadata receipt could not be saved: {exc}'
        return MetadataResult(replace(observed.audio, path=target, sha256=observed.source.sha256),
                              receipt, saved_receipt, warning)
