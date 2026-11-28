"""Passive, receipt-bound transcription sidecars for the canonical backup.

Only producer-owned declarations and retained data are accepted. No worker,
model, media decoder, credential, or external path is opened. The caller first
copies and hashes the staged files; this adapter checks their typed relationships.
Restoring these bytes never starts recognition or turns a stopped job complete.
"""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .backup_producers import fields, text, number, digest, path as external_path
from .ingest.bundle import _unique_object
from .transcription import (
    MAX_FILES, MAX_SOURCE_BYTES, MAX_BATCH_BYTES, MAX_TEXT_BYTES, MEDIA_SUFFIXES,
    RECEIPTS_PROTOCOL, V5_PROTOCOL, V5_SOURCES, V5_PROVENANCE,
    RECOGNITION_KEYS, EXPORT_KEYS, V5_RECOGNITION_KEYS,
    settings, _identity, _bound, _v5_settings, _v5_attempts, _v5_digest,
    _v5_timing, _word_subtitles, configuration_fingerprint,
)

PREFIX = 'state/transcription/'
MAX_MEDIA_BYTES = 32 * 1024**3
MAX_METADATA_BYTES = 512 * 1024**2
_HEX = r'[0-9a-f]{64}'
_ID = r'[0-9a-f]{32}'
_JOB = re.compile(PREFIX + r'(' + _HEX + r')/jobs/(' + _ID + r')/(.+)\Z')
_SOURCE = re.compile(r'[0-9]{3}-' + _ID + r'\.[a-z0-9]+\Z')
_RECEIPT = re.compile(r'receipts/(' + _HEX + r')\.result\.json\Z')
_EDIT = re.compile(r'edits/(' + _ID + r')\.json\Z')
_V5_RESULT = re.compile(r'runs/(' + _ID + r')/Whisper_V5/(' + _ID + r')/(.+)\Z')
_BASE = frozenset(('job.json', 'job_config.json', 'job_status.json', 'host_status.json',
                   'control.json', 'rejected-execution.json'))
_PROFILE = re.compile(PREFIX + r'profiles/(' + _ID + r')\.json\Z')
_REVIEW = re.compile(PREFIX + r'(' + _HEX + r')/reviews/(' + _ID + r')\.json\Z')
_FORMATS = ('words.srt', 'words.vtt', 'words.txt', 'txt', 'srt', 'vtt', 'json')


def _relative(value, *, depth=6, hidden=False):
    text(value, 1024)
    path = PurePosixPath(value)
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
                *(prefix + digit for prefix in ('com', 'lpt') for digit in '123456789\u00b9\u00b2\u00b3')}
    if (path.is_absolute() or str(path) != value or len(path.parts) > depth
            or any(character in '<>:"\\|?*' or ord(character) < 32 for character in value)
            or any(part in ('.', '..') or (not hidden and part.startswith('.')) or part.endswith((' ', '.'))
                   or part.split('.')[0].rstrip(' ').casefold() in reserved for part in path.parts)):
        raise ValueError('Invalid retained transcription filename.')
    return path


def _source_name(value):
    if (type(value) is not str or not _SOURCE.fullmatch(value)
            or PurePosixPath(value).suffix not in MEDIA_SUFFIXES):
        raise ValueError('Invalid retained transcription source name.')
    return value


def _kind(value):
    return next((kind for kind in _FORMATS if value.endswith('.' + kind)), None)


def _member(name):
    if type(name) is not str:
        return None
    if name == PREFIX + 'engine.json':
        return ('engine', None, None, 'engine.json')
    if profile := _PROFILE.fullmatch(name):
        return ('profile', None, profile[1], name[len(PREFIX):])
    if review := _REVIEW.fullmatch(name):
        return ('review', review[1], review[2], 'execution-review.json')
    match = _JOB.fullmatch(name)
    if not match:
        return None
    scope, identity, tail = match.groups()
    if tail in _BASE:
        return (tail, scope, identity, tail)
    if _RECEIPT.fullmatch(tail):
        return ('receipt', scope, identity, tail)
    if _EDIT.fullmatch(tail):
        return ('edit', scope, identity, tail)
    if tail.startswith('input/'):
        try:
            _source_name(tail[6:])
        except ValueError:
            return None
        return ('input', scope, identity, tail)
    if tail.startswith('results/'):
        relative = tail[8:]
        try:
            _relative(relative)
        except ValueError:
            return None
        v5 = _V5_RESULT.fullmatch(relative)
        if relative.startswith('runs/'):
            if v5 is None or v5[2] != identity:
                return None
            suffix = v5[3]
            if (len(PurePosixPath(suffix).parts) > 2
                    or suffix.startswith('host-derived/') and _kind(suffix) not in ('words.srt', 'words.vtt')):
                return None
        if relative.endswith('/result_manifest.json'):
            if v5 is None or v5[3] != 'result_manifest.json':
                return None
            return ('manifest', scope, identity, tail)
        if _kind(relative) is not None:
            return ('result', scope, identity, tail)
    return None


def limit(name):
    member = _member(name)
    if member is None:
        return None
    kind = member[0]
    if kind == 'input':
        return MAX_SOURCE_BYTES
    if kind in ('engine', 'profile'):
        return 32 * 1024**2
    if kind in ('control.json', 'rejected-execution.json', 'review'):
        return 16384
    if kind in ('job_status.json', 'host_status.json', 'job_config.json'):
        return 4 * 1024**2
    if kind == 'edit':
        return 32 * 1024**2
    if kind in ('job.json', 'receipt', 'manifest'):
        return 32 * 1024**2
    return MAX_METADATA_BYTES


def is_media(name):
    member = _member(name)
    return member is not None and member[0] == 'input'


def allow_empty(name):
    member = _member(name)
    return member is not None and (
        member[0] == 'input' or member[0] == 'result' and not member[3].endswith('.json'))



def is_cache_directory(name):
    """Recognize only optional logs caches owned by this flat job layout.

    This is an omission boundary, not an admission schema. Accepted results and
    V5 manifests remain required even when their payload records cache identity.
    """
    match = _JOB.fullmatch(name) if type(name) is str else None
    return bool(match and match[3] in ('logs/cache', 'logs/cache_v5'))


def is_cache_file(name):
    """Identify optional cache entries without reading or accepting their data."""
    match = _JOB.fullmatch(name) if type(name) is str else None
    return bool(match and match[3].startswith(('logs/cache/', 'logs/cache_v5/')))


def ignored(name):
    """Only transient ownership/commands, never arbitrary worker files or caches."""
    if name == PREFIX + '.engine.lock':
        return True
    if re.fullmatch(PREFIX + _HEX + r'/jobs/\.submit\.lock', name):
        return True
    if name == PREFIX + 'profiles/.profiles.lock':
        return True
    match = _JOB.fullmatch(name) if type(name) is str else None
    return bool(match and match[3] in ('.transcribe.lock', '.control.lock', 'stop.flag', 'pause.flag',
                                      'settings_live.json'))


def _json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid transcription number.')))


def _scope(db, value, scope):
    text(value, 128)
    if (sha256(value.encode('utf-8')).hexdigest() != scope
            or db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (value,)).fetchone() is None):
        raise ValueError('Transcription checkpoint does not match its saved library scope.')


def _stat_values(value):
    if type(value) is not list or len(value) != 5:
        raise ValueError('Invalid transcription executable binding.')
    for item in value:
        number(item, integer=True, maximum=2**64)


def _engine(value):
    fields(value, {'version', 'python', 'worker', 'models_dir', 'python_sha256',
                   'worker_sha256', 'network', 'inference_qualified'},
           {'protocol', 'python_binding', 'wrapper_sources_sha256', 'retry_policy',
            'runtime_inventory', 'models_inventory', 'fingerprint', 'profile_id',
            'execution_review_required'})
    if (type(value['version']) is not int or value['version'] != 1
            or value['network'] != 'local-files-only; Python network calls denied'
            or value['inference_qualified'] is not False):
        raise ValueError('Invalid passive transcription configuration.')
    protocol = value.get('protocol', RECEIPTS_PROTOCOL)
    settings(protocol=protocol)
    for key in ('python', 'worker', 'models_dir'):
        external_path(value[key])
    for key in ('python_sha256', 'worker_sha256'):
        digest(value[key])
    if 'python_binding' in value:
        binding = value['python_binding']
        fields(binding, {'resolved', 'selected', 'binary'})
        external_path(binding['resolved'])
        _stat_values(binding['selected'])
        _stat_values(binding['binary'])
    if protocol == V5_PROTOCOL:
        _wrappers(value.get('wrapper_sources_sha256'))
        if value['wrapper_sources_sha256']['worker.py'] != value['worker_sha256']:
            raise ValueError('The saved Whisper wrapper identities disagree.')
        text(value.get('retry_policy'), 512)
    elif 'wrapper_sources_sha256' in value or 'retry_policy' in value:
        raise ValueError('A receipt-profile configuration contains V5-only fields.')

    sealed = {'runtime_inventory', 'models_inventory', 'fingerprint'}
    if sealed & value.keys():
        if not sealed <= value.keys():
            raise ValueError('An installed runtime seal is incomplete.')
        from .transcription_runtime import validate_runtime, validate_models
        runtime = validate_runtime(value['runtime_inventory'])
        models = validate_models(value['models_inventory'])
        if (not _same_path(runtime['python_executable'], value['python'])
                or runtime['python_identity']['sha256'] != value['python_sha256']
                or not _same_path(models['root'], value['models_dir'])
                or value['fingerprint'] != configuration_fingerprint(value)):
            raise ValueError('Runtime/model inventories disagree with the selected configuration.')
        if ('python_binding' in value
                and not _same_path(runtime['python_resolved'], value['python_binding']['resolved'])):
            raise ValueError('The operational Python and selected executable binding disagree.')
    if 'execution_review_required' in value and type(value['execution_review_required']) is not bool:
        raise ValueError('Invalid restored execution review flag.')
    if 'profile_id' in value:
        if (not sealed <= value.keys() or type(value['profile_id']) is not str
                or value['profile_id'] != value['fingerprint'][:32]):
            raise ValueError('The selected profile identity differs from its runtime seal.')


def _profile(value, identity):
    fields(value, {'schema', 'id', 'name', 'fingerprint', 'trusted', 'restored',
                   'qualification', 'configuration'})
    if (value['schema'] != 'carr-transcription-profile/1' or value['id'] != identity
            or type(value['trusted']) is not bool or type(value['restored']) is not bool
            or value['qualification'] != 'unqualified'):
        raise ValueError('Invalid passive named runtime profile.')
    text(value['name'], 200)
    digest(value['fingerprint'])
    _engine(value['configuration'])
    if (value['configuration'].get('fingerprint') != value['fingerprint']
            or identity != value['fingerprint'][:32]
            or 'profile_id' in value['configuration']):
        raise ValueError('Named runtime profile changed its exact configuration.')


def _control(value, identity, scope):
    fields(value, {'version', 'job', 'workspace', 'action', 'revision'})
    if (type(value['version']) is not int or value['version'] != 1 or value['job'] != identity
            or type(value['workspace']) is not str
            or sha256(value['workspace'].encode('utf-8')).hexdigest() != scope
            or value['action'] not in ('run', 'pause', 'cancel')
            or type(value['revision']) is not str or not re.fullmatch(_ID, value['revision'])):
        raise ValueError('Invalid durable transcription control.')


def _rejection(value, identity, scope):
    fields(value, {'version', 'job', 'workspace', 'attempt', 'configuration', 'reason'})
    if (type(value['version']) is not int or value['version'] != 1 or value['job'] != identity
            or type(value['workspace']) is not str
            or sha256(value['workspace'].encode('utf-8')).hexdigest() != scope
            or type(value['attempt']) is not str or not re.fullmatch(_ID, value['attempt'])):
        raise ValueError('Invalid rejected transcription execution identity.')
    digest(value['configuration'])
    text(value['reason'], 3000)


def _review(value, identity, scope):
    fields(value, {'schema', 'job', 'workspace', 'attempt', 'configuration'})
    if (value['schema'] != 'carr-transcription-review/1' or value['job'] != identity
            or type(value['workspace']) is not str
            or sha256(value['workspace'].encode('utf-8')).hexdigest() != scope
            or type(value['attempt']) is not str or not re.fullmatch(_ID, value['attempt'])):
        raise ValueError('Invalid pending transcription execution review.')
    digest(value['configuration'])


def _wrappers(value):
    fields(value, set(V5_SOURCES))
    for item in value.values():
        digest(item)


def _file_identity(value):
    fields(value, {'path', 'size', 'mtime_ns', 'ctime_ns', 'device', 'inode', 'sha256'})
    external_path(value['path'])
    number(value['size'], integer=True, maximum=32 * 1024**3)
    for key in ('mtime_ns', 'ctime_ns', 'device', 'inode'):
        number(value[key], integer=True, maximum=2**64)
    digest(value['sha256'])


def _model_identity(value):
    fields(value, {'path', 'files'})
    external_path(value['path'])
    files = value['files']
    if (type(files) is not dict or not {'model.bin', 'config.json', 'tokenizer.json'} <= files.keys()
            or len(files) > 4096):
        raise ValueError('Invalid retained Whisper model identity.')
    total = 0
    for name, item in files.items():
        _relative(name, depth=32, hidden=True)
        _file_identity(item)
        if not _same_path(item['path'], _joined(value['path'], name)):
            raise ValueError('Whisper model identity paths disagree.')
        total += item['size']
    if total > 64 * 1024**3:
        raise ValueError('Whisper model identity exceeds its declared bound.')


def _joined(root, name):
    external_path(root)
    # Historical Windows paths remain Windows references on a POSIX restore.
    kind = PureWindowsPath if PureWindowsPath(root).drive else PurePosixPath
    return str(kind(root).joinpath(*PurePosixPath(name).parts))


def _same_path(left, right):
    external_path(left)
    external_path(right)
    if PureWindowsPath(left).drive or PureWindowsPath(right).drive:
        return PureWindowsPath(left) == PureWindowsPath(right)
    return PurePosixPath(left) == PurePosixPath(right)


def _input_root(value, scope, identity):
    external_path(value)
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if tuple(part.casefold() for part in path.parts[-6:]) != (
            'state', 'transcription', scope, 'jobs', identity, 'input'):
        raise ValueError('Historical transcription input root belongs to another job.')


def _context(value, job, scope):
    if value is None:
        return
    common = {'input_root', 'worker_sha256', 'python_sha256'}
    review = {'configuration_fingerprint', 'model_content_fingerprint'}
    if review & value.keys():
        if not review <= value.keys():
            raise ValueError('The recorded execution review is incomplete.')
        for name in review:
            digest(value[name])
    if job['protocol'] == V5_PROTOCOL:
        fields(value, common | {'protocol', 'host_run_id', 'wrapper_sources_sha256',
                               'model_identity', 'worker_settings', 'source_identities',
                               'result_directory'}, {'manifest_sha256'} | review)
        if value['protocol'] != V5_PROTOCOL or not re.fullmatch(_ID, value['host_run_id']):
            raise ValueError('Invalid Whisper host run.')
        _wrappers(value['wrapper_sources_sha256'])
        if value['wrapper_sources_sha256']['worker.py'] != value['worker_sha256']:
            raise ValueError('Whisper wrapper and worker identities disagree.')
        _model_identity(value['model_identity'])
        if value['worker_settings'] != _v5_settings(job['settings'], value['model_identity']['path']):
            raise ValueError('Whisper requested settings changed.')
        sources = value['source_identities']
        expected = {item['filename']: item for item in job['sources']}
        if type(sources) is not dict or set(sources) != set(expected):
            raise ValueError('Whisper source identity inventory changed.')
        for name, row in sources.items():
            _file_identity(row)
            if (row['size'] != expected[name]['size_bytes'] or row['sha256'] != expected[name]['sha256']
                    or not _same_path(row['path'], _joined(value['input_root'], name))):
                raise ValueError('Whisper source identity changed.')
        if value['result_directory'] != 'runs/' + value['host_run_id'] + '/Whisper_V5/' + job['id']:
            raise ValueError('Whisper result directory belongs to another run.')
        if 'manifest_sha256' in value:
            digest(value['manifest_sha256'])
    else:
        fields(value, common, review)
    _input_root(value['input_root'], scope, job['id'])
    digest(value['worker_sha256'])
    digest(value['python_sha256'])


def _artifact(value, *, accepted=False):
    required = {'path', 'size_bytes', 'sha256'}
    fields(value, required | ({'source', 'format', 'recognition_identity'} if accepted else set()))
    _relative(value['path'])
    kind = _kind(value['path'])
    if kind not in ('txt', 'srt', 'vtt', 'json', 'words.srt', 'words.vtt'):
        raise ValueError('Unknown retained transcript format.')
    number(value['size_bytes'], integer=True, maximum=MAX_METADATA_BYTES)
    digest(value['sha256'])
    if accepted:
        _source_name(value['source'])
        digest(value['recognition_identity'])
        if value['format'] != kind:
            raise ValueError('Transcription format and filename disagree.')


def _job(db, value, scope, identity):
    fields(value, {'version', 'id', 'workspace', 'title', 'created', 'updated', 'status',
                   'sources', 'settings', 'done', 'failed', 'receipts', 'artifacts',
                   'run_context', 'last_error'}, {'protocol', 'worker_run_id', 'effective_runs',
                    'runtime_profile_id', 'runtime_profile_fingerprint',
                    'execution_review_required', 'recovery_review_required'})
    if type(value['version']) is not int or value['version'] != 1 or value['id'] != identity:
        raise ValueError('Invalid transcription job identity.')
    _scope(db, value['workspace'], scope)
    text(value['title'], 200)
    for key in ('created', 'updated'):
        number(value[key])
    if value['status'] not in ('ready', 'running', 'stopped', 'partial', 'failed', 'completed',
                               'paused', 'cancelled', 'interrupted'):
        raise ValueError('Invalid retained transcription status.')
    for key in ('execution_review_required', 'recovery_review_required'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('Invalid transcription recovery review flag.')
    bindings = {'runtime_profile_id', 'runtime_profile_fingerprint'}
    if bindings & value.keys():
        if not bindings <= value.keys():
            raise ValueError('An explicitly bound runtime profile is incomplete.')
        digest(value['runtime_profile_fingerprint'])
        if value['runtime_profile_id'] != value['runtime_profile_fingerprint'][:32]:
            raise ValueError('The job profile identity changed.')
    protocol = value.get('protocol', RECEIPTS_PROTOCOL)
    chosen = settings(value['settings'], protocol=protocol)
    if chosen != value['settings']:
        raise ValueError('Incomplete saved transcription settings.')
    sources = value['sources']
    if type(sources) is not list or not 1 <= len(sources) <= MAX_FILES:
        raise ValueError('Invalid retained source inventory.')
    names, total = set(), 0
    for row in sources:
        fields(row, {'filename', 'original_name', 'size_bytes', 'sha256'})
        name = _source_name(row['filename'])
        # Original labels are never treated as paths to read on this machine.
        text(row['original_name'], 32768)
        number(row['size_bytes'], integer=True, maximum=MAX_SOURCE_BYTES)
        digest(row['sha256'])
        total += row['size_bytes']
        if name in names:
            raise ValueError('Duplicate retained transcription input.')
        names.add(name)
    if total > MAX_BATCH_BYTES:
        raise ValueError('Retained transcription inputs exceed the batch bound.')
    for key in ('done', 'failed'):
        number(value[key], integer=True, maximum=len(sources))
    if (type(value['receipts']) is not list or len(value['receipts']) > len(sources)
            or any(type(name) is not str or not _RECEIPT.fullmatch(name) for name in value['receipts'])
            or len(set(value['receipts'])) != len(value['receipts'])
            or value['done'] != len(value['receipts'])):
        raise ValueError('Invalid accepted transcription receipt list.')
    if type(value['artifacts']) is not list or len(value['artifacts']) > MAX_FILES * 6:
        raise ValueError('Invalid accepted transcription outputs.')
    for row in value['artifacts']:
        _artifact(row, accepted=True)
    if value['status'] == 'completed' and (value['done'] != len(sources) or value['failed']):
        raise ValueError('A completed transcription has incomplete counters.')
    if value['status'] == 'ready' and (value['run_context'] is not None or value['done']
                                     or value['failed'] or value['receipts'] or value['artifacts']):
        raise ValueError('An unstarted transcription contains run authority.')
    text(value['last_error'], 8192, empty=True)
    if 'worker_run_id' in value and value['worker_run_id'] is not None:
        if type(value['worker_run_id']) is not str or not re.fullmatch(_ID, value['worker_run_id']):
            raise ValueError('Invalid saved worker run ID.')
    if 'effective_runs' in value:
        rows = value['effective_runs']
        if type(rows) is not list or len(rows) > len(sources):
            raise ValueError('Invalid effective recognition inventory.')
        seen = set()
        for row in rows:
            fields(row, {'source', 'attempt', 'settings'})
            if row['source'] not in names or row['source'] in seen:
                raise ValueError('Invalid effective recognition source.')
            seen.add(row['source'])
            text(row['attempt'], 64)
            if type(row['settings']) is not dict:
                raise ValueError('Invalid effective recognition settings.')
    normalized = {**value, 'protocol': protocol}
    _context(value['run_context'], normalized, scope)
    return normalized


def _receipt(value, checksum):
    fields(value, {'version', 'state', 'run_id', 'identity', 'requested_exports',
                   'outcome', 'artifacts', 'result_metadata_sha256'},
           {'detected_language', 'language_probability', 'attempt', 'v5_manifest_sha256',
            'v5_record', 'word_subtitle_provenance'})
    if (type(value['version']) is not int or value['version'] != 1 or value['state'] != 'complete'
            or type(value['run_id']) is not str or not re.fullmatch(_ID, value['run_id'])
            or value['outcome'] not in ('speech', 'no_speech')):
        raise ValueError('Invalid saved transcription receipt.')
    identity = value['identity']
    if _identity(identity) != checksum:
        raise ValueError('Transcription receipt filename and identity disagree.')
    fields(identity, {'version', 'source', 'model', 'runtime', 'recognition', 'execution', 'sha256'},
           {'protocol', 'host_run_id', 'effective_settings', 'attempt'})
    if type(identity['version']) is not int or identity['version'] != 1:
        raise ValueError('Invalid recognition identity schema.')
    source = identity['source']
    fields(source, {'path', 'root', 'relative', 'size_bytes', 'sha256'}, {'group'})
    _source_name(source['relative'])
    external_path(source['root'])
    if not _same_path(source['path'], _joined(source['root'], source['relative'])):
        raise ValueError('Recognition source paths disagree.')
    number(source['size_bytes'], integer=True, maximum=MAX_SOURCE_BYTES)
    digest(source['sha256'])
    if 'group' in source:
        text(source['group'], 32768)
    for key in ('model', 'runtime'):
        _identity(identity[key])
    text(identity['model'].get('selected'), 100)
    digest(identity['runtime'].get('worker_sha256'))
    executable = identity['runtime'].get('executable')
    if type(executable) is not dict:
        raise ValueError('Invalid receipt interpreter identity.')
    digest(executable.get('sha256'))
    if type(identity['recognition']) is not dict or set(identity['recognition']) != set(RECOGNITION_KEYS):
        raise ValueError('Invalid recognition settings identity.')
    if type(identity['execution']) is not dict:
        raise ValueError('Invalid recognition execution identity.')
    fields(value['requested_exports'], set(EXPORT_KEYS))
    if type(value['artifacts']) is not list or not 4 <= len(value['artifacts']) <= 6:
        raise ValueError('Invalid transcription receipt outputs.')
    kinds, names = set(), set()
    for row in value['artifacts']:
        _artifact(row)
        kind = _kind(row['path'])
        if kind in kinds or row['path'] in names:
            raise ValueError('Duplicate receipt output.')
        kinds.add(kind)
        names.add(row['path'])
        if kind == 'json' and row['sha256'] != value['result_metadata_sha256']:
            raise ValueError('Receipt metadata digest disagrees.')
    required = {'txt', 'srt', 'vtt', 'json'}
    if value['requested_exports']['word_timestamps']:
        required |= {'words.srt', 'words.vtt'}
    if kinds != required:
        raise ValueError('A requested transcription format is missing.')
    digest(value['result_metadata_sha256'])
    if 'detected_language' in value and value['detected_language'] is not None:
        text(value['detected_language'], 128, empty=True)
    if 'language_probability' in value and value['language_probability'] is not None:
        number(value['language_probability'], maximum=1)
    if identity.get('protocol') == V5_PROTOCOL:
        if identity.get('host_run_id') != value['run_id']:
            raise ValueError('Whisper normalized receipt changed its host run.')
        digest(value.get('v5_manifest_sha256'))
        number(value.get('v5_record'), integer=True, maximum=MAX_FILES - 1)
        _wrappers(identity['runtime'].get('wrapper_sources_sha256'))
        _model_identity(identity['model'].get('identity'))
        text(value.get('word_subtitle_provenance'), 512)
    elif any(key in value for key in ('v5_manifest_sha256', 'v5_record', 'word_subtitle_provenance')):
        raise ValueError('A receipt contains foreign protocol authority.')


def _edit(value, identity, revision):
    fields(value, {'version', 'id', 'job', 'source', 'source_sha256',
                   'recognition_identity', 'original_text_sha256', 'created', 'text', 'kind'})
    if (type(value['version']) is not int or value['version'] != 1 or value['id'] != revision
            or value['job'] != identity
            or value['kind'] != 'user-edited derivative; source and timed recognition remain unchanged'):
        raise ValueError('Invalid transcription edit identity.')
    _source_name(value['source'])
    for key in ('source_sha256', 'recognition_identity', 'original_text_sha256'):
        digest(value[key])
    number(value['created'])
    text(value['text'], MAX_TEXT_BYTES, empty=True)
    if len(value['text'].encode('utf-8')) > MAX_TEXT_BYTES:
        raise ValueError('Transcription edit exceeds its byte allowance.')


def _status(value, *, host=False):
    if type(value) is not dict:
        raise ValueError('Invalid transcription status.')
    for key in ('done', 'total', 'failed'):
        number(value.get(key), integer=True, maximum=MAX_FILES)
    if 'run_id' in value:
        if type(value['run_id']) is not str or not re.fullmatch(_ID, value['run_id']):
            raise ValueError('Invalid worker status run.')
        if type(value.get('receipts')) is not list or len(value['receipts']) > MAX_FILES:
            raise ValueError('Invalid worker status receipts.')
        if any(type(name) is not str or not _RECEIPT.fullmatch(name) for name in value['receipts']):
            raise ValueError('Invalid worker status receipt path.')
        if len(set(value['receipts'])) != len(value['receipts']):
            raise ValueError('Duplicate worker status receipt.')
        if value.get('result_state') not in ('running', 'stopped', 'partial', 'completed', 'failed'):
            raise ValueError('Invalid worker result status.')
    elif host:
        raise ValueError('Missing normalized host run identity.')
    else:
        external_path(value.get('results_dir'))
    if 'state' in value:
        text(value['state'], 4096, empty=True)


def _manifest(value):
    fields(value, {'format', 'version', 'app_version', 'complete', 'total', 'failed',
                   'runtime_provenance', 'wrapper_sources_sha256', 'records', 'artifacts'})
    if (value['format'] != 'whisper-result-manifest' or type(value['version']) is not int
            or value['version'] != 1 or value['app_version'] != '5.0.0'
            or value['runtime_provenance'] != V5_PROVENANCE or type(value['complete']) is not bool):
        raise ValueError('Unsupported Whisper result manifest.')
    number(value['total'], integer=True, minimum=1, maximum=MAX_FILES)
    number(value['failed'], integer=True, maximum=value['total'])
    if (value['complete'] != (value['failed'] == 0) or type(value['records']) is not list
            or len(value['records']) != value['total'] - value['failed']
            or type(value['artifacts']) is not list or len(value['artifacts']) > MAX_FILES * 5):
        raise ValueError('Whisper manifest counters disagree.')
    _wrappers(value['wrapper_sources_sha256'])
    for row in value['artifacts']:
        fields(row, {'path', 'bytes', 'sha256'})
        _relative(row['path'], depth=2)
        if _kind(row['path']) not in ('txt', 'srt', 'vtt', 'json', 'words.txt'):
            raise ValueError('Unknown Whisper output format.')
        number(row['bytes'], integer=True, maximum=MAX_METADATA_BYTES)
        digest(row['sha256'])
    for row in value['records']:
        fields(row, {'source_identity', 'model_identity', 'backend', 'model_runtime', 'fingerprint',
                     'metadata_sha256', 'requested_settings', 'effective_settings', 'attempt', 'artifacts'})
        _file_identity(row['source_identity'])
        _model_identity(row['model_identity'])
        for key in ('fingerprint', 'metadata_sha256'):
            digest(row[key])
        if (type(row['backend']) is not dict or len(row['backend']) > 32
                or any(type(key) is not str or type(item) is not str or len(item) > 4096
                       for key, item in row['backend'].items())
                or type(row['artifacts']) is not list or not 4 <= len(row['artifacts']) <= 5):
            raise ValueError('Invalid Whisper provenance or outputs.')


def validate(db, name, raw):
    member = _member(name)
    if member is None or type(raw) is not bytes or len(raw) > limit(name):
        raise ValueError('Unknown or excessive transcription backup file.')
    kind, scope, identity, tail = member
    if not raw and not allow_empty(name):
        raise ValueError('Empty transcription metadata.')
    if kind == 'input':
        return
    if kind == 'result' and not tail.endswith('.json'):
        text(raw.decode('utf-8-sig'), MAX_METADATA_BYTES, empty=True)
        return
    value = _json(raw)
    if kind == 'engine':
        _engine(value)
    elif kind == 'profile':
        _profile(value, identity)
    elif kind == 'control.json':
        _scope(db, value.get('workspace'), scope)
        _control(value, identity, scope)
    elif kind == 'rejected-execution.json':
        _scope(db, value.get('workspace'), scope)
        _rejection(value, identity, scope)
    elif kind == 'review':
        _scope(db, value.get('workspace'), scope)
        _review(value, identity, scope)
    elif kind == 'job.json':
        _job(db, value, scope, identity)
    elif kind == 'receipt':
        _receipt(value, _RECEIPT.fullmatch(tail)[1])
    elif kind == 'edit':
        _edit(value, identity, _EDIT.fullmatch(tail)[1])
    elif kind in ('job_status.json', 'host_status.json'):
        _status(value, host=kind == 'host_status.json')
    elif kind == 'job_config.json':
        fields(value, {'job_name', 'folders', 'results_root', 'models_dir', 'settings'})
        text(value['job_name'], 200)
        if type(value['folders']) is not list or len(value['folders']) != 1:
            raise ValueError('Invalid transcription job configuration sources.')
        for item in (value['folders'][0], value['results_root'], value['models_dir']):
            external_path(item)
        if type(value['settings']) is not dict:
            raise ValueError('Invalid transcription job configuration settings.')
    elif kind == 'manifest':
        _manifest(value)
    elif kind == 'result':
        if type(value) is not dict:
            raise ValueError('Timed transcription JSON must be an object.')
        _v5_timing(value, False)


def _cancel(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Transcription backup validation was cancelled.')


def _read(stage, entry, cancelled):
    # The core has copied and hashed all members. Recheck metadata before using
    # it as a cross-file authority; paths are archive members, never saved paths.
    from .backup_sidecars import _regular, _identity as stamp
    target = Path(stage).joinpath(*PurePosixPath(entry['filename']).parts)
    before = _regular(target)
    if before.st_size != entry['size_bytes'] or before.st_size > limit(entry['filename']):
        raise ValueError('Staged transcription metadata changed size.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    parts, size, checksum = [], 0, sha256()
    with os.fdopen(os.open(target, flags), 'rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('Staged transcription metadata changed before reading.')
        while block := stream.read(min(1024**2, before.st_size - size + 1)):
            _cancel(cancelled)
            size += len(block)
            if size > before.st_size:
                raise ValueError('Staged transcription metadata grew.')
            parts.append(block)
            checksum.update(block)
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('Staged transcription metadata changed while reading.')
    if (size != before.st_size or checksum.hexdigest() != entry['sha256']
            or stamp(_regular(target)) != stamp(before)):
        raise ValueError('Staged transcription metadata hash changed.')
    _cancel(cancelled)
    return b''.join(parts)


def _matches(entry, size, checksum):
    return entry is not None and entry['size_bytes'] == size and entry['sha256'] == checksum


def _receipt_source(receipt, job, scope, *, current):
    identity = receipt['identity']
    source = identity['source']
    name = source['relative']
    original = next((row for row in job['sources'] if row['filename'] == name), None)
    if (original is None or source['size_bytes'] != original['size_bytes']
            or source['sha256'] != original['sha256']):
        raise ValueError('A transcription receipt belongs to another retained source.')
    _input_root(source['root'], scope, job['id'])
    if (identity['recognition'] != {key: job['settings'][key] for key in RECOGNITION_KEYS}
            or receipt['requested_exports'] != {key: job['settings'][key] for key in EXPORT_KEYS}
            or identity['model']['selected'] != job['settings']['model']
            or identity.get('protocol', RECEIPTS_PROTOCOL) != job['protocol']):
        raise ValueError('A transcription receipt changed its requested recognition profile.')
    if not current:
        return
    context = job['run_context']
    if (context is None or receipt['run_id'] != job.get('worker_run_id')
            or source['root'] != context['input_root']
            or identity['runtime']['worker_sha256'] != context['worker_sha256']
            or identity['runtime']['executable']['sha256'] != context['python_sha256']):
        raise ValueError('An accepted transcription receipt belongs to another run.')
    if job['protocol'] == V5_PROTOCOL:
        effective = next((row for row in job.get('effective_runs', ()) if row['source'] == name), None)
        if (identity['host_run_id'] != context['host_run_id']
                or identity['runtime']['wrapper_sources_sha256'] != context['wrapper_sources_sha256']
                or identity['model']['identity'] != context['model_identity']
                or receipt['v5_manifest_sha256'] != context.get('manifest_sha256')
                or effective is None or identity.get('effective_settings') != effective['settings']
                or identity.get('attempt') != effective['attempt']):
            raise ValueError('An accepted Whisper receipt changed its current-run provenance.')


def _manifest_collection(job, tail, manifest, entry, members, values, receipts, assigned, cancelled):
    relative_directory = str(PurePosixPath(tail).parent)[len('results/'):]
    run = _V5_RESULT.fullmatch(relative_directory + '/result_manifest.json')[1]
    bound = [receipt for receipt in receipts.values()
             if receipt['identity'].get('protocol') == V5_PROTOCOL
             and receipt.get('v5_manifest_sha256') == entry['sha256']
             and receipt['identity']['host_run_id'] == run]
    current = job['run_context']
    if current is not None and current.get('result_directory') == relative_directory:
        context = current
        if context.get('manifest_sha256', entry['sha256']) != entry['sha256']:
            raise ValueError('The current Whisper result manifest changed.')
    elif bound:
        identity = bound[0]['identity']
        context = {
            'host_run_id': run, 'input_root': identity['source']['root'],
            'model_identity': identity['model']['identity'],
            'worker_sha256': identity['runtime']['worker_sha256'],
            'python_sha256': identity['runtime']['executable']['sha256'],
            'wrapper_sources_sha256': identity['runtime']['wrapper_sources_sha256'],
            'worker_settings': _v5_settings(job['settings'], identity['model']['identity']['path']),
        }
    else:
        # A stopped attempt can leave a complete file manifest without host
        # admission. Later explicit runs replace run_context. Keep that typed
        # history as data, never manufacture a receipt or a completed counter.
        if not manifest['records']:
            if manifest['artifacts'] or manifest['total'] != len(job['sources']):
                raise ValueError('An empty historical Whisper manifest has outputs or changed sources.')
            assigned.add(tail)
            return
        first = manifest['records'][0]
        source_path = first['source_identity']['path']
        path_type = PureWindowsPath if PureWindowsPath(source_path).drive else PurePosixPath
        root = str(path_type(source_path).parent)
        _input_root(root, sha256(job['workspace'].encode('utf-8')).hexdigest(), job['id'])
        context = {
            'host_run_id': run, 'input_root': root, 'model_identity': first['model_identity'],
            'wrapper_sources_sha256': manifest['wrapper_sources_sha256'],
            'worker_settings': _v5_settings(job['settings'], first['model_identity']['path']),
        }
    if (manifest['total'] != len(job['sources'])
            or manifest['wrapper_sources_sha256'] != context['wrapper_sources_sha256']):
        raise ValueError('Whisper manifest changed its retained inputs or wrapper identities.')
    outputs = {}
    for row in manifest['artifacts']:
        name = row['path']
        target = str(PurePosixPath(tail).parent / name)
        if name in outputs or not _matches(members.get(target), row['bytes'], row['sha256']):
            raise ValueError('A Whisper manifest output is missing or changed.')
        outputs[name] = row
        assigned.add(target)
    requested = context['worker_settings']
    attempts = _v5_attempts(requested)
    required = {'txt', 'srt', 'vtt', 'json'} | ({'words.txt'} if requested['output_words'] else set())
    sources = {row['filename']: row for row in job['sources']}
    seen, used = set(), set()
    for index, row in enumerate(manifest['records']):
        _cancel(cancelled)
        source = row['source_identity']
        matches = [name for name in sources if _same_path(source['path'], _joined(context['input_root'], name))]
        if len(matches) != 1:
            raise ValueError('Whisper manifest source has no retained original.')
        name = matches[0]
        if (name in seen or source['size'] != sources[name]['size_bytes']
                or source['sha256'] != sources[name]['sha256']
                or row['model_identity'] != context['model_identity']
                or 'source_identities' in context and source != context['source_identities'][name]):
            raise ValueError('Whisper manifest source/model identity changed.')
        selected = attempts.get(row['attempt'])
        effective = ({key: selected[key] for key in V5_RECOGNITION_KEYS}
                     | {'word_timestamps': requested['output_words']}) if selected else None
        runtime = {'device': requested['device'], 'compute_type': requested['compute_type'],
                   'parallel_files': requested['parallel_files']}
        if runtime['compute_type'] == 'auto':
            runtime['compute_type'] = 'float16' if runtime['device'] == 'cuda' else 'int8'
        if (selected is None or row['requested_settings'] != requested
                or row['effective_settings'] != effective or row['model_runtime'] != runtime):
            raise ValueError('Whisper manifest requested/effective settings changed.')
        kinds = {}
        for item in row['artifacts']:
            if type(item) is not str:
                raise ValueError('Invalid Whisper artifact name.')
            kind = _kind(item)
            if item not in outputs or item in used or kind not in required or kind in kinds:
                raise ValueError('Whisper manifest contains shared or duplicate outputs.')
            kinds[kind] = outputs[item]
            used.add(item)
        if set(kinds) != required:
            raise ValueError('A requested Whisper export is missing.')
        meta_name = str(PurePosixPath(tail).parent / kinds['json']['path'])
        meta = values[meta_name]
        cache = {'schema': 5, 'content_identity_version': 1,
                 'source': source, 'model': context['model_identity'],
                 'recognition': {key: requested[key] for key in V5_RECOGNITION_KEYS},
                 'backend': row['backend'], 'wrapper_sources_sha256': context['wrapper_sources_sha256'],
                 'runtime_provenance': V5_PROVENANCE}
        if (meta.get('cache_identity') != cache or row['fingerprint'] != _v5_digest(cache)
                or meta.get('fingerprint') != row['fingerprint']
                or meta.get('metadata_sha256') != row['metadata_sha256']
                or _v5_digest({key: item for key, item in meta.items() if key != 'metadata_sha256'}) != row['metadata_sha256']
                or meta.get('cache_schema') != 5 or meta.get('complete') is not True
                or meta.get('source_identity') != source or meta.get('model_identity') != context['model_identity']
                or meta.get('requested_settings') != requested or meta.get('settings') != selected
                or meta.get('attempt') != row['attempt'] or meta.get('model_runtime') != runtime
                or meta.get('task') != 'transcribe' or meta.get('selected_language') != requested['language']
                or meta.get('multilingual') is not requested['multilingual']
                or meta.get('word_timestamps') is not requested['output_words']):
            raise ValueError('Whisper timed metadata changed its bound source, model or settings.')
        recognized = meta.get('text')
        text(recognized, MAX_TEXT_BYTES, empty=True)
        if len(recognized.encode('utf-8')) > MAX_TEXT_BYTES:
            raise ValueError('Whisper transcript exceeds its byte allowance.')
        text_hash = sha256((recognized + ('\n' if recognized else '')).encode('utf-8')).hexdigest()
        if meta.get('text_sha256') != text_hash or kinds['txt']['sha256'] != text_hash:
            raise ValueError('Whisper text and timed metadata disagree.')
        words = _v5_timing(meta, requested['output_words'])
        candidates = [receipt for receipt in bound if receipt['v5_record'] == index]
        if len(candidates) > 1:
            raise ValueError('Duplicate normalized Whisper record receipt.')
        if candidates:
            receipt = candidates[0]
            expected_identity = _bound({
                'version': 1, 'protocol': V5_PROTOCOL, 'host_run_id': run,
                'source': {'root': context['input_root'], 'path': source['path'], 'relative': name,
                           'size_bytes': source['size'], 'sha256': source['sha256']},
                'model': _bound({'selected': job['settings']['model'], 'identity': context['model_identity']}),
                'runtime': _bound({'worker_sha256': context['worker_sha256'],
                    'executable': {'sha256': context['python_sha256']},
                    'wrapper_sources_sha256': context['wrapper_sources_sha256'],
                    'backend': row['backend'], 'runtime_provenance': V5_PROVENANCE}),
                'recognition': {key: job['settings'][key] for key in RECOGNITION_KEYS},
                'effective_settings': effective, 'attempt': row['attempt'], 'execution': runtime})
            expected_artifacts = [
                {'path': relative_directory + '/' + kinds[kind]['path'],
                 'size_bytes': kinds[kind]['bytes'], 'sha256': kinds[kind]['sha256']}
                for kind in ('txt', 'srt', 'vtt', 'json')]
            if requested['output_words']:
                for kind in ('srt', 'vtt'):
                    data = _word_subtitles(words, vtt=kind == 'vtt')
                    output_name = relative_directory + '/host-derived/' + name + '.words.' + kind
                    expected_artifacts.append({'path': output_name, 'size_bytes': len(data),
                                               'sha256': sha256(data).hexdigest()})
            if (receipt['identity'] != expected_identity or receipt['artifacts'] != expected_artifacts
                    or receipt['outcome'] != ('speech' if recognized.strip() else 'no_speech')
                    or receipt['result_metadata_sha256'] != kinds['json']['sha256']):
                raise ValueError('A normalized Whisper receipt disagrees with the original manifest.')
            for artifact in expected_artifacts:
                target = 'results/' + artifact['path']
                if not _matches(members.get(target), artifact['size_bytes'], artifact['sha256']):
                    raise ValueError('A normalized Whisper derivative changed.')
                assigned.add(target)
        seen.add(name)
    if used != set(outputs) or any(receipt['v5_record'] >= len(manifest['records']) for receipt in bound):
        raise ValueError('Whisper manifest has unassigned outputs or records.')
    assigned.add(tail)


def validate_collection(db, stage, entries, *, cancelled):
    """Check a fully staged snapshot without replaying any execution authority."""
    _cancel(cancelled)
    groups, profiles, engines, total_media, total_metadata = {}, {}, [], 0, 0
    for entry in entries:
        name = entry['filename']
        if not name.startswith(PREFIX):
            continue
        member = _member(name)
        if member is None:
            raise ValueError('Unknown transcription backup member.')
        kind, scope, identity, tail = member
        fields(entry, {'filename', 'size_bytes', 'sha256'})
        number(entry['size_bytes'], integer=True, maximum=limit(name))
        digest(entry['sha256'])
        if entry['size_bytes'] == 0 and not allow_empty(name):
            raise ValueError('Empty transcription backup metadata.')
        if kind == 'input':
            total_media += entry['size_bytes']
        else:
            total_metadata += entry['size_bytes']
        if total_media > MAX_MEDIA_BYTES or total_metadata > MAX_METADATA_BYTES:
            raise ValueError('Transcription backup exceeds its retained data budget.')
        if kind in ('engine', 'profile'):
            raw = _read(stage, entry, cancelled)
            validate(db, name, raw)
            value = _json(raw)
            if kind == 'profile':
                profiles[identity] = value
                if len(profiles) > 1024:
                    raise ValueError('Too many passive runtime profiles.')
            else:
                engines.append(value)
            continue
        members = groups.setdefault((scope, identity), {})
        if tail in members:
            raise ValueError('Duplicate transcription backup member.')
        members[tail] = entry
    for engine in engines:
        if 'profile_id' in engine:
            profile = profiles.get(engine['profile_id'])
            if profile is None or profile['fingerprint'] != engine['fingerprint']:
                raise ValueError('The selected runtime lost its exact named declaration.')
    scopes = {}
    for scope, _ in groups:
        scopes[scope] = scopes.get(scope, 0) + 1
        if scopes[scope] > 10000:
            raise ValueError('Transcription backup exceeds the per-library job bound.')
    for (scope, identity), members in groups.items():
        _cancel(cancelled)
        if 'job.json' not in members:
            raise ValueError('Retained transcription data has no owning job.')
        values = {}
        for tail, entry in members.items():
            _cancel(cancelled)
            kind = _member(entry['filename'])[0]
            if kind != 'input' and tail.endswith('.json'):
                raw = _read(stage, entry, cancelled)
                validate(db, entry['filename'], raw)
                values[tail] = _json(raw)
        job = _job(db, values['job.json'], scope, identity)
        if 'runtime_profile_id' in job:
            profile = profiles.get(job['runtime_profile_id'])
            if profile is None or profile['fingerprint'] != job['runtime_profile_fingerprint']:
                raise ValueError('A saved transcription job lost its named runtime declaration.')
        sources = {row['filename']: row for row in job['sources']}
        expected_inputs = {'input/' + name for name in sources}
        if {tail for tail in members if tail.startswith('input/')} != expected_inputs:
            raise ValueError('The retained transcription input inventory changed.')
        for name, source in sources.items():
            if not _matches(members['input/' + name], source['size_bytes'], source['sha256']):
                raise ValueError('A retained original changed its exact source hash.')
        assigned = set(expected_inputs)
        receipts = {tail: value for tail, value in values.items() if _RECEIPT.fullmatch(tail)}
        for name, receipt in receipts.items():
            _receipt_source(receipt, job, scope, current=name in job['receipts'])
            for artifact in receipt['artifacts']:
                tail = 'results/' + artifact['path']
                if _matches(members.get(tail), artifact['size_bytes'], artifact['sha256']):
                    assigned.add(tail)
                elif name in job['receipts']:
                    raise ValueError('An accepted transcription result is missing or changed.')
        if not set(job['receipts']) <= receipts.keys():
            raise ValueError('An accepted transcription receipt is missing.')
        accepted, seen = [], set()
        for name in job['receipts']:
            receipt = receipts[name]
            source = receipt['identity']['source']['relative']
            if source in seen:
                raise ValueError('Current receipts share a retained source.')
            seen.add(source)
            for artifact in receipt['artifacts']:
                accepted.append({**artifact, 'source': source, 'format': _kind(artifact['path']),
                                 'recognition_identity': receipt['identity']['sha256']})
        if accepted != job['artifacts']:
            raise ValueError('Saved transcription artifacts disagree with their receipts.')
        status_name = 'host_status.json' if job['protocol'] == V5_PROTOCOL else 'job_status.json'
        if job['receipts'] or job['status'] in ('partial', 'completed'):
            status = values.get(status_name)
            if (status is None or status.get('run_id') != job.get('worker_run_id')
                    or status.get('receipts') != job['receipts']
                    or status.get('done') != job['done'] or status.get('total') != len(sources)
                    or job['status'] == 'completed' and (
                        status.get('result_state') != 'completed' or status.get('failed') != 0)):
                raise ValueError('Accepted transcription status changed its verified run.')
        if 'job_config.json' in values:
            config, context = values['job_config.json'], job['run_context']
            if context is None:
                raise ValueError('An unstarted job contains a worker configuration.')
            expected_settings = context['worker_settings'] if job['protocol'] == V5_PROTOCOL else job['settings']
            path_type = PureWindowsPath if PureWindowsPath(context['input_root']).drive else PurePosixPath
            result_root = _joined(str(path_type(context['input_root']).parent), 'results')
            if job['protocol'] == V5_PROTOCOL:
                result_root = _joined(result_root, 'runs/' + context['host_run_id'])
            if (config['job_name'] != job['title'] or config['folders'] != [context['input_root']]
                    or config['settings'] != expected_settings
                    or not _same_path(config['results_root'], result_root)):
                raise ValueError('Worker configuration changed its saved job or requested settings.')
        for tail, manifest in values.items():
            if _member(members[tail]['filename'])[0] == 'manifest':
                if job['protocol'] != V5_PROTOCOL:
                    raise ValueError('A receipt-profile job contains a V5 result manifest.')
                _manifest_collection(job, tail, manifest, members[tail], members, values,
                                     receipts, assigned, cancelled)
        for name, receipt in receipts.items():
            if receipt['identity'].get('protocol') == V5_PROTOCOL:
                run = receipt['identity']['host_run_id']
                manifest_name = 'results/runs/' + run + '/Whisper_V5/' + identity + '/result_manifest.json'
                if not _matches(members.get(manifest_name),
                                members.get(manifest_name, {}).get('size_bytes'),
                                receipt['v5_manifest_sha256']):
                    raise ValueError('A normalized Whisper receipt lost its bound manifest.')
        for tail, edit in values.items():
            if _EDIT.fullmatch(tail):
                source = sources.get(edit['source'])
                original = receipts.get('receipts/' + edit['recognition_identity'] + '.result.json')
                transcript = next((row for row in original['artifacts'] if _kind(row['path']) == 'txt'), None) if original else None
                if (source is None or source['sha256'] != edit['source_sha256']
                        or original is None or original['identity']['source']['relative'] != edit['source']
                        or transcript is None or transcript['sha256'] != edit['original_text_sha256']):
                    raise ValueError('A transcription edit lost its exact original recognition.')
        if {tail for tail in members if tail.startswith('results/')} - assigned:
            raise ValueError('Unbound transcription worker results need a supported producer adapter.')
        _cancel(cancelled)


def revoke_restored_execution(db, stage, entries, *, cancelled=lambda: False):
    """Revoke execution only after the source snapshot was fully authenticated.

    The parent restore publishes new checksums after this transformation. Recorded
    selectors, model inventories, completed receipts, edits and worker evidence
    remain passive data; no external path is resolved or opened here.
    """
    from .durable_state import atomic_json
    stage = Path(stage)
    pending = []
    rejected, uncertain = set(), set()
    for entry in entries:
        _cancel(cancelled)
        member = _member(entry['filename'])
        if member is None:
            continue
        kind, scope, identity, tail = member
        if kind not in ('engine', 'profile', 'job.json', 'control.json', 'rejected-execution.json', 'review'):
            continue
        raw = _read(stage, entry, cancelled)
        validate(db, entry['filename'], raw)
        value = _json(raw)
        if kind == 'rejected-execution.json':
            rejected.add((scope, identity))
            continue
        if kind == 'review':
            # Retain this host-owned veto exactly. It grants neither a live
            # process nor acceptance, and may represent a failed rejection write.
            uncertain.add((scope, identity))
            continue
        pending.append((entry['filename'], kind, scope, identity, value))
    for name, kind, scope, identity, value in pending:
        _cancel(cancelled)
        if kind == 'engine':
            value['execution_review_required'] = True
        elif kind == 'profile':
            value.update(trusted=False, restored=True)
        elif kind == 'control.json':
            # Restored intent cannot silently start or keep running a worker.
            value['action'] = 'pause'
        else:
            value['execution_review_required'] = True
            if value['status'] in ('running', 'interrupted') or (scope, identity) in uncertain:
                value.update(status='interrupted', recovery_review_required=True)
            if (scope, identity) in rejected:
                value.update(status='failed', done=0, failed=len(value['sources']),
                             receipts=[], artifacts=[],
                             last_error='Rejected worker evidence was restored without execution authority.')
        validate(db, name, (json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8'))
        atomic_json(stage / name, value, replace=True)
    _cancel(cancelled)
