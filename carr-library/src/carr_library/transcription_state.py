"""Portable definitions of supplied transcription jobs and retained inputs.

The definition, original provenance and content hashes survive a library move.
Old absolute source locations are inert metadata. A supervisor binds a fresh
execution; backup validation never executes or imports the private runtime.
"""
from datetime import datetime
from pathlib import PurePosixPath
import re
import unicodedata

from .audio.jobs import checked_directory, regular_file
from .audio.model_manifest import _read_json_snapshot, digest_file, local_path
from .audio.paths import filename_component
from .transcription_receipt import validate_settings

DIRECTORY = 'transcription-jobs'
SCHEMA = 'carr-supplied-transcription-job/1'
MAX_INPUTS = 1000
MAX_ENTRIES = 20000
MAX_JOB_BYTES = 32 * 1024**3
MAX_JSON_BYTES = 16 * 1024**2
MAX_LOG_BYTES = 16 * 1024**2
EXTENSIONS = frozenset(('.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.opus', '.flac',
                        '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'))
JOB_ID = re.compile('[0-9a-f]{32}\\Z')
_CONTENT_HASH = re.compile('[0-9a-f]{64}\\Z')


def check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Transcription stopped. Retained inputs and sealed checkpoints remain available.')


def content_hash(value):
    if type(value) is not str or not _CONTENT_HASH.fullmatch(value):
        raise ValueError('Invalid transcription content identity.')
    return value


def relative_path(value):
    if (type(value) is not str or not value or len(value) > 4096 or '\\' in value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Invalid transcription artifact path.')
    result = PurePosixPath(value)
    if result.is_absolute() or len(result.parts) > 12:
        raise ValueError('Invalid transcription artifact path.')
    for part in result.parts:
        filename_component(part)
    return result


def regular(path):
    info = regular_file(local_path(path))
    if info.st_nlink != 1:
        raise ValueError('Transcription files must not have hard-link aliases.')
    return info


def validate_definition(value, *, identity=None):
    keys = {'schema', 'id', 'title', 'created', 'settings', 'runtime_manifest_sha256',
            'model_sha256', 'inputs', 'source_provenance'}
    if (type(value) is not dict or set(value) != keys or value['schema'] != SCHEMA
            or type(value['id']) is not str or not JOB_ID.fullmatch(value['id'])
            or identity is not None and value['id'] != identity):
        raise ValueError('Invalid supplied-transcription job definition.')
    if (type(value['title']) is not str or not value['title'].strip() or len(value['title']) > 200
            or any(ord(c) < 32 for c in value['title'])):
        raise ValueError('Invalid transcription job title.')
    if type(value['created']) is not str or len(value['created']) > 64:
        raise ValueError('Invalid transcription creation timestamp.')
    try:
        if datetime.fromisoformat(value['created']).utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Invalid transcription creation timestamp.') from None
    if validate_settings(value['settings']) != value['settings']:
        raise ValueError('The transcription definition must capture every setting.')
    content_hash(value['runtime_manifest_sha256'])
    content_hash(value['model_sha256'])
    inputs, provenance = value['inputs'], value['source_provenance']
    if (type(inputs) is not list or not 1 <= len(inputs) <= MAX_INPUTS
            or type(provenance) is not list or len(provenance) != len(inputs)):
        raise ValueError('Invalid transcription input inventory.')
    aliases, total = set(), 0
    for row, origin in zip(inputs, provenance):
        if type(row) is not dict or set(row) != {'path', 'root', 'relative', 'bytes', 'sha256'}:
            raise ValueError('Invalid transcription input entry.')
        path, root, relative = relative_path(row['path']), relative_path(row['root']), relative_path(row['relative'])
        if (len(root.parts) != 2 or root.parts[0] != 'inputs'
                or not re.fullmatch('root-[0-9]{1,4}', root.parts[1]) or path != root / relative
                or path.suffix.lower() not in EXTENSIONS):
            raise ValueError('A transcription input is outside its owned source folder.')
        alias = unicodedata.normalize('NFC', str(path)).casefold()
        if alias in aliases:
            raise ValueError('Duplicate transcription input path.')
        aliases.add(alias)
        if type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_JOB_BYTES:
            raise ValueError('Invalid transcription input size.')
        total += row['bytes']
        content_hash(row['sha256'])
        if (type(origin) is not dict or set(origin) != {'path', 'root', 'relative', 'bytes', 'sha256'}
                or origin['bytes'] != row['bytes'] or origin['sha256'] != row['sha256']
                or origin['relative'] != row['relative']):
            raise ValueError('Transcription source provenance does not match the retained input.')
        # The existing reader validates foreign-platform provenance without
        # opening a path. The snapshot only ever reads the owned relative copy.
        from .reader.narration import _original_path
        for name in ('path', 'root'):
            if type(origin[name]) is not str:
                raise ValueError('Invalid original transcription source location.')
            _original_path(origin[name])
    if total > MAX_JOB_BYTES:
        raise ValueError('Transcription inputs exceed the retained byte budget.')
    return value


def read_definition(job, *, cancelled=lambda: False):
    job = checked_directory(job)
    value, checksum = _read_json_snapshot(job / 'job.json', max_bytes=MAX_JSON_BYTES)
    regular(job / 'job.json')
    validate_definition(value, identity=job.name)
    for row in value['inputs']:
        check_cancelled(cancelled)
        path = job.joinpath(*relative_path(row['path']).parts)
        if (regular(path).st_size != row['bytes']
                or digest_file(path, cancelled=cancelled, max_bytes=row['bytes']) != row['sha256']):
            raise ValueError('A retained transcription input failed its content check.')
    if digest_file(job / 'job.json', cancelled=cancelled, max_bytes=MAX_JSON_BYTES) != checksum:
        raise ValueError('The transcription definition changed during verification.')
    return value
