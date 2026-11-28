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
from .integrations import transcription_backup as _canonical

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
    if _canonical.is_input(name):
        return True
    match = member(name)
    return bool(match and _streamed_tail(match[3]))


def allows_empty(name):
    return is_media(name) or _canonical.allows_empty(name)


def limit(name):
    if maximum := _canonical.limit(name):
        return maximum
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
    if is_local_origin(name) or name == PREFIX + '.origin.lock' or _canonical.ignored(name):
        return True
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
           {'protocol', 'wrapper_sources_sha256', 'retry_policy', 'runtime_profile_id',
            'runtime_profile_fingerprint', 'review_required', 'restored'})
    if type(value['version']) is not int or value['version'] != 1 or value['inference_qualified'] is not False:
        raise ValueError('Invalid transcription engine declaration.')
    protocol = _protocol(value.get('protocol', RECEIPTS_PROTOCOL))
    for key in ('python', 'worker', 'models_dir'):
        path(value[key])
    for key in ('python_sha256', 'worker_sha256'):
        digest(value[key])
    profile_fields = {'runtime_profile_id', 'runtime_profile_fingerprint'}
    if set(value) & profile_fields:
        if not profile_fields <= value.keys():
            raise ValueError('Incomplete full runtime admission binding.')
        if type(value['runtime_profile_id']) is not str or not re.fullmatch('[0-9a-f]{32}', value['runtime_profile_id']):
            raise ValueError('Invalid full runtime profile identity.')
        digest(value['runtime_profile_fingerprint'])
    if 'review_required' in value or 'restored' in value:
        if value.get('review_required') is not True or value.get('restored') is not True:
            raise ValueError('Restored legacy execution must remain explicitly unreviewed.')
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
    from .assistant.transcription_provider import PROTOCOL as REMOTE_PROTOCOL
    if type(value) is dict and value.get('protocol') == REMOTE_PROTOCOL:
        from .integrations.transcription_remote_values import job
        job(value, identity=identity)
        if (sha256(value['workspace'].encode('utf-8')).hexdigest() != workspace_key
                or db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?',
                              (value['workspace'],)).fetchone() is None):
            raise ValueError('Remote transcription belongs to a missing or different copied workspace.')
        return
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
    if _canonical.owned(name):
        from .durable_state import encode
        return validate_raw(db, name, encode(value))
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
    canonical_records = {name: value for name, value in records.items() if _canonical.owned(name)}
    _canonical.validate_collection([row for row in entries if _canonical.owned(row['filename'])],
                                   canonical_records)
    engine = records.get(PREFIX + 'engine.json')
    if engine and 'runtime_profile_id' in engine:
        profile = canonical_records.get(PREFIX + 'profiles/' + engine['runtime_profile_id'] + '.json')
        if (profile is None or profile['fingerprint'] != engine['runtime_profile_fingerprint']
                or profile['protocol'] != engine.get('protocol', RECEIPTS_PROTOCOL)
                or profile['worker_identity']['sha256'] != engine['worker_sha256']
                or profile['python_identity']['sha256'] != engine['python_sha256']):
            raise ValueError('A legacy full runtime binding has no exact retained profile.')
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
        from .assistant.transcription_provider import PROTOCOL as REMOTE_PROTOCOL
        if job['protocol'] == REMOTE_PROTOCOL:
            from .integrations.transcription_remote_values import verify_files
            verify_files(directory, job, cancelled=cancelled,
                         names=[name[len(prefix):] for name in names])
            continue
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


def canonical_member(name):
    return _canonical.owned(name)


def is_local_origin(name):
    parts = PurePosixPath(name).parts
    return (len(parts) == 3 and parts[0].rstrip(' .').casefold() == 'state'
            and parts[1].rstrip(' .').casefold() == 'transcription'
            and parts[2].rstrip(' .').casefold() == '.local-origin.json')


def is_cache_directory(name):
    return _canonical.is_cache_directory(name)


def is_cache_file(name):
    """Only the canonical producer's explicitly optional recognition cache."""
    return _canonical.is_cache_file(name)


def validate_raw(db, name, raw):
    """Schema-dispatch only; external runtime paths remain inert references."""
    if _canonical.owned(name):
        value = _canonical.validate(name, raw)
        if _canonical.BINDING.fullmatch(name):
            if db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?',
                          (value['workspace'],)).fetchone() is None:
                raise ValueError('The transcription binding belongs to a missing saved workspace.')
        return value
    from .backup_sidecars import _json
    value = _json(raw)
    validate(db, name, value)
    return value


def prepare_restored_inventory(stage, destination, entries, *, cancelled):
    """Revoke external execution after byte verification, before publication.

    Accepted source/result bytes and original receipt provenance do not change.
    The new local-origin nonce is never copied or created during restoration.
    """
    from hashlib import sha256
    from . import backup_sidecars
    from .durable_state import atomic_json, read_json
    from .integrations.transcription_protocol import prepare_restored_state
    backup_sidecars._check_cancelled(cancelled)
    originals = {entry['filename']: entry for entry in entries}
    if not any(name.startswith(PREFIX) for name in originals):
        return tuple(entries)
    root = stage / PREFIX
    prepare_restored_state(stage / 'state', destination, cancelled=cancelled)
    names = set(originals)
    names.update(path.relative_to(stage).as_posix() for path in root.glob('jobs/*/control.json'))
    output, records = [], {}
    for name in sorted(names):
        backup_sidecars._check_cancelled(cancelled)
        if not name.startswith(PREFIX):
            output.append(originals[name])
            continue
        target = stage / name
        if not target.exists():
            # Only scrubbed canonical worker control files may disappear.
            match = _canonical.WORKER.fullmatch(name)
            if not match or match[3] not in _canonical._CONTROL:
                raise ValueError('An unexpected transcription file disappeared during restoration.')
            continue
        if is_media(name):
            output.append(originals[name])
            continue
        raw = backup_sidecars.read_regular(target, limit(name), allow_empty=allows_empty(name))
        if _canonical.owned(name):
            value = _canonical.validate(name, raw)
        else:
            from .backup_sidecars import _json
            value = _json(raw)
            from .assistant.transcription_provider import PROTOCOL as REMOTE_PROTOCOL
            if (member(name) and member(name)[3] == 'job.json'
                    and value.get('protocol') == REMOTE_PROTOCOL):
                from .integrations.transcription_remote_values import job
                job(value)
                value['remote'] = dict(value['remote'], restored=True)
                if value['status'] == 'running':
                    value.update(status='stopped', last_error=
                        'Restored remote work has no active consent. Verify any saved result; review charge state before a new explicit call.')
                atomic_json(target, value, replace=True)
                raw = backup_sidecars.read_regular(target, limit(name))
            if name == PREFIX + 'engine.json':
                _engine(value)
        if value is not None:
            records[name] = value
        output.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    validate_collection(stage, output, records, cancelled=cancelled)
    backup_sidecars._check_cancelled(cancelled)
    return tuple(output)
