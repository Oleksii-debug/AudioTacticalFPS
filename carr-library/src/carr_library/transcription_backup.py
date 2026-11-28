"""Typed transcription state on the existing library sidecar protocol.

Only retained media, job/status/receipt JSON, outputs and edit history are durable.
Runtime homes, logs, temporary files and recognizer caches stay outside a snapshot.
Validation never executes a worker or reads the configured external runtime/model.
"""
from contextlib import ExitStack, contextmanager
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
from types import SimpleNamespace

from .backup_files import local_path, portable_relative, regular, optional_present
from .durable_state import read_json, state_lock
from .transcription import (TranscriptionService, MAX_SOURCE_BYTES, MAX_TEXT_BYTES, MEDIA_SUFFIXES,
                            RECEIPTS_PROTOCOL, V5_PROTOCOL, V5_SOURCES, V5_PROVENANCE,
                            V5_RECOGNITION_KEYS, _v5_settings, _v5_attempts)
from .transcription_host import AudioCancelled

DIRECTORY = 'state/transcription'
PREFIX = DIRECTORY + '/'
_JOB = r'[a-f0-9]{64}/jobs/[a-f0-9]{32}/'
_ROOT = re.compile(re.escape(PREFIX) + '(' + _JOB + ')(.+)\\Z')
_JOB_DIRECTORY = re.compile(re.escape(PREFIX) + _JOB[:-1] + r'\Z')
_WORKSPACE_DIRECTORY = re.compile(re.escape(PREFIX) + r'[a-f0-9]{64}\Z')
_SUBMIT_DIRECTORY = re.compile(re.escape(PREFIX) + r'[a-f0-9]{64}/jobs\Z')
JSON_LIMIT = 32 * 1024**2
MAX_FILES = 100000
# A portable member has at most sixteen components. Count directories and
# ignored entries too, before collecting/sorting, without scanning cache trees.
MAX_ENTRIES = MAX_FILES * 16 + 32


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Transcription backup validation cancelled.')


def ignored(name):
    if not isinstance(name, str) or not name.startswith(PREFIX):
        return False
    parts = PurePosixPath(name).parts
    if any(part.startswith('.pending-') for part in parts[2:]):
        return True
    if name == PREFIX + '.engine.lock' or re.fullmatch(
            re.escape(PREFIX) + r'[a-f0-9]{64}/jobs/\.submit\.lock', name):
        return True
    match = _ROOT.fullmatch(name)
    return bool(match and (match[2] == '.transcribe.lock'
        or match[2].split('/')[0] in ('logs', 'temporary', 'runtime-home')))


def state_limit(name):
    portable_relative(name)
    if name == PREFIX + 'engine.json':
        return JSON_LIMIT
    match = _ROOT.fullmatch(name)
    if match:
        rest = match[2]
        if rest in ('job.json', 'job_config.json', 'job_status.json', 'host_status.json', 'settings_live.json'):
            return JSON_LIMIT
        if rest in ('stop.flag', 'pause.flag'):
            return 128
        if (re.fullmatch(r'input/[0-9]{3}-[a-f0-9]{32}\.[a-z0-9]+', rest)
                and PurePosixPath(rest).suffix in MEDIA_SUFFIXES):
            return MAX_SOURCE_BYTES
        if re.fullmatch(r'receipts/[a-f0-9]{64}\.result\.json', rest):
            return JSON_LIMIT
        if re.fullmatch(r'edits/[a-f0-9]{32}\.json', rest):
            return JSON_LIMIT
        if rest.startswith('results/') and rest.endswith(('.txt', '.srt', '.vtt', '.json')):
            return 512 * 1024**2
    raise ValueError('Unknown transcription state file; backup was not completed: ' + str(name))


def is_state_path(name):
    if not isinstance(name, str) or not name.startswith(PREFIX):
        return False
    try:
        state_limit(name)
    except ValueError:
        return False
    return True


def _directory_allowed(name):
    if (_WORKSPACE_DIRECTORY.fullmatch(name) or _SUBMIT_DIRECTORY.fullmatch(name)
            or _JOB_DIRECTORY.fullmatch(name)):
        return True
    match = _ROOT.fullmatch(name)
    return bool(match and (match[2] in ('input', 'receipts', 'edits', 'results')
                           or match[2].startswith('results/')))


def _inventory(root, cancelled):
    root = Path(root)
    folder = root
    for part in DIRECTORY.split('/'):
        folder = folder / part
        if not optional_present(folder):
            return (), (), ()
        local_path(folder, root=root, exists=True)
        regular(folder, directory=True)
    files, jobs, submissions, pending, count = [], [], [], [folder], 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancelled)
                count += 1
                if count > MAX_ENTRIES:
                    raise ValueError('Transcription state exceeds its entry budget.')
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                portable_relative(name)
                is_directory = entry.is_dir(follow_symlinks=False)
                info = regular(path, directory=is_directory)
                if directory == folder and entry.name in ('profiles', 'jobs'):
                    # Earlier canonical ASR is a separate typed producer. The
                    # unified snapshot archives these siblings as inert history.
                    regular(path, directory=True)
                    continue
                if ignored(name):
                    if name.endswith('.lock') and is_directory:
                        raise ValueError('A transcription lock must be an ordinary file.')
                    continue
                if is_directory:
                    if not _directory_allowed(name):
                        raise ValueError('Unknown transcription state directory: ' + name)
                    if _JOB_DIRECTORY.fullmatch(name):
                        jobs.append(path)
                    if _SUBMIT_DIRECTORY.fullmatch(name):
                        submissions.append(path)
                    pending.append(path)
                    continue
                if not 0 <= info.st_size <= state_limit(name) or len(files) >= MAX_FILES:
                    raise ValueError('Transcription state exceeds its file or byte budget.')
                files.append(path)
    return tuple(sorted(files)), tuple(sorted(jobs)), tuple(sorted(submissions))


def state_paths(root, *, cancelled=lambda: False):
    """Enumerate only ordinary typed members, with bounded pre-sort traversal."""
    return _inventory(root, cancelled)[0]


@contextmanager
def snapshot_guard(root):
    """Borrow the caller's application writer; lock only this typed producer."""
    root = Path(root)
    directory = root / DIRECTORY
    if not optional_present(directory):
        yield
        return
    local_path(directory, root=root, exists=True)
    regular(directory, directory=True)
    # submit acquires engine before creating a workspace namespace. Holding it
    # first prevents new jobs appearing outside the sorted per-job lock set.
    with state_lock(directory, 'engine'), ExitStack() as stack:
        before = _inventory(root, lambda: False)
        for submission in before[2]:
            stack.enter_context(state_lock(submission, 'submit'))
        for job in before[1]:
            stack.enter_context(state_lock(job, 'transcribe'))
        if _inventory(root, lambda: False) != before:
            raise ValueError('Transcription state changed before backup; retry when idle.')
        yield
        if _inventory(root, lambda: False) != before:
            raise ValueError('Transcription state changed during backup; retry when idle.')


def _wrapper_hashes(value, worker_hash):
    return (type(value) is dict and set(value) == set(V5_SOURCES)
            and all(isinstance(item, str) and re.fullmatch('[a-f0-9]{64}', item)
                    for item in value.values())
            and value['worker.py'] == worker_hash)


def _engine(path):
    value = read_json(path)
    required = {'version', 'python', 'worker', 'models_dir', 'python_sha256',
                'worker_sha256', 'network', 'inference_qualified'}
    if type(value) is not dict:
        raise ValueError('Invalid passive transcription engine configuration.')
    protocol = value.get('protocol', RECEIPTS_PROTOCOL)
    if protocol not in (RECEIPTS_PROTOCOL, V5_PROTOCOL):
        raise ValueError('Unsupported passive transcription engine profile.')
    if 'protocol' in value:
        required.add('protocol')
    if protocol == V5_PROTOCOL:
        required.update(('wrapper_sources_sha256', 'retry_policy'))
        if (not _wrapper_hashes(value.get('wrapper_sources_sha256'), value.get('worker_sha256'))
                or not isinstance(value.get('retry_policy'), str)
                or not 1 <= len(value['retry_policy']) <= 4096 or '\0' in value['retry_policy']):
            raise ValueError('Invalid passive Whisper V5 source identities.')
    if (set(value) != required
            or type(value['version']) is not int or value['version'] != 1
            or value['inference_qualified'] is not False
            or any(not isinstance(value[key], str) or not value[key] or '\0' in value[key]
                   for key in ('python', 'worker', 'models_dir', 'network'))
            or any(not isinstance(value[key], str) or not re.fullmatch('[a-f0-9]{64}', value[key])
                   for key in ('python_sha256', 'worker_sha256'))):
        raise ValueError('Invalid passive transcription engine configuration.')


def _historical_v5_view(service, path, job, receipt, identity, context, cancelled):
    """Reconstruct only a passive old-run view from the edit-bound receipt.

    The model and runtime values are recorded provenance. No path from those
    descriptors is opened and no worker admission or file publication is run.
    """
    run_id = identity.get('host_run_id')
    manifest_hash = receipt.get('v5_manifest_sha256')
    runtime = identity['runtime']
    if (identity.get('protocol') != V5_PROTOCOL or receipt.get('run_id') != run_id
            or not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id)
            or not isinstance(manifest_hash, str) or not re.fullmatch('[a-f0-9]{64}', manifest_hash)
            or runtime.get('runtime_provenance') != V5_PROVENANCE
            or not _wrapper_hashes(runtime.get('wrapper_sources_sha256'), context['worker_sha256'])):
        raise ValueError('The retained Whisper V5 receipt lost its original run binding.')
    result_directory = 'runs/' + run_id + '/Whisper_V5/' + job['id']
    context = {**context, 'protocol': V5_PROTOCOL, 'host_run_id': run_id,
               'manifest_sha256': manifest_hash, 'result_directory': result_directory,
               'wrapper_sources_sha256': runtime['wrapper_sources_sha256'],
               'model_identity': identity['model']['identity']}
    effective = {'source': identity['source']['relative'], 'attempt': identity['attempt'],
                 'settings': identity['effective_settings']}
    passive = {**job, 'run_context': context, 'effective_runs': [effective]}
    _, manifest, outputs, _ = service._v5_manifest(path, passive, cancelled)
    index = receipt.get('v5_record')
    if type(index) is not int or not 0 <= index < len(manifest['records']):
        raise ValueError('The retained Whisper V5 receipt lost its manifest record.')
    row = manifest['records'][index]
    requested = _v5_settings(job['settings'], context['model_identity']['path'])
    selected = _v5_attempts(requested).get(effective['attempt'])
    allowed = ({key: selected[key] for key in V5_RECOGNITION_KEYS}
               | {'word_timestamps': requested['output_words']}) if selected else None
    source = identity['source']
    recorded_source = row.get('source_identity', {})
    if (row.get('model_identity') != context['model_identity']
            or recorded_source.get('path') != source['path']
            or recorded_source.get('sha256') != source['sha256']
            or recorded_source.get('size') != source['size_bytes']
            or row.get('requested_settings') != requested
            or row.get('attempt') != effective['attempt']
            or row.get('effective_settings') != effective['settings']
            or allowed is None or effective['settings'] != allowed
            or row.get('backend') != runtime.get('backend')
            or row.get('model_runtime') != identity.get('execution')):
        raise ValueError('The retained Whisper V5 record differs from the accepted recognition.')
    # Original formats must belong to this manifest row, not another retained
    # source/run. Word subtitle files are the host's existing derived exports.
    for artifact in receipt['artifacts']:
        relative = portable_relative(artifact['path']).relative_to(PurePosixPath(result_directory)).as_posix()
        if artifact['path'].endswith(('.words.srt', '.words.vtt')):
            extension = 'srt' if artifact['path'].endswith('.srt') else 'vtt'
            if relative != 'host-derived/' + source['relative'] + '.words.' + extension:
                raise ValueError('A retained Whisper V5 word export crossed its original run.')
        elif (relative not in row.get('artifacts', ()) or relative not in outputs
                or outputs[relative]['sha256'] != artifact['sha256']
                or outputs[relative]['bytes'] != artifact['size_bytes']):
            raise ValueError('A retained Whisper V5 output differs from its original manifest.')
    return passive

def _historical_artifacts(service, path, job, recognition, members, cancelled):
    """Recheck one edit's retained receipt without changing the saved job.

    An explicit resume may select another worker. The old receipt is still
    anchored by the edit's recognition hash, source seal and original TXT seal;
    its model/runtime identities and complete export profile must validate too.
    """
    name = 'receipts/' + recognition + '.result.json'
    receipt_path = path / name
    if receipt_path not in members:
        raise ValueError('Transcript edit lost its original source binding: retained receipt is missing.')
    try:
        receipt = read_json(receipt_path)
        identity = receipt['identity']
        if identity['sha256'] != recognition:
            raise ValueError('The retained receipt differs from the edit recognition identity.')
        source, runtime = identity['source'], identity['runtime']
        context = {'input_root': source['root'],
                   'worker_sha256': runtime['worker_sha256'],
                   'python_sha256': runtime['executable']['sha256']}
        if (not isinstance(context['input_root'], str) or not context['input_root']
                or '\0' in context['input_root']
                or any(not isinstance(context[key], str)
                       or not re.fullmatch('[a-f0-9]{64}', context[key])
                       for key in ('worker_sha256', 'python_sha256'))):
            raise ValueError('Invalid retained receipt execution identity.')
        status = {'run_id': receipt['run_id'], 'receipts': [name],
                  'done': 1, 'failed': len(job['sources']) - 1, 'total': len(job['sources'])}
        # This is a passive validation view, never _save or a promotion of the
        # stopped job's completion counters. _receipts checks actual media and
        # every required artifact against the immutable source/settings seals.
        passive = {**job, 'run_context': context}
        if job.get('protocol', RECEIPTS_PROTOCOL) == V5_PROTOCOL:
            passive = _historical_v5_view(service, path, job, receipt, identity, context, cancelled)
        _, artifacts = service._receipts(path, passive, status, cancelled)
        return artifacts
    except AudioCancelled as exc:
        raise InterruptedError('Transcription backup validation cancelled.') from exc
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as exc:
        raise ValueError('Transcript edit lost its original source binding: retained receipt or output is invalid.') from exc


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Validate saved scopes and seals passively against the copied catalog.

    Stopped/failed jobs retain unaccepted worker files for explicit resume; their
    presence does not promote them into verified exports. Completed/partial jobs
    must retain every artifact and receipt already accepted by the job service.
    """
    root = Path(root)
    paths, jobs, _ = _inventory(root, cancelled)
    by_job = {path: [] for path in jobs}
    for path in paths:
        _check(cancelled)
        name = path.relative_to(root).as_posix()
        if name == PREFIX + 'engine.json':
            _engine(path)
            continue
        match = _ROOT.fullmatch(name)
        job_path = root / (PREFIX + match[1])
        if job_path not in by_job:
            raise ValueError('Transcription state has no matching saved job.')
        by_job[job_path].append(path)
    for path, members in by_job.items():
        _check(cancelled)
        value = read_json(path / 'job.json')
        if type(value) is not dict:
            raise ValueError('Invalid transcription job.')
        workspace = value.get('workspace')
        expected = (PREFIX + sha256(workspace.encode('utf-8')).hexdigest()
                    + '/jobs/' + str(value.get('id'))) if isinstance(workspace, str) else None
        if path.relative_to(root).as_posix() != expected:
            raise ValueError('Transcription job path differs from its library or identity.')
        if db.execute('SELECT 1 FROM workspace WHERE id=?', (workspace,)).fetchone() is None:
            raise ValueError('Transcription job has no saved library.')

        @contextmanager
        def read(selected):
            if selected != workspace:
                raise ValueError('Transcription validation crossed its library scope.')
            yield db

        service = TranscriptionService(SimpleNamespace(root=root, read=read), workspace)
        job = service.get(value['id'])
        if job.get('status') not in ('ready', 'stopped', 'failed', 'partial', 'completed'):
            raise ValueError('Stop transcription before making a complete backup.')
        try:
            service._sources(path, job, cancelled)
            if job['status'] in ('completed', 'partial') or job.get('artifacts'):
                service.verify(job['id'], cancelled=cancelled)
        except AudioCancelled as exc:
            raise InterruptedError('Transcription backup validation cancelled.') from exc
        for member in members:
            _check(cancelled)
            if member.suffix == '.json':
                read_json(member)
        sources = {item['filename']: item for item in job['sources']}
        history, member_set = {}, set(members)
        for edit in service.edits(job['id']):
            _check(cancelled)
            source = sources.get(edit.get('source'))
            recognition, original_text = edit.get('recognition_identity'), edit.get('original_text_sha256')
            if (type(edit.get('version')) is not int or edit['version'] != 1 or source is None
                    or edit.get('source_sha256') != source['sha256']
                    or any(not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)
                           for value in (recognition, original_text))
                    or not isinstance(edit.get('text'), str)
                    or len(edit['text'].encode('utf-8')) > MAX_TEXT_BYTES or '\0' in edit['text']):
                raise ValueError('Transcript edit lost its original source binding.')
            def matches(row):
                return (row['source'] == edit['source'] and row['format'] == 'txt'
                        and row['recognition_identity'] == recognition and row['sha256'] == original_text)
            if any(matches(row) for row in job['artifacts']):
                continue
            if recognition not in history:
                history[recognition] = _historical_artifacts(
                    service, path, job, recognition, member_set, cancelled)
            if not any(matches(row) for row in history[recognition]):
                raise ValueError('Transcript edit lost its original source binding.')
