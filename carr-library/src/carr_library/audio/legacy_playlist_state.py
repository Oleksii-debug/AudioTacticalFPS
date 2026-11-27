"""Exact passive canonical queue data, without another playback runtime.

Only published carr-playback-queue/1 fields are interpreted. Device identities,
controls and saved coordinates remain historical data until explicit migration.
"""
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import wave

from ..backup_files import optional_present
from ..ingest.bundle import _unique_object
from .jobs import file_identity, regular_file, verified_reader

NAME = 'state/speech/playback-queue.json'
SCHEMA = 'carr-playback-queue/1'
ASSET = re.compile(r'state/studio/inputs/([0-9a-f]{64})\.audio\Z')
MAX_ITEMS = 256
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_STATE_BYTES = 2 * 1024**2
PROVENANCE_NAME = 'legacy-playback-queue.json'
PROVENANCE_SCHEMA = 'carr-imported-playback-queue/1'
MAX_PROVENANCE_BYTES = 16 * 1024**2
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[0-9a-f]{32}\Z')
_ITEM = {'filename', 'label', 'sha256', 'size_bytes', 'frames',
         'sample_rate', 'channels', 'sample_width'}


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Historical playback queue verification cancelled.')


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum
            or (not empty and not value.strip())
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF
                   for c in value)):
        raise ValueError('Invalid historical playback queue text.')
    return value


def _number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError('Invalid historical playback setting.')
    return value


def validate_queue(value):
    """Pure bounded legacy schema check; never enumerate or open a device."""
    fields = {'schema', 'items', 'index', 'frame', 'volume', 'muted',
              'balance', 'skips', 'output'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or type(value['items']) is not list or len(value['items']) > MAX_ITEMS
            or type(value['skips']) is not list or len(value['skips']) != 2):
        raise ValueError('Unsupported historical playback queue.')
    total, rows, identities = 0, [], {}
    for row in value['items']:
        if type(row) is not dict or set(row) != _ITEM:
            raise ValueError('Invalid historical playback queue item.')
        match = ASSET.fullmatch(row['filename']) if type(row['filename']) is str else None
        if (match is None or type(row['sha256']) is not str or row['sha256'] != match[1]
                or type(row['size_bytes']) is not int or not 44 <= row['size_bytes'] <= MAX_FILE_BYTES
                or type(row['frames']) is not int or not 1 <= row['frames'] <= MAX_FILE_BYTES
                or type(row['sample_rate']) is not int or not 1 <= row['sample_rate'] <= 2**32 - 1
                or type(row['channels']) is not int or row['channels'] not in (1, 2)
                or type(row['sample_width']) is not int or row['sample_width'] not in (1, 2, 3, 4)
                or row['frames'] * row['channels'] * row['sample_width'] > row['size_bytes'] - 44):
            raise ValueError('Invalid historical PCM identity or byte allowance.')
        _text(row['label'], 256)
        identity = tuple(row[key] for key in sorted(_ITEM - {'label'}))
        previous = identities.setdefault(row['filename'], identity)
        if previous != identity:
            raise ValueError('Repeated historical media identities disagree.')
        total += row['size_bytes']
        if total > MAX_TOTAL_BYTES:
            raise ValueError('The historical queue exceeds its 32 GiB allowance.')
        rows.append(dict(row))
    index, frame = value['index'], value['frame']
    if (index is not None and (type(index) is not int or not 0 <= index < len(rows))
            or type(frame) is not int or frame < 0
            or index is None and frame != 0
            or index is not None and frame > rows[index]['frames']):
        raise ValueError('The historical cursor is outside its declared recording.')
    _number(value['volume'], 0, 100)
    _number(value['balance'], -1, 1)
    if type(value['muted']) is not bool:
        raise ValueError('Invalid historical mute setting.')
    for amount in value['skips']:
        _number(amount, .1, 600)
    output = value['output']
    if output is not None:
        if type(output) is not dict or set(output) != {
                'index', 'name', 'interface', 'manufacturer', 'product', 'driver', 'channels'}:
            raise ValueError('Invalid historical output declaration.')
        for key, high in (('index', 127), ('manufacturer', 65535), ('product', 65535),
                          ('driver', 2**32 - 1), ('channels', 65535)):
            if type(output[key]) is not int or not 0 <= output[key] <= high:
                raise ValueError('Invalid historical output capability.')
        _text(output['name'], 32)
        _text(output['interface'], 32768)
        output = dict(output)
    return dict(schema=SCHEMA, items=rows, index=index, frame=frame,
                volume=value['volume'], muted=value['muted'], balance=value['balance'],
                skips=list(value['skips']), output=output)


def _decode(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError('Historical queue metadata exceeds its byte allowance.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite historical metadata.')))
    except (UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError('Invalid historical queue JSON.') from error


def decode_queue(raw):
    return validate_queue(_decode(raw, MAX_STATE_BYTES))


def read_bytes(path, maximum, *, cancelled=lambda: False):
    _cancel(cancelled)
    before = regular_file(path)
    if before.st_nlink != 1 or not 0 < before.st_size <= maximum:
        raise ValueError('Historical metadata must be a bounded independent regular file.')
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        raw = stream.read(maximum + 1)
    _cancel(cancelled)
    if len(raw) != before.st_size:
        raise ValueError('Historical metadata changed while it was read.')
    return raw


def native_identity(path):
    """A cheap current handle pin includes Windows ChangeTime, not creation time."""
    from ..native_file_metadata import stream_change_time
    before = regular_file(path)
    if before.st_nlink != 1:
        raise ValueError('Historical queue files cannot be aliases.')
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        change_time = stream_change_time(stream)
    return file_identity(before) + (change_time,)


def verify_pcm(path, item, *, cancelled=lambda: False):
    """Stream the complete original, accepting only the donor's actual PCM widths."""
    _cancel(cancelled)
    before = regular_file(path)
    if before.st_nlink != 1 or before.st_size != item['size_bytes']:
        raise ValueError('A historical queue recording is missing, aliased or changed.')
    digest, count = sha256(), 0
    from ..native_file_metadata import stream_change_time
    with verified_reader(path, expected_identity=file_identity(before)) as stream:
        change_time = stream_change_time(stream)
        try:
            with wave.open(stream, 'rb') as audio:
                dimensions = (audio.getnframes(), audio.getframerate(),
                              audio.getnchannels(), audio.getsampwidth())
                if audio.getcomptype() != 'NONE':
                    raise ValueError('Historical queue inputs must contain ordinary PCM.')
                start = stream.tell()
        except (wave.Error, EOFError) as error:
            raise ValueError('The historical queue input is not complete PCM WAV.') from error
        expected = tuple(item[key] for key in ('frames', 'sample_rate', 'channels', 'sample_width'))
        if (dimensions != expected or dimensions[0] * dimensions[2] * dimensions[3]
                > before.st_size - start):
            raise ValueError('Historical PCM dimensions differ from the saved queue.')
        stream.seek(0)
        while count < before.st_size:
            _cancel(cancelled)
            block = stream.read(min(1024**2, before.st_size - count))
            if not block:
                raise ValueError('A historical recording was truncated during verification.')
            count += len(block)
            digest.update(block)
        if stream.read(1) or os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('A historical recording changed during verification.')
        _cancel(cancelled)
    if digest.hexdigest() != item['sha256']:
        raise ValueError('A historical queue recording failed its complete checksum.')
    pin = file_identity(before) + (change_time,)
    if native_identity(path) != pin:
        raise ValueError('A historical recording changed before verification was acknowledged.')
    # The first immutable reader has now closed. Timestamps are not content
    # identities: a rewrite with restored mtime can fall within one filesystem
    # timestamp quantum. Reopen under the same immutable ownership contract and
    # re-attest the complete bounded bytes before acknowledging this snapshot.
    rechecked, remaining = sha256(), before.st_size
    try:
        with verified_reader(path, expected_identity=file_identity(before)) as stream:
            if stream_change_time(stream) != change_time:
                raise ValueError('A historical recording changed before verification was acknowledged.')
            while remaining:
                _cancel(cancelled)
                block = stream.read(min(1024**2, remaining))
                if not block:
                    raise ValueError('A historical recording changed before verification was acknowledged.')
                remaining -= len(block)
                rechecked.update(block)
            if stream.read(1):
                raise ValueError('A historical recording changed before verification was acknowledged.')
            _cancel(cancelled)
            if rechecked.hexdigest() != item['sha256']:
                raise ValueError('A historical recording changed before verification was acknowledged.')
    except RuntimeError as error:
        raise ValueError('A historical recording changed before verification was acknowledged.') from error
    if native_identity(path) != pin:
        raise ValueError('A historical recording changed before verification was acknowledged.')
    return pin


def validate_historical(entries, resolve, *, cancelled=lambda: False):
    """Bind a declared queue to already-scoped archive members and actual PCM."""
    if type(entries) is not list or len(entries) > 100000:
        raise ValueError('Invalid historical queue inventory.')
    inventory = {row['filename']: row for row in entries}
    if len(inventory) != len(entries):
        raise ValueError('Repeated historical inventory members.')
    declaration = inventory.get(NAME)
    if declaration is None:
        return frozenset()
    if not 0 < declaration['size_bytes'] <= MAX_STATE_BYTES:
        raise ValueError('Historical queue metadata exceeds its byte allowance.')
    raw = read_bytes(resolve(NAME), MAX_STATE_BYTES, cancelled=cancelled)
    if len(raw) != declaration['size_bytes'] or sha256(raw).hexdigest() != declaration['sha256']:
        raise ValueError('The historical queue differs from its manifest.')
    queue = decode_queue(raw)
    selected = {}
    for item in queue['items']:
        entry = inventory.get(item['filename'])
        if entry is None or (entry['size_bytes'], entry['sha256']) != (
                item['size_bytes'], item['sha256']):
            raise ValueError('The historical queue references missing or changed retained media.')
        selected[item['filename']] = item
    for name, item in selected.items():
        verify_pcm(resolve(name), item, cancelled=cancelled)
    return frozenset(selected)


def validate_origin(value):
    if type(value) is not dict or value.get('kind') not in ('historical-backup', 'selected-root'):
        raise ValueError('Invalid historical queue provenance.')
    if value['kind'] == 'historical-backup':
        if (set(value) != {'kind', 'manifest_sha256', 'queue_member'}
                or type(value['manifest_sha256']) is not str or not _HASH.fullmatch(value['manifest_sha256'])
                or value['queue_member'] != NAME):
            raise ValueError('Invalid historical queue archive identity.')
    else:
        if set(value) != {'kind', 'original_root', 'queue_member'} or value['queue_member'] != NAME:
            raise ValueError('Invalid selected queue provenance.')
        path = value['original_root']
        if type(path) is not str or not 0 < len(path) <= 32768 or '\x00' in path:
            raise ValueError('Invalid inert original queue location.')
        path.encode('utf-8', 'strict')
    return dict(value)


def provenance(raw, origin, project_id):
    decode_queue(raw)
    if type(project_id) is not str or not _ID.fullmatch(project_id):
        raise ValueError('Invalid current playback project identity.')
    return dict(schema=PROVENANCE_SCHEMA, project_id=project_id,
                queue_utf8=raw.decode('utf-8'), queue_sha256=sha256(raw).hexdigest(),
                origin=validate_origin(origin))


def validate_provenance(value, project_id):
    if (type(value) is not dict or set(value) != {
            'schema', 'project_id', 'queue_utf8', 'queue_sha256', 'origin'}
            or value['schema'] != PROVENANCE_SCHEMA or value['project_id'] != project_id
            or type(project_id) is not str or not _ID.fullmatch(project_id)
            or type(value['queue_utf8']) is not str or len(value['queue_utf8']) > MAX_STATE_BYTES):
        raise ValueError('Invalid imported playback queue provenance.')
    raw = value['queue_utf8'].encode('utf-8', 'strict')
    if sha256(raw).hexdigest() != value['queue_sha256']:
        raise ValueError('The original queue differs from its retained provenance.')
    validate_origin(value['origin'])
    return decode_queue(raw)


def provenance_assets(audio, project_id, *, cancelled=lambda: False):
    """Retain original recordings even after later edits remove them from the queue."""
    path = Path(audio) / PROVENANCE_NAME
    if not optional_present(path):
        return {}
    value = _decode(read_bytes(path, MAX_PROVENANCE_BYTES, cancelled=cancelled),
                    MAX_PROVENANCE_BYTES)
    queue = validate_provenance(value, project_id)
    result = {PROVENANCE_NAME: path}
    for item in queue['items']:
        name = 'assets/' + item['sha256'] + '.wav'
        if name in result:
            continue
        verify_pcm(Path(audio) / name, item, cancelled=cancelled)
        result[name] = Path(audio) / name
    return result
