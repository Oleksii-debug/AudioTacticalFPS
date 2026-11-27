"""Inert, portable provenance in a generated PCM WAV, never citation authority.

The direct reader export uses its existing single-file publication boundary.
A bounded trailing RIFF rsrc chunk binds the reviewed reading snapshot and
selection to the actual PCM bytes. No source text or source filesystem path is
embedded. This is an integrity receipt, not a signature or source approval.
"""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct

from ..durable_state import encode
from ._io import check_cancelled, local_file, text_value
from .reading import _validated_source_anchor, MAX_HISTORY

SCHEMA = 'carr-narration-wav/1'
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_WAV_BYTES = 3_999_999_999
MAX_CHARACTERS = 2_000_000
HEADER = struct.Struct('<4sI4s4sIHHIIHH4sI')
CHUNK = struct.Struct('<4sI')
NOTICE = 'A reading derivative; this receipt never authorizes a quotation.'


def _hash(value):
    return sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _fields(value, names):
    return type(value) is dict and set(value) == set(names.split())


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate narration receipt field.')
        result[key] = value
    return result


def validate_narration(value, text=None):
    """Validate inert structure, and the exact rendered selection when supplied."""
    if (not _fields(value, 'source_anchor source_sha256 source_characters '
            'reading_project_sha256 reading_text_sha256 reading_characters '
            'reading_revision selection source_span quotation_authority')
            or value['quotation_authority'] is not False
            or any(not _digest(value[key]) for key in ('source_sha256',
                'reading_project_sha256', 'reading_text_sha256'))
            or not _integer(value['source_characters'], 0, MAX_CHARACTERS)
            or not _integer(value['reading_characters'], 1, MAX_CHARACTERS)
            or not _integer(value['reading_revision'], 0, MAX_HISTORY)):
        raise ValueError('Invalid narration snapshot receipt.')
    if value['source_anchor'] is not None:
        _validated_source_anchor(value['source_anchor'],
                                 source_characters=value['source_characters'])
    selected = value['selection']
    if (not _fields(selected, 'start end text_sha256')
            or not _integer(selected['start'], 0, value['reading_characters'] - 1)
            or not _integer(selected['end'], selected['start'] + 1, value['reading_characters'])
            or not _digest(selected['text_sha256'])):
        raise ValueError('Invalid narration selection receipt.')
    span = value['source_span']
    if not _fields(span, 'start end exact') or type(span['exact']) is not bool:
        raise ValueError('Invalid narration source span.')
    if span['start'] is None or span['end'] is None:
        if span['start'] is not None or span['end'] is not None or span['exact']:
            raise ValueError('Invalid unmapped narration span.')
    elif (not _integer(span['start'], 0, value['source_characters'])
            or not _integer(span['end'], span['start'], value['source_characters'])
            or (span['exact'] and span['end'] - span['start'] != selected['end'] - selected['start'])):
        raise ValueError('Invalid mapped narration span.')
    if text is not None:
        text_value(text, 'Narrated selection')
        if len(text) != selected['end'] - selected['start'] or _hash(text) != selected['text_sha256']:
            raise ValueError('The narrated selection differs from its receipt.')
    raw = encode(value)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('The narration receipt exceeds its metadata budget.')
    return json.loads(raw.decode('utf-8'))


def capture_narration(document, start, end):
    """Capture an exact Unicode selection from the existing reading document."""
    current = document.text
    if not _integer(start, 0, len(current)) or not _integer(end, start, len(current)):
        raise ValueError('Choose a valid narration character range.')
    selected = current[start:end]
    text_value(selected, 'Narrated selection')
    span = document.source_span(start, end)
    return validate_narration({
        'source_anchor': document.source_anchor, 'source_sha256': document.source_sha256,
        'source_characters': len(document.source_text),
        'reading_project_sha256': _hash(encode(document.to_dict())),
        'reading_text_sha256': _hash(current), 'reading_characters': len(current),
        'reading_revision': document.revision,
        'selection': {'start': start, 'end': end, 'text_sha256': _hash(selected)},
        'source_span': {'start': span.start, 'end': span.end, 'exact': span.exact},
        'quotation_authority': False}, selected)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _unchanged(stream, path, before):
    if (_identity(os.fstat(stream.fileno())) != _identity(before)
            or _identity(local_file(path).stat()) != _identity(before)):
        raise ValueError('The narration WAV changed during receipt inspection.')


def _header(stream, size):
    """Admit only the canonical PCM layout emitted by this speech writer."""
    raw = stream.read(HEADER.size)
    if len(raw) != HEADER.size or not HEADER.size < size <= MAX_WAV_BYTES:
        raise ValueError('Unsupported or incomplete generated PCM WAV.')
    riff, length, wave, fmt, fmt_size, pcm, channels, rate, byte_rate, align, bits, data, count = HEADER.unpack(raw)
    width = bits // 8
    if (riff != b'RIFF' or wave != b'WAVE' or fmt != b'fmt ' or fmt_size != 16
            or pcm != 1 or data != b'data' or length != size - 8
            or channels not in (1, 2) or bits not in (8, 16, 24, 32)
            or not 8000 <= rate <= 192000 or align != channels * width
            or byte_rate != rate * align or count <= 0 or count % align
            or HEADER.size + count > size):
        raise ValueError('Unsupported or inconsistent generated PCM WAV.')
    return count, {'frames': count // align, 'sample_rate': rate,
                   'channels': channels, 'sample_width': width}


def _pcm_sha256(stream, count, cancelled):
    stream.seek(HEADER.size)
    h = sha256()
    while count:
        check_cancelled(cancelled)
        block = stream.read(min(count, 1024 * 1024))
        if not block:
            raise ValueError('The narration PCM data is truncated.')
        h.update(block)
        count -= len(block)
    check_cancelled(cancelled)
    return h.hexdigest()


def _validate_record(record):
    if (not _fields(record, 'schema narration voice settings pcm notice')
            or record['schema'] != SCHEMA or record['notice'] != NOTICE):
        raise ValueError('Unsupported narration receipt schema.')
    validate_narration(record['narration'])
    voice = record['voice']
    if (not _fields(voice, 'id_sha256 engine language runtime_sha256')
            or not _digest(voice['id_sha256'])
            or (voice['runtime_sha256'] is not None and not _digest(voice['runtime_sha256']))):
        raise ValueError('Invalid narration voice identity.')
    text_value(voice['engine'], 'Voice engine', maximum=2048)
    text_value(voice['language'], 'Voice language', maximum=2048)
    from .runtime import SpeechSettings
    settings = record['settings']
    if not _fields(settings, 'rate volume pitch punctuation timeout_seconds max_output_bytes'):
        raise ValueError('Invalid narration settings receipt.')
    SpeechSettings(**settings)
    pcm = record['pcm']
    if (not _fields(pcm, 'sha256 frames sample_rate channels sample_width')
            or not _digest(pcm['sha256']) or not _integer(pcm['frames'], 1, MAX_WAV_BYTES)
            or not _integer(pcm['sample_rate'], 8000, 192000)
            or not _integer(pcm['channels'], 1, 2) or not _integer(pcm['sample_width'], 1, 4)):
        raise ValueError('Invalid narration PCM receipt.')


def append_narration_receipt(path, narration, voice, settings, *, pcm_sha256,
                             runtime_sha256=None, cancelled=lambda: False):
    """Attach a single receipt only to the unpublished, freshly generated WAV."""
    check_cancelled(cancelled)
    target = local_file(path)
    with target.open('r+b') as stream:
        before = os.fstat(stream.fileno())
        count, geometry = _header(stream, before.st_size)
        pcm_end = HEADER.size + count
        if before.st_size not in (pcm_end, pcm_end + (count & 1)):
            raise ValueError('A narration receipt can only be attached once to fresh PCM.')
        actual = _pcm_sha256(stream, count, cancelled)
        if actual != pcm_sha256:
            raise ValueError('The narration PCM changed before its receipt was written.')
        if before.st_size > pcm_end and stream.read(1) != b'\0':
            raise ValueError('Invalid narration PCM padding.')
        record = {'schema': SCHEMA, 'narration': validate_narration(narration),
                  'voice': {'id_sha256': _hash(voice.id), 'engine': voice.engine,
                            'language': voice.language, 'runtime_sha256': runtime_sha256},
                  'settings': asdict(settings), 'pcm': {'sha256': actual, **geometry},
                  'notice': NOTICE}
        _validate_record(record)
        raw = encode({'record': record, 'sha256': _hash(encode(record))})
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError('The narration receipt exceeds its metadata budget.')
        end = pcm_end + (count & 1) + CHUNK.size + len(raw) + (len(raw) & 1)
        if end > min(MAX_WAV_BYTES, settings.max_output_bytes):
            raise ValueError('The narrated WAV and receipt exceed the audio-output budget.')
        _unchanged(stream, target, before)
        check_cancelled(cancelled)
        stream.seek(pcm_end)
        if count & 1:
            stream.write(b'\0')
        stream.write(CHUNK.pack(b'rsrc', len(raw)))
        stream.write(raw)
        if len(raw) & 1:
            stream.write(b'\0')
        stream.seek(4)
        stream.write(struct.pack('<I', end - 8))
        stream.flush()
        check_cancelled(cancelled)
    return record


def read_narration_receipt(path, *, document=None, cancelled=lambda: False):
    """Inspect a generated WAV after reopening; never resolve a claimed source.

    None denotes a compatible legacy PCM WAV with no receipt. A different WAV
    layout is unsupported for provenance inspection, not rejected for playback.
    Passing the saved reading document checks its exact snapshot and selection.
    A valid receipt is self-consistent, not signed or authorized library evidence.
    """
    check_cancelled(cancelled)
    target = local_file(path)
    with target.open('rb') as stream:
        before = os.fstat(stream.fileno())
        size = before.st_size
        count, geometry = _header(stream, size)
        pcm_end = HEADER.size + count
        stream.seek(pcm_end)
        if size == pcm_end:
            _unchanged(stream, target, before)
            return None
        if count & 1 and stream.read(1) != b'\0':
            raise ValueError('Invalid narration PCM padding.')
        if stream.tell() == size:
            _unchanged(stream, target, before)
            return None
        header = stream.read(CHUNK.size)
        if len(header) != CHUNK.size:
            raise ValueError('Truncated narration receipt chunk.')
        tag, length = CHUNK.unpack(header)
        if (tag != b'rsrc' or not 0 < length <= MAX_RECEIPT_BYTES
                or stream.tell() + length + (length & 1) != size):
            raise ValueError('Invalid narration receipt chunk or metadata budget.')
        raw = stream.read(length)
        if len(raw) != length or (length & 1 and stream.read(1) != b'\0'):
            raise ValueError('Truncated or invalid narration receipt padding.')
        try:
            envelope = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
            if (not _fields(envelope, 'record sha256') or not _digest(envelope['sha256'])
                    or encode(envelope) != raw or _hash(encode(envelope['record'])) != envelope['sha256']):
                raise ValueError('Narration receipt checksum or encoding mismatch.')
            record = envelope['record']
            _validate_record(record)
        except (UnicodeError, RecursionError, TypeError, OverflowError) as exc:
            raise ValueError('Invalid narration receipt encoding.') from exc
        if (record['pcm'] != {'sha256': _pcm_sha256(stream, count, cancelled), **geometry}
                or size > record['settings']['max_output_bytes']):
            raise ValueError('The narration receipt does not match the retained PCM.')
        if document is not None:
            selected = record['narration']['selection']
            if capture_narration(document, selected['start'], selected['end']) != record['narration']:
                raise ValueError('The saved reading project differs from the narration receipt.')
        _unchanged(stream, target, before)
        check_cancelled(cancelled)
        return record


def describe_narration_receipt(record):
    if record is None:
        return 'This PCM WAV contains no Resonance narration provenance receipt.'
    narration = record['narration']
    selected = narration['selection']
    anchor = narration['source_anchor']
    source = 'No retained-library source anchor.'
    if anchor is not None:
        positions = anchor['positions']
        kind = 'PDF pages' if positions[0]['kind'] == 'physical_page' else 'logical segments'
        indices = ', '.join(str(row['index']) for row in positions[:16])
        if len(positions) > 16:
            indices += f', and {len(positions) - 16} more (listed in the receipt)'
        source = (f"Workspace {anchor['workspace']}; edition {anchor['edition']}; "
                  f"revision {anchor['revision']}; original SHA-256 {anchor['asset_sha256']}. "
                  f"Captured snapshot {kind} ({len(positions)}): {indices}. "
                  'These are snapshot positions, not necessarily the narrated selection; '
                  'printed page labels are not attested by this receipt.')
    span = narration['source_span']
    if span['start'] is None:
        mapping = 'Narrated selection has no mapped source offsets.'
    else:
        precision = 'exact' if span['exact'] else 'approximate'
        mapping = (f"Narrated selection has {precision} snapshot source offsets "
                   f"{span['start']} to {span['end']} (end exclusive); not quotation authority.")
    voice = record['voice']
    runtime = voice['runtime_sha256'] or 'Unavailable'
    return (f"PCM and receipt checksums match. {NOTICE}\n{source}\n{mapping}\n"
            f"Reading revision {narration['reading_revision']}; Unicode selection offsets "
            f"{selected['start']} to {selected['end']} (end exclusive). "
            f"Text SHA-256 {selected['text_sha256']}.\n"
            f"Voice engine {voice['engine']}; language {voice['language']}; "
            f"runtime fingerprint {runtime}.\nPCM SHA-256 {record['pcm']['sha256']}.")
