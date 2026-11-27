"""Pure legacy batch declarations, including paths from another operating system.

These records are retained inert. Portable validation never translates a saved
path or grants it filesystem access; activation belongs to a separate importer.
"""
from collections import Counter
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from .formats import SUFFIXES
from .ocr_config import Config

STATES = ('queued', 'running', 'complete', 'duplicate', 'rejected', 'failed', 'quarantined', 'excluded')
BATCH_KEYS = {'version', 'batch_id', 'data_dir', 'workspace', 'created', 'updated',
              'state', 'collections', 'recursive', 'config', 'rows', 'counts', 'total'}
ROW_KEYS = {'row_id', 'work_id', 'edition_id', 'request_id', 'source', 'file', 'title',
            'state', 'message', 'manifest', 'duplicate_of', 'result'}


def _text(value, limit, *, empty=True):
    return isinstance(value, str) and len(value) <= limit and '\x00' not in value and (empty or bool(value))


def _source(value):
    if not _text(value, 32768, empty=False) or value.startswith(('//', '\\\\')) or any(ord(c) < 32 for c in value):
        return False
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    return path.is_absolute() and '..' not in path.parts


def validate_batch(record, batch_id, source_root):
    if (type(record) is not dict or set(record) != BATCH_KEYS
            or type(record['version']) is not int or record['version'] != 1
            or record['batch_id'] != batch_id or record['data_dir'] != source_root
            or not _text(record['workspace'], 200, empty=False)
            or not _text(record['created'], 64, empty=False) or not _text(record['updated'], 64, empty=False)
            or record['state'] not in ('queued', 'running', 'paused', 'attention', 'complete')
            or type(record['recursive']) is not bool or type(record['collections']) is not list
            or len(record['collections']) > 128 or any(not _text(c, 200, empty=False) for c in record['collections'])
            or len(set(record['collections'])) != len(record['collections'])
            or type(record['config']) is not dict or type(record['rows']) is not list or len(record['rows']) > 10000):
        raise ValueError('Invalid legacy batch schema or source-root binding.')
    Config(**record['config'])
    seen = {key: set() for key in ('row_id', 'work_id', 'edition_id', 'request_id')}
    for row in record['rows']:
        if (type(row) is not dict or set(row) != ROW_KEYS or row['state'] not in STATES
                or not _source(row['source']) or not _text(row['file'], 32768, empty=False)
                or not _text(row['title'], 4000) or not _text(row['message'], 4000)
                or row['duplicate_of'] is not None and not _text(row['duplicate_of'], 32768)
                or row['result'] is not None and (type(row['result']) is not dict
                    or len(json.dumps(row['result']).encode('utf-8')) > 64000)):
            raise ValueError('Invalid inert legacy batch row.')
        for key, known in seen.items():
            if not isinstance(row[key], str) or not re.fullmatch('[0-9a-f]{32}', row[key]) or row[key] in known:
                raise ValueError('Invalid or repeated legacy batch row identity.')
            known.add(row[key])
        manifest = row['manifest']
        if manifest is None:
            if row['state'] != 'rejected':
                raise ValueError('Legacy batch source has no planned identity.')
        elif (type(manifest) is not dict or set(manifest) != {'sha256', 'mime', 'size'}
                or not isinstance(manifest['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', manifest['sha256'])
                or manifest['mime'] not in SUFFIXES or type(manifest['size']) is not int
                or not 0 < manifest['size'] <= 64 * 1024 * 1024):
            raise ValueError('Invalid legacy batch source manifest.')
    counts = dict.fromkeys(STATES, 0)
    counts.update(Counter(row['state'] for row in record['rows']))
    if (type(record['total']) is not int or record['total'] != len(record['rows'])
            or type(record['counts']) is not dict or any(type(value) is not int for value in record['counts'].values())
            or record['counts'] != counts):
        raise ValueError('Legacy batch summary differs from its rows.')
    return record
