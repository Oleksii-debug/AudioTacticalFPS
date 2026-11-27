"""Typed snapshots of managed supplied-worker transcription jobs.

Only owned relative inputs and data beneath the registered job namespace are
copied. Runtime/model manifests and original provenance paths remain inert.
Historical receipts are validated as data; they never grant execution or
publication authority at the restored location.
"""
from contextlib import contextmanager, ExitStack
from collections import Counter
from datetime import datetime
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import unicodedata

from .audio.jobs import checked_directory, project_lock, read_json, regular_file
from .audio.model_manifest import _read_json_snapshot, digest_file, local_path
from .transcription_receipt import (PROTOCOL, OUTPUT_SETTINGS_KEYS, DEFAULT_SETTINGS,
    canonical_digest, validate_record, validate_settings)
from .transcription_state import (DIRECTORY, SCHEMA, MAX_INPUTS, MAX_ENTRIES,
    MAX_JOB_BYTES, MAX_JSON_BYTES, MAX_LOG_BYTES, EXTENSIONS, JOB_ID,
    check_cancelled, content_hash, relative_path, regular, read_definition,
    validate_definition)

MAX_JOBS = 1000
MAX_FILES = 100000
MAX_SNAPSHOT_BYTES = 128 * 1024**3
MAX_RECORD_BYTES = 64 * 1024**2
MAX_OUTPUT_BYTES = 4 * 1024**3
MAX_SEGMENTS = 250000
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_OWNER = re.compile(r'[0-9a-f]{24}\Z')
_STAGE = re.compile(r'\.transactions/staging-[0-9a-f]{32}/[0-9]{6}\.part\Z')
_TEMPORARY = re.compile(r'\.(?:checkpoint|publish|writing)-[A-Za-z0-9_.-]{1,128}\Z')
_EXECUTION_NAMES = frozenset(('job_config.json', 'job_status.json', 'execution.json',
                              'completion-v1.json', 'settings_live.json'))
_ROOT_NAMES = frozenset(('job.json', 'job_config.json', 'job_status.json',
                         'execution.json', 'settings_live.json'))
_ROOT_TRANSIENT = frozenset(('.render.lock', 'pause.flag', 'stop.flag'))


def _error(message):
    raise ValueError('Transcription backup: ' + message)


def _fields(value, names, label):
    if type(value) is not dict or set(value) != set(names):
        _error(label + ' has unknown or missing fields.')
    return value


def _count(value, maximum, label, *, empty=True):
    if type(value) is not int or not (0 if empty else 1) <= value <= maximum:
        _error(label + ' is outside its bounded integer range.')
    return value


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        _error(label + ' is not a finite nonnegative time.')
    return value


def _absolute(value):
    # Foreign-platform metadata must be portable without resolving/opening it.
    from .reader.narration import _original_path
    if type(value) is not str:
        _error('An archived absolute location is invalid.')
    _original_path(value)
    result = PureWindowsPath(value) if PureWindowsPath(value).is_absolute() else PurePosixPath(value)
    if not result.is_absolute() or any(part in ('.', '..') for part in result.parts):
        _error('An archived absolute location is invalid.')
    return result


def _relative_to(value, root):
    value, root = _absolute(value), _absolute(root)
    try:
        parts = value.relative_to(root).parts
    except ValueError:
        _error('An archived path escapes its captured job root.')
    return relative_path('/'.join(parts))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(path, *, directory=False, linked=False):
    path = local_path(path)
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or not directory and info.st_nlink != 1 and not (linked and info.st_nlink == 2)):
        _error('A snapshot member is not an ordinary owned local file or directory.')
    return info


def _split(value):
    if type(value) is not str:
        return None
    parts = PurePosixPath(value).parts
    if (len(parts) < 3 or parts[0] != DIRECTORY or not JOB_ID.fullmatch(parts[1])
            or '/'.join(parts) != value):
        return None
    try:
        tail = relative_path('/'.join(parts[2:]))
    except ValueError:
        return None
    return parts[1], tail


def _result_parts(tail):
    parts = tail.parts
    if len(parts) < 4 or parts[:2] != ('results', 'verified-v43') or not _OWNER.fullmatch(parts[2]):
        return None
    return '/'.join(parts[:3]), PurePosixPath(*parts[3:])


def _output_path(value):
    path = relative_path(value)
    if (not path.parts or path.parts[0].startswith('.')
            or any(part in ('.receipts', '.transactions') for part in path.parts)
            or path.as_posix() == 'owner.json'
            or path.suffix.lower() not in ('.txt', '.srt', '.vtt', '.json')):
        _error('A declared output path is outside its text-output namespace.')
    return path


def is_state_path(value):
    split = _split(value)
    if split is None:
        return False
    _, tail = split
    parts = tail.parts
    if len(parts) == 1:
        return parts[0] in _ROOT_NAMES
    if parts[0] == 'history':
        return len(parts) == 3 and bool(JOB_ID.fullmatch(parts[1])) and parts[2] in _EXECUTION_NAMES
    if parts[0] == 'inputs':
        return (len(parts) >= 3 and re.fullmatch(r'root-[0-9]{1,4}', parts[1]) is not None
                and tail.suffix.lower() in EXTENSIONS)
    if parts[0] == 'logs':
        if parts[1] == 'integrity-v1':
            if len(parts) == 4 and parts[2] == 'engines':
                return tail.suffix == '.json' and bool(_HASH.fullmatch(tail.stem))
            if len(parts) == 5 and parts[2] == 'inference':
                return (tail.suffix == '.json' and bool(_HASH.fullmatch(tail.stem))
                        and parts[3] == tail.stem[:2])
            return False
        return all(not part.startswith('.') for part in parts[1:]) and tail.suffix.lower() in ('.json', '.log', '.txt')
    result = _result_parts(tail)
    if result is None:
        return False
    _, relative = result
    if relative.as_posix() == 'owner.json':
        return True
    if relative.parts[0] in ('.receipts', '.transactions'):
        return ((len(relative.parts) == 2 and relative.suffix == '.json' and bool(_HASH.fullmatch(relative.stem)))
                or bool(_STAGE.fullmatch(relative.as_posix())))
    try:
        _output_path(relative.as_posix())
        return True
    except ValueError:
        return False


def state_limit(value):
    split = _split(value)
    if split is None or not is_state_path(value):
        _error('Unsupported snapshot member.')
    _, tail = split
    if tail.parts[0] == 'inputs':
        return MAX_JOB_BYTES
    if tail.parts[:2] == ('logs', 'integrity-v1'):
        return MAX_RECORD_BYTES
    result = _result_parts(tail)
    if result is not None:
        _, relative = result
        return MAX_RECORD_BYTES if relative.as_posix() == 'owner.json' or relative.parts[0] == '.receipts' or (
            relative.parts[0] == '.transactions' and relative.suffix == '.json') else MAX_OUTPUT_BYTES
    return MAX_LOG_BYTES if tail.suffix.lower() in ('.log', '.txt') else MAX_JSON_BYTES


def allows_empty(value):
    split = _split(value)
    if split is None:
        return False
    _, tail = split
    result = _result_parts(tail)
    if result is not None:
        _, relative = result
        return bool(_STAGE.fullmatch(relative.as_posix())) or not relative.parts[0].startswith('.') and relative.as_posix() != 'owner.json'
    return tail.parts[0] == 'logs' and tail.suffix.lower() in ('.log', '.txt')


def link_path(value):
    split = _split(value)
    if split is None:
        return False
    result = _result_parts(split[1])
    if result is None:
        return False
    _, relative = result
    return bool(_STAGE.fullmatch(relative.as_posix())) or (
        not relative.parts[0].startswith('.') and relative.as_posix() != 'owner.json')


def _read(path, *, maximum=MAX_RECORD_BYTES):
    regular(path)
    return read_json(path, limit=maximum)


def _definition(job):
    return validate_definition(_read(job / 'job.json', maximum=MAX_JSON_BYTES), identity=job.name)


def _jobs(root, *, cancelled=lambda: False):
    folder = Path(root) / DIRECTORY
    if not os.path.lexists(folder):
        return ()
    _plain(folder, directory=True)
    jobs = []
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            check_cancelled(cancelled)
            if count > MAX_JOBS + 32:
                _error('The job directory exceeds its bounded entry count.')
            path = Path(entry.path)
            if entry.name == '.render.lock':
                if _plain(path).st_size > 16:
                    _error('The namespace lock is malformed.')
                continue
            if not JOB_ID.fullmatch(entry.name):
                _error('An incomplete preparation or unknown job remains; preserve it and review it before backup.')
            _plain(path, directory=True)
            jobs.append(path)
            if len(jobs) > MAX_JOBS:
                _error('Too many managed jobs.')
    return tuple(sorted(jobs))


def _walk(job, *, cancelled):
    files, aliases, pending, entries, total = {}, {}, [job], 0, 0
    while pending:
        folder = pending.pop()
        _plain(folder, directory=True)
        with os.scandir(folder) as children:
            for child in children:
                check_cancelled(cancelled)
                entries += 1
                if entries > MAX_ENTRIES:
                    _error('One job exceeds the file/directory entry budget.')
                path = Path(child.path)
                relative = path.relative_to(job).as_posix()
                relative_path(relative)
                alias = unicodedata.normalize('NFC', relative).casefold()
                if alias in aliases:
                    _error('Snapshot names alias one another on Windows.')
                aliases[alias] = relative
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    _plain(path, directory=True)
                    pending.append(path)
                else:
                    full = DIRECTORY + '/' + job.name + '/' + relative
                    info = _plain(path, linked=link_path(full))
                    total += info.st_size
                    if info.st_size > MAX_JOB_BYTES or total > MAX_SNAPSHOT_BYTES:
                        _error('The retained job exceeds its byte budget.')
                    files[relative] = path, info
    return files


def _exports(value, definition):
    _fields(value, OUTPUT_SETTINGS_KEYS, 'output settings')
    settings = validate_settings(dict(definition['settings'], **value))
    if any(settings[key] != value[key] for key in OUTPUT_SETTINGS_KEYS):
        _error('Output settings are invalid.')
    return value


def _owner(value, definition):
    _fields(value, ('job_dir', 'protocol'), 'output owner')
    path = _absolute(value['job_dir'])
    if path.name != definition['id'] or path.parent.name != DIRECTORY or value['protocol'] != PROTOCOL:
        _error('An output owner belongs to another managed job.')
    return value


def _request(value, definition):
    _fields(value, ('owner', 'selection', 'paths'), 'output request')
    _owner(value['owner'], definition)
    selection = value['selection']
    if type(selection) is not dict or set(selection) not in ({'inference', 'exports'}, {'batch', 'exports'}):
        _error('The output selection is invalid.')
    _exports(selection['exports'], definition)
    if 'inference' in selection:
        content_hash(selection['inference'])
    else:
        if type(selection['batch']) is not list or not 1 <= len(selection['batch']) <= MAX_INPUTS:
            _error('The output batch selection is invalid.')
        for key in selection['batch']:
            content_hash(key)
        if len(set(selection['batch'])) != len(selection['batch']):
            _error('The output batch repeats an inference.')
    paths = value['paths']
    if type(paths) is not list or not 1 <= len(paths) <= 4000:
        _error('The output path list exceeds its budget.')
    aliases = set()
    for path in paths:
        canonical = _output_path(path).as_posix()
        alias = unicodedata.normalize('NFC', canonical).casefold()
        if alias in aliases:
            _error('The output paths contain aliases.')
        aliases.add(alias)
    return value


def _output_entry(value, *, staged):
    expected = ('path', 'stage', 'bytes', 'sha256', 'identity') if staged else ('path', 'bytes', 'sha256', 'identity')
    _fields(value, expected, 'output entry')
    _output_path(value['path'])
    _count(value['bytes'], MAX_OUTPUT_BYTES, 'output byte count')
    content_hash(value['sha256'])
    identity = value['identity']
    if (type(identity) is not list or len(identity) != (4 if staged else 5)
            or any(type(part) is not int or part < 0 for part in identity)
            or identity[1] <= 0 or identity[2] != value['bytes']):
        _error('The historical publication identity is invalid.')
    if staged and (type(value['stage']) is not str or not _STAGE.fullmatch(value['stage'])):
        _error('The publication stage is not owned by this transaction.')
    return value


def _transaction(value, *, name, prefix, definition, staged):
    kind = 'output-intent' if staged else 'outputs'
    payload = validate_record(value, kind)
    member = 'entries' if staged else 'outputs'
    _fields(payload, ('request', member), 'publication record')
    request = _request(payload['request'], definition)
    if canonical_digest(request) != name or canonical_digest(request['owner'])[:24] != PurePosixPath(prefix).name:
        _error('The publication record name or owner does not match its content.')
    rows = payload[member]
    if type(rows) is not list or len(rows) != len(request['paths']):
        _error('The publication entry count differs from its request.')
    total, stages = 0, set()
    for row in rows:
        _output_entry(row, staged=staged)
        total += row['bytes']
        if total > MAX_OUTPUT_BYTES:
            _error('The publication transaction exceeds its output byte budget.')
        if staged:
            if row['stage'] in stages:
                _error('A publication stage is repeated.')
            stages.add(row['stage'])
    if [row['path'] for row in rows] != request['paths']:
        _error('The publication entries do not match their ordered request.')
    return payload


def _inventory(job, *, cancelled):
    definition = _definition(job)
    files = _walk(job, cancelled=cancelled)
    intents, stages, declared_outputs = {}, set(), set()
    for relative, (path, _) in files.items():
        result = _result_parts(PurePosixPath(relative))
        if result is None:
            continue
        prefix, tail = result
        if len(tail.parts) == 2 and tail.parts[0] == '.transactions' and tail.suffix == '.json':
            payload = _transaction(_read(path), name=tail.stem, prefix=prefix,
                                   definition=definition, staged=True)
            intents[relative] = payload
            for entry in payload['entries']:
                stages.add(prefix + '/' + entry['stage'])
                declared_outputs.add(prefix + '/' + entry['path'])
        elif len(tail.parts) == 2 and tail.parts[0] == '.receipts' and tail.suffix == '.json':
            payload = _transaction(_read(path), name=tail.stem, prefix=prefix,
                                   definition=definition, staged=False)
            declared_outputs.update(prefix + '/' + entry['path'] for entry in payload['outputs'])
    inputs = {row['path'] for row in definition['inputs']}
    if not inputs <= files.keys():
        _error('A retained input is missing.')
    selected = {}
    for relative, (path, info) in files.items():
        check_cancelled(cancelled)
        tail = PurePosixPath(relative)
        full = DIRECTORY + '/' + job.name + '/' + relative
        result = _result_parts(tail)
        stage = result is not None and bool(_STAGE.fullmatch(result[1].as_posix()))
        transient = (
            relative in _ROOT_TRANSIENT
            or relative == 'logs/.worker-v5.lock'
            or result is not None and result[1].as_posix() == '.publication.lock'
            or tail.parts[0] != 'inputs' and relative not in declared_outputs and bool(_TEMPORARY.fullmatch(tail.name))
            or len(tail.parts) == 4 and tail.parts[:3] == ('logs', 'integrity-v1', 'inputs')
                and re.fullmatch(r'input-[A-Za-z0-9_-]+', tail.stem) is not None and tail.suffix.lower() in EXTENSIONS
            or stage and relative not in stages)
        if transient:
            if info.st_nlink != 1:
                _error('A transient file has an unexpected hard-link alias.')
            continue
        if tail.parts[0] == 'inputs' and relative not in inputs:
            _error('An unregistered source remains in the owned-input directory.')
        if not is_state_path(full):
            _error('An unknown durable file remains in a managed job.')
        if not (0 if allows_empty(full) else 1) <= info.st_size <= state_limit(full):
            _error('A snapshot member exceeds its type-specific byte limit.')
        selected[relative] = path, info
    return definition, selected, intents


def state_paths(root, *, cancelled=lambda: False):
    paths, total = [], 0
    for job in _jobs(root, cancelled=cancelled):
        _, files, _ = _inventory(job, cancelled=cancelled)
        for path, info in files.values():
            check_cancelled(cancelled)
            paths.append(path)
            total += info.st_size
            if len(paths) > MAX_FILES or total > MAX_SNAPSHOT_BYTES:
                _error('Managed transcription state exceeds its snapshot budget.')
    return tuple(sorted(paths))


@contextmanager
def snapshot_guard(root):
    """Caller holds the app writer. The standalone worker lock is mandatory."""
    folder = Path(root) / DIRECTORY
    if not os.path.lexists(folder):
        yield
        return
    with project_lock(folder), ExitStack() as stack:
        jobs = _jobs(root)
        for job in jobs:
            # The supplied worker uses exactly byte zero / flock on this file,
            # including when launched independently of the desktop supervisor.
            stack.enter_context(project_lock(job / 'logs', lock_name='.worker-v5.lock'))
        if _jobs(root) != jobs:
            _error('The job inventory changed while snapshot locks were acquired.')
        yield


def internal_link_pairs(root, *, cancelled=lambda: False):
    """Only an intent's own stage/target pair can survive a POSIX link crash."""
    pairs = set()
    for job in _jobs(root, cancelled=cancelled):
        _, files, intents = _inventory(job, cancelled=cancelled)
        for relative, payload in intents.items():
            prefix, _ = _result_parts(PurePosixPath(relative))
            for entry in payload['entries']:
                names = (prefix + '/' + entry['stage'], prefix + '/' + entry['path'])
                if not all(name in files for name in names):
                    continue
                left, right = (files[name][1] for name in names)
                if left.st_nlink == 2 or right.st_nlink == 2:
                    expected = tuple(entry['identity'])
                    actual = left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns
                    if (_stamp(left) != _stamp(right) or left.st_nlink != 2 or actual != expected):
                        _error('The publication hard-link pair does not match its captured intent.')
                    pairs.add(frozenset(DIRECTORY + '/' + job.name + '/' + name for name in names))
    return pairs


def _verify_file(path, entry, cancelled):
    before = _plain(path, linked=True)
    if before.st_size != entry['bytes']:
        _error('A retained artifact changed its byte count.')
    if (digest_file(path, max_bytes=max(1, entry['bytes']), cancelled=cancelled) != entry['sha256']
            or _stamp(_plain(path, linked=True)) != _stamp(before)):
        _error('A retained artifact failed its content check.')


def _execution(value):
    _fields(value, ('schema', 'state', 'run_id', 'updated', 'exit_code'), 'execution record')
    valid_exit = (type(value['exit_code']) is int and value['exit_code'] == 0
                  if value['state'] == 'complete' else value['exit_code'] is None)
    if (value['schema'] != SCHEMA
            or value['state'] not in ('queued', 'verifying', 'running', 'paused', 'cancelled', 'failed', 'complete')
            or type(value['run_id']) is not str or not JOB_ID.fullmatch(value['run_id'])
            or not valid_exit):
        _error('The execution record is invalid.')
    if type(value['updated']) is not str:
        _error('The execution timestamp is invalid.')
    try:
        if datetime.fromisoformat(value['updated']).utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        _error('The execution timestamp is invalid.')


def _engine(value, definition, name):
    payload = validate_record(value, 'engine')
    _fields(payload, ('identity', 'execution', 'protocol'), 'engine record')
    if payload['protocol'] != PROTOCOL or canonical_digest(payload) != name:
        _error('The engine record does not match its registered name.')
    identity = _fields(payload['identity'], ('model_sha256', 'runtime_sha256', 'evidence'), 'engine identity')
    evidence = _fields(identity['evidence'], ('manifest_sha256', 'model_files', 'files'), 'registered runtime evidence')
    if (identity['model_sha256'] != definition['model_sha256']
            or identity['runtime_sha256'] != definition['runtime_manifest_sha256']
            or evidence['manifest_sha256'] != definition['runtime_manifest_sha256']):
        _error('Engine evidence differs from the immutable managed job.')
    inventory = evidence['model_files']
    if (type(inventory) is not dict or not {'model.bin', 'config.json', 'tokenizer.json'} <= inventory.keys()
            or not 1 <= len(inventory) <= 40960 or canonical_digest(inventory) != identity['model_sha256']):
        _error('The historical model inventory is invalid.')
    for path, checksum in inventory.items():
        relative_path(path)
        content_hash(checksum)
    files = evidence['files']
    if type(files) is not list or not 1 <= len(files) <= 40960:
        _error('The historical runtime inventory is invalid.')
    names, total = [], 0
    for row in files:
        _fields(row, ('path', 'bytes', 'sha256'), 'runtime inventory entry')
        names.append(relative_path(row['path']).as_posix())
        total += _count(row['bytes'], 64 * 1024**3, 'runtime inventory byte count')
        content_hash(row['sha256'])
    if names != sorted(set(names)) or total > 64 * 1024**3:
        _error('The historical runtime inventory is unsorted or exceeds its budget.')
    _execution_details(payload['execution'])
    return payload


def _execution_details(value):
    _fields(value, ('device', 'compute_type', 'device_index', 'num_workers'), 'engine execution')
    if (type(value['device']) is not str or value['device'] not in ('cpu', 'cuda')
            or type(value['compute_type']) is not str or not value['compute_type'] or len(value['compute_type']) > 64):
        _error('The observed execution device is invalid.')
    index = value['device_index']
    if type(index) is list:
        if not 1 <= len(index) <= 64:
            _error('The observed execution devices exceed their budget.')
        for part in index:
            _count(part, 65535, 'device index')
    else:
        _count(index, 65535, 'device index')
    _count(value['num_workers'], 20, 'worker count', empty=False)


def _inference(value, definition, name, engines, cancelled):
    payload = validate_record(value, 'inference')
    _fields(payload, ('context', 'text', 'detected_language', 'duration', 'segments', 'created'), 'inference record')
    context = _fields(payload['context'], ('source', 'settings', 'model_sha256', 'runtime_sha256',
                                          'engine_sha256', 'execution'), 'inference context')
    if canonical_digest(context) != name:
        _error('The inference name does not match its context.')
    if (context['model_sha256'] != definition['model_sha256']
            or context['runtime_sha256'] != definition['runtime_manifest_sha256']
            or context['engine_sha256'] not in engines
            or context['execution'] != engines[context['engine_sha256']]['execution']):
        _error('The inference runtime binding is invalid.')
    expected = {key: value for key, value in definition['settings'].items() if key not in OUTPUT_SETTINGS_KEYS}
    if context['settings'] != expected:
        _error('Inference settings differ from the managed job.')
    source = _fields(context['source'], ('path', 'root', 'relative', 'group', 'bytes', 'sha256'), 'inference source')
    candidates = [row for row in definition['inputs'] if all(source[key] == row[key]
                  for key in ('relative', 'bytes', 'sha256'))]
    original_root = _absolute(source['root'])
    matched = [row for row in candidates if tuple(original_root.parts[-2:]) == relative_path(row['root']).parts]
    if (len(matched) != 1 or _relative_to(source['path'], source['root']).as_posix() != source['relative']
            or original_root.parent.parent.name != definition['id']
            or original_root.parent.parent.parent.name != DIRECTORY):
        _error('The inference source is not a captured owned input.')
    if source['group'] != original_root.name:
        _error('The inference group differs from its captured input root.')
    _finite(payload['duration'], 'inference duration')
    if payload['detected_language'] is not None and (
            type(payload['detected_language']) is not str
            or not re.fullmatch('[a-z][a-z0-9-]{0,63}', payload['detected_language'])):
        _error('The detected language is invalid.')
    if type(payload['created']) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', payload['created']):
        _error('The inference timestamp is invalid.')
    segments = payload['segments']
    if type(segments) is not list or len(segments) > MAX_SEGMENTS:
        _error('The transcript segment count exceeds its budget.')
    lines, previous = [], -1.
    for segment in segments:
        check_cancelled(cancelled)
        _fields(segment, ('id', 'seek', 'start', 'end', 'text', 'words'), 'transcript segment')
        _count(segment['id'], 2**63 - 1, 'segment id')
        _count(segment['seek'], 2**63 - 1, 'segment seek')
        start, end = _finite(segment['start'], 'segment start'), _finite(segment['end'], 'segment end')
        if end < start or start < previous or type(segment['text']) is not str:
            _error('Transcript segment times or text are invalid.')
        previous = start
        if segment['text'].strip():
            lines.append(segment['text'].strip())
        words = segment['words']
        if type(words) is not list or len(words) > MAX_SEGMENTS:
            _error('The transcript word inventory exceeds its budget.')
        for word in words:
            _fields(word, ('start', 'end', 'word'), 'transcript word')
            start, end = _finite(word['start'], 'word start'), _finite(word['end'], 'word end')
            if end < start or type(word['word']) is not str:
                _error('Transcript word times or text are invalid.')
    if type(payload['text']) is not str or payload['text'] != '\n'.join(lines):
        _error('The transcript text differs from its captured segments.')
    return payload


def _completion(value, definition, files, cancelled):
    # Completion is a different public envelope than internal integrity records.
    _fields(value, ('schema', 'payload', 'sha256'), 'completion envelope')
    if value['schema'] != PROTOCOL or canonical_digest(value['payload']) != content_hash(value['sha256']):
        _error('The historical completion envelope is invalid.')
    payload = _fields(value['payload'], ('run_id', 'job_root', 'job_config_sha256', 'results_root',
        'output_settings', 'inputs', 'outputs'), 'completion payload')
    if type(payload['run_id']) is not str or not JOB_ID.fullmatch(payload['run_id']):
        _error('The historical completion run identity is invalid.')
    content_hash(payload['job_config_sha256'])
    owner = dict(job_dir=payload['job_root'], protocol=PROTOCOL)
    _owner(owner, definition)
    expected_root = PurePosixPath('results/verified-v43') / canonical_digest(owner)[:24]
    if _relative_to(payload['results_root'], payload['job_root']) != expected_root:
        _error('The historical completion result directory is not its owned namespace.')
    exports = _exports(payload['output_settings'], definition)
    if exports != {key: definition['settings'][key] for key in OUTPUT_SETTINGS_KEYS}:
        _error('The historical completion output settings differ from its immutable job.')
    inputs = payload['inputs']
    if type(inputs) is not list or len(inputs) != len(definition['inputs']):
        _error('The historical completion input inventory is invalid.')
    for row, expected in zip(inputs, definition['inputs']):
        _fields(row, ('path', 'root', 'relative', 'bytes', 'sha256', 'effective_settings',
                      'model_sha256', 'runtime_sha256'), 'completion input')
        if (any(row[key] != expected[key] for key in ('relative', 'bytes', 'sha256'))
                or _relative_to(row['path'], payload['job_root']).as_posix() != expected['path']
                or _relative_to(row['root'], payload['job_root']).as_posix() != expected['root']
                or row['effective_settings'] != definition['settings']
                or row['model_sha256'] != definition['model_sha256']
                or row['runtime_sha256'] != definition['runtime_manifest_sha256']):
            _error('The completion inputs differ from the immutable managed job.')
    rows = payload['outputs']
    if type(rows) is not list or not 1 <= len(rows) <= 4000:
        _error('The historical completion output count is invalid.')
    seen, coverage = set(), Counter()
    for row in rows:
        check_cancelled(cancelled)
        _fields(row, ('path', 'format', 'source_indexes', 'bytes', 'sha256'), 'completion output')
        relative = _output_path(row['path'])
        if row['format'] not in ('txt', 'srt', 'vtt', 'json') or relative.suffix.lower() != '.' + row['format']:
            _error('The historical output format is invalid.')
        indices = row['source_indexes']
        if (type(indices) is not list or not indices
                or any(type(index) is not int or not 0 <= index < len(inputs) for index in indices)
                or indices != sorted(set(indices))):
            _error('The historical output source bindings are invalid.')
        name = (expected_root / relative).as_posix()
        if name in seen or name not in files:
            _error('A historical completion output is missing or repeated.')
        seen.add(name)
        _count(row['bytes'], MAX_OUTPUT_BYTES, 'completion output size')
        content_hash(row['sha256'])
        _verify_file(files[name][0], row, cancelled)
        coverage[row['format'], tuple(indices)] += 1
    expected_coverage = Counter()
    for format in ('txt', 'srt', 'vtt', 'json'):
        if not payload['output_settings']['output_' + format]:
            continue
        groups = [(index,) for index in range(len(inputs))]
        if format == 'txt' and payload['output_settings']['output_mode'] == 'one_whole_job':
            groups = [tuple(range(len(inputs)))]
        elif format == 'txt' and payload['output_settings']['output_mode'] == 'one_per_folder':
            by_root = {}
            for index, row in enumerate(inputs):
                by_root.setdefault(row['root'], []).append(index)
            groups = [tuple(group) for group in by_root.values()]
        expected_coverage.update((format, group) for group in groups)
    if coverage != expected_coverage:
        _error('The historical output formats or source coverage are incomplete.')
    return payload


def verify_saved_completion(job, definition, *, cancelled=lambda: False):
    """Read accepted saved text after relocation; never execute an old config.

    Absolute strings in the saved receipt establish only lexical provenance.
    Only the current job's immutable input copies and declared result files are
    opened. Returned history is explicitly not a fresh inference attestation.
    """
    job = checked_directory(job)
    definition = validate_definition(definition, identity=job.name)
    watched = {}
    def captured(relative, maximum):
        check_cancelled(cancelled)
        path = job / relative
        before = _stamp(regular(path))
        value, checksum = _read_json_snapshot(path, max_bytes=maximum)
        if _stamp(regular(path)) != before:
            _error('A saved execution record changed while being read.')
        watched[path] = before
        return value, checksum
    current = read_definition(job, cancelled=cancelled)
    if current != definition:
        _error('The saved definition differs from its owned input inventory.')
    config, config_sha = captured('job_config.json', MAX_JSON_BYTES)
    status, _ = captured('job_status.json', 1024 * 1024)
    execution, _ = captured('execution.json', 65536)
    completion, completion_sha = captured('logs/completion-v1.json', MAX_JSON_BYTES)
    _execution(execution)
    if execution['state'] != 'complete' or execution['exit_code'] != 0:
        _error('No successfully completed saved execution is available.')
    _fields(config, ('run_id', 'job_name', 'folders', 'inputs', 'source_provenance',
                     'settings', 'results_root', 'models_dir', 'runtime_manifest',
                     'runtime_manifest_sha256'), 'archived job configuration')
    if (config['run_id'] != execution['run_id'] or config['job_name'] != definition['title']
            or config['runtime_manifest_sha256'] != definition['runtime_manifest_sha256']
            or config['settings'] != definition['settings']
            or config['source_provenance'] != definition['source_provenance']):
        _error('The archived configuration differs from its immutable managed definition.')
    _absolute(config['models_dir'])
    _absolute(config['runtime_manifest'])
    if (type(status) is not dict or status.get('protocol') != PROTOCOL
            or status.get('run_id') != execution['run_id']
            or status.get('state') not in ('completed', 'завершено')
            or status.get('completion_sha256') != completion_sha
            or any(name in status and (type(status[name]) is not int or status[name] != len(definition['inputs']))
                   for name in ('done', 'total'))):
        _error('The saved status does not confirm the exact retained completion receipt.')
    _fields(completion, ('schema', 'payload', 'sha256'), 'completion envelope')
    payload = completion['payload']
    if type(payload) is not dict:
        _error('The saved completion payload is invalid.')
    if (payload.get('run_id') != execution['run_id']
            or payload.get('job_config_sha256') != config_sha):
        _error('The saved completion is not bound to the exact archived configuration bytes.')
    original_root = payload.get('job_root')
    if _relative_to(config['results_root'], original_root).as_posix() != 'results':
        _error('The saved configuration did not select its owned results folder.')
    inputs = config['inputs']
    if type(inputs) is not list or len(inputs) != len(definition['inputs']):
        _error('The saved configuration input inventory is invalid.')
    for row, expected in zip(inputs, definition['inputs']):
        _fields(row, ('path', 'root', 'relative', 'bytes', 'sha256'), 'archived configuration input')
        if (any(row[key] != expected[key] for key in ('relative', 'bytes', 'sha256'))
                or _relative_to(row['path'], original_root).as_posix() != expected['path']
                or _relative_to(row['root'], original_root).as_posix() != expected['root']):
            _error('The archived configuration points outside its retained relative inputs.')
    if config['folders'] != list(dict.fromkeys(row['root'] for row in inputs)):
        _error('The archived folder selection does not match its retained inputs.')
    _, files, _ = _inventory(job, cancelled=cancelled)
    payload = _completion(completion, definition, files, cancelled)
    relative_results = _relative_to(payload['results_root'], original_root)
    results = job.joinpath(*relative_results.parts)
    _plain(results, directory=True)
    for row in payload['outputs']:
        # A moved history may be read, never used to adopt an old publication
        # lease. Finished output reads still require independent regular files.
        regular(results.joinpath(*relative_path(row['path']).parts))
    for path, identity in watched.items():
        check_cancelled(cancelled)
        if _stamp(regular(path)) != identity:
            _error('A saved execution record changed during validation.')
    if read_definition(job, cancelled=cancelled) != definition:
        _error('The retained inputs changed while validating saved output.')
    return dict(protocol=PROTOCOL, run_id=execution['run_id'], job_root=str(job),
        results_root=str(results), completion_sha256=completion_sha, job_config_sha256=config_sha,
        runtime_manifest_sha256=definition['runtime_manifest_sha256'],
        model_sha256=definition['model_sha256'], input_count=len(definition['inputs']),
        output_count=len(payload['outputs']), output_settings=dict(payload['output_settings']),
        outputs=[dict(row) for row in payload['outputs']], historical=True, runtime_current_verified=False)


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Validate retained data only. Never resolve an archived external path."""
    all_paths = state_paths(root, cancelled=cancelled)
    if len(all_paths) > MAX_FILES:
        _error('Too many snapshot members.')
    allowed_pairs = None
    for job in _jobs(root, cancelled=cancelled):
        definition, files, intents = _inventory(job, cancelled=cancelled)
        if read_definition(job, cancelled=cancelled) != definition:
            _error('The managed definition changed during validation.')
        engines, inferences, owners, output_records = {}, {}, {}, {}
        for relative, (path, _) in files.items():
            check_cancelled(cancelled)
            tail = PurePosixPath(relative)
            if tail.parts[:3] == ('logs', 'integrity-v1', 'engines'):
                engines[tail.stem] = _engine(_read(path), definition, tail.stem)
        for relative, (path, _) in files.items():
            check_cancelled(cancelled)
            tail = PurePosixPath(relative)
            if tail.parts[:3] == ('logs', 'integrity-v1', 'inference'):
                inferences[tail.stem] = _inference(_read(path), definition, tail.stem, engines, cancelled)
            elif tail.name == 'execution.json':
                _execution(_read(path, maximum=MAX_JSON_BYTES))
            elif tail.name == 'completion-v1.json':
                _completion(_read(path, maximum=MAX_JSON_BYTES), definition, files, cancelled)
            elif tail.parts[0] in ('history',) or relative in _ROOT_NAMES:
                _read(path, maximum=MAX_JSON_BYTES)  # Archived settings/config/status are inert JSON.
            result = _result_parts(tail)
            if result is None:
                continue
            prefix, name = result
            if name.as_posix() == 'owner.json':
                owner = _owner(validate_record(_read(path), 'owner'), definition)
                if canonical_digest(owner)[:24] != PurePosixPath(prefix).name:
                    _error('The retained output owner directory is invalid.')
                owners[prefix] = owner
            elif len(name.parts) == 2 and name.parts[0] == '.receipts':
                output_records[relative] = _transaction(_read(path), name=name.stem,
                    prefix=prefix, definition=definition, staged=False)
        for relative, payload in {**intents, **output_records}.items():
            prefix, name = _result_parts(PurePosixPath(relative))
            if prefix not in owners or owners[prefix] != payload['request']['owner']:
                _error('A publication record has no matching retained owner.')
            selection = payload['request']['selection']
            references = [selection['inference']] if 'inference' in selection else selection['batch']
            if any(reference not in inferences for reference in references):
                _error('A publication record has no retained inference.')
            staged = name.parts[0] == '.transactions'
            for row in payload['entries' if staged else 'outputs']:
                target = prefix + '/' + row['path']
                stage = prefix + '/' + row['stage'] if staged else None
                present = [key for key in (stage, target) if key is not None and key in files]
                if not present:
                    _error('A publication record lost both its stage and output.')
                if not staged and target not in files:
                    _error('A published output is missing.')
                for key in present:
                    _verify_file(files[key][0], row, cancelled)
        # Every actual alias must be exactly the fully owned, intent-declared
        # two-name crash window. Restore writes independent files instead.
        groups = {}
        for relative, (_, info) in files.items():
            if info.st_nlink > 1:
                groups.setdefault((info.st_dev, info.st_ino), []).append(relative)
        if groups and allowed_pairs is None:
            allowed_pairs = internal_link_pairs(root, cancelled=cancelled)
        for names in groups.values():
            full = frozenset(DIRECTORY + '/' + job.name + '/' + name for name in names)
            if len(names) != 2 or full not in allowed_pairs:
                _error('A hard-link alias extends outside its declared publication pair.')
