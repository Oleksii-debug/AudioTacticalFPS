"""Passive metadata for explicitly armed, host-session local schedules.

Time-window and volatile-arming rules reuse the reviewed foreground scheduling
design. These records are declarations and receipts, never execution authority.
"""
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
import re

SCHEMA = 'carr-local-schedule/1'
MAX_RULES = 32
MAX_BYTES = 64 * 1024
MAX_INTERVAL = 7 * 86400
MAX_HORIZON = 366 * 86400
_ID = re.compile(r'[0-9a-f]{32}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z')
_FIELDS = frozenset({
    'schema', 'id', 'title', 'data_dir', 'root_identity', 'kind', 'target',
    'target_identity', 'workspace', 'recursive', 'collections', 'config',
    'include_generated', 'parent', 'created', 'next_due', 'interval_seconds',
    'lateness_seconds', 'disabled', 'sequence', 'last',
})
_COUNTS = frozenset({'queued', 'running', 'complete', 'duplicate', 'rejected',
                     'failed', 'quarantined', 'excluded'})


def utc(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('A schedule timestamp needs an explicit timezone.')
        return value.astimezone(timezone.utc)
    if type(value) is not str or _TIME.fullmatch(value) is None:
        raise ValueError('Use a complete UTC timestamp with Z or +00:00.')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def timestamp(value):
    return utc(value).isoformat()


def _text(value, maximum, *, empty=False):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or not empty and not value.strip()
            or any(ord(c) < 32 and c not in '\n\t' for c in value)):
        raise ValueError('Invalid bounded schedule text.')
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid schedule numeric allowance.')
    return value


def identity(value):
    if (type(value) is not list or len(value) != 2
            or any(type(part) is not int or not 0 <= part < 2**128 for part in value)
            or value[1] == 0):
        raise ValueError('A schedule needs an ordinary local directory identity.')
    return value


def path_text(value):
    _text(value, 32768)
    paths = (PurePosixPath(value), PureWindowsPath(value))
    if (not any(path.is_absolute() for path in paths)
            or value.startswith(('//', '\\\\')) or '://' in value
            or any('..' in path.parts for path in paths)):
        raise ValueError('A schedule path must be a direct local absolute declaration.')
    return value


def digest(value, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError('A snapshot reference must be an exact SHA256.')
    return value


def rule_id(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError('Invalid local schedule identity.')
    return value


def _backup_result(value):
    fields = {'snapshot_sha256', 'backup_manifest_sha256', 'parent', 'files',
              'payload_bytes', 'published', 'durability_confirmed',
              'staging_cleanup_pending', 'new_chunks', 'reused_chunks',
              'new_bytes', 'reused_bytes', 'control_cleanup_pending'}
    if type(value) is not dict or set(value) != fields or value['published'] is not True:
        raise ValueError('Invalid returned snapshot receipt.')
    for key in ('snapshot_sha256', 'backup_manifest_sha256'):
        digest(value[key])
    digest(value['parent'], optional=True)
    for key in ('files', 'payload_bytes', 'new_chunks', 'reused_chunks', 'new_bytes', 'reused_bytes'):
        _integer(value[key], 0, 2**63 - 1)
    for key in ('durability_confirmed', 'staging_cleanup_pending', 'control_cleanup_pending'):
        if type(value[key]) is not bool:
            raise ValueError('Invalid snapshot completion qualification.')
    return value


def _watch_result(value):
    if (type(value) is not dict or set(value) != {'batch_id', 'state', 'counts', 'total'}
            or value['state'] not in ('complete', 'paused', 'attention')):
        raise ValueError('Invalid existing import-batch result.')
    if value['batch_id'] is not None:
        rule_id(value['batch_id'])
    if type(value['counts']) is not dict or set(value['counts']) - _COUNTS:
        raise ValueError('Invalid import-batch counters.')
    for count in value['counts'].values():
        _integer(count, 0, 10000)
    _integer(value['total'], 0, 10000)
    if sum(value['counts'].values()) != value['total']:
        raise ValueError('Import counters do not cover the existing batch.')
    if value['state'] == 'complete' and any(value['counts'].get(name, 0)
            for name in ('queued', 'running', 'rejected', 'failed', 'quarantined')):
        raise ValueError('An unfinished import batch cannot claim completion.')
    if value['batch_id'] is None and (value['total'] or value['state'] != 'complete'):
        raise ValueError('An idle scan cannot claim an import attempt.')
    return value


def validate_result(kind, value):
    if kind == 'backup':
        return _backup_result(value)
    if kind == 'watch':
        return _watch_result(value)
    raise ValueError('Unknown scheduled operation result.')


def validate_record(value, *, identity_hint=None):
    if type(value) is not dict or set(value) != _FIELDS or value['schema'] != SCHEMA:
        raise ValueError('Invalid local schedule record.')
    rule_id(value['id'])
    if identity_hint is not None and value['id'] != rule_id(identity_hint):
        raise ValueError('Schedule filename and identity differ.')
    _text(value['title'], 200)
    path_text(value['data_dir'])
    path_text(value['target'])
    identity(value['root_identity'])
    identity(value['target_identity'])
    if value['kind'] not in ('backup', 'watch'):
        raise ValueError('Choose an existing backup or watched-folder operation.')
    for key in ('disabled', 'recursive', 'include_generated'):
        if type(value[key]) is not bool:
            raise ValueError('Schedule flags must be exact booleans.')
    _integer(value['interval_seconds'], 60, MAX_INTERVAL)
    _integer(value['lateness_seconds'], 1, 86400)
    _integer(value['sequence'], 0, 2**53 - 1)
    created, due = utc(value['created']), utc(value['next_due'])
    if due < created:
        raise ValueError('A schedule cannot precede its creation.')
    digest(value['parent'], optional=True)
    if type(value['collections']) is not list or len(value['collections']) > 128:
        raise ValueError('Too many selected collections.')
    for item in value['collections']:
        _text(item, 128)
    if len(set(value['collections'])) != len(value['collections']):
        raise ValueError('A collection cannot be selected twice.')
    if value['kind'] == 'backup':
        if (value['workspace'] is not None or value['recursive'] or value['collections']
                or value['config'] is not None):
            raise ValueError('A backup rule cannot contain import settings.')
    else:
        _text(value['workspace'], 128)
        if value['include_generated'] or value['parent'] is not None:
            raise ValueError('A watched folder cannot carry backup authority.')
        from .extract.page_ocr import Config
        from dataclasses import asdict
        config = value['config']
        if type(config) is not dict or set(config) != set(asdict(Config())):
            raise ValueError('Invalid saved import settings.')
        for name in ('dpi', 'timeout', 'max_pixels'):
            if type(config[name]) is not int:
                raise ValueError('Import allowances must be exact integers.')
        Config(**config)
    last = value['last']
    if last is None:
        if value['sequence']:
            raise ValueError('A schedule sequence needs its last attempt declaration.')
        return value
    fields = {'sequence', 'status', 'started', 'finished', 'batch_id', 'result', 'message'}
    if (type(last) is not dict or set(last) != fields
            or last['status'] not in ('dispatching', 'complete', 'attention', 'reviewed')
            or type(last['sequence']) is not int or last['sequence'] != value['sequence']
            or not last['sequence']):
        raise ValueError('Invalid local schedule attempt.')
    _text(last['message'], 1000, empty=True)
    started = None if last['started'] is None else utc(last['started'])
    finished = None if last['finished'] is None else utc(last['finished'])
    if (started is not None and started < created
            or finished is not None and finished < (started or created)):
        raise ValueError('Schedule event order is inconsistent.')
    if last['batch_id'] is not None:
        rule_id(last['batch_id'])
        if value['kind'] != 'watch':
            raise ValueError('Only watched-folder attempts reference import batches.')
    if last['status'] == 'dispatching':
        if started is None or finished is not None or last['result'] is not None:
            raise ValueError('An unresolved dispatch cannot claim a returned result.')
    elif finished is None:
        raise ValueError('A settled attempt needs a completion timestamp.')
    if last['result'] is not None:
        if value['kind'] == 'backup':
            _backup_result(last['result'])
        else:
            _watch_result(last['result'])
            if last['result']['batch_id'] != last['batch_id']:
                raise ValueError('The batch reference differs from the returned result.')
    if last['status'] == 'complete':
        if started is None or last['result'] is None:
            raise ValueError('Completion requires an actually returned result.')
        if value['kind'] == 'watch' and last['result']['state'] != 'complete':
            raise ValueError('A partial batch is not complete.')
    return value
