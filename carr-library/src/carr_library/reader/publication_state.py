"""Bounded resume positions for exact external publication bytes, never paths."""
import json
import re

from ..audio.jobs import atomic_json, checked_directory, read_json
from ..audio.models import number
from ..backup_files import optional_present
from ..ingest.writer_guard import exclusive_writer
from .reading_state import _now, _timestamp, _unique


FILENAME = 'reading-publications.json'
MAX_BYTES = 8 * 1024**2
MAX_POSITIONS = 10000


def decode_positions(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Publication reading positions exceed their bounded byte budget.')
    value = json.loads(raw, object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {'schema', 'positions'}
            or value['schema'] != 'carr-publication-reading/1'):
        raise ValueError('Unsupported publication reading positions.')
    if type(value['positions']) is not list or len(value['positions']) > MAX_POSITIONS:
        raise ValueError('Too many publication reading positions.')
    rows = {}
    for row in value['positions']:
        if (type(row) is not dict
                or set(row) != {'publication_sha256', 'kind', 'duration', 'position', 'updated_at'}
                or type(row['publication_sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}', row['publication_sha256']) is None
                or row['publication_sha256'] in rows or row['kind'] not in ('epub', 'daisy')):
            raise ValueError('Invalid publication reading position.')
        duration = number(row['duration'], .000001, 365 * 86400, 'Publication duration')
        number(row['position'], 0, duration, 'Publication resume position')
        _timestamp(row['updated_at'])
        rows[row['publication_sha256']] = row
    return rows


class PublicationPositions:
    def __init__(self, root):
        self.root = checked_directory(root)
        self.path = self.root / FILENAME

    def _read(self):
        if not optional_present(self.path):
            return {}
        value = read_json(self.path, limit=MAX_BYTES)
        return decode_positions(json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def load(self, document):
        from .media_overlay import PreparedOverlay
        if type(document) is not PreparedOverlay:
            raise ValueError('Prepare the exact publication before restoring its cursor.')
        row = self._read().get(document.publication_sha256)
        if row is not None and (row['kind'], row['duration']) == (document.kind, document.duration):
            return row['position']
        return None

    def remember(self, document, position, *, guard=lambda: None):
        from .media_overlay import PreparedOverlay
        if type(document) is not PreparedOverlay:
            raise ValueError('Prepare the exact publication before saving its cursor.')
        row = dict(publication_sha256=document.publication_sha256, kind=document.kind,
                   duration=document.duration, position=position, updated_at=_now())
        decode_positions(json.dumps({'schema': 'carr-publication-reading/1', 'positions': [row]}).encode())
        with exclusive_writer(self.root):
            guard()
            rows = self._read()
            rows[document.publication_sha256] = row
            value = {'schema': 'carr-publication-reading/1', 'positions': list(rows.values())}
            decode_positions(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            guard()
            atomic_json(self.path, value)
        return row
