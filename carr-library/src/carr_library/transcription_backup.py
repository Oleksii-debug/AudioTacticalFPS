"""Typed transcription producer for the existing library sidecar manifest.

Retain media, job/status/receipt JSON, outputs and edit history. Runtime homes,
diagnostics, temporary files and recognizer caches are explicitly rebuildable.
Validation is passive: restoring state never executes a selected worker.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import PurePosixPath
import re
from types import SimpleNamespace

from .durable_state import read_json
from .transcription import TranscriptionService, MAX_SOURCE_BYTES, MAX_TEXT_BYTES, _copy
from .transcription_host import AudioCancelled

PREFIX = 'state/transcription/'
_JOB = r'[a-f0-9]{64}/jobs/[a-f0-9]{32}/'
_ROOT = re.compile(re.escape(PREFIX) + '(' + _JOB + ')(.+)\\Z')
JSON_LIMIT = 32 * 1024**2


def ignored(name):
    if not name.startswith(PREFIX):
        return False
    parts = PurePosixPath(name).parts
    if any(part.startswith('.pending-') for part in parts[2:]):
        return True
    if name == PREFIX + '.engine.lock' or re.fullmatch(re.escape(PREFIX) + r'[a-f0-9]{64}/jobs/\.submit\.lock', name):
        return True
    match = _ROOT.fullmatch(name)
    return bool(match and (match[2] == '.transcribe.lock'
        or match[2].split('/')[0] in ('logs', 'temporary', 'runtime-home')))


def limit(name):
    if name == PREFIX + 'engine.json':
        return JSON_LIMIT
    match = _ROOT.fullmatch(name)
    if match:
        rest = match[2]
        if rest in ('job.json', 'job_config.json', 'job_status.json', 'settings_live.json'):
            return JSON_LIMIT
        if rest in ('stop.flag', 'pause.flag'):
            return 128
        if re.fullmatch(r'input/[0-9]{3}-[a-f0-9]{32}\.[a-z0-9]+', rest):
            return MAX_SOURCE_BYTES
        if re.fullmatch(r'receipts/[a-f0-9]{64}\.result\.json', rest):
            return JSON_LIMIT
        if re.fullmatch(r'edits/[a-f0-9]{32}\.json', rest):
            return JSON_LIMIT
        if rest.startswith('results/') and rest.endswith(('.txt', '.srt', '.vtt', '.json')):
            return 512 * 1024**2
    raise ValueError('Unknown transcription state file; backup was not completed: ' + name)


def copy_entry(source, stage, name, *, expected=None, cancelled=lambda: False):
    try:
        record = _copy(source / name, stage / name, maximum=limit(name), cancelled=cancelled)
    except AudioCancelled as exc:
        raise InterruptedError('Transcription backup was cancelled.') from exc
    record['filename'] = name
    if expected is not None and record != expected:
        raise ValueError('Transcription sidecar digest or size mismatch.')
    return record


def validate_snapshot(db, root, entries, *, cancelled=lambda: False):
    entries = [entry for entry in entries if entry['filename'].startswith(PREFIX)]
    if not entries:
        return
    names = {entry['filename'] for entry in entries}
    for name in names:
        limit(name)
    engine = PREFIX + 'engine.json'
    if engine in names:
        value = read_json(root / engine)
        required = {'version', 'python', 'worker', 'models_dir', 'python_sha256',
                    'worker_sha256', 'network', 'inference_qualified'}
        if (not isinstance(value, dict) or set(value) != required
                or type(value['version']) is not int or value['version'] != 1
                or value['inference_qualified'] is not False
                or any(not isinstance(value[key], str) or not value[key] or '\0' in value[key]
                       for key in ('python', 'worker', 'models_dir', 'network'))
                or any(not isinstance(value[key], str) or not re.fullmatch('[a-f0-9]{64}', value[key])
                       for key in ('python_sha256', 'worker_sha256'))):
            raise ValueError('Invalid passive transcription engine configuration.')
    roots = {name[:-len('job.json')] for name in names if name.endswith('/job.json')}
    if any(name != engine and not any(name.startswith(prefix) for prefix in roots) for name in names):
        raise ValueError('Transcription state has no matching saved job.')
    for prefix in sorted(roots):
        if cancelled():
            raise InterruptedError('Transcription backup validation cancelled.')
        value = read_json(root / (prefix + 'job.json'))
        workspace = value.get('workspace')
        if (not isinstance(workspace, str)
                or prefix != PREFIX + sha256(workspace.encode('utf-8')).hexdigest() + '/jobs/' + str(value.get('id')) + '/'):
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
        path = service._path(job['id'])
        service._sources(path, job, cancelled)
        if job['status'] in ('completed', 'partial'):
            service.verify(job['id'], cancelled=cancelled)
        for name in names:
            if name.startswith(prefix) and name.endswith('.json'):
                read_json(root / name)
        sources = {item['filename']: item for item in job['sources']}
        for edit in service.edits(job['id']):
            source = sources.get(edit.get('source'))
            artifact = next((row for row in job['artifacts']
                if row['source'] == edit.get('source') and row['format'] == 'txt'), None)
            if (edit.get('version') != 1 or source is None
                    or edit.get('source_sha256') != source['sha256']
                    or artifact is None
                    or edit.get('recognition_identity') != artifact['recognition_identity']
                    or edit.get('original_text_sha256') != artifact['sha256']
                    or not isinstance(edit.get('text'), str)
                    or len(edit['text'].encode('utf-8')) > MAX_TEXT_BYTES):
                raise ValueError('Transcript edit lost its original source binding.')
