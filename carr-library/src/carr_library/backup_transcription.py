"""Passive backup adapter for the existing retained transcription service.

Only producer-owned paths and protocol data are admitted. Original inputs and
worker outputs stream through the common backup copier. Completed receipts are
checked with the same engine-free validator used by normal read/export; no
configured runtime, model, old input root or credential is opened.
"""
from hashlib import sha256
from pathlib import PurePosixPath
import re

from .backup_producers import fields, text, number, digest, path

PREFIX = 'state/transcription/'
_JOB = re.compile(r'state/transcription/([0-9a-f]{64})/jobs/([0-9a-f]{32})/(.+)\Z')
_HASH = r'[0-9a-f]{64}'
_INPUT = re.compile(r'input/[0-9]{3}-[0-9a-f]{32}\.[a-z0-9]{2,5}\Z')
_RECEIPT = re.compile(r'receipts/(' + _HASH + r')\.result\.json\Z')
_EDIT = re.compile(r'edits/([0-9a-f]{32})\.json\Z')
_METADATA = {'job.json', 'job_config.json', 'job_status.json', 'host_status.json',
             'settings_live.json'}
MAX_BYTES = 32 * 1024**3


def member(name):
    return _JOB.fullmatch(name)


def _streamed_tail(tail):
    value = PurePosixPath(tail)
    if _INPUT.fullmatch(tail):
        return True
    if value.parts[0] == 'results' and len(value.parts) >= 2:
        return value.suffix in ('.json', '.txt', '.srt', '.vtt')
    if tail in ('logs/progress.log', 'logs/failed.log'):
        return True
    # The two admitted worker profiles own separate, inert text/JSON caches.
    # Keep these resume inputs even when generated Studio audio is omitted.
    if (len(value.parts) in (3, 4) and value.parts[:2] in
            (('logs', 'cache'), ('logs', 'cache_v5'))):
        return value.suffix in ('.json', '.txt')
    return False


def is_media(name):
    match = member(name)
    return bool(match and _streamed_tail(match[3]))


def allows_empty(name):
    return is_media(name)


def limit(name):
    if name == PREFIX + 'engine.json':
        return 32768
    match = member(name)
    if match is None:
        return None
    tail = match[3]
    if _INPUT.fullmatch(tail):
        from .transcription import MEDIA_SUFFIXES, MAX_SOURCE_BYTES
        return MAX_SOURCE_BYTES if PurePosixPath(tail).suffix in MEDIA_SUFFIXES else None
    if _streamed_tail(tail):
        return 512 * 1024**2
    if tail in _METADATA or _RECEIPT.fullmatch(tail) or _EDIT.fullmatch(tail):
        return 32 * 1024**2
    return None


def ignored(name):
    match = member(name)
    if match:
        tail = PurePosixPath(match[3])
        return (tail.as_posix() in ('.transcribe.lock', 'stop.flag', 'pause.flag')
                or tail.name.startswith('.pending-')
                or bool(re.fullmatch(r'\.[A-Za-z0-9_.-]+\.[0-9a-f]{32}\.tmp', tail.name)))
    return bool(re.fullmatch(r'state/transcription/(?:\.engine\.lock|[0-9a-f]{64}/jobs/\.submit\.lock)', name))


def _engine(value):
    from .transcription import _protocol, RECEIPTS_PROTOCOL, V5_PROTOCOL, V5_SOURCES
    fields(value, {'version', 'python', 'worker', 'models_dir', 'python_sha256',
                   'worker_sha256', 'network', 'inference_qualified'},
           {'protocol', 'wrapper_sources_sha256', 'retry_policy'})
    if type(value['version']) is not int or value['version'] != 1 or value['inference_qualified'] is not False:
        raise ValueError('Invalid transcription engine declaration.')
    protocol = _protocol(value.get('protocol', RECEIPTS_PROTOCOL))
    for key in ('python', 'worker', 'models_dir'):
        path(value[key])
    for key in ('python_sha256', 'worker_sha256'):
        digest(value[key])
    if value['network'] != 'local-files-only; Python network calls denied':
        raise ValueError('Invalid transcription isolation declaration.')
    if protocol == V5_PROTOCOL:
        hashes = value.get('wrapper_sources_sha256')
        fields(hashes, V5_SOURCES)
        for checksum in hashes.values():
            digest(checksum)
        if hashes['worker.py'] != value['worker_sha256']:
            raise ValueError('Whisper V5 helper identity disagrees with its worker.')
        text(value.get('retry_policy'), 8192)
    elif set(value) & {'wrapper_sources_sha256', 'retry_policy'}:
        raise ValueError('Legacy transcription cannot carry a different profile declaration.')


def _job(db, value, workspace_key, identity):
    from .transcription import settings, _protocol, RECEIPTS_PROTOCOL, MAX_FILES, MAX_SOURCE_BYTES, MAX_BATCH_BYTES
    fields(value, {'version', 'id', 'workspace', 'title', 'created', 'updated', 'status',
                   'sources', 'settings', 'done', 'failed', 'receipts', 'artifacts',
                   'run_context', 'last_error'}, {'protocol', 'worker_run_id', 'effective_runs'})
    if type(value['version']) is not int or value['version'] != 1 or value['id'] != identity:
        raise ValueError('Invalid retained transcription job identity.')
    text(value['workspace'], 200)
    if sha256(value['workspace'].encode('utf-8')).hexdigest() != workspace_key:
        raise ValueError('Transcription job belongs to another workspace directory.')
    if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (value['workspace'],)).fetchone() is None:
        raise ValueError('Transcription job belongs to a missing saved library.')
    text(value['title'], 200)
    for key in ('created', 'updated'):
        number(value[key])
    if value['status'] not in ('ready', 'running', 'completed', 'partial', 'failed', 'stopped'):
        raise ValueError('Invalid transcription status.')
    protocol = _protocol(value.get('protocol', RECEIPTS_PROTOCOL))
    settings(value['settings'], protocol=protocol)
    if type(value['sources']) is not list or not 1 <= len(value['sources']) <= MAX_FILES:
        raise ValueError('Invalid retained transcription source inventory.')
    names, total = set(), 0
    for row in value['sources']:
        fields(row, {'filename', 'original_name', 'size_bytes', 'sha256'})
        if (type(row['filename']) is not str or not _INPUT.fullmatch('input/' + row['filename'])
                or row['filename'] in names):
            raise ValueError('Invalid retained transcription source filename.')
        names.add(row['filename'])
        text(row['original_name'], 4096)
        number(row['size_bytes'], 0, MAX_SOURCE_BYTES, integer=True)
        digest(row['sha256'])
        total += row['size_bytes']
    if total > MAX_BATCH_BYTES:
        raise ValueError('Retained transcription sources exceed their byte allowance.')
    for key in ('done', 'failed'):
        number(value[key], 0, len(names), integer=True)
    text(value['last_error'], 8192, empty=True)
    if type(value['receipts']) is not list or len(value['receipts']) > len(names):
        raise ValueError('Invalid accepted transcription receipt inventory.')
    if len(set(value['receipts'])) != len(value['receipts']) or any(
            type(name) is not str or not _RECEIPT.fullmatch(name) for name in value['receipts']):
        raise ValueError('Invalid accepted transcription receipt path.')
    if type(value['artifacts']) is not list or len(value['artifacts']) > 6 * len(names):
        raise ValueError('Invalid transcription artifact inventory.')
    for row in value['artifacts']:
        fields(row, {'path', 'size_bytes', 'sha256', 'source', 'format', 'recognition_identity'})
        relative = PurePosixPath(row['path']) if type(row['path']) is str else None
        if (relative is None or relative.is_absolute() or str(relative) != row['path']
                or '..' in relative.parts or '\\' in row['path'] or ':' in row['path']
                or row['source'] not in names
                or row['format'] not in ('txt', 'srt', 'vtt', 'json', 'words.srt', 'words.vtt')):
            raise ValueError('Invalid transcription artifact declaration.')
        number(row['size_bytes'], 0, 512 * 1024**2, integer=True)
        digest(row['sha256'])
        digest(row['recognition_identity'])
    context = value['run_context']
    if context is not None:
        fields(context, {'input_root', 'worker_sha256', 'python_sha256'},
               {'protocol', 'host_run_id', 'wrapper_sources_sha256', 'model_identity',
                'worker_settings', 'source_identities', 'result_directory', 'manifest_sha256'})
        path(context['input_root'])
        digest(context['worker_sha256'])
        digest(context['python_sha256'])
    if 'worker_run_id' in value and (type(value['worker_run_id']) is not str
                                    or not re.fullmatch(r'[0-9a-f]{32}', value['worker_run_id'])):
        raise ValueError('Invalid saved transcription run identity.')
    if 'effective_runs' in value and (type(value['effective_runs']) is not list
                                     or len(value['effective_runs']) > len(names)):
        raise ValueError('Invalid effective transcription settings inventory.')


def validate(db, name, value):
    from .transcription import _identity
    if name == PREFIX + 'engine.json':
        return _engine(value)
    match = member(name)
    if match is None:
        raise ValueError('Unknown transcription producer path.')
    tail = match[3]
    if tail == 'job.json':
        return _job(db, value, match[1], match[2])
    if edit := _EDIT.fullmatch(tail):
        fields(value, {'version', 'id', 'job', 'source', 'source_sha256', 'recognition_identity',
                       'original_text_sha256', 'created', 'text', 'kind'})
        if (type(value['version']) is not int or value['version'] != 1
                or value['id'] != edit[1] or value['job'] != match[2]
                or not _INPUT.fullmatch('input/' + value['source'])):
            raise ValueError('Invalid retained transcript edit identity.')
        for key in ('source_sha256', 'recognition_identity', 'original_text_sha256'):
            digest(value[key])
        number(value['created'])
        text(value['text'], 16 * 1024**2, empty=True)
        if value['kind'] != 'user-edited derivative; source and timed recognition remain unchanged':
            raise ValueError('Invalid transcript derivative declaration.')
    elif _RECEIPT.fullmatch(tail):
        if (type(value) is not dict or value.get('version') != 1 or value.get('state') != 'complete'
                or type(value.get('run_id')) is not str or not re.fullmatch(r'[0-9a-f]{32}', value['run_id'])):
            raise ValueError('Invalid saved transcription recognition receipt.')
        _identity(value.get('identity'))
        _identity(value['identity'].get('model'))
        _identity(value['identity'].get('runtime'))
    elif tail == 'job_config.json':
        fields(value, {'job_name', 'folders', 'results_root', 'models_dir', 'settings'})
        text(value['job_name'], 200)
        if type(value['folders']) is not list or len(value['folders']) != 1:
            raise ValueError('Invalid transcription input root declaration.')
        for item in (*value['folders'], value['results_root'], value['models_dir']):
            path(item)
        if type(value['settings']) is not dict:
            raise ValueError('Invalid transcription worker settings.')
    elif tail in ('job_status.json', 'host_status.json', 'settings_live.json'):
        if type(value) is not dict or len(value) > 256:
            raise ValueError('Invalid transcription worker metadata.')
    else:
        raise ValueError('Unknown transcription metadata path.')


def validate_collection(root, entries, records, *, cancelled):
    from .transcription import TranscriptionService, RECEIPTS_PROTOCOL, V5_PROTOCOL
    by_name = {row['filename']: row for row in entries}
    groups = {}
    for name in by_name:
        if match := member(name):
            prefix = name[:-len(match[3])]
            groups.setdefault(prefix, []).append(name)
    # Bypass construction deliberately: these methods only inspect the copied
    # job/input/results tree and do not need an app, vault or engine selection.
    validator = object.__new__(TranscriptionService)
    for prefix, names in groups.items():
        job = records.get(prefix + 'job.json')
        if job is None:
            raise ValueError('Retained transcription files have no owning job declaration.')
        job = dict(job, protocol=job.get('protocol', RECEIPTS_PROTOCOL))
        directory = root / prefix
        TranscriptionService._sources(validator, directory, job, cancelled)
        sources = {row['filename']: row for row in job['sources']}
        for name in names:
            tail = name[len(prefix):]
            if _EDIT.fullmatch(tail):
                edit = records[name]
                source = sources.get(edit['source'])
                if source is None or source['sha256'] != edit['source_sha256']:
                    raise ValueError('Transcript edit differs from its retained original.')
        if job['status'] in ('completed', 'partial'):
            status_name = 'host_status.json' if job['protocol'] == V5_PROTOCOL else 'job_status.json'
            status = records.get(prefix + status_name)
            if type(status) is not dict or status.get('run_id') != job.get('worker_run_id'):
                raise ValueError('A completed transcription is missing its exact current-run status.')
            receipts, artifacts = TranscriptionService._receipts(validator, directory, job, status, cancelled)
            if receipts != job['receipts'] or artifacts != job['artifacts']:
                raise ValueError('Saved transcription results differ from their verified receipts.')
            if job['status'] == 'completed' and len(receipts) != len(sources):
                raise ValueError('A completed transcription is missing accepted sources.')
