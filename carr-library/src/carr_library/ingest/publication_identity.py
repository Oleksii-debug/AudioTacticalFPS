"""Bounded immutable launch identities for the existing import supervisor.

This module stores no receipts and defines no schema. The canonical
import_publication row remains the sole committed publication authority.
"""
from dataclasses import asdict, dataclass
import json
import re

from ..catalog.membership import _identity, _metadata


MAX_IDENTITY = 64_000
METADATA_FIELDS = {'title', 'authors', 'language', 'edition', 'volume',
                   'collections', 'work_id', 'edition_id'}
REQUEST_FIELDS = {'version', 'request_id', 'data_dir', 'source', 'workspace', 'metadata', 'config',
                  'expected_manifest', 'pinned_asset', 'resume_job'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hex(value, length):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
        raise ValueError('Invalid publication receipt identity.')
    return value


def _manifest(value):
    from ..ingest.retained_assets import SUFFIXES
    from ..catalog.streaming_pdf import valid_original_size
    if (not isinstance(value, dict) or set(value) != {'sha256', 'mime', 'size'}
            or value['mime'] not in SUFFIXES or type(value['size']) is not int
            or not valid_original_size(value['mime'], value['size'])):
        raise ValueError('Invalid publication source manifest.')
    _hex(value['sha256'], 64)
    return dict(value)


def _registration(value):
    if not isinstance(value, dict) or set(value) != METADATA_FIELDS:
        raise ValueError('Invalid publication registration.')
    result = dict(value)
    for name in ('work_id', 'edition_id'):
        result[name] = _identity(value[name])
    for name in ('title', 'language', 'edition', 'volume'):
        result[name] = _metadata(value[name])
    for name in ('authors', 'collections'):
        if not isinstance(value[name], (list, tuple)) or len(value[name]) > 100:
            raise ValueError('Invalid publication registration sequence.')
        result[name] = [(_metadata(item) if name == 'authors' else _identity(item))
                        for item in value[name]]
    if any(not author or not author.strip() for author in result['authors']):
        raise ValueError('Invalid publication author.')
    return result



# Exact persisted configuration before and after verified offline pack support.
# Reading the earlier form does not rewrite its immutable journal/receipt hash.
_LEGACY_CONFIG_FIELDS = frozenset((
    'languages', 'dpi', 'timeout', 'max_pixels', 'orientation', 'model_policy',
    'model_profile'))


def validate_configuration(value):
    """Read either released config shape; reject partial or unknown settings."""
    from ..extract.page_ocr import Config
    current = set(asdict(Config()))
    if (not isinstance(value, dict)
            or set(value) not in (current, _LEGACY_CONFIG_FIELDS)):
        raise ValueError('Invalid publication configuration fields.')
    if any(type(value[key]) is not int for key in ('dpi', 'timeout', 'max_pixels')):
        raise ValueError('Invalid publication configuration resource limits.')
    return Config(**value)


def _request(value):
    if (not isinstance(value, dict) or set(value) not in (REQUEST_FIELDS, REQUEST_FIELDS | {'pdf_max_bytes'})
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Invalid publication request schema.')
    result = dict(value)
    if 'pdf_max_bytes' in value:
        from ..catalog.streaming_pdf import pdf_budget
        pdf_budget(value['pdf_max_bytes'])
    _hex(value['request_id'], 32)
    _identity(value['workspace'])
    for name in ('data_dir', 'source'):
        if not isinstance(value[name], str) or not value[name] or '\x00' in value[name]:
            raise ValueError('Invalid publication launch path.')
    result['metadata'] = _registration(value['metadata'])
    validate_configuration(value['config'])
    result['config'] = dict(value['config'])
    if value['expected_manifest'] is not None:
        result['expected_manifest'] = _manifest(value['expected_manifest'])
    for name in ('pinned_asset', 'resume_job'):
        if value[name] is not None:
            _hex(value[name], 64)
    return result


def request_identity(record):
    """Freeze the exact launch identity before progress can change it."""
    value = dict(version=record['version'], request_id=record['request_id'],
                 data_dir=record['data_dir'], source=record['source'],
                 workspace=record['workspace'], metadata=record['metadata'],
                 config=record['config'], expected_manifest=record.get('expected_manifest'),
                 pinned_asset=record.get('asset_sha256'), resume_job=record.get('job'))
    if 'pdf_max_bytes' in record:
        value['pdf_max_bytes'] = record['pdf_max_bytes']
    result = _canonical(_request(value))
    if len(result.encode('utf-8')) > MAX_IDENTITY:
        raise ValueError('Publication request identity exceeds its limit.')
    return result


@dataclass(frozen=True)
class PublicationAttempt:
    attempt_id: str
    identity: str

    def request(self):
        _hex(self.attempt_id, 32)
        if not isinstance(self.identity, str) or len(self.identity.encode('utf-8')) > MAX_IDENTITY:
            raise ValueError('Invalid publication attempt.')
        value = _request(json.loads(self.identity))
        if _canonical(value) != self.identity:
            raise ValueError('Noncanonical publication attempt.')
        return value
