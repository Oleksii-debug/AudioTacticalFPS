"""Read-only acceptance of the supplied hardened V4.3 worker's public receipts.

This module contains no inference implementation or private worker source. A
successful process exit is insufficient: the terminal result, per-file record,
recognition receipt and requested exported bytes must agree with this run.
"""
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

from .supplied_whisper import HARDENED_PROTOCOL
from ..durable_state import local_path, read_json, regular

RECOGNITION_KEYS = ('model', 'device', 'compute_type', 'language', 'task',
                    'translation_target', 'beam_size', 'vad_filter',
                    'condition_on_previous_text', 'multilingual')
DISTRIBUTIONS = ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime',
                 'numpy', 'huggingface-hub', 'tokenizers')


def worker_digest(value):
    """Canonical digest declared by protocol carr-asr-v43-hardened-1."""
    return sha256((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                             allow_nan=False) + '\n').encode('utf-8')).hexdigest()


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid hardened worker content identity.')
    return value


def _runtime(value, manifest, worker_sha256):
    required = {'python', 'platform', 'executable_sha256', 'worker_sha256', 'packages', 'sha256'}
    if (not isinstance(value, dict) or set(value) != required
            or value['sha256'] != worker_digest({k: v for k, v in value.items() if k != 'sha256'})
            or value['worker_sha256'] != worker_sha256
            or value['executable_sha256'] != manifest['python_sha256']):
        raise ValueError('The hardened worker runtime receipt does not match its reviewed executable.')
    for name in ('python', 'platform'):
        if not isinstance(value[name], str) or not 1 <= len(value[name]) <= 4000:
            raise ValueError('Invalid hardened worker runtime declaration.')
    packages = value['packages']
    if not isinstance(packages, list) or len(packages) != len(DISTRIBUTIONS):
        raise ValueError('Missing hardened worker dependency receipts.')
    for package, name in zip(packages, DISTRIBUTIONS):
        if (not isinstance(package, dict) or set(package) != {'name', 'version', 'sha256'}
                or package['name'] != name or not isinstance(package['version'], str)
                or not 1 <= len(package['version']) <= 200):
            raise ValueError('Invalid hardened worker dependency receipt.')
        _sha(package['sha256'])


def verify_hardened_result(stage, snapshot, item, settings, manifest, status, *,
                           job_id, worker_sha256, cancelled=lambda: False):
    from .transcription import _bounded_bytes, _cancel
    terminal = read_json(stage / 'job_result.json')
    if (not isinstance(terminal, dict) or not isinstance(status, dict)
            or terminal.get('schema_version') != 1 or status.get('schema_version') != 1
            or terminal.get('protocol') != HARDENED_PROTOCOL or status.get('protocol') != HARDENED_PROTOCOL
            or terminal.get('job_id') != job_id or status.get('job_id') != job_id
            or not isinstance(terminal.get('run_id'), str)
            or not re.fullmatch('[0-9a-f]{32}', terminal['run_id'])
            or status.get('run_id') != terminal['run_id']
            or terminal.get('state') != 'succeeded' or status.get('result_state') != 'succeeded'
            or status.get('phase') != 'terminal'
            or terminal.get('results_dir') != str(stage / 'results')
            or status.get('results_dir') != terminal['results_dir']
            or terminal.get('errors') != []):
        raise ValueError('The hardened worker has no successful receipt for this run.')
    for name, expected in (('done', 1), ('total', 1), ('failed', 0), ('effective_parallel_files', 1)):
        if type(terminal.get(name)) is not int or terminal[name] != expected:
            raise ValueError('The hardened worker terminal counts do not agree.')
    records = terminal.get('files')
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError('The hardened worker did not report the selected single input.')
    record = records[0]
    source = {'path': str(snapshot), 'sha256': item['sha256'], 'bytes': item['bytes']}
    if (record.get('source') != source or record.get('state') != 'succeeded'
            or record.get('error') is not None or type(record.get('recognition_reused')) is not bool):
        raise ValueError('The hardened worker completed a different or failed source.')
    key = _sha(record.get('cache_key'))
    cache = read_json(stage / 'logs' / 'cache-v1' / key[:2] / (key + '.json'))
    if (not isinstance(cache, dict) or set(cache) != {'schema_version', 'cache_key', 'identity', 'result', 'result_sha256'}
            or cache['schema_version'] != 1 or cache['cache_key'] != key
            or cache['result_sha256'] != worker_digest(cache['result'])
            or key != worker_digest(cache['identity'])):
        raise ValueError('The hardened recognition receipt failed its identity check.')
    identity = cache['identity']
    recognition = {name: settings[name] for name in RECOGNITION_KEYS}
    if (not isinstance(identity, dict)
            or set(identity) != {'protocol', 'source', 'runtime', 'model', 'actual_runtime', 'recognition'}
            or identity['protocol'] != HARDENED_PROTOCOL or identity['source'] != source
            or identity['recognition'] != recognition):
        raise ValueError('The hardened recognition settings differ from the selected job.')
    _runtime(identity['runtime'], manifest, worker_sha256)
    files = []
    model_root = Path(manifest['models_directory']) / manifest['model']
    for relative in sorted(map(Path, manifest['model_files'])):
        _cancel(cancelled)
        path = local_path(model_root / relative, root=model_root, exists=True)
        files.append({'path': relative.as_posix(), 'bytes': regular(path).st_size,
                      'sha256': manifest['model_files'][relative.as_posix()]})
    if identity['model'] != {'sha256': worker_digest(files), 'files': files}:
        raise ValueError('The hardened worker model receipt differs from the reviewed model.')
    actual = identity['actual_runtime']
    allowed_compute = {settings['compute_type']}
    if settings['compute_type'] == 'int8':
        allowed_compute.add('int8_float32' if settings['device'] == 'cpu' else 'int8_float16')
    if (not isinstance(actual, dict) or set(actual) != {'device', 'compute_type', 'effective_parallel_files'}
            or actual['device'] != settings['device'] or actual['compute_type'] not in allowed_compute
            or type(actual['effective_parallel_files']) is not int or actual['effective_parallel_files'] != 1):
        raise ValueError('The hardened worker used a different inference device or compute type.')
    meta = cache['result']
    if (not isinstance(meta, dict) or meta.get('source') != source
            or meta.get('settings') != recognition or meta.get('selected_language') != settings['language']
            or meta.get('attempt') != 'requested-settings' or type(meta.get('attempt_no')) is not int
            or meta['attempt_no'] != 1 or meta.get('actual_runtime') != actual):
        raise ValueError('The hardened result differs from its source, settings or inference receipt.')
    artifacts = record.get('artifacts')
    required_formats = {format for format in ('txt', 'srt', 'vtt', 'json') if settings['output_' + format]}
    if (not isinstance(artifacts, list) or len(artifacts) != len(required_formats)
            or terminal.get('artifacts') != artifacts):
        raise ValueError('The hardened worker did not produce every requested format.')
    outputs = {}
    for artifact in artifacts:
        _cancel(cancelled)
        if not isinstance(artifact, dict) or set(artifact) != {'path', 'format', 'bytes', 'sha256'}:
            raise ValueError('Invalid hardened worker artifact record.')
        name, format = artifact['path'], artifact['format']
        if (not isinstance(name, str) or len(name) > 1000 or '\\' in name or ':' in name
                or not name or format not in required_formats or format in outputs):
            raise ValueError('Invalid hardened worker artifact path or format.')
        relative = PurePosixPath(name)
        if relative.is_absolute() or str(relative) != name or any(part in ('', '.', '..') for part in relative.parts):
            raise ValueError('The hardened worker artifact is outside its results directory.')
        path = local_path(stage / 'results' / name, root=stage / 'results', exists=True)
        raw = _bounded_bytes(path)
        if (type(artifact['bytes']) is not int or artifact['bytes'] != len(raw)
                or _sha(artifact['sha256']) != sha256(raw).hexdigest()):
            raise ValueError('A hardened worker artifact failed its byte identity check.')
        outputs[format] = path
    if set(outputs) != required_formats or 'json' not in outputs or read_json(outputs['json']) != meta:
        raise ValueError('The hardened JSON output differs from its recognition receipt.')
    return meta, outputs
