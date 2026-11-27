"""Strict decoder for the explicitly selected verified-file-job/1 protocol.

This module reads receipts from a separately reviewed local worker. It does not
import an ASR engine, execute worker code, locate a model, or download anything.
Synthetic fixtures prove file contracts, not acoustic quality.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from ..durable_state import local_path, regular
from .models import MAX_RESULT_BYTES, MAX_SEGMENTS, validated_result

PROTOCOL = 'verified-file-job/1'
MAX_RECEIPTS = 8192
MAX_SOURCES = 512
MAX_METADATA_BYTES = 2 * 1024**2
ROOT_DISTRIBUTIONS = frozenset(('faster-whisper', 'ctranslate2', 'av', 'numpy',
    'onnxruntime', 'tokenizers', 'huggingface-hub', 'packaging'))
RECOGNITION_FIELDS = frozenset((
    'task', 'translation_target', 'language', 'model', 'device', 'compute_type',
    'beam_size', 'vad_filter', 'multilingual', 'condition_on_previous_text',
))
EXPORT_FIELDS = frozenset((
    'output_mode', 'include_headers', 'output_txt', 'output_srt',
    'output_vtt', 'output_json',
))
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(canonical(value)).hexdigest()


def _object(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('Invalid ' + label + ' fields.')
    return value


def _integer(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('Invalid ' + label + '.')
    return value


def _hash(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError('Invalid protocol SHA-256.')
    return value


def _text(value, maximum, label):
    if not isinstance(value, str) or not value or len(value) > maximum or '\x00' in value:
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8', 'strict')
    return value


def _version(value, kind, keys):
    _object(value, {'kind', 'version', *keys}, kind)
    if value['kind'] != kind or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported ' + kind + ' version.')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate protocol JSON key.')
        result[key] = value
    return result


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_bytes(path, *, maximum=MAX_RESULT_BYTES):
    """Read exactly one stable, ordinary local file without following links."""
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size > maximum:
        raise ValueError('Protocol file exceeds its byte budget.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                         getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Protocol file changed while opening.')
        raw = stream.read(maximum + 1)
        if len(raw) > maximum or _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Protocol file changed while reading.')
    if _stamp(regular(path)) != _stamp(before) or len(raw) != before.st_size:
        raise ValueError('Protocol file changed during verification.')
    return raw


def decode(raw):
    return json.loads(raw.decode('utf-8', 'strict'), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError('Non-finite protocol JSON number.')))


def read_json(path, *, maximum=MAX_METADATA_BYTES):
    raw = read_bytes(path, maximum=maximum)
    return decode(raw), {'sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw)}


def relative_name(value):
    _text(value, 2048, 'relative artifact path')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or path.as_posix() != value or '\\' in value or ':' in value
            or any(part in ('', '.', '..') or part[-1:] in (' ', '.')
                   or any(ord(character) < 32 for character in part)
                   or part.split('.')[0].casefold() in (
                       'con', 'prn', 'aux', 'nul', *(f'com{x}' for x in range(1, 10)),
                       *(f'lpt{x}' for x in range(1, 10)))
                   for part in path.parts)):
        raise ValueError('Unsafe protocol artifact path.')
    return value


def _record(value):
    _object(value, ('path', 'sha256', 'size_bytes'), 'artifact identity')
    relative_name(value['path'])
    _hash(value['sha256'])
    _integer(value['size_bytes'], MAX_RESULT_BYTES, 'artifact byte count')
    return value


def _file(root, record):
    _record(record)
    path = local_path(Path(root) / record['path'], root=root, exists=True)
    raw = read_bytes(path)
    if sha256(raw).hexdigest() != record['sha256'] or len(raw) != record['size_bytes']:
        raise ValueError('A sealed worker artifact changed or is incomplete.')
    return path, raw


def _records(value, *, maximum):
    if type(value) is not list or not 1 <= len(value) <= maximum:
        raise ValueError('Invalid worker artifact inventory.')
    result = [_record(row) for row in value]
    if len({row['path'].casefold() for row in result}) != len(result):
        raise ValueError('Duplicate or case-aliased worker artifact path.')
    return result


def validate_packages(value):
    if type(value) is not list or not 1 <= len(value) <= 100:
        raise ValueError('Invalid complete runtime dependency inventory.')
    names = []
    for row in value:
        _object(row, ('name', 'version', 'sha256'), 'runtime distribution')
        if not isinstance(row['name'], str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', row['name']):
            raise ValueError('Invalid normalized runtime distribution name.')
        _text(row['version'], 256, 'runtime distribution version')
        _hash(row['sha256'])
        names.append(row['name'])
    if names != sorted(set(names)) or not ROOT_DISTRIBUTIONS <= set(names):
        raise ValueError('Runtime distributions must be uniquely sorted.')
    return value


def validate_implementation(value):
    if type(value) is not list or len(value) != 3:
        raise ValueError('Worker, helper and interpreter identities are all required.')
    names = []
    for row in value:
        _object(row, ('name', 'sha256', 'size_bytes'), 'runtime implementation')
        if not isinstance(row['name'], str) or not _NAME.fullmatch(row['name']):
            raise ValueError('Invalid implementation filename.')
        _hash(row['sha256'])
        if not 0 < _integer(row['size_bytes'], 256 * 1024**2, 'implementation byte count'):
            raise ValueError('An implementation file is empty.')
        names.append(row['name'])
    if len(set(names)) != 3 or names[1] != 'recovery_support.py':
        raise ValueError('The reviewed sibling helper identity is required.')
    return value


def model_identity(files):
    """Convert independently inspected model files to the worker tree digest."""
    if type(files) is not dict or not 4 <= len(files) <= 4096:
        raise ValueError('A complete local model inventory is required.')
    if not {'model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt'} <= set(files):
        raise ValueError('The selected local model pack is incomplete.')
    rows, total = [], 0
    for name, identity in sorted(files.items()):
        relative_name(name)
        _object(identity, ('sha256', 'size'), 'inspected model file')
        _hash(identity['sha256'])
        size = _integer(identity['size'], 64 * 1024**3, 'model file byte count')
        if name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt') and not size:
            raise ValueError('A required model file is empty.')
        total += size
        rows.append({'path': name, 'sha256': identity['sha256'], 'size_bytes': size})
    if total > 64 * 1024**3:
        raise ValueError('Model inventory exceeds its total byte budget.')
    return {'sha256': digest(rows), 'files': len(rows), 'size_bytes': total}


class DifferentRecipe(ValueError):
    """A valid-shaped checkpoint belongs to an older, unselected recipe."""


@dataclass(frozen=True)
class Checkpoint:
    source_path: str
    source_id: str
    source_locator: dict
    result: dict
    execution: dict
    request: dict
    request_sha256: str
    receipt: dict
    cache: dict
    files: tuple


class ReceiptReader:
    """Bind receipts to inspected bytes and app-owned retained inputs.

    sources maps absolute retained paths to records with id, sha256 and size.
    No path claimed by the worker is opened before matching that allowlist.
    results_root is status results_dir confined beneath worker_root/results.
    """
    def __init__(self, worker_root, results_root, sources, settings, profile):
        self.worker_root = local_path(worker_root, exists=True)
        self.results_root = local_path(results_root, root=self.worker_root / 'results', exists=True)
        regular(self.results_root, directory=True)
        if type(sources) is not dict or not 1 <= len(sources) <= MAX_SOURCES:
            raise ValueError('A bounded retained input allowlist is required.')
        self.sources = {}
        for path, row in sources.items():
            actual = local_path(path, exists=True)
            if str(actual) != path or type(row) is not dict:
                raise ValueError('Invalid retained source binding.')
            _text(row.get('id'), 100, 'retained source identity')
            _hash(row.get('sha256'))
            _integer(row.get('size'), 32 * 1024**3, 'retained source byte count')
            self.sources[path] = row
        if len({row['id'] for row in self.sources.values()}) != len(self.sources):
            raise ValueError('Duplicate retained source identity.')
        if type(settings) is not dict or not (RECOGNITION_FIELDS | EXPORT_FIELDS) <= set(settings):
            raise ValueError('Complete requested recognition and export settings are required.')
        self.recognition = {key: settings[key] for key in RECOGNITION_FIELDS}
        self.exports = {key: settings[key] for key in EXPORT_FIELDS}
        if canonical(self.exports) != canonical({
                'output_mode': 'separate', 'include_headers': False,
                'output_txt': True, 'output_srt': False,
                'output_vtt': False, 'output_json': True}):
            raise ValueError('This adapter requires the sealed separate TXT/JSON handoff.')
        if profile.get('protocol') != PROTOCOL:
            raise ValueError('The reviewed profile did not select this protocol.')
        self.model = model_identity(profile['model_files'])
        self.packages = validate_packages(profile['verified_packages'])
        self.implementation = validate_implementation(profile['verified_implementation'])

    def _source(self, value):
        _object(value, ('group', 'root', 'path', 'relative'), 'source locator')
        path = value['path']
        if not isinstance(path, str) or path not in self.sources:
            raise ValueError('The receipt is outside the retained input allowlist.')
        source = self.sources[path]
        if value['root'] != str(Path(path).parent) or value['relative'] != Path(path).name:
            raise ValueError('The receipt source locator does not match its retained input.')
        group = _text(value['group'], 512, 'source group')
        if any(character in group for character in '/\\:') or any(ord(character) < 32 for character in group):
            raise ValueError('Invalid source group.')
        from .runtime import file_identity
        if file_identity(path, maximum=32 * 1024**3) != {
                'sha256': source['sha256'], 'size': source['size']}:
            raise ValueError('Retained source bytes changed.')
        return source

    def _runtime(self, value):
        _object(value, ('python', 'platform', 'machine', 'packages', 'implementation'), 'runtime identity')
        for name in ('python', 'platform', 'machine'):
            _text(value[name], 2048, 'runtime diagnostic ' + name)
        packages = validate_packages(value['packages'])
        implementation = validate_implementation(value['implementation'])
        # Display strings are diagnostics, never an authorization shortcut.
        if (canonical(packages) != canonical(self.packages) or
                canonical(implementation) != canonical(self.implementation)):
            raise DifferentRecipe('The receipt does not match the complete reviewed runtime identity.')

    def _request(self, value, source):
        _version(value, 'whisper-transcribe-request',
                 ('source', 'model', 'runtime', 'recognition'))
        _object(value['source'], ('sha256', 'size_bytes'), 'request source')
        _object(value['model'], ('sha256', 'files', 'size_bytes'), 'request model')
        _object(value['recognition'], RECOGNITION_FIELDS, 'recognition settings')
        expected_source = {'sha256': source['sha256'], 'size_bytes': source['size']}
        if (canonical(value['source']) != canonical(expected_source) or
                canonical(value['model']) != canonical(self.model) or
                canonical(value['recognition']) != canonical(self.recognition)):
            raise DifferentRecipe('The receipt belongs to different source, model or settings bytes.')
        self._runtime(value['runtime'])

    def _result(self, value):
        _object(value, ('text', 'segments', 'detected_language', 'duration', 'execution'), 'recognition result')
        _object(value['execution'], ('device', 'compute_type'), 'effective execution')
        execution = value['execution']
        if execution['device'] != self.recognition['device']:
            raise ValueError('The worker changed the explicitly requested execution device.')
        accepted = {self.recognition['compute_type']}
        if self.recognition['compute_type'] == 'int8':
            accepted |= {'int8_float32', 'int8_float16', 'int8_bfloat16'}
        if execution['compute_type'] not in accepted:
            raise ValueError('The worker changed the requested computation profile.')
        if type(value['segments']) is not list or len(value['segments']) > MAX_SEGMENTS:
            raise ValueError('Invalid segment inventory.')
        for segment in value['segments']:
            _object(segment, ('id', 'seek', 'start', 'end', 'text', 'words'), 'segment')
            _integer(segment['id'], 2**63 - 1, 'segment identity')
            _integer(segment['seek'], 2**63 - 1, 'segment seek')
            if type(segment['words']) is not list:
                raise ValueError('Invalid word inventory.')
            for word in segment['words']:
                _object(word, ('start', 'end', 'word'), 'word')
        result = validated_result(value, value['text'])
        language = self.recognition['language']
        if language != 'auto' and result['language'] != language:
            raise ValueError('The worker changed the explicitly requested source language.')
        return result

    def read_checkpoint(self, receipt_record):
        """Recheck a sealed export receipt, its cache and its actual outputs."""
        receipt_path, raw = _file(self.worker_root, receipt_record)
        prefix = 'logs/export-receipts/'
        if not receipt_record['path'].startswith(prefix):
            raise ValueError('A per-file export receipt is required.')
        name = receipt_record['path'][len(prefix):]
        if not re.fullmatch(r'[0-9a-f]{64}\.json', name):
            raise ValueError('Invalid sealed export receipt filename.')
        receipt = decode(raw)
        _version(receipt, 'whisper-transcribe-export-receipt',
                 ('source', 'request_sha256', 'settings', 'files'))
        if name != digest(receipt) + '.json':
            raise ValueError('Export receipt filename does not match its canonical identity.')
        source = self._source(receipt['source'])
        if canonical(receipt['settings']) != canonical(self.exports):
            raise DifferentRecipe('The checkpoint belongs to different export settings.')
        request_hash = _hash(receipt['request_sha256'])
        relative = f'logs/verified-cache-v1/{request_hash[:2]}/{request_hash}.json'
        cache_path = local_path(self.worker_root / relative, root=self.worker_root, exists=True)
        cache, cache_identity = read_json(cache_path, maximum=MAX_RESULT_BYTES)
        _version(cache, 'whisper-transcribe-result',
                 ('request', 'request_sha256', 'result', 'result_sha256'))
        if (cache['request_sha256'] != request_hash or digest(cache['request']) != request_hash
                or _hash(cache['result_sha256']) != digest(cache['result'])):
            raise ValueError('Recognition receipt canonical hashes do not match.')
        self._request(cache['request'], source)
        result = self._result(cache['result'])
        files = _records(receipt['files'], maximum=2)
        if len(files) != 2 or {PurePosixPath(row['path']).suffix for row in files} != {'.txt', '.json'}:
            raise ValueError('The worker did not seal the required TXT and JSON exports.')
        for record in files:
            path, payload = _file(self.results_root, record)
            if path.suffix == '.txt':
                text = payload.decode('utf-8', 'strict')
                if text not in (result['text'], result['text'] + '\n'):
                    raise ValueError('Exported TXT bytes do not match the sealed segments.')
            else:
                export = decode(payload)
                _version(export, 'whisper-transcribe-export', ('source', 'request', 'result'))
                if (canonical(export['source']) != canonical(receipt['source']) or
                        canonical(export['request']) != canonical(cache['request']) or
                        canonical(export['result']) != canonical(cache['result'])):
                    raise ValueError('Exported JSON does not match the sealed source and result.')
        return Checkpoint(receipt['source']['path'], source['id'], dict(receipt['source']),
                          result, dict(cache['result']['execution']), cache['request'],
                          request_hash, dict(receipt_record),
                          {'path': relative, **cache_identity}, tuple(files))

    def checkpoints(self):
        """Yield selected, sealed checkpoints; loose cache entries are ignored."""
        folder = self.worker_root / 'logs' / 'export-receipts'
        if not os.path.lexists(folder):
            return
        folder = local_path(folder, root=self.worker_root, exists=True)
        regular(folder, directory=True)
        seen, count = set(), 0
        with os.scandir(folder) as entries:
            for entry in entries:
                count += 1
                if count > MAX_RECEIPTS:
                    raise ValueError('Worker checkpoint inventory exceeds its bound.')
                if entry.name.startswith('.'):
                    continue  # An unpublished atomic stage grants no admission.
                # Windows DirEntry.stat may report zero inode/link counts. Ask
                # the filesystem for this exact final path before admitting it.
                observed = Path(entry.path).lstat()
                if (not stat.S_ISREG(observed.st_mode) or getattr(observed, 'st_file_attributes', 0) & 0x400):
                    raise ValueError('A checkpoint must be an ordinary local file.')
                if observed.st_nlink != 1:
                    continue  # No admission during create-only publication; final reads remain strict.
                path = local_path(entry.path, root=folder, exists=True)
                regular(path)
                if not re.fullmatch(r'[0-9a-f]{64}\.json', entry.name):
                    raise ValueError('Unexpected worker export receipt filename.')
                value, identity = read_json(path)
                if (type(value) is dict and type(value.get('source')) is dict
                        and value['source'].get('path') not in self.sources):
                    continue  # A previous subset is not authority for this run.
                record = {'path': path.relative_to(self.worker_root).as_posix(), **identity}
                try:
                    checkpoint = self.read_checkpoint(record)
                except DifferentRecipe:
                    continue  # Never adopt an old configuration after rebinding.
                if checkpoint.source_path in seen:
                    raise ValueError('Ambiguous sealed checkpoints for the same retained input.')
                seen.add(checkpoint.source_path)
                yield checkpoint

    def completion(self, status, *, run_id=None, job_name=None):
        """Require exact completion.files + inputs, not a done counter alone."""
        if type(status) is not dict:
            raise ValueError('Invalid worker terminal status.')
        current_run = status.get('run_id')
        if not isinstance(current_run, str) or not re.fullmatch(r'[0-9a-f]{32}', current_run):
            raise ValueError('A fresh worker run identity is required.')
        if run_id is not None and current_run != run_id:
            raise ValueError('Worker run identity changed.')
        if (status.get('state') != 'завершено' or status.get('result_state') != 'completed'
                or type(status.get('failed')) is not int or status['failed'] != 0
                or type(status.get('done')) is not int or status['done'] != len(self.sources)
                or type(status.get('total')) is not int or status['total'] != len(self.sources)):
            raise ValueError('The worker did not report a complete selected run.')
        record = _record(status.get('completion'))
        if record['path'] != 'completion.json':
            raise ValueError('Invalid completion manifest reference.')
        _, raw = _file(self.worker_root, record)
        value = decode(raw)
        _version(value, 'whisper-transcribe-completion', ('job_name', 'run_id', 'files', 'inputs'))
        _text(value['job_name'], 512, 'job name')
        if value['run_id'] != current_run or (job_name is not None and value['job_name'] != job_name):
            raise ValueError('Completion belongs to a different worker run or job.')
        files = _records(value['files'], maximum=MAX_SOURCES * 2)
        inputs = value['inputs']
        if type(inputs) is not list or len(inputs) != len(self.sources):
            raise ValueError('Completion does not cover the selected input inventory.')
        accepted, union = {}, {}
        for item in inputs:
            _object(item, ('source', 'request_sha256', 'receipt', 'cache_reused'), 'completed input')
            if type(item['cache_reused']) is not bool:
                raise ValueError('Invalid cache reuse flag.')
            source = self._source(item['source'])
            checkpoint = self.read_checkpoint(item['receipt'])
            if (checkpoint.source_id != source['id'] or
                    checkpoint.source_path != item['source']['path'] or
                    checkpoint.request_sha256 != _hash(item['request_sha256'])):
                raise ValueError('Completed input does not match its sealed export receipt.')
            if canonical(checkpoint.source_locator) != canonical(item['source']):
                raise ValueError('Completed source locator changed.')
            if checkpoint.source_path in accepted:
                raise ValueError('Duplicate completed input.')
            accepted[checkpoint.source_path] = checkpoint
            for row in checkpoint.files:
                if row['path'] in union:
                    raise ValueError('Separate inputs cannot share a worker export path.')
                union[row['path']] = row
        if set(accepted) != set(self.sources) or canonical(sorted(files, key=lambda row: row['path'])) != canonical(
                sorted(union.values(), key=lambda row: row['path'])):
            raise ValueError('Completion artifact inventory is not the exact sealed file union.')
        return tuple(accepted[path] for path in sorted(accepted)), dict(record)


def result_directory(worker_root, status):
    """Confine a worker-reported namespace beneath the configured output root."""
    if type(status) is not dict or not isinstance(status.get('results_dir'), str):
        raise ValueError('The worker did not identify its result directory.')
    root = local_path(Path(worker_root) / 'results', exists=True)
    path = Path(status['results_dir'])
    if not path.is_absolute():
        raise ValueError('The worker result directory must be absolute.')
    result = local_path(path, root=root, exists=True)
    regular(result, directory=True)
    return result
