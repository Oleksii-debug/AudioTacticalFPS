"""Pure transcription admission and recovery contracts; no engine is imported.

A literal declaration describes a reviewed private adapter's protocol. It is
not proof of acoustic quality or permission to run an arbitrary downloaded
script. Current bytes and actual local model files are bound again at launch.
"""
import ast
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

from .durable_state import local_path, regular

PROTOCOL = 'carr-transcription-receipts/1'
DECLARATION = 'CARR_TRANSCRIPTION_CAPABILITIES'
CAPABILITY_KEYS = {'version', 'protocol', 'word_timestamps', 'multilingual',
                   'local_models_only', 'content_bound_cache', 'no_language_fallback'}
MAX_MODEL_BYTES = 32 * 1024**3
MAX_MODEL_FILES = 4096


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _path_text(value):
    return isinstance(value, str) and 0 < len(value) <= 32768 and '\0' not in value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def bound_identity(value):
    result = {key: item for key, item in value.items() if key != 'sha256'}
    raw = json.dumps(result, sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    return {**result, 'sha256': sha256(raw).hexdigest()}


def validate_capabilities(value):
    if (type(value) is not dict or set(value) != CAPABILITY_KEYS
            or type(value['version']) is not int or value['version'] != 1
            or value['protocol'] != PROTOCOL
            or any(type(value[key]) is not bool for key in CAPABILITY_KEYS - {'version', 'protocol'})
            or not all(value[key] for key in ('local_models_only', 'content_bound_cache', 'no_language_fallback'))):
        raise ValueError('The worker needs the reviewed offline receipt adapter; its protocol is incompatible.')
    return value


def declared_capabilities(source):
    """Read one literal declaration without importing or running private code."""
    if not isinstance(source, str) or len(source.encode('utf-8')) > 4 * 1024**2:
        raise ValueError('The selected worker exceeds the source review allowance.')
    found = []
    try:
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == DECLARATION for target in node.targets):
                found.append(ast.literal_eval(node.value))
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError('The selected worker has no valid literal receipt capability declaration.') from exc
    if len(found) != 1:
        raise ValueError('This worker is incompatible: select the reviewed receipt adapter, not the original unmodified worker.')
    return validate_capabilities(found[0])


def validate_engine_document(value):
    fields = {'version', 'python', 'worker', 'models_dir', 'python_sha256',
              'worker_sha256', 'network', 'inference_qualified'}
    optional = {'capabilities', 'restore_requires_revalidation'}
    if (type(value) is not dict or not fields <= set(value) or set(value) - fields - optional
            or type(value['version']) is not int or value['version'] != 1
            or any(not _path_text(value[key]) for key in ('python', 'worker', 'models_dir'))
            or any(not _hash(value[key]) for key in ('python_sha256', 'worker_sha256'))
            or type(value['inference_qualified']) is not bool or value['inference_qualified']
            or not isinstance(value['network'], str) or len(value['network']) > 300
            or type(value.get('restore_requires_revalidation', False)) is not bool):
        raise ValueError('Invalid transcription engine registration.')
    if 'capabilities' in value:
        validate_capabilities(value['capabilities'])
    return value


def check_requested_capabilities(config, options):
    capability = validate_capabilities(config.get('capabilities'))
    for key in ('word_timestamps', 'multilingual'):
        if options[key] and not capability[key]:
            raise ValueError('The selected worker does not support the requested option: ' + key)


def model_inventory(models_dir, options, *, cancelled=lambda: False):
    """Require a complete named local model, with bounded streamed byte hashes."""
    from .audio.models import AudioCancelled
    root = local_path(models_dir, exists=True)
    regular(root, directory=True)
    model = local_path(root / options['model'], root=root, exists=True)
    regular(model, directory=True)
    files, total = [], 0
    for directory, dirs, names in os.walk(model, followlinks=False):
        regular(directory, directory=True)
        for name in dirs:
            regular(Path(directory) / name, directory=True)
        for name in names:
            if cancelled():
                raise AudioCancelled('Local model verification was cancelled.')
            path = Path(directory) / name
            before = regular(path)
            relative = path.relative_to(model).as_posix()
            if (len(files) >= MAX_MODEL_FILES or before.st_size > MAX_MODEL_BYTES
                    or total + before.st_size > MAX_MODEL_BYTES):
                raise ValueError('The selected local model exceeds its verification allowance.')
            digest, count = sha256(), 0
            def stamp(info):
                return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
            with path.open('rb') as stream:
                if stamp(os.fstat(stream.fileno())) != stamp(before):
                    raise ValueError('Local model bytes changed before verification.')
                while block := stream.read(min(1024**2, before.st_size - count + 1)):
                    if cancelled():
                        raise AudioCancelled('Local model verification was cancelled.')
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Local model bytes grew during verification.')
                    digest.update(block)
                if stamp(os.fstat(stream.fileno())) != stamp(before):
                    raise ValueError('Local model bytes changed during verification.')
            if count != before.st_size or stamp(regular(path)) != stamp(before):
                raise ValueError('Local model bytes changed during verification.')
            files.append({'path': relative, 'size_bytes': count, 'sha256': digest.hexdigest()})
            total += count
    names = {item['path'] for item in files}
    if (not {'model.bin', 'config.json', 'tokenizer.json'} <= names
            or not names.intersection({'vocabulary.json', 'vocabulary.txt'})
            or any(item['size_bytes'] == 0 for item in files)):
        raise ValueError('Install the complete selected local model before transcription; nothing will be downloaded.')
    revision = None
    if 'revision.txt' in names:
        path = model / 'revision.txt'
        if path.stat().st_size > 80:
            raise ValueError('Invalid local model revision marker.')
        revision = path.read_text(encoding='ascii').strip()
        if not re.fullmatch('[0-9a-f]{40}', revision):
            raise ValueError('Invalid local model revision marker.')
    if options['model_revision'] is not None and revision != options['model_revision']:
        raise ValueError('The installed local model does not match the requested exact revision.')
    return bound_identity({'selected': options['model'], 'requested_revision': options['model_revision'],
                           'resolved_revision': revision, 'files': sorted(files, key=lambda item: item['path'])})


def validate_job_document(value, *, identity=None, workspace=None):
    from .transcription import MAX_FILES, MAX_SOURCE_BYTES, settings
    fields = {'version', 'id', 'workspace', 'title', 'created', 'updated', 'status',
              'sources', 'settings', 'done', 'failed', 'receipts', 'artifacts',
              'run_context', 'last_error'}
    optional = {'worker_run_id', 'restore_requires_review'}
    if (type(value) is not dict or not fields <= set(value) or set(value) - fields - optional
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['id'], str) or not re.fullmatch('[0-9a-f]{32}', value['id'])
            or not _path_text(value['workspace'])
            or (identity is not None and value['id'] != identity)
            or (workspace is not None and value['workspace'] != workspace)
            or not isinstance(value['title'], str) or not 1 <= len(value['title'].strip()) <= 300
            or '\0' in value['title'] or not all(_number(value[key]) for key in ('created', 'updated'))
            or value['status'] not in ('ready', 'running', 'completed', 'partial', 'failed', 'stopped')
            or type(value['sources']) is not list or not 1 <= len(value['sources']) <= MAX_FILES
            or type(value.get('restore_requires_review', False)) is not bool):
        raise ValueError('Invalid transcription job checkpoint.')
    settings(value['settings'])
    seen = set()
    for item in value['sources']:
        if (type(item) is not dict or set(item) != {'filename', 'original_name', 'size_bytes', 'sha256'}
                or not _path_text(item['filename']) or PurePosixPath(item['filename']).name != item['filename']
                or item['filename'] in ('.', '..') or '\\' in item['filename'] or ':' in item['filename']
                or item['filename'] in seen or not _path_text(item['original_name'])
                or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_SOURCE_BYTES
                or not _hash(item['sha256'])):
            raise ValueError('Invalid retained transcription source identity.')
        seen.add(item['filename'])
    for key in ('done', 'failed'):
        if type(value[key]) is not int or not 0 <= value[key] <= len(seen):
            raise ValueError('Invalid transcription job count.')
    if (type(value['receipts']) is not list or len(value['receipts']) > len(seen)
            or type(value['artifacts']) is not list or len(value['artifacts']) > 6 * len(seen)
            or not isinstance(value['last_error'], str) or len(value['last_error']) > 3000
            or value['run_context'] is not None and type(value['run_context']) is not dict):
        raise ValueError('Invalid transcription result checkpoint.')
    if value.get('worker_run_id') is not None and (
            not isinstance(value['worker_run_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['worker_run_id'])):
        raise ValueError('Invalid transcription run identity.')
    return value


def validate_timed_result(value, *, words, outcome):
    if type(value) is not dict or type(value.get('segments')) is not list or len(value['segments']) > 500000:
        raise ValueError('Transcription timed JSON has no valid bounded segment list.')
    text_segments = 0
    for segment in value['segments']:
        if (type(segment) is not dict or not _number(segment.get('start'))
                or not _number(segment.get('end')) or segment['end'] < segment['start']
                or not isinstance(segment.get('text'), str) or '\0' in segment['text']):
            raise ValueError('Transcription has invalid segment timestamps or text.')
        spoken = bool(segment['text'].strip())
        text_segments += spoken
        tokens = segment.get('words', [])
        if type(tokens) is not list or len(tokens) > 100000 or (words and spoken and not tokens):
            raise ValueError('Requested word timestamps were not produced.')
        previous = segment['start'] - .05
        for word in tokens:
            if (type(word) is not dict or not _number(word.get('start')) or not _number(word.get('end'))
                    or word['end'] < word['start'] or word['start'] < previous - .05
                    or word['start'] < segment['start'] - .05 or word['end'] > segment['end'] + .05
                    or not isinstance(word.get('word'), str) or not word['word'].strip() or '\0' in word['word']):
                raise ValueError('Transcription has invalid word timestamps or text.')
            previous = word['end']
    if (outcome == 'speech' and text_segments == 0) or (outcome == 'no_speech' and text_segments):
        raise ValueError('Transcription outcome differs from its actual timed text.')

